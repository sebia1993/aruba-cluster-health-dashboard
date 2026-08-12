from __future__ import annotations

import argparse
import copy
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLockFile, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

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
    AppPathError,
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
from .logging_setup import LoggingContext, current_process_metrics, setup_logging
from .lazy_text_mapping import snapshot_raw_outputs
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


_INSTANCE_LOCK_FILENAME = ".aruba-mini-dashboard.lock"
_INSTANCE_LOCK_STALE_TIME_MS = 30_000


class InstanceAlreadyRunningError(RuntimeError):
    """Another live process owns this data root."""


class InstanceLockUnavailableError(RuntimeError):
    """The per-data-root lock could not be created safely."""


def _acquire_instance_lock(paths: AppPaths) -> QLockFile:
    """Acquire the process guard before settings or SQLite are opened.

    QLockFile records the owning PID and application identity and removes a
    dead owner's stale file during ``tryLock``. Its OS-level behavior also
    releases ownership after a crash, without another runtime dependency.
    """

    lock = QLockFile(str(paths.root / _INSTANCE_LOCK_FILENAME))
    lock.setStaleLockTime(_INSTANCE_LOCK_STALE_TIME_MS)
    if lock.tryLock(0):
        return lock
    if lock.error() == QLockFile.LockError.LockFailedError:
        raise InstanceAlreadyRunningError(
            "이 데이터 폴더를 사용하는 대시보드가 이미 실행 중입니다. "
            "작업 표시줄 또는 알림 영역에서 기존 창을 열어 주세요."
        )
    raise InstanceLockUnavailableError(
        "단일 실행 보호 파일을 만들 수 없습니다. "
        "데이터 폴더 쓰기 권한과 보안 소프트웨어 차단 여부를 확인하세요."
    )


def _report_early_startup_issue(title: str, message: str, *, critical: bool) -> None:
    """Report an actionable, pre-logging startup result to GUI and stderr."""

    if sys.stderr is not None:
        try:
            print(f"{title}: {message}", file=sys.stderr, flush=True)
        except (OSError, UnicodeError):
            LOGGER.debug("Early startup stderr notice unavailable", exc_info=True)
    try:
        if critical:
            QMessageBox.critical(None, title, message)
        else:
            QMessageBox.information(None, title, message)
    except Exception:
        # stderr above is the non-GUI fallback for headless or damaged Qt
        # environments. No shared state has been opened at this point.
        LOGGER.debug("Early startup GUI notice unavailable", exc_info=True)


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
    "performance.low_spec_mode": ("performance", "low_spec_mode"),
    "performance.performance_logging": ("performance", "performance_logging"),
    "ui.always_on_top": ("ui", "always_on_top"),
    "ui.opacity_percent": ("ui", "opacity_percent"),
    "ui.window_maximized": ("ui", "window_maximized"),
    "ui.window_x": ("ui", "window_x"),
    "ui.window_y": ("ui", "window_y"),
    "ui.window_width": ("ui", "window_width"),
    "ui.window_height": ("ui", "window_height"),
}


