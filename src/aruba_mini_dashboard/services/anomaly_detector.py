from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Iterable, Mapping

from aruba_mini_dashboard.models import ClientDistributionRow, DetectionMode


@dataclass(slots=True, frozen=True)
class AnomalySettings:
    low_client_threshold: int = 10
    anomaly_confirmations: int = 3
    recovery_confirmations: int = 2
    relative_ratio: float = 0.25
    cluster_min_total_active: int = 50
    peer_minimum: int = 30
    detection_mode: DetectionMode = DetectionMode.ABSOLUTE_AND_RELATIVE
    missing_confirmations: int = 3
    missing_recovery_confirmations: int = 2

    def __post_init__(self) -> None:
        if self.low_client_threshold < 0:
            raise ValueError("low_client_threshold must be non-negative")
        if self.anomaly_confirmations < 1 or self.recovery_confirmations < 1:
            raise ValueError("confirmation counts must be at least one")
        if not 0 < self.relative_ratio <= 1:
            raise ValueError("relative_ratio must be greater than zero and at most one")
        if self.cluster_min_total_active < 0 or self.peer_minimum < 0:
            raise ValueError("cluster and peer minimums must be non-negative")
        if self.missing_confirmations < 1 or self.missing_recovery_confirmations < 1:
            raise ValueError("missing confirmation counts must be at least one")


@dataclass(slots=True)
class DetectorCounter:
    anomaly_streak: int = 0
    recovery_streak: int = 0
    active: bool = False


@dataclass(slots=True, frozen=True)
class ClientAnomalyEvaluation:
    ip: str
    active: bool
    condition_met: bool
    activated: bool
    recovered: bool
    anomaly_streak: int
    recovery_streak: int
    active_clients: int | None
    standby_clients: int | None
    peer_active_median: float | None = None
    peer_standby_median: float | None = None
    low_usage: bool = False
    deferred: bool = False
    reason: str = ""


@dataclass(slots=True, frozen=True)
class MissingEvaluation:
    source: str
    ip: str
    present: bool | None
    active: bool
    activated: bool
    recovered: bool
    missing_streak: int
    recovery_streak: int
    deferred: bool = False


