from __future__ import annotations

import json
import logging
import hashlib
import io
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aruba_mini_dashboard.storage as storage_module
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
    MAX_STORAGE_JSON_BYTES,
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
        assert storage.schema_version == SCHEMA_VERSION
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
    # Start from the complete previous schema contract, then reshape only the
    # v4 tables/indexes back to v3. A partial hand-built SQLite file is corrupt
    # and must no longer be accepted merely because ``quick_check`` says OK.
    with SQLiteStorage(paths):
        pass
    legacy = sqlite3.connect(paths.database)
    legacy.executescript(
        """
        DROP INDEX idx_connection_changes_one_pending_member;
        ALTER TABLE connection_baselines RENAME TO connection_baselines_v4;
        CREATE TABLE connection_baselines (
            source_controller_ip TEXT NOT NULL,
            member_ip TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            normalized_connection_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_controller_ip, member_ip)
        );
        DROP TABLE connection_baselines_v4;
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
        assert migrated.schema_version == SCHEMA_VERSION
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


def test_v4_migration_repairs_status_pollution_without_losing_real_changes(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    with SQLiteStorage(paths):
        pass

    observed = "2026-08-11T01:30:00+00:00"
    false_token = "false-status-token"
    true_token = "real-type-token"

    def incident_payload(token: str, previous: str, current: str) -> str:
        return json.dumps(
            {
                "severity": "warning",
                "reason": f"Connection-Type 변경: {previous} → {current}",
                "alias": None,
                "acknowledged_at": None,
                "recovered_at": None,
                "last_notified_at": None,
                "event_token": token,
                "details": {"previous": previous, "current": current},
            },
            ensure_ascii=False,
        )

    legacy = sqlite3.connect(paths.database)
    legacy.executemany(
        """INSERT INTO connection_baselines
               (source_controller_ip, member_ip, connection_type,
                normalized_connection_type, observed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            (
                "192.0.2.11",
                "192.0.2.12",
                "L2-Connected CONNECTED (Member, last HBT_RSP 67ms ago, RTD = 0.125 ms)",
                "legacy-false-normalized",
                observed,
            ),
            (
                "192.0.2.11",
                "192.0.2.13",
                "L2-Connected CONNECTED (Member, last HBT_RSP 82ms ago, RTD = 0.396 ms)",
                "legacy-true-normalized",
                observed,
            ),
            (
                "192.0.2.11",
                "192.0.2.14",
                "Vendor-Future CONNECTED (Member)",
                "vendor-future-connected(member)",
                observed,
            ),
        ),
    )
    false_previous = (
        "L2-Connected CONNECTED (Member, last HBT_RSP 44ms ago, RTD = 0.000 ms)"
    )
    false_current = (
        "L2-Connected CONNECTED (Member, last HBT_RSP 67ms ago, RTD = 0.125 ms)"
    )
    true_previous = "N/A CONNECTED (Leader)"
    true_current = (
        "L2-Connected CONNECTED (Member, last HBT_RSP 82ms ago, RTD = 0.396 ms)"
    )
    legacy.executemany(
        """INSERT INTO connection_changes
               (event_token, collector_ip, member_ip, previous_value, current_value,
                first_detected_at, last_confirmed_at, acknowledged)
           VALUES (?, '192.0.2.11', ?, ?, ?, ?, ?, 0)""",
        (
            (
                false_token,
                "192.0.2.12",
                false_previous,
                false_current,
                observed,
                observed,
            ),
            (
                true_token,
                "192.0.2.13",
                true_previous,
                true_current,
                observed,
                observed,
            ),
        ),
    )
    legacy.executemany(
        """INSERT INTO incidents
               (incident_id, ip, incident_type, reason_key, first_detected_at,
                last_seen_at, resolved_at, active, acknowledged, payload_json)
           VALUES (?, ?, 'connection_type_changed', ?, ?, ?, NULL, 1, 0, ?)""",
        (
            (
                "false-incident",
                "192.0.2.12",
                false_token,
                observed,
                observed,
                incident_payload(false_token, false_previous, false_current),
            ),
            (
                "true-incident",
                "192.0.2.13",
                true_token,
                observed,
                observed,
                incident_payload(true_token, true_previous, true_current),
            ),
        ),
    )
    legacy.execute(
        """INSERT INTO events(event_type, ip, incident_id, occurred_at, payload_json)
           VALUES ('activated', '192.0.2.12', 'false-incident', ?, '{}')""",
        (observed,),
    )
    legacy.execute("PRAGMA user_version=4")
    legacy.commit()
    legacy.close()

    with SQLiteStorage(paths) as migrated:
        assert migrated.schema_version == 5
        baselines = {
            item.member_ip: item for item in migrated.load_connection_baselines()
        }
        assert baselines["192.0.2.12"].connection_type == "L2-Connected"
        assert baselines["192.0.2.12"].normalized_connection_type == "l2connected"
        assert baselines["192.0.2.13"].connection_type == "L2-Connected"
        assert baselines["192.0.2.14"].connection_type == "Vendor-Future CONNECTED (Member)"

        pending = migrated.load_pending_connection_changes()
        assert len(pending) == 1
        assert pending[0].event_token == true_token
        assert pending[0].previous_value == "N/A"
        assert pending[0].current_value == "L2-Connected"

        changes = migrated._read(
            lambda db: db.execute(
                """SELECT event_token, previous_value, current_value, acknowledged
                   FROM connection_changes ORDER BY event_token"""
            ).fetchall()
        )
        assert [tuple(row) for row in changes] == [
            (false_token, "L2-Connected", "L2-Connected", 1),
            (true_token, "N/A", "L2-Connected", 0),
        ]

        incidents = {item.incident_id: item for item in migrated.list_incidents()}
        assert incidents["false-incident"].active is False
        assert incidents["false-incident"].acknowledged is True
        assert incidents["false-incident"].payload["acknowledged_at"] is not None
        assert incidents["false-incident"].payload["details"]["automatic_cleanup"] == (
            "legacy_connection_status_column_v5"
        )
        assert incidents["true-incident"].active is True
        assert incidents["true-incident"].acknowledged is False
        assert [item.incident_id for item in migrated.load_domain_incidents(active_only=True)] == [
            "true-incident"
        ]
        assert len(migrated.list_events()) == 1


