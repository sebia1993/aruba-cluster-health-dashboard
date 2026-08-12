from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from aruba_mini_dashboard.models import (
    CollectionError,
    ConnectionBaseline,
    ConnectionChange,
    ControllerState,
    DistributionState,
    IncidentTransitionKind,
    IncidentType,
    ParseIssue,
    ParseStatus,
    PollCycleResult,
    Severity,
)
from aruba_mini_dashboard.parsers import (
    parse_group_membership,
    parse_load_distribution,
    parse_show_switches,
)
from aruba_mini_dashboard.services.correlation_engine import (
    CorrelationEngine,
    InMemoryConnectionBaselineStore,
)
from aruba_mini_dashboard.services.incident_manager import IncidentManager


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
MEMBERS = {f"192.0.2.{value}": f"WLC-{value - 10:02}" for value in range(11, 15)}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_forget_known_mm_devices_never_removes_registered_scope() -> None:
    engine = CorrelationEngine(
        known_mm_devices={"192.0.2.11": "WLC-01", "192.0.2.99": "OLD-WLC"}
    )
    engine.reconcile_monitoring_scope(("192.0.2.11",))

    assert engine.forget_known_mm_devices(("192.0.2.11", "192.0.2.99")) == {
        "192.0.2.99"
    }
    assert engine.dump_known_mm_devices() == {"192.0.2.11": "WLC-01"}


def cycle(
    *,
    mm: str = "mm_show_switches_normal.txt",
    load: str = "cluster_load_normal.txt",
    membership: str = "group_membership_initial.txt",
    controller: str | None = "192.0.2.11",
    checked_at: datetime = NOW,
) -> PollCycleResult:
    return PollCycleResult(
        checked_at=checked_at,
        expected_cluster_members=MEMBERS,
        mm_result=parse_show_switches(fixture(mm)),
        load_result=parse_load_distribution(fixture(load)),
        membership_result=parse_group_membership(fixture(membership)),
        requested_cluster_controller_ip="192.0.2.11",
        actual_cluster_controller_ip=controller,
    )


def test_all_successful_healthy_sources_are_normal() -> None:
    health = CorrelationEngine().correlate(cycle())
    assert health.severity is Severity.NORMAL
    assert health.problem_ips == []
    assert health.primary_problem_ip is None
    assert all(device.severity is Severity.NORMAL for device in health.devices)


def test_mm_down_is_immediate_critical_for_exact_ip() -> None:
    health = CorrelationEngine().correlate(cycle(mm="mm_show_switches_down.txt"))
    assert health.severity is Severity.CRITICAL
    assert health.problem_ips == ["192.0.2.12"]
    device = health.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.mm_status == "Down"
    assert any("Status = Down" in reason for reason in device.issue_reasons)


def test_multiple_mm_down_members_are_all_reported() -> None:
    health = CorrelationEngine().correlate(cycle(mm="mm_show_switches_multiple_down.txt"))
    assert health.problem_ips == ["192.0.2.12", "192.0.2.13"]
    assert "2개" in health.summary


def test_parse_failure_is_unknown_and_never_down() -> None:
    health = CorrelationEngine().correlate(cycle(mm="malformed_output.txt"))
    assert health.severity is Severity.UNKNOWN
    assert health.problem_ips == []
    assert all(device.mm_status is None for device in health.devices)
    assert not any("Down" in reason for device in health.devices for reason in device.issue_reasons)


def test_mm_ssh_failure_marks_status_unknown_without_treating_collector_ip_as_down() -> None:
    poll = cycle()
    poll.mm_result = None
    poll.collection_errors = [
        CollectionError(
            source="show switches",
            code="AUTH_FAILED",
            user_message="MM 장비 로그인에 실패했습니다.",
            target_ip="198.51.100.10",
            occurred_at=NOW,
        )
    ]
    health = CorrelationEngine().correlate(poll)
    assert health.severity is Severity.UNKNOWN
    assert health.problem_ips == []
    assert health.device_by_ip("198.51.100.10") is None
    assert all(device.severity is Severity.UNKNOWN for device in health.devices)
    assert all(device.mm_status is None for device in health.devices)