class AnomalyDetector:
    """Stateful, deterministic debounce logic with serializable counters."""

    def __init__(
        self,
        settings: AnomalySettings | None = None,
        state: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.settings = settings or AnomalySettings()
        self._counters: dict[str, DetectorCounter] = {}
        if state:
            for key, value in state.items():
                self._counters[str(key)] = DetectorCounter(
                    anomaly_streak=max(0, int(value.get("anomaly_streak", 0))),
                    recovery_streak=max(0, int(value.get("recovery_streak", 0))),
                    active=bool(value.get("active", False)),
                )

    def dump_state(self) -> dict[str, dict[str, object]]:
        return {key: asdict(value) for key, value in sorted(self._counters.items())}

    def prune_ips(self, expected_ips: Iterable[str]) -> set[str]:
        """Forget debounce state for members that are no longer monitored.

        Persisted counters must not reactivate when an operator removes a
        controller and later adds it again.  Returning the removed IPs lets the
        runtime prune the corresponding durable rows in the same transaction
        as incident lifecycle updates.
        """

        allowed = {str(ip) for ip in expected_ips}
        removed: set[str] = set()
        for key in list(self._counters):
            _category, separator, ip = key.rpartition("|")
            if separator and ip not in allowed:
                removed.add(ip)
                del self._counters[key]
        return removed

    def _counter(self, category: str, ip: str) -> DetectorCounter:
        return self._counters.setdefault(f"{category}|{ip}", DetectorCounter())

    @staticmethod
    def _advance(
        counter: DetectorCounter,
        condition: bool,
        anomaly_confirmations: int,
        recovery_confirmations: int,
    ) -> tuple[bool, bool]:
        activated = False
        recovered = False
        if condition:
            counter.recovery_streak = 0
            counter.anomaly_streak += 1
            if not counter.active and counter.anomaly_streak >= anomaly_confirmations:
                counter.active = True
                activated = True
        elif counter.active:
            counter.recovery_streak += 1
            if counter.recovery_streak >= recovery_confirmations:
                counter.active = False
                counter.anomaly_streak = 0
                counter.recovery_streak = 0
                recovered = True
        else:
            counter.anomaly_streak = 0
            counter.recovery_streak = 0
        return activated, recovered

    def evaluate_client_distribution(
        self,
        rows: Iterable[ClientDistributionRow],
        expected_ips: Iterable[str],
        *,
        data_complete: bool,
        total_active: int | None = None,
    ) -> dict[str, ClientAnomalyEvaluation]:
        expected = tuple(dict.fromkeys(str(ip) for ip in expected_ips))
        row_map = {row.ip: row for row in rows}
        complete = data_complete and all(ip in row_map for ip in expected)
        if total_active is None and complete:
            total_active = sum(row_map[ip].active_clients for ip in expected)
        low_usage = complete and total_active is not None and (
            total_active < self.settings.cluster_min_total_active
        )

        evaluations: dict[str, ClientAnomalyEvaluation] = {}
        for ip in expected:
            counter = self._counter("load", ip)
            row = row_map.get(ip)
            if not complete:
                # A failed/untrusted collection breaks a not-yet-confirmed
                # consecutive anomaly sequence. Once active, however, freeze
                # both counters because missing evidence cannot prove recovery.
                if counter.active:
                    counter.recovery_streak = 0
                else:
                    counter.anomaly_streak = 0
                    counter.recovery_streak = 0
                evaluations[ip] = ClientAnomalyEvaluation(
                    ip=ip,
                    active=counter.active,
                    condition_met=False,
                    activated=False,
                    recovered=False,
                    anomaly_streak=counter.anomaly_streak,
                    recovery_streak=counter.recovery_streak,
                    active_clients=None if row is None else row.active_clients,
                    standby_clients=None if row is None else row.standby_clients,
                    deferred=True,
                    reason="수집 데이터가 불완전하여 Client 분배 판단을 보류했습니다.",
                )
                continue

            assert row is not None
            peer_rows = [row_map[peer] for peer in expected if peer != ip]
            peer_active = float(median(item.active_clients for item in peer_rows)) if peer_rows else None
            peer_standby = float(median(item.standby_clients for item in peer_rows)) if peer_rows else None

            if low_usage:
                # Low overall utilization contains no evidence of a specific
                # failed member.  Do not let it recover an already-active event.
                if counter.active:
                    counter.recovery_streak = 0
                else:
                    counter.anomaly_streak = 0
                    counter.recovery_streak = 0
                evaluations[ip] = ClientAnomalyEvaluation(
                    ip=ip,
                    active=counter.active,
                    condition_met=False,
                    activated=False,
                    recovered=False,
                    anomaly_streak=counter.anomaly_streak,
                    recovery_streak=counter.recovery_streak,
                    active_clients=row.active_clients,
                    standby_clients=row.standby_clients,
                    peer_active_median=peer_active,
                    peer_standby_median=peer_standby,
                    low_usage=True,
                    deferred=True,
                    reason="전체 Client 사용량이 낮아 특정 구성원의 이상 판단을 보류했습니다.",
                )
                continue

            absolute_low = (
                row.active_clients <= self.settings.low_client_threshold
                and row.standby_clients <= self.settings.low_client_threshold
            )
            relative_low = True
            if self.settings.detection_mode is DetectionMode.ABSOLUTE_AND_RELATIVE:
                relative_low = (
                    peer_active is not None
                    and peer_standby is not None
                    and peer_active >= self.settings.peer_minimum
                    and peer_standby >= self.settings.peer_minimum
                    and row.active_clients <= peer_active * self.settings.relative_ratio
                    and row.standby_clients <= peer_standby * self.settings.relative_ratio
                )
            condition = absolute_low and relative_low
            activated, recovered = self._advance(
                counter,
                condition,
                self.settings.anomaly_confirmations,
                self.settings.recovery_confirmations,
            )
            evaluations[ip] = ClientAnomalyEvaluation(
                ip=ip,
                active=counter.active,
                condition_met=condition,
                activated=activated,
                recovered=recovered,
                anomaly_streak=counter.anomaly_streak,
                recovery_streak=counter.recovery_streak,
                active_clients=row.active_clients,
                standby_clients=row.standby_clients,
                peer_active_median=peer_active,
                peer_standby_median=peer_standby,
                reason=(
                    "Active 및 Standby Client 수가 설정된 이상 조건을 충족합니다."
                    if condition
                    else "Client 수가 설정된 이상 조건을 충족하지 않습니다."
                ),
            )
        return evaluations

    def evaluate_missing(
        self,
        source: str,
        present_ips: Iterable[str],
        expected_ips: Iterable[str],
        *,
        data_complete: bool,
    ) -> dict[str, MissingEvaluation]:
        present_set = set(present_ips)
        evaluations: dict[str, MissingEvaluation] = {}
        for ip in dict.fromkeys(str(value) for value in expected_ips):
            counter = self._counter(f"missing:{source}", ip)
            if not data_complete:
                # Missing-row confirmation is consecutive trusted evidence.
                # An untrusted poll breaks a pending sequence, while an active
                # incident remains frozen until trusted rows can recover it.
                if counter.active:
                    counter.recovery_streak = 0
                else:
                    counter.anomaly_streak = 0
                    counter.recovery_streak = 0
                evaluations[ip] = MissingEvaluation(
                    source=source,
                    ip=ip,
                    present=None,
                    active=counter.active,
                    activated=False,
                    recovered=False,
                    missing_streak=counter.anomaly_streak,
                    recovery_streak=counter.recovery_streak,
                    deferred=True,
                )
                continue
            present = ip in present_set
            activated, recovered = self._advance(
                counter,
                not present,
                self.settings.missing_confirmations,
                self.settings.missing_recovery_confirmations,
            )
            evaluations[ip] = MissingEvaluation(
                source=source,
                ip=ip,
                present=present,
                active=counter.active,
                activated=activated,
                recovered=recovered,
                missing_streak=counter.anomaly_streak,
                recovery_streak=counter.recovery_streak,
            )
        return evaluations