def test_v4_connection_cleanup_rolls_back_all_rows_when_incident_payload_is_corrupt(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    with SQLiteStorage(paths):
        pass
    observed = "2026-08-11T01:30:00+00:00"
    legacy = sqlite3.connect(paths.database)
    legacy.execute(
        """INSERT INTO connection_baselines
               (source_controller_ip, member_ip, connection_type,
                normalized_connection_type, observed_at)
           VALUES ('192.0.2.11', '192.0.2.12',
                   'L2-Connected CONNECTED (Member, last HBT_RSP 67ms ago)',
                   'polluted', ?)""",
        (observed,),
    )
    legacy.execute(
        """INSERT INTO connection_changes
               (event_token, collector_ip, member_ip, previous_value, current_value,
                first_detected_at, last_confirmed_at, acknowledged)
           VALUES ('false-token', '192.0.2.11', '192.0.2.12',
                   'L2-Connected CONNECTED (Member, last HBT_RSP 44ms ago)',
                   'L2-Connected CONNECTED (Member, last HBT_RSP 67ms ago)',
                   ?, ?, 0)""",
        (observed, observed),
    )
    legacy.execute(
        """INSERT INTO incidents
               (incident_id, ip, incident_type, reason_key, first_detected_at,
                last_seen_at, resolved_at, active, acknowledged, payload_json)
           VALUES ('false-incident', '192.0.2.12', 'connection_type_changed',
                   'false-token', ?, ?, NULL, 1, 0, '{')""",
        (observed, observed),
    )
    legacy.execute("PRAGMA user_version=4")
    legacy.commit()
    legacy.close()

    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)

    preserved = sqlite3.connect(paths.database)
    try:
        assert preserved.execute("PRAGMA user_version").fetchone()[0] == 4
        assert preserved.execute(
            "SELECT connection_type FROM connection_baselines"
        ).fetchone()[0].startswith("L2-Connected CONNECTED")
        assert preserved.execute(
            "SELECT acknowledged FROM connection_changes"
        ).fetchone()[0] == 0
        assert preserved.execute("SELECT active FROM incidents").fetchone()[0] == 1
    finally:
        preserved.close()


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


@pytest.mark.parametrize(
    "secret_key",
    (
        "password",
        "api_token",
        "sharedSecret",
        "credential-blob",
        "apiKey",
        "private_key",
        "Authorization",
        "password_value",
        "token_value",
        "access_token_value",
        "secret_key",
        "credentialBlobValue",
        "apiKeyValue",
        "private_key_value",
        "authorizationHeader",
        "clientsecret",
        "userpassword",
        "dbpassword",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
    ),
)
def test_storage_rejects_secret_fields_and_secret_never_reaches_file(
    tmp_path: Path,
    secret_key: str,
) -> None:
    paths = make_paths(tmp_path)
    sentinel = "DO-NOT-PERSIST-SECRET"
    with SQLiteStorage(paths) as storage:
        with pytest.raises(ValueError, match="secret-bearing"):
            storage.set_preference("unsafe", {secret_key: sentinel})
        storage.set_preference("safe", {"credential_id": "opaque-id"})
    assert sentinel.encode() not in paths.database.read_bytes()


