from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aruba_mini_dashboard.config import AppPaths, AppSettings, SettingsStore
from aruba_mini_dashboard.credentials import CredentialService, SessionCredentialStore
from aruba_mini_dashboard.demo import DEMO_STAGES, DemoPoller, demo_fixture_directory
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.models import (
    ConnectionBaseline,
    ConnectionChange,
    Incident,
    IncidentType,
    PollCycleResult,
    Severity,
)
from aruba_mini_dashboard.parsers import (
    parse_group_membership,
    parse_load_distribution,
    parse_show_switches,
)
from aruba_mini_dashboard.storage import SQLiteStorage, StorageBusyError


FIXTURE_DIR = Path(__file__).parent / "fixtures"
EXPECTED_MEMBERS = {
    f"192.0.2.{number}": f"WLC-{number - 10:02d}" for number in range(11, 15)
}


def membership_cycle(
    filename: str,
    checked_at: datetime,
    *,
    controller_ip: str = "192.0.2.11",
) -> PollCycleResult:
    return PollCycleResult(
        checked_at=checked_at,
        expected_cluster_members=EXPECTED_MEMBERS,
        mm_result=parse_show_switches(
            (FIXTURE_DIR / "mm_show_switches_normal.txt").read_text(encoding="utf-8")
        ),
        load_result=parse_load_distribution(
            (FIXTURE_DIR / "cluster_load_normal.txt").read_text(encoding="utf-8")
        ),
        membership_result=parse_group_membership(
            (FIXTURE_DIR / filename).read_text(encoding="utf-8")
        ),
        requested_cluster_controller_ip="192.0.2.11",
        actual_cluster_controller_ip=controller_ip,
    )