def test_same_cluster_source_failure_is_one_incident_signal_not_one_per_command() -> None:
    poll = cycle()
    poll.load_result = None
    poll.membership_result = None
    poll.collection_errors = [
        CollectionError(
            source=source,
            code="AUTH_FAILED",
            user_message="Cluster 장비 로그인에 실패했습니다.",
            target_ip="192.0.2.11",
            occurred_at=NOW,
        )
        for source in (
            "show lc-cluster load distribution client",
            "show lc-cluster group-membership",
        )
    ]

    health = CorrelationEngine().correlate(poll)

    collection_signals = [
        signal for signal in health.signals if signal.incident_type.value == "collection_failure"
    ]
    assert len(health.collection_errors) == 2  # command detail is preserved
    assert len(collection_signals) == 1
    assert collection_signals[0].details["sources"] == [
        "show lc-cluster group-membership",
        "show lc-cluster load distribution client",
    ]


def test_all_three_parse_failures_are_unknown_without_a_problem_ip() -> None:
    poll = cycle(
        mm="malformed_output.txt",
        load="malformed_output.txt",
        membership="malformed_output.txt",
    )
    health = CorrelationEngine().correlate(poll)
    assert health.severity is Severity.UNKNOWN
    assert health.problem_ips == []
    assert health.primary_problem_ip is None
    assert len(health.collection_errors) >= 3


def test_valid_down_row_from_partial_parse_remains_usable_but_health_is_partial() -> None:
    poll = cycle(mm="mm_show_switches_down.txt")
    assert poll.mm_result is not None
    poll.mm_result.status = ParseStatus.PARTIAL
    poll.mm_result.issues.append(ParseIssue("BROKEN_ROW", "일부 행을 해석하지 못했습니다."))
    health = CorrelationEngine().correlate(poll)
    assert health.severity is Severity.CRITICAL
    assert health.primary_problem_ip == "192.0.2.12"
    assert health.partial is True


def test_partial_mm_up_does_not_recover_a_previous_down_incident() -> None:
    engine = CorrelationEngine()
    manager = IncidentManager()
    down_health = engine.correlate(cycle(mm="mm_show_switches_down.txt"))
    incident = next(
        item
        for item in manager.process(down_health)
        if item.incident.incident_type is IncidentType.MM_DOWN
    ).incident

    partial_up = cycle(checked_at=NOW + timedelta(minutes=1))
    assert partial_up.mm_result is not None
    partial_up.mm_result.status = ParseStatus.PARTIAL
    partial_up.mm_result.issues.append(ParseIssue("BROKEN_ROW", "untrusted trailing row"))
    deferred_health = engine.correlate(partial_up)

    assert deferred_health.severity is Severity.UNKNOWN
    assert deferred_health.problem_ips == []
    assert any(
        item.incident_type is IncidentType.MM_DOWN and item.ip == "192.0.2.12"
        for item in deferred_health.deferred_incidents
    )
    transitions = manager.process(deferred_health)
    assert not any(
        item.kind is IncidentTransitionKind.RECOVERED
        and item.incident.incident_id == incident.incident_id
        for item in transitions
    )
    assert manager.active_incidents()[0].incident_id == incident.incident_id

    trusted_up = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=2)))
    recovered = manager.process(trusted_up)
    assert any(
        item.kind is IncidentTransitionKind.RECOVERED
        and item.incident.incident_id == incident.incident_id
        for item in recovered
    )


def test_client_anomaly_activates_only_on_third_complete_cycle() -> None:
    engine = CorrelationEngine()
    for index in range(3):
        health = engine.correlate(
            cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=index))
        )
        if index < 2:
            assert health.problem_ips == []
    device = health.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.load_anomaly is True
    assert device.load_anomaly_streak == 3
    assert device.severity is Severity.WARNING