def test_storage_secret_rejection_and_logged_exception_do_not_echo_untrusted_key(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    canary = "CANARY-SECRET-IN-KEY"
    with SQLiteStorage(paths) as storage:
        with pytest.raises(ValueError, match="secret-bearing") as raised:
            storage.set_preference("unsafe", {f"password_{canary}": "ordinary-value"})

    assert canary not in str(raised.value)
    context = setup_logging(paths)
    context.logger.error("preference validation failed: %s", raised.value)
    for handler in context.logger.handlers:
        handler.flush()
    assert canary not in paths.app_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("preference_key", ("password", "api_token", "clientsecret"))
def test_storage_rejects_secret_bearing_outer_preference_keys(
    tmp_path: Path,
    preference_key: str,
) -> None:
    paths = make_paths(tmp_path)
    canary = "OUTER-PREFERENCE-SECRET-CANARY"
    with SQLiteStorage(paths) as storage:
        with pytest.raises(ValueError, match="secret-bearing") as raised:
            storage.set_preference(preference_key, canary)
        with pytest.raises(ValueError, match="secret-bearing"):
            storage.set_preferences({preference_key: canary})
        assert storage.get_preference(preference_key) is None

    assert preference_key not in str(raised.value).casefold()
    assert canary not in str(raised.value)
    assert canary.encode() not in paths.database.read_bytes()


@pytest.mark.parametrize(
    "safe_key",
    (
        "event_token",
        "durable_event_token",
        "event_tokens",
        "tokenizer",
        "secretary_name",
        "passwordless",
    ),
)
def test_storage_allows_non_secret_token_like_fields(tmp_path: Path, safe_key: str) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.set_preference("safe", {safe_key: "ordinary-value"})

        assert storage.get_preference("safe") == {safe_key: "ordinary-value"}


def test_corrupt_database_is_preserved(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True)
    paths.database.write_bytes(b"not a sqlite database")
    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)
    assert paths.database.read_bytes() == b"not a sqlite database"


def test_incomplete_current_schema_is_rejected_and_preserved(tmp_path: Path) -> None:
    paths = make_paths(tmp_path / "private operator path")
    paths.database.parent.mkdir(parents=True)
    partial = sqlite3.connect(paths.database)
    partial.execute("CREATE TABLE unrelated(value TEXT)")
    partial.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    partial.commit()
    partial.close()
    original_hash = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    storage = SQLiteStorage(paths, initialize=False)

    with pytest.raises(StorageCorruptError) as raised:
        storage.initialize()

    assert storage._connection is None
    assert "private operator path" not in str(raised.value)
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == original_hash


def _assert_edited_current_schema_is_rejected(paths: AppPaths, script: str) -> None:
    with SQLiteStorage(paths):
        pass
    edited = sqlite3.connect(paths.database)
    try:
        edited.executescript(script)
    finally:
        edited.close()
    storage = SQLiteStorage(paths, initialize=False)

    with pytest.raises(StorageCorruptError):
        storage.initialize()

    assert storage._connection is None


def test_current_schema_missing_preferences_primary_key_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    _assert_edited_current_schema_is_rejected(
        paths,
        """
        ALTER TABLE preferences RENAME TO preferences_valid;
        CREATE TABLE preferences (
            key TEXT,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO preferences SELECT * FROM preferences_valid;
        DROP TABLE preferences_valid;
        """,
    )


def test_current_schema_reversed_composite_primary_key_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    _assert_edited_current_schema_is_rejected(
        paths,
        """
        ALTER TABLE detector_streaks RENAME TO detector_streaks_valid;
        CREATE TABLE detector_streaks (
            detector TEXT NOT NULL,
            ip TEXT NOT NULL,
            anomaly_count INTEGER NOT NULL,
            recovery_count INTEGER NOT NULL,
            active INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ip, detector)
        );
        INSERT INTO detector_streaks SELECT * FROM detector_streaks_valid;
        DROP TABLE detector_streaks_valid;
        """,
    )


def test_current_schema_missing_important_not_null_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    _assert_edited_current_schema_is_rejected(
        paths,
        """
        ALTER TABLE preferences RENAME TO preferences_valid;
        CREATE TABLE preferences (
            key TEXT PRIMARY KEY,
            value_json TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO preferences SELECT * FROM preferences_valid;
        DROP TABLE preferences_valid;
        """,
    )


def test_current_schema_missing_detector_check_constraints_is_rejected(
    tmp_path: Path,
) -> None:
    _assert_edited_current_schema_is_rejected(
        make_paths(tmp_path),
        """
        ALTER TABLE detector_streaks RENAME TO detector_streaks_valid;
        CREATE TABLE detector_streaks (
            detector TEXT NOT NULL,
            ip TEXT NOT NULL,
            anomaly_count INTEGER NOT NULL,
            recovery_count INTEGER NOT NULL,
            active INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(detector, ip)
        );
        INSERT INTO detector_streaks
            VALUES ('load', '192.0.2.10', -4, -9, 7, '2026-08-13T00:00:00+00:00');
        DROP TABLE detector_streaks_valid;
        """,
    )


