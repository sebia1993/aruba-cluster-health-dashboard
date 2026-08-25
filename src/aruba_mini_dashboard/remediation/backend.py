from __future__ import annotations

import copy
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aruba_mini_dashboard.collectors.base import (
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    SHOW_SWITCHES,
    SshConnectionOptions,
)
from aruba_mini_dashboard.collectors.cluster_collector import ClusterCollector
from aruba_mini_dashboard.collectors.mm_collector import MmCollector
from aruba_mini_dashboard.credentials import CredentialService, DeviceCredential
from aruba_mini_dashboard.models import ParseStatus
from aruba_mini_dashboard.parsers import (
    parse_group_membership,
    parse_load_distribution,
    parse_show_switches,
)

from .models import (
    ClusterMemberObservation,
    ClusterObservation,
    MmObservation,
)
from .ssh_actions import ArubaActionSshAdapter


_LEADER = re.compile(r"\(\s*leader\s*\)", re.I)


class RemediationBackend:
    """Existing read-only collectors plus a separately allow-listed action session."""

    def __init__(
        self,
        app_settings: Any,
        credential_service: CredentialService,
        *,
        known_hosts_path: str | Path,
        cancel_event: threading.Event | None = None,
        action_adapter_factory: Callable[..., ArubaActionSshAdapter] | None = None,
    ) -> None:
        self.settings = copy.deepcopy(app_settings)
        self.credential_service = credential_service
        self.known_hosts_path = Path(known_hosts_path)
        self.cancel_event = cancel_event
        self.action_adapter_factory = action_adapter_factory
        self._active_lock = threading.RLock()
        self._active_action_adapters: set[ArubaActionSshAdapter] = set()

    @property
    def expected_member_ips(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(member.ip).strip()
                for member in self.settings.cluster.members
                if str(member.ip).strip()
            )
        )

    def _credential(self, role: str) -> DeviceCredential:
        credential_id = self.settings.credentials.effective_id(role, self.settings)
        if not credential_id:
            raise RuntimeError(f"{role.upper()} 장비 자격 증명이 설정되지 않았습니다.")
        return self.credential_service.get(credential_id)

    def collect_mm(self) -> MmObservation:
        settings = copy.deepcopy(self.settings.mobility_master)
        collector = MmCollector(
            known_hosts_path=self.known_hosts_path,
            cancel_event=self.cancel_event,
        )
        bundle = collector.collect(settings, self._credential("mm"))
        command = bundle.commands.get(SHOW_SWITCHES)
        source_ip = bundle.actual_controller_ip or bundle.requested_controller_ip
        if command is None or not command.success:
            return MmObservation(
                collected_at=bundle.collected_at,
                states={},
                complete=False,
                source_ip=source_ip,
                error_code=(bundle.terminal_error_code or ("COMMAND_NOT_RUN" if command is None else command.error_code)),
                error_message=(bundle.terminal_error_message or ("MM 상태 명령을 실행하지 못했습니다." if command is None else command.error_message)),
            )
        parsed = parse_show_switches(command.output, cancel_event=self.cancel_event)
        states = {str(row.ip): str(row.status) for row in parsed.rows}
        hostnames = {str(row.ip): row.hostname for row in parsed.rows}
        issue = parsed.issues[0] if parsed.issues else None
        return MmObservation(
            collected_at=bundle.collected_at,
            states=states,
            hostnames=hostnames,
            complete=parsed.status is ParseStatus.COMPLETE,
            source_ip=source_ip,
            error_code="" if issue is None else issue.code,
            error_message="" if issue is None else issue.message,
        )

    def collect_cluster(self, controller_ip: str | None = None) -> ClusterObservation:
        settings = copy.deepcopy(self.settings.cluster)
        if controller_ip:
            settings.primary_controller_ip = str(controller_ip).strip()
            settings.fallback_controller_ips = []
        collector = ClusterCollector(
            known_hosts_path=self.known_hosts_path,
            cancel_event=self.cancel_event,
        )
        bundle = collector.collect(settings, self._credential("cluster"))
        membership_command = bundle.commands.get(SHOW_GROUP_MEMBERSHIP)
        load_command = bundle.commands.get(SHOW_CLIENT_DISTRIBUTION)
        membership = (
            parse_group_membership(membership_command.output, cancel_event=self.cancel_event)
            if membership_command is not None and membership_command.success
            else None
        )
        load = (
            parse_load_distribution(load_command.output, cancel_event=self.cancel_event)
            if load_command is not None and load_command.success
            else None
        )
        load_by_ip = {} if load is None else {str(row.ip): row for row in load.rows}
        members: dict[str, ClusterMemberObservation] = {}
        if membership is not None:
            for row in membership.rows:
                fields = row.raw_fields if isinstance(row.raw_fields, dict) else dict(row.raw_fields)
                status = str(fields.get("status", "")).strip()
                normalized_status = status.upper()
                load_row = load_by_ip.get(str(row.ip))
                members[str(row.ip)] = ClusterMemberObservation(
                    ip=str(row.ip),
                    status=status,
                    connection_type=str(row.connection_type),
                    is_connected=normalized_status.startswith("CONNECTED"),
                    is_leader=bool(_LEADER.search(status)),
                    active_clients=None if load_row is None else int(load_row.active_clients),
                    standby_clients=None if load_row is None else int(load_row.standby_clients),
                )
        for ip, row in load_by_ip.items():
            members.setdefault(
                ip,
                ClusterMemberObservation(
                    ip=ip,
                    active_clients=int(row.active_clients),
                    standby_clients=int(row.standby_clients),
                ),
            )
        membership_complete = bool(
            membership is not None and membership.status is ParseStatus.COMPLETE
        )
        distribution_complete = bool(load is not None and load.status is ParseStatus.COMPLETE)
        complete = membership_complete and distribution_complete
        error_code = bundle.terminal_error_code
        error_message = bundle.terminal_error_message
        if not error_code:
            issues = []
            if membership is not None:
                issues.extend(membership.issues)
            if load is not None:
                issues.extend(load.issues)
            if issues:
                error_code = issues[0].code
                error_message = issues[0].message
        return ClusterObservation(
            collected_at=bundle.collected_at or datetime.now(timezone.utc),
            source_ip=bundle.actual_controller_ip or bundle.requested_controller_ip,
            members=members,
            complete=complete,
            membership_complete=membership_complete,
            distribution_complete=distribution_complete,
            error_code=error_code,
            error_message=error_message,
        )

    def open_action_session(self, controller_ip: str) -> ArubaActionSshAdapter:
        settings = self.settings.cluster
        options = SshConnectionOptions(
            host=str(controller_ip).strip(),
            port=settings.ssh_port,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            command_timeout_seconds=settings.command_timeout_seconds,
            known_hosts_path=self.known_hosts_path,
            enable_required=settings.enable_required,
        )
        factory = self.action_adapter_factory
        if factory is None:
            adapter = ArubaActionSshAdapter(
                options,
                self._credential("cluster"),
                cancel_event=self.cancel_event,
                on_close=self._unregister_action_adapter,
            )
        else:
            adapter = factory(
                options,
                self._credential("cluster"),
                cancel_event=self.cancel_event,
                on_close=self._unregister_action_adapter,
            )
        with self._active_lock:
            self._active_action_adapters.add(adapter)
        try:
            adapter.connect()
        except Exception:
            adapter.close()
            raise
        return adapter

    def _unregister_action_adapter(self, adapter: ArubaActionSshAdapter) -> None:
        with self._active_lock:
            self._active_action_adapters.discard(adapter)

    def cancel_active_connections(self) -> None:
        with self._active_lock:
            adapters = tuple(self._active_action_adapters)
        for adapter in adapters:
            try:
                adapter.abort()
            except Exception:
                pass


