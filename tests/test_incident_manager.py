from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aruba_mini_dashboard.models import (
    DeferredIncidentState,
    DeviceHealth,
    HealthSignal,
    IncidentTransitionKind,
    IncidentType,
    OverallHealth,
    Severity,
)
from aruba_mini_dashboard.services.incident_manager import IncidentManager


NOW = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)


def health(*signals: HealthSignal, checked_at: datetime = NOW) -> OverallHealth:
    ips = sorted({signal.ip for signal in signals if signal.ip is not None})
    devices = [DeviceHealth(ip=ip, alias=f"alias-{ip}") for ip in ips]
    return OverallHealth(
        checked_at=checked_at,
        severity=(Severity.WARNING if signals else Severity.NORMAL),
        devices=devices,
        signals=list(signals),
    )


def signal(
    incident_type: IncidentType = IncidentType.CLIENT_DISTRIBUTION,
    *,
    ip: str | None = "192.0.2.12",
    reason: str = "Client 분배 이상",
    token: str = "",
) -> HealthSignal:
    return HealthSignal(
        incident_type=incident_type,
        severity=Severity.WARNING,
        reason=reason,
        ip=ip,
        source="test",
        event_token=token,
    )


def test_new_signal_activates_one_incident_and_same_signal_does_not_duplicate() -> None:
    manager = IncidentManager()
    first = manager.process(health(signal()))
    assert len(first) == 1
    assert first[0].kind is IncidentTransitionKind.ACTIVATED
    assert first[0].should_notify is True
    assert manager.process(health(signal(), checked_at=NOW + timedelta(minutes=1))) == []
    assert len(manager.events()) == 1


def test_changed_reason_updates_without_new_notification() -> None:
    manager = IncidentManager()
    manager.process(health(signal(reason="연속 3회")))
    transitions = manager.process(
        health(signal(reason="연속 4회"), checked_at=NOW + timedelta(minutes=1))
    )
    assert len(transitions) == 1
    assert transitions[0].kind is IncidentTransitionKind.UPDATED
    assert transitions[0].should_notify is False
    assert len(manager.events()) == 1


def test_absent_signal_records_recovery_event() -> None:
    manager = IncidentManager()
    activated = manager.process(health(signal()))[0].incident
    transitions = manager.process(health(checked_at=NOW + timedelta(minutes=2)))
    assert len(transitions) == 1
    assert transitions[0].kind is IncidentTransitionKind.RECOVERED
    assert transitions[0].should_notify is True
    event = next(item for item in manager.events() if item.incident_id == activated.incident_id)
    assert event.active is False
    assert event.recovered_at == NOW + timedelta(minutes=2)


def test_recovery_notification_can_be_disabled() -> None:
    manager = IncidentManager(recovery_notifications=False)
    manager.process(health(signal()))
    transition = manager.process(health(checked_at=NOW + timedelta(minutes=1)))[0]
    assert transition.kind is IncidentTransitionKind.RECOVERED
    assert transition.should_notify is False


def test_acknowledgement_stops_repeat_but_not_monitoring() -> None:
    manager = IncidentManager(repeat_unacknowledged=True)
    incident = manager.process(health(signal()))[0].incident
    acknowledged = manager.acknowledge(incident.incident_id, now=NOW + timedelta(minutes=1))
    assert acknowledged is not None
    assert acknowledged.kind is IncidentTransitionKind.ACKNOWLEDGED
    assert manager.due_notifications(now=NOW + timedelta(hours=1)) == []
    assert manager.active_incidents()[0].active is True


def test_repeat_notification_uses_ten_minute_default_interval() -> None:
    manager = IncidentManager(repeat_unacknowledged=True)
    incident = manager.process(health(signal()))[0].incident
    assert manager.due_notifications(now=NOW + timedelta(minutes=9, seconds=59)) == []
    assert [
        item.incident_id for item in manager.due_notifications(now=NOW + timedelta(minutes=10))
    ] == [incident.incident_id]
    assert manager.mark_notified(incident.incident_id, now=NOW + timedelta(minutes=10)) is True
    assert manager.due_notifications(now=NOW + timedelta(minutes=19, seconds=59)) == []
    assert [
        item.incident_id for item in manager.due_notifications(now=NOW + timedelta(minutes=20))
    ] == [incident.incident_id]


