"""Thread-safe local SQLite state store.

The database never stores user names, passwords, enable secrets, command raw
output, or credential blobs.  Public methods accept plain values so services
can evolve their dataclasses without coupling the persistence schema to UI
types.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .config import AppPaths, default_app_paths


SCHEMA_VERSION = 4
_T = TypeVar("_T")
_SECRET_KEYS = {"password", "passwd", "secret", "enable_secret", "credential_blob", "token"}


class StorageError(RuntimeError):
    pass


class StorageBusyError(StorageError):
    pass


class StorageCorruptError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectionBaseline:
    source_controller_ip: str
    member_ip: str
    connection_type: str
    normalized_connection_type: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class DetectorStreak:
    detector: str
    ip: str
    anomaly_count: int
    recovery_count: int
    active: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredIncident:
    incident_id: str
    ip: str
    incident_type: str
    reason_key: str
    first_detected_at: str
    last_seen_at: str
    resolved_at: str | None
    active: bool
    acknowledged: bool
    payload: dict[str, Any]


class SQLiteStorage:
    """Small synchronous repository safe to call from worker threads."""

    def __init__(
        self,
        path_or_paths: str | os.PathLike[str] | AppPaths | None = None,
        *,
        busy_timeout_ms: int = 1500,
        lock_retries: int = 3,
        initialize: bool = True,
    ) -> None:
        if isinstance(path_or_paths, AppPaths):
            self.path = path_or_paths.database
        elif path_or_paths is None:
            self.path = default_app_paths().database
        else:
            self.path = Path(path_or_paths)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.lock_retries = max(0, int(lock_retries))
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                connection = sqlite3.connect(
                    self.path,
                    timeout=self.busy_timeout_ms / 1000,
                    check_same_thread=False,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                self._connection = connection
                self._migrate()
                # Force SQLite to inspect pages now so a corrupt file is not
                # mistaken for an empty first-run database.
                result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or str(result[0]).casefold() != "ok":
                    raise sqlite3.DatabaseError(f"quick_check returned {result!r}")
            except sqlite3.DatabaseError as exc:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                raise StorageCorruptError(
                    f"로컬 상태 저장소를 열 수 없습니다. 원본을 보존한 채 확인하세요: {self.path}"
                ) from exc

    def _migrate(self) -> None:
        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StorageCorruptError(f"지원하지 않는 데이터베이스 버전입니다: {version}")
        if version < 1:
            script = """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connection_baselines (
                    source_controller_ip TEXT NOT NULL,
                    member_ip TEXT NOT NULL,
                    connection_type TEXT NOT NULL,
                    normalized_connection_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (source_controller_ip, member_ip)
                );
                CREATE TABLE IF NOT EXISTS detector_streaks (
                    detector TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    anomaly_count INTEGER NOT NULL CHECK (anomaly_count >= 0),
                    recovery_count INTEGER NOT NULL CHECK (recovery_count >= 0),
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (detector, ip)
                );
                CREATE TABLE IF NOT EXISTS device_states (
                    ip TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    is_normal INTEGER NOT NULL CHECK (is_normal IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS mm_discovered_devices (
                    ip TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing_streak INTEGER NOT NULL DEFAULT 0,
                    recovery_streak INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    ip TEXT NOT NULL,
                    incident_type TEXT NOT NULL,
                    reason_key TEXT NOT NULL,
                    first_detected_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1)),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_active_ip
                    ON incidents(active, ip, first_detected_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_active_reason
                    ON incidents(ip, incident_type, reason_key) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    incident_id TEXT,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS failover_collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_controller_ip TEXT NOT NULL,
                    actual_controller_ip TEXT NOT NULL,
                    primary_error_code TEXT NOT NULL,
                    collected_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
            """
            connection.executescript(script)
        if version < 2:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS connection_changes (
                    event_token TEXT PRIMARY KEY,
                    collector_ip TEXT NOT NULL,
                    member_ip TEXT NOT NULL,
                    previous_value TEXT NOT NULL,
                    current_value TEXT NOT NULL,
                    first_detected_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS idx_connection_changes_pending
                    ON connection_changes(acknowledged, member_ip, first_detected_at);
                PRAGMA user_version=2;
                COMMIT;
                """
            )
        if version < 3:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS device_normal_states (
                    ip TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO device_normal_states(ip, payload_json, observed_at)
                    SELECT ip, payload_json, observed_at
                    FROM device_states
                    WHERE is_normal=1;
                PRAGMA user_version=3;
                COMMIT;
                """
            )
        if version < 4:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE connection_baselines RENAME TO connection_baselines_v3;
                CREATE TABLE connection_baselines (
                    source_controller_ip TEXT NOT NULL,
                    member_ip TEXT NOT NULL PRIMARY KEY,
                    connection_type TEXT NOT NULL,
                    normalized_connection_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                INSERT INTO connection_baselines
                    (source_controller_ip, member_ip, connection_type,
                     normalized_connection_type, observed_at)
                    SELECT candidate.source_controller_ip,
                           candidate.member_ip,
                           candidate.connection_type,
                           candidate.normalized_connection_type,
                           candidate.observed_at
                    FROM connection_baselines_v3 AS candidate
                    WHERE candidate.rowid = (
                        SELECT latest.rowid
                        FROM connection_baselines_v3 AS latest
                        WHERE latest.member_ip = candidate.member_ip
                        ORDER BY latest.observed_at DESC, latest.rowid DESC
                        LIMIT 1
                    );
                DROP TABLE connection_baselines_v3;
                CREATE INDEX idx_connection_baselines_collector
                    ON connection_baselines(source_controller_ip);
                UPDATE connection_changes AS older
                    SET acknowledged=1
                    WHERE older.acknowledged=0
                      AND EXISTS (
                          SELECT 1
                          FROM connection_changes AS newer
                          WHERE newer.member_ip=older.member_ip
                            AND newer.acknowledged=0
                            AND (
                                newer.first_detected_at > older.first_detected_at
                                OR (
                                    newer.first_detected_at = older.first_detected_at
                                    AND newer.rowid > older.rowid
                                )
                            )
                      );
                CREATE UNIQUE INDEX idx_connection_changes_one_pending_member
                    ON connection_changes(member_ip) WHERE acknowledged=0;
                PRAGMA user_version=4;
                COMMIT;
                """
            )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def reset_monitoring_state_for_demo(self) -> None:
        """Clear only transient monitoring state for a deterministic demo loop."""

        tables = (
            "connection_baselines",
            "detector_streaks",
            "device_states",
            "device_normal_states",
            "mm_discovered_devices",
            "incidents",
            "events",
            "failover_collections",
            "connection_changes",
        )

        def operation(db: sqlite3.Connection) -> None:
            for table in tables:
                db.execute(f"DELETE FROM {table}")

        self._write(operation)

    def __enter__(self) -> "SQLiteStorage":
        self.initialize()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._read(lambda db: db.execute("PRAGMA user_version").fetchone()[0]))

    def set_preference(self, key: str, value: object, *, updated_at: datetime | str | None = None) -> None:
        payload = _json_dump(value)
        timestamp = _timestamp(updated_at)
        self._write(
            lambda db: db.execute(
                """INSERT INTO preferences(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (str(key), payload, timestamp),
            )
        )

    def set_preferences(
        self,
        values: Mapping[str, object],
        *,
        updated_at: datetime | str | None = None,
    ) -> None:
        """Persist a complete preference mirror in one bounded transaction."""

        encoded = {str(key): _json_dump(value) for key, value in values.items()}
        timestamp = _timestamp(updated_at)

        def operation(db: sqlite3.Connection) -> None:
            for key, payload in encoded.items():
                db.execute(
                    """INSERT INTO preferences(key, value_json, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           value_json=excluded.value_json, updated_at=excluded.updated_at""",
                    (key, payload, timestamp),
                )

        self._write(operation)

    def get_preference(self, key: str, default: _T | None = None) -> Any | _T | None:
        row = self._read(lambda db: db.execute("SELECT value_json FROM preferences WHERE key=?", (str(key),)).fetchone())
        return default if row is None else json.loads(row[0])

    # Compatibility aliases for UI/runtime consumers.
    set_setting = set_preference
    get_setting = get_preference

    def save_connection_baseline(
        self,
        source_controller_ip: str,
        member_ip: str,
        connection_type: str,
        *,
        normalized_connection_type: str | None = None,
        observed_at: datetime | str | None = None,
    ) -> None:
        normalized = normalized_connection_type if normalized_connection_type is not None else _normalize_type(connection_type)
        self._write(
            lambda db: db.execute(
                """INSERT INTO connection_baselines
                       (source_controller_ip, member_ip, connection_type, normalized_connection_type, observed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(member_ip) DO UPDATE SET
                       source_controller_ip=excluded.source_controller_ip,
                       connection_type=excluded.connection_type,
                       normalized_connection_type=excluded.normalized_connection_type,
                       observed_at=excluded.observed_at""",
                (source_controller_ip, member_ip, connection_type, normalized, _timestamp(observed_at)),
            )
        )

    def get_connection_baseline(self, member_ip: str) -> ConnectionBaseline | None:
        row = self._read(
            lambda db: db.execute(
                "SELECT * FROM connection_baselines WHERE member_ip=?",
                (member_ip,),
            ).fetchone()
        )
        return None if row is None else ConnectionBaseline(**dict(row))

    def load_connection_baselines(self, source_controller_ip: str | None = None) -> list[ConnectionBaseline]:
        if source_controller_ip is None:
            rows = self._read(lambda db: db.execute("SELECT * FROM connection_baselines").fetchall())
        else:
            rows = self._read(
                lambda db: db.execute(
                    "SELECT * FROM connection_baselines WHERE source_controller_ip=?", (source_controller_ip,)
                ).fetchall()
            )
        return [ConnectionBaseline(**dict(row)) for row in rows]

    def get(self, member_ip: str):
        """Implement the correlation engine's baseline-store protocol."""

        stored = self.get_connection_baseline(member_ip)
        if stored is None:
            return None
        from .models import ConnectionBaseline as DomainConnectionBaseline

        return DomainConnectionBaseline(
            collector_ip=stored.source_controller_ip,
            member_ip=stored.member_ip,
            display_value=stored.connection_type,
            normalized_value=stored.normalized_connection_type,
            observed_at=_parse_timestamp(stored.observed_at),
        )

    def set(self, baseline: object) -> None:
        """Persist a domain ``ConnectionBaseline`` without importing it eagerly."""

        self.save_connection_baseline(
            str(getattr(baseline, "collector_ip")),
            str(getattr(baseline, "member_ip")),
            str(getattr(baseline, "display_value")),
            normalized_connection_type=str(getattr(baseline, "normalized_value")),
            observed_at=getattr(baseline, "observed_at"),
        )

    def discard(self, member_ip: str) -> None:
        """Remove monitored-member state while retaining discovery/history rows."""

        member = str(member_ip)

        def operation(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM connection_baselines WHERE member_ip=?", (member,))
            db.execute("DELETE FROM detector_streaks WHERE ip=?", (member,))
            db.execute(
                "UPDATE connection_changes SET acknowledged=1 "
                "WHERE member_ip=? AND acknowledged=0",
                (member,),
            )

        self._write(operation)

    def prune(self, expected_ips: Iterable[str]) -> set[str]:
        """Prune durable detector/baseline state outside the configured scope."""

        allowed = {str(ip) for ip in expected_ips}
        rows = self._read(
            lambda db: db.execute(
                "SELECT member_ip AS ip FROM connection_baselines "
                "UNION SELECT ip FROM detector_streaks "
                "UNION SELECT member_ip AS ip FROM connection_changes WHERE acknowledged=0"
            ).fetchall()
        )
        removed = {str(row["ip"]) for row in rows} - allowed
        if removed:
            self._write(lambda db: self._prune_member_rows(db, removed))
        return removed

    @staticmethod
    def _prune_member_rows(db: sqlite3.Connection, member_ips: set[str]) -> None:
        for member_ip in member_ips:
            db.execute("DELETE FROM connection_baselines WHERE member_ip=?", (member_ip,))
            db.execute("DELETE FROM detector_streaks WHERE ip=?", (member_ip,))
            db.execute(
                "UPDATE connection_changes SET acknowledged=1 "
                "WHERE member_ip=? AND acknowledged=0",
                (member_ip,),
            )

    def save_connection_change(self, change: object, *, acknowledged: bool = False) -> None:
        event_token = str(getattr(change, "event_token"))
        member_ip = str(getattr(change, "member_ip"))

        def operation(db: sqlite3.Connection) -> None:
            if not acknowledged:
                db.execute(
                    "UPDATE connection_changes SET acknowledged=1 "
                    "WHERE member_ip=? AND acknowledged=0 AND event_token<>?",
                    (member_ip, event_token),
                )
            db.execute(
                """INSERT INTO connection_changes
                       (event_token, collector_ip, member_ip, previous_value, current_value,
                        first_detected_at, last_confirmed_at, acknowledged)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_token) DO UPDATE SET
                       last_confirmed_at=excluded.last_confirmed_at,
                       acknowledged=MAX(connection_changes.acknowledged, excluded.acknowledged)""",
                (
                    event_token,
                    str(getattr(change, "collector_ip")),
                    member_ip,
                    str(getattr(change, "previous_value")),
                    str(getattr(change, "current_value")),
                    _timestamp(getattr(change, "first_detected_at")),
                    _timestamp(getattr(change, "last_confirmed_at")),
                    int(acknowledged),
                ),
            )

        self._write(operation)

    def save_membership_state(
        self,
        baselines: list[object],
        changes: list[object],
    ) -> None:
        """Atomically persist Connection-Type baselines and their change events.

        A changed durable baseline without its pending event would make the
        transition impossible to reconstruct after a crash.  Keep both sides
        in one transaction, including ordinary first-seen baseline updates.
        """

        def operation(db: sqlite3.Connection) -> None:
            for baseline in baselines:
                db.execute(
                    """INSERT INTO connection_baselines
                           (source_controller_ip, member_ip, connection_type,
                            normalized_connection_type, observed_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(member_ip) DO UPDATE SET
                           source_controller_ip=excluded.source_controller_ip,
                           connection_type=excluded.connection_type,
                           normalized_connection_type=excluded.normalized_connection_type,
                           observed_at=excluded.observed_at""",
                    (
                        str(getattr(baseline, "collector_ip")),
                        str(getattr(baseline, "member_ip")),
                        str(getattr(baseline, "display_value")),
                        str(getattr(baseline, "normalized_value")),
                        _timestamp(getattr(baseline, "observed_at")),
                    ),
                )
            for change in changes:
                event_token = str(getattr(change, "event_token"))
                member_ip = str(getattr(change, "member_ip"))
                db.execute(
                    "UPDATE connection_changes SET acknowledged=1 "
                    "WHERE member_ip=? AND acknowledged=0 AND event_token<>?",
                    (member_ip, event_token),
                )
                db.execute(
                    """INSERT INTO connection_changes
                           (event_token, collector_ip, member_ip, previous_value,
                            current_value, first_detected_at, last_confirmed_at, acknowledged)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(event_token) DO UPDATE SET
                           last_confirmed_at=excluded.last_confirmed_at,
                           acknowledged=MAX(connection_changes.acknowledged, excluded.acknowledged)""",
                    (
                        event_token,
                        str(getattr(change, "collector_ip")),
                        member_ip,
                        str(getattr(change, "previous_value")),
                        str(getattr(change, "current_value")),
                        _timestamp(getattr(change, "first_detected_at")),
                        _timestamp(getattr(change, "last_confirmed_at")),
                    ),
                )

        self._write(operation)

    def save_cycle_domain_state(
        self,
        baselines: list[object],
        changes: list[object],
        acknowledged_members: set[str],
        incidents: list[object],
        transitions: list[object],
        removed_members: set[str] | None = None,
    ) -> None:
        """Atomically commit membership state, acknowledgements, and incidents."""

        def operation(db: sqlite3.Connection) -> None:
            self._prune_member_rows(db, set(removed_members or ()))
            for baseline in baselines:
                db.execute(
                    """INSERT INTO connection_baselines
                           (source_controller_ip, member_ip, connection_type,
                            normalized_connection_type, observed_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(member_ip) DO UPDATE SET
                           source_controller_ip=excluded.source_controller_ip,
                           connection_type=excluded.connection_type,
                           normalized_connection_type=excluded.normalized_connection_type,
                           observed_at=excluded.observed_at""",
                    (
                        str(getattr(baseline, "collector_ip")),
                        str(getattr(baseline, "member_ip")),
                        str(getattr(baseline, "display_value")),
                        str(getattr(baseline, "normalized_value")),
                        _timestamp(getattr(baseline, "observed_at")),
                    ),
                )
            for member_ip in acknowledged_members:
                db.execute(
                    "UPDATE connection_changes SET acknowledged=1 "
                    "WHERE member_ip=? AND acknowledged=0",
                    (member_ip,),
                )
            for change in changes:
                event_token = str(getattr(change, "event_token"))
                member_ip = str(getattr(change, "member_ip"))
                db.execute(
                    "UPDATE connection_changes SET acknowledged=1 "
                    "WHERE member_ip=? AND acknowledged=0 AND event_token<>?",
                    (member_ip, event_token),
                )
                db.execute(
                    """INSERT INTO connection_changes
                           (event_token, collector_ip, member_ip, previous_value,
                            current_value, first_detected_at, last_confirmed_at, acknowledged)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(event_token) DO UPDATE SET
                           last_confirmed_at=excluded.last_confirmed_at,
                           acknowledged=MAX(connection_changes.acknowledged, excluded.acknowledged)""",
                    (
                        event_token,
                        str(getattr(change, "collector_ip")),
                        member_ip,
                        str(getattr(change, "previous_value")),
                        str(getattr(change, "current_value")),
                        _timestamp(getattr(change, "first_detected_at")),
                        _timestamp(getattr(change, "last_confirmed_at")),
                    ),
                )
            for incident in incidents:
                incident_type = getattr(
                    getattr(incident, "incident_type"),
                    "value",
                    getattr(incident, "incident_type"),
                )
                severity = getattr(
                    getattr(incident, "severity"),
                    "value",
                    getattr(incident, "severity"),
                )
                event_token = str(getattr(incident, "event_token", ""))
                reason = str(getattr(incident, "reason"))
                payload = {
                    "severity": str(severity),
                    "reason": reason,
                    "alias": getattr(incident, "alias", None),
                    "acknowledged_at": _optional_timestamp(
                        getattr(incident, "acknowledged_at", None)
                    ),
                    "recovered_at": _optional_timestamp(
                        getattr(incident, "recovered_at", None)
                    ),
                    "last_notified_at": _optional_timestamp(
                        getattr(incident, "last_notified_at", None)
                    ),
                    "event_token": event_token,
                    "details": getattr(incident, "details", {}),
                }
                db.execute(
                    """INSERT INTO incidents
                           (incident_id, ip, incident_type, reason_key, first_detected_at,
                            last_seen_at, resolved_at, active, acknowledged, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(incident_id) DO UPDATE SET
                           last_seen_at=excluded.last_seen_at,
                           resolved_at=excluded.resolved_at,
                           active=excluded.active,
                           acknowledged=MAX(incidents.acknowledged, excluded.acknowledged),
                           payload_json=excluded.payload_json""",
                    (
                        str(getattr(incident, "incident_id")),
                        str(getattr(incident, "ip", "") or ""),
                        str(incident_type),
                        event_token or reason,
                        _timestamp(getattr(incident, "first_detected_at")),
                        _timestamp(getattr(incident, "last_seen_at")),
                        _optional_timestamp(getattr(incident, "recovered_at", None)),
                        int(bool(getattr(incident, "active"))),
                        int(getattr(incident, "acknowledged_at", None) is not None),
                        _json_dump(payload),
                    ),
                )
            for transition in transitions:
                incident = getattr(transition, "incident")
                kind = getattr(getattr(transition, "kind"), "value", getattr(transition, "kind"))
                severity = getattr(
                    getattr(incident, "severity"),
                    "value",
                    getattr(incident, "severity"),
                )
                incident_type = getattr(
                    getattr(incident, "incident_type"),
                    "value",
                    getattr(incident, "incident_type"),
                )
                db.execute(
                    "INSERT INTO events(event_type, ip, incident_id, occurred_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(kind),
                        str(getattr(incident, "ip", "") or ""),
                        str(getattr(incident, "incident_id")),
                        _timestamp(getattr(incident, "last_seen_at")),
                        _json_dump(
                            {
                                "incident_type": str(incident_type),
                                "severity": str(severity),
                                "reason": str(getattr(incident, "reason")),
                            }
                        ),
                    ),
                )

        self._write(operation)

    def acknowledge_connection_change(self, *, event_token: str | None = None, member_ip: str | None = None) -> int:
        if bool(event_token) == bool(member_ip):
            raise ValueError("event_token 또는 member_ip 중 하나만 지정해야 합니다.")
        if event_token is not None:
            cursor = self._write(
                lambda db: db.execute(
                    "UPDATE connection_changes SET acknowledged=1 WHERE event_token=? AND acknowledged=0",
                    (event_token,),
                )
            )
        else:
            cursor = self._write(
                lambda db: db.execute(
                    "UPDATE connection_changes SET acknowledged=1 WHERE member_ip=? AND acknowledged=0",
                    (member_ip,),
                )
            )
        return int(cursor.rowcount)

    def load_pending_connection_changes(self):
        from .models import ConnectionChange

        rows = self._read(
            lambda db: db.execute(
                "SELECT * FROM connection_changes WHERE acknowledged=0 "
                "ORDER BY member_ip, first_detected_at DESC, last_confirmed_at DESC, rowid DESC"
            ).fetchall()
        )
        newest_by_member: dict[str, sqlite3.Row] = {}
        for row in rows:
            newest_by_member.setdefault(str(row["member_ip"]), row)
        newest_rows = sorted(
            newest_by_member.values(),
            key=lambda row: (str(row["first_detected_at"]), str(row["member_ip"])),
        )
        return [
            ConnectionChange(
                collector_ip=row["collector_ip"],
                member_ip=row["member_ip"],
                previous_value=row["previous_value"],
                current_value=row["current_value"],
                first_detected_at=_parse_timestamp(row["first_detected_at"]),
                last_confirmed_at=_parse_timestamp(row["last_confirmed_at"]),
                durable_event_token=row["event_token"],
            )
            for row in newest_rows
        ]

    def save_streak(
        self,
        detector: str,
        ip: str,
        anomaly_count: int,
        recovery_count: int,
        active: bool,
        *,
        updated_at: datetime | str | None = None,
    ) -> None:
        if anomaly_count < 0 or recovery_count < 0:
            raise ValueError("streak counts cannot be negative")
        self._write(
            lambda db: db.execute(
                """INSERT INTO detector_streaks
                       (detector, ip, anomaly_count, recovery_count, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(detector, ip) DO UPDATE SET
                       anomaly_count=excluded.anomaly_count,
                       recovery_count=excluded.recovery_count,
                       active=excluded.active,
                       updated_at=excluded.updated_at""",
                (detector, ip, anomaly_count, recovery_count, int(active), _timestamp(updated_at)),
            )
        )

    def get_streak(self, detector: str, ip: str) -> DetectorStreak | None:
        row = self._read(
            lambda db: db.execute(
                "SELECT * FROM detector_streaks WHERE detector=? AND ip=?", (detector, ip)
            ).fetchone()
        )
        if row is None:
            return None
        data = dict(row)
        data["active"] = bool(data["active"])
        return DetectorStreak(**data)

    def load_streaks(self, detector: str | None = None) -> list[DetectorStreak]:
        if detector is None:
            rows = self._read(lambda db: db.execute("SELECT * FROM detector_streaks").fetchall())
        else:
            rows = self._read(
                lambda db: db.execute("SELECT * FROM detector_streaks WHERE detector=?", (detector,)).fetchall()
            )
        result: list[DetectorStreak] = []
        for row in rows:
            data = dict(row)
            data["active"] = bool(data["active"])
            result.append(DetectorStreak(**data))
        return result

    def save_device_state(
        self,
        ip: str,
        payload: object,
        *,
        observed_at: datetime | str | None = None,
        is_normal: bool,
    ) -> None:
        encoded = _json_dump(payload)
        timestamp = _timestamp(observed_at)

        def operation(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO device_states(ip, payload_json, observed_at, is_normal) VALUES (?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET payload_json=excluded.payload_json,
                       observed_at=excluded.observed_at, is_normal=excluded.is_normal""",
                (ip, encoded, timestamp, int(is_normal)),
            )
            if is_normal:
                db.execute(
                    """INSERT INTO device_normal_states(ip, payload_json, observed_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(ip) DO UPDATE SET
                           payload_json=excluded.payload_json,
                           observed_at=excluded.observed_at""",
                    (ip, encoded, timestamp),
                )

        self._write(operation)

    def load_device_states(self, *, normal_only: bool = False) -> dict[str, dict[str, Any]]:
        query = (
            "SELECT ip, payload_json, observed_at, 1 AS is_normal FROM device_normal_states"
            if normal_only
            else "SELECT * FROM device_states"
        )
        rows = self._read(lambda db: db.execute(query).fetchall())
        return {
            row["ip"]: {
                "payload": json.loads(row["payload_json"]),
                "observed_at": row["observed_at"],
                "is_normal": bool(row["is_normal"]),
            }
            for row in rows
        }

    def save_mm_discovered_device(
        self,
        ip: str,
        *,
        alias: str = "",
        hostname: str = "",
        last_seen_at: datetime | str | None = None,
        missing_streak: int = 0,
        recovery_streak: int = 0,
    ) -> None:
        self._write(
            lambda db: db.execute(
                """INSERT INTO mm_discovered_devices
                       (ip, alias, hostname, last_seen_at, missing_streak, recovery_streak)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET alias=excluded.alias, hostname=excluded.hostname,
                       last_seen_at=excluded.last_seen_at, missing_streak=excluded.missing_streak,
                       recovery_streak=excluded.recovery_streak""",
                (ip, alias, hostname, _timestamp(last_seen_at), missing_streak, recovery_streak),
            )
        )

    def load_mm_discovered_devices(self) -> list[dict[str, Any]]:
        rows = self._read(lambda db: db.execute("SELECT * FROM mm_discovered_devices ORDER BY ip").fetchall())
        return [dict(row) for row in rows]

    def upsert_incident(
        self,
        incident_id: str,
        ip: str,
        incident_type: str,
        reason_key: str,
        *,
        first_detected_at: datetime | str,
        last_seen_at: datetime | str,
        active: bool = True,
        acknowledged: bool = False,
        resolved_at: datetime | str | None = None,
        payload: object | None = None,
    ) -> None:
        encoded = _json_dump(payload or {})
        self._write(
            lambda db: db.execute(
                """INSERT INTO incidents
                       (incident_id, ip, incident_type, reason_key, first_detected_at, last_seen_at,
                        resolved_at, active, acknowledged, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                       last_seen_at=excluded.last_seen_at, resolved_at=excluded.resolved_at,
                       active=excluded.active,
                       acknowledged=MAX(incidents.acknowledged, excluded.acknowledged),
                       payload_json=excluded.payload_json""",
                (
                    incident_id,
                    ip,
                    incident_type,
                    reason_key,
                    _timestamp(first_detected_at),
                    _timestamp(last_seen_at),
                    _timestamp(resolved_at) if resolved_at is not None else None,
                    int(active),
                    int(acknowledged),
                    encoded,
                ),
            )
        )

    def acknowledge_incident(self, incident_id: str, *, acknowledged: bool = True) -> bool:
        cursor = self._write(
            lambda db: db.execute(
                "UPDATE incidents SET acknowledged=? WHERE incident_id=?", (int(acknowledged), incident_id)
            )
        )
        return bool(cursor.rowcount)

    def resolve_incident(self, incident_id: str, *, resolved_at: datetime | str | None = None) -> bool:
        timestamp = _timestamp(resolved_at)
        cursor = self._write(
            lambda db: db.execute(
                "UPDATE incidents SET active=0, resolved_at=?, last_seen_at=? WHERE incident_id=? AND active=1",
                (timestamp, timestamp, incident_id),
            )
        )
        return bool(cursor.rowcount)

    def list_incidents(self, *, active_only: bool = False, limit: int = 500) -> list[StoredIncident]:
        bounded_limit = max(1, min(int(limit), 10_000))
        condition = "WHERE active=1" if active_only else ""
        rows = self._read(
            lambda db: db.execute(
                f"SELECT * FROM incidents {condition} ORDER BY first_detected_at DESC LIMIT ?", (bounded_limit,)
            ).fetchall()
        )
        result: list[StoredIncident] = []
        for row in rows:
            data = dict(row)
            data["active"] = bool(data["active"])
            data["acknowledged"] = bool(data["acknowledged"])
            data["payload"] = json.loads(data.pop("payload_json"))
            result.append(StoredIncident(**data))
        return result

    def save_domain_incident(self, incident: object) -> None:
        """Persist the full domain incident without coupling the SQL schema to enums."""

        incident_type = getattr(getattr(incident, "incident_type"), "value", getattr(incident, "incident_type"))
        severity = getattr(getattr(incident, "severity"), "value", getattr(incident, "severity"))
        event_token = str(getattr(incident, "event_token", ""))
        reason = str(getattr(incident, "reason"))
        payload = {
            "severity": str(severity),
            "reason": reason,
            "alias": getattr(incident, "alias", None),
            "acknowledged_at": _optional_timestamp(getattr(incident, "acknowledged_at", None)),
            "recovered_at": _optional_timestamp(getattr(incident, "recovered_at", None)),
            "last_notified_at": _optional_timestamp(getattr(incident, "last_notified_at", None)),
            "event_token": event_token,
            "details": getattr(incident, "details", {}),
        }
        self.upsert_incident(
            incident_id=str(getattr(incident, "incident_id")),
            ip=str(getattr(incident, "ip", "") or ""),
            incident_type=str(incident_type),
            reason_key=event_token or reason,
            first_detected_at=getattr(incident, "first_detected_at"),
            last_seen_at=getattr(incident, "last_seen_at"),
            active=bool(getattr(incident, "active")),
            acknowledged=getattr(incident, "acknowledged_at", None) is not None,
            resolved_at=getattr(incident, "recovered_at", None),
            payload=payload,
        )

    def load_domain_incidents(self, *, active_only: bool = False, limit: int = 10_000):
        from .models import Incident, IncidentType, Severity

        result = []
        for stored in self.list_incidents(active_only=active_only, limit=limit):
            payload = stored.payload
            result.append(
                Incident(
                    incident_id=stored.incident_id,
                    incident_type=IncidentType(stored.incident_type),
                    severity=Severity(str(payload.get("severity", "unknown"))),
                    reason=str(payload.get("reason", stored.reason_key)),
                    first_detected_at=_parse_timestamp(stored.first_detected_at),
                    last_seen_at=_parse_timestamp(stored.last_seen_at),
                    ip=stored.ip or None,
                    alias=payload.get("alias"),
                    active=stored.active,
                    acknowledged_at=_optional_parse_timestamp(payload.get("acknowledged_at")),
                    recovered_at=_optional_parse_timestamp(payload.get("recovered_at") or stored.resolved_at),
                    last_notified_at=_optional_parse_timestamp(payload.get("last_notified_at")),
                    event_token=str(payload.get("event_token", "")),
                    details=dict(payload.get("details", {})),
                )
            )
        return result

    def append_event(
        self,
        event_type: str,
        *,
        ip: str = "",
        incident_id: str | None = None,
        occurred_at: datetime | str | None = None,
        payload: object | None = None,
    ) -> int:
        cursor = self._write(
            lambda db: db.execute(
                "INSERT INTO events(event_type, ip, incident_id, occurred_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (event_type, ip, incident_id, _timestamp(occurred_at), _json_dump(payload or {})),
            )
        )
        return int(cursor.lastrowid)

    def list_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 10_000))
        rows = self._read(
            lambda db: db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (bounded_limit,)).fetchall()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def record_failover(
        self,
        primary_controller_ip: str,
        actual_controller_ip: str,
        primary_error_code: str,
        *,
        collected_at: datetime | str | None = None,
    ) -> int:
        cursor = self._write(
            lambda db: db.execute(
                """INSERT INTO failover_collections
                       (primary_controller_ip, actual_controller_ip, primary_error_code, collected_at)
                   VALUES (?, ?, ?, ?)""",
                (primary_controller_ip, actual_controller_ip, primary_error_code, _timestamp(collected_at)),
            )
        )
        return int(cursor.lastrowid)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("저장소가 닫혀 있습니다.")
        return self._connection

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            connection = self._require_connection()
            try:
                return operation(connection)
            except sqlite3.DatabaseError as exc:
                if _is_locked(exc):
                    raise StorageBusyError("로컬 상태 저장소가 사용 중입니다. 잠시 후 다시 시도하세요.") from exc
                raise StorageError("로컬 상태를 읽지 못했습니다.") from exc

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            connection = self._require_connection()
            for attempt in range(self.lock_retries + 1):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    result = operation(connection)
                    connection.commit()
                    return result
                except sqlite3.DatabaseError as exc:
                    connection.rollback()
                    if _is_locked(exc) and attempt < self.lock_retries:
                        time.sleep(min(0.05 * (2**attempt), 0.25))
                        continue
                    if _is_locked(exc):
                        raise StorageBusyError(
                            "로컬 상태 저장소가 사용 중입니다. 잠시 후 다시 시도하세요."
                        ) from exc
                    raise StorageError("로컬 상태를 저장하지 못했습니다.") from exc
            raise AssertionError("unreachable")


# Concise compatibility names used by service and UI code.
Storage = SQLiteStorage
StateStore = SQLiteStorage


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    candidate = str(value).strip()
    if not candidate:
        raise ValueError("timestamp cannot be empty")
    return candidate


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: datetime | str | None) -> str | None:
    return None if value is None else _timestamp(value)


def _optional_parse_timestamp(value: object) -> datetime | None:
    return None if value in (None, "") else _parse_timestamp(str(value))


def _normalize_type(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("-", " ").split())


def _json_dump(value: object) -> str:
    safe = _json_safe(value)
    _reject_secret_payload(safe)
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_safe(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(nested) for nested in value]
    if isinstance(value, datetime):
        return _timestamp(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        return _json_safe(getattr(value, "value"))
    return str(value)


def _reject_secret_payload(value: object, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_password"):
                raise ValueError(f"secret-bearing field cannot be persisted: {path}.{key}")
            _reject_secret_payload(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_secret_payload(nested, f"{path}[{index}]")


def _is_locked(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "locked" in text or "busy" in text