def test_active_client_anomaly_is_hidden_during_low_usage_without_false_recovery() -> None:
    engine = CorrelationEngine()
    manager = IncidentManager()
    for index in range(3):
        active_health = engine.correlate(
            cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=index))
        )
        manager.process(active_health)
    incident = next(
        item
        for item in manager.active_incidents()
        if item.incident_type is IncidentType.CLIENT_DISTRIBUTION
    )

    deferred_health = engine.correlate(
        cycle(load="cluster_load_all_low.txt", checked_at=NOW + timedelta(minutes=3))
    )
    device = deferred_health.device_by_ip("192.0.2.12")
    assert deferred_health.problem_ips == []
    assert deferred_health.severity is Severity.NORMAL
    assert device is not None
    assert device.load_anomaly is False
    assert device.load_anomaly_streak == 3
    assert not any(
        signal.incident_type is IncidentType.CLIENT_DISTRIBUTION
        for signal in deferred_health.signals
    )
    assert any(
        item.incident_type is IncidentType.CLIENT_DISTRIBUTION
        and item.ip == "192.0.2.12"
        for item in deferred_health.deferred_incidents
    )
    assert manager.process(deferred_health) == []
    assert next(item for item in manager.active_incidents() if item.incident_id == incident.incident_id)

    partial_poll = cycle(checked_at=NOW + timedelta(minutes=4))
    assert partial_poll.load_result is not None
    partial_poll.load_result.status = ParseStatus.PARTIAL
    partial_poll.load_result.issues.append(ParseIssue("BROKEN_ROW", "untrusted load row"))
    partial_health = engine.correlate(partial_poll)
    assert partial_health.severity is Severity.UNKNOWN
    assert partial_health.problem_ips == []
    assert partial_health.device_by_ip("192.0.2.12").load_anomaly is False  # type: ignore[union-attr]
    partial_transitions = manager.process(partial_health)
    assert not any(
        item.kind is IncidentTransitionKind.RECOVERED
        and item.incident.incident_id == incident.incident_id
        for item in partial_transitions
    )

    first_normal = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=5)))
    manager.process(first_normal)
    assert "192.0.2.12" in first_normal.problem_ips
    second_normal = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=6)))
    recovered = manager.process(second_normal)
    assert second_normal.problem_ips == []
    assert any(
        item.kind is IncidentTransitionKind.RECOVERED
        and item.incident.incident_id == incident.incident_id
        for item in recovered
    )


def test_all_unavailable_sources_hide_active_client_issue_but_preserve_incident() -> None:
    engine = CorrelationEngine()
    manager = IncidentManager(repeat_unacknowledged=True)
    for index in range(3):
        active_health = engine.correlate(
            cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=index))
        )
        manager.process(active_health)
    incident = next(
        item
        for item in manager.active_incidents()
        if item.incident_type is IncidentType.CLIENT_DISTRIBUTION
    )

    unavailable = cycle(checked_at=NOW + timedelta(minutes=20))
    unavailable.mm_result = None
    unavailable.load_result = None
    unavailable.membership_result = None
    unknown_health = engine.correlate(unavailable)

    assert unknown_health.severity is Severity.UNKNOWN
    assert unknown_health.problem_ips == []
    assert len(unknown_health.collection_errors) >= 3
    assert not any(
        signal.incident_type is IncidentType.CLIENT_DISTRIBUTION
        for signal in unknown_health.signals
    )
    transitions = manager.process(unknown_health)
    assert not any(
        item.kind is IncidentTransitionKind.RECOVERED
        and item.incident.incident_id == incident.incident_id
        for item in transitions
    )
    assert any(item.incident_id == incident.incident_id for item in manager.active_incidents())
    assert incident.incident_id not in {
        item.incident_id for item in manager.due_notifications(now=unknown_health.checked_at)
    }


