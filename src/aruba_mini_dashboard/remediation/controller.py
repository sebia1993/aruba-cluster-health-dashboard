from __future__ import annotations

import copy
import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QSystemTrayIcon

from aruba_mini_dashboard import __version__
from aruba_mini_dashboard.config import AppPaths

from .backend import RemediationBackend
from .models import RemediationCandidate, RemediationOutcome, WorkflowResult
from .report import write_report
from .repository import (
    RemediationRepository,
    RemediationStorageError,
)
from .settings import RemediationSettings, RemediationSettingsStore
from .ui_panel import RemediationPanel
from .workflow import RemediationWorkflow


LOGGER = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class _RemediationWorker(QRunnable):
    def __init__(self, workflow: RemediationWorkflow) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.workflow = workflow
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.workflow.run_workflow()
        except Exception as exc:
            self.signals.failed.emit(exc)
            return
        self.signals.finished.emit(result)


class RemediationFeatureController(QObject):
    """Explicitly composed, independently toggled remediation application service."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window
        self.coordinator = window.coordinator
        self.demo_mode = bool(getattr(window, "demo_mode", False))
        self._closing = False
        self._active = False
        self._cancel_event: threading.Event | None = None
        self._backend: RemediationBackend | None = None
        self._resume_automatic = False
        self._latest_report_path = ""
        self._last_multiple_down: tuple[str, ...] = ()
        self._last_circuit_breaker_reason: dict[str, str] = {}
        self._workers = QThreadPool(self)
        self._workers.setMaxThreadCount(1)
        self._workers.setExpiryTimeout(30_000)
        self._detection_timer = QTimer(self)
        self._detection_timer.setSingleShot(False)
        self._detection_timer.timeout.connect(self._request_detection_poll)

        self.paths = AppPaths.from_environment().ensure()
        self.settings_store = RemediationSettingsStore(self.paths.root)
        settings_error = ""
        if self.demo_mode:
            self.settings = RemediationSettings()
        else:
            try:
                self.settings = self.settings_store.load()
            except RuntimeError as exc:
                self.settings = RemediationSettings()
                settings_error = str(exc)

        self._storage_error = ""
        database = ":memory:" if self.demo_mode else self.paths.root / "remediation" / "remediation.db"
        try:
            self.repository = RemediationRepository(database)
        except RemediationStorageError as exc:
            self.repository = RemediationRepository(":memory:")
            self._storage_error = str(exc)
            self.settings.enabled = False
        self.reports_dir = self.paths.root / "remediation" / "reports"
        recovered = self._recover_and_retry_reports()
        self._latest_report_path = self.repository.latest_report_path()

        self.panel = RemediationPanel(self.window.central_root)
        self.window.central_root_layout.insertWidget(0, self.panel)
        self._connect_signals()
        self.panel.set_checked(self.settings.enabled)
        self.panel.set_report_enabled(bool(self._latest_report_path))

        unavailable = self._unavailable_reason()
        if unavailable:
            self.panel.set_feature_enabled(False)
            self._set_status(unavailable)
        elif settings_error:
            self._set_status(settings_error)
        elif recovered:
            self._set_status(
                f"이전 또는 미완료 실행 {recovered}건의 보고서를 복구했습니다. 최근 보고서를 확인하세요."
            )
        elif self.settings.enabled:
            self._set_status("자동 장애조치 활성화 · MM Down 감지 대기")
        else:
            self._set_status("자동 장애조치 꺼짐")
        self._sync_detection_schedule(immediate=self.settings.enabled)

    def _connect_signals(self) -> None:
        self.panel.enabled_changed.connect(self._on_toggle)
        self.panel.report_requested.connect(self.open_latest_report)
        self.coordinator.cycle_finished.connect(self._on_health_cycle)
        self.coordinator.automatic_changed.connect(self._on_automatic_changed)
        quit_signal = getattr(self.window, "quit_requested", None)
        if quit_signal is not None:
            quit_signal.connect(self.request_stop)

    def _unavailable_reason(self) -> str:
        if self.demo_mode:
            return "데모 모드에서는 자동 장애조치를 실행하지 않습니다."
        if self._storage_error:
            return self._storage_error + " 기존 읽기 전용 점검만 사용하세요."
        if bool(getattr(self.window, "startup_issue", False)):
            return "시작 시 오류가 있어 자동 장애조치를 활성화할 수 없습니다."
        if getattr(self.window, "credential_service", None) is None:
            return "장비 자격 증명 서비스를 사용할 수 없어 자동 장애조치를 활성화할 수 없습니다."
        settings = self.window.settings
        expected = [str(member.ip).strip() for member in settings.cluster.members if str(member.ip).strip()]
        if not str(settings.mobility_master.management_ip).strip() or not expected:
            return "MM 및 Cluster 구성원 설정을 먼저 완료하세요."
        try:
            mm_id = settings.credentials.effective_id("mm", settings)
            cluster_id = settings.credentials.effective_id("cluster", settings)
        except Exception:
            return "MM 및 Cluster 자격 증명 설정을 확인하세요."
        if not mm_id or not cluster_id:
            return "MM 및 Cluster 자격 증명 설정을 먼저 완료하세요."
        return ""

    @Slot(bool)
    def _on_toggle(self, checked: bool) -> None:
        if checked:
            reason = self._unavailable_reason()
            if reason:
                self.panel.set_checked(False)
                QMessageBox.warning(self.window, "자동 장애조치 사용 불가", reason)
                return
            answer = QMessageBox.warning(
                self.window,
                "자동 장애조치 활성화",
                "자동 장애조치를 켜면 신뢰 가능한 MM Down 감지 시 대상 Controller에 "
                "'reload force'를 실행하고, 복구 후 현재 Leader에서 "
                "'cluster-debug bucketmap rebalance'를 자동 실행합니다.\n\n"
                "장비 변경 권한과 승인된 운영 절차를 확인하셨습니까?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.panel.set_checked(False)
                return
            self.settings.enabled = True
            if not self._save_settings():
                self.panel.set_checked(False)
                return
            self._set_status("자동 장애조치 활성화 · MM Down 감지 대기")
            self._sync_detection_schedule(immediate=True)
        else:
            self.settings.enabled = False
            self._save_settings(show_error=False)
            self._detection_timer.stop()
            if self._active:
                self._set_status("자동 장애조치 중단 요청 · 추가 변경 명령을 실행하지 않습니다.")
                self.request_stop()
            else:
                self._set_status("자동 장애조치 꺼짐")

    def _save_settings(self, *, show_error: bool = True) -> bool:
        try:
            self.settings_store.save(self.settings)
            return True
        except RuntimeError as exc:
            if show_error:
                QMessageBox.warning(self.window, "자동 장애조치 설정 저장 실패", str(exc))
            self._set_status(str(exc))
            return False

    @Slot(bool)
    def _on_automatic_changed(self, _enabled: bool) -> None:
        self._sync_detection_schedule()

    def _sync_detection_schedule(self, *, immediate: bool = False) -> None:
        if self._closing or not self.settings.enabled or self._active or self.demo_mode:
            self._detection_timer.stop()
            return
        if self.coordinator.automatic:
            self._detection_timer.stop()
            return
        interval = int(
            getattr(
                self.window.settings,
                "effective_poll_interval_seconds",
                self.window.settings.polling.interval_seconds,
            )
        )
        self._detection_timer.setInterval(max(10, min(interval, 3600)) * 1000)
        if not self._detection_timer.isActive():
            self._detection_timer.start()
        if immediate:
            QTimer.singleShot(0, self._request_detection_poll)

    @Slot()
    def _request_detection_poll(self) -> None:
        if (
            self._closing
            or not self.settings.enabled
            or self._active
            or self.coordinator.automatic
            or self.coordinator.busy
        ):
            return
        self.coordinator.check_now()

    @staticmethod
    def _trusted_mm_cycle(snapshot: Any) -> bool:
        health = getattr(snapshot, "health", snapshot)
        for error in list(getattr(health, "collection_errors", []) or []):
            source = str(getattr(error, "source", "")).casefold().replace("-", "_")
            if "show_switches" in source or source.startswith("mm") or ".mm" in source:
                return False
        devices = [
            device
            for device in list(getattr(health, "devices", []) or [])
            if bool(getattr(device, "is_registered", True))
        ]
        return bool(devices) and all(
            getattr(device, "mm_present", None) is True
            and bool(str(getattr(device, "mm_status", "")).strip())
            for device in devices
        )

    @staticmethod
    def _device_operationally_healthy(device: Any) -> bool:
        state = getattr(
            getattr(device, "controller_state", ""),
            "value",
            getattr(device, "controller_state", ""),
        )
        return (
            str(getattr(device, "mm_status", "")).strip().casefold() == "up"
            and str(state).strip().casefold() == "up"
        )

    @Slot(object)
    def _on_health_cycle(self, snapshot: Any) -> None:
        if not self._trusted_mm_cycle(snapshot):
            if self.settings.enabled and not self._active:
                self._set_status("MM 상태가 완전하지 않아 자동 장애조치 판단을 보류했습니다.")
            return
        health = getattr(snapshot, "health", snapshot)
        registered = [
            device
            for device in list(getattr(health, "devices", []) or [])
            if bool(getattr(device, "is_registered", True))
        ]
        for device in registered:
            ip = str(getattr(device, "ip", ""))
            if not ip:
                continue
            try:
                self.repository.observe_target_recovery(
                    ip,
                    self._device_operationally_healthy(device),
                    self.settings.recovery_unlock_confirmations,
                )
            except Exception:
                LOGGER.warning("Automatic remediation target recovery observation failed", exc_info=True)

        if not self.settings.enabled or self._active or self._closing:
            return
        downs = [
            device
            for device in registered
            if str(getattr(device, "mm_status", "")).strip().casefold() == "down"
        ]
        down_ips = tuple(sorted(str(getattr(device, "ip", "")) for device in downs))
        if len(downs) > 1:
            if down_ips != self._last_multiple_down:
                self._last_multiple_down = down_ips
                message = "여러 Controller가 동시에 Down으로 확인되어 자동 재부팅을 차단했습니다: " + ", ".join(down_ips)
                self._set_status(message)
                self._notify("자동 장애조치 차단", message, critical=True)
            return
        self._last_multiple_down = ()
        if len(downs) != 1:
            self._set_status("자동 장애조치 활성화 · MM Down 감지 대기")
            return
        target = downs[0]
        target_ip = str(getattr(target, "ip", ""))
        if not target_ip:
            return
        reason = self.repository.circuit_breaker_reason(
            target_ip,
            cooldown_seconds=self.settings.cluster_cooldown_seconds,
            max_actions_24h=self.settings.target_max_actions_per_24h,
        )
        if reason:
            if self._last_circuit_breaker_reason.get(target_ip) != reason:
                self._last_circuit_breaker_reason[target_ip] = reason
                self._set_status("자동 장애조치 차단 · " + reason)
                self._notify("자동 장애조치 Circuit Breaker", reason, critical=True)
            return
        self._last_circuit_breaker_reason.pop(target_ip, None)
        candidate = self._candidate(snapshot, target)
        self._start_workflow(candidate)

    def _candidate(self, snapshot: Any, target: Any) -> RemediationCandidate:
        health = getattr(snapshot, "health", snapshot)
        checked_at = getattr(health, "checked_at", datetime.now(timezone.utc))
        if not isinstance(checked_at, datetime):
            checked_at = datetime.now(timezone.utc)
        target_ip = str(getattr(target, "ip", ""))
        target_alias = str(
            getattr(target, "alias", "") or getattr(target, "hostname", "") or target_ip
        )
        incident_key = ""
        for incident in list(getattr(snapshot, "active_incidents", []) or []):
            incident_type = getattr(
                getattr(incident, "incident_type", ""),
                "value",
                getattr(incident, "incident_type", ""),
            )
            if str(incident_type) == "mm_down" and str(getattr(incident, "ip", "")) == target_ip:
                incident_key = str(getattr(incident, "incident_id", ""))
                break
        incident_key = incident_key or f"mm-down|{target_ip}|{checked_at.astimezone(timezone.utc).isoformat()}"
        expected = tuple(
            dict.fromkeys(
                str(member.ip).strip()
                for member in self.window.settings.cluster.members
                if str(member.ip).strip()
            )
        )
        members: dict[str, dict[str, Any]] = {}
        for device in list(getattr(health, "devices", []) or []):
            if not bool(getattr(device, "is_registered", True)):
                continue
            ip = str(getattr(device, "ip", ""))
            members[ip] = {
                "mm_status": str(getattr(device, "mm_status", "")),
                "status": "-",
                "active_clients": getattr(device, "active_clients", None),
                "standby_clients": getattr(device, "standby_clients", None),
                "connection_type": getattr(device, "connection_type", None),
            }
        return RemediationCandidate(
            incident_key=incident_key,
            target_ip=target_ip,
            target_alias=target_alias,
            cluster_name=str(self.window.settings.cluster.name),
            expected_member_ips=expected,
            detected_at=checked_at,
            detected_snapshot={
                "captured_at": checked_at,
                "source": "health_dashboard_cycle",
                "members": members,
            },
        )

    def _start_workflow(self, candidate: RemediationCandidate) -> None:
        if self._active or self._closing:
            return
        self._active = True
        self._detection_timer.stop()
        self._cancel_event = threading.Event()
        self._resume_automatic = bool(self.coordinator.automatic)
        if self._resume_automatic:
            self.coordinator.pause_automatic()
        self._set_normal_controls_enabled(False)
        self._set_status(f"자동 장애조치 진행 중 · {candidate.target_alias or candidate.target_ip}")
        self._notify(
            "자동 장애조치 시작",
            f"{candidate.target_alias or candidate.target_ip} ({candidate.target_ip}) 자동 장애조치를 시작합니다.",
            critical=True,
        )
        backend = RemediationBackend(
            copy.deepcopy(self.window.settings),
            self.window.credential_service,
            known_hosts_path=self.paths.known_hosts,
            cancel_event=self._cancel_event,
        )
        candidate = replace(
            candidate,
            configuration_fingerprint=backend.configuration_fingerprint,
        )
        self._backend = backend
        workflow = RemediationWorkflow(
            candidate,
            backend,
            self.repository,
            copy.deepcopy(self.settings),
            reports_dir=self.reports_dir,
            app_version=f"v{__version__}",
            cancel_event=self._cancel_event,
        )
        worker = _RemediationWorker(workflow)
        worker.signals.finished.connect(self._on_workflow_finished)
        worker.signals.failed.connect(self._on_workflow_failed)
        try:
            self._workers.start(worker)
        except Exception:
            self._active = False
            self._backend = None
            self._set_normal_controls_enabled(True)
            self._set_status("자동 장애조치 작업을 시작하지 못했습니다.")
            self._notify("자동 장애조치 시작 실패", "백그라운드 작업을 시작하지 못했습니다.", critical=True)
            self._restore_after_workflow()

    @Slot(object)
    def _on_workflow_finished(self, result: WorkflowResult) -> None:
        self._active = False
        self._backend = None
        self._cancel_event = None
        self._set_normal_controls_enabled(True)
        if result.report_path:
            self._latest_report_path = result.report_path
            self.panel.set_report_enabled(True)
        if result.outcome is RemediationOutcome.COMPLETED:
            label, critical = "정상 완료", False
        elif result.outcome is RemediationOutcome.PARTIAL:
            label, critical = "부분 완료", True
        elif result.outcome is RemediationOutcome.STOPPED:
            label, critical = "중단", True
        else:
            label, critical = "실패", True
        report_note = f"\n보고서: {result.report_path}" if result.report_path else ""
        if result.report_error:
            report_note += f"\n보고서 생성 오류: {result.report_error} (다음 시작 시 재생성)"
        self._set_status(f"{label} · {result.message}")
        self._notify(f"자동 장애조치 {label}", result.message + report_note, critical=critical)
        if (
            self.settings.pause_after_non_success
            and result.outcome is not RemediationOutcome.COMPLETED
        ):
            self.settings.enabled = False
            self._save_settings(show_error=False)
            self.panel.set_checked(False)
            self._set_status(f"{label} · 자동 장애조치가 안전상 일시정지되었습니다. 보고서를 확인 후 다시 켜세요.")
        self._restore_after_workflow()

    @Slot(object)
    def _on_workflow_failed(self, error: BaseException) -> None:
        LOGGER.error(
            "Unhandled automatic remediation worker failure",
            exc_info=(type(error), error, error.__traceback__),
        )
        self._active = False
        self._backend = None
        self._cancel_event = None
        self._set_normal_controls_enabled(True)
        self.settings.enabled = False
        self._save_settings(show_error=False)
        self.panel.set_checked(False)
        self._set_status("예기치 않은 오류로 자동 장애조치가 종료되어 기능을 일시정지했습니다.")
        self._notify(
            "자동 장애조치 오류",
            "예기치 않은 오류로 작업이 종료되었습니다. 기능을 일시정지했으며 저장된 보고서와 로그를 확인하세요.",
            critical=True,
        )
        self._restore_after_workflow()

    def _restore_after_workflow(self) -> None:
        if self._closing:
            return
        resume = self._resume_automatic
        self._resume_automatic = False
        if resume and not self.coordinator.automatic:
            QTimer.singleShot(0, self.coordinator.start_automatic)
        self._sync_detection_schedule()

    def _set_normal_controls_enabled(self, enabled: bool) -> None:
        names = (
            "check_now_button", "start_button", "pause_button", "settings_button",
            "compact_check_now_button", "compact_auto_button", "compact_settings_button",
            "tray_check_now_action", "tray_start_action", "tray_pause_action", "tray_settings_action",
        )
        for name in names:
            item = getattr(self.window, name, None)
            if item is not None:
                item.setEnabled(enabled)

    def _notify(self, title: str, message: str, *, critical: bool) -> None:
        tray = getattr(self.window, "tray_icon", None)
        if tray is not None and tray.isVisible():
            icon = (
                QSystemTrayIcon.MessageIcon.Critical
                if critical
                else QSystemTrayIcon.MessageIcon.Information
            )
            tray.showMessage(title, message, icon, 15_000)
        self.window.statusBar().showMessage(message, 15_000)

    def _set_status(self, message: str) -> None:
        self.panel.set_status(str(message))

    @Slot()
    def open_latest_report(self) -> None:
        path = self._latest_report_path or self.repository.latest_report_path()
        if not path or not Path(path).is_file():
            QMessageBox.information(self.window, "장애조치 보고서", "열 수 있는 장애조치 보고서가 없습니다.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _recover_and_retry_reports(self) -> int:
        run_ids = list(dict.fromkeys([
            *self.repository.recover_interrupted_runs(),
            *self.repository.pending_report_run_ids(),
        ]))
        recovered = 0
        for run_id in run_ids:
            try:
                run = self.repository.load_run(run_id)
                path = write_report(
                    run,
                    self.reports_dir,
                    timezone_name=self.settings.report_timezone,
                )
                run.report_path = str(path)
                run.report_error = ""
                run.report_pending = False
                self.repository.update_run(run)
                self._latest_report_path = str(path)
                recovered += 1
            except Exception:
                LOGGER.warning("Pending remediation report generation failed", exc_info=True)
        return recovered

    @Slot()
    def request_stop(self) -> None:
        event = self._cancel_event
        if event is not None:
            event.set()
        backend = self._backend
        if backend is not None:
            backend.cancel_active_connections()

    @Slot()
    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._detection_timer.stop()
        self.request_stop()
        if self._workers.waitForDone(5_000):
            try:
                self.repository.close()
            except Exception:
                LOGGER.debug("Remediation repository close failed", exc_info=True)
        else:
            LOGGER.warning(
                "Automatic remediation worker did not stop within the shutdown grace period; "
                "leaving the audit store open until process exit"
            )