def test_demo_reads_fixtures_and_replays_full_detection_sequence(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(":memory:")
    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    demo = DemoPoller(runtime, fixture_dir=Path(__file__).parent / "fixtures")
    results = [demo() for _ in DEMO_STAGES]
    assert results[0].severity.value == "normal"
    assert results[1].severity.value == "normal"
    assert results[2].severity.value == "normal"
    assert results[3].severity.value == "warning"
    assert results[3].problem_ips == ["192.0.2.12"]
    assert results[4].severity.value == "critical"
    assert results[5].severity.value == "critical"
    assert results[6].severity.value == "warning"
    assert results[7].severity.value == "normal"
    assert all(result.notes[0].startswith("데모 단계") for result in results)
    wrapped = demo()
    assert wrapped.severity.value == "normal"
    assert wrapped.problem_ips == []
    assert "1/8" in wrapped.notes[0]
    storage.close()


def test_fixture_directory_resolves_source_tree() -> None:
    path = demo_fixture_directory()
    assert path.name == "fixtures"
    assert (path / "mm_show_switches_normal.txt").is_file()


def test_v4_false_connection_warning_is_silent_after_restart_and_first_poll(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    observed = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    previous = (
        "L2-Connected CONNECTED (Member, last HBT_RSP 44ms ago, RTD = 0.000 ms)"
    )
    current = (
        "L2-Connected CONNECTED (Member, last HBT_RSP 67ms ago, RTD = 0.125 ms)"
    )
    change = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        previous_value=previous,
        current_value=current,
        first_detected_at=observed,
        last_confirmed_at=observed,
    )
    with SQLiteStorage(paths) as legacy_storage:
        legacy_storage.set(
            ConnectionBaseline(
                collector_ip="192.0.2.11",
                member_ip="192.0.2.12",
                display_value=current,
                normalized_value="legacy-polluted-value",
                observed_at=observed,
            )
        )
        legacy_storage.save_connection_change(change)
        legacy_storage.save_domain_incident(
            Incident(
                incident_id="legacy-false-incident",
                incident_type=IncidentType.CONNECTION_TYPE_CHANGED,
                severity=Severity.WARNING,
                reason=f"Connection-Type 변경: {previous} → {current}",
                first_detected_at=observed,
                last_seen_at=observed,
                ip="192.0.2.12",
                event_token=change.event_token,
                details={"previous": previous, "current": current},
            )
        )

    legacy = sqlite3.connect(paths.database)
    legacy.execute("PRAGMA user_version=4")
    legacy.commit()
    legacy.close()

    storage = SQLiteStorage(paths)
    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    snapshot = runtime.correlate(
        membership_cycle("group_membership_7240xm.txt", observed + timedelta(minutes=1))
    )

    assert snapshot.problem_ips == []
    assert snapshot.notification_events == []
    assert snapshot.active_incidents == []
    assert storage.load_pending_connection_changes() == []
    assert storage.get("192.0.2.12").display_value == "L2-Connected"
    stored_incident = next(
        item
        for item in storage.list_incidents()
        if item.incident_id == "legacy-false-incident"
    )
    assert stored_incident.active is False
    assert stored_incident.acknowledged is True
    storage.close()


def test_runtime_prunes_stale_unregistered_inventory_before_restoring_it(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    old = datetime.now(timezone.utc) - timedelta(days=181)
    registered = "192.0.2.11"
    stale = "192.0.2.99"
    for ip in (registered, stale):
        storage.save_device_state(
            ip,
            {"ip": ip, "hostname": f"WLC-{ip.rsplit('.', 1)[-1]}"},
            observed_at=old,
            is_normal=True,
        )
        storage.save_mm_discovered_device(
            ip,
            hostname=f"WLC-{ip.rsplit('.', 1)[-1]}",
            last_seen_at=old,
        )
    settings = AppSettings.default()
    for member, ip in zip(settings.cluster.members, EXPECTED_MEMBERS, strict=True):
        member.ip = ip

    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )

    assert set(storage.load_device_states()) == {registered}
    assert set(runtime._last_devices) == {registered}
    assert runtime.engine.dump_known_mm_devices() == {registered: "WLC-11"}
    storage.close()


def test_incomplete_settings_never_prune_recoverable_inventory_on_startup(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    stale = "192.0.2.99"
    old = datetime.now(timezone.utc) - timedelta(days=181)
    storage.save_device_state(
        stale,
        {"ip": stale, "hostname": "RECOVERABLE-WLC"},
        observed_at=old,
        is_normal=True,
    )
    storage.save_mm_discovered_device(
        stale,
        hostname="RECOVERABLE-WLC",
        last_seen_at=old,
    )

    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )

    assert stale in storage.load_device_states()
    assert stale in runtime._last_devices
    assert runtime.engine.dump_known_mm_devices()[stale] == "RECOVERABLE-WLC"
    storage.close()


def test_demo_members_are_always_derived_from_fixtures_not_configured_endpoints(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    for index, member in enumerate(settings.cluster.members, start=11):
        member.ip = f"198.51.100.{index}"
        member.alias = f"PROD-{index}"
    storage = SQLiteStorage(":memory:")
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    demo = DemoPoller(runtime, fixture_dir=Path(__file__).parent / "fixtures")
    results = [demo() for _ in range(4)]
    assert results[-1].problem_ips == ["192.0.2.12"]
    assert all(not device.ip.startswith("198.51.100.") for device in results[-1].devices)
    storage.close()


def test_membership_event_survives_locked_flush_and_settings_rebuild(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    storage = SQLiteStorage(paths)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))
    assert storage.get("192.0.2.12").display_value == "Type-A"
    original_flush = storage.save_cycle_domain_state

    def locked(_baselines, _changes, _acknowledged_members, _incidents, _transitions) -> None:
        raise StorageBusyError("locked")

    monkeypatch.setattr(storage, "save_cycle_domain_state", locked)
    changed = runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    assert changed.device_by_ip("192.0.2.12").connection_type_changed is True
    assert storage.get("192.0.2.12").display_value == "Type-A"
    assert storage.load_pending_connection_changes() == []

    updated = copy.deepcopy(settings)
    updated.polling.interval_seconds = 30
    runtime.update_settings(updated)
    monkeypatch.setattr(storage, "save_cycle_domain_state", original_flush)
    runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=2))
    )

    assert storage.get("192.0.2.12").display_value == "Type-B"
    pending = storage.load_pending_connection_changes()
    assert len(pending) == 1
    assert (pending[0].previous_value, pending[0].current_value) == ("Type-A", "Type-B")
    incidents = storage.load_domain_incidents()
    assert len(incidents) == 1
    assert incidents[0].active is True
    assert incidents[0].event_token == pending[0].event_token
    events = storage.list_events()
    assert [item["event_type"] for item in events].count("activated") == 1
    assert all(item["event_type"] != "recovered" for item in events)
    storage.close()