def test_active_missing_incidents_are_deferred_when_their_source_is_untrusted() -> None:
    mm_missing = fixture("mm_show_switches_normal.txt").replace(
        "192.0.2.12       WLC-02            UP\n",
        "",
    )

    def missing_poll(source: str, checked_at: datetime) -> PollCycleResult:
        poll = cycle(checked_at=checked_at)
        if source == "mm":
            poll.mm_result = parse_show_switches(mm_missing)
        elif source == "load":
            poll.load_result = parse_load_distribution(fixture("cluster_load_missing_member.txt"))
        else:
            poll.membership_result = parse_group_membership(
                fixture("group_membership_missing_member.txt")
            )
        return poll

    cases = (
        ("mm", "mm_result", IncidentType.MM_MEMBER_MISSING),
        ("load", "load_result", IncidentType.LOAD_MEMBER_MISSING),
        ("membership", "membership_result", IncidentType.MEMBERSHIP_MEMBER_MISSING),
    )
    for source, result_attribute, incident_type in cases:
        engine = CorrelationEngine()
        manager = IncidentManager()
        manager.process(engine.correlate(cycle()))
        for index in range(3):
            missing_health = engine.correlate(
                missing_poll(source, NOW + timedelta(minutes=index + 1))
            )
            manager.process(missing_health)
        incident = next(
            item for item in manager.active_incidents() if item.incident_type is incident_type
        )

        failed_poll = cycle(checked_at=NOW + timedelta(minutes=5))
        setattr(failed_poll, result_attribute, None)
        deferred_health = engine.correlate(failed_poll)
        assert deferred_health.severity is Severity.UNKNOWN
        assert deferred_health.problem_ips == []
        assert not any(signal.incident_type is incident_type for signal in deferred_health.signals)
        assert any(
            item.incident_type is incident_type and item.ip == "192.0.2.12"
            for item in deferred_health.deferred_incidents
        )
        transitions = manager.process(deferred_health)
        assert not any(
            item.kind is IncidentTransitionKind.RECOVERED
            and item.incident.incident_id == incident.incident_id
            for item in transitions
        )
        assert any(item.incident_id == incident.incident_id for item in manager.active_incidents())

        partial_poll = cycle(checked_at=NOW + timedelta(minutes=6))
        partial_result = getattr(partial_poll, result_attribute)
        assert partial_result is not None
        partial_result.status = ParseStatus.PARTIAL
        partial_result.issues.append(ParseIssue("BROKEN_ROW", f"untrusted {source} row"))
        partial_health = engine.correlate(partial_poll)
        assert partial_health.severity is Severity.UNKNOWN
        assert partial_health.problem_ips == []
        assert not any(signal.incident_type is incident_type for signal in partial_health.signals)
        partial_transitions = manager.process(partial_health)
        assert not any(
            item.kind is IncidentTransitionKind.RECOVERED
            and item.incident.incident_id == incident.incident_id
            for item in partial_transitions
        )


def test_pending_connection_change_is_hidden_and_not_repeated_until_reconfirmed() -> None:
    engine = CorrelationEngine()
    manager = IncidentManager(repeat_unacknowledged=True)
    manager.process(engine.correlate(cycle()))
    changed = engine.correlate(
        cycle(
            membership="group_membership_changed.txt",
            checked_at=NOW + timedelta(minutes=1),
        )
    )
    incident = next(
        item
        for item in manager.process(changed)[0:1]
        if item.incident.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
    ).incident

    unavailable = cycle(checked_at=NOW + timedelta(minutes=19))
    unavailable.membership_result = None
    deferred_health = engine.correlate(unavailable)
    assert deferred_health.severity is Severity.UNKNOWN
    assert deferred_health.problem_ips == []
    assert not any(
        signal.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
        for signal in deferred_health.signals
    )
    assert any(
        item.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
        and item.ip == "192.0.2.12"
        and item.event_token == incident.event_token
        for item in deferred_health.deferred_incidents
    )
    transitions = manager.process(deferred_health)
    assert not any(item.kind is IncidentTransitionKind.RECOVERED for item in transitions)
    assert manager.due_notifications(now=deferred_health.checked_at) == []

    partial_poll = cycle(
        membership="group_membership_changed.txt",
        checked_at=NOW + timedelta(minutes=20),
    )
    assert partial_poll.membership_result is not None
    partial_poll.membership_result.status = ParseStatus.PARTIAL
    partial_poll.membership_result.issues.append(
        ParseIssue("BROKEN_ROW", "untrusted membership row")
    )
    partial_health = engine.correlate(partial_poll)
    assert partial_health.problem_ips == []
    assert any(
        item.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
        and item.event_token == incident.event_token
        for item in partial_health.deferred_incidents
    )
    manager.process(partial_health)
    assert manager.due_notifications(now=partial_health.checked_at) == []

    reconfirmed = engine.correlate(
        cycle(
            membership="group_membership_changed.txt",
            checked_at=NOW + timedelta(minutes=21),
        )
    )
    manager.process(reconfirmed)
    assert incident.incident_id in {
        item.incident_id for item in manager.due_notifications(now=reconfirmed.checked_at)
    }