def test_current_schema_check_text_in_comment_does_not_satisfy_contract(
    tmp_path: Path,
) -> None:
    _assert_edited_current_schema_is_rejected(
        make_paths(tmp_path),
        """
        ALTER TABLE detector_streaks RENAME TO detector_streaks_valid;
        CREATE TABLE detector_streaks (
            detector TEXT NOT NULL,
            ip TEXT NOT NULL,
            anomaly_count INTEGER NOT NULL,
            recovery_count INTEGER NOT NULL,
            active INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(detector, ip)
            /* CHECK(anomaly_count >= 0)
               CHECK(recovery_count >= 0)
               CHECK(active IN (0, 1)) */
        );
        INSERT INTO detector_streaks
            VALUES ('load', '192.0.2.10', -4, -9, 7, '2026-08-13T00:00:00+00:00');
        DROP TABLE detector_streaks_valid;
        """,
    )


def test_current_schema_extra_column_is_rejected_before_dataclass_reader(
    tmp_path: Path,
) -> None:
    _assert_edited_current_schema_is_rejected(
        make_paths(tmp_path),
        "ALTER TABLE detector_streaks ADD COLUMN injected TEXT;",
    )


def test_current_schema_null_text_primary_keys_fail_closed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    with SQLiteStorage(paths):
        pass
    edited = sqlite3.connect(paths.database)
    edited.execute(
        "INSERT INTO preferences(key, value_json, updated_at) VALUES(NULL, '1', '2026-08-13T00:00:00+00:00')"
    )
    edited.execute(
        "INSERT INTO preferences(key, value_json, updated_at) VALUES(NULL, '2', '2026-08-13T00:00:01+00:00')"
    )
    edited.commit()
    edited.close()

    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)


def test_corrupt_v3_preflight_preserves_version_and_duplicate_baselines(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    with SQLiteStorage(paths):
        pass
    legacy = sqlite3.connect(paths.database)
    legacy.executescript(
        """
        DROP INDEX idx_connection_changes_one_pending_member;
        ALTER TABLE connection_baselines RENAME TO connection_baselines_v4;
        CREATE TABLE connection_baselines (
            source_controller_ip TEXT NOT NULL,
            member_ip TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            normalized_connection_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_controller_ip, member_ip)
        );
        DROP TABLE connection_baselines_v4;
        INSERT INTO connection_baselines VALUES
            ('192.0.2.11', '192.0.2.12', 'Type-A', 'type a', '2026-08-11T01:00:00+00:00'),
            ('192.0.2.13', '192.0.2.12', 'Type-B', 'type b', '2026-08-11T01:01:00+00:00');
        ALTER TABLE preferences RENAME TO preferences_valid;
        CREATE TABLE preferences (
            key TEXT,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO preferences SELECT * FROM preferences_valid;
        DROP TABLE preferences_valid;
        PRAGMA user_version=3;
        """
    )
    before_rows = legacy.execute(
        "SELECT * FROM connection_baselines ORDER BY observed_at"
    ).fetchall()
    legacy.close()

    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)

    preserved = sqlite3.connect(paths.database)
    try:
        assert preserved.execute("PRAGMA user_version").fetchone()[0] == 3
        assert preserved.execute(
            "SELECT * FROM connection_baselines ORDER BY observed_at"
        ).fetchall() == before_rows
    finally:
        preserved.close()


def test_nonempty_unversioned_database_is_preserved_before_any_migration(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True)
    legacy = sqlite3.connect(paths.database)
    legacy.executescript(
        """
        CREATE TABLE preferences (
            key TEXT,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE connection_baselines (
            source_controller_ip TEXT NOT NULL,
            member_ip TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            normalized_connection_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_controller_ip, member_ip)
        );
        INSERT INTO connection_baselines VALUES
            ('192.0.2.11', '192.0.2.12', 'Type-A', 'type a', '2026-08-11T01:00:00+00:00'),
            ('192.0.2.13', '192.0.2.12', 'Type-B', 'type b', '2026-08-11T01:01:00+00:00');
        """
    )
    before_rows = legacy.execute(
        "SELECT * FROM connection_baselines ORDER BY observed_at"
    ).fetchall()
    before_preferences_sql = legacy.execute(
        "SELECT sql FROM sqlite_schema WHERE name='preferences'"
    ).fetchone()[0]
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
    legacy.close()
    before_hash = hashlib.sha256(paths.database.read_bytes()).hexdigest()

    storage = SQLiteStorage(paths, initialize=False)
    with pytest.raises(StorageCorruptError):
        storage.initialize()
    assert storage._connection is None

    preserved = sqlite3.connect(paths.database)
    try:
        assert preserved.execute("PRAGMA user_version").fetchone()[0] == 0
        assert preserved.execute(
            "SELECT * FROM connection_baselines ORDER BY observed_at"
        ).fetchall() == before_rows
        assert preserved.execute(
            "SELECT sql FROM sqlite_schema WHERE name='preferences'"
        ).fetchone()[0] == before_preferences_sql
    finally:
        preserved.close()
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before_hash


