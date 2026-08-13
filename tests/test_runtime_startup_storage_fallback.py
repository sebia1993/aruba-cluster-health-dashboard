from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aruba_mini_dashboard.config import AppPaths, AppSettings
from aruba_mini_dashboard.credentials import CredentialService, SessionCredentialStore
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.main import (
    RuntimePoller,
    _create_runtime_with_storage_fallback,
)
from aruba_mini_dashboard.models import (
    HealthSignal,
    Incident,
    IncidentType,
    OverallHealth,
    Severity,
)
from aruba_mini_dashboard.storage import (
    SQLiteStorage,
    StorageBusyError,
    StorageCorruptError,
)


MEMBER_IPS = tuple(f"192.0.2.{index}" for index in range(11, 15))


def _configured_settings() -> AppSettings:
    settings = AppSettings.default()
    settings.polling.automatic_enabled = True
    for member, ip in zip(settings.cluster.members, MEMBER_IPS, strict=True):
        member.ip = ip
    return settings


def _seed_active_incident(storage: SQLiteStorage) -> Incident:
    observed_at = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
    incident = Incident(
        incident_id="durable-active-before-restore",
        incident_type=IncidentType.CLIENT_DISTRIBUTION,
        severity=Severity.WARNING,
        reason="fixture anomaly",
        first_detected_at=observed_at,
        last_seen_at=observed_at,
        ip=MEMBER_IPS[0],
    )
    storage.save_domain_incident(incident)
    return incident


def _database_hash(storage: SQLiteStorage, path: Path) -> str:
    storage._require_connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("method_name", "method_kwargs"),
    (
        ("load_domain_connection_baselines", {}),
        ("load_streaks", {}),
        ("load_pending_connection_changes", {}),
        ("load_domain_incidents", {"active_only": True}),
        ("load_runtime_inventory", {}),
    ),
)
def test_runtime_never_treats_storage_restore_failure_as_empty_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
    method_kwargs: dict[str, object],
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    logging_context = setup_logging(paths)

    def unavailable(*_args: object, **kwargs: object) -> object:
        if method_kwargs:
            assert kwargs == method_kwargs
        raise StorageBusyError("fixture detail must not be exposed")

    monkeypatch.setattr(storage, method_name, unavailable)
    try:
        with pytest.raises(StorageBusyError):
            RuntimePoller(
                _configured_settings(),
                paths,
                CredentialService(persistent=SessionCredentialStore()),
                storage,
                logging_context,
            )
    finally:
        logging_context.close()
        storage.close()


@pytest.mark.parametrize(
    ("error_type", "expected_fragment"),
    (
        (StorageBusyError, "사용 중"),
        (StorageCorruptError, "손상"),
    ),
)
def test_runtime_restore_failure_uses_isolated_memory_without_mutating_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_type: type[Exception],
    expected_fragment: str,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    durable = _seed_active_incident(storage)
    before_hash = _database_hash(storage, paths.database)
    settings = _configured_settings()
    logging_context = setup_logging(paths)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise error_type("private fixture path and raw database detail")

    monkeypatch.setattr(storage, "load_domain_incidents", unavailable)
    fallback: SQLiteStorage | None = None
    try:
        runtime, fallback, startup_error = _create_runtime_with_storage_fallback(
            settings,
            paths,
            CredentialService(persistent=SessionCredentialStore()),
            storage,
            logging_context,
        )

        assert fallback is not storage
        assert storage._connection is None
        assert settings.polling.automatic_enabled is False
        assert expected_fragment in startup_error
        assert "private fixture" not in startup_error
        assert runtime.incident_manager.events() == []

        # A reconfirmed signal is safe in the isolated store and cannot collide
        # with or overwrite the durable incident that could not be restored.
        observed_at = durable.last_seen_at + timedelta(minutes=1)
        health = OverallHealth(
            checked_at=observed_at,
            severity=Severity.WARNING,
            devices=[],
            monitoring_scope_ips=MEMBER_IPS,
            signals=[
                HealthSignal(
                    incident_type=IncidentType.CLIENT_DISTRIBUTION,
                    severity=Severity.WARNING,
                    reason=durable.reason,
                    ip=durable.ip,
                    source="fixture",
                )
            ],
        )
        runtime._persist_incidents(
            runtime.incident_manager.process(health, now=observed_at)
        )
        assert len(fallback.load_domain_incidents(active_only=True)) == 1
        assert runtime._pending_persistence_transitions == []
        assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before_hash
    finally:
        if fallback is not None:
            fallback.close()
        logging_context.close()

    reopened = SQLiteStorage(paths)
    try:
        persisted = reopened.load_domain_incidents(active_only=True)
        assert [item.incident_id for item in persisted] == [durable.incident_id]
        assert reopened.list_events() == []
    finally:
        reopened.close()