def test_client_anomaly_and_connection_change_on_same_ip_escalate_to_critical() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())  # stores the member-IP membership baseline
    engine.correlate(cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=1)))
    engine.correlate(cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=2)))
    health = engine.correlate(
        cycle(
            load="cluster_load_abnormal.txt",
            membership="group_membership_changed.txt",
            checked_at=NOW + timedelta(minutes=3),
        )
    )
    device = health.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.severity is Severity.CRITICAL
    assert device.load_anomaly is True
    assert device.connection_type_changed is True


def test_anomalies_on_different_ips_remain_multiple_warnings() -> None:
    changed_thirteen = fixture("group_membership_initial.txt").replace(
        "192.0.2.13       Type-A", "192.0.2.13       Type-B"
    )
    engine = CorrelationEngine()
    engine.correlate(cycle())
    for index in range(2):
        engine.correlate(
            cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=index + 1))
        )
    poll = cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=3))
    poll.membership_result = parse_group_membership(changed_thirteen)
    health = engine.correlate(poll)
    assert set(health.problem_ips) == {"192.0.2.12", "192.0.2.13"}
    assert all(health.device_by_ip(ip).severity is Severity.WARNING for ip in health.problem_ips)  # type: ignore[union-attr]


def test_first_connection_values_create_baselines_without_notifications() -> None:
    store = InMemoryConnectionBaselineStore()
    health = CorrelationEngine(baseline_store=store).correlate(cycle())
    assert health.problem_ips == []
    assert len(store.values()) == 4


def test_connection_baseline_survives_engine_restart_via_repository() -> None:
    store = InMemoryConnectionBaselineStore()
    CorrelationEngine(baseline_store=store).correlate(cycle())
    restarted = CorrelationEngine(baseline_store=store)
    health = restarted.correlate(cycle(membership="group_membership_changed.txt"))
    device = health.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.previous_connection_type == "Type-A"
    assert device.connection_type == "Type-B"
    assert device.connection_type_changed is True


def test_member_baseline_detects_change_after_failover_and_keeps_collector_metadata() -> None:
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)
    engine.correlate(cycle(controller="192.0.2.11"))
    failover = engine.correlate(
        cycle(membership="group_membership_changed.txt", controller="192.0.2.13")
    )
    device = failover.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.previous_connection_type == "Type-A"
    assert device.connection_type == "Type-B"
    assert device.connection_type_changed is True
    assert failover.problem_ips == ["192.0.2.12"]
    assert len(store.values()) == 4
    assert store.get("192.0.2.12").collector_ip == "192.0.2.13"  # type: ignore[union-attr]
    change = engine.pending_connection_changes()[0]
    assert change.member_ip == "192.0.2.12"
    assert change.collector_ip == "192.0.2.13"


def test_refailover_keeps_one_pending_change_per_member_and_original_event_collector() -> None:
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)
    engine.correlate(cycle(controller="192.0.2.11"))
    first = engine.correlate(
        cycle(membership="group_membership_changed.txt", controller="192.0.2.13")
    )
    first_change = engine.pending_connection_changes()[0]

    returned = engine.correlate(
        cycle(
            membership="group_membership_changed.txt",
            controller="192.0.2.11",
            checked_at=NOW + timedelta(minutes=1),
        )
    )

    assert first.problem_ips == returned.problem_ips == ["192.0.2.12"]
    assert len(engine.pending_connection_changes()) == 1
    retained = engine.pending_connection_changes()[0]
    assert retained.event_token == first_change.event_token
    assert retained.collector_ip == "192.0.2.13"
    assert retained.last_confirmed_at == NOW + timedelta(minutes=1)
    assert store.get("192.0.2.12").collector_ip == "192.0.2.11"  # type: ignore[union-attr]