def test_empty_unversioned_sqlite_file_remains_a_valid_first_run(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.database.parent.mkdir(parents=True)
    sqlite3.connect(paths.database).close()

    with SQLiteStorage(paths) as storage:
        assert storage.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("table", "required_check", "weakened_check"),
    (
        ("detector_streaks", "CHECK (anomaly_count >= 0)", "CHECK (anomaly_count >= -1)"),
        ("detector_streaks", "CHECK (recovery_count >= 0)", "CHECK (recovery_count >= -1)"),
        ("detector_streaks", "CHECK (active IN (0, 1))", "CHECK (active IN (0, 1, 7))"),
        ("device_states", "CHECK (is_normal IN (0, 1))", "CHECK (is_normal IN (0, 1, 7))"),
        ("incidents", "CHECK (active IN (0, 1))", "CHECK (active IN (0, 1, 7))"),
        (
            "incidents",
            "CHECK (acknowledged IN (0, 1))",
            "CHECK (acknowledged IN (0, 1, 7))",
        ),
        (
            "connection_changes",
            "CHECK (acknowledged IN (0, 1))",
            "CHECK (acknowledged IN (0, 1, 7))",
        ),
    ),
)
def test_current_schema_weakened_core_check_meaning_is_rejected(
    tmp_path: Path,
    table: str,
    required_check: str,
    weakened_check: str,
) -> None:
    _assert_edited_current_schema_is_rejected(
        make_paths(tmp_path),
        f"""
        PRAGMA writable_schema=ON;
        UPDATE sqlite_schema
           SET sql=replace(sql, '{required_check}', '{weakened_check}')
         WHERE type='table' AND name='{table}';
        PRAGMA writable_schema=OFF;
        """,
    )


@pytest.mark.parametrize(
    ("insert_sql", "loader_name", "loader_kwargs"),
    (
        (
            """INSERT INTO detector_streaks VALUES
               ('load', '192.0.2.61', -4, 1.5, 7, '2026-08-13T00:00:00+00:00')""",
            "load_streaks",
            {},
        ),
        (
            """INSERT INTO device_states VALUES
               ('192.0.2.62', '{}', '2026-08-13T00:00:00+00:00', 7)""",
            "load_device_states",
            {},
        ),
        (
            """INSERT INTO mm_discovered_devices VALUES
               ('192.0.2.63', '', '', '2026-08-13T00:00:00+00:00', -1, 1.5)""",
            "load_mm_discovered_devices",
            {},
        ),
        (
            """INSERT INTO incidents VALUES
               ('bad-flags', '192.0.2.64', 'controller_down', 'reason',
                '2026-08-13T00:00:00+00:00', '2026-08-13T00:00:00+00:00',
                NULL, 7, -1, '{}')""",
            "list_incidents",
            {"active_only": True},
        ),
        (
            """INSERT INTO connection_changes VALUES
               ('bad-ack', '192.0.2.1', '192.0.2.65', 'a', 'b',
                '2026-08-13T00:00:00+00:00', '2026-08-13T00:00:00+00:00', 7)""",
            "load_pending_connection_changes",
            {},
        ),
    ),
)
def test_integer_affinity_bypass_rows_fail_closed_at_reader_and_restart(
    tmp_path: Path,
    insert_sql: str,
    loader_name: str,
    loader_kwargs: dict[str, object],
) -> None:
    paths = make_paths(tmp_path)
    storage = SQLiteStorage(paths)
    storage._read(lambda db: db.execute("PRAGMA ignore_check_constraints=ON"))
    storage._write(lambda db: db.execute(insert_sql))

    with pytest.raises(StorageCorruptError):
        getattr(storage, loader_name)(**loader_kwargs)
    storage.close()

    with pytest.raises(StorageCorruptError):
        SQLiteStorage(paths)


def test_current_schema_missing_required_unique_index_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    _assert_edited_current_schema_is_rejected(
        paths,
        "DROP INDEX idx_connection_changes_one_pending_member;",
    )


@pytest.mark.parametrize(
    "script",
    (
        """
        DROP INDEX idx_connection_changes_pending;
        CREATE INDEX idx_connection_changes_pending
            ON connection_changes(member_ip, acknowledged, first_detected_at);
        """,
        """
        DROP INDEX idx_events_occurred_at;
        CREATE INDEX idx_events_occurred_at ON events(occurred_at ASC);
        """,
    ),
)
def test_current_schema_wrong_index_column_sequence_or_direction_is_rejected(
    tmp_path: Path,
    script: str,
) -> None:
    _assert_edited_current_schema_is_rejected(make_paths(tmp_path), script)


@pytest.mark.parametrize(
    "script",
    (
        """
        DROP INDEX idx_connection_changes_one_pending_member;
        CREATE UNIQUE INDEX idx_connection_changes_one_pending_member
            ON connection_changes(member_ip);
        """,
        """
        DROP INDEX idx_connection_changes_one_pending_member;
        CREATE UNIQUE INDEX idx_connection_changes_one_pending_member
            ON connection_changes(member_ip) WHERE acknowledged=1;
        """,
        """
        DROP INDEX idx_incidents_active_reason;
        CREATE UNIQUE INDEX idx_incidents_active_reason
            ON incidents(ip, incident_type, reason_key);
        """,
        """
        DROP INDEX idx_incidents_active_reason;
        CREATE UNIQUE INDEX idx_incidents_active_reason
            ON incidents(ip, incident_type, reason_key) WHERE active=0;
        """,
    ),
)
def test_current_schema_missing_or_wrong_unique_partial_predicate_is_rejected(
    tmp_path: Path,
    script: str,
) -> None:
    _assert_edited_current_schema_is_rejected(make_paths(tmp_path), script)


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


