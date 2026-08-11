from __future__ import annotations

import argparse
import copy
import logging
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from .collectors.base import (
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    SHOW_SWITCHES,
    CollectionBundle,
    CommandResult,
    SshOperationError,
)
from .collectors.aruba_ssh import ArubaSshAdapter
from .collectors.cluster_collector import ClusterCollector
from .collectors.mm_collector import MmCollector
from .collectors.ssh_host_keys import (
    SshHostKeyCancelledError,
    SshHostKeyError,
    check_scanned_host_key,
    register_scanned_host_key,
    scan_ssh_host_key,
)
from .config import (
    AppPaths,
    AppSettings,
    SettingsError,
    SettingsStore,
    settings_fingerprint,
)
from .credentials import (
    CredentialError,
    CredentialNotFoundError,
    CredentialService,
    CredentialStoreUnavailableError,
    DeviceCredential,
    SessionCredentialStore,
)
from .logging_setup import LoggingContext, setup_logging
from .models import (
    CollectionError,
    ConnectionBaseline,
    DetectionMode,
    IncidentTransitionKind,
    IncidentType,
    ParseStatus,
    PollCycleResult,
    Severity,
)
from .parsers import parse_group_membership, parse_load_distribution, parse_show_switches
from .services.anomaly_detector import AnomalyDetector, AnomalySettings
from .services.notification_service import NotificationService
from .services.poll_coordinator import PollCoordinator
from .storage import SQLiteStorage, StorageError
from .ui.main_window import MainWindow


LOGGER = logging.getLogger(__name__)


_PREFERENCE_PATHS: dict[str, tuple[str, str]] = {
    "polling.interval_seconds": ("polling", "interval_seconds"),
    "polling.automatic_enabled": ("polling", "automatic_enabled"),
    "detection.low_client_threshold": ("detection", "low_client_threshold"),
    "detection.anomaly_cycles": ("detection", "anomaly_cycles"),
    "detection.recovery_cycles": ("detection", "recovery_cycles"),
    "detection.comparison_mode": ("detection", "comparison_mode"),
    "detection.relative_ratio_percent": ("detection", "relative_ratio_percent"),
    "detection.minimum_cluster_active_clients": ("detection", "minimum_cluster_active_clients"),
    "detection.minimum_peer_median": ("detection", "minimum_peer_median"),
    "detection.missing_cycles": ("detection", "missing_cycles"),
    "notifications.notify_new_incidents": ("notifications", "notify_new_incidents"),
    "notifications.repeat_unacknowledged": ("notifications", "repeat_unacknowledged"),
    "notifications.repeat_interval_minutes": ("notifications", "repeat_interval_minutes"),
    "notifications.sound_enabled": ("notifications", "sound_enabled"),
    "notifications.recovery_notifications": ("notifications", "recovery_notifications"),
    "ui.always_on_top": ("ui", "always_on_top"),
    "ui.opacity_percent": ("ui", "opacity_percent"),
    "ui.window_x": ("ui", "window_x"),
    "ui.window_y": ("ui", "window_y"),
    "ui.window_width": ("ui", "window_width"),
    "ui.window_height": ("ui", "window_height"),
}


def restore_persisted_preferences(settings: AppSettings, storage: SQLiteStorage) -> AppSettings:
    """Overlay non-secret SQLite preferences over the JSON configuration."""

    restored = copy.deepcopy(settings)
    try:
        mirror_fingerprint = storage.get_setting("_base_config_fingerprint", "")
    except Exception:
        LOGGER.warning("SQLite preference restore failed; using JSON settings", exc_info=True)
        return restored
    if mirror_fingerprint != settings_fingerprint(settings):
        # JSON is authoritative. A mismatched mirror means the last batch was
        # stale, partial, externally superseded, or never completed.
        return restored
    sentinel = object()
    for key, (section_name, field_name) in _PREFERENCE_PATHS.items():
        try:
            candidate = storage.get_setting(key, sentinel)
        except Exception:
            LOGGER.warning("SQLite preference restore failed; using JSON settings", exc_info=True)
            return copy.deepcopy(settings)
        if candidate is sentinel:
            continue
        section = getattr(restored, section_name)
        current = getattr(section, field_name)
        try:
            if current is None:
                if candidate is not None and type(candidate) is not int:
                    raise TypeError("optional integer preference must be null or an integer")
                value_ = candidate
            elif type(current) is bool:
                if type(candidate) is not bool:
                    raise TypeError("boolean preference must be a JSON boolean")
                value_ = candidate
            elif type(current) is int:
                if type(candidate) is not int:
                    raise TypeError("integer preference must be an integer")
                value_ = candidate
            else:
                if type(candidate) is not str:
                    raise TypeError("string preference must be a string")
                value_ = candidate
            setattr(section, field_name, value_)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid persisted preference: %s", key)
    try:
        restored.validate()
    except Exception:
        LOGGER.exception("SQLite preference overlay is invalid; using JSON settings")
        return copy.deepcopy(settings)
    return restored


