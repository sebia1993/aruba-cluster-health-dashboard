from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Mapping, Protocol

from aruba_mini_dashboard.models import (
    CollectionError,
    ConnectionBaseline,
    ConnectionChange,
    ControllerState,
    DeferredIncidentState,
    DeviceHealth,
    DistributionState,
    HealthSignal,
    IncidentType,
    OverallHealth,
    ParseResult,
    ParseStatus,
    PollCycleResult,
    Severity,
)
from aruba_mini_dashboard.parsers.common import normalize_connection_type
from aruba_mini_dashboard.services.anomaly_detector import AnomalyDetector, AnomalySettings


MM_SOURCE = "mm.show_switches"
LOAD_SOURCE = "cluster.load_distribution"
MEMBERSHIP_SOURCE = "cluster.group_membership"


class ConnectionBaselineStore(Protocol):
    def get(self, member_ip: str) -> ConnectionBaseline | None: ...

    def set(self, baseline: ConnectionBaseline) -> None: ...

    def discard(self, member_ip: str) -> None: ...

    def prune(self, expected_ips: Iterable[str]) -> set[str]: ...


class InMemoryConnectionBaselineStore:
    def __init__(self, baselines: Iterable[ConnectionBaseline] = ()) -> None:
        self._values = {item.member_ip: item for item in baselines}

    def get(self, member_ip: str) -> ConnectionBaseline | None:
        return self._values.get(member_ip)

    def set(self, baseline: ConnectionBaseline) -> None:
        self._values[baseline.member_ip] = baseline

    def discard(self, member_ip: str) -> None:
        self._values.pop(member_ip, None)

    def prune(self, expected_ips: Iterable[str]) -> set[str]:
        allowed = {str(ip) for ip in expected_ips}
        removed = set(self._values) - allowed
        for member_ip in removed:
            self.discard(member_ip)
        return removed

    def values(self) -> tuple[ConnectionBaseline, ...]:
        return tuple(self._values[member_ip] for member_ip in sorted(self._values))


def _source_matches(source: str, expected: str) -> bool:
    compact = source.casefold().replace("-", "_").replace(" ", "_")
    if expected == MM_SOURCE:
        return "show_switches" in compact or compact.startswith("mm")
    if expected == LOAD_SOURCE:
        return "load_distribution" in compact or "load" in compact
    return "group_membership" in compact or "membership" in compact


def _parser_errors(
    cycle: PollCycleResult,
    source: str,
    result: ParseResult[object] | None,
    existing: list[CollectionError],
) -> list[CollectionError]:
    if result is None:
        if any(_source_matches(error.source, source) for error in existing):
            return []
        return [
            CollectionError(
                source=source,
                code="RESULT_MISSING",
                user_message=f"{source} 명령 결과를 확인할 수 없습니다.",
                occurred_at=cycle.checked_at,
            )
        ]
    if result.status is ParseStatus.COMPLETE:
        return []
    if result.issues:
        return [
            CollectionError(
                source=source,
                code=issue.code,
                user_message=issue.message,
                technical_message=issue.snippet,
                occurred_at=cycle.checked_at,
            )
            for issue in result.issues
        ]
    return [
        CollectionError(
            source=source,
            code="PARSE_FAILED" if result.status is ParseStatus.FAILED else "PARSE_PARTIAL",
            user_message=f"{source} 명령 출력을 완전하게 해석하지 못했습니다.",
            occurred_at=cycle.checked_at,
        )
    ]


