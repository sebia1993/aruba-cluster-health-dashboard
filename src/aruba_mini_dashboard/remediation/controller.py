from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
)

from aruba_mini_dashboard import __version__
from aruba_mini_dashboard.config import AppPaths

from .backend import RemediationBackend
from .models import RemediationCandidate, RemediationOutcome, WorkflowResult
from .report import write_report
from .repository import RemediationRepository
from .settings import RemediationSettings, RemediationSettingsStore
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
    """Main-window integration for the independently toggled remediation workflow."""

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
        database = ":memory:" if self.demo_mode else self.paths.root / "remediation" / "remediation.db"
        self.repository = RemediationRepository(database)
        self.reports_dir = self.paths.root / "remediation" / "reports"
        interrupted = self._recover_interrupted_runs()
        self._latest_report_path = self.repository.latest_report_path()

        self._build_panel()
        self._connect_signals()
        self._set_toggle_checked(self.settings.enabled)
        self.report_button.setEnabled(bool(self._latest_report_path))

        unavailable = self._unavailable_reason()
        if unavailable:
            self.toggle.setEnabled(False)
            self._set_status(unavailable)
        elif settings_error:
            self._set_status(settings_error)
        elif interrupted:
            self._set_status(
                f"이전 실행 {interrupted}건을 비정상 중단으로 복구했습니다. 최근 보고서를 확인하세요."
            )
        elif self.settings.enabled:
            self._set_status("자동 장애조치 활성화 · MM Down 감지 대기")
        else:
            self._set_status("자동 장애조치 꺼짐")
        self._sync_detection_schedule(immediate=self.settings.enabled)

    def _build_panel(self) -> None:
        self.panel = QFrame(self.window.central_root)
        self.panel.setObjectName("automaticRemediationPanel")
        self.panel.setStyleSheet(
            "QFrame#automaticRemediationPanel { background:#F6F8FB; border-bottom:1px solid #D9E2EC; }"
        )
        layout = QHBoxLayout(self.panel)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        self.toggle = QCheckBox("자동 장애조치", self.panel)
        self.toggle.setAccessibleName("자동 장애조치 켜기 또는 끄기")
        self.toggle.setToolTip(
            "MM Down Controller에 reload force를 실행하고 복구 후 현재 Leader에서 Cluster 재분배를 수행합니다."
        )
        layout.addWidget(self.toggle)
        self.status_label = QLabel("", self.panel)
        self.status_label.setStyleSheet("color:#52606D;")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.status_label.setTextInteractionFlags(self.status_label.textInteractionFlags())
        layout.addWidget(self.status_label, 1)
        self.report_button = QPushButton("최근 조치 보고서", self.panel)
        self.report_button.setToolTip("가장 최근에 생성된 HTML 장애조치 보고서를 엽니다.")
        layout.addWidget(self.report_button)
        self.window.central_root_layout.insertWidget(0, self.panel)

    def _connect_signals(self) -> None:
        self.toggle.toggled.connect(self._on_toggle)
        self.report_button.clicked.connect(self.open_latest_report)
        self.coordinator.cycle_finished.connect(self._on_health_cycle)
        self.coordinator.automatic_changed.connect(self._on_automatic_changed)
        quit_signal = getattr(self.window, "quit_requested", None)
        if quit_signal is not None:
            quit_signal.connect(self.request_stop)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _unavailable_reason(self) -> str:
        if self.demo_mode:
            return "데모 모드에서는 자동 장애조치를 실행하지 않습니다."
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
        self._sync_peer_actions(checked)
        if checked:
            reason = self._unavailable_reason()
            if reason:
                self._set_toggle_checked(False)
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
                self._set_toggle_checked(False)
                return
            self.settings.enabled = True
            if not self._save_settings():
                self._set_toggle_checked(False)
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

    def _set_toggle_checked(self, checked: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(bool(checked))
        self.toggle.blockSignals(False)
        self._sync_peer_actions(bool(checked))

    def _sync_peer_actions(self, checked: bool) -> None:
        for name in ("remediation_menu_action", "remediation_tray_action"):
            action = getattr(self.window, name, None)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(bool(checked))
                action.blockSignals(False)

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
            if str(getattr(device, "mm_status", "")).strip().casefold() == "up":
                try:
                    self.repository.release_target(str(getattr(device, "ip", "")))
                except Exception:
                    LOGGER.warning("Automatic remediation target release failed", exc_info=True)
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
        if not target_ip or self.repository.is_target_claimed(target_ip):
            return
        candidate = self._candidate(snapshot, target)
        self._start_workflow(candidate)

    def _candidate(self, snapshot: Any, target: Any) -> RemediationCandidate:
        health = getattr(snapshot, "health", snapshot)
        checked_at = getattr(health, "checked_at", datetime.now(timezone.utc))
        if not isinstance(checked_at, datetime):
            checked_at = datetime.now(timezone.utc)
        target_ip = str(getattr(target, "ip", ""))
        target_alias = str(
            getattr(target, "alias", "")
            or getattr(target, "hostname", "")
            or target_ip
        )
        incident_key = ""
        for incident in list(getattr(snapshot, "active_incidents", []) or []):
            incident_type = getattr(getattr(incident, "incident_type", ""), "value", getattr(incident, "incident_type", ""))
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
            self.report_button.setEnabled(True)
        if result.outcome is RemediationOutcome.COMPLETED:
            label = "정상 완료"
            critical = False
        elif result.outcome is RemediationOutcome.PARTIAL:
            label = "부분 완료"
            critical = True
        elif result.outcome is RemediationOutcome.STOPPED:
            label = "중단"
            critical = True
        else:
            label = "실패"
            critical = True
        self._set_status(f"{label} · {result.message}")
        report_note = f"\n보고서: {result.report_path}" if result.report_path else ""
        self._notify(f"자동 장애조치 {label}", result.message + report_note, critical=critical)
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
        self._set_status("예기치 않은 오류로 자동 장애조치 작업이 종료되었습니다.")
        self._notify(
            "자동 장애조치 오류",
            "예기치 않은 오류로 자동 장애조치 작업이 종료되었습니다.",
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
            "check_now_button",
            "start_button",
            "pause_button",
            "compact_check_now_button",
            "compact_auto_button",
            "tray_check_now_action",
            "tray_start_action",
            "tray_pause_action",
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
        self.status_label.setText(str(message))
        self.status_label.setToolTip(str(message))

    @Slot()
    def open_latest_report(self) -> None:
        path = self._latest_report_path or self.repository.latest_report_path()
        if not path or not Path(path).is_file():
            QMessageBox.information(
                self.window,
                "장애조치 보고서",
                "열 수 있는 장애조치 보고서가 없습니다.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _recover_interrupted_runs(self) -> int:
        run_ids = self.repository.recover_interrupted_runs()
        for run_id in run_ids:
            try:
                run = self.repository.load_run(run_id)
                path = write_report(
                    run,
                    self.reports_dir,
                    timezone_name=self.settings.report_timezone,
                )
                run.report_path = str(path)
                self.repository.update_run(run)
                self._latest_report_path = str(path)
            except Exception:
                LOGGER.warning("Interrupted remediation report generation failed", exc_info=True)
        return len(run_ids)

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
