from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Mapping, TypeVar


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persisted domain events."""

    return datetime.now(timezone.utc)


class Severity(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ParseStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class DetectionMode(str, Enum):
    ABSOLUTE_ONLY = "absolute_only"
    ABSOLUTE_AND_RELATIVE = "absolute_and_relative"


class IncidentType(str, Enum):
    MM_DOWN = "mm_down"
    CLIENT_DISTRIBUTION = "client_distribution"
    CONNECTION_TYPE_CHANGED = "connection_type_changed"
    MM_MEMBER_MISSING = "mm_member_missing"
    LOAD_MEMBER_MISSING = "load_member_missing"
    MEMBERSHIP_MEMBER_MISSING = "membership_member_missing"
    COLLECTION_FAILURE = "collection_failure"


class IncidentTransitionKind(str, Enum):
    ACTIVATED = "activated"
    UPDATED = "updated"
    RECOVERED = "recovered"
    ACKNOWLEDGED = "acknowledged"
    SUPERSEDED = "superseded"


@dataclass(slots=True, frozen=True)
class ParseIssue:
    code: str
    message: str
    line_number: int | None = None
    snippet: str = ""


T = TypeVar("T")


@dataclass(slots=True)
class ParseResult(Generic[T]):
    status: ParseStatus
    rows: list[T] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    header_map: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    output_excerpt: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status is not ParseStatus.FAILED and bool(self.rows)

    @property
    def is_complete(self) -> bool:
        return self.status is ParseStatus.COMPLETE


@dataclass(slots=True, frozen=True)
class MmSwitchRow:
    ip: str
    hostname: str | None
    status: str
    raw_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ClientDistributionRow:
    ip: str
    active_clients: int
    standby_clients: int
    raw_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class GroupMembershipRow:
    ip: str
    connection_type: str
    raw_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CollectionError:
    source: str
    code: str
    user_message: str
    technical_message: str = ""
    target_ip: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PollCycleResult:
    """Parsed, immutable-in-practice input to health correlation.

    Collectors deliberately expose their transport contract separately.  The
    poll coordinator converts command outputs into these parser results so a
    failed SSH operation can never masquerade as a parsed device state.
    """

    checked_at: datetime
    expected_cluster_members: Mapping[str, str]
    mm_result: ParseResult[MmSwitchRow] | None = None
    load_result: ParseResult[ClientDistributionRow] | None = None
    membership_result: ParseResult[GroupMembershipRow] | None = None
    collection_errors: list[CollectionError] = field(default_factory=list)
    requested_cluster_controller_ip: str | None = None
    actual_cluster_controller_ip: str | None = None
    primary_failed: bool = False
    failover_at: datetime | None = None
    raw_outputs: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class HealthSignal:
    incident_type: IncidentType
    severity: Severity
    reason: str
    ip: str | None = None
    source: str = ""
    event_token: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DeferredIncidentState:
    """An active incident whose source cannot be trusted in this poll.

    Deferred incidents are deliberately absent from the current health signals
    (and therefore from problem IPs), while the incident manager keeps their
    existing lifecycle open until a trusted observation can confirm recovery.
    """

    incident_type: IncidentType
    ip: str | None
    event_token: str = ""


@dataclass(slots=True)
class DeviceHealth:
    ip: str
    alias: str | None = None
    hostname: str | None = None
    mm_status: str | None = None
    active_clients: int | None = None
    standby_clients: int | None = None
    load_anomaly: bool = False
    load_anomaly_streak: int = 0
    connection_type: str | None = None
    previous_connection_type: str | None = None
    connection_type_changed: bool = False
    membership_present: bool | None = None
    load_present: bool | None = None
    mm_present: bool | None = None
    last_seen: datetime | None = None
    collection_errors: list[CollectionError] = field(default_factory=list)
    issue_reasons: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    signals: list[HealthSignal] = field(default_factory=list)
    severity: Severity = Severity.UNKNOWN

    @property
    def display_name(self) -> str:
        return self.alias or self.hostname or self.ip


@dataclass(slots=True)
class OverallHealth:
    checked_at: datetime
    severity: Severity
    devices: list[DeviceHealth]
    problem_ips: list[str] = field(default_factory=list)
    primary_problem_ip: str | None = None
    summary: str = ""
    signals: list[HealthSignal] = field(default_factory=list)
    deferred_incidents: list[DeferredIncidentState] = field(default_factory=list)
    collection_errors: list[CollectionError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    partial: bool = False
    requested_cluster_controller_ip: str | None = None
    actual_cluster_controller_ip: str | None = None
    primary_failed: bool = False
    failover_at: datetime | None = None

    def device_by_ip(self, ip: str) -> DeviceHealth | None:
        return next((device for device in self.devices if device.ip == ip), None)


@dataclass(slots=True, frozen=True)
class ConnectionBaseline:
    collector_ip: str
    member_ip: str
    display_value: str
    normalized_value: str
    observed_at: datetime


@dataclass(slots=True, frozen=True)
class ConnectionChange:
    collector_ip: str
    member_ip: str
    previous_value: str
    current_value: str
    first_detected_at: datetime
    last_confirmed_at: datetime
    durable_event_token: str = field(default="", compare=False)

    @property
    def event_token(self) -> str:
        if self.durable_event_token:
            return self.durable_event_token
        stamp = self.first_detected_at.astimezone(timezone.utc).isoformat()
        return (
            f"{self.collector_ip}|{self.member_ip}|{self.previous_value}|"
            f"{self.current_value}|{stamp}"
        )


@dataclass(slots=True)
class Incident:
    incident_id: str
    incident_type: IncidentType
    severity: Severity
    reason: str
    first_detected_at: datetime
    last_seen_at: datetime
    ip: str | None = None
    alias: str | None = None
    active: bool = True
    acknowledged_at: datetime | None = None
    recovered_at: datetime | None = None
    last_notified_at: datetime | None = None
    event_token: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None


@dataclass(slots=True, frozen=True)
class IncidentTransition:
    kind: IncidentTransitionKind
    incident: Incident
    should_notify: bool