def restore_persisted_preferences(settings: AppSettings, storage: SQLiteStorage) -> AppSettings:
    """Overlay non-secret SQLite preferences over the JSON configuration."""

    restored = copy.deepcopy(settings)
    keys = ("_base_config_fingerprint", *_PREFERENCE_PATHS)
    try:
        persisted = storage.get_preferences(keys)
        mirror_fingerprint = persisted.get("_base_config_fingerprint", "")
    except Exception:
        LOGGER.warning("SQLite preference restore failed; using JSON settings", exc_info=True)
        return restored
    if mirror_fingerprint != settings_fingerprint(settings):
        # JSON is authoritative. A mismatched mirror means the last batch was
        # stale, partial, externally superseded, or never completed.
        return restored
    for key, (section_name, field_name) in _PREFERENCE_PATHS.items():
        if key not in persisted:
            continue
        candidate = persisted[key]
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
    raw_outputs: Mapping[str, str] = field(default_factory=dict)
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
        self._removed: set[str] = set()
        try:
            for baseline in storage.load_domain_connection_baselines():
                self._values[baseline.member_ip] = baseline
        except Exception:
            LOGGER.warning("Connection baseline restore failed; starting with an empty cache", exc_info=True)

    def get(self, member_ip: str) -> ConnectionBaseline | None:
        return self._values.get(member_ip)

    def set(self, baseline: ConnectionBaseline) -> None:
        self._values[baseline.member_ip] = baseline
        self._dirty[baseline.member_ip] = baseline
        self._removed.discard(baseline.member_ip)

    def discard(self, member_ip: str) -> None:
        member_ip = str(member_ip)
        self._values.pop(member_ip, None)
        self._dirty.pop(member_ip, None)
        self._removed.add(member_ip)

    def prune(self, expected_ips: Any) -> set[str]:
        allowed = {str(ip) for ip in expected_ips}
        removed = set(self._values) - allowed
        for member_ip in removed:
            self.discard(member_ip)
        return removed

    def snapshot_state(
        self,
    ) -> tuple[dict[str, ConnectionBaseline], dict[str, ConnectionBaseline], set[str]]:
        """Capture the small in-memory cache for a reversible settings stage."""

        return dict(self._values), dict(self._dirty), set(self._removed)

    def restore_state(
        self,
        state: tuple[
            dict[str, ConnectionBaseline],
            dict[str, ConnectionBaseline],
            set[str],
        ],
    ) -> None:
        values, dirty, removed = state
        self._values = dict(values)
        self._dirty = dict(dirty)
        self._removed = set(removed)

    def flush(
        self,
        changes: list[Any],
        acknowledged_members: set[str],
        incidents: list[Any],
        transitions: list[Any],
    ) -> None:
        """Atomically flush baselines, changes, incidents, and journal rows."""

        dirty = list(self._dirty.values())
        removed = set(self._removed)
        if (
            not dirty
            and not changes
            and not acknowledged_members
            and not incidents
            and not transitions
            and not removed
        ):
            return
        if removed:
            self.storage.save_cycle_domain_state(
                dirty,
                changes,
                acknowledged_members,
                incidents,
                transitions,
                removed,
            )
        else:
            # Keep the established five-argument protocol for test/fallback
            # stores when no scope reconciliation is pending.
            self.storage.save_cycle_domain_state(
                dirty,
                changes,
                acknowledged_members,
                incidents,
                transitions,
            )
        for baseline in dirty:
            self._dirty.pop(baseline.member_ip, None)
        self._removed.difference_update(removed)