@dataclass(slots=True)
class RuntimeSnapshot:
    health: Any
    notification_events: list[Any]
    raw_outputs: dict[str, str] = field(default_factory=dict)
    parse_results: dict[str, Any] = field(default_factory=dict)
    previous_devices: dict[str, Any] = field(default_factory=dict)
    active_incidents: list[Any] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.health, name)


@dataclass(slots=True)
class ConnectionTestResult:
    status: str
    role: str
    host: str
    port: int
    message: str
    fingerprint: str = ""
    algorithm: str = ""
    scanned: Any | None = None
    expected_fingerprints: tuple[str, ...] = ()


class CachedBaselineStore:
    """In-memory baseline authority with best-effort SQLite persistence."""

    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage
        self._values: dict[str, ConnectionBaseline] = {}
        self._dirty: dict[str, ConnectionBaseline] = {}
        try:
            for row in storage.load_connection_baselines():
                baseline = storage.get(row.member_ip)
                if baseline is not None:
                    self._values[baseline.member_ip] = baseline
        except Exception:
            LOGGER.warning("Connection baseline restore failed; starting with an empty cache", exc_info=True)

    def get(self, member_ip: str) -> ConnectionBaseline | None:
        return self._values.get(member_ip)

    def set(self, baseline: ConnectionBaseline) -> None:
        self._values[baseline.member_ip] = baseline
        self._dirty[baseline.member_ip] = baseline

    def flush(
        self,
        changes: list[Any],
        acknowledged_members: set[str],
        incidents: list[Any],
        transitions: list[Any],
    ) -> None:
        """Atomically flush baselines, changes, incidents, and journal rows."""

        dirty = list(self._dirty.values())
        if not dirty and not changes and not acknowledged_members and not incidents and not transitions:
            return
        self.storage.save_cycle_domain_state(
            dirty,
            changes,
            acknowledged_members,
            incidents,
            transitions,
        )
        for baseline in dirty:
            self._dirty.pop(baseline.member_ip, None)


