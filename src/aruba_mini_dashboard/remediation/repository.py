from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .models import (
    DispatchPhase,
    RemediationEvent,
    RemediationOutcome,
    RemediationRun,
    RemediationStage,
    utc_now,
)


SCHEMA_VERSION = 2
MAX_JSON_BYTES = 256 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 10_000
RETENTION_DAYS = 180


class RemediationStorageError(RuntimeError):
    pass


class RemediationStorageCorruptError(RemediationStorageError):
    pass


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _validate_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError("remediation JSON node count exceeds the safe limit")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("remediation JSON depth exceeds the safe limit")
        if current is None or isinstance(current, (str, int, float, bool)):
            continue
        if isinstance(current, Mapping):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError("remediation JSON object keys must be text")
                stack.append((item, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            for item in current:
                stack.append((item, depth + 1))
            continue
        if isinstance(current, (datetime, Enum)):
            continue
        raise ValueError(f"unsupported remediation JSON value: {type(current).__name__}")


def _dump_json(value: Any) -> str:
    _validate_json_shape(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("remediation JSON payload exceeds the safe limit")
    return encoded


def _load_json(value: str) -> Any:
    if len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise RemediationStorageCorruptError("stored remediation JSON exceeds the safe limit")
    try:
        decoded = json.loads(value)
        _validate_json_shape(decoded)
        return decoded
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RemediationStorageCorruptError("stored remediation JSON is invalid") from exc


_REQUIRED_COLUMNS = {
    "remediation_runs": {
        "run_id", "incident_key", "target_ip", "target_alias", "cluster_name",
        "expected_member_ips_json", "started_at", "ended_at", "stage", "outcome",
        "leader_ip", "reload_reserved", "reload_sent", "rebalance_reserved",
        "rebalance_sent", "rebalance_confirmed", "reload_dispatch_phase",
        "rebalance_dispatch_phase", "failure_code", "summary", "report_path",
        "report_error", "report_pending", "configuration_fingerprint", "app_version",
    },
    "remediation_events": {
        "run_id", "sequence_no", "occurred_at", "stage", "operation", "result_code",
        "message", "endpoint_ip", "attempt", "duration_ms", "evidence_json",
    },
    "remediation_snapshots": {"run_id", "name", "captured_at", "data_json"},
    "remediation_target_locks": {
        "target_ip", "incident_key", "run_id", "reload_reserved",
        "rebalance_reserved", "recovery_streak", "active", "updated_at",
    },
}


class RemediationRepository:
    """Transactional remediation audit store and duplicate-action circuit breaker."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.database,
                timeout=5.0,
                check_same_thread=False,
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            with self._lock:
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA busy_timeout = 5000")
                if self.database != ":memory:":
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    self._connection.execute("PRAGMA synchronous = FULL")
                self._initialize()
                self._validate_integrity()
                self._maintain()
        except sqlite3.DatabaseError as exc:
            raise RemediationStorageCorruptError("자동 장애조치 저장소가 손상되었거나 읽을 수 없습니다.") from exc

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS remediation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remediation_runs (
                run_id TEXT PRIMARY KEY,
                incident_key TEXT NOT NULL,
                target_ip TEXT NOT NULL,
                target_alias TEXT NOT NULL,
                cluster_name TEXT NOT NULL,
                expected_member_ips_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                stage TEXT NOT NULL,
                outcome TEXT NOT NULL,
                leader_ip TEXT NOT NULL DEFAULT '',
                reload_reserved INTEGER NOT NULL DEFAULT 0 CHECK (reload_reserved IN (0,1)),
                reload_sent INTEGER NOT NULL DEFAULT 0 CHECK (reload_sent IN (0,1)),
                rebalance_reserved INTEGER NOT NULL DEFAULT 0 CHECK (rebalance_reserved IN (0,1)),
                rebalance_sent INTEGER NOT NULL DEFAULT 0 CHECK (rebalance_sent IN (0,1)),
                rebalance_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (rebalance_confirmed IN (0,1)),
                reload_dispatch_phase TEXT NOT NULL DEFAULT 'not_attempted',
                rebalance_dispatch_phase TEXT NOT NULL DEFAULT 'not_attempted',
                failure_code TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                report_path TEXT NOT NULL DEFAULT '',
                report_error TEXT NOT NULL DEFAULT '',
                report_pending INTEGER NOT NULL DEFAULT 0 CHECK (report_pending IN (0,1)),
                configuration_fingerprint TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS remediation_events (
                run_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                operation TEXT NOT NULL,
                result_code TEXT NOT NULL,
                message TEXT NOT NULL,
                endpoint_ip TEXT NOT NULL DEFAULT '',
                attempt INTEGER,
                duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
                evidence_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(run_id, sequence_no),
                FOREIGN KEY(run_id) REFERENCES remediation_runs(run_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS remediation_snapshots (
                run_id TEXT NOT NULL,
                name TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY(run_id, name),
                FOREIGN KEY(run_id) REFERENCES remediation_runs(run_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS remediation_target_locks (
                target_ip TEXT PRIMARY KEY,
                incident_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                reload_reserved INTEGER NOT NULL DEFAULT 0 CHECK (reload_reserved IN (0,1)),
                rebalance_reserved INTEGER NOT NULL DEFAULT 0 CHECK (rebalance_reserved IN (0,1)),
                recovery_streak INTEGER NOT NULL DEFAULT 0 CHECK (recovery_streak >= 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES remediation_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_remediation_runs_target_started
                ON remediation_runs(target_ip, started_at DESC);
            """
        )
        row = self._connection.execute(
            "SELECT value FROM remediation_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO remediation_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        version = int(row["value"])
        if version == 1:
            self._migrate_v1_to_v2()
            version = 2
        if version != SCHEMA_VERSION:
            raise RemediationStorageCorruptError("지원하지 않는 자동 장애조치 저장소 버전입니다.")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_remediation_runs_report_pending "
            "ON remediation_runs(report_pending, ended_at DESC)"
        )

    def _column_names(self, table: str) -> set[str]:
        return {str(row["name"]) for row in self._connection.execute(f"PRAGMA table_info({table})")}

    def _migrate_v1_to_v2(self) -> None:
        additions = {
            "remediation_runs": {
                "reload_dispatch_phase": "TEXT NOT NULL DEFAULT 'not_attempted'",
                "rebalance_dispatch_phase": "TEXT NOT NULL DEFAULT 'not_attempted'",
                "report_error": "TEXT NOT NULL DEFAULT ''",
                "report_pending": "INTEGER NOT NULL DEFAULT 0",
                "configuration_fingerprint": "TEXT NOT NULL DEFAULT ''",
            },
            "remediation_target_locks": {
                "recovery_streak": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in additions.items():
                existing = self._column_names(table)
                for name, declaration in columns.items():
                    if name not in existing:
                        self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self._connection.execute(
                "UPDATE remediation_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def _validate_integrity(self) -> None:
        quick = self._connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            raise RemediationStorageCorruptError("자동 장애조치 저장소 무결성 검사에 실패했습니다.")
        tables = {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, expected in _REQUIRED_COLUMNS.items():
            if table not in tables or not expected.issubset(self._column_names(table)):
                raise RemediationStorageCorruptError(f"자동 장애조치 저장소 스키마가 불완전합니다: {table}")

    def _maintain(self) -> None:
        cutoff = _iso(utc_now() - timedelta(days=RETENTION_DAYS))
        self._connection.execute(
            """
            DELETE FROM remediation_runs
            WHERE ended_at IS NOT NULL AND ended_at < ?
              AND run_id NOT IN (SELECT run_id FROM remediation_target_locks WHERE active=1)
            """,
            (cutoff,),
        )

    def preflight(self) -> None:
        with self._lock:
            self._validate_integrity()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("SELECT 1")
                self._connection.execute("ROLLBACK")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            if self.database != ":memory:":
                try:
                    self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
            self._connection.close()

    @staticmethod
    def _run_values(run: RemediationRun) -> tuple[Any, ...]:
        return (
            _iso(run.ended_at), run.stage.value, run.outcome.value, run.leader_ip,
            int(run.reload_reserved), int(run.reload_sent), int(run.rebalance_reserved),
            int(run.rebalance_sent), int(run.rebalance_confirmed),
            run.reload_dispatch_phase.value, run.rebalance_dispatch_phase.value,
            run.failure_code, run.summary, run.report_path, run.report_error,
            int(run.report_pending), run.configuration_fingerprint, run.app_version,
            run.run_id,
        )

    def create_run(self, run: RemediationRun) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO remediation_runs(
                        run_id, incident_key, target_ip, target_alias, cluster_name,
                        expected_member_ips_json, started_at, ended_at, stage, outcome,
                        leader_ip, reload_reserved, reload_sent, rebalance_reserved,
                        rebalance_sent, rebalance_confirmed, reload_dispatch_phase,
                        rebalance_dispatch_phase, failure_code, summary, report_path,
                        report_error, report_pending, configuration_fingerprint, app_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run.run_id, run.incident_key, run.target_ip, run.target_alias,
                        run.cluster_name, _dump_json(list(run.expected_member_ips)),
                        _iso(run.started_at), _iso(run.ended_at), run.stage.value,
                        run.outcome.value, run.leader_ip, int(run.reload_reserved),
                        int(run.reload_sent), int(run.rebalance_reserved),
                        int(run.rebalance_sent), int(run.rebalance_confirmed),
                        run.reload_dispatch_phase.value, run.rebalance_dispatch_phase.value,
                        run.failure_code, run.summary, run.report_path, run.report_error,
                        int(run.report_pending), run.configuration_fingerprint, run.app_version,
                    ),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _update_run_sql(self, run: RemediationRun) -> None:
        cursor = self._connection.execute(
            """
            UPDATE remediation_runs SET
                ended_at=?, stage=?, outcome=?, leader_ip=?, reload_reserved=?, reload_sent=?,
                rebalance_reserved=?, rebalance_sent=?, rebalance_confirmed=?,
                reload_dispatch_phase=?, rebalance_dispatch_phase=?, failure_code=?, summary=?,
                report_path=?, report_error=?, report_pending=?, configuration_fingerprint=?,
                app_version=? WHERE run_id=?
            """,
            self._run_values(run),
        )
        if cursor.rowcount != 1:
            raise RemediationStorageCorruptError("자동 장애조치 실행 상태 행을 찾지 못했습니다.")
        self._connection.execute(
            """
            UPDATE remediation_target_locks SET
                reload_reserved=?, rebalance_reserved=?, updated_at=?
            WHERE target_ip=? AND run_id=?
            """,
            (
                int(run.reload_reserved), int(run.rebalance_reserved), _iso(utc_now()),
                run.target_ip, run.run_id,
            ),
        )

    def update_run(self, run: RemediationRun) -> None:
        self.commit_transition(run)

    def commit_transition(
        self,
        run: RemediationRun,
        *,
        event: RemediationEvent | None = None,
        snapshot_name: str | None = None,
        snapshot_data: Mapping[str, Any] | None = None,
        captured_at: datetime | None = None,
    ) -> None:
        """Atomically persist run, lock, event and optional snapshot."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._update_run_sql(run)
                if event is not None:
                    self._connection.execute(
                        """
                        INSERT INTO remediation_events(
                            run_id, sequence_no, occurred_at, stage, operation, result_code,
                            message, endpoint_ip, attempt, duration_ms, evidence_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run.run_id, event.sequence_no, _iso(event.occurred_at),
                            event.stage.value, event.operation, event.result_code,
                            event.message, event.endpoint_ip, event.attempt,
                            max(0, int(event.duration_ms)), _dump_json(dict(event.evidence)),
                        ),
                    )
                if snapshot_name is not None:
                    if snapshot_data is None:
                        raise ValueError("snapshot_data is required with snapshot_name")
                    self._connection.execute(
                        """
                        INSERT INTO remediation_snapshots(run_id, name, captured_at, data_json)
                        VALUES(?,?,?,?)
                        ON CONFLICT(run_id, name) DO UPDATE SET
                            captured_at=excluded.captured_at, data_json=excluded.data_json
                        """,
                        (
                            run.run_id, snapshot_name, _iso(captured_at or utc_now()),
                            _dump_json(dict(snapshot_data)),
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def append_event(self, run_id: str, event: RemediationEvent) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO remediation_events(
                    run_id, sequence_no, occurred_at, stage, operation, result_code,
                    message, endpoint_ip, attempt, duration_ms, evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, event.sequence_no, _iso(event.occurred_at), event.stage.value,
                    event.operation, event.result_code, event.message, event.endpoint_ip,
                    event.attempt, max(0, int(event.duration_ms)),
                    _dump_json(dict(event.evidence)),
                ),
            )

    def save_snapshot(
        self,
        run_id: str,
        name: str,
        data: Mapping[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO remediation_snapshots(run_id, name, captured_at, data_json)
                VALUES(?,?,?,?)
                ON CONFLICT(run_id, name) DO UPDATE SET
                    captured_at=excluded.captured_at, data_json=excluded.data_json
                """,
                (run_id, name, _iso(captured_at or utc_now()), _dump_json(dict(data))),
            )

    def claim_target(self, target_ip: str, incident_key: str, run_id: str) -> bool:
        now = _iso(utc_now())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT active FROM remediation_target_locks WHERE target_ip=?",
                    (target_ip,),
                ).fetchone()
                if row is not None and int(row["active"]) == 1:
                    self._connection.execute("ROLLBACK")
                    return False
                self._connection.execute(
                    """
                    INSERT INTO remediation_target_locks(
                        target_ip, incident_key, run_id, reload_reserved,
                        rebalance_reserved, recovery_streak, active, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(target_ip) DO UPDATE SET
                        incident_key=excluded.incident_key, run_id=excluded.run_id,
                        reload_reserved=0, rebalance_reserved=0, recovery_streak=0,
                        active=1, updated_at=excluded.updated_at
                    """,
                    (target_ip, incident_key, run_id, 0, 0, 0, 1, now),
                )
                self._connection.execute("COMMIT")
                return True
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def is_target_claimed(self, target_ip: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT active FROM remediation_target_locks WHERE target_ip=?",
                (target_ip,),
            ).fetchone()
            return row is not None and int(row["active"]) == 1

    def release_target(self, target_ip: str, run_id: str | None = None) -> bool:
        with self._lock:
            if run_id is None:
                cursor = self._connection.execute(
                    "UPDATE remediation_target_locks SET active=0, recovery_streak=0, updated_at=? WHERE target_ip=?",
                    (_iso(utc_now()), target_ip),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE remediation_target_locks
                    SET active=0, recovery_streak=0, updated_at=?
                    WHERE target_ip=? AND run_id=?
                    """,
                    (_iso(utc_now()), target_ip, run_id),
                )
            return cursor.rowcount == 1

    def observe_target_recovery(self, target_ip: str, healthy: bool, required: int) -> bool:
        """Release a stale target lock only after consecutive trusted recovery cycles."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT active, recovery_streak FROM remediation_target_locks WHERE target_ip=?",
                    (target_ip,),
                ).fetchone()
                if row is None or int(row["active"]) == 0:
                    self._connection.execute("ROLLBACK")
                    return False
                streak = int(row["recovery_streak"]) + 1 if healthy else 0
                release = healthy and streak >= max(2, int(required))
                self._connection.execute(
                    """
                    UPDATE remediation_target_locks
                    SET recovery_streak=?, active=?, updated_at=? WHERE target_ip=?
                    """,
                    (0 if release else streak, 0 if release else 1, _iso(utc_now()), target_ip),
                )
                self._connection.execute("COMMIT")
                return release
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def circuit_breaker_reason(
        self,
        target_ip: str,
        *,
        cooldown_seconds: int,
        max_actions_24h: int,
        now: datetime | None = None,
    ) -> str:
        reference = now or utc_now()
        with self._lock:
            lock = self._connection.execute(
                "SELECT active FROM remediation_target_locks WHERE target_ip=?",
                (target_ip,),
            ).fetchone()
            if lock is not None and int(lock["active"]) == 1:
                return "동일 Controller의 이전 자동 장애조치 잠금이 유지되고 있습니다."
            since_24h = _iso(reference - timedelta(hours=24))
            count = self._connection.execute(
                """
                SELECT COUNT(*) AS value FROM remediation_runs
                WHERE target_ip=? AND reload_reserved=1 AND started_at>=?
                """,
                (target_ip, since_24h),
            ).fetchone()["value"]
            if int(count) >= int(max_actions_24h):
                return f"동일 Controller의 24시간 자동조치 한도({max_actions_24h}회)에 도달했습니다."
            latest = self._connection.execute(
                """
                SELECT ended_at, started_at FROM remediation_runs
                WHERE reload_reserved=1 ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 1
                """
            ).fetchone()
            if latest is not None:
                latest_time = _dt(latest["ended_at"] or latest["started_at"])
                if latest_time is not None:
                    remaining = int(cooldown_seconds - (reference - latest_time).total_seconds())
                    if remaining > 0:
                        return f"Cluster 자동조치 냉각시간이 {remaining}초 남아 있습니다."
        return ""

    def recover_interrupted_runs(self) -> list[str]:
        now = utc_now()
        recovered: list[str] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT run_id FROM remediation_runs WHERE outcome=?",
                    (RemediationOutcome.RUNNING.value,),
                ).fetchall()
                for row in rows:
                    run_id = str(row["run_id"])
                    recovered.append(run_id)
                    self._connection.execute(
                        """
                        UPDATE remediation_runs SET outcome=?, stage=?, ended_at=?,
                            failure_code=?, summary=?, report_pending=1
                        WHERE run_id=?
                        """,
                        (
                            RemediationOutcome.INTERRUPTED.value,
                            RemediationStage.INTERRUPTED.value,
                            _iso(now), "PROCESS_INTERRUPTED",
                            "프로그램 종료로 자동 장애조치가 비정상 중단되었습니다.",
                            run_id,
                        ),
                    )
                    next_seq = self._connection.execute(
                        "SELECT COALESCE(MAX(sequence_no),0)+1 AS value FROM remediation_events WHERE run_id=?",
                        (run_id,),
                    ).fetchone()["value"]
                    self._connection.execute(
                        """
                        INSERT INTO remediation_events(
                            run_id, sequence_no, occurred_at, stage, operation,
                            result_code, message, endpoint_ip, duration_ms, evidence_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id, int(next_seq), _iso(now),
                            RemediationStage.INTERRUPTED.value, "process_recovery",
                            "PROCESS_INTERRUPTED",
                            "프로그램 종료로 진행 중 조치를 비정상 중단 상태로 전환했습니다.",
                            "", 0, "{}",
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return recovered

    def pending_report_run_ids(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM remediation_runs WHERE report_pending=1 ORDER BY COALESCE(ended_at,started_at)"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def load_run(self, run_id: str) -> RemediationRun:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM remediation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            events = self._connection.execute(
                "SELECT * FROM remediation_events WHERE run_id=? ORDER BY sequence_no", (run_id,)
            ).fetchall()
            snapshots = self._connection.execute(
                "SELECT * FROM remediation_snapshots WHERE run_id=? ORDER BY name", (run_id,)
            ).fetchall()
        run = RemediationRun(
            run_id=str(row["run_id"]), incident_key=str(row["incident_key"]),
            target_ip=str(row["target_ip"]), target_alias=str(row["target_alias"]),
            cluster_name=str(row["cluster_name"]),
            expected_member_ips=tuple(str(item) for item in _load_json(row["expected_member_ips_json"])),
            started_at=_dt(row["started_at"]) or utc_now(),
            stage=RemediationStage(str(row["stage"])),
            outcome=RemediationOutcome(str(row["outcome"])),
            ended_at=_dt(row["ended_at"]), leader_ip=str(row["leader_ip"]),
            reload_reserved=bool(row["reload_reserved"]), reload_sent=bool(row["reload_sent"]),
            rebalance_reserved=bool(row["rebalance_reserved"]), rebalance_sent=bool(row["rebalance_sent"]),
            rebalance_confirmed=bool(row["rebalance_confirmed"]),
            reload_dispatch_phase=DispatchPhase(str(row["reload_dispatch_phase"])),
            rebalance_dispatch_phase=DispatchPhase(str(row["rebalance_dispatch_phase"])),
            failure_code=str(row["failure_code"]), summary=str(row["summary"]),
            report_path=str(row["report_path"]), report_error=str(row["report_error"]),
            report_pending=bool(row["report_pending"]),
            configuration_fingerprint=str(row["configuration_fingerprint"]),
            app_version=str(row["app_version"]),
        )
        run.events = [
            RemediationEvent(
                sequence_no=int(item["sequence_no"]),
                occurred_at=_dt(item["occurred_at"]) or utc_now(),
                stage=RemediationStage(str(item["stage"])), operation=str(item["operation"]),
                result_code=str(item["result_code"]), message=str(item["message"]),
                endpoint_ip=str(item["endpoint_ip"]),
                attempt=None if item["attempt"] is None else int(item["attempt"]),
                duration_ms=int(item["duration_ms"]),
                evidence=dict(_load_json(item["evidence_json"])),
            )
            for item in events
        ]
        run.snapshots = {
            str(item["name"]): dict(_load_json(item["data_json"])) for item in snapshots
        }
        return run

    def latest_report_path(self) -> str:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT report_path FROM remediation_runs WHERE report_path<>''
                ORDER BY COALESCE(ended_at,started_at) DESC LIMIT 1
                """
            ).fetchone()
        return "" if row is None else str(row["report_path"])
