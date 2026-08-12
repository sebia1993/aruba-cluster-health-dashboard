from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable
from uuid import uuid4

from aruba_mini_dashboard.models import (
    DeferredIncidentState,
    HealthSignal,
    Incident,
    IncidentTransition,
    IncidentTransitionKind,
    IncidentType,
    OverallHealth,
    utc_now,
)


class IncidentManager:
    """Tracks incident lifecycle and notification de-duplication in memory.

    The returned records are plain dataclasses so the SQLite layer can persist
    and restore them without the manager importing storage code.
    """

    def __init__(
        self,
        incidents: Iterable[Incident] = (),
        *,
        repeat_unacknowledged: bool = False,
        repeat_interval: timedelta = timedelta(minutes=10),
        recovery_notifications: bool = True,
    ) -> None:
        if repeat_interval <= timedelta(0):
            raise ValueError("repeat_interval must be positive")
        self.repeat_unacknowledged = repeat_unacknowledged
        self.repeat_interval = repeat_interval
        self.recovery_notifications = recovery_notifications
        self._incidents: dict[str, Incident] = {item.incident_id: item for item in incidents}
        self._active_keys: dict[tuple[IncidentType, str | None, str], str] = {}
        self._confirmed_current_keys: set[tuple[IncidentType, str | None, str]] = set()
        for item in self._incidents.values():
            if item.active:
                self._active_keys[self._key_for_incident(item)] = item.incident_id

    @staticmethod
    def _signal_key(signal: HealthSignal) -> tuple[IncidentType, str | None, str]:
        token = signal.event_token if signal.incident_type in (
            IncidentType.CONNECTION_TYPE_CHANGED,
            IncidentType.COLLECTION_FAILURE,
        ) else ""
        return signal.incident_type, signal.ip, token

    @staticmethod
    def _key_for_incident(incident: Incident) -> tuple[IncidentType, str | None, str]:
        token = incident.event_token if incident.incident_type in (
            IncidentType.CONNECTION_TYPE_CHANGED,
            IncidentType.COLLECTION_FAILURE,
        ) else ""
        return incident.incident_type, incident.ip, token

    @staticmethod
    def _key_for_deferred(
        deferred: DeferredIncidentState,
    ) -> tuple[IncidentType, str | None, str]:
        token = deferred.event_token if deferred.incident_type in (
            IncidentType.CONNECTION_TYPE_CHANGED,
            IncidentType.COLLECTION_FAILURE,
        ) else ""
        return deferred.incident_type, deferred.ip, token

    @staticmethod
    def _snapshot(incident: Incident) -> Incident:
        return replace(incident, details=dict(incident.details))

    def process(
        self,
        health: OverallHealth,
        *,
        now: datetime | None = None,
    ) -> list[IncidentTransition]:
        observed_at = now or health.checked_at
        monitoring_scope = self._monitoring_scope(health)
        aliases = {device.ip: device.display_name for device in health.devices}
        current: dict[tuple[IncidentType, str | None, str], HealthSignal] = {}
        for signal in health.signals:
            if (
                signal.incident_type is not IncidentType.COLLECTION_FAILURE
                and signal.ip is not None
                and monitoring_scope is not None
                and signal.ip not in monitoring_scope
            ):
                # Defense in depth: correlation should already suppress these,
                # but a restored/stale snapshot must never reactivate an
                # inventory-only device incident.
                continue
            key = self._signal_key(signal)
            # A single poll can surface the same global collection error through
            # several UI paths.  Preserve only one deterministic incident key.
            current.setdefault(key, signal)
        deferred_keys = {
            self._key_for_deferred(deferred)
            for deferred in health.deferred_incidents
        }
        self._confirmed_current_keys = set(current)

        transitions = (
            []
            if monitoring_scope is None
            else self.reconcile_monitoring_scope(monitoring_scope, now=observed_at)
        )
        for key, signal in current.items():
            incident_id = self._active_keys.get(key)
            if incident_id is None:
                incident = Incident(
                    incident_id=str(uuid4()),
                    incident_type=signal.incident_type,
                    severity=signal.severity,
                    reason=signal.reason,
                    first_detected_at=observed_at,
                    last_seen_at=observed_at,
                    ip=signal.ip,
                    alias=None if signal.ip is None else aliases.get(signal.ip),
                    event_token=signal.event_token,
                    details=dict(signal.details),
                )
                self._incidents[incident.incident_id] = incident
                self._active_keys[key] = incident.incident_id
                transitions.append(
                    IncidentTransition(
                        IncidentTransitionKind.ACTIVATED,
                        self._snapshot(incident),
                        True,
                    )
                )
                continue

            incident = self._incidents[incident_id]
            changed = (
                incident.reason != signal.reason
                or incident.severity is not signal.severity
                or incident.details != dict(signal.details)
            )
            incident.last_seen_at = observed_at
            incident.reason = signal.reason
            incident.severity = signal.severity
            incident.details = dict(signal.details)
            if changed:
                transitions.append(
                    IncidentTransition(
                        IncidentTransitionKind.UPDATED,
                        self._snapshot(incident),
                        False,
                    )
                )

        for key, incident_id in list(self._active_keys.items()):
            incident = self._incidents[incident_id]
            if key in current:
                continue
            if self._is_superseded_connection_event(incident, current):
                # A trusted newer event is conclusive even if an older token
                # also appears in a stale deferred-state snapshot.
                incident.active = False
                incident.last_seen_at = observed_at
                del self._active_keys[key]
                transitions.append(
                    IncidentTransition(
                        IncidentTransitionKind.SUPERSEDED,
                        self._snapshot(incident),
                        False,
                    )
                )
                continue
            if key in deferred_keys:
                # An unavailable/partial source or a deliberate low-usage
                # deferral is not evidence that the prior incident recovered.
                continue
            if not self._recovery_is_confirmed(incident, health):
                # In particular, an MM transport/parser failure or a missing
                # row is not evidence that a previously Down switch is Up.
                continue
            incident.active = False
            incident.recovered_at = observed_at
            incident.last_seen_at = observed_at
            del self._active_keys[key]
            transitions.append(
                IncidentTransition(
                    IncidentTransitionKind.RECOVERED,
                    self._snapshot(incident),
                    self.recovery_notifications,
                )
            )
        return transitions

    def reconcile_monitoring_scope(
        self,
        monitoring_scope_ips: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> list[IncidentTransition]:
        """Close device incidents removed from configuration without recovery.

        Configuration removal is an operator scope change, not evidence that
        the underlying condition recovered, so it is persisted as a silent
        ``SUPERSEDED`` transition. Source-level collection failures remain
        active because they describe monitor reachability rather than a member
        health judgment.
        """

        scope = {str(ip) for ip in monitoring_scope_ips}
        observed_at = now or utc_now()
        transitions: list[IncidentTransition] = []
        for key, incident_id in list(self._active_keys.items()):
            incident = self._incidents[incident_id]
            if (
                incident.incident_type is IncidentType.COLLECTION_FAILURE
                or incident.ip is None
                or incident.ip in scope
            ):
                continue
            incident.active = False
            incident.last_seen_at = observed_at
            del self._active_keys[key]
            transitions.append(
                IncidentTransition(
                    IncidentTransitionKind.SUPERSEDED,
                    self._snapshot(incident),
                    False,
                )
            )
        return transitions

    @staticmethod
    def _monitoring_scope(health: OverallHealth) -> set[str] | None:
        if health.monitoring_scope_ips is not None:
            return set(health.monitoring_scope_ips)
        registered = {device.ip for device in health.devices if device.is_registered}
        if registered:
            return registered
        # Legacy/external snapshots that do not declare a scope retain the
        # pre-scope lifecycle semantics: absence of a signal can confirm
        # recovery. Runtime correlation always supplies an explicit tuple.
        return None

    @staticmethod
    def _is_superseded_connection_event(
        incident: Incident,
        current: dict[tuple[IncidentType, str | None, str], HealthSignal],
    ) -> bool:
        if incident.incident_type is not IncidentType.CONNECTION_TYPE_CHANGED:
            return False
        return any(
            incident_type is IncidentType.CONNECTION_TYPE_CHANGED
            and ip == incident.ip
            and token != incident.event_token
            for incident_type, ip, token in current
        )

    @staticmethod
    def _recovery_is_confirmed(incident: Incident, health: OverallHealth) -> bool:
        if incident.incident_type is not IncidentType.MM_DOWN:
            return True
        if not incident.ip:
            return False
        device = health.device_by_ip(incident.ip)
        return bool(
            device is not None
            and device.mm_present is True
            and device.mm_status is not None
            and device.mm_status.strip().casefold() == "up"
        )

    def acknowledge(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> IncidentTransition | None:
        incident = self._incidents.get(incident_id)
        if incident is None or incident.acknowledged:
            return None
        incident.acknowledged_at = now or utc_now()
        if incident.incident_type is IncidentType.CONNECTION_TYPE_CHANGED:
            # Connection-Type changes are discrete events, not persistent
            # fault conditions. Acknowledging consumes the current event; it
            # must not later produce a misleading "recovered" transition.
            incident.active = False
            self._active_keys.pop(self._key_for_incident(incident), None)
        return IncidentTransition(
            IncidentTransitionKind.ACKNOWLEDGED,
            self._snapshot(incident),
            False,
        )

    def acknowledge_ip(
        self,
        ip: str | None,
        *,
        now: datetime | None = None,
    ) -> list[IncidentTransition]:
        transitions: list[IncidentTransition] = []
        for incident in self.active_incidents():
            if incident.ip == ip:
                transition = self.acknowledge(incident.incident_id, now=now)
                if transition is not None:
                    transitions.append(transition)
        return transitions

    def due_notifications(self, *, now: datetime | None = None) -> list[Incident]:
        check_at = now or utc_now()
        due: list[Incident] = []
        if not self.repeat_unacknowledged:
            return due
        for incident in self.active_incidents():
            if incident.acknowledged:
                continue
            if self._key_for_incident(incident) not in self._confirmed_current_keys:
                # Repeat only an incident reconfirmed by the current trusted
                # health result. Stale state must not alert during collection
                # failure or another explicitly deferred judgment.
                continue
            reference_at = incident.last_notified_at or incident.first_detected_at
            if check_at - reference_at >= self.repeat_interval:
                due.append(incident)
        return [self._snapshot(item) for item in due]

    def mark_notified(self, incident_id: str, *, now: datetime | None = None) -> bool:
        incident = self._incidents.get(incident_id)
        if incident is None:
            return False
        incident.last_notified_at = now or utc_now()
        return True

    def active_incidents(self) -> list[Incident]:
        values = [item for item in self._incidents.values() if item.active]
        values.sort(key=lambda item: (item.severity.value, item.first_detected_at, item.incident_id))
        return values

    def compact_inactive(self) -> int:
        """Release closed incident objects after their durable save succeeds.

        Active incidents remain authoritative in memory. Callers deliberately
        invoke this only after SQLite commits, so a locked database keeps the
        closed objects available for the next persistence retry.
        """

        inactive_ids = [
            incident_id
            for incident_id, incident in self._incidents.items()
            if not incident.active
        ]
        for incident_id in inactive_ids:
            self._incidents.pop(incident_id, None)
        return len(inactive_ids)

    def events(self) -> list[Incident]:
        return sorted(
            self._incidents.values(),
            key=lambda item: (item.first_detected_at, item.incident_id),
        )
