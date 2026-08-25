from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .models import (
    RemediationEvent,
    RemediationOutcome,
    RemediationRun,
    RemediationStage,
    utc_now,
)


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 256 * 1024


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


def _dump_json(value: Any) -> str:
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
        raise ValueError("stored remediation JSON payload exceeds the safe limit")
    return json.loads(value)


class RemediationRepository:
    """Small append-oriented audit store isolated from the health-state database."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
                reload_reserved INTEGER NOT NULL CHECK (reload_reserved IN (0,1)),
                reload_sent INTEGER NOT NULL CHECK (reload_sent IN (0,1)),
                rebalance_reserved INTEGER NOT NULL CHECK (rebalance_reserved IN (0,1)),
                rebalance_sent INTEGER NOT NULL CHECK (rebalance_sent IN (0,1)),
                rebalance_confirmed INTEGER NOT NULL CHECK (rebalance_confirmed IN (0,1)),
                failure_code TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                report_path TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_remediation_runs_target_started
                ON remediation_runs(target_ip, started_at DESC);
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
                reload_reserved INTEGER NOT NULL CHECK (reload_reserved IN (0,1)),
                rebalance_reserved INTEGER NOT NULL CHECK (rebalance_reserved IN (0,1)),
                active INTEGER NOT NULL CHECK (active IN (0,1)),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES remediation_runs(run_id) ON DELETE CASCADE
            );
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
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError("지원하지 않는 자동 장애조치 저장소 버전입니다.")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

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
                        rebalance_sent, rebalance_confirmed, failure_code, summary,
                        report_path, app_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run.run_id,
                        run.incident_key,
                        run.target_ip,
                        run.target_alias,
                        run.cluster_name,
                        _dump_json(list(run.expected_member_ips)),
                        _iso(run.started_at),
                        _iso(run.ended_at),
                        run.stage.value,
                        run.outcome.value,
                        run.leader_ip,
                        int(run.reload_reserved),
                        int(run.reload_sent),
                        int(run.rebalance_reserved),
                        int(run.rebalance_sent),
                        int(run.rebalance_confirmed),
                        run.failure_code,
                        run.summary,
                        run.report_path,
                        run.app_version,
                    ),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def update_run(self, run: RemediationRun) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE remediation_runs SET
                    ended_at=?, stage=?, outcome=?, leader_ip=?,
                    reload_reserved=?, reload_sent=?, rebalance_reserved=?,
                    rebalance_sent=?, rebalance_confirmed=?, failure_code=?,
                    summary=?, report_path=?, app_version=?
                WHERE run_id=?
                """,
                (
                    _iso(run.ended_at),
                    run.stage.value,
                    run.outcome.value,
                    run.leader_ip,
                    int(run.reload_reserved),
                    int(run.reload_sent),
                    int(run.rebalance_reserved),
                    int(run.rebalance_sent),
                    int(run.rebalance_confirmed),
                    run.failure_code,
                    run.summary,
                    run.report_path,
                    run.app_version,
                    run.run_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE remediation_target_locks SET
                    reload_reserved=?, rebalance_reserved=?, updated_at=?
                WHERE target_ip=? AND run_id=?
                """,
                (
                    int(run.reload_reserved),
                    int(run.rebalance_reserved),
                    _iso(utc_now()),
                    run.target_ip,
                    run.run_id,
                ),
            )

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
                    run_id,
                    event.sequence_no,
                    _iso(event.occurred_at),
                    event.stage.value,
                    event.operation,
                    event.result_code,
                    event.message,
                    event.endpoint_ip,
                    event.attempt,
                    max(0, int(event.duration_ms)),
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
                    captured_at=excluded.captured_at,
                    data_json=excluded.data_json
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
                        rebalance_reserved, active, updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(target_ip) DO UPDATE SET
                        incident_key=excluded.incident_key,
                        run_id=excluded.run_id,
                        reload_reserved=0,
                        rebalance_reserved=0,
                        active=1,
                        updated_at=excluded.updated_at
                    """,
                    (target_ip, incident_key, run_id, 0, 0, 1, now),
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

    def release_target(self, target_ip: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE remediation_target_locks SET active=0, updated_at=? WHERE target_ip=?",
                (_iso(utc_now()), target_ip),
            )

    def recover_interrupted_runs(self) -> list[str]:
        """Close process-owned RUNNING rows without releasing their target locks."""

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
                        UPDATE remediation_runs SET
                            outcome=?, stage=?, ended_at=?, failure_code=?, summary=?
                        WHERE run_id=?
                        """,
                        (
                            RemediationOutcome.INTERRUPTED.value,
                            RemediationStage.INTERRUPTED.value,
                            _iso(now),
                            "PROCESS_INTERRUPTED",
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
                            run_id,
                            int(next_seq),
                            _iso(now),
                            RemediationStage.INTERRUPTED.value,
                            "process_recovery",
                            "PROCESS_INTERRUPTED",
                            "프로그램 종료로 진행 중 조치를 비정상 중단 상태로 전환했습니다.",
                            "",
                            0,
                            "{}",
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return recovered

    def load_run(self, run_id: str) -> RemediationRun:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM remediation_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            events = self._connection.execute(
                "SELECT * FROM remediation_events WHERE run_id=? ORDER BY sequence_no",
                (run_id,),
            ).fetchall()
            snapshots = self._connection.execute(
                "SELECT * FROM remediation_snapshots WHERE run_id=? ORDER BY name",
                (run_id,),
            ).fetchall()
        run = RemediationRun(
            run_id=str(row["run_id"]),
            incident_key=str(row["incident_key"]),
            target_ip=str(row["target_ip"]),
            target_alias=str(row["target_alias"]),
            cluster_name=str(row["cluster_name"]),
            expected_member_ips=tuple(str(item) for item in _load_json(row["expected_member_ips_json"])),
            started_at=_dt(row["started_at"]) or utc_now(),
            stage=RemediationStage(str(row["stage"])),
            outcome=RemediationOutcome(str(row["outcome"])),
            ended_at=_dt(row["ended_at"]),
            leader_ip=str(row["leader_ip"]),
            reload_reserved=bool(row["reload_reserved"]),
            reload_sent=bool(row["reload_sent"]),
            rebalance_reserved=bool(row["rebalance_reserved"]),
            rebalance_sent=bool(row["rebalance_sent"]),
            rebalance_confirmed=bool(row["rebalance_confirmed"]),
            failure_code=str(row["failure_code"]),
            summary=str(row["summary"]),
            report_path=str(row["report_path"]),
            app_version=str(row["app_version"]),
        )
        run.events = [
            RemediationEvent(
                sequence_no=int(item["sequence_no"]),
                occurred_at=_dt(item["occurred_at"]) or utc_now(),
                stage=RemediationStage(str(item["stage"])),
                operation=str(item["operation"]),
                result_code=str(item["result_code"]),
                message=str(item["message"]),
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
                SELECT report_path FROM remediation_runs
                WHERE report_path <> ''
                ORDER BY COALESCE(ended_at, started_at) DESC
                LIMIT 1
                """
            ).fetchone()
        return "" if row is None else str(row["report_path"])