class RuntimeSettingsUpdate:
    """In-memory settings stage committed only after authoritative JSON."""

    def __init__(
        self,
        runtime: "RuntimePoller",
        *,
        previous: tuple[Any, Any, Any, Any],
        baseline_state: tuple[
            dict[str, ConnectionBaseline],
            dict[str, ConnectionBaseline],
            set[str],
        ],
        pending_transitions: list[Any],
        pending_acknowledgements: set[str],
        scope_transitions: list[Any],
        debug_changed: bool,
        low_spec_changed: bool,
        performance_log_changed: bool,
    ) -> None:
        self._runtime = runtime
        self._previous = previous
        self._baseline_state = baseline_state
        self._pending_transitions = pending_transitions
        self._pending_acknowledgements = pending_acknowledgements
        self._scope_transitions = list(scope_transitions)
        self._debug_changed = debug_changed
        self._low_spec_changed = low_spec_changed
        self._performance_log_changed = performance_log_changed
        self._finished = False

    def commit(self) -> None:
        if self._finished:
            return
        with self._runtime._lock:
            self._runtime._pending_persistence_transitions.extend(self._scope_transitions)
            # Persistence is retryable and _persist_incidents deliberately
            # retains pending state after a bounded SQLite failure. The JSON
            # has already committed, so never roll the authoritative setting
            # back merely because the cleanup must wait for the next write.
            self._runtime._persist_incidents([])
            self._finished = True

    def rollback(self) -> None:
        if self._finished:
            return
        with self._runtime._lock:
            self._runtime._restore_settings_stage(
                self._previous,
                self._baseline_state,
                self._pending_transitions,
                self._pending_acknowledgements,
                debug_changed=self._debug_changed,
                low_spec_changed=self._low_spec_changed,
                performance_log_changed=self._performance_log_changed,
            )
            self._finished = True


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
        configured_members = {
            member.ip.strip()
            for member in self.settings.cluster.members
            if member.ip.strip()
        }
        if len(configured_members) == 4:
            try:
                # Run before loading snapshots/MM inventory so an upgraded
                # database with years of churn cannot inflate startup memory
                # first. Incomplete/default settings deliberately skip cleanup:
                # a missing or corrupt JSON file must never erase the previous
                # registered inventory before the operator can recover it.
                storage.maintain_device_inventory(configured_members)
            except Exception:
                LOGGER.warning("Stale device inventory maintenance deferred", exc_info=True)
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
                incidents = self.storage.load_domain_incidents(active_only=True)
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
        """Apply and immediately commit settings for non-UI callers/tests."""

        update = self.begin_settings_update(settings)
        update.commit()

    def begin_settings_update(self, settings: AppSettings) -> RuntimeSettingsUpdate:
        """Stage runtime settings without irreversibly pruning SQLite state."""

        with self._lock:
            previous_config_scope = tuple(
                member.ip.strip()
                for member in self.settings.cluster.members
                if member.ip.strip()
            )
            previous_runtime_scope = self.engine.monitoring_scope_ips()
            configured_scope = tuple(
                member.ip.strip()
                for member in settings.cluster.members
                if member.ip.strip()
            )
            # A threshold/polling-only rebuild must retain the scope observed
            # in the most recent explicit cycle (notably demo and test
            # runtimes whose AppSettings remain intentionally unconfigured).
            # A real member-list edit, including clearing it, is authoritative.
            monitoring_scope = (
                previous_runtime_scope
                if configured_scope == previous_config_scope and previous_runtime_scope
                else configured_scope
            )
            previous = (
                self.settings,
                self.detector,
                self.engine,
                self.incident_manager,
            )
            baseline_state = self.baseline_store.snapshot_state()
            previous_pending_transitions = list(self._pending_persistence_transitions)
            previous_pending_acknowledgements = set(
                self._pending_connection_acknowledgements
            )
            detector_state = self.detector.dump_state()
            pending_changes = self.engine.pending_connection_changes()
            incidents = copy.deepcopy(self.incident_manager.events())
            debug_changed = settings.ssh_debug_logging != self.settings.ssh_debug_logging
            low_spec_changed = (
                settings.performance.low_spec_mode
                != self.settings.performance.low_spec_mode
            )
            performance_log_changed = (
                settings.performance.performance_logging
                != self.settings.performance.performance_logging
            )
            try:
                self.settings = copy.deepcopy(settings)
                if debug_changed:
                    self.logging_context.set_ssh_debug_enabled(settings.ssh_debug_logging)
                if low_spec_changed:
                    self.logging_context.set_low_spec_mode(
                        settings.performance.low_spec_mode
                    )
                if performance_log_changed:
                    self.logging_context.set_performance_logging_enabled(
                        settings.performance.performance_logging
                    )
                # Preserve streak state but apply the newly selected thresholds.
                self.detector = self._create_detector(detector_state)
                self.engine = self._create_engine(pending_changes)
                self.incident_manager = self._create_incident_manager(incidents)
                pruned_restored_state = self.engine.reconcile_monitoring_scope(monitoring_scope)
                removed_scope = (
                    set(previous_runtime_scope) - set(monitoring_scope)
                ) | set(pruned_restored_state)
                for member_ip in removed_scope:
                    self.baseline_store.discard(member_ip)
                scope_transitions = self.incident_manager.reconcile_monitoring_scope(
                    monitoring_scope,
                    now=datetime.now(timezone.utc),
                )
            except Exception:
                self._restore_settings_stage(
                    previous,
                    baseline_state,
                    previous_pending_transitions,
                    previous_pending_acknowledgements,
                    debug_changed=debug_changed,
                    low_spec_changed=low_spec_changed,
                    performance_log_changed=performance_log_changed,
                )
                raise
            return RuntimeSettingsUpdate(
                self,
                previous=previous,
                baseline_state=baseline_state,
                pending_transitions=previous_pending_transitions,
                pending_acknowledgements=previous_pending_acknowledgements,
                scope_transitions=scope_transitions,
                debug_changed=debug_changed,
                low_spec_changed=low_spec_changed,
                performance_log_changed=performance_log_changed,
            )

    def _restore_settings_stage(
        self,
        previous: tuple[Any, Any, Any, Any],
        baseline_state: tuple[
            dict[str, ConnectionBaseline],
            dict[str, ConnectionBaseline],
            set[str],
        ],
        pending_transitions: list[Any],
        pending_acknowledgements: set[str],
        *,
        debug_changed: bool,
        low_spec_changed: bool,
        performance_log_changed: bool,
    ) -> None:
        self.settings, self.detector, self.engine, self.incident_manager = previous
        self.baseline_store.restore_state(baseline_state)
        self._pending_persistence_transitions = list(pending_transitions)
        self._pending_connection_acknowledgements = set(pending_acknowledgements)
        if debug_changed:
            try:
                self.logging_context.set_ssh_debug_enabled(self.settings.ssh_debug_logging)
            except Exception:
                LOGGER.critical("SSH debug logger rollback failed", exc_info=True)
        if low_spec_changed:
            try:
                self.logging_context.set_low_spec_mode(
                    self.settings.performance.low_spec_mode
                )
            except Exception:
                LOGGER.critical("Log mode rollback failed", exc_info=True)
        if performance_log_changed:
            try:
                self.logging_context.set_performance_logging_enabled(
                    self.settings.performance.performance_logging
                )
            except Exception:
                LOGGER.critical("Performance logger rollback failed", exc_info=True)

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
        from .collectors.base import SshConnectionOptions

        with self.logging_context.scoped_secrets(
            (credential.username, credential.password, credential.enable_secret)
        ):
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
        poll_started = time.perf_counter()
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
        resolved_credentials = {
            item for item in (mm_credential, cluster_credential) if item is not None
        }
        self.logging_context.replace_current_secrets(
            value
            for credential in resolved_credentials
            for value in (credential.username, credential.password, credential.enable_secret)
        )

        cancel_event = cancellation_event or threading.Event()

        def adapter_factory(options, credential):
            return ArubaSshAdapter(
                options,
                credential,
                cancel_event=cancel_event,
                logger=self.logging_context.ssh_logger,
            )

        mm_collector = MmCollector(
            known_hosts_path=self.paths.known_hosts,
            adapter_factory=adapter_factory,
            cancel_event=cancel_event,
        )
        cluster_collector = ClusterCollector(
            known_hosts_path=self.paths.known_hosts,
            adapter_factory=adapter_factory,
            cancel_event=cancel_event,
        )
        def collect_mm() -> CollectionBundle:
            try:
                return mm_collector.collect(settings.mobility_master, mm_credential)
            except Exception:
                LOGGER.exception("MM collector failed outside the SSH error boundary")
                return self._source_failure_bundle(
                    "mm",
                    settings.mobility_master.management_ip,
                    (SHOW_SWITCHES,),
                    "COLLECTOR_INTERNAL_ERROR",
                    "MM 수집 처리 중 오류가 발생했습니다. 로그를 확인하세요.",
                )

        def collect_cluster() -> CollectionBundle:
            try:
                return cluster_collector.collect(settings.cluster, cluster_credential)
            except Exception:
                LOGGER.exception("Cluster collector failed outside the SSH error boundary")
                return self._source_failure_bundle(
                    "cluster",
                    settings.cluster.primary_controller_ip,
                    (SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP),
                    "COLLECTOR_INTERNAL_ERROR",
                    "Cluster 수집 처리 중 오류가 발생했습니다. 로그를 확인하세요.",
                )

        # MM and cluster are independent read-only sources. Keep both modes on
        # the same bounded executor so one slow source cannot delay the other;
        # low-spec mode still caps collection concurrency at two workers.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="aruba-collector") as executor:
            mm_future = executor.submit(collect_mm) if mm_credential is not None else None
            cluster_future = (
                executor.submit(collect_cluster)
                if cluster_credential is not None
                else None
            )
            if mm_future is not None:
                mm_bundle = mm_future.result()
            if cluster_future is not None:
                cluster_bundle = cluster_future.result()
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
        if self.logging_context.performance_logging_enabled:
            self.logging_context.performance_logger.info(
                "poll_complete duration_ms=%d mode=%s devices=%d errors=%d output_bytes=%d metrics=%s",
                round((time.perf_counter() - poll_started) * 1000),
                "low" if settings.performance.low_spec_mode else "normal",
                len(health.devices),
                len(errors),
                sum(
                    len(value.encode("utf-8", errors="replace"))
                    for value in cycle.raw_outputs.values()
                ),
                current_process_metrics(),
            )
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
                raw_outputs=snapshot_raw_outputs(
                    cycle.raw_outputs,
                    low_spec_mode=self.settings.performance.low_spec_mode,
                ),
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
            self.storage.save_poll_runtime_state(
                detector_state=self.detector.dump_state(),
                device_states=(),
                observed_at=datetime.now(timezone.utc),
            )
        except Exception:
            LOGGER.warning("Detector streak persistence deferred", exc_info=True)

    def _persist_result(self, health: Any, cluster_bundle: CollectionBundle) -> None:
        started = time.perf_counter()
        try:
            failover = None
            if (
                cluster_bundle.primary_failed
                and cluster_bundle.complete
                and cluster_bundle.actual_controller_ip
                and cluster_bundle.actual_controller_ip != cluster_bundle.requested_controller_ip
            ):
                first_error = next((attempt.error_code for attempt in cluster_bundle.attempts if not attempt.success), "")
                failover = (
                    cluster_bundle.requested_controller_ip,
                    cluster_bundle.actual_controller_ip,
                    first_error or "PRIMARY_FAILED",
                    cluster_bundle.failover_at or health.checked_at,
                )
            protected_ips = set(health.monitoring_scope_ips or ())
            protected_ips.update(
                device.ip
                for device in health.devices
                if getattr(device, "last_seen", None) == health.checked_at
            )
            protected_ips.update(
                incident.ip
                for incident in self.incident_manager.active_incidents()
                if getattr(incident, "ip", None)
            )
            protected_ips.update(
                change.member_ip for change in self.engine.pending_connection_changes()
            )
            pruned_ips = self.storage.save_poll_runtime_state(
                detector_state=self.detector.dump_state(),
                device_states=(
                    (device, device.severity is Severity.NORMAL)
                    for device in health.devices
                ),
                observed_at=health.checked_at,
                failover=failover,
                retention_protected_ips=protected_ips,
            )
            if pruned_ips:
                self.engine.forget_known_mm_devices(pruned_ips)
                for ip in pruned_ips:
                    self._last_devices.pop(ip, None)
            self.logging_context.performance_logger.info(
                "persist_runtime duration_ms=%d devices=%d failover=%d pruned_inventory=%d",
                round((time.perf_counter() - started) * 1000),
                len(health.devices),
                int(failover is not None),
                len(pruned_ips),
            )
        except Exception:
            LOGGER.exception("상태 저장 중 오류가 발생했습니다. 현재 점검 결과는 화면에 계속 표시합니다.")

    def _persist_incidents(self, transitions: list[Any], *, engine: Any | None = None) -> None:
        self._pending_persistence_transitions.extend(transitions)
        started = time.perf_counter()
        try:
            self.baseline_store.flush(
                (engine or self.engine).pending_connection_changes(),
                self._pending_connection_acknowledgements,
                self.incident_manager.events(),
                self._pending_persistence_transitions,
            )
            self._pending_persistence_transitions.clear()
            self._pending_connection_acknowledgements.clear()
            compacted = self.incident_manager.compact_inactive()
            self.logging_context.performance_logger.info(
                "persist_domain duration_ms=%d active=%d transitions=%d compacted=%d",
                round((time.perf_counter() - started) * 1000),
                len(self.incident_manager.active_incidents()),
                len(transitions),
                compacted,
            )
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
    """Exercise the real dashboard, one worker cycle, and graceful shutdown.

    The smoke stays isolated from the operator data directory and never opens a
    network connection.  It uses the bundled, sanitized demo fixtures so a
    frozen package proves that the main window can consume a worker result and
    tear its application-owned thread pool down cleanly.
    """

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ArubaMiniDashboardUiSmoke")
    app.setQuitOnLastWindowClosed(False)

    from .demo import DemoPoller
    from .services.correlation_engine import CorrelationEngine

    settings = AppSettings.default()
    worker_pool = QThreadPool(app)
    worker_pool.setMaxThreadCount(1)
    worker_pool.setExpiryTimeout(30_000)
    coordinator = PollCoordinator(
        DemoPoller(CorrelationEngine()),
        settings.effective_poll_interval_seconds,
        thread_pool=worker_pool,
    )
    window = MainWindow(coordinator, settings, demo_mode=True)
    marker = "WINDOWS_QT_UI_OK\nWINDOWS_LIFECYCLE_OK\n"
    completed = False
    timed_out = False

    def finish() -> None:
        nonlocal completed
        if completed or timed_out:
            return
        if not coordinator.shutdown(5000):
            app.quit()
            return
        if output_path is not None:
            _write_atomic_text(output_path, marker)
        completed = True
        window.request_quit()

    def cycle_finished(_result: Any) -> None:
        # MainWindow receives the same signal first and renders the dashboard.
        # Let the QRunnable return before waiting for the pool to become idle.
        QTimer.singleShot(0, finish)

    def cycle_failed(_error: Any) -> None:
        app.quit()

    def timeout() -> None:
        nonlocal timed_out
        if completed:
            return
        timed_out = True
        coordinator.request_shutdown()
        window.tray_icon.hide()
        app.quit()

    coordinator.cycle_finished.connect(cycle_finished)
    coordinator.cycle_failed.connect(cycle_failed)
    window.show()
    QTimer.singleShot(0, coordinator.check_now)
    QTimer.singleShot(10_000, timeout)
    exit_code = int(app.exec())
    workers_stopped = coordinator.shutdown(5000)
    window.tray_icon.hide()
    window.close()
    return exit_code if completed and workers_stopped else 2