def test_same_connection_change_is_retained_without_creating_a_new_event() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    first = engine.correlate(cycle(membership="group_membership_changed.txt"))
    token = next(
        signal.event_token
        for signal in first.signals
        if signal.ip == "192.0.2.12" and signal.event_token
    )
    second = engine.correlate(
        cycle(membership="group_membership_changed.txt", checked_at=NOW + timedelta(minutes=1))
    )
    assert next(
        signal.event_token
        for signal in second.signals
        if signal.ip == "192.0.2.12" and signal.event_token
    ) == token
    assert len(engine.pending_connection_changes()) == 1


def test_connection_return_to_previous_value_creates_a_new_change_event() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    first = engine.correlate(cycle(membership="group_membership_changed.txt"))
    first_token = next(signal.event_token for signal in first.signals if signal.event_token)
    returned = engine.correlate(cycle(membership="group_membership_initial.txt"))
    returned_signal = next(
        signal for signal in returned.signals if signal.ip == "192.0.2.12" and signal.event_token
    )
    assert returned_signal.event_token != first_token
    assert "Type-B" in returned_signal.reason and "Type-A" in returned_signal.reason


def test_pending_connection_change_can_be_restored_after_restart() -> None:
    store = InMemoryConnectionBaselineStore()
    first_engine = CorrelationEngine(baseline_store=store)
    first_engine.correlate(cycle())
    first_engine.correlate(cycle(membership="group_membership_changed.txt"))
    restarted = CorrelationEngine(
        baseline_store=store,
        pending_connection_changes=first_engine.pending_connection_changes(),
    )
    health = restarted.correlate(cycle(membership="group_membership_changed.txt"))
    assert health.device_by_ip("192.0.2.12").connection_type_changed is True  # type: ignore[union-attr]


def test_acknowledged_connection_change_leaves_monitoring_active_but_clears_warning() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert engine.acknowledge_connection_change("192.0.2.12") is True
    health = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert health.problem_ips == []
    assert health.device_by_ip("192.0.2.12").connection_type == "Type-B"  # type: ignore[union-attr]


def test_connection_format_only_difference_is_not_a_change() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    formatted = fixture("group_membership_initial.txt").replace("Type-A", " type a ")
    poll = cycle()
    poll.membership_result = parse_group_membership(formatted)
    health = engine.correlate(poll)
    assert health.problem_ips == []


def test_load_member_missing_is_debounced_and_recovers_after_two_present_cycles() -> None:
    engine = CorrelationEngine()
    for index in range(3):
        health = engine.correlate(
            cycle(load="cluster_load_missing_member.txt", checked_at=NOW + timedelta(minutes=index))
        )
        if index < 2:
            assert "192.0.2.12" not in health.problem_ips
    assert health.device_by_ip("192.0.2.12").severity is Severity.WARNING  # type: ignore[union-attr]
    first = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=3)))
    assert "192.0.2.12" in first.problem_ips
    second = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=4)))
    assert "192.0.2.12" not in second.problem_ips


def test_membership_missing_is_not_reported_as_connection_change() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    for index in range(3):
        health = engine.correlate(
            cycle(
                membership="group_membership_missing_member.txt",
                checked_at=NOW + timedelta(minutes=index + 1),
            )
        )
    device = health.device_by_ip("192.0.2.12")
    assert device is not None
    assert device.severity is Severity.WARNING
    assert device.connection_type_changed is False
    assert any("Membership 행 누락" in reason for reason in device.issue_reasons)


def test_dynamic_mm_ip_is_inventory_only_and_never_advances_missing_state() -> None:
    extra = fixture("mm_show_switches_normal.txt").replace(
        "\nTotal Switches: 4", "\n192.0.2.99       EDGE-MM            Up\n\nTotal Switches: 5"
    )
    engine = CorrelationEngine()
    first_poll = cycle()
    first_poll.mm_result = parse_show_switches(extra)
    first = engine.correlate(first_poll)
    assert first.device_by_ip("192.0.2.99") is not None
    for index in range(3):
        health = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=index + 1)))
    dynamic = health.device_by_ip("192.0.2.99")
    assert dynamic is not None
    assert dynamic.is_registered is False
    assert dynamic.controller_state is ControllerState.MISSING
    assert dynamic.severity is Severity.NORMAL
    assert health.problem_ips == []
    assert not dynamic.collection_errors  # no Cluster data is expected for MM-only devices
    first_recovery = engine.correlate(first_poll)
    assert first_recovery.device_by_ip("192.0.2.99").severity is Severity.NORMAL  # type: ignore[union-attr]


