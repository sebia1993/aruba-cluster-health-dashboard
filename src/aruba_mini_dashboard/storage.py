"""Thread-safe local SQLite state store.

The database never stores user names, passwords, enable secrets, command raw
output, or credential blobs.  Public methods accept plain values so services
can evolve their dataclasses without coupling the persistence schema to UI
types.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from .config import AppPaths, _field_name_tokens, _is_secret_field_name, default_app_paths


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 4
HISTORY_RETENTION_DAYS = 180
HISTORY_RETENTION_MAX_ROWS = 10_000
HISTORY_MAINTENANCE_INTERVAL = timedelta(days=1)
DEVICE_INVENTORY_RETENTION_MARKER = "_device_inventory_retention_last_run"
MAX_STORAGE_JSON_BYTES = 256 * 1024
MAX_STORAGE_JSON_DEPTH = 32
MAX_STORAGE_JSON_NODES = 20_000
_T = TypeVar("_T")
_NON_SECRET_TOKEN_KEYS = {("durable", "event", "token"), ("event", "token")}
_REQUIRED_SCHEMA_COLUMNS = {
    "preferences": {"key", "value_json", "updated_at"},
    "connection_baselines": {
        "source_controller_ip",
        "member_ip",
        "connection_type",
        "normalized_connection_type",
        "observed_at",
    },
    "detector_streaks": {
        "detector",
        "ip",
        "anomaly_count",
        "recovery_count",
        "active",
        "updated_at",
    },
    "device_states": {"ip", "payload_json", "observed_at", "is_normal"},
    "device_normal_states": {"ip", "payload_json", "observed_at"},
    "mm_discovered_devices": {
        "ip",
        "alias",
        "hostname",
        "last_seen_at",
        "missing_streak",
        "recovery_streak",
    },
    "incidents": {
        "incident_id",
        "ip",
        "incident_type",
        "reason_key",
        "first_detected_at",
        "last_seen_at",
        "resolved_at",
        "active",
        "acknowledged",
        "payload_json",
    },
    "events": {"id", "event_type", "ip", "incident_id", "occurred_at", "payload_json"},
    "failover_collections": {
        "id",
        "primary_controller_ip",
        "actual_controller_ip",
        "primary_error_code",
        "collected_at",
    },
    "connection_changes": {
        "event_token",
        "collector_ip",
        "member_ip",
        "previous_value",
        "current_value",
        "first_detected_at",
        "last_confirmed_at",
        "acknowledged",
    },
}
_REQUIRED_SCHEMA_PRIMARY_KEYS = {
    "preferences": ("key",),
    "connection_baselines": ("member_ip",),
    "detector_streaks": ("detector", "ip"),
    "device_states": ("ip",),
    "device_normal_states": ("ip",),
    "mm_discovered_devices": ("ip",),
    "incidents": ("incident_id",),
    "events": ("id",),
    "failover_collections": ("id",),
    "connection_changes": ("event_token",),
}
_REQUIRED_SCHEMA_NOT_NULL = {
    "preferences": {"value_json", "updated_at"},
    "connection_baselines": {
        "source_controller_ip",
        "member_ip",
        "connection_type",
        "normalized_connection_type",
        "observed_at",
    },
    "detector_streaks": {
        "detector",
        "ip",
        "anomaly_count",
        "recovery_count",
        "active",
        "updated_at",
    },
    "device_states": {"payload_json", "observed_at", "is_normal"},
    "device_normal_states": {"payload_json", "observed_at"},
    "mm_discovered_devices": {
        "alias",
        "hostname",
        "last_seen_at",
        "missing_streak",
        "recovery_streak",
    },
    "incidents": {
        "ip",
        "incident_type",
        "reason_key",
        "first_detected_at",
        "last_seen_at",
        "active",
        "acknowledged",
        "payload_json",
    },
    "events": {"event_type", "ip", "occurred_at", "payload_json"},
    "failover_collections": {
        "primary_controller_ip",
        "actual_controller_ip",
        "primary_error_code",
        "collected_at",
    },
    "connection_changes": {
        "collector_ip",
        "member_ip",
        "previous_value",
        "current_value",
        "first_detected_at",
        "last_confirmed_at",
        "acknowledged",
    },
}
_REQUIRED_SCHEMA_INDEXES = {
    "connection_baselines": {
        "idx_connection_baselines_collector": (
            False,
            (("source_controller_ip", False),),
            None,
        )
    },
    "connection_changes": {
        "idx_connection_changes_pending": (
            False,
            (
                ("acknowledged", False),
                ("member_ip", False),
                ("first_detected_at", False),
            ),
            None,
        ),
        "idx_connection_changes_one_pending_member": (
            True,
            (("member_ip", False),),
            "acknowledged=0",
        ),
    },
    "events": {
        "idx_events_occurred_at": (
            False,
            (("occurred_at", True),),
            None,
        )
    },
    "incidents": {
        "idx_incidents_active_ip": (
            False,
            (("active", False), ("ip", False), ("first_detected_at", False)),
            None,
        ),
        "idx_incidents_active_reason": (
            True,
            (("ip", False), ("incident_type", False), ("reason_key", False)),
            "active=1",
        ),
    },
}
_REQUIRED_SCHEMA_CHECKS = {
    "detector_streaks": (
        "check(anomaly_count>=0)",
        "check(recovery_count>=0)",
        "check(activein(0,1))",
    ),
    "device_states": ("check(is_normalin(0,1))",),
    "incidents": (
        "check(activein(0,1))",
        "check(acknowledgedin(0,1))",
    ),
    "connection_changes": ("check(acknowledgedin(0,1))",),
}
_LEGACY_BASE_TABLES = (
    "preferences",
    "connection_baselines",
    "detector_streaks",
    "device_states",
    "mm_discovered_devices",
    "incidents",
    "events",
    "failover_collections",
)
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_REQUIRED_INTEGER_RANGES = {
    "detector_streaks": {
        "anomaly_count": (0, _SQLITE_MAX_INTEGER),
        "recovery_count": (0, _SQLITE_MAX_INTEGER),
        "active": (0, 1),
    },
    "device_states": {"is_normal": (0, 1)},
    # These counters predate the v4 CHECK contract. Keep existing databases
    # compatible while enforcing their declared type and values at every
    # startup and read boundary.
    "mm_discovered_devices": {
        "missing_streak": (0, _SQLITE_MAX_INTEGER),
        "recovery_streak": (0, _SQLITE_MAX_INTEGER),
    },
    "incidents": {"active": (0, 1), "acknowledged": (0, 1)},
    "connection_changes": {"acknowledged": (0, 1)},
}


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
        busy_timeout_ms: int = 50,
        lock_retries: int = 1,
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
            connection: sqlite3.Connection | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    self.path,
                    timeout=self.busy_timeout_ms / 1000,
                    check_same_thread=False,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys=ON")
                self._connection = connection
                # Check/migrate the schema before changing persistent database
                # pragmas. A database from a newer application version is a
                # read-preserving startup error and must not be switched from
                # DELETE to WAL merely by probing it with an older build.
                self._migrate()
                self._validate_schema()
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                # Force SQLite to inspect pages now so a corrupt file is not
                # mistaken for an empty first-run database.
                result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or str(result[0]).casefold() != "ok":
                    raise sqlite3.DatabaseError(f"quick_check returned {result!r}")
            except StorageError:
                # _migrate() deliberately raises StorageError for a database
                # created by a newer application version. Close that newly
                # opened handle before propagating so startup can safely stop
                # without retaining a WAL/file lock.
                self._discard_initializing_connection(connection)
                raise
            except OSError as exc:
                self._discard_initializing_connection(connection)
                raise StorageError(
                    "로컬 상태 저장소 폴더를 사용할 수 없습니다. "
                    "쓰기 권한과 디스크 상태를 확인하세요."
                ) from exc
            except sqlite3.DatabaseError as exc:
                self._discard_initializing_connection(connection)
                if _is_locked(exc):
                    raise StorageBusyError(
                        "로컬 상태 저장소가 사용 중입니다. 잠시 후 다시 시도하세요."
                    ) from exc
                raise StorageCorruptError(
                    "로컬 상태 저장소를 열 수 없습니다. 원본을 보존한 채 확인하세요."
                ) from exc

    def _discard_initializing_connection(
        self,
        connection: sqlite3.Connection | None,
    ) -> None:
        """Drop a partially initialized connection without masking its error."""

        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.DatabaseError:
            # The initialization failure is the actionable root cause. A close
            # error on the already-unusable handle must not replace it.
            LOGGER.debug("Failed to close unusable SQLite initialization handle", exc_info=True)

    def _migrate(self) -> None:
        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StorageCorruptError(
                "현재 프로그램보다 새로운 데이터베이스 버전입니다: "
                f"{version}. 더 최신 버전의 프로그램으로 실행하세요."
            )
        if version == 0:
            # A genuine first run has no user-defined SQLite objects. Never
            # run committed migration scripts over a partially initialized,
            # damaged, or unrelated unversioned database.
            existing_object = connection.execute(
                """SELECT 1 FROM sqlite_schema
                   WHERE name NOT GLOB 'sqlite_*'
                   LIMIT 1"""
            ).fetchone()
            if existing_object is not None:
                raise sqlite3.DatabaseError("unversioned local database is not empty")
        if 0 < version < SCHEMA_VERSION:
            # Migration scripts commit by design. Validate the legacy schema
            # first so malformed data is never partly transformed before the
            # final current-schema validation rejects it.
            self._validate_legacy_schema(version)
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

    def _validate_legacy_schema(self, version: int) -> None:
        """Fail before a committed migration can mutate malformed legacy data."""

        connection = self._require_connection()
        tables = list(_LEGACY_BASE_TABLES)
        if version >= 2:
            tables.append("connection_changes")
        if version >= 3:
            tables.append("device_normal_states")
        for table in tables:
            primary_key = (
                ("source_controller_ip", "member_ip")
                if table == "connection_baselines"
                else _REQUIRED_SCHEMA_PRIMARY_KEYS[table]
            )
            _validate_table_contract(connection, table, primary_key)

        legacy_indexes = {
            "events": _REQUIRED_SCHEMA_INDEXES["events"],
            "incidents": _REQUIRED_SCHEMA_INDEXES["incidents"],
        }
        if version >= 2:
            legacy_indexes["connection_changes"] = {
                "idx_connection_changes_pending": _REQUIRED_SCHEMA_INDEXES[
                    "connection_changes"
                ]["idx_connection_changes_pending"]
            }
        _validate_index_contracts(connection, legacy_indexes)

    def _validate_schema(self) -> None:
        """Reject an incomplete or weakened v4 schema before runtime reads."""

        connection = self._require_connection()
        for table in _REQUIRED_SCHEMA_COLUMNS:
            _validate_table_contract(
                connection,
                table,
                _REQUIRED_SCHEMA_PRIMARY_KEYS[table],
            )
        _validate_index_contracts(connection, _REQUIRED_SCHEMA_INDEXES)

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
        _reject_secret_preference_key(key)
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

        encoded: dict[str, str] = {}
        for key, value in values.items():
            _reject_secret_preference_key(key)
            encoded[str(key)] = _json_dump(value)
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
        return default if row is None else _json_load(row[0])

    def get_preferences(self, keys: Iterable[str] | None = None) -> dict[str, Any]:
        """Load a preference set with one SQLite read instead of N key reads."""

        normalized = None if keys is None else tuple(dict.fromkeys(str(key) for key in keys))
        if normalized == ():
            return {}

        def operation(db: sqlite3.Connection) -> list[sqlite3.Row]:
            if normalized is None:
                return db.execute("SELECT key, value_json FROM preferences").fetchall()
            placeholders = ",".join("?" for _ in normalized)
            return db.execute(
                f"SELECT key, value_json FROM preferences WHERE key IN ({placeholders})",
                normalized,
            ).fetchall()

        rows = self._read(operation)
        return {str(row["key"]): _json_load(row["value_json"]) for row in rows}

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

    def load_domain_connection_baselines(self):
        """Restore all correlation baselines from the same SQLite read."""

        from .models import ConnectionBaseline as DomainConnectionBaseline

        return [
            DomainConnectionBaseline(
                collector_ip=stored.source_controller_ip,
                member_ip=stored.member_ip,
                display_value=stored.connection_type,
                normalized_value=stored.normalized_connection_type,
                observed_at=_parse_stored_timestamp(stored.observed_at),
            )
            for stored in self.load_connection_baselines()
        ]

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
            observed_at=_parse_stored_timestamp(stored.observed_at),
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
            self._cleanup_history_if_due(db)

        self._write(operation)

    def try_set_preferences(
        self,
        values: Mapping[str, object],
        *,
        lock_timeout_ms: int = 50,
        updated_at: datetime | str | None = None,
    ) -> None:
        """Persist preferences without waiting indefinitely for an app worker.

        UI preference mirroring is best effort because the JSON settings file
        remains authoritative.  A timed acquisition prevents a long poll
        transaction on the shared connection from freezing Qt's event loop.
        """

        acquired = self._lock.acquire(timeout=max(0, int(lock_timeout_ms)) / 1000)
        if not acquired:
            raise StorageBusyError("로컬 상태 저장소가 사용 중입니다. 잠시 후 다시 시도하세요.")
        try:
            self.set_preferences(values, updated_at=updated_at)
        finally:
            self._lock.release()

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

        def operation(db: sqlite3.Connection) -> list[sqlite3.Row]:
            _raise_if_invalid_integer_rows(db, "connection_changes")
            return db.execute(
                "SELECT * FROM connection_changes WHERE acknowledged=0 "
                "ORDER BY member_ip, first_detected_at DESC, last_confirmed_at DESC, rowid DESC"
            ).fetchall()

        rows = self._read(operation)
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
                first_detected_at=_parse_stored_timestamp(row["first_detected_at"]),
                last_confirmed_at=_parse_stored_timestamp(row["last_confirmed_at"]),
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
        anomaly_count = _input_nonnegative_integer(anomaly_count)
        recovery_count = _input_nonnegative_integer(recovery_count)
        if type(active) is not bool:
            raise ValueError("active must be a boolean")
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
        _validate_stored_integer_record(data, "detector_streaks")
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
            _validate_stored_integer_record(data, "detector_streaks")
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
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not normal_only:
                _validate_stored_integer_record(row, "device_states")
            result[row["ip"]] = {
                "payload": _json_load(row["payload_json"], require_mapping=True),
                "observed_at": row["observed_at"],
                "is_normal": bool(row["is_normal"]),
            }
        return result

    def load_runtime_inventory(
        self,
        protected_ips: Iterable[str] | None,
    ) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Atomically bound and load startup device/MM inventory.

        ``None`` means settings are incomplete, so cleanup is skipped and the
        recoverable inventory is only read. With an authoritative configured
        scope, stale rows are selected and removed before either bulk result is
        materialized. JSON/integer validation remains inside the same write
        transaction, so any failure rolls the cleanup and retention marker back.
        """

        normalized = (
            None
            if protected_ips is None
            else tuple(
                dict.fromkeys(
                    str(ip).strip() for ip in protected_ips if str(ip).strip()
                )
            )
        )

        def operation(
            db: sqlite3.Connection,
        ) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
            removed = (
                set()
                if normalized is None
                else self._cleanup_device_inventory_if_due(
                    db,
                    protected_ips=normalized,
                )
            )

            device_rows = db.execute("SELECT * FROM device_states").fetchall()
            devices: dict[str, dict[str, Any]] = {}
            for row in device_rows:
                _validate_stored_integer_record(row, "device_states")
                devices[str(row["ip"])] = {
                    "payload": _json_load(row["payload_json"], require_mapping=True),
                    "observed_at": row["observed_at"],
                    "is_normal": bool(row["is_normal"]),
                }

            mm_rows = db.execute(
                "SELECT * FROM mm_discovered_devices ORDER BY ip"
            ).fetchall()
            discovered = [dict(row) for row in mm_rows]
            for row in discovered:
                _validate_stored_integer_record(row, "mm_discovered_devices")
            return removed, devices, discovered

        return self._write(operation)

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
        missing_streak = _input_nonnegative_integer(missing_streak)
        recovery_streak = _input_nonnegative_integer(recovery_streak)
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
        result = [dict(row) for row in rows]
        for row in result:
            _validate_stored_integer_record(row, "mm_discovered_devices")
        return result

    def save_poll_runtime_state(
        self,
        *,
        detector_state: Mapping[str, Mapping[str, object]],
        device_states: Iterable[tuple[object, bool]],
        observed_at: datetime | str,
        failover: tuple[str, str, str, datetime | str | None] | None = None,
        retention_protected_ips: Iterable[str] | None = None,
    ) -> set[str]:
        """Persist one completed poll's non-domain state in one transaction.

        Connection baselines, changes, incidents and their journal transitions
        intentionally remain in :meth:`save_cycle_domain_state`; that is the
        separate crash-consistency boundary which must never be split.
        """

        timestamp = _timestamp(observed_at)
        streak_rows: list[tuple[str, str, int, int, int, str]] = []
        for key, counter in detector_state.items():
            detector, separator, member_ip = str(key).partition("|")
            if not separator:
                continue
            anomaly_count = _input_nonnegative_integer(counter["anomaly_streak"])
            recovery_count = _input_nonnegative_integer(counter["recovery_streak"])
            streak_rows.append(
                (
                    detector,
                    member_ip,
                    anomaly_count,
                    recovery_count,
                    int(bool(counter["active"])),
                    timestamp,
                )
            )

        device_rows: list[tuple[str, str, str, int]] = []
        normal_rows: list[tuple[str, str, str]] = []
        discovered_rows: list[tuple[str, str, str, str, int, int]] = []
        for payload, is_normal in device_states:
            member_ip = str(getattr(payload, "ip"))
            encoded = _json_dump(payload)
            device_rows.append((member_ip, encoded, timestamp, int(bool(is_normal))))
            if is_normal:
                normal_rows.append((member_ip, encoded, timestamp))
            if bool(getattr(payload, "mm_present", False)):
                discovered_rows.append(
                    (
                        member_ip,
                        str(getattr(payload, "alias", "") or ""),
                        str(getattr(payload, "hostname", "") or ""),
                        _timestamp(getattr(payload, "last_seen", None) or observed_at),
                        0,
                        0,
                    )
                )

        protected_ips = (
            tuple(dict.fromkeys(str(ip).strip() for ip in retention_protected_ips if str(ip).strip()))
            if retention_protected_ips is not None
            else None
        )

        def operation(db: sqlite3.Connection) -> set[str]:
            db.executemany(
                """INSERT INTO detector_streaks
                       (detector, ip, anomaly_count, recovery_count, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(detector, ip) DO UPDATE SET
                       anomaly_count=excluded.anomaly_count,
                       recovery_count=excluded.recovery_count,
                       active=excluded.active,
                       updated_at=excluded.updated_at""",
                streak_rows,
            )
            db.executemany(
                """INSERT INTO device_states(ip, payload_json, observed_at, is_normal)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET payload_json=excluded.payload_json,
                       observed_at=excluded.observed_at, is_normal=excluded.is_normal""",
                device_rows,
            )
            db.executemany(
                """INSERT INTO device_normal_states(ip, payload_json, observed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET
                       payload_json=excluded.payload_json,
                       observed_at=excluded.observed_at""",
                normal_rows,
            )
            db.executemany(
                """INSERT INTO mm_discovered_devices
                       (ip, alias, hostname, last_seen_at, missing_streak, recovery_streak)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET alias=excluded.alias,
                       hostname=excluded.hostname, last_seen_at=excluded.last_seen_at,
                       missing_streak=excluded.missing_streak,
                       recovery_streak=excluded.recovery_streak""",
                discovered_rows,
            )
            if failover is not None:
                primary, actual, error_code, collected_at = failover
                db.execute(
                    """INSERT INTO failover_collections
                           (primary_controller_ip, actual_controller_ip,
                            primary_error_code, collected_at)
                       VALUES (?, ?, ?, ?)""",
                    (primary, actual, error_code, _timestamp(collected_at)),
                )
            self._cleanup_history_if_due(db)
            if protected_ips is None:
                return set()
            return self._cleanup_device_inventory_if_due(
                db,
                protected_ips=protected_ips,
                now=observed_at,
            )

        return self._write(operation)

    def maintain_device_inventory(
        self,
        protected_ips: Iterable[str],
        *,
        now: datetime | str | None = None,
        max_age_days: int = HISTORY_RETENTION_DAYS,
        max_rows: int = HISTORY_RETENTION_MAX_ROWS,
        force: bool = False,
    ) -> set[str]:
        """Bound stale, unregistered device inventory without touching live state.

        Registered IPs supplied by the runtime, durable active incidents, and
        unacknowledged Connection-Type changes are always protected.  Cleanup
        removes a selected IP from all three snapshot/inventory tables in one
        transaction and deliberately does not run ``VACUUM``.
        """

        if int(max_age_days) < 1 or int(max_rows) < 1:
            raise ValueError("device inventory retention limits must be positive")
        normalized = tuple(
            dict.fromkeys(str(ip).strip() for ip in protected_ips if str(ip).strip())
        )
        return self._write(
            lambda db: self._cleanup_device_inventory_if_due(
                db,
                protected_ips=normalized,
                now=now,
                max_age_days=int(max_age_days),
                max_rows=int(max_rows),
                force=force,
            )
        )

    @staticmethod
    def _cleanup_device_inventory_if_due(
        db: sqlite3.Connection,
        *,
        protected_ips: Iterable[str],
        now: datetime | str | None = None,
        max_age_days: int = HISTORY_RETENTION_DAYS,
        max_rows: int = HISTORY_RETENTION_MAX_ROWS,
        force: bool = False,
    ) -> set[str]:
        maintenance_at = _parse_timestamp(_timestamp(now))
        row = db.execute(
            "SELECT value_json FROM preferences WHERE key=?",
            (DEVICE_INVENTORY_RETENTION_MARKER,),
        ).fetchone()
        if row is not None and not force:
            try:
                last_run = _parse_timestamp(str(_json_load(row["value_json"])))
            except (StorageCorruptError, TypeError, ValueError):
                last_run = None
            if last_run is not None:
                elapsed = maintenance_at - last_run
                if timedelta(0) <= elapsed < HISTORY_MAINTENANCE_INTERVAL:
                    return set()

        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS inventory_retention_protected "
            "(ip TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        db.execute("DELETE FROM inventory_retention_protected")
        db.executemany(
            "INSERT OR IGNORE INTO inventory_retention_protected(ip) VALUES (?)",
            ((str(ip).strip(),) for ip in protected_ips if str(ip).strip()),
        )
        db.execute(
            "INSERT OR IGNORE INTO inventory_retention_protected(ip) "
            "SELECT ip FROM incidents WHERE active=1 AND ip<>''"
        )
        db.execute(
            "INSERT OR IGNORE INTO inventory_retention_protected(ip) "
            "SELECT member_ip FROM connection_changes WHERE acknowledged=0 AND member_ip<>''"
        )

        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS inventory_retention_prune "
            "(ip TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        db.execute("DELETE FROM inventory_retention_prune")
        cutoff_text = _timestamp(maintenance_at - timedelta(days=max_age_days))
        db.execute(
            """INSERT OR IGNORE INTO inventory_retention_prune(ip)
               WITH all_sightings(ip, observed_at) AS (
                   SELECT ip, last_seen_at FROM mm_discovered_devices WHERE ip<>''
                   UNION ALL
                   SELECT ip, observed_at FROM device_states WHERE ip<>''
                     AND ip NOT IN (SELECT ip FROM mm_discovered_devices)
                   UNION ALL
                   SELECT ip, observed_at FROM device_normal_states WHERE ip<>''
                     AND ip NOT IN (SELECT ip FROM mm_discovered_devices)
                     AND ip NOT IN (SELECT ip FROM device_states)
               ),
               eligible AS (
                   SELECT sightings.ip, MAX(sightings.observed_at) AS recency
                   FROM all_sightings AS sightings
                   WHERE sightings.ip NOT IN (SELECT ip FROM inventory_retention_protected)
                   GROUP BY sightings.ip
                   HAVING julianday(MAX(sightings.observed_at)) IS NOT NULL
               )
               SELECT eligible.ip FROM eligible
               WHERE julianday(eligible.recency) < julianday(?)
                  OR eligible.ip NOT IN (
                      SELECT newest.ip FROM eligible AS newest
                      ORDER BY julianday(newest.recency) DESC, newest.ip DESC
                      LIMIT ?
                  )""",
            (cutoff_text, int(max_rows)),
        )
        removed = {
            str(row["ip"])
            for row in db.execute("SELECT ip FROM inventory_retention_prune").fetchall()
        }
        for table in ("device_normal_states", "device_states", "mm_discovered_devices"):
            db.execute(
                f"DELETE FROM {table} WHERE ip IN (SELECT ip FROM inventory_retention_prune)"
            )
        db.execute(
            """INSERT INTO preferences(key, value_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value_json=excluded.value_json,
                   updated_at=excluded.updated_at""",
            (
                DEVICE_INVENTORY_RETENTION_MARKER,
                _json_dump(_timestamp(maintenance_at)),
                _timestamp(maintenance_at),
            ),
        )
        return removed

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
        return self._list_incidents(
            active_only=active_only,
            limit=limit,
            enforce_active_restore_cap=False,
        )

    def _list_incidents(
        self,
        *,
        active_only: bool,
        limit: int,
        enforce_active_restore_cap: bool,
    ) -> list[StoredIncident]:
        bounded_limit = max(1, min(int(limit), 10_000))
        condition = "WHERE active=1" if active_only else ""
        def operation(db: sqlite3.Connection) -> list[sqlite3.Row]:
            _raise_if_invalid_integer_rows(db, "incidents")
            if enforce_active_restore_cap:
                active_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM incidents WHERE active=1"
                    ).fetchone()[0]
                )
                if active_count > HISTORY_RETENTION_MAX_ROWS:
                    raise StorageCorruptError(
                        "활성 장애 상태가 안전한 복원 한도를 초과했습니다. "
                        "원본 데이터베이스를 보존한 채 확인하세요."
                    )
            return db.execute(
                f"SELECT * FROM incidents {condition} ORDER BY first_detected_at DESC LIMIT ?", (bounded_limit,)
            ).fetchall()

        rows = self._read(operation)
        result: list[StoredIncident] = []
        for row in rows:
            data = dict(row)
            _validate_stored_integer_record(data, "incidents")
            data["active"] = bool(data["active"])
            data["acknowledged"] = bool(data["acknowledged"])
            data["payload"] = _json_load(data.pop("payload_json"), require_mapping=True)
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
        for stored in self._list_incidents(
            active_only=active_only,
            limit=limit,
            enforce_active_restore_cap=active_only,
        ):
            payload = stored.payload
            try:
                result.append(
                    Incident(
                        incident_id=stored.incident_id,
                        incident_type=IncidentType(stored.incident_type),
                        severity=Severity(str(payload.get("severity", "unknown"))),
                        reason=str(payload.get("reason", stored.reason_key)),
                        first_detected_at=_parse_stored_timestamp(stored.first_detected_at),
                        last_seen_at=_parse_stored_timestamp(stored.last_seen_at),
                        ip=stored.ip or None,
                        alias=payload.get("alias"),
                        active=stored.active,
                        acknowledged_at=_optional_parse_stored_timestamp(
                            payload.get("acknowledged_at")
                        ),
                        recovered_at=_optional_parse_stored_timestamp(
                            payload.get("recovered_at") or stored.resolved_at
                        ),
                        last_notified_at=_optional_parse_stored_timestamp(
                            payload.get("last_notified_at")
                        ),
                        event_token=str(payload.get("event_token", "")),
                        details=dict(payload.get("details", {})),
                    )
                )
            except StorageCorruptError:
                raise
            except (KeyError, TypeError, ValueError):
                raise StorageCorruptError(
                    "저장된 장애 이력 형식이 손상되었습니다."
                ) from None
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
            item["payload"] = _json_load(item.pop("payload_json"), require_mapping=True)
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

    def maintain_history(
        self,
        *,
        now: datetime | str | None = None,
        max_age_days: int = HISTORY_RETENTION_DAYS,
        max_rows: int = HISTORY_RETENTION_MAX_ROWS,
        force: bool = False,
    ) -> dict[str, int]:
        """Apply bounded history retention without running a blocking VACUUM."""

        if int(max_age_days) < 1 or int(max_rows) < 1:
            raise ValueError("history retention limits must be positive")
        return self._write(
            lambda db: self._cleanup_history_if_due(
                db,
                now=now,
                max_age_days=int(max_age_days),
                max_rows=int(max_rows),
                force=force,
            )
        )

    @staticmethod
    def _cleanup_history_if_due(
        db: sqlite3.Connection,
        *,
        now: datetime | str | None = None,
        max_age_days: int = HISTORY_RETENTION_DAYS,
        max_rows: int = HISTORY_RETENTION_MAX_ROWS,
        force: bool = False,
    ) -> dict[str, int]:
        maintenance_at = _parse_timestamp(_timestamp(now))
        row = db.execute(
            "SELECT value_json FROM preferences WHERE key='_history_retention_last_run'"
        ).fetchone()
        if row is not None and not force:
            try:
                last_run = _parse_timestamp(str(_json_load(row["value_json"])))
            except (StorageCorruptError, TypeError, ValueError):
                last_run = None
            if last_run is not None:
                elapsed = maintenance_at - last_run
                if timedelta(0) <= elapsed < HISTORY_MAINTENANCE_INTERVAL:
                    return {
                        "incidents": 0,
                        "events": 0,
                        "failovers": 0,
                        "connection_changes": 0,
                    }

        cutoff = maintenance_at - timedelta(days=max_age_days)
        cutoff_text = _timestamp(cutoff)
        incident_cursor = db.execute(
            """DELETE FROM incidents
               WHERE active=0
                 AND (
                    COALESCE(resolved_at, last_seen_at) < ?
                    OR incident_id NOT IN (
                        SELECT incident_id FROM incidents
                        WHERE active=0
                        ORDER BY last_seen_at DESC, incident_id DESC
                        LIMIT ?
                    )
                 )""",
            (cutoff_text, max_rows),
        )
        event_cursor = db.execute(
            """DELETE FROM events
               WHERE NOT EXISTS (
                    SELECT 1 FROM incidents
                    WHERE incidents.incident_id=events.incident_id
                      AND incidents.active=1
               )
                 AND (
                    occurred_at < ?
                    OR id NOT IN (
                        SELECT candidate.id FROM events AS candidate
                        WHERE NOT EXISTS (
                            SELECT 1 FROM incidents
                            WHERE incidents.incident_id=candidate.incident_id
                              AND incidents.active=1
                        )
                        ORDER BY candidate.occurred_at DESC, candidate.id DESC
                        LIMIT ?
                    )
                 )""",
            (cutoff_text, max_rows),
        )
        failover_cursor = db.execute(
            """DELETE FROM failover_collections
               WHERE collected_at < ?
                  OR id NOT IN (
                      SELECT id FROM failover_collections
                      ORDER BY collected_at DESC, id DESC
                      LIMIT ?
                  )""",
            (cutoff_text, max_rows),
        )
        connection_change_cursor = db.execute(
            """DELETE FROM connection_changes
               WHERE acknowledged=1
                 AND (
                    last_confirmed_at < ?
                    OR event_token NOT IN (
                        SELECT event_token FROM connection_changes
                        WHERE acknowledged=1
                        ORDER BY last_confirmed_at DESC, event_token DESC
                        LIMIT ?
                    )
                 )""",
            (cutoff_text, max_rows),
        )
        db.execute(
            """INSERT INTO preferences(key, value_json, updated_at)
               VALUES ('_history_retention_last_run', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value_json=excluded.value_json,
                   updated_at=excluded.updated_at""",
            (_json_dump(_timestamp(maintenance_at)), _timestamp(maintenance_at)),
        )
        return {
            "incidents": max(0, int(incident_cursor.rowcount)),
            "events": max(0, int(event_cursor.rowcount)),
            "failovers": max(0, int(failover_cursor.rowcount)),
            "connection_changes": max(0, int(connection_change_cursor.rowcount)),
        }

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
                except BaseException:
                    # Python-side serialization/callback failures can happen
                    # after BEGIN and one or more successful statements. Always
                    # return the shared connection to a clean transaction state
                    # before propagating the original failure.
                    try:
                        connection.rollback()
                    except sqlite3.DatabaseError as rollback_error:
                        raise StorageError(
                            "로컬 상태 저장 트랜잭션을 되돌리지 못했습니다."
                        ) from rollback_error
                    raise
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
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError("timestamp must use ISO 8601 format") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_stored_timestamp(value: object) -> datetime:
    try:
        if type(value) is not str:
            raise TypeError("stored timestamp must be text")
        return _parse_timestamp(value)
    except (TypeError, ValueError):
        raise StorageCorruptError("저장된 시간 정보 형식이 손상되었습니다.") from None