def test_settings_member_replacement_silently_supersedes_and_prunes_old_scope(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    for member, (ip, alias) in zip(settings.cluster.members, EXPECTED_MEMBERS.items()):
        member.ip = ip
        member.alias = alias
    storage = SQLiteStorage(paths)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))
    activated = runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    incident_id = activated.active_incidents[0].incident_id

    updated = copy.deepcopy(settings)
    updated.cluster.members[1].ip = "192.0.2.99"
    updated.cluster.members[1].alias = "WLC-NEW"
    runtime.update_settings(updated)

    assert runtime.incident_manager.active_incidents() == []
    superseded = next(
        incident
        for incident in storage.load_domain_incidents()
        if incident.incident_id == incident_id
    )
    assert superseded.active is False
    assert superseded.recovered_at is None
    assert storage.get("192.0.2.12") is None
    assert storage.load_pending_connection_changes() == []
    assert "superseded" in [item["event_type"] for item in storage.list_events()]

    storage.close()
    reopened = SQLiteStorage(paths)
    assert reopened.get("192.0.2.12") is None
    assert reopened.load_pending_connection_changes() == []
    assert not any(item.active for item in reopened.load_domain_incidents())
    reopened.close()


def test_member_replacement_before_first_poll_flushes_restored_scope_state(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    for member, (ip, alias) in zip(settings.cluster.members, EXPECTED_MEMBERS.items()):
        member.ip = ip
        member.alias = alias
    storage = SQLiteStorage(paths)
    storage.set(
        ConnectionBaseline(
            collector_ip="192.0.2.11",
            member_ip="192.0.2.12",
            display_value="Type-A",
            normalized_value="type a",
            observed_at=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
        )
    )
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    assert runtime.engine.monitoring_scope_ips() == ()
    assert storage.get("192.0.2.12") is not None

    updated = copy.deepcopy(settings)
    updated.cluster.members[1].ip = "192.0.2.99"
    updated.cluster.members[1].alias = "WLC-NEW"
    runtime.update_settings(updated)

    assert storage.get("192.0.2.12") is None
    storage.close()
    reopened = SQLiteStorage(paths)
    assert reopened.get("192.0.2.12") is None
    reopened.close()


def test_member_replacement_stage_keeps_durable_baseline_until_json_commit(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    for member, (ip, alias) in zip(settings.cluster.members, EXPECTED_MEMBERS.items()):
        member.ip = ip
        member.alias = alias
    settings_store = SettingsStore(paths)
    settings_store.save(settings)
    storage = SQLiteStorage(paths)
    baseline = ConnectionBaseline(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        display_value="Type-A",
        normalized_value="type a",
        observed_at=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
    )
    storage.set(baseline)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    updated = copy.deepcopy(settings)
    updated.cluster.members[1].ip = "192.0.2.99"
    updated.cluster.members[1].alias = "WLC-NEW"

    file_update = settings_store.begin_update(updated)
    runtime_update = runtime.begin_settings_update(updated)
    # A crash here restores the old JSON. The old SQLite authority must still
    # exist because the destructive cleanup has not crossed the commit point.
    assert storage.get("192.0.2.12") == baseline
    assert SettingsStore(paths).load() == settings
    runtime_update.rollback()
    assert runtime.settings == settings
    assert runtime.baseline_store.get("192.0.2.12") == baseline
    assert storage.get("192.0.2.12") == baseline

    # Repeat the normal two-phase path: JSON commits first, then the runtime
    # cleanup becomes durable.
    file_update = settings_store.begin_update(updated)
    runtime_update = runtime.begin_settings_update(updated)
    file_update.commit()
    runtime_update.commit()
    assert settings_store.load() == updated
    assert storage.get("192.0.2.12") is None
    storage.close()


def test_failed_settings_stage_restores_shared_baseline_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    for member, (ip, alias) in zip(settings.cluster.members, EXPECTED_MEMBERS.items()):
        member.ip = ip
        member.alias = alias
    storage = SQLiteStorage(paths)
    baseline = ConnectionBaseline(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        display_value="Type-A",
        normalized_value="type a",
        observed_at=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
    )
    storage.set(baseline)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    updated = copy.deepcopy(settings)
    updated.cluster.members[1].ip = "192.0.2.99"
    real_factory = runtime._create_incident_manager

    def failing_factory(incidents=None):
        manager = real_factory(incidents)

        def fail_reconcile(*_args, **_kwargs):
            raise RuntimeError("injected scope reconciliation failure")

        manager.reconcile_monitoring_scope = fail_reconcile
        return manager

    monkeypatch.setattr(runtime, "_create_incident_manager", failing_factory)

    with pytest.raises(RuntimeError, match="injected scope"):
        runtime.begin_settings_update(updated)

    assert runtime.settings == settings
    assert runtime.baseline_store.get("192.0.2.12") == baseline
    assert storage.get("192.0.2.12") == baseline
    storage.close()


def test_disabled_new_alert_waits_full_repeat_interval(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    settings.notifications.notify_new_incidents = False
    settings.notifications.repeat_unacknowledged = True
    settings.notifications.repeat_interval_minutes = 10
    storage = SQLiteStorage(paths)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
    runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))

    first = runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    before_interval = runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=10, seconds=59),
        )
    )
    at_interval = runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=11))
    )

    assert first.notification_events == []
    assert before_interval.notification_events == []
    assert len(at_interval.notification_events) == 1
    assert at_interval.notification_events[0].first_detected_at == initial_at + timedelta(minutes=1)
    storage.close()


