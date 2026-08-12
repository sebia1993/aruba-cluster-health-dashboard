from __future__ import annotations

import logging
import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aruba_mini_dashboard.config import AppPaths
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.models import (
    ConnectionBaseline as DomainConnectionBaseline,
    ConnectionChange,
    Incident,
    IncidentType,
    Severity,
)
from aruba_mini_dashboard.storage import (
    SCHEMA_VERSION,
    SQLiteStorage,
    StorageBusyError,
    StorageCorruptError,
)


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_environment(tmp_path)


def test_sqlite_uses_wal_and_persists_baseline_protocol_across_restart(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    observed = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    with SQLiteStorage(paths) as storage:
        assert storage.schema_version == 4
        storage.set(
            DomainConnectionBaseline(
                collector_ip="192.0.2.11",
                member_ip="192.0.2.12",
                display_value="Type-A",
                normalized_value="type a",
                observed_at=observed,
            )
        )
        mode = storage._read(lambda db: db.execute("PRAGMA journal_mode").fetchone()[0])
        assert str(mode).casefold() == "wal"

    with SQLiteStorage(paths) as reopened:
        baseline = reopened.get("192.0.2.12")
        assert baseline == DomainConnectionBaseline(
            collector_ip="192.0.2.11",
            member_ip="192.0.2.12",
            display_value="Type-A",
            normalized_value="type a",
            observed_at=observed,
        )


def test_v3_migration_selects_latest_member_baseline_and_pending_change(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(paths.database)
    legacy.executescript(
        """
        CREATE TABLE connection_baselines (
            source_controller_ip TEXT NOT NULL,
            member_ip TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            normalized_connection_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_controller_ip, member_ip)
        );
        CREATE TABLE connection_changes (
            event_token TEXT PRIMARY KEY,
            collector_ip TEXT NOT NULL,
            member_ip TEXT NOT NULL,
            previous_value TEXT NOT NULL,
            current_value TEXT NOT NULL,
            first_detected_at TEXT NOT NULL,
            last_confirmed_at TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
        );
        INSERT INTO connection_baselines VALUES
            ('192.0.2.11', '192.0.2.12', 'Type-A', 'type a', '2026-08-11T01:00:00+00:00'),
            ('192.0.2.13', '192.0.2.12', 'Type-B', 'type b', '2026-08-11T01:01:00+00:00');
        INSERT INTO connection_changes VALUES
            ('older-token', '192.0.2.11', '192.0.2.12', 'Type-X', 'Type-A',
             '2026-08-11T00:59:00+00:00', '2026-08-11T00:59:00+00:00', 0),
            ('newer-token', '192.0.2.13', '192.0.2.12', 'Type-A', 'Type-B',
             '2026-08-11T01:01:00+00:00', '2026-08-11T01:01:00+00:00', 0);
        PRAGMA user_version=3;
        """
    )
    legacy.close()

    with SQLiteStorage(paths) as migrated:
        assert migrated.schema_version == 4
        baseline = migrated.get("192.0.2.12")
        assert baseline is not None
        assert baseline.collector_ip == "192.0.2.13"
        assert baseline.display_value == "Type-B"
        assert len(migrated.load_connection_baselines()) == 1
        pending = migrated.load_pending_connection_changes()
        assert len(pending) == 1
        assert pending[0].event_token == "newer-token"
        acknowledgement_rows = migrated._read(
            lambda db: db.execute(
                "SELECT event_token, acknowledged FROM connection_changes ORDER BY event_token"
            ).fetchall()
        )
        assert [(row["event_token"], row["acknowledged"]) for row in acknowledgement_rows] == [
            ("newer-token", 0),
            ("older-token", 1),
        ]


def test_storage_persists_streak_incident_ack_recovery_and_event(tmp_path: Path) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime.now(timezone.utc)
    storage.save_streak("client", "192.0.2.12", 3, 0, True, updated_at=now)
    storage.upsert_incident(
        "event-1",
        "192.0.2.12",
        "client_distribution",
        "low-clients",
        first_detected_at=now,
        last_seen_at=now,
        payload={"active_clients": 0, "standby_clients": 4},
    )
    storage.acknowledge_incident("event-1")
    storage.append_event("activated", ip="192.0.2.12", incident_id="event-1")
    storage.resolve_incident("event-1")

    assert storage.get_streak("client", "192.0.2.12").anomaly_count == 3
    incident = storage.list_incidents()[0]
    assert incident.acknowledged is True
    assert incident.active is False
    assert incident.payload["standby_clients"] == 4
    assert storage.list_events()[0]["event_type"] == "activated"
    storage.close()


def test_latest_normal_device_state_survives_a_later_abnormal_observation(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    normal_at = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    abnormal_at = datetime(2026, 8, 11, 1, 31, tzinfo=timezone.utc)
    with SQLiteStorage(paths) as storage:
        storage.save_device_state(
            "192.0.2.12",
            {"mm_status": "Up", "active_clients": 250},
            observed_at=normal_at,
            is_normal=True,
        )
        storage.save_device_state(
            "192.0.2.12",
            {"mm_status": "Down", "active_clients": 0},
            observed_at=abnormal_at,
            is_normal=False,
        )

    with SQLiteStorage(paths) as reopened:
        latest = reopened.load_device_states()["192.0.2.12"]
        last_normal = reopened.load_device_states(normal_only=True)["192.0.2.12"]
        assert latest["payload"]["mm_status"] == "Down"
        assert last_normal["payload"]["mm_status"] == "Up"
        assert last_normal["observed_at"] == normal_at.isoformat(timespec="seconds")


def test_connection_change_and_domain_incident_survive_restart(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    now = datetime(2026, 8, 11, 1, 31, tzinfo=timezone.utc)
    change = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=now,
        last_confirmed_at=now,
    )
    incident = Incident(
        incident_id="incident-change-1",
        incident_type=IncidentType.CONNECTION_TYPE_CHANGED,
        severity=Severity.WARNING,
        reason="Connection-Type changed",
        first_detected_at=now,
        last_seen_at=now,
        ip="192.0.2.12",
        event_token=change.event_token,
        details={"previous": "Type-A", "current": "Type-B"},
    )
    with SQLiteStorage(paths) as storage:
        storage.save_connection_change(change)
        storage.save_domain_incident(incident)

    with SQLiteStorage(paths) as reopened:
        assert reopened.load_pending_connection_changes() == [change]
        assert reopened.load_domain_incidents() == [incident]
        assert reopened.acknowledge_connection_change(member_ip="192.0.2.12") == 1
        assert reopened.load_pending_connection_changes() == []


def test_scope_prune_survives_restart_but_preserves_discovered_inventory(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    now = datetime(2026, 8, 11, 1, 31, tzinfo=timezone.utc)
    baseline = DomainConnectionBaseline(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        display_value="Type-A",
        normalized_value="type a",
        observed_at=now,
    )
    change = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=now,
        last_confirmed_at=now,
    )
    with SQLiteStorage(paths) as storage:
        storage.set(baseline)
        storage.save_connection_change(change)
        storage.save_streak("load", "192.0.2.12", 2, 0, False, updated_at=now)
        storage.save_mm_discovered_device(
            "192.0.2.12",
            hostname="WLC-02",
            last_seen_at=now,
        )
        storage.save_cycle_domain_state([], [], set(), [], [], {"192.0.2.12"})

    with SQLiteStorage(paths) as reopened:
        assert reopened.get("192.0.2.12") is None
        assert reopened.get_streak("load", "192.0.2.12") is None
        assert reopened.load_pending_connection_changes() == []
        assert reopened.load_mm_discovered_devices()[0]["ip"] == "192.0.2.12"


def test_connection_change_token_round_trips_microseconds_without_duplicate_rows(tmp_path: Path) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    detected = datetime(2026, 8, 11, 1, 31, 0, 123456, tzinfo=timezone.utc)
    original = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=detected,
        last_confirmed_at=detected,
    )
    original_token = original.event_token
    storage.save_connection_change(original)

    reloaded = storage.load_pending_connection_changes()[0]
    assert reloaded.event_token == original_token
    storage.save_connection_change(reloaded)
    count = storage._read(
        lambda db: db.execute("SELECT COUNT(*) FROM connection_changes").fetchone()[0]
    )
    assert count == 1
    storage.close()


def test_new_connection_change_supersedes_older_pending_change_for_same_member(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    first_at = datetime(2026, 8, 11, 1, 31, tzinfo=timezone.utc)
    first = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.12",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=first_at,
        last_confirmed_at=first_at,
    )
    second = ConnectionChange(
        collector_ip="192.0.2.13",
        member_ip="192.0.2.12",
        previous_value="Type-B",
        current_value="Type-C",
        first_detected_at=first_at.replace(minute=32),
        last_confirmed_at=first_at.replace(minute=32),
    )

    storage.save_connection_change(first)
    storage.save_connection_change(second)

    assert storage.load_pending_connection_changes() == [second]
    rows = storage._read(
        lambda db: db.execute(
            "SELECT event_token, acknowledged FROM connection_changes ORDER BY first_detected_at"
        ).fetchall()
    )
    assert [(row["event_token"], row["acknowledged"]) for row in rows] == [
        (first.event_token, 1),
        (second.event_token, 0),
    ]
    storage.close()


def test_storage_rejects_secret_fields_and_secret_never_reaches_file(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    sentinel = "DO-NOT-PERSIST-SECRET"
    with SQLiteStorage(paths) as storage:
        with pytest.raises(ValueError, match="secret-bearing"):
            storage.set_preference("unsafe", {"password": sentinel})
        storage.set_preference("safe", {"credential_id": "opaque-id"})
    assert sentinel.encode() not in paths.database.read_bytes()


def test_corrupt_database_is_preserved(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True)
    paths.database.write_bytes(b"not a sqlite database")
    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)
    assert paths.database.read_bytes() == b"not a sqlite database"


def test_future_schema_is_preserved_and_initializing_connection_is_closed(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True)
    future = sqlite3.connect(paths.database)
    future.execute("PRAGMA journal_mode=DELETE")
    future.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    future.close()
    original_hash = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    storage = SQLiteStorage(paths, initialize=False)

    with pytest.raises(StorageCorruptError, match="현재 프로그램보다 새로운 데이터베이스"):
        storage.initialize()

    assert storage._connection is None
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == original_hash
    reopened = sqlite3.connect(paths.database)
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert str(reopened.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "delete"
    finally:
        reopened.close()


def test_external_sqlite_write_lock_has_bounded_retry(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    storage = SQLiteStorage(paths, busy_timeout_ms=5, lock_retries=1)
    external = sqlite3.connect(paths.database, timeout=0)
    external.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageBusyError):
            storage.set_preference("blocked", True)
    finally:
        external.rollback()
        external.close()
        storage.close()


def test_preferences_are_loaded_in_one_batch(tmp_path: Path) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.set_preferences({"one": 1, "two": False, "three": "value"})
        assert storage.get_preferences(("one", "three", "missing")) == {
            "one": 1,
            "three": "value",
        }


def test_poll_runtime_state_is_one_transaction_and_rolls_back_together(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aruba_mini_dashboard.models import DeviceHealth

    storage = SQLiteStorage(make_paths(tmp_path))
    observed = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    state = {
        "client|192.0.2.12": {
            "anomaly_streak": 3,
            "recovery_streak": 0,
            "active": True,
        }
    }
    devices = [(DeviceHealth(ip="192.0.2.12", mm_present=True), False)]
    write_calls = 0
    original_write = storage._write

    def counted(operation):
        nonlocal write_calls
        write_calls += 1
        return original_write(operation)

    monkeypatch.setattr(storage, "_write", counted)
    storage.save_poll_runtime_state(
        detector_state=state,
        device_states=devices,
        observed_at=observed,
        failover=("192.0.2.11", "192.0.2.13", "TCP_TIMEOUT", observed),
    )
    assert write_calls == 1
    assert storage.get_streak("client", "192.0.2.12") is not None
    assert "192.0.2.12" in storage.load_device_states()

    bad_devices = [
        (DeviceHealth(ip="192.0.2.14"), False),
        ({"password": "must-not-persist"}, False),
    ]
    with pytest.raises((AttributeError, ValueError)):
        storage.save_poll_runtime_state(
            detector_state={},
            device_states=bad_devices,
            observed_at=observed,
        )
    assert "192.0.2.14" not in storage.load_device_states()
    storage.close()


def test_history_retention_preserves_active_incident_and_bounds_closed_history(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=181)
    for index in range(5):
        storage.upsert_incident(
            f"closed-{index}",
            "192.0.2.12",
            "client_distribution",
            f"closed-{index}",
            first_detected_at=old,
            last_seen_at=old,
            active=False,
            resolved_at=old,
        )
        storage.append_event(
            "recovered",
            incident_id=f"closed-{index}",
            occurred_at=old,
        )
        storage.record_failover("192.0.2.11", "192.0.2.13", "TIMEOUT", collected_at=old)
    storage.upsert_incident(
        "active-old",
        "192.0.2.14",
        "mm_down",
        "down",
        first_detected_at=old,
        last_seen_at=old,
        active=True,
    )
    storage.append_event("activated", incident_id="active-old", occurred_at=old)
    for index in range(4):
        change = ConnectionChange(
            collector_ip="192.0.2.11",
            member_ip=f"192.0.2.{20 + index}",
            previous_value="Type-A",
            current_value="Type-B",
            first_detected_at=old + timedelta(seconds=index),
            last_confirmed_at=old + timedelta(seconds=index),
        )
        storage.save_connection_change(change)
        storage.acknowledge_connection_change(event_token=change.event_token)
    pending = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip="192.0.2.99",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=old,
        last_confirmed_at=old,
    )
    storage.save_connection_change(pending)

    removed = storage.maintain_history(now=now, max_rows=2, force=True)

    assert removed["incidents"] == 5
    assert storage.list_incidents(active_only=True)[0].incident_id == "active-old"
    assert storage.list_events(limit=20)[0]["incident_id"] == "active-old"
    assert storage._read(
        lambda db: db.execute("SELECT COUNT(*) FROM failover_collections").fetchone()[0]
    ) == 0
    assert removed["connection_changes"] == 4
    assert storage.load_pending_connection_changes() == [pending]
    storage.close()


def _seed_inventory_row(
    storage: SQLiteStorage,
    ip: str,
    observed_at: datetime | str,
) -> None:
    storage.save_device_state(
        ip,
        {"ip": ip, "state": "normal"},
        observed_at=observed_at,
        is_normal=True,
    )
    storage.save_mm_discovered_device(
        ip,
        hostname=f"WLC-{ip.rsplit('.', 1)[-1]}",
        last_seen_at=observed_at,
    )


def test_device_inventory_retention_prunes_only_stale_unprotected_ips(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=181)
    recent = now - timedelta(days=1)
    stale = "192.0.2.20"
    registered = "192.0.2.21"
    active = "192.0.2.22"
    pending_ip = "192.0.2.23"
    malformed = "192.0.2.24"
    current = "192.0.2.25"
    for ip, observed_at in (
        (stale, old),
        (registered, old),
        (active, old),
        (pending_ip, old),
        (malformed, "not-a-timestamp"),
        (current, recent),
    ):
        _seed_inventory_row(storage, ip, observed_at)
    storage.upsert_incident(
        "active-inventory",
        active,
        "mm_down",
        "down",
        first_detected_at=old,
        last_seen_at=old,
        active=True,
    )
    pending = ConnectionChange(
        collector_ip="192.0.2.11",
        member_ip=pending_ip,
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=old,
        last_confirmed_at=old,
    )
    storage.save_connection_change(pending)

    removed = storage.maintain_device_inventory({registered}, now=now, force=True)

    assert removed == {stale}
    for table in ("device_states", "device_normal_states", "mm_discovered_devices"):
        remaining = {
            row[0]
            for row in storage._read(
                lambda db, table=table: db.execute(f"SELECT ip FROM {table}").fetchall()
            )
        }
        assert stale not in remaining
        assert {registered, active, pending_ip, malformed, current} <= remaining
    storage.close()


def test_device_inventory_cap_is_deterministic_and_protected_rows_do_not_count(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    ips = [f"192.0.2.{number}" for number in range(30, 35)]
    for offset, ip in enumerate(ips):
        _seed_inventory_row(storage, ip, now - timedelta(days=offset + 1))

    removed = storage.maintain_device_inventory(
        {ips[-1]}, now=now, max_rows=2, force=True
    )

    assert removed == {ips[2], ips[3]}
    assert set(storage.load_device_states()) == {ips[0], ips[1], ips[-1]}
    storage.close()


def test_device_inventory_cleanup_is_atomic_and_daily_bounded(tmp_path: Path) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=181)
    first = "192.0.2.40"
    second = "192.0.2.41"
    _seed_inventory_row(storage, first, old)
    storage._write(
        lambda db: db.execute(
            f"""CREATE TRIGGER block_inventory_delete BEFORE DELETE ON device_states
                WHEN OLD.ip='{first}' BEGIN SELECT RAISE(ABORT, 'blocked'); END"""
        )
    )
    with pytest.raises(Exception):
        storage.maintain_device_inventory(set(), now=now, force=True)
    assert first in storage.load_device_states()
    assert first in {row["ip"] for row in storage.load_mm_discovered_devices()}
    storage._write(lambda db: db.execute("DROP TRIGGER block_inventory_delete"))
    assert storage.maintain_device_inventory(set(), now=now, force=True) == {first}

    _seed_inventory_row(storage, second, old)
    assert storage.maintain_device_inventory(set(), now=now) == set()
    assert second in storage.load_device_states()
    assert storage.maintain_device_inventory(set(), now=now, force=True) == {second}
    storage.close()


def test_default_sqlite_contention_budget_prevents_multi_second_ui_stalls(
    tmp_path: Path,
) -> None:
    database = tmp_path / "app.db"
    storage = SQLiteStorage(database)
    blocker = sqlite3.connect(database, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    try:
        with pytest.raises(StorageBusyError):
            storage.set_preferences({"ui.opacity_percent": 80})
    finally:
        elapsed = time.perf_counter() - started
        blocker.rollback()
        blocker.close()
        storage.close()

    # The preference mirror is best effort and must never inherit SQLite's
    # multi-second default wait on the GUI thread.
    assert elapsed < 0.5


def test_logging_redacts_registered_and_key_value_secrets(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    sentinel = "registered-secret-value"
    context = setup_logging(paths, ssh_debug_enabled=True, redaction_values=[sentinel])
    context.logger.error("failure password=%s raw=%s", "inline-secret", sentinel)
    context.ssh_logger.debug('payload={"enable_secret":"second-secret"}')
    for logger in (context.logger, context.ssh_logger):
        for handler in logger.handlers:
            handler.flush()

    combined = paths.app_log.read_text(encoding="utf-8") + paths.ssh_debug_log.read_text(encoding="utf-8")
    assert sentinel not in combined
    assert "inline-secret" not in combined
    assert "second-secret" not in combined
    assert combined.count("[REDACTED]") >= 3

    # A second setup replaces handlers rather than duplicating every line.
    setup_logging(paths)
    logging.getLogger("aruba_mini_dashboard").info("one-line")
    for handler in logging.getLogger("aruba_mini_dashboard").handlers:
        handler.flush()
    assert paths.app_log.read_text(encoding="utf-8").count("one-line") == 1


def test_ssh_debug_log_can_be_enabled_and_disabled_at_runtime(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    context = setup_logging(paths, ssh_debug_enabled=False)
    context.ssh_logger.debug("disabled-line")
    assert not paths.ssh_debug_log.exists()

    context.set_ssh_debug_enabled(True)
    context.ssh_logger.debug("enabled-line")
    for handler in context.ssh_logger.handlers:
        handler.flush()
    assert "enabled-line" in paths.ssh_debug_log.read_text(encoding="utf-8")

    context.set_ssh_debug_enabled(False)
    context.ssh_logger.debug("disabled-again")
    assert "disabled-again" not in paths.ssh_debug_log.read_text(encoding="utf-8")


def test_performance_log_is_optional_sanitized_and_bounded(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    context = setup_logging(
        paths,
        low_spec_mode=True,
        performance_logging_enabled=False,
    )
    assert not paths.performance_log.exists()
    app_handler = context.logger.handlers[0]
    assert app_handler.maxBytes == 2 * 1024 * 1024
    assert app_handler.backupCount == 2

    context.set_performance_logging_enabled(True)
    context.performance_logger.info("poll_complete duration_ms=12 password=hidden")
    for handler in context.performance_logger.handlers:
        handler.flush()
        assert handler.maxBytes == 1024 * 1024
        assert handler.backupCount == 2
    content = paths.performance_log.read_text(encoding="utf-8")
    assert "duration_ms=12" in content
    assert "hidden" not in content
    assert "[REDACTED]" in content


def test_python_exception_inside_write_rolls_back_and_keeps_connection_usable(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "app.db")

    def fail_after_statement(db) -> None:
        db.execute(
            "INSERT INTO preferences(key, value_json, updated_at) VALUES (?, ?, ?)",
            ("partial", '"must-rollback"', "2026-08-12T00:00:00+00:00"),
        )
        raise ValueError("synthetic serialization failure")

    with pytest.raises(ValueError, match="synthetic serialization failure"):
        storage._write(fail_after_statement)

    assert storage._read(lambda db: db.in_transaction) is False
    assert storage.get_setting("partial") is None
    storage.set_setting("after_failure", "usable")
    assert storage.get_setting("after_failure") == "usable"
    storage.close()