def _optional_timestamp(value: datetime | str | None) -> str | None:
    return None if value is None else _timestamp(value)


def _optional_parse_stored_timestamp(value: object) -> datetime | None:
    return None if value in (None, "") else _parse_stored_timestamp(value)


def _normalize_type(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("-", " ").split())


def _validate_json_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        candidate, depth = stack.pop()
        nodes += 1
        if nodes > MAX_STORAGE_JSON_NODES or depth > MAX_STORAGE_JSON_DEPTH:
            raise ValueError("JSON structure exceeds the safe limit")
        if isinstance(candidate, Mapping):
            if len(candidate) > MAX_STORAGE_JSON_NODES:
                raise ValueError("JSON object exceeds the safe limit")
            for key, nested in candidate.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be text")
                stack.append((nested, depth + 1))
        elif isinstance(candidate, list):
            if len(candidate) > MAX_STORAGE_JSON_NODES:
                raise ValueError("JSON array exceeds the safe limit")
            stack.extend((nested, depth + 1) for nested in candidate)
        elif type(candidate) is float:
            if not math.isfinite(candidate):
                raise ValueError("JSON number must be finite")
        elif candidate is None or type(candidate) in {bool, int}:
            continue
        elif type(candidate) is str:
            if len(candidate) > MAX_STORAGE_JSON_BYTES:
                raise ValueError("JSON text exceeds the safe limit")
        else:
            raise ValueError("unsupported JSON value")


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _json_load(value: object, *, require_mapping: bool = False) -> Any:
    try:
        if type(value) is str:
            if len(value) > MAX_STORAGE_JSON_BYTES:
                raise ValueError("stored JSON exceeds the safe limit")
            encoded = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            encoded = bytes(value)
        else:
            raise TypeError("stored JSON must be text or bytes")
        if len(encoded) > MAX_STORAGE_JSON_BYTES:
            raise ValueError("stored JSON exceeds the safe limit")
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(payload)
        if require_mapping and type(payload) is not dict:
            raise TypeError("stored JSON payload must be an object")
        return payload
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise StorageCorruptError("저장된 로컬 상태 형식이 손상되었습니다.") from None