def test_failed_atomic_membership_write_restart_has_one_activation_and_no_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    first_runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))

    def locked(_baselines, _changes, _acknowledged_members, _incidents, _transitions) -> None:
        raise StorageBusyError("locked")

    monkeypatch.setattr(first_storage, "save_cycle_domain_state", locked)
    failed = first_runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    assert len(failed.notification_events) == 1
    assert first_storage.get("192.0.2.12").display_value == "Type-A"
    assert first_storage.load_pending_connection_changes() == []
    assert first_storage.load_domain_incidents() == []
    assert first_storage.list_events() == []
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    restarted = reopened_runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=2))
    )

    assert len(restarted.notification_events) == 1
    incidents = reopened_storage.load_domain_incidents()
    assert len(incidents) == 1
    assert incidents[0].active is True
    event_types = [item["event_type"] for item in reopened_storage.list_events()]
    assert event_types == ["activated"]
    reopened_storage.close()


def test_failed_atomic_acknowledgement_restart_does_not_reactivate_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    first_runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))
    activated = first_runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    incident_id = activated.active_incidents[0].incident_id
    assert [item["event_type"] for item in first_storage.list_events()] == ["activated"]

    def locked(_baselines, _changes, _acknowledged_members, _incidents, _transitions) -> None:
        raise StorageBusyError("locked")

    monkeypatch.setattr(first_storage, "save_cycle_domain_state", locked)
    first_runtime.acknowledge_ip("192.0.2.12")

    persisted_before_restart = first_storage.load_domain_incidents()
    assert len(persisted_before_restart) == 1
    assert persisted_before_restart[0].incident_id == incident_id
    assert persisted_before_restart[0].active is True
    assert persisted_before_restart[0].acknowledged is False
    assert len(first_storage.load_pending_connection_changes()) == 1
    assert [item["event_type"] for item in first_storage.list_events()] == ["activated"]
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    restarted = reopened_runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=2))
    )

    assert restarted.active_incidents[0].incident_id == incident_id
    event_types = [item["event_type"] for item in reversed(reopened_storage.list_events())]
    assert event_types.count("activated") == 1
    assert "acknowledged" not in event_types
    assert event_types[:1] == ["activated"]
    reopened_storage.close()