def test_normal_runtime_restore_keeps_durable_incident_identity(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    durable = _seed_active_incident(storage)
    settings = _configured_settings()
    logging_context = setup_logging(paths)
    try:
        runtime, selected_storage, startup_error = (
            _create_runtime_with_storage_fallback(
                settings,
                paths,
                CredentialService(persistent=SessionCredentialStore()),
                storage,
                logging_context,
            )
        )

        assert selected_storage is storage
        assert startup_error == ""
        assert settings.polling.automatic_enabled is True
        assert [item.incident_id for item in runtime.incident_manager.active_incidents()] == [
            durable.incident_id
        ]
    finally:
        logging_context.close()
        storage.close()


def test_duplicate_logical_active_incidents_are_isolated_without_db_mutation(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    first = _seed_active_incident(storage)
    second = Incident(
        incident_id="durable-conflicting-active",
        incident_type=first.incident_type,
        severity=first.severity,
        reason="changed fixture anomaly",
        first_detected_at=first.first_detected_at + timedelta(seconds=1),
        last_seen_at=first.last_seen_at + timedelta(seconds=1),
        ip=first.ip,
    )
    # The SQL reason_key differs, so a legacy runtime that lost its in-memory
    # identity could have persisted both rows even though they represent one
    # IncidentManager key.
    storage.save_domain_incident(second)
    before_hash = _database_hash(storage, paths.database)
    settings = _configured_settings()
    logging_context = setup_logging(paths)
    fallback: SQLiteStorage | None = None
    try:
        runtime, fallback, startup_error = _create_runtime_with_storage_fallback(
            settings,
            paths,
            CredentialService(persistent=SessionCredentialStore()),
            storage,
            logging_context,
        )

        assert fallback is not storage
        assert storage._connection is None
        assert settings.polling.automatic_enabled is False
        assert "손상" in startup_error
        assert runtime.incident_manager.events() == []
        assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before_hash
    finally:
        if fallback is not None:
            fallback.close()
        logging_context.close()

    reopened = SQLiteStorage(paths)
    try:
        assert {item.incident_id for item in reopened.load_domain_incidents(active_only=True)} == {
            first.incident_id,
            second.incident_id,
        }
    finally:
        reopened.close()


def test_atomic_inventory_cleanup_happens_before_bulk_result_materialization(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    old = datetime.now(timezone.utc) - timedelta(days=181)
    stale_ip = "192.0.2.99"
    storage.save_device_state(
        stale_ip,
        {"ip": stale_ip, "hostname": "STALE"},
        observed_at=old,
        is_normal=True,
    )
    storage.save_mm_discovered_device(
        stale_ip,
        hostname="STALE",
        last_seen_at=old,
    )
    try:
        removed, devices, discovered = storage.load_runtime_inventory(MEMBER_IPS)

        assert removed == {stale_ip}
        assert stale_ip not in devices
        assert all(row["ip"] != stale_ip for row in discovered)
    finally:
        storage.close()


def test_atomic_inventory_validation_failure_rolls_cleanup_back(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    old = datetime.now(timezone.utc) - timedelta(days=181)
    stale_ip = "192.0.2.99"
    invalid_ip = "192.0.2.98"
    storage.save_device_state(
        stale_ip,
        {"ip": stale_ip},
        observed_at=old,
        is_normal=True,
    )
    storage.save_mm_discovered_device(stale_ip, last_seen_at=old)
    storage._write(
        lambda db: db.execute(
            "INSERT INTO device_states(ip, payload_json, observed_at, is_normal) "
            "VALUES (?, ?, ?, 1)",
            (
                invalid_ip,
                '{"duplicate": 1, "duplicate": 2}',
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    )

    with pytest.raises(StorageCorruptError):
        storage.load_runtime_inventory(MEMBER_IPS)

    # Cleanup selected the old row before parsing the malformed current row,
    # but both the deletion and its retention marker were rolled back.
    remaining = storage._read(
        lambda db: {
            str(row["ip"])
            for row in db.execute("SELECT ip FROM device_states").fetchall()
        }
    )
    marker = storage._read(
        lambda db: db.execute(
            "SELECT 1 FROM preferences WHERE key=?",
            ("_device_inventory_retention_last_run",),
        ).fetchone()
    )
    assert remaining == {stale_ip, invalid_ip}
    assert marker is None
    storage.close()


def test_incomplete_scope_loads_inventory_without_cleanup(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    old = datetime.now(timezone.utc) - timedelta(days=181)
    stale_ip = "192.0.2.99"
    storage.save_device_state(
        stale_ip,
        {"ip": stale_ip},
        observed_at=old,
        is_normal=True,
    )
    try:
        removed, devices, _discovered = storage.load_runtime_inventory(None)

        assert removed == set()
        assert stale_ip in devices
    finally:
        storage.close()


def test_active_incident_restore_accepts_exact_cap_and_rejects_overflow(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(paths)
    observed_at = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc).isoformat()

    def insert_rows(db: object, start: int, stop: int) -> None:
        db.executemany(
            "INSERT INTO incidents(incident_id, ip, incident_type, reason_key, "
            "first_detected_at, last_seen_at, resolved_at, active, acknowledged, "
            "payload_json) VALUES (?, '', 'collection_failure', ?, ?, ?, NULL, 1, 0, ?)",
            (
                (
                    f"incident-{index}",
                    f"event-{index}",
                    observed_at,
                    observed_at,
                    '{"severity":"warning","reason":"fixture",'
                    f'"event_token":"event-{index}","details":{{}}}}',
                )
                for index in range(start, stop)
            ),
        )

    storage._write(lambda db: insert_rows(db, 0, 10_000))
    assert len(storage.load_domain_incidents(active_only=True)) == 10_000

    storage._write(lambda db: insert_rows(db, 10_000, 10_001))
    with pytest.raises(StorageCorruptError, match="복원 한도"):
        storage.load_domain_incidents(active_only=True)
    storage.close()