class CorrelationEngine:
    def __init__(
        self,
        settings: AnomalySettings | None = None,
        detector: AnomalyDetector | None = None,
        baseline_store: ConnectionBaselineStore | None = None,
        known_mm_devices: Mapping[str, str | None] | None = None,
        pending_connection_changes: Iterable[ConnectionChange] | None = None,
    ) -> None:
        self.detector = detector or AnomalyDetector(settings)
        self.baseline_store = baseline_store or InMemoryConnectionBaselineStore()
        self._known_mm_devices: dict[str, str | None] = dict(known_mm_devices or {})
        self._pending_connection_changes: dict[str, ConnectionChange] = {
            change.member_ip: change
            for change in (pending_connection_changes or ())
        }
        self._resolved_connection_change_members: set[str] = set()
        self._monitoring_scope_ips: tuple[str, ...] = ()
        self._restore_legacy_auto_promoted_baselines()

    def _restore_legacy_auto_promoted_baselines(self) -> None:
        """Restore the last operator-accepted value for pre-v0.6.1 state.

        Older releases moved the baseline to the observed changed value before
        the operator acknowledged the event. A durable pending event contains
        both the prior accepted value and the current candidate, so it is the
        authoritative migration source. The repaired baseline is persisted by
        the existing atomic domain-state flush.
        """

        for change in self._pending_connection_changes.values():
            baseline = self.baseline_store.get(change.member_ip)
            previous_normalized = normalize_connection_type(change.previous_value)
            current_normalized = normalize_connection_type(change.current_value)
            if baseline is None:
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=change.collector_ip,
                        member_ip=change.member_ip,
                        display_value=change.previous_value,
                        normalized_value=previous_normalized,
                        observed_at=change.first_detected_at,
                    )
                )
                continue
            if (
                baseline.normalized_value == current_normalized
                and previous_normalized != current_normalized
            ):
                self.baseline_store.set(
                    replace(
                        baseline,
                        display_value=change.previous_value,
                        normalized_value=previous_normalized,
                        observed_at=change.first_detected_at,
                    )
                )

    def dump_known_mm_devices(self) -> dict[str, str | None]:
        return dict(self._known_mm_devices)

    def forget_known_mm_devices(self, ips: Iterable[str]) -> set[str]:
        """Forget pruned informational MM inventory outside monitoring scope."""

        protected = set(self._monitoring_scope_ips)
        removed: set[str] = set()
        for raw_ip in ips:
            ip = str(raw_ip)
            if ip in protected:
                continue
            if ip in self._known_mm_devices:
                del self._known_mm_devices[ip]
                removed.add(ip)
        return removed

    def pending_connection_changes(self) -> tuple[ConnectionChange, ...]:
        return tuple(
            self._pending_connection_changes[member_ip]
            for member_ip in sorted(self._pending_connection_changes)
        )

    def monitoring_scope_ips(self) -> tuple[str, ...]:
        return self._monitoring_scope_ips

    def acknowledge_connection_change(self, ip: str, collector_ip: str | None = None) -> bool:
        """Accept the currently observed Connection-Type as the new normal."""

        change = self._pending_connection_changes.get(ip)
        if change is None or (collector_ip is not None and change.collector_ip != collector_ip):
            return False
        existing = self.baseline_store.get(ip)
        accepted_collector = (
            collector_ip
            or (existing.collector_ip if existing is not None else "")
            or change.collector_ip
        )
        self.baseline_store.set(
            ConnectionBaseline(
                collector_ip=accepted_collector,
                member_ip=ip,
                display_value=change.current_value,
                normalized_value=normalize_connection_type(change.current_value),
                observed_at=change.last_confirmed_at,
            )
        )
        del self._pending_connection_changes[ip]
        self._resolved_connection_change_members.discard(ip)
        return True

    def acknowledge_all_connection_changes(self) -> None:
        for member_ip in tuple(self._pending_connection_changes):
            self.acknowledge_connection_change(member_ip)

    def drain_connection_change_resolutions(self) -> set[str]:
        """Return members whose unaccepted change returned to the baseline."""

        resolved = set(self._resolved_connection_change_members)
        self._resolved_connection_change_members.clear()
        return resolved

    def reconcile_monitoring_scope(self, expected_ips: Iterable[str]) -> set[str]:
        """Drop health state for IPs outside the authoritative configured scope.

        MM discovery remains independent so removed members can still appear as
        informational inventory rows in the expanded dashboard.
        """

        ordered = tuple(dict.fromkeys(str(ip) for ip in expected_ips))
        allowed = set(ordered)
        removed = self.detector.prune_ips(ordered)
        for member_ip in list(self._pending_connection_changes):
            if member_ip not in allowed:
                removed.add(member_ip)
                del self._pending_connection_changes[member_ip]
                self._resolved_connection_change_members.discard(member_ip)

        prune = getattr(self.baseline_store, "prune", None)
        if callable(prune):
            removed.update(prune(ordered))
        else:
            discard = getattr(self.baseline_store, "discard", None)
            if callable(discard):
                for member_ip in removed:
                    discard(member_ip)

        # A removed IP may have only an incident or a pending change, without a
        # baseline/counter. Mark every known removal for durable cleanup.
        discard = getattr(self.baseline_store, "discard", None)
        if callable(discard):
            for member_ip in removed:
                discard(member_ip)
        self._monitoring_scope_ips = ordered
        return removed

    def correlate(self, cycle: PollCycleResult) -> OverallHealth:
        expected_aliases = dict(cycle.expected_cluster_members)
        monitoring_scope_ips = tuple(expected_aliases)
        self.reconcile_monitoring_scope(monitoring_scope_ips)
        errors = list(cycle.collection_errors)
        errors.extend(_parser_errors(cycle, MM_SOURCE, cycle.mm_result, errors))
        errors.extend(_parser_errors(cycle, LOAD_SOURCE, cycle.load_result, errors))
        errors.extend(_parser_errors(cycle, MEMBERSHIP_SOURCE, cycle.membership_result, errors))

        mm_rows = [] if cycle.mm_result is None else list(cycle.mm_result.rows)
        load_rows = [] if cycle.load_result is None else list(cycle.load_result.rows)
        membership_rows = [] if cycle.membership_result is None else list(cycle.membership_result.rows)
        mm_by_ip = {row.ip: row for row in mm_rows}
        load_by_ip = {row.ip: row for row in load_rows}
        membership_by_ip = {row.ip: row for row in membership_rows}

        for row in mm_rows:
            self._known_mm_devices[row.ip] = row.hostname or self._known_mm_devices.get(row.ip)

        all_ips = set(expected_aliases)
        all_ips.update(self._known_mm_devices)
        all_ips.update(mm_by_ip)
        all_ips.update(load_by_ip)
        all_ips.update(membership_by_ip)
        all_ips.update(change.member_ip for change in self._pending_connection_changes.values())
        devices = {
            ip: DeviceHealth(
                ip=ip,
                alias=expected_aliases.get(ip),
                hostname=(mm_by_ip[ip].hostname if ip in mm_by_ip else self._known_mm_devices.get(ip)),
                is_registered=ip in expected_aliases,
            )
            for ip in sorted(all_ips)
        }

        deferred_incidents: list[DeferredIncidentState] = []
        self._apply_mm(cycle, devices, mm_by_ip, deferred_incidents)
        low_usage = self._apply_load(cycle, devices, load_by_ip, deferred_incidents)
        self._apply_membership(cycle, devices, membership_by_ip, deferred_incidents)
        for device in devices.values():
            for error in device.collection_errors:
                if error not in errors:
                    errors.append(error)
        self._attach_errors(devices, errors, expected_aliases)
        self._finalize_device_severity(devices.values())

        ordered_devices = sorted(devices.values(), key=lambda item: item.ip)
        problem_devices = [
            item for item in ordered_devices if item.severity in (Severity.CRITICAL, Severity.WARNING)
        ]
        problem_devices.sort(key=self._problem_sort_key)
        problem_ips = [item.ip for item in problem_devices]

        if any(item.severity is Severity.CRITICAL for item in ordered_devices):
            severity = Severity.CRITICAL
        elif any(item.severity is Severity.WARNING for item in ordered_devices):
            severity = Severity.WARNING
        elif errors or any(item.severity is Severity.UNKNOWN for item in ordered_devices):
            severity = Severity.UNKNOWN
        else:
            severity = Severity.NORMAL

        notes: list[str] = []
        if low_usage:
            notes.append("낮은 전체 사용량: Client 분배 장애 판단을 보류했습니다.")
        if (
            cycle.primary_failed
            and cycle.actual_cluster_controller_ip
            and cycle.actual_cluster_controller_ip != cycle.requested_cluster_controller_ip
        ):
            notes.append(
                f"Primary 수집 실패 후 {cycle.actual_cluster_controller_ip}에서 수집했습니다."
            )
        summary = self._summary(severity, problem_devices)
        signals = [signal for device in ordered_devices for signal in device.signals]
        grouped_errors: dict[tuple[str, str, str], list[CollectionError]] = {}
        for error in errors:
            grouped_errors.setdefault(
                (error.target_ip or "", error.code, error.user_message),
                [],
            ).append(error)
        signals.extend(
            HealthSignal(
                incident_type=IncidentType.COLLECTION_FAILURE,
                severity=Severity.UNKNOWN,
                reason=group[0].user_message,
                ip=group[0].target_ip,
                source=", ".join(sorted({item.source for item in group})),
                event_token=f"collection|{group[0].code}|{group[0].target_ip or ''}",
                details={
                    "code": group[0].code,
                    "sources": sorted({item.source for item in group}),
                },
            )
            for group in grouped_errors.values()
        )
        return OverallHealth(
            checked_at=cycle.checked_at,
            severity=severity,
            devices=ordered_devices,
            monitoring_scope_ips=monitoring_scope_ips,
            problem_ips=problem_ips,
            primary_problem_ip=problem_ips[0] if problem_ips else None,
            summary=summary,
            signals=signals,
            deferred_incidents=deferred_incidents,
            collection_errors=errors,
            notes=notes,
            partial=bool(errors),
            requested_cluster_controller_ip=cycle.requested_cluster_controller_ip,
            actual_cluster_controller_ip=cycle.actual_cluster_controller_ip,
            primary_failed=cycle.primary_failed,
            failover_at=cycle.failover_at,
        )

    def _apply_mm(
        self,
        cycle: PollCycleResult,
        devices: dict[str, DeviceHealth],
        mm_by_ip: dict,
        deferred_incidents: list[DeferredIncidentState],
    ) -> None:
        complete = cycle.mm_result is not None and cycle.mm_result.status is ParseStatus.COMPLETE
        expected_mm = tuple(cycle.expected_cluster_members)
        if not complete:
            for ip in expected_mm:
                self._defer(deferred_incidents, IncidentType.MM_DOWN, ip)
                self._defer(deferred_incidents, IncidentType.MM_MEMBER_MISSING, ip)
        missing = self.detector.evaluate_missing(
            "mm",
            mm_by_ip,
            expected_mm,
            data_complete=complete,
        )
        for ip in expected_mm:
            device = devices[ip]
            evaluation = missing[ip]
            device.mm_present = evaluation.present
            if evaluation.present is False:
                device.controller_state = ControllerState.MISSING
            elif evaluation.present is None:
                device.controller_state = ControllerState.UNKNOWN
            if evaluation.active and evaluation.deferred:
                self._defer(deferred_incidents, IncidentType.MM_MEMBER_MISSING, ip)
            elif evaluation.active:
                self._add_signal(
                    device,
                    IncidentType.MM_MEMBER_MISSING,
                    Severity.WARNING,
                    f"MM show switches 행 누락이 {evaluation.missing_streak}회 연속 감지됨",
                    MM_SOURCE,
                    details={"streak": evaluation.missing_streak},
                )
            elif evaluation.present is False:
                device.observations.append(
                    f"MM 행 누락 감지 {evaluation.missing_streak}/{self.detector.settings.missing_confirmations}회"
                )

        # Discovered but unregistered MM rows remain visible inventory. Their
        # absence is informational only and never advances a missing streak.
        for ip, device in devices.items():
            if device.is_registered or ip in mm_by_ip:
                continue
            device.mm_present = False if complete else None
            device.controller_state = (
                ControllerState.MISSING if complete else ControllerState.UNKNOWN
            )
        for ip, row in mm_by_ip.items():
            device = devices[ip]
            device.mm_present = True
            device.mm_status = row.status
            device.hostname = row.hostname or device.hostname
            device.last_seen = cycle.checked_at
            normalized_status = row.status.strip().casefold()
            if normalized_status == "up":
                device.controller_state = ControllerState.UP
            elif normalized_status == "down":
                device.controller_state = ControllerState.DOWN
            else:
                device.controller_state = ControllerState.UNKNOWN

            if not device.is_registered:
                continue
            if normalized_status == "down":
                self._add_signal(
                    device,
                    IncidentType.MM_DOWN,
                    Severity.CRITICAL,
                    "MM show switches Status = Down",
                    MM_SOURCE,
                )
            elif normalized_status != "up":
                device.collection_errors.append(
                    CollectionError(
                        source=MM_SOURCE,
                        code="MM_STATUS_UNRECOGNIZED",
                        user_message=f"MM Status 값 '{row.status}'의 의미를 확인할 수 없습니다.",
                        target_ip=ip,
                        occurred_at=cycle.checked_at,
                    )
                )

    def _apply_load(
        self,
        cycle: PollCycleResult,
        devices: dict[str, DeviceHealth],
        load_by_ip: dict,
        deferred_incidents: list[DeferredIncidentState],
    ) -> bool:
        expected = tuple(cycle.expected_cluster_members)
        complete = cycle.load_result is not None and cycle.load_result.status is ParseStatus.COMPLETE
        total_conflict = bool(
            cycle.load_result is not None
            and cycle.load_result.metadata.get("total_active_conflict", False)
        )
        total_active: int | None = None
        if cycle.load_result is not None:
            value = cycle.load_result.metadata.get("total_active")
            if isinstance(value, int):
                total_active = value
        if complete and all(ip in load_by_ip for ip in expected):
            # Detection is scoped to configured members. A parser-reported
            # cluster total may include unregistered rows discovered in the
            # same command output and must not influence monitored-member
            # low-usage suppression.
            total_active = sum(load_by_ip[ip].active_clients for ip in expected)
        if not complete:
            for ip in expected:
                self._defer(deferred_incidents, IncidentType.LOAD_MEMBER_MISSING, ip)
                self._defer(deferred_incidents, IncidentType.CLIENT_DISTRIBUTION, ip)
        elif total_conflict:
            for ip in expected:
                self._defer(deferred_incidents, IncidentType.CLIENT_DISTRIBUTION, ip)
        missing = self.detector.evaluate_missing(
            "load",
            load_by_ip,
            expected,
            data_complete=complete,
        )
        evaluations = self.detector.evaluate_client_distribution(
            load_by_ip.values(),
            expected,
            data_complete=complete and not total_conflict,
            total_active=total_active,
        )
        low_usage = any(value.low_usage for value in evaluations.values())
        if low_usage:
            for ip in expected:
                self._defer(deferred_incidents, IncidentType.CLIENT_DISTRIBUTION, ip)
        for ip in expected:
            device = devices[ip]
            missing_result = missing[ip]
            device.load_present = missing_result.present
            if not complete:
                device.distribution_state = DistributionState.UNKNOWN
            elif missing_result.present is False:
                device.distribution_state = (
                    DistributionState.MISSING
                    if missing_result.active
                    else DistributionState.OBSERVING
                )
            if missing_result.active and missing_result.deferred:
                self._defer(deferred_incidents, IncidentType.LOAD_MEMBER_MISSING, ip)
            elif missing_result.active:
                self._add_signal(
                    device,
                    IncidentType.LOAD_MEMBER_MISSING,
                    Severity.WARNING,
                    f"Client 분배 출력 행 누락이 {missing_result.missing_streak}회 연속 감지됨",
                    LOAD_SOURCE,
                    details={"streak": missing_result.missing_streak},
                )
            elif missing_result.present is False:
                device.observations.append(
                    f"Client 분배 행 누락 {missing_result.missing_streak}/{self.detector.settings.missing_confirmations}회"
                )

            evaluation = evaluations[ip]
            device.load_anomaly = evaluation.active and not evaluation.deferred
            device.load_anomaly_streak = evaluation.anomaly_streak
            if missing_result.present is not False:
                if evaluation.low_usage:
                    device.distribution_state = DistributionState.LOW_USAGE
                elif evaluation.deferred:
                    device.distribution_state = DistributionState.UNKNOWN
                elif evaluation.active and evaluation.condition_met:
                    device.distribution_state = DistributionState.ANOMALOUS
                elif evaluation.active and evaluation.recovery_streak:
                    device.distribution_state = DistributionState.RECOVERING
                elif evaluation.condition_met:
                    device.distribution_state = DistributionState.OBSERVING
                else:
                    device.distribution_state = DistributionState.NORMAL
            if evaluation.active and evaluation.deferred:
                self._defer(deferred_incidents, IncidentType.CLIENT_DISTRIBUTION, ip)
                device.observations.append(evaluation.reason)
            elif evaluation.active:
                self._add_signal(
                    device,
                    IncidentType.CLIENT_DISTRIBUTION,
                    Severity.WARNING,
                    (
                        f"Client 분배 이상: Active {evaluation.active_clients} / "
                        f"Standby {evaluation.standby_clients}, 연속 {evaluation.anomaly_streak}회"
                    ),
                    LOAD_SOURCE,
                    details={
                        "active_clients": evaluation.active_clients,
                        "standby_clients": evaluation.standby_clients,
                        "streak": evaluation.anomaly_streak,
                    },
                )
        for ip, row in load_by_ip.items():
            device = devices[ip]
            device.load_present = True
            device.active_clients = row.active_clients
            device.standby_clients = row.standby_clients
            device.last_seen = cycle.checked_at
        return low_usage

    def _apply_membership(
        self,
        cycle: PollCycleResult,
        devices: dict[str, DeviceHealth],
        membership_by_ip: dict,
        deferred_incidents: list[DeferredIncidentState],
    ) -> None:
        expected = tuple(cycle.expected_cluster_members)
        complete = cycle.membership_result is not None and (
            cycle.membership_result.status is ParseStatus.COMPLETE
        )
        if not complete:
            for ip in expected:
                self._defer(
                    deferred_incidents,
                    IncidentType.MEMBERSHIP_MEMBER_MISSING,
                    ip,
                )
        missing = self.detector.evaluate_missing(
            "membership",
            membership_by_ip,
            expected,
            data_complete=complete,
        )
        for ip in expected:
            device = devices[ip]
            result = missing[ip]
            device.membership_present = result.present
            if result.active and result.deferred:
                self._defer(
                    deferred_incidents,
                    IncidentType.MEMBERSHIP_MEMBER_MISSING,
                    ip,
                )
            elif result.active:
                self._add_signal(
                    device,
                    IncidentType.MEMBERSHIP_MEMBER_MISSING,
                    Severity.WARNING,
                    f"Membership 행 누락이 {result.missing_streak}회 연속 감지됨",
                    MEMBERSHIP_SOURCE,
                    details={"streak": result.missing_streak},
                )
            elif result.present is False:
                device.observations.append(
                    f"Membership 행 누락 {result.missing_streak}/{self.detector.settings.missing_confirmations}회"
                )

        collector_ip = cycle.actual_cluster_controller_ip
        for ip, row in membership_by_ip.items():
            device = devices[ip]
            device.membership_present = True
            device.connection_type = row.connection_type
            device.last_seen = cycle.checked_at
            if not device.is_registered or not complete or not collector_ip:
                continue
            normalized = normalize_connection_type(row.connection_type)
            baseline = self.baseline_store.get(ip)
            if baseline is None:
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=collector_ip,
                        member_ip=ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
                continue

            pending = self._pending_connection_changes.get(ip)
            if baseline.normalized_value == normalized:
                # Refresh harmless formatting/source metadata without changing
                # the accepted semantic value. Returning to the accepted value
                # is trusted recovery of an unacknowledged change.
                self.baseline_store.set(
                    replace(
                        baseline,
                        collector_ip=collector_ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
                if pending is not None:
                    del self._pending_connection_changes[ip]
                    self._resolved_connection_change_members.add(ip)
                continue

            # Keep the accepted baseline value stable. Only source metadata is
            # refreshed while the operator decides whether the new value is
            # normal. A third value supersedes the prior pending event.
            self.baseline_store.set(
                replace(
                    baseline,
                    collector_ip=collector_ip,
                )
            )
            if (
                pending is not None
                and normalize_connection_type(pending.current_value) == normalized
            ):
                self._pending_connection_changes[ip] = replace(
                    pending,
                    last_confirmed_at=cycle.checked_at,
                )
                continue
            self._pending_connection_changes[ip] = ConnectionChange(
                collector_ip=collector_ip,
                member_ip=ip,
                previous_value=baseline.display_value,
                current_value=row.connection_type,
                first_detected_at=cycle.checked_at,
                last_confirmed_at=cycle.checked_at,
            )

        for change in self._pending_connection_changes.values():
            ip = change.member_ip
            if ip not in devices:
                devices[ip] = DeviceHealth(ip=ip, is_registered=ip in cycle.expected_cluster_members)
            device = devices[ip]
            if not device.is_registered:
                continue
            row = membership_by_ip.get(ip)
            change_is_current = bool(
                complete
                and row is not None
                and normalize_connection_type(row.connection_type)
                == normalize_connection_type(change.current_value)
            )
            if not change_is_current:
                self._defer(
                    deferred_incidents,
                    IncidentType.CONNECTION_TYPE_CHANGED,
                    ip,
                    change.event_token,
                )
                continue
            device.previous_connection_type = change.previous_value
            device.connection_type_changed = True
            if device.connection_type is None:
                device.connection_type = change.current_value
            self._add_signal(
                device,
                IncidentType.CONNECTION_TYPE_CHANGED,
                Severity.WARNING,
                f"Connection-Type 변경: {change.previous_value} → {change.current_value}",
                MEMBERSHIP_SOURCE,
                event_token=change.event_token,
                details={
                    "collector_ip": change.collector_ip,
                    "previous": change.previous_value,
                    "current": change.current_value,
                    "first_detected_at": change.first_detected_at.isoformat(),
                    "last_confirmed_at": change.last_confirmed_at.isoformat(),
                },
            )

    @staticmethod
    def _defer(
        deferred_incidents: list[DeferredIncidentState],
        incident_type: IncidentType,
        ip: str | None,
        event_token: str = "",
    ) -> None:
        deferred = DeferredIncidentState(incident_type, ip, event_token)
        if deferred not in deferred_incidents:
            deferred_incidents.append(deferred)

    @staticmethod
    def _add_signal(
        device: DeviceHealth,
        incident_type: IncidentType,
        severity: Severity,
        reason: str,
        source: str,
        *,
        event_token: str = "",
        details: Mapping[str, object] | None = None,
    ) -> None:
        signal = HealthSignal(
            incident_type=incident_type,
            severity=severity,
            reason=reason,
            ip=device.ip,
            source=source,
            event_token=event_token,
            details={} if details is None else dict(details),
        )
        device.signals.append(signal)
        device.issue_reasons.append(reason)

    @staticmethod
    def _attach_errors(
        devices: dict[str, DeviceHealth],
        errors: Iterable[CollectionError],
        expected_aliases: Mapping[str, str],
    ) -> None:
        for error in errors:
            # Errors created for one parsed member are already attached by the
            # rule that discovered them. A transport or command error's target
            # is the collector endpoint; it makes every device in that source
            # unknown and must not be mistaken for a failed member.
            existing_targets = [
                ip for ip, device in devices.items() if error in device.collection_errors
            ]
            if existing_targets:
                targets = existing_targets
            elif _source_matches(error.source, MM_SOURCE):
                targets = list(devices)
            elif _source_matches(error.source, LOAD_SOURCE) or _source_matches(
                error.source, MEMBERSHIP_SOURCE
            ):
                targets = list(expected_aliases)
            elif error.target_ip is not None:
                targets = [error.target_ip]
            else:
                targets = []
            for ip in targets:
                if ip in devices and error not in devices[ip].collection_errors:
                    devices[ip].collection_errors.append(error)

    @staticmethod
    def _finalize_device_severity(devices: Iterable[DeviceHealth]) -> None:
        for device in devices:
            if not device.is_registered:
                # Inventory-only rows may carry raw observed values and source
                # errors, but they never participate in monitored health.
                device.signals.clear()
                device.issue_reasons.clear()
                device.severity = Severity.NORMAL
                continue
            abnormal = [
                signal
                for signal in device.signals
                if signal.severity in (Severity.CRITICAL, Severity.WARNING)
            ]
            types = {signal.incident_type for signal in abnormal}
            if IncidentType.MM_DOWN in types or len(types) >= 2:
                device.severity = Severity.CRITICAL
            elif types:
                device.severity = Severity.WARNING
            elif device.collection_errors:
                device.severity = Severity.UNKNOWN
            else:
                device.severity = Severity.NORMAL

    @staticmethod
    def _problem_sort_key(device: DeviceHealth) -> tuple[int, str]:
        types = {signal.incident_type for signal in device.signals}
        if IncidentType.MM_DOWN in types:
            rank = 1
        elif len(types) >= 2:
            rank = 2
        elif IncidentType.CLIENT_DISTRIBUTION in types:
            rank = 3
        elif IncidentType.CONNECTION_TYPE_CHANGED in types:
            rank = 4
        else:
            rank = 5
        return rank, device.ip

    @staticmethod
    def _summary(severity: Severity, problem_devices: list[DeviceHealth]) -> str:
        if not problem_devices:
            if severity is Severity.UNKNOWN:
                return "최종 판단: 확인 불가 (수집 또는 파싱 오류)"
            return "상태: 정상 / 문제 IP: 없음"
        if len(problem_devices) == 1:
            device = problem_devices[0]
            return f"상태: {('장애' if device.severity is Severity.CRITICAL else '주의')} / 주요 문제 IP: {device.ip}"
        return f"문제 IP {len(problem_devices)}개 감지: " + ", ".join(
            device.ip for device in problem_devices
        )
