from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RemediationStage(str, Enum):
    DETECTED = "detected"
    LOCAL_PREFLIGHT = "local_preflight"
    PRECHECK = "precheck"
    TARGET_SSH = "target_ssh"
    RELOAD = "reload"
    WAITING_MM_UP = "waiting_mm_up"
    FINDING_LEADER = "finding_leader"
    VERIFYING_REJOIN = "verifying_rejoin"
    LEADER_SSH = "leader_ssh"
    FINAL_GATE = "final_gate"
    REBALANCE = "rebalance"
    POST_MONITORING = "post_monitoring"
    REPORTING = "reporting"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class RemediationOutcome(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class DispatchPhase(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    RESERVED = "reserved"
    WRITE_ATTEMPTED = "write_attempted"
    WRITE_RETURNED = "write_returned"
    RESPONSE_OBSERVED = "response_observed"


class ActionResultCode(str, Enum):
    RELOAD_DISPATCHED = "reload_dispatched"
    EXPECTED_DISCONNECT = "expected_disconnect"
    RELOAD_REJECTED = "reload_rejected"
    RESULT_UNKNOWN_AFTER_SEND = "result_unknown_after_send"
    ACTION_NOT_SENT = "action_not_sent"
    REBALANCE_TRIGGERED = "rebalance_triggered"
    REBALANCE_REJECTED = "rebalance_rejected"
    REBALANCE_UNCONFIRMED = "rebalance_unconfirmed"
    REBALANCE_RESULT_UNKNOWN = "rebalance_result_unknown"


@dataclass(slots=True, frozen=True)
class ActionCommandResult:
    command: str
    code: ActionResultCode
    sent: bool
    accepted: bool | None
    output_excerpt: str = ""
    duration_ms: int = 0
    message: str = ""
    dispatch_phase: DispatchPhase = DispatchPhase.NOT_ATTEMPTED


@dataclass(slots=True, frozen=True)
class MmObservation:
    collected_at: datetime
    states: Mapping[str, str]
    hostnames: Mapping[str, str | None] = field(default_factory=dict)
    complete: bool = True
    source_ip: str = ""
    error_code: str = ""
    error_message: str = ""

    def state_for(self, ip: str) -> str:
        return str(self.states.get(ip, "unknown")).strip().casefold()

    @property
    def down_ips(self) -> tuple[str, ...]:
        return tuple(sorted(ip for ip in self.states if self.state_for(ip) == "down"))


@dataclass(slots=True, frozen=True)
class ClusterMemberObservation:
    ip: str
    status: str = ""
    connection_type: str = ""
    is_connected: bool = False
    is_leader: bool = False
    active_clients: int | None = None
    standby_clients: int | None = None


@dataclass(slots=True, frozen=True)
class ClusterObservation:
    collected_at: datetime
    source_ip: str
    members: Mapping[str, ClusterMemberObservation]
    complete: bool = True
    membership_complete: bool = True
    distribution_complete: bool = True
    error_code: str = ""
    error_message: str = ""

    @property
    def leader_ips(self) -> tuple[str, ...]:
        return tuple(sorted(ip for ip, row in self.members.items() if row.is_leader))

    def all_expected_connected(self, expected_ips: tuple[str, ...]) -> bool:
        return bool(expected_ips) and all(
            ip in self.members and self.members[ip].is_connected for ip in expected_ips
        )

    def all_expected_have_distribution(self, expected_ips: tuple[str, ...]) -> bool:
        return bool(expected_ips) and all(
            ip in self.members
            and self.members[ip].active_clients is not None
            and self.members[ip].standby_clients is not None
            for ip in expected_ips
        )


@dataclass(slots=True, frozen=True)
class RebalanceGateObservation:
    leader_ip: str
    mm: MmObservation
    cluster: ClusterObservation
    passed: bool
    reason_code: str = ""
    reason_message: str = ""


@dataclass(slots=True, frozen=True)
class RemediationCandidate:
    incident_key: str
    target_ip: str
    target_alias: str
    cluster_name: str
    expected_member_ips: tuple[str, ...]
    detected_at: datetime
    detected_snapshot: Mapping[str, Any] = field(default_factory=dict)
    configuration_fingerprint: str = ""


@dataclass(slots=True, frozen=True)
class RemediationEvent:
    sequence_no: int
    occurred_at: datetime
    stage: RemediationStage
    operation: str
    result_code: str
    message: str
    endpoint_ip: str = ""
    attempt: int | None = None
    duration_ms: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RemediationRun:
    run_id: str
    incident_key: str
    target_ip: str
    target_alias: str
    cluster_name: str
    expected_member_ips: tuple[str, ...]
    started_at: datetime
    stage: RemediationStage = RemediationStage.DETECTED
    outcome: RemediationOutcome = RemediationOutcome.RUNNING
    ended_at: datetime | None = None
    leader_ip: str = ""
    reload_reserved: bool = False
    reload_sent: bool = False
    rebalance_reserved: bool = False
    rebalance_sent: bool = False
    rebalance_confirmed: bool = False
    reload_dispatch_phase: DispatchPhase = DispatchPhase.NOT_ATTEMPTED
    rebalance_dispatch_phase: DispatchPhase = DispatchPhase.NOT_ATTEMPTED
    failure_code: str = ""
    summary: str = ""
    report_path: str = ""
    report_error: str = ""
    report_pending: bool = False
    configuration_fingerprint: str = ""
    app_version: str = ""
    events: list[RemediationEvent] = field(default_factory=list)
    snapshots: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or utc_now()
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(slots=True, frozen=True)
class WorkflowResult:
    run_id: str
    outcome: RemediationOutcome
    stage: RemediationStage
    target_ip: str
    message: str
    report_path: str = ""
    leader_ip: str = ""
    report_error: str = ""