class RuntimePoller:
    """Compose credentials, collectors, parsers, correlation, and persistence."""

    def __init__(
        self,
        settings: AppSettings,
        paths: AppPaths,
        credential_service: CredentialService,
        storage: SQLiteStorage,
        logging_context: LoggingContext,
    ) -> None:
        self._lock = threading.RLock()
        self.settings = copy.deepcopy(settings)
        self.paths = paths
        self.credential_service = credential_service
        self.storage = storage
        self.logging_context = logging_context
        self.baseline_store = CachedBaselineStore(storage)
        try:
            restored_devices = storage.load_device_states()
        except Exception:
            LOGGER.warning("Previous device snapshot restore failed", exc_info=True)
            restored_devices = {}
        self._last_devices: dict[str, Any] = {
            ip: row.get("payload", {}) for ip, row in restored_devices.items()
        }
        self.detector = self._create_detector()
        self.engine = self._create_engine()
        self.incident_manager = self._create_incident_manager()
        self._pending_persistence_transitions: list[Any] = []
        self._pending_connection_acknowledgements: set[str] = set()

    def _create_detector(self, state: dict[str, dict[str, object]] | None = None) -> AnomalyDetector:
        if state is None:
            try:
                state = {
                    f"{row.detector}|{row.ip}": {
                        "anomaly_streak": row.anomaly_count,
                        "recovery_streak": row.recovery_count,
                        "active": row.active,
                    }
                    for row in self.storage.load_streaks()
                }
            except Exception:
                LOGGER.warning("Detector streak restore failed; starting from zero", exc_info=True)
                state = {}
        return AnomalyDetector(self._anomaly_settings(self.settings), state=state)

    def _create_engine(self, pending_changes: list[Any] | None = None):
        from .services.correlation_engine import CorrelationEngine

        try:
            known_mm = {
                row["ip"]: row.get("hostname") or row.get("alias")
                for row in self.storage.load_mm_discovered_devices()
            }
        except Exception:
            LOGGER.warning("MM device restore failed", exc_info=True)
            known_mm = {}
        if pending_changes is None:
            try:
                pending_changes = self.storage.load_pending_connection_changes()
            except Exception:
                LOGGER.warning("Pending Connection-Type event restore failed", exc_info=True)
                pending_changes = []
        return CorrelationEngine(
            settings=self._anomaly_settings(self.settings),
            detector=self.detector,
            baseline_store=self.baseline_store,
            known_mm_devices=known_mm,
            pending_connection_changes=pending_changes,
        )

    def _create_incident_manager(self, incidents: list[Any] | None = None):
        from .services.incident_manager import IncidentManager

        notifications = self.settings.notifications
        if incidents is None:
            try:
                incidents = self.storage.load_domain_incidents()
            except Exception:
                LOGGER.warning("Incident restore failed", exc_info=True)
                incidents = []
        return IncidentManager(
            incidents,
            repeat_unacknowledged=notifications.repeat_unacknowledged,
            repeat_interval=timedelta(minutes=notifications.repeat_interval_minutes),
            recovery_notifications=notifications.recovery_notifications,
        )

    @staticmethod
    def _anomaly_settings(settings: AppSettings) -> AnomalySettings:
        detection = settings.detection
        return AnomalySettings(
            low_client_threshold=detection.low_client_threshold,
            anomaly_confirmations=detection.anomaly_cycles,
            recovery_confirmations=detection.recovery_cycles,
            relative_ratio=detection.relative_ratio_percent / 100.0,
            cluster_min_total_active=detection.minimum_cluster_active_clients,
            peer_minimum=detection.minimum_peer_median,
            detection_mode=DetectionMode(detection.comparison_mode),
            missing_confirmations=detection.missing_cycles,
            missing_recovery_confirmations=detection.recovery_cycles,
        )

    def update_settings(self, settings: AppSettings) -> None:
        with self._lock:
            previous = (
                self.settings,
                self.detector,
                self.engine,
                self.incident_manager,
            )
            detector_state = self.detector.dump_state()
            pending_changes = self.engine.pending_connection_changes()
            incidents = copy.deepcopy(self.incident_manager.events())
            self._persist_detector_state()
            debug_changed = settings.ssh_debug_logging != self.settings.ssh_debug_logging
            try:
                self.settings = copy.deepcopy(settings)
                if debug_changed:
                    self.logging_context.set_ssh_debug_enabled(settings.ssh_debug_logging)
                # Preserve streak state but apply the newly selected thresholds.
                self.detector = self._create_detector(detector_state)
                self.engine = self._create_engine(pending_changes)
                self.incident_manager = self._create_incident_manager(incidents)
            except Exception:
                self.settings, self.detector, self.engine, self.incident_manager = previous
                if debug_changed:
                    try:
                        self.logging_context.set_ssh_debug_enabled(
                            self.settings.ssh_debug_logging
                        )
                    except Exception:
                        LOGGER.critical("SSH debug logger rollback failed", exc_info=True)
                raise

    def can_auto_start(self) -> tuple[bool, str]:
        try:
            with self._lock:
                settings = copy.deepcopy(self.settings)
            settings.validate_for_monitoring()
            self.credential_service.get(settings.credentials.effective_id("mm", settings))
            self.credential_service.get(settings.credentials.effective_id("cluster", settings))
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def reset_demo_state(self) -> None:
        """Reset fixture-only state before replaying the demo sequence."""

        with self._lock:
            self.storage.reset_monitoring_state_for_demo()
            self.baseline_store = CachedBaselineStore(self.storage)
            self.detector = self._create_detector({})
            self.engine = self._create_engine()
            self.incident_manager = self._create_incident_manager()
            self._last_devices = {}
            self._pending_persistence_transitions.clear()
            self._pending_connection_acknowledgements.clear()

    def test_connection(
        self,
        role: str,
        candidate_settings: Any,
        cancellation_event: threading.Event | None = None,
    ) -> ConnectionTestResult:
        """Verify the host key before retrieving or sending credentials."""

        transient_credential = getattr(candidate_settings, "credential", None)
        candidate_settings = getattr(candidate_settings, "settings", candidate_settings)
        if role == "mm":
            endpoint = candidate_settings.mobility_master
            hosts = [endpoint.management_ip.strip()]
        elif role == "cluster":
            endpoint = candidate_settings.cluster
            hosts = list(
                dict.fromkeys(
                    host.strip()
                    for host in [
                        endpoint.primary_controller_ip,
                        *endpoint.fallback_controller_ips,
                    ]
                    if host.strip()
                )
            )
        else:
            raise ValueError("알 수 없는 연결 테스트 대상입니다.")
        if not hosts:
            raise ValueError("연결 테스트할 장비 IP를 입력하세요.")
        scans = []
        scan_failures: list[str] = []
        for host in hosts:
            try:
                scanned = scan_ssh_host_key(
                    host,
                    endpoint.ssh_port,
                    timeout=endpoint.connect_timeout_seconds,
                    cancel_event=cancellation_event,
                )
            except SshHostKeyCancelledError:
                raise
            except SshHostKeyError:
                if role == "mm":
                    raise
                scan_failures.append(host)
                LOGGER.info("Cluster host-key scan unavailable for %s; trying the next controller", host)
                continue
            scans.append(scanned)
            check = check_scanned_host_key(scanned, self.paths.known_hosts)
            if check.status in {"unregistered", "unregistered_algorithm"}:
                return ConnectionTestResult(
                    status="approval_required",
                    role=role,
                    host=host,
                    port=endpoint.ssh_port,
                    message="최초 연결 전에 SSH 호스트 키 지문 승인이 필요합니다.",
                    fingerprint=scanned.fingerprint,
                    algorithm=scanned.algorithm,
                    scanned=scanned,
                    expected_fingerprints=check.expected_fingerprints,
                )
            if check.status == "mismatch":
                return ConnectionTestResult(
                    status="mismatch",
                    role=role,
                    host=host,
                    port=endpoint.ssh_port,
                    message="저장된 SSH 호스트 키와 장비가 제시한 키가 다릅니다. 자동 교체하지 않았습니다.",
                    fingerprint=scanned.fingerprint,
                    algorithm=scanned.algorithm,
                    scanned=scanned,
                    expected_fingerprints=check.expected_fingerprints,
                )
        if not scans:
            raise SshHostKeyError(
                "모든 Cluster 수집 Controller의 SSH 지문을 확인하지 못했습니다. "
                "IP, 포트 및 네트워크 연결을 확인하세요."
            )
        credential = transient_credential
        if credential is None:
            credential_id = candidate_settings.credentials.effective_id(role, candidate_settings)
            if not credential_id:
                raise RuntimeError("자격 증명을 입력하거나 먼저 저장한 뒤 연결 테스트를 실행하세요.")
            credential = self.credential_service.get(credential_id)
        self.logging_context.register_secret(credential.username)
        self.logging_context.register_secret(credential.password)
        self.logging_context.register_secret(credential.enable_secret)
        from .collectors.base import SshConnectionOptions

        authenticated_hosts: list[str] = []
        last_auth_error: SshOperationError | None = None
        for scanned in scans:
            host = scanned.host
            options = SshConnectionOptions(
                host=host,
                port=endpoint.ssh_port,
                connect_timeout_seconds=endpoint.connect_timeout_seconds,
                command_timeout_seconds=endpoint.command_timeout_seconds,
                known_hosts_path=self.paths.known_hosts,
                enable_required=endpoint.enable_required,
            )
            adapter = ArubaSshAdapter(
                options,
                credential,
                cancel_event=cancellation_event,
                logger=self.logging_context.ssh_logger,
            )
            try:
                adapter.connect()
                authenticated_hosts.append(host)
            except SshOperationError as exc:
                last_auth_error = exc
                if role == "mm":
                    raise
                LOGGER.info("Cluster connection test failed for %s: %s", host, exc.code)
            finally:
                adapter.close()
        if not authenticated_hosts:
            if last_auth_error is not None:
                raise last_auth_error
            raise RuntimeError("인증 가능한 Cluster 수집 Controller가 없습니다.")
        first_scan = scans[0]
        skipped_note = (
            f" 연결할 수 없어 건너뛴 Controller {len(scan_failures)}개가 있습니다."
            if scan_failures
            else ""
        )
        return ConnectionTestResult(
            status="success",
            role=role,
            host=", ".join(authenticated_hosts),
            port=endpoint.ssh_port,
            message=(
                f"{len(authenticated_hosts)}개 수집 Controller의 SSH 호스트 키와 로그인을 확인했습니다."
                + skipped_note
            ),
            fingerprint=first_scan.fingerprint,
            algorithm=first_scan.algorithm,
        )

    def approve_host_key(self, scanned: Any) -> Path:
        return register_scanned_host_key(scanned, self.paths.known_hosts)

    def __call__(self, cancellation_event: threading.Event | None = None):
        with self._lock:
            settings = copy.deepcopy(self.settings)
        settings.validate_for_monitoring()
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("점검이 취소되었습니다.")
        mm_credential, mm_bundle = self._resolve_credential(
            "mm",
            settings.credentials.effective_id("mm", settings),
            settings.mobility_master.management_ip,
            (SHOW_SWITCHES,),
        )
        cluster_credential, cluster_bundle = self._resolve_credential(
            "cluster",
            settings.credentials.effective_id("cluster", settings),
            settings.cluster.primary_controller_ip,
            (SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP),
        )
        for credential in {item for item in (mm_credential, cluster_credential) if item is not None}:
            self.logging_context.register_secret(credential.username)
            self.logging_context.register_secret(credential.password)
            self.logging_context.register_secret(credential.enable_secret)

        cancel_event = cancellation_event or threading.Event()

        def adapter_factory(options, credential):
            return ArubaSshAdapter(
                options,
                credential,
                cancel_event=cancel_event,
                logger=self.logging_context.ssh_logger,
            )

        mm_collector = MmCollector(known_hosts_path=self.paths.known_hosts, adapter_factory=adapter_factory)
        cluster_collector = ClusterCollector(
            known_hosts_path=self.paths.known_hosts,
            adapter_factory=adapter_factory,
        )
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="aruba-collector") as executor:
            mm_future = (
                executor.submit(mm_collector.collect, settings.mobility_master, mm_credential)
                if mm_credential is not None
                else None
            )
            cluster_future = (
                executor.submit(cluster_collector.collect, settings.cluster, cluster_credential)
                if cluster_credential is not None
                else None
            )
            if mm_future is not None:
                try:
                    mm_bundle = mm_future.result()
                except Exception:
                    LOGGER.exception("MM collector failed outside the SSH error boundary")
                    mm_bundle = self._source_failure_bundle(
                        "mm",
                        settings.mobility_master.management_ip,
                        (SHOW_SWITCHES,),
                        "COLLECTOR_INTERNAL_ERROR",
                        "MM 수집 처리 중 오류가 발생했습니다. 로그를 확인하세요.",
                    )
            if cluster_future is not None:
                try:
                    cluster_bundle = cluster_future.result()
                except Exception:
                    LOGGER.exception("Cluster collector failed outside the SSH error boundary")
                    cluster_bundle = self._source_failure_bundle(
                        "cluster",
                        settings.cluster.primary_controller_ip,
                        (SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP),
                        "COLLECTOR_INTERNAL_ERROR",
                        "Cluster 수집 처리 중 오류가 발생했습니다. 로그를 확인하세요.",
                    )
        assert mm_bundle is not None and cluster_bundle is not None
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("점검이 취소되었습니다.")

        checked_at = datetime.now(timezone.utc)
        errors: list[CollectionError] = []
        mm_result = self._parse_command(mm_bundle, SHOW_SWITCHES, parse_show_switches, errors)
        load_result = self._parse_command(
            cluster_bundle, SHOW_CLIENT_DISTRIBUTION, parse_load_distribution, errors
        )
        membership_result = self._parse_command(
            cluster_bundle, SHOW_GROUP_MEMBERSHIP, parse_group_membership, errors
        )
        expected = {member.ip.strip(): member.alias.strip() for member in settings.cluster.members if member.ip.strip()}
        cycle = PollCycleResult(
            checked_at=checked_at,
            expected_cluster_members=expected,
            mm_result=mm_result,
            load_result=load_result,
            membership_result=membership_result,
            collection_errors=errors,
            requested_cluster_controller_ip=cluster_bundle.requested_controller_ip or None,
            actual_cluster_controller_ip=cluster_bundle.actual_controller_ip or None,
            primary_failed=cluster_bundle.primary_failed,
            failover_at=cluster_bundle.failover_at,
            raw_outputs=self._raw_outputs(mm_bundle, cluster_bundle),
        )
        # Settings may have changed while SSH was in flight. Correlate through
        # the currently active engine so one completed cycle cannot update a
        # detached detector/incident manager.
        snapshot = self.correlate(cycle)
        health = snapshot.health
        self._persist_result(health, cluster_bundle)
        return snapshot

    def _resolve_credential(
        self,
        source: str,
        credential_id: str,
        requested_controller_ip: str,
        commands: tuple[str, ...],
    ) -> tuple[DeviceCredential | None, CollectionBundle | None]:
        try:
            return self.credential_service.get(credential_id), None
        except (CredentialError, ValueError) as exc:
            if isinstance(exc, CredentialNotFoundError):
                code = "CREDENTIAL_NOT_FOUND"
            elif isinstance(exc, CredentialStoreUnavailableError):
                code = "CREDENTIAL_STORE_UNAVAILABLE"
            else:
                code = "CREDENTIAL_ERROR"
            LOGGER.warning("%s credential resolution failed: %s", source, code)
            return None, self._source_failure_bundle(
                source,
                requested_controller_ip,
                commands,
                code,
                str(exc),
            )

    @staticmethod
    def _source_failure_bundle(
        source: str,
        requested_controller_ip: str,
        commands: tuple[str, ...],
        code: str,
        message: str,
    ) -> CollectionBundle:
        return CollectionBundle(
            source=source,
            requested_controller_ip=requested_controller_ip.strip(),
            commands={
                command: CommandResult(
                    command,
                    False,
                    error_code=code,
                    error_message=message,
                )
                for command in commands
            },
            terminal_error_code=code,
            terminal_error_message=message,
        )

    def correlate(self, cycle: PollCycleResult, *, engine: Any | None = None) -> RuntimeSnapshot:
        """Correlate a live or demo cycle and advance persistent incidents."""

        with self._lock:
            active_engine = engine or self.engine
            manager = self.incident_manager
            notify_new = self.settings.notifications.notify_new_incidents
            repeat_enabled = self.settings.notifications.repeat_unacknowledged
            health = active_engine.correlate(cycle)
            transitions = manager.process(health, now=health.checked_at)
            activated_ids = {
                transition.incident.incident_id
                for transition in transitions
                if transition.kind is IncidentTransitionKind.ACTIVATED
            }
            notifications: list[Any] = []
            for transition in transitions:
                if transition.kind is IncidentTransitionKind.ACTIVATED and notify_new:
                    notifications.append(transition.incident)
                elif transition.kind is IncidentTransitionKind.RECOVERED and transition.should_notify:
                    notifications.append(transition.incident)
            if repeat_enabled:
                notification_ids = {incident.incident_id for incident in notifications}
                for incident in manager.due_notifications(now=health.checked_at):
                    if incident.incident_id not in activated_ids and incident.incident_id not in notification_ids:
                        notifications.append(incident)
            self._persist_incidents(transitions, engine=active_engine)
            previous_devices = copy.deepcopy(self._last_devices)
            self._last_devices = {
                device.ip: copy.deepcopy(device) for device in health.devices
            }
            return RuntimeSnapshot(
                health=health,
                notification_events=notifications,
                raw_outputs=dict(cycle.raw_outputs),
                parse_results={
                    SHOW_SWITCHES: cycle.mm_result,
                    SHOW_CLIENT_DISTRIBUTION: cycle.load_result,
                    SHOW_GROUP_MEMBERSHIP: cycle.membership_result,
                },
                previous_devices=previous_devices,
                active_incidents=manager.active_incidents(),
            )

    def _parse_command(
        self,
        bundle: CollectionBundle,
        command: str,
        parser: Any,
        errors: list[CollectionError],
    ) -> Any | None:
        result = bundle.commands.get(command)
        if result is None or not result.success:
            errors.append(
                CollectionError(
                    source=command,
                    code=(result.error_code if result else bundle.terminal_error_code) or "COMMAND_NOT_RUN",
                    user_message=(result.error_message if result else bundle.terminal_error_message)
                    or "명령 결과를 수집하지 못했습니다.",
                    target_ip=bundle.actual_controller_ip or bundle.requested_controller_ip or None,
                )
            )
            return None
        parsed = parser(result.output)
        if parsed.status is not ParseStatus.COMPLETE:
            code = "PARSE_FAILED" if parsed.status is ParseStatus.FAILED else "PARSE_PARTIAL"
            reasons = "; ".join(
                f"{issue.code}: {issue.message}" for issue in parsed.issues[:5]
            ) or "구조가 불완전합니다."
            LOGGER.warning("%s %s: %s", command, code, reasons)
            excerpt = self.logging_context.redactor.redact(parsed.output_excerpt)
            self.logging_context.ssh_logger.debug(
                "%s %s output excerpt:\n%s",
                command,
                code,
                excerpt,
            )
        return parsed

    @staticmethod
    def _raw_outputs(mm_bundle: CollectionBundle, cluster_bundle: CollectionBundle) -> dict[str, str]:
        outputs: dict[str, str] = {}
        for bundle in (mm_bundle, cluster_bundle):
            for command, result in bundle.commands.items():
                if result.success:
                    outputs[command] = result.output
        return outputs

    def _persist_detector_state(self) -> None:
        try:
            for key, counter in self.detector.dump_state().items():
                detector, separator, ip = key.partition("|")
                if not separator:
                    continue
                self.storage.save_streak(
                    detector,
                    ip,
                    int(counter["anomaly_streak"]),
                    int(counter["recovery_streak"]),
                    bool(counter["active"]),
                )
        except Exception:
            LOGGER.warning("Detector streak persistence deferred", exc_info=True)

    def _persist_result(self, health: Any, cluster_bundle: CollectionBundle) -> None:
        try:
            self._persist_detector_state()
            for device in health.devices:
                self.storage.save_device_state(
                    device.ip,
                    device,
                    observed_at=health.checked_at,
                    is_normal=device.severity is Severity.NORMAL,
                )
                if device.mm_present:
                    self.storage.save_mm_discovered_device(
                        device.ip,
                        alias=device.alias or "",
                        hostname=device.hostname or "",
                        last_seen_at=device.last_seen or health.checked_at,
                        missing_streak=0,
                        recovery_streak=0,
                    )
            if (
                cluster_bundle.primary_failed
                and cluster_bundle.complete
                and cluster_bundle.actual_controller_ip
                and cluster_bundle.actual_controller_ip != cluster_bundle.requested_controller_ip
            ):
                first_error = next((attempt.error_code for attempt in cluster_bundle.attempts if not attempt.success), "")
                self.storage.record_failover(
                    cluster_bundle.requested_controller_ip,
                    cluster_bundle.actual_controller_ip,
                    first_error or "PRIMARY_FAILED",
                    collected_at=cluster_bundle.failover_at or health.checked_at,
                )
        except Exception:
            LOGGER.exception("상태 저장 중 오류가 발생했습니다. 현재 점검 결과는 화면에 계속 표시합니다.")

    def _persist_incidents(self, transitions: list[Any], *, engine: Any | None = None) -> None:
        self._pending_persistence_transitions.extend(transitions)
        try:
            self.baseline_store.flush(
                (engine or self.engine).pending_connection_changes(),
                self._pending_connection_acknowledgements,
                self.incident_manager.events(),
                self._pending_persistence_transitions,
            )
            self._pending_persistence_transitions.clear()
            self._pending_connection_acknowledgements.clear()
        except Exception:
            LOGGER.exception("상태와 장애 사건의 원자 저장에 실패했습니다. 다음 저장에서 재시도합니다.")

    def acknowledge_ip(self, ip: str) -> None:
        with self._lock:
            transitions = self.incident_manager.acknowledge_ip(ip)
            if self.engine.acknowledge_connection_change(ip):
                self._pending_connection_acknowledgements.add(ip)
            self._persist_incidents(transitions)

    def acknowledge_global(self) -> None:
        with self._lock:
            transitions = []
            for incident in self.incident_manager.active_incidents():
                if incident.incident_type is not IncidentType.COLLECTION_FAILURE:
                    continue
                transition = self.incident_manager.acknowledge(incident.incident_id)
                if transition is not None:
                    transitions.append(transition)
        self._persist_incidents(transitions)

    def mark_notification_delivered(self, event: Any) -> None:
        incident_id = str(getattr(event, "incident_id", "") or "")
        if not incident_id:
            return
        with self._lock:
            if not self.incident_manager.mark_notified(incident_id):
                return
            self._persist_incidents([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aruba MM/WLC 상태 미니 대시보드")
    parser.add_argument("--demo", action="store_true", help="실제 SSH 없이 fixture 시나리오 실행")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--demo-fixtures", type=Path, help=argparse.SUPPRESS)
    return parser


def _run_frozen_smoke(fixture_dir: Path | None = None) -> str:
    """Exercise packaged runtime dependencies without UI or network access."""

    import netmiko
    import paramiko

    if not callable(getattr(netmiko, "ConnectHandler", None)):
        raise RuntimeError("Netmiko ConnectHandler is unavailable")
    if not callable(getattr(paramiko, "SSHClient", None)):
        raise RuntimeError("Paramiko SSHClient is unavailable")

    markers = [
        "ARUBA_MINI_DASHBOARD_SMOKE_OK",
        "NETMIKO_OK",
        "PARAMIKO_OK",
    ]
    if os.name == "nt":
        import win32cred

        if not callable(getattr(win32cred, "CredRead", None)):
            raise RuntimeError("Windows Credential Manager API is unavailable")
        if not isinstance(getattr(win32cred, "CRED_TYPE_GENERIC", None), int):
            raise RuntimeError("Windows Credential Manager constants are unavailable")
        markers.append("WIN32CRED_OK")
    else:
        markers.append("WIN32CRED_SKIPPED_NON_WINDOWS")

    from .demo import DemoPoller, demo_fixture_directory
    from .services.correlation_engine import CorrelationEngine

    discovered = demo_fixture_directory(fixture_dir)
    markers.append("FIXTURE_DISCOVERY_OK")
    health = DemoPoller(CorrelationEngine(), fixture_dir=discovered)()
    if health.partial or health.severity is not Severity.NORMAL or len(health.devices) != 4:
        raise RuntimeError("Demo fixture parse/correlation smoke produced an invalid result")
    if any(device.ip is None for device in health.devices):
        raise RuntimeError("Demo correlation result is missing a device IP")
    markers.append("DEMO_CORRELATION_OK")
    return "\n".join(markers) + "\n"


def _run_qt_ui_smoke(output_path: Path | None) -> int:
    """Create a real Qt window briefly without touching runtime state or network."""

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ArubaMiniDashboardUiSmoke")
    window = QWidget()
    window.setWindowTitle("Aruba Mini Dashboard UI Smoke")
    window.resize(240, 120)
    marker = "WINDOWS_QT_UI_OK\n"
    completed = False

    def finish() -> None:
        nonlocal completed
        if output_path is not None:
            _write_atomic_text(output_path, marker)
        completed = True
        window.close()
        app.quit()

    window.show()
    QTimer.singleShot(100, finish)
    QTimer.singleShot(5000, app.quit)
    exit_code = int(app.exec())
    return exit_code if completed else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke:
        marker = _run_frozen_smoke(args.demo_fixtures)
        if args.smoke_output is not None:
            _write_atomic_text(args.smoke_output, marker)
        if sys.stdout is not None:
            print(marker.rstrip(), flush=True)
        return 0
    if args.ui_smoke:
        return _run_qt_ui_smoke(args.smoke_output)

    paths = AppPaths.from_environment().ensure()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ArubaMiniDashboard")
    app.setOrganizationName("ArubaMiniDashboard")
    app.setQuitOnLastWindowClosed(False)

    store = SettingsStore(paths)
    settings_error = ""
    try:
        settings = store.load()
    except SettingsError as exc:
        settings = AppSettings.default()
        settings_error = str(exc)
    if args.demo:
        # Demo is intentionally isolated from production endpoints, aliases,
        # thresholds, credentials, and automatic-start preferences.
        settings = AppSettings.default()
        settings_error = ""
    storage_error = ""
    try:
        storage = SQLiteStorage(":memory:" if args.demo else paths)
    except StorageError as exc:
        storage_error = str(exc)
        storage = SQLiteStorage(":memory:")
    settings = restore_persisted_preferences(settings, storage)
    if settings_error or storage_error:
        settings.polling.automatic_enabled = False
    logging_context = setup_logging(paths, ssh_debug_enabled=settings.ssh_debug_logging)
    credentials = (
        CredentialService(persistent=SessionCredentialStore())
        if args.demo
        else CredentialService()
    )
    runtime = RuntimePoller(settings, paths, credentials, storage, logging_context)
    collect_cycle: Any = runtime
    if args.demo:
        from .demo import DemoPoller

        collect_cycle = DemoPoller(
            runtime,
            fixture_dir=args.demo_fixtures,
        )
    coordinator = PollCoordinator(
        collect_cycle,
        settings.polling.interval_seconds,
        connection_tester=runtime.test_connection,
        host_key_approver=runtime.approve_host_key,
        start_guard=None if args.demo else runtime.can_auto_start,
    )
    notifications = NotificationService(
        sound_enabled=settings.notifications.sound_enabled,
        repeat_enabled=settings.notifications.repeat_unacknowledged,
        repeat_minutes=settings.notifications.repeat_interval_minutes,
        recovery_enabled=settings.notifications.recovery_notifications,
    )
    window = MainWindow(
        coordinator,
        settings,
        settings_store=None if args.demo else store,
        credential_service=credentials,
        notification_service=notifications,
        storage=storage,
        settings_apply_handler=runtime.update_settings,
        demo_mode=args.demo,
    )
    window.acknowledge_requested.connect(runtime.acknowledge_ip)
    window.acknowledge_global_requested.connect(runtime.acknowledge_global)
    window.acknowledge_requested.connect(notifications.acknowledge_ip)
    notifications.notification_shown.connect(runtime.mark_notification_delivered)
    app.setQuitOnLastWindowClosed(not window.tray_icon.isVisible())

    closed = False

    def cleanup() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if not coordinator.shutdown(0):
            # Keep storage/credentials alive until the still-running worker is
            # torn down by process shutdown; closing them here creates a race.
            LOGGER.warning("Background worker still active during external application shutdown")
            return
        credentials.close()
        storage.close()

    app.aboutToQuit.connect(cleanup)
    window.show()
    if settings_error or storage_error:
        startup_errors = "\n\n".join(error for error in (settings_error, storage_error) if error)
        QTimer.singleShot(
            0,
            lambda: QMessageBox.warning(
                window,
                "로컬 상태 저장소 확인 필요",
                startup_errors + "\n기존 파일은 변경하지 않았으며 자동 점검을 시작하지 않습니다.",
            ),
        )
    elif args.demo:
        QTimer.singleShot(0, coordinator.check_now)
    elif settings.polling.automatic_enabled:
        ready, reason = runtime.can_auto_start()
        if ready:
            QTimer.singleShot(0, coordinator.start_automatic)
        else:
            settings.polling.automatic_enabled = False
            window.statusBar().showMessage("자동 점검 일시정지: " + reason, 15000)

    exit_code = app.exec()
    cleanup()
    return int(exit_code)


def _write_atomic_text(path: Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