def main(argv: list[str] | None = None) -> int:
    startup_started = time.perf_counter()
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

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ArubaMiniDashboard")
    app.setOrganizationName("ArubaMiniDashboard")
    app.setQuitOnLastWindowClosed(False)

    try:
        paths = AppPaths.from_environment().ensure()
    except (AppPathError, OSError) as exc:
        _report_early_startup_issue(
            "프로그램 데이터 폴더 확인 필요",
            str(exc)
            if isinstance(exc, AppPathError)
            else (
                "프로그램 데이터 폴더를 준비하지 못했습니다. "
                "폴더 쓰기 권한과 디스크 상태를 확인한 뒤 다시 실행하세요."
            ),
            critical=True,
        )
        return 2

    # Demo uses in-memory settings/state, but it still shares the data-root log
    # files and desktop/tray identity. Keep one dashboard per data root. Frozen
    # dependency smoke and isolated Qt UI smoke return above and never lock or
    # create production runtime state.
    try:
        instance_lock = _acquire_instance_lock(paths)
    except InstanceAlreadyRunningError as exc:
        _report_early_startup_issue("대시보드가 이미 실행 중입니다", str(exc), critical=False)
        return 3
    except InstanceLockUnavailableError as exc:
        _report_early_startup_issue("단일 실행 보호를 시작할 수 없습니다", str(exc), critical=True)
        return 2

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
    logging_context = setup_logging(
        paths,
        ssh_debug_enabled=settings.ssh_debug_logging,
        low_spec_mode=settings.performance.low_spec_mode,
        performance_logging_enabled=settings.performance.performance_logging,
    )
    credentials = (
        CredentialService(persistent=SessionCredentialStore())
        if args.demo
        else CredentialService()
    )
    runtime = RuntimePoller(settings, paths, credentials, storage, logging_context)
    if logging_context.performance_logging_enabled:
        logging_context.performance_logger.info(
            "startup_runtime_ready duration_ms=%d metrics=%s",
            round((time.perf_counter() - startup_started) * 1000),
            current_process_metrics(),
        )
    collect_cycle: Any = runtime
    if args.demo:
        from .demo import DemoPoller

        collect_cycle = DemoPoller(
            runtime,
            fixture_dir=args.demo_fixtures,
        )
    worker_pool = QThreadPool(app)
    worker_pool.setMaxThreadCount(1)
    worker_pool.setExpiryTimeout(30_000)
    coordinator = PollCoordinator(
        collect_cycle,
        settings.effective_poll_interval_seconds,
        thread_pool=worker_pool,
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
        settings_apply_handler=runtime.begin_settings_update,
        demo_mode=args.demo,
        setup_readiness_check=(
            None if args.demo else (lambda _settings: runtime.can_auto_start())
        ),
        startup_issue=bool(settings_error or storage_error),
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
        instance_lock.unlock()

    app.aboutToQuit.connect(cleanup)
    window.show()
    if logging_context.performance_logging_enabled:
        logging_context.performance_logger.info(
            "startup_window_shown duration_ms=%d metrics=%s",
            round((time.perf_counter() - startup_started) * 1000),
            current_process_metrics(),
        )
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