def _json_dump(value: object) -> str:
    try:
        safe = _json_safe(value)
        _validate_json_shape(safe)
    except (RecursionError, ValueError):
        raise ValueError("payload structure exceeds the safe persistence limit") from None
    _reject_secret_payload(safe)
    try:
        encoded = json.dumps(
            safe,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("payload cannot be encoded safely") from None
    if len(encoded) > MAX_STORAGE_JSON_BYTES or len(encoded.encode("utf-8")) > MAX_STORAGE_JSON_BYTES:
        raise ValueError("payload exceeds the safe persistence size limit")
    return encoded


def _json_safe(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(nested) for nested in value]
    if isinstance(value, datetime):
        return _timestamp(value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if hasattr(value, "value"):
        return _json_safe(getattr(value, "value"))
    return str(value)


def _reject_secret_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            tokens = _field_name_tokens(key)
            if tokens not in _NON_SECRET_TOKEN_KEYS and _is_secret_field_name(key):
                raise ValueError("secret-bearing field cannot be persisted")
            _reject_secret_payload(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_payload(nested)


def _reject_secret_preference_key(key: object) -> None:
    tokens = _field_name_tokens(key)
    if tokens not in _NON_SECRET_TOKEN_KEYS and _is_secret_field_name(key):
        raise ValueError("secret-bearing preference key cannot be persisted")


def _input_nonnegative_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _SQLITE_MAX_INTEGER:
        raise ValueError("counter must be a non-negative integer")
    return value


def _invalid_integer_where_clause(table: str) -> tuple[str, tuple[int, ...]]:
    clauses: list[str] = []
    parameters: list[int] = []
    for column, (minimum, maximum) in _REQUIRED_INTEGER_RANGES[table].items():
        clauses.append(
            f'(typeof("{column}")<>\'integer\' OR "{column}"<? OR "{column}">?)'
        )
        parameters.extend((minimum, maximum))
    return " OR ".join(clauses), tuple(parameters)


def _has_invalid_integer_rows(connection: sqlite3.Connection, table: str) -> bool:
    if table not in _REQUIRED_INTEGER_RANGES:
        return False
    clause, parameters = _invalid_integer_where_clause(table)
    return (
        connection.execute(
            f'SELECT 1 FROM "{table}" WHERE {clause} LIMIT 1',
            parameters,
        ).fetchone()
        is not None
    )


def _raise_if_invalid_integer_rows(connection: sqlite3.Connection, table: str) -> None:
    if _has_invalid_integer_rows(connection, table):
        raise StorageCorruptError("stored integer state is invalid")


def _validate_stored_integer_record(record: object, table: str) -> None:
    for column, (minimum, maximum) in _REQUIRED_INTEGER_RANGES[table].items():
        try:
            value = record[column]  # type: ignore[index]
        except (IndexError, KeyError, TypeError):
            raise StorageCorruptError("stored integer state is invalid") from None
        if type(value) is not int or not minimum <= value <= maximum:
            raise StorageCorruptError("stored integer state is invalid")


def _validate_table_contract(
    connection: sqlite3.Connection,
    table: str,
    expected_primary_key: tuple[str, ...],
) -> None:
    object_row = connection.execute(
        "SELECT type, sql FROM sqlite_schema WHERE name=?",
        (table,),
    ).fetchone()
    if object_row is None or str(object_row["type"]).casefold() != "table":
        raise sqlite3.DatabaseError("required local table is unavailable")
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns_by_name = {str(row["name"]): row for row in rows}
    if set(columns_by_name) != _REQUIRED_SCHEMA_COLUMNS[table]:
        raise sqlite3.DatabaseError("required local schema is unavailable")
    actual_primary_key = tuple(
        str(row["name"])
        for row in sorted(rows, key=lambda candidate: int(candidate["pk"]))
        if int(row["pk"]) > 0
    )
    if actual_primary_key != expected_primary_key:
        raise sqlite3.DatabaseError("required local primary key is unavailable")
    if any(
        not bool(columns_by_name[column]["notnull"])
        for column in _REQUIRED_SCHEMA_NOT_NULL[table]
    ):
        raise sqlite3.DatabaseError("required local null constraint is unavailable")
    if any(
        str(columns_by_name[column]["type"]).strip().casefold() != "integer"
        for column in _REQUIRED_INTEGER_RANGES.get(table, {})
    ):
        raise sqlite3.DatabaseError("required local integer type is unavailable")
    normalized_sql = _normalized_schema_sql(object_row["sql"])
    if any(
        required_check not in normalized_sql
        for required_check in _REQUIRED_SCHEMA_CHECKS.get(table, ())
    ):
        raise sqlite3.DatabaseError("required local check constraint is unavailable")
    if expected_primary_key:
        null_clause = " OR ".join(f'"{column}" IS NULL' for column in expected_primary_key)
        if connection.execute(
            f'SELECT 1 FROM "{table}" WHERE {null_clause} LIMIT 1'
        ).fetchone() is not None:
            raise sqlite3.DatabaseError("stored primary key is invalid")
    if _has_invalid_integer_rows(connection, table):
        raise sqlite3.DatabaseError("stored integer value violates the local schema")


def _validate_index_contracts(
    connection: sqlite3.Connection,
    contracts: Mapping[
        str,
        Mapping[str, tuple[bool, tuple[tuple[str, bool], ...], str | None]],
    ],
) -> None:
    for table, required_indexes in contracts.items():
        indexes_by_name = {
            str(row["name"]): row
            for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall()
        }
        for index_name, (expected_unique, expected_columns, expected_predicate) in (
            required_indexes.items()
        ):
            index_row = indexes_by_name.get(index_name)
            expected_partial = expected_predicate is not None
            if (
                index_row is None
                or str(index_row["origin"]).casefold() != "c"
                or bool(index_row["unique"]) is not expected_unique
                or bool(index_row["partial"]) is not expected_partial
            ):
                raise sqlite3.DatabaseError("required local index is unavailable")
            actual_columns = tuple(
                (str(row["name"]), bool(row["desc"]))
                for row in connection.execute(f'PRAGMA index_xinfo("{index_name}")').fetchall()
                if bool(row["key"])
            )
            if actual_columns != expected_columns:
                raise sqlite3.DatabaseError("required local index columns are unavailable")
            sql_row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=? AND tbl_name=?",
                (index_name, table),
            ).fetchone()
            actual_predicate = _normalized_index_predicate(
                None if sql_row is None else sql_row["sql"]
            )
            if actual_predicate != expected_predicate:
                raise sqlite3.DatabaseError("required local index predicate is unavailable")


def _normalized_index_predicate(sql: object) -> str | None:
    """Return a strict canonical WHERE clause for a named application index."""

    if type(sql) is not str:
        return None
    match = re.search(r"\bwhere\b(?P<predicate>.+?)\s*;?\s*$", sql, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    predicate = re.sub(r"\s+", "", match.group("predicate").casefold())
    predicate = predicate.translate(str.maketrans("", "", '"`[]'))
    while _has_outer_parentheses(predicate):
        predicate = predicate[1:-1]
    return predicate


def _normalized_schema_sql(sql: object) -> str:
    if type(sql) is not str:
        return ""
    return re.sub(r"\s+", "", _strip_sql_comments_and_strings(sql).casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def _strip_sql_comments_and_strings(sql: str) -> str:
    """Remove places where CHECK-shaped text has no executable meaning."""

    result: list[str] = []
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            result.append(" ")
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = len(sql) if end < 0 else end + 2
            result.append(" ")
            continue
        if sql[index] == "'":
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            result.append("''")
            continue
        result.append(sql[index])
        index += 1
    return "".join(result)


def _has_outer_parentheses(value: str) -> bool:
    if len(value) < 2 or not value.startswith("(") or not value.endswith(")"):
        return False
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return False
            if depth < 0:
                return False
    return depth == 0


def _is_locked(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "locked" in text or "busy" in text