def test_unregistered_mm_down_is_visible_but_excluded_from_health_and_incidents() -> None:
    extra = fixture("mm_show_switches_normal.txt").replace(
        "\nTotal Switches: 4",
        "\n192.0.2.99       EDGE-MM            Down\n\nTotal Switches: 5",
    )
    poll = cycle()
    poll.mm_result = parse_show_switches(extra)

    health = CorrelationEngine().correlate(poll)

    inventory = health.device_by_ip("192.0.2.99")
    assert health.monitoring_scope_ips == tuple(MEMBERS)
    assert health.severity is Severity.NORMAL
    assert health.problem_ips == []
    assert inventory is not None
    assert inventory.is_registered is False
    assert inventory.controller_state is ControllerState.DOWN
    assert inventory.mm_status == "Down"
    assert inventory.signals == []
    assert IncidentManager().process(health) == []


def test_unregistered_membership_row_never_creates_or_changes_a_baseline() -> None:
    initial = fixture("group_membership_initial.txt").replace(
        "\n(WLC-01) #",
        "\n192.0.2.99       Type-Z\n(WLC-01) #",
    )
    changed = initial.replace("Type-Z", "Type-Y")
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)

    first_poll = cycle()
    first_poll.membership_result = parse_group_membership(initial)
    first = engine.correlate(first_poll)
    second_poll = cycle(checked_at=NOW + timedelta(minutes=1))
    second_poll.membership_result = parse_group_membership(changed)
    second = engine.correlate(second_poll)

    inventory = second.device_by_ip("192.0.2.99")
    assert inventory is not None and inventory.is_registered is False
    assert inventory.connection_type == "Type-Y"
    assert store.get("192.0.2.99") is None
    assert not any(signal.ip == "192.0.2.99" for signal in first.signals + second.signals)


def test_scope_removal_prunes_restored_baseline_and_pending_change() -> None:
    baseline = ConnectionBaseline(
        collector_ip="192.0.2.99",
        member_ip="192.0.2.99",
        display_value="Type-A",
        normalized_value="type a",
        observed_at=NOW,
    )
    change = ConnectionChange(
        collector_ip="192.0.2.99",
        member_ip="192.0.2.99",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=NOW,
        last_confirmed_at=NOW,
    )
    store = InMemoryConnectionBaselineStore([baseline])
    engine = CorrelationEngine(
        baseline_store=store,
        pending_connection_changes=[change],
    )

    health = engine.correlate(cycle())

    assert health.monitoring_scope_ips == tuple(MEMBERS)
    assert store.get("192.0.2.99") is None
    assert engine.pending_connection_changes() == ()
    assert not any(signal.ip == "192.0.2.99" for signal in health.signals)


def test_removed_then_readded_member_uses_first_membership_as_a_new_baseline() -> None:
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)
    engine.correlate(cycle())

    removed_poll = cycle(checked_at=NOW + timedelta(minutes=1))
    removed_poll.expected_cluster_members = {
        ip: alias for ip, alias in MEMBERS.items() if ip != "192.0.2.12"
    }
    removed = engine.correlate(removed_poll)
    assert removed.device_by_ip("192.0.2.12").is_registered is False  # type: ignore[union-attr]
    assert store.get("192.0.2.12") is None

    readded = engine.correlate(
        cycle(
            membership="group_membership_changed.txt",
            checked_at=NOW + timedelta(minutes=2),
        )
    )
    target = readded.device_by_ip("192.0.2.12")
    assert target is not None and target.is_registered is True
    assert target.connection_type == "Type-B"
    assert target.connection_type_changed is False
    assert store.get("192.0.2.12").display_value == "Type-B"  # type: ignore[union-attr]