def mm_snapshot(observation: MmObservation) -> dict[str, Any]:
    return {
        "collected_at": observation.collected_at,
        "source_ip": observation.source_ip,
        "complete": observation.complete,
        "states": dict(observation.states),
        "hostnames": dict(observation.hostnames),
        "error_code": observation.error_code,
        "error_message": observation.error_message,
    }


def cluster_snapshot(observation: ClusterObservation) -> dict[str, Any]:
    return {
        "collected_at": observation.collected_at,
        "source_ip": observation.source_ip,
        "complete": observation.complete,
        "membership_complete": observation.membership_complete,
        "distribution_complete": observation.distribution_complete,
        "leader_ips": list(observation.leader_ips),
        "error_code": observation.error_code,
        "error_message": observation.error_message,
        "members": {
            ip: {
                "status": row.status,
                "connection_type": row.connection_type,
                "is_connected": row.is_connected,
                "is_leader": row.is_leader,
                "active_clients": row.active_clients,
                "standby_clients": row.standby_clients,
            }
            for ip, row in observation.members.items()
        },
    }


def combined_snapshot(
    mm: MmObservation | None,
    cluster: ClusterObservation | None,
    expected_ips: tuple[str, ...],
) -> dict[str, Any]:
    members: dict[str, dict[str, Any]] = {}
    for ip in expected_ips:
        cluster_row = None if cluster is None else cluster.members.get(ip)
        members[ip] = {
            "mm_status": "-" if mm is None else str(mm.states.get(ip, "-")),
            "status": "-" if cluster_row is None else cluster_row.status,
            "connection_type": "-" if cluster_row is None else cluster_row.connection_type,
            "is_connected": False if cluster_row is None else cluster_row.is_connected,
            "is_leader": False if cluster_row is None else cluster_row.is_leader,
            "active_clients": None if cluster_row is None else cluster_row.active_clients,
            "standby_clients": None if cluster_row is None else cluster_row.standby_clients,
        }
    return {
        "captured_at": datetime.now(timezone.utc),
        "mm_complete": False if mm is None else mm.complete,
        "cluster_complete": False if cluster is None else cluster.complete,
        "leader_ips": [] if cluster is None else list(cluster.leader_ips),
        "members": members,
    }