def test_repeat_notifications_are_off_by_default() -> None:
    manager = IncidentManager()
    incident = manager.process(health(signal()))[0].incident
    assert manager.due_notifications(now=NOW + timedelta(days=1)) == []
    manager.mark_notified(incident.incident_id, now=NOW)
    assert manager.due_notifications(now=NOW + timedelta(days=1)) == []


def test_deferred_incident_stays_active_and_is_ineligible_for_repeat() -> None:
    manager = IncidentManager(repeat_unacknowledged=True)
    incident = manager.process(health(signal()))[0].incident
    deferred_health = OverallHealth(
        checked_at=NOW + timedelta(minutes=20),
        severity=Severity.UNKNOWN,
        devices=[DeviceHealth(ip="192.0.2.12")],
        deferred_incidents=[
            DeferredIncidentState(IncidentType.CLIENT_DISTRIBUTION, "192.0.2.12")
        ],
    )

    assert manager.process(deferred_health) == []
    assert manager.active_incidents()[0].incident_id == incident.incident_id
    assert manager.due_notifications(now=deferred_health.checked_at) == []

    manager.process(health(signal(), checked_at=NOW + timedelta(minutes=21)))
    assert [
        item.incident_id
        for item in manager.due_notifications(now=NOW + timedelta(minutes=21))
    ] == [incident.incident_id]


def test_new_connection_event_supersedes_old_without_recovery_or_acknowledgement() -> None:
    manager = IncidentManager()
    first_signal = signal(
        IncidentType.CONNECTION_TYPE_CHANGED,
        reason="A → B",
        token="controller|member|first",
    )
    manager.process(health(first_signal))
    second_signal = signal(
        IncidentType.CONNECTION_TYPE_CHANGED,
        reason="B → C",
        token="controller|member|second",
    )
    second_health = health(second_signal, checked_at=NOW + timedelta(minutes=1))
    second_health.deferred_incidents = [
        DeferredIncidentState(
            IncidentType.CONNECTION_TYPE_CHANGED,
            "192.0.2.12",
            "controller|member|first",
        )
    ]
    transitions = manager.process(
        second_health
    )
    assert {item.kind for item in transitions} == {
        IncidentTransitionKind.ACTIVATED,
        IncidentTransitionKind.SUPERSEDED,
    }
    superseded = next(
        item for item in transitions if item.kind is IncidentTransitionKind.SUPERSEDED
    )
    assert superseded.should_notify is False
    assert superseded.incident.active is False
    assert superseded.incident.acknowledged is False
    assert superseded.incident.recovered_at is None
    assert len(manager.events()) == 2
    assert len(manager.active_incidents()) == 1
    assert manager.active_incidents()[0].event_token.endswith("second")

    restored = IncidentManager(manager.events())
    assert len(restored.events()) == 2
    assert len(restored.active_incidents()) == 1
    assert restored.active_incidents()[0].event_token.endswith("second")


def test_collection_failure_tokens_keep_different_causes_separate() -> None:
    manager = IncidentManager()
    auth = signal(
        IncidentType.COLLECTION_FAILURE,
        ip=None,
        reason="로그인 실패",
        token="mm|AUTH_FAILED",
    )
    parse = signal(
        IncidentType.COLLECTION_FAILURE,
        ip=None,
        reason="파싱 실패",
        token="mm|PARSE_HEADER_MISSING",
    )
    manager.process(health(auth, parse))
    assert len(manager.active_incidents()) == 2


def test_acknowledge_ip_acknowledges_all_active_causes_for_device() -> None:
    manager = IncidentManager()
    manager.process(
        health(
            signal(),
            signal(IncidentType.MM_DOWN, reason="MM Down"),
        )
    )
    transitions = manager.acknowledge_ip("192.0.2.12", now=NOW + timedelta(minutes=1))
    assert len(transitions) == 2
    assert all(item.acknowledged for item in manager.active_incidents())