def test_failed_atomic_acknowledgement_is_retained_for_same_runtime_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    storage = SQLiteStorage(paths)
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 4, 30, tzinfo=timezone.utc)
    runtime.correlate(membership_cycle("group_membership_initial.txt", initial_at))
    runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=1))
    )
    original_flush = storage.save_cycle_domain_state

    def locked(_baselines, _changes, _acknowledged_members, _incidents, _transitions) -> None:
        raise StorageBusyError("locked")

    monkeypatch.setattr(storage, "save_cycle_domain_state", locked)
    runtime.acknowledge_ip("192.0.2.12")
    assert runtime._pending_connection_acknowledgements == {"192.0.2.12"}
    assert len(storage.load_pending_connection_changes()) == 1

    monkeypatch.setattr(storage, "save_cycle_domain_state", original_flush)
    runtime.correlate(
        membership_cycle("group_membership_changed.txt", initial_at + timedelta(minutes=2))
    )

    assert runtime._pending_connection_acknowledgements == set()
    assert storage.load_pending_connection_changes() == []
    incident = storage.load_domain_incidents()[0]
    assert incident.acknowledged is True
    assert incident.active is False
    event_types = [item["event_type"] for item in reversed(storage.list_events())]
    assert event_types == ["activated", "acknowledged"]
    storage.close()


def test_failover_connection_change_persists_by_member_across_restart(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc)
    first_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at,
            controller_ip="192.0.2.11",
        )
    )
    failover = first_runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=1),
            controller_ip="192.0.2.13",
        )
    )
    incident_id = failover.active_incidents[0].incident_id
    pending_token = first_storage.load_pending_connection_changes()[0].event_token
    assert first_storage.get("192.0.2.12").collector_ip == "192.0.2.13"
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    returned_to_primary = reopened_runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=2),
            controller_ip="192.0.2.11",
        )
    )

    assert returned_to_primary.active_incidents[0].incident_id == incident_id
    pending = reopened_storage.load_pending_connection_changes()
    assert len(pending) == 1
    assert pending[0].event_token == pending_token
    assert pending[0].collector_ip == "192.0.2.13"
    assert reopened_storage.get("192.0.2.12").collector_ip == "192.0.2.11"
    event_types = [item["event_type"] for item in reopened_storage.list_events()]
    assert event_types.count("activated") == 1
    assert "recovered" not in event_types
    reopened_storage.close()


def test_reverted_connection_type_supersedes_old_event_without_recovery_across_restart(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    first_runtime.correlate(
        membership_cycle("group_membership_initial.txt", initial_at)
    )
    changed = first_runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=1),
        )
    )
    original = changed.active_incidents[0]
    assert original.details["previous"] == "Type-A"
    assert original.details["current"] == "Type-B"

    reverted = first_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=2),
        )
    )

    assert len(reverted.notification_events) == 1
    replacement = reverted.notification_events[0]
    assert replacement.incident_id != original.incident_id
    assert replacement.details["previous"] == "Type-B"
    assert replacement.details["current"] == "Type-A"
    assert [item.incident_id for item in reverted.active_incidents] == [
        replacement.incident_id
    ]

    persisted = first_storage.load_domain_incidents()
    persisted_by_id = {item.incident_id: item for item in persisted}
    superseded = persisted_by_id[original.incident_id]
    assert superseded.active is False
    assert superseded.acknowledged is False
    assert superseded.recovered_at is None
    assert persisted_by_id[replacement.incident_id].active is True
    pending = first_storage.load_pending_connection_changes()
    assert len(pending) == 1
    assert (pending[0].previous_value, pending[0].current_value) == (
        "Type-B",
        "Type-A",
    )
    event_types = [
        item["event_type"] for item in reversed(first_storage.list_events())
    ]
    assert event_types == ["activated", "activated", "superseded"]
    assert "recovered" not in event_types
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    reconfirmed = reopened_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=3),
        )
    )

    assert reconfirmed.notification_events == []
    assert [item.incident_id for item in reconfirmed.active_incidents] == [
        replacement.incident_id
    ]
    restarted_events = [
        item["event_type"] for item in reversed(reopened_storage.list_events())
    ]
    assert restarted_events == [*event_types, "updated"]
    assert restarted_events.count("activated") == 2
    assert restarted_events.count("superseded") == 1
    assert "recovered" not in restarted_events
    reopened_storage.close()