def test_scope_removal_resets_client_anomaly_streak_before_readd() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle(load="cluster_load_abnormal.txt"))
    second = engine.correlate(
        cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=1))
    )
    assert second.device_by_ip("192.0.2.12").load_anomaly_streak == 2  # type: ignore[union-attr]

    removed_poll = cycle(checked_at=NOW + timedelta(minutes=2))
    removed_poll.expected_cluster_members = {
        ip: alias for ip, alias in MEMBERS.items() if ip != "192.0.2.12"
    }
    engine.correlate(removed_poll)
    readded = engine.correlate(
        cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=3))
    )

    target = readded.device_by_ip("192.0.2.12")
    assert target is not None
    assert target.distribution_state is DistributionState.OBSERVING
    assert target.load_anomaly_streak == 1
    assert target.load_anomaly is False


def test_distribution_state_exposes_observation_anomaly_recovery_and_low_usage() -> None:
    engine = CorrelationEngine()
    observing = engine.correlate(cycle(load="cluster_load_abnormal.txt"))
    assert observing.device_by_ip("192.0.2.12").distribution_state is DistributionState.OBSERVING  # type: ignore[union-attr]
    engine.correlate(
        cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=1))
    )
    anomalous = engine.correlate(
        cycle(load="cluster_load_abnormal.txt", checked_at=NOW + timedelta(minutes=2))
    )
    assert anomalous.device_by_ip("192.0.2.12").distribution_state is DistributionState.ANOMALOUS  # type: ignore[union-attr]
    recovering = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=3)))
    assert recovering.device_by_ip("192.0.2.12").distribution_state is DistributionState.RECOVERING  # type: ignore[union-attr]
    recovered = engine.correlate(cycle(checked_at=NOW + timedelta(minutes=4)))
    assert recovered.device_by_ip("192.0.2.12").distribution_state is DistributionState.NORMAL  # type: ignore[union-attr]

    low_usage = CorrelationEngine().correlate(cycle(load="cluster_load_all_low.txt"))
    assert all(
        device.distribution_state is DistributionState.LOW_USAGE
        for device in low_usage.devices
        if device.is_registered
    )


def test_distribution_missing_state_is_observing_until_confirmed() -> None:
    engine = CorrelationEngine()
    first = engine.correlate(cycle(load="cluster_load_missing_member.txt"))
    assert first.device_by_ip("192.0.2.12").distribution_state is DistributionState.OBSERVING  # type: ignore[union-attr]
    engine.correlate(
        cycle(load="cluster_load_missing_member.txt", checked_at=NOW + timedelta(minutes=1))
    )
    confirmed = engine.correlate(
        cycle(load="cluster_load_missing_member.txt", checked_at=NOW + timedelta(minutes=2))
    )
    assert confirmed.device_by_ip("192.0.2.12").distribution_state is DistributionState.MISSING  # type: ignore[union-attr]


def test_low_overall_usage_is_normal_with_an_explicit_note() -> None:
    health = CorrelationEngine().correlate(cycle(load="cluster_load_all_low.txt"))
    assert health.severity is Severity.NORMAL
    assert health.problem_ips == []
    assert any("낮은 전체 사용량" in note for note in health.notes)


def test_unrecognized_mm_status_makes_overall_health_unknown() -> None:
    unusual = fixture("mm_show_switches_normal.txt").replace(
        "192.0.2.11       WLC-01            Up", "192.0.2.11       WLC-01            Maintenance"
    )
    poll = cycle()
    poll.mm_result = parse_show_switches(unusual)
    health = CorrelationEngine().correlate(poll)
    assert health.severity is Severity.UNKNOWN
    assert health.device_by_ip("192.0.2.11").severity is Severity.UNKNOWN  # type: ignore[union-attr]


def test_successful_failover_is_recorded_without_becoming_device_down() -> None:
    poll = cycle(controller="192.0.2.13")
    poll.primary_failed = True
    poll.failover_at = NOW
    health = CorrelationEngine().correlate(poll)
    assert health.severity is Severity.NORMAL
    assert health.primary_failed is True
    assert any("Primary 수집 실패" in note for note in health.notes)


def test_partial_primary_collection_is_not_described_as_failover() -> None:
    poll = cycle(controller="192.0.2.11")
    poll.primary_failed = True
    health = CorrelationEngine().correlate(poll)
    assert health.actual_cluster_controller_ip == health.requested_cluster_controller_ip
    assert not any("Primary 수집 실패 후" in note for note in health.notes)