def test_persisted_active_incident_can_be_restored_without_duplicate() -> None:
    first = IncidentManager()
    first.process(health(signal()))
    restored = IncidentManager(first.events())
    assert restored.process(health(signal(), checked_at=NOW + timedelta(minutes=1))) == []
    assert len(restored.events()) == 1


def test_mm_down_recovers_only_after_trusted_up_not_collection_failure_or_missing_row() -> None:
    manager = IncidentManager()
    down = DeviceHealth(
        ip="192.0.2.12",
        mm_status="Down",
        mm_present=True,
        severity=Severity.CRITICAL,
    )
    down_health = OverallHealth(
        checked_at=NOW,
        severity=Severity.CRITICAL,
        devices=[down],
        signals=[signal(IncidentType.MM_DOWN, reason="MM Down")],
    )
    activated = manager.process(down_health)[0].incident

    unknown_health = OverallHealth(
        checked_at=NOW + timedelta(minutes=1),
        severity=Severity.UNKNOWN,
        devices=[DeviceHealth(ip="192.0.2.12", mm_present=None)],
    )
    assert manager.process(unknown_health) == []
    missing_health = OverallHealth(
        checked_at=NOW + timedelta(minutes=2),
        severity=Severity.WARNING,
        devices=[DeviceHealth(ip="192.0.2.12", mm_present=False)],
    )
    assert manager.process(missing_health) == []
    assert manager.active_incidents()[0].incident_id == activated.incident_id

    up_health = OverallHealth(
        checked_at=NOW + timedelta(minutes=3),
        severity=Severity.NORMAL,
        devices=[DeviceHealth(ip="192.0.2.12", mm_status="Up", mm_present=True)],
    )
    recovered = manager.process(up_health)
    assert len(recovered) == 1
    assert recovered[0].kind is IncidentTransitionKind.RECOVERED
    assert recovered[0].incident.incident_id == activated.incident_id


def test_acknowledged_connection_change_closes_without_recovery_transition() -> None:
    manager = IncidentManager()
    changed = signal(
        IncidentType.CONNECTION_TYPE_CHANGED,
        reason="A → B",
        token="controller|member|event",
    )
    incident = manager.process(health(changed))[0].incident
    acknowledged = manager.acknowledge(incident.incident_id, now=NOW + timedelta(minutes=1))
    assert acknowledged is not None
    assert acknowledged.kind is IncidentTransitionKind.ACKNOWLEDGED
    assert acknowledged.incident.active is False
    assert manager.active_incidents() == []
    assert manager.process(health(checked_at=NOW + timedelta(minutes=2))) == []


def test_scope_removed_incident_is_silently_superseded_not_recovered() -> None:
    manager = IncidentManager()
    scoped = health(signal())
    scoped.monitoring_scope_ips = ("192.0.2.12",)
    incident = manager.process(scoped)[0].incident

    transitions = manager.reconcile_monitoring_scope(
        ("192.0.2.11",),
        now=NOW + timedelta(minutes=1),
    )

    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.kind is IncidentTransitionKind.SUPERSEDED
    assert transition.should_notify is False
    assert transition.incident.incident_id == incident.incident_id
    assert transition.incident.active is False
    assert transition.incident.recovered_at is None
    assert manager.active_incidents() == []


def test_collection_failure_remains_active_when_target_is_outside_member_scope() -> None:
    manager = IncidentManager()
    failure = signal(
        IncidentType.COLLECTION_FAILURE,
        ip="198.51.100.10",
        reason="MM 수집 실패",
        token="collection|AUTH_FAILED|198.51.100.10",
    )
    snapshot = health(failure)
    snapshot.monitoring_scope_ips = ("192.0.2.11",)

    activated = manager.process(snapshot)
    scope_transitions = manager.reconcile_monitoring_scope(
        ("192.0.2.11",),
        now=NOW + timedelta(minutes=1),
    )

    assert len(activated) == 1
    assert activated[0].kind is IncidentTransitionKind.ACTIVATED
    assert scope_transitions == []
    assert manager.active_incidents()[0].incident_type is IncidentType.COLLECTION_FAILURE