def test_initialization_lock_is_busy_not_corruption_and_releases_handle(tmp_path: Path) -> None:
    database = tmp_path / "locked startup.db"
    blocker = sqlite3.connect(database, timeout=0, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    storage = SQLiteStorage(database, busy_timeout_ms=5, lock_retries=0, initialize=False)
    try:
        with pytest.raises(StorageBusyError):
            storage.initialize()
        assert storage._connection is None
    finally:
        blocker.rollback()
        blocker.close()


def test_preferences_are_loaded_in_one_batch(tmp_path: Path) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.set_preferences({"one": 1, "two": False, "three": "value"})
        assert storage.get_preferences(("one", "three", "missing")) == {
            "one": 1,
            "three": "value",
        }


def test_corrupt_stored_json_raises_sanitized_storage_error(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.set_preference("bad", True)
        storage.save_device_state("192.0.2.12", {"ok": True}, is_normal=False)
        storage.upsert_incident(
            "bad-incident",
            "192.0.2.12",
            "mm_down",
            "down",
            first_detected_at=now,
            last_seen_at=now,
        )
        storage.append_event("activated", occurred_at=now)

        def corrupt_rows(db: sqlite3.Connection) -> None:
            db.execute("UPDATE preferences SET value_json='{' WHERE key='bad'")
            db.execute("UPDATE device_states SET payload_json='[' WHERE ip='192.0.2.12'")
            db.execute(
                "UPDATE incidents SET payload_json='null' WHERE incident_id='bad-incident'"
            )
            db.execute("UPDATE events SET payload_json='[1]' WHERE event_type='activated'")

        storage._write(corrupt_rows)

        for operation in (
            lambda: storage.get_preference("bad"),
            storage.load_device_states,
            storage.list_incidents,
            storage.list_events,
        ):
            with pytest.raises(StorageCorruptError, match="손상") as raised:
                operation()
            assert "192.0.2.12" not in str(raised.value)


def test_corrupt_stored_timestamp_does_not_escape_as_value_error(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.save_connection_baseline(
            "192.0.2.11",
            "192.0.2.12",
            "Type-A",
            observed_at=now,
        )
        storage._write(
            lambda db: db.execute(
                "UPDATE connection_baselines SET observed_at='not-a-timestamp'"
            )
        )

        with pytest.raises(StorageCorruptError, match="시간"):
            storage.get("192.0.2.12")


def test_invalid_timestamp_is_rejected_before_it_reaches_sqlite(tmp_path: Path) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        with pytest.raises(ValueError, match="ISO 8601"):
            storage.save_mm_discovered_device(
                "192.0.2.12",
                last_seen_at="not-a-timestamp",
            )
        assert storage.load_mm_discovered_devices() == []


def test_storage_json_read_and_write_size_are_bounded_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        with pytest.raises(ValueError, match="size"):
            storage.set_preference("too-large", "x" * MAX_STORAGE_JSON_BYTES)
        assert storage.get_preference("too-large") is None

        storage.set_preference("corrupt-large", True)
        storage._write(
            lambda db: db.execute(
                "UPDATE preferences SET value_json=? WHERE key='corrupt-large'",
                ("x" * (MAX_STORAGE_JSON_BYTES + 1),),
            )
        )

        def decode_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("oversized stored JSON reached json.loads")

        monkeypatch.setattr(storage_module.json, "loads", decode_must_not_run)
        with pytest.raises(StorageCorruptError, match="손상"):
            storage.get_preference("corrupt-large")


@pytest.mark.parametrize("encoded", ('{"nested":[1e999]}', '{"nested":[-1e999]}'))
def test_storage_rejects_nested_exponent_overflow_when_loading(
    tmp_path: Path,
    encoded: str,
) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        storage.set_preference("overflow", {"nested": [0.0]})
        storage._write(
            lambda db: db.execute(
                "UPDATE preferences SET value_json=? WHERE key='overflow'",
                (encoded,),
            )
        )

        with pytest.raises(StorageCorruptError, match="손상"):
            storage.get_preference("overflow")


@pytest.mark.parametrize("overflow", (float("inf"), float("-inf")))
def test_storage_rejects_nested_exponent_overflow_when_dumping(
    tmp_path: Path,
    overflow: float,
) -> None:
    with SQLiteStorage(make_paths(tmp_path)) as storage:
        with pytest.raises(ValueError, match="safe persistence"):
            storage.set_preference("overflow", {"nested": [overflow]})

        storage.set_preference("finite", {"nested": [1e308, -1e308]})
        assert storage.get_preference("finite") == {"nested": [1e308, -1e308]}


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


def test_history_retention_runs_when_saved_clock_marker_is_in_the_future(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=181)
    storage.maintain_history(now=now + timedelta(days=30), force=True)
    storage.append_event("old-after-clock-fix", occurred_at=old)

    removed = storage.maintain_history(now=now)

    assert removed["events"] == 1
    assert storage.list_events(limit=10) == []
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
        (malformed, old),
        (current, recent),
    ):
        _seed_inventory_row(storage, ip, observed_at)
    storage._write(
        lambda db: db.execute(
            "UPDATE mm_discovered_devices SET last_seen_at='not-a-timestamp' WHERE ip=?",
            (malformed,),
        )
    )
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


def test_device_inventory_retention_runs_after_clock_moves_back(tmp_path: Path) -> None:
    storage = SQLiteStorage(make_paths(tmp_path))
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=181)
    storage.maintain_device_inventory(set(), now=now + timedelta(days=30), force=True)
    _seed_inventory_row(storage, "192.0.2.51", old)

    removed = storage.maintain_device_inventory(set(), now=now)

    assert removed == {"192.0.2.51"}
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
    context.logger.error('api_token="quoted secret with spaces"')
    context.logger.error("shared_secret: 'another quoted secret'")
    context.logger.error("api-key=third-secret authorization=Bearer-fourth-secret")
    for logger in (context.logger, context.ssh_logger):
        for handler in logger.handlers:
            handler.flush()

    combined = paths.app_log.read_text(encoding="utf-8") + paths.ssh_debug_log.read_text(encoding="utf-8")
    assert sentinel not in combined
    assert "inline-secret" not in combined
    assert "second-secret" not in combined
    assert "quoted secret with spaces" not in combined
    assert "another quoted secret" not in combined
    assert "third-secret" not in combined
    assert "fourth-secret" not in combined
    assert combined.count("[REDACTED]") >= 7

    # A second setup replaces handlers rather than duplicating every line.
    replaced_handlers = [*context.logger.handlers, *context.ssh_logger.handlers]
    setup_logging(paths)
    assert all(getattr(handler, "stream", None) is None for handler in replaced_handlers)
    logging.getLogger("aruba_mini_dashboard").info("one-line")
    for handler in logging.getLogger("aruba_mini_dashboard").handlers:
        handler.flush()
    assert paths.app_log.read_text(encoding="utf-8").count("one-line") == 1


def test_logging_redacts_complete_authorization_values_and_preserves_normal_fields(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    context = setup_logging(paths)
    context.logger.error("Authorization: Bearer TOP-SECRET-TOKEN, duration_ms=12")
    context.logger.error("authorization=Basic dXNlcjpwYXNz; status=401")
    context.logger.error('authorization="Bearer QUOTED-AUTH-CANARY" retry=1')
    context.logger.error("password=PASSWORD-CANARY duration_ms=34")
    for handler in context.logger.handlers:
        handler.flush()

    content = paths.app_log.read_text(encoding="utf-8")
    for canary in (
        "TOP-SECRET-TOKEN",
        "dXNlcjpwYXNz",
        "QUOTED-AUTH-CANARY",
        "PASSWORD-CANARY",
    ):
        assert canary not in content
    assert content.count("[REDACTED]") >= 4
    assert "duration_ms=12" in content
    assert "status=401" in content
    assert "retry=1" in content
    assert "duration_ms=34" in content


def test_logging_uses_token_boundaries_for_secret_field_names_without_false_positives(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    context = setup_logging(paths)
    secret_messages = (
        "password_value=LOG-SECRET-ONE duration_ms=1",
        "token_value: 'LOG SECRET TWO', duration_ms=2",
        'secret-key="LOG SECRET THREE" duration_ms=3',
        "clientSecret=LOG-SECRET-FOUR duration_ms=4",
        "credentialBlobValue=LOG-SECRET-FIVE duration_ms=5",
        '"apiKeyValue": "LOG SECRET SIX" duration_ms=6',
        "prefix_password_suffix=LOG-SECRET-SEVEN duration_ms=7",
        "2fa_token=LOG-SECRET-EIGHT duration_ms=8",
        '"api key": "LOG SECRET NINE" duration_ms=9',
        "api key=LOG-SECRET-TEN duration_ms=10",
        "password.value=LOG-SECRET-ELEVEN duration_ms=11",
        "password/value=LOG-SECRET-TWELVE duration_ms=12",
        "password[value]=LOG-SECRET-THIRTEEN duration_ms=13",
        "api/key=LOG-SECRET-FOURTEEN duration_ms=14",
        "authorization/header=LOG-SECRET-FIFTEEN duration_ms=15",
        "clientsecret=LOG-SECRET-SIXTEEN duration_ms=16",
        "userpassword=LOG-SECRET-SEVENTEEN duration_ms=17",
        "dbpassword=LOG-SECRET-EIGHTEEN duration_ms=18",
        "accesstoken=LOG-SECRET-NINETEEN duration_ms=19",
        "refreshtoken=LOG-SECRET-TWENTY duration_ms=20",
        "bearertoken=LOG-SECRET-TWENTYONE duration_ms=21",
    )
    visible_messages = (
        "tokenizer=TOKENIZER-VISIBLE",
        'event_tokens="EVENT-TOKENS-VISIBLE"',
        "secretary_name=SECRETARY-VISIBLE",
    )
    for message in (*secret_messages, *visible_messages):
        context.logger.error(message)
    for handler in context.logger.handlers:
        handler.flush()

    content = paths.app_log.read_text(encoding="utf-8")
    for canary in (
        "LOG-SECRET-ONE",
        "LOG SECRET TWO",
        "LOG SECRET THREE",
        "LOG-SECRET-FOUR",
        "LOG-SECRET-FIVE",
        "LOG SECRET SIX",
        "LOG-SECRET-SEVEN",
        "LOG-SECRET-EIGHT",
        "LOG SECRET NINE",
        "LOG-SECRET-TEN",
        "LOG-SECRET-ELEVEN",
        "LOG-SECRET-TWELVE",
        "LOG-SECRET-THIRTEEN",
        "LOG-SECRET-FOURTEEN",
        "LOG-SECRET-FIFTEEN",
        "LOG-SECRET-SIXTEEN",
        "LOG-SECRET-SEVENTEEN",
        "LOG-SECRET-EIGHTEEN",
        "LOG-SECRET-NINETEEN",
        "LOG-SECRET-TWENTY",
        "LOG-SECRET-TWENTYONE",
    ):
        assert canary not in content
    assert content.count("[REDACTED]") >= len(secret_messages)
    for duration in range(1, 22):
        assert f"duration_ms={duration}" in content
    assert "TOKENIZER-VISIBLE" in content
    assert "EVENT-TOKENS-VISIBLE" in content
    assert "SECRETARY-VISIBLE" in content


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


def test_stale_logging_context_cannot_close_or_toggle_active_handlers(tmp_path: Path) -> None:
    stale = setup_logging(make_paths(tmp_path / "stale"))
    active_paths = make_paths(tmp_path / "active")
    active = setup_logging(
        active_paths,
        ssh_debug_enabled=True,
        performance_logging_enabled=True,
    )
    owned_handlers = {
        logger.name: tuple(logger.handlers)
        for logger in (active.logger, active.ssh_logger, active.performance_logger)
    }

    stale.set_ssh_debug_enabled(True)
    stale.set_performance_logging_enabled(True)
    stale.set_low_spec_mode(True)
    stale.close()

    for logger in (active.logger, active.ssh_logger, active.performance_logger):
        assert tuple(logger.handlers) == owned_handlers[logger.name]
    active.logger.info("active-app-line")
    active.ssh_logger.debug("active-ssh-line")
    active.performance_logger.info("active-performance-line")
    for logger in (active.logger, active.ssh_logger, active.performance_logger):
        for handler in logger.handlers:
            handler.flush()
    assert "active-app-line" in active_paths.app_log.read_text(encoding="utf-8")
    assert "active-ssh-line" in active_paths.ssh_debug_log.read_text(encoding="utf-8")
    assert "active-performance-line" in active_paths.performance_log.read_text(encoding="utf-8")
    active.close()


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


def test_logging_write_failure_never_echoes_raw_record_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = setup_logging(make_paths(tmp_path))
    handler = context.logger.handlers[0]
    stderr = io.StringIO()
    sentinel = "failure-path-secret"

    monkeypatch.setattr("sys.stderr", stderr)
    handler.handleError(
        logging.LogRecord(
            context.logger.name,
            logging.ERROR,
            __file__,
            1,
            "password=%s",
            (sentinel,),
            None,
        )
    )

    diagnostic = stderr.getvalue()
    assert "could not be written" in diagnostic
    assert sentinel not in diagnostic
    assert "password" not in diagnostic.casefold()


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


def test_storage_and_logging_support_korean_and_space_paths(tmp_path: Path) -> None:
    paths = make_paths(tmp_path / "한글 운영 폴더 with spaces")
    with SQLiteStorage(paths) as storage:
        storage.set_preference("표시 이름", "정상")
        assert storage.get_preference("표시 이름") == "정상"

    context = setup_logging(paths)
    context.logger.info("korean-path-ok")
    for handler in context.logger.handlers:
        handler.flush()
    assert "korean-path-ok" in paths.app_log.read_text(encoding="utf-8")

    # Replace and close the opened handler so the temporary directory can be
    # removed on Windows without relying on interpreter shutdown.
    opened_handlers = list(context.logger.handlers)
    replacement = setup_logging(make_paths(tmp_path / "logging cleanup"))
    assert all(getattr(handler, "stream", None) is None for handler in opened_handlers)
    assert replacement.logger.handlers
    replacement_handlers = list(replacement.logger.handlers)
    replacement.close()
    replacement.close()
    assert replacement.logger.handlers == []
    assert all(getattr(handler, "stream", None) is None for handler in replacement_handlers)
