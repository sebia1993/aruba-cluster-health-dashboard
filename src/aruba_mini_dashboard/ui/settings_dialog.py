from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.credentials import CredentialError, DeviceCredential


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, repr=False)
class ConnectionTestRequest:
    settings: AppSettings
    credential: DeviceCredential | None = None

    def __repr__(self) -> str:
        return "ConnectionTestRequest(settings=[NON_SECRET], credential=[REDACTED])"

from .widgets import NoWheelSlider


class _CredentialFields(QGroupBox):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        layout = QFormLayout(self)
        self.username = QLineEdit(self)
        self.username.setPlaceholderText("사용자 ID")
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("변경할 때만 입력")
        self.enable_secret = QLineEdit(self)
        self.enable_secret.setEchoMode(QLineEdit.Password)
        self.enable_secret.setPlaceholderText("선택 입력")
        layout.addRow("사용자 ID", self.username)
        layout.addRow("비밀번호", self.password)
        layout.addRow("Enable 비밀번호", self.enable_secret)

    def has_new_value(self) -> bool:
        return bool(self.username.text().strip() or self.password.text() or self.enable_secret.text())

    def credential(self, current: DeviceCredential | None = None) -> DeviceCredential:
        """Build a credential, retaining omitted values from the current one."""

        return DeviceCredential(
            username=self.username.text().strip() or (current.username if current else ""),
            password=self.password.text() or (current.password if current else ""),
            enable_secret=(
                self.enable_secret.text()
                if self.enable_secret.text()
                else (current.enable_secret if current else "")
            ),
        )


class SettingsDialog(QDialog):
    connection_test_requested = Signal(str, object)
    sound_test_requested = Signal()
    notification_test_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        credential_service: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aruba 미니 대시보드 설정")
        self.resize(700, 650)
        self.setMinimumSize(580, 520)
        self.settings = copy.deepcopy(settings)
        self.credential_service = credential_service
        self._staged_new_credential_ids: list[str] = []
        self._staged_old_credential_ids: list[str] = []

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self._build_devices_tab()
        self._build_detection_tab()
        self._build_polling_tab()
        self._build_notifications_tab()
        self._build_ui_tab()
        self._build_advanced_tab()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.Cancel).setText("취소")
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    def _build_devices_tab(self) -> None:
        contents = QWidget(self)
        outer = QVBoxLayout(contents)

        mm_box = QGroupBox("Mobility Master", contents)
        mm_form = QFormLayout(mm_box)
        mm = self.settings.mobility_master
        self.mm_ip = QLineEdit(mm.management_ip)
        self.mm_name = QLineEdit(mm.display_name)
        self.mm_port = self._spin(1, 65535, mm.ssh_port)
        self.mm_connect_timeout = self._spin(1, 600, mm.connect_timeout_seconds, "초")
        self.mm_command_timeout = self._spin(1, 600, mm.command_timeout_seconds, "초")
        self.mm_retries = self._spin(0, 10, mm.retries, "회")
        self.mm_enable = QCheckBox("Enable 진입 필요")
        self.mm_enable.setChecked(mm.enable_required)
        mm_form.addRow("관리 IP", self.mm_ip)
        mm_form.addRow("표시 이름", self.mm_name)
        mm_form.addRow("SSH 포트", self.mm_port)
        mm_form.addRow("연결 제한시간", self.mm_connect_timeout)
        mm_form.addRow("명령 제한시간", self.mm_command_timeout)
        mm_form.addRow("재시도 횟수", self.mm_retries)
        mm_form.addRow("", self.mm_enable)
        outer.addWidget(mm_box)

        cluster_box = QGroupBox("Aruba 7240XM 클러스터", contents)
        cluster_layout = QVBoxLayout(cluster_box)
        cluster_form = QFormLayout()
        cluster = self.settings.cluster
        self.cluster_name = QLineEdit(cluster.name)
        cluster_form.addRow("클러스터 명칭", self.cluster_name)
        cluster_layout.addLayout(cluster_form)

        member_grid = QGridLayout()
        member_grid.addWidget(QLabel("구성원"), 0, 0)
        member_grid.addWidget(QLabel("IP"), 0, 1)
        member_grid.addWidget(QLabel("장비 별칭"), 0, 2)
        self.member_ips: list[QLineEdit] = []
        self.member_aliases: list[QLineEdit] = []
        members = list(cluster.members) + [ClusterMemberSettings() for _ in range(4)]
        for index, member in enumerate(members[:4], start=1):
            ip_edit = QLineEdit(member.ip)
            alias_edit = QLineEdit(member.alias)
            ip_edit.setPlaceholderText(f"WLC-{index:02d} IP")
            alias_edit.setPlaceholderText(f"WLC-{index:02d}")
            self.member_ips.append(ip_edit)
            self.member_aliases.append(alias_edit)
            member_grid.addWidget(QLabel(str(index)), index, 0)
            member_grid.addWidget(ip_edit, index, 1)
            member_grid.addWidget(alias_edit, index, 2)
        cluster_layout.addLayout(member_grid)

        endpoint_form = QFormLayout()
        self.primary_ip = QLineEdit(cluster.primary_controller_ip)
        self.fallback_ips = QLineEdit(", ".join(cluster.fallback_controller_ips))
        self.fallback_ips.setPlaceholderText("쉼표로 구분, 순서대로 시도")
        self.cluster_port = self._spin(1, 65535, cluster.ssh_port)
        self.cluster_connect_timeout = self._spin(1, 600, cluster.connect_timeout_seconds, "초")
        self.cluster_command_timeout = self._spin(1, 600, cluster.command_timeout_seconds, "초")
        self.cluster_retries = self._spin(0, 10, cluster.retries, "회")
        self.cluster_enable = QCheckBox("Enable 진입 필요")
        self.cluster_enable.setChecked(cluster.enable_required)
        endpoint_form.addRow("Primary Controller IP", self.primary_ip)
        endpoint_form.addRow("대체 Controller IP", self.fallback_ips)
        endpoint_form.addRow("SSH 포트", self.cluster_port)
        endpoint_form.addRow("연결 제한시간", self.cluster_connect_timeout)
        endpoint_form.addRow("명령 제한시간", self.cluster_command_timeout)
        endpoint_form.addRow("재시도 횟수", self.cluster_retries)
        endpoint_form.addRow("", self.cluster_enable)
        cluster_layout.addLayout(endpoint_form)
        outer.addWidget(cluster_box)

        credentials_box = QGroupBox("접속 계정", contents)
        credentials_layout = QVBoxLayout(credentials_box)
        self.shared_credentials = QCheckBox("MM과 WLC에서 같은 계정 사용")
        self.shared_credentials.setChecked(self.settings.credentials.use_shared_credentials)
        self.session_only = QCheckBox("세션 전용 자격 증명 (프로그램 종료 시 삭제)")
        self._initial_session_only = self._configured_session_only()
        self.session_only.setChecked(self._initial_session_only)
        credentials_layout.addWidget(self.shared_credentials)
        credentials_layout.addWidget(self.session_only)
        self.shared_fields = _CredentialFields("공통 계정")
        self.mm_fields = _CredentialFields("MM 계정")
        self.cluster_fields = _CredentialFields("WLC 계정")
        credentials_layout.addWidget(self.shared_fields)
        credentials_layout.addWidget(self.mm_fields)
        credentials_layout.addWidget(self.cluster_fields)
        self.shared_credentials.toggled.connect(self._update_credential_mode)
        self._update_credential_mode(self.shared_credentials.isChecked())
        outer.addWidget(credentials_box)

        tests = QHBoxLayout()
        self.mm_test_button = QPushButton("MM 연결 테스트")
        self.cluster_test_button = QPushButton("클러스터 연결 테스트")
        self.mm_test_button.clicked.connect(lambda: self._emit_connection_test("mm"))
        self.cluster_test_button.clicked.connect(lambda: self._emit_connection_test("cluster"))
        tests.addWidget(self.mm_test_button)
        tests.addWidget(self.cluster_test_button)
        tests.addStretch(1)
        outer.addLayout(tests)
        outer.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(contents)
        self.tabs.addTab(scroll, "장비·자격 증명")

    def _build_detection_tab(self) -> None:
        page = QWidget(self)
        form = QFormLayout(page)
        d = self.settings.detection
        self.low_threshold = self._spin(0, 1_000_000, d.low_client_threshold)
        self.anomaly_cycles = self._spin(1, 100, d.anomaly_cycles, "회")
        self.recovery_cycles = self._spin(1, 100, d.recovery_cycles, "회")
        self.comparison_mode = QComboBox()
        self.comparison_mode.addItem("절대값과 상대 비교 함께 사용", "absolute_and_relative")
        self.comparison_mode.addItem("절대값만 사용", "absolute_only")
        self.comparison_mode.setCurrentIndex(max(0, self.comparison_mode.findData(d.comparison_mode)))
        self.relative_ratio = self._spin(1, 100, d.relative_ratio_percent, "%")
        self.minimum_total = self._spin(0, 1_000_000, d.minimum_cluster_active_clients)
        self.minimum_peer = self._spin(0, 1_000_000, d.minimum_peer_median)
        self.missing_cycles = self._spin(1, 100, d.missing_cycles, "회")
        form.addRow("Low Client Threshold", self.low_threshold)
        form.addRow("연속 이상 감지", self.anomaly_cycles)
        form.addRow("복구 확인", self.recovery_cycles)
        form.addRow("감지 모드", self.comparison_mode)
        form.addRow("상대 비교 기준", self.relative_ratio)
        form.addRow("클러스터 최소 전체 Active", self.minimum_total)
        form.addRow("Peer 중앙값 최소", self.minimum_peer)
        form.addRow("행 누락 활성화", self.missing_cycles)
        self.tabs.addTab(page, "감지 기준")

    def _build_polling_tab(self) -> None:
        page = QWidget(self)
        form = QFormLayout(page)
        self.poll_interval = self._spin(10, 3600, self.settings.polling.interval_seconds, "초")
        self.auto_start = QCheckBox("프로그램 시작 후 자동 점검 복원")
        self.auto_start.setChecked(self.settings.polling.automatic_enabled)
        form.addRow("점검 주기", self.poll_interval)
        form.addRow("", self.auto_start)
        note = QLabel("점검 중 예약 시각이 도래하면 해당 회차는 건너뛰며, 중복 SSH 작업을 실행하지 않습니다.")
        note.setWordWrap(True)
        form.addRow(note)
        self.tabs.addTab(page, "점검")

    def _build_notifications_tab(self) -> None:
        page = QWidget(self)
        form = QFormLayout(page)
        n = self.settings.notifications
        self.notify_new = QCheckBox("신규 장애 즉시 알림")
        self.notify_new.setChecked(n.notify_new_incidents)
        self.repeat_unack = QCheckBox("미확인 장애 반복 알림")
        self.repeat_unack.setChecked(n.repeat_unacknowledged)
        self.repeat_minutes = self._spin(1, 1440, n.repeat_interval_minutes, "분")
        self.sound_enabled = QCheckBox("알림음 사용")
        self.sound_enabled.setChecked(n.sound_enabled)
        self.recovery_notifications = QCheckBox("복구 알림")
        self.recovery_notifications.setChecked(n.recovery_notifications)
        form.addRow(self.notify_new)
        form.addRow(self.repeat_unack)
        form.addRow("반복 알림 간격", self.repeat_minutes)
        form.addRow(self.sound_enabled)
        form.addRow(self.recovery_notifications)
        test_row = QWidget(page)
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        sound_test = QPushButton("알림음 테스트")
        windows_test = QPushButton("Windows 알림 테스트")
        sound_test.clicked.connect(self.sound_test_requested)
        windows_test.clicked.connect(self.notification_test_requested)
        test_layout.addWidget(sound_test)
        test_layout.addWidget(windows_test)
        test_layout.addStretch(1)
        form.addRow("테스트", test_row)
        self.tabs.addTab(page, "알림")

    def _build_ui_tab(self) -> None:
        page = QWidget(self)
        form = QFormLayout(page)
        ui = self.settings.ui
        self.always_on_top = QCheckBox("항상 위에 표시")
        self.always_on_top.setChecked(ui.always_on_top)
        opacity_row = QWidget(page)
        opacity_layout = QHBoxLayout(opacity_row)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        self.opacity = NoWheelSlider(Qt.Horizontal)
        self.opacity.setRange(40, 100)
        self.opacity.setValue(ui.opacity_percent)
        self.opacity_value = QLabel(f"{ui.opacity_percent}%")
        self.opacity.valueChanged.connect(lambda value: self.opacity_value.setText(f"{value}%"))
        opacity_layout.addWidget(self.opacity, 1)
        opacity_layout.addWidget(self.opacity_value)
        reset = QPushButton("기본값 복원")
        reset.clicked.connect(self._reset_ui_defaults)
        form.addRow(self.always_on_top)
        form.addRow("창 투명도", opacity_row)
        form.addRow("", reset)
        note = QLabel("마우스 휠로는 투명도가 변경되지 않습니다. 창 위치와 크기는 종료 시 저장됩니다.")
        note.setWordWrap(True)
        form.addRow(note)
        self.tabs.addTab(page, "화면")

    def _build_advanced_tab(self) -> None:
        page = QWidget(self)
        form = QFormLayout(page)
        self.ssh_debug_logging = QCheckBox("SSH 디버그 로그 사용")
        self.ssh_debug_logging.setChecked(self.settings.ssh_debug_logging)
        warning = QLabel(
            "파싱 실패 분석을 위해 비식별화되지 않은 장비 출력 일부가 ssh_debug.log에 "
            "기록될 수 있습니다. 필요한 기간에만 사용하고 공유 전에 내용을 확인하세요."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #8A5A00;")
        form.addRow(self.ssh_debug_logging)
        form.addRow(warning)
        self.tabs.addTab(page, "고급")

    def _update_credential_mode(self, shared: bool) -> None:
        self.shared_fields.setVisible(shared)
        self.mm_fields.setVisible(not shared)
        self.cluster_fields.setVisible(not shared)

    def _reset_ui_defaults(self) -> None:
        self.always_on_top.setChecked(False)
        self.opacity.setValue(100)

    def _emit_connection_test(self, role: str) -> None:
        try:
            candidate = self._collect_settings(save_credentials=False)
            candidate.validate()
            fields = (
                self.shared_fields
                if self.shared_credentials.isChecked()
                else (self.mm_fields if role == "mm" else self.cluster_fields)
            )
            transient = None
            if fields.has_new_value():
                credential_id = candidate.credentials.effective_id(role, candidate) or None
                transient = self._credential_with_current(fields, credential_id)
        except Exception as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        self.connection_test_requested.emit(role, ConnectionTestRequest(candidate, transient))

    def _apply(self) -> None:
        try:
            candidate = self._collect_settings(save_credentials=True)
            candidate.validate()
        except Exception as exc:
            self.rollback_staged_credentials()
            QMessageBox.warning(self, "설정을 저장할 수 없음", str(exc))
            return
        self.settings = candidate
        self.accept()

    def _collect_settings(self, *, save_credentials: bool) -> AppSettings:
        settings = copy.deepcopy(self.settings)
        mm = settings.mobility_master
        mm.management_ip = self.mm_ip.text().strip()
        mm.display_name = self.mm_name.text().strip() or "Aruba Mobility Master"
        mm.ssh_port = self.mm_port.value()
        mm.connect_timeout_seconds = self.mm_connect_timeout.value()
        mm.command_timeout_seconds = self.mm_command_timeout.value()
        mm.retries = self.mm_retries.value()
        mm.enable_required = self.mm_enable.isChecked()

        cluster = settings.cluster
        cluster.name = self.cluster_name.text().strip() or "Aruba 7240XM Cluster"
        cluster.members = [
            ClusterMemberSettings(ip=ip.text().strip(), alias=alias.text().strip())
            for ip, alias in zip(self.member_ips, self.member_aliases, strict=True)
        ]
        cluster.primary_controller_ip = self.primary_ip.text().strip()
        cluster.fallback_controller_ips = [
            item.strip() for item in self.fallback_ips.text().split(",") if item.strip()
        ]
        cluster.ssh_port = self.cluster_port.value()
        cluster.connect_timeout_seconds = self.cluster_connect_timeout.value()
        cluster.command_timeout_seconds = self.cluster_command_timeout.value()
        cluster.retries = self.cluster_retries.value()
        cluster.enable_required = self.cluster_enable.isChecked()

        settings.credentials.use_shared_credentials = self.shared_credentials.isChecked()
        settings.credentials.session_only = self.session_only.isChecked()
        settings.polling.interval_seconds = self.poll_interval.value()
        settings.polling.automatic_enabled = self.auto_start.isChecked()
        settings.detection.low_client_threshold = self.low_threshold.value()
        settings.detection.anomaly_cycles = self.anomaly_cycles.value()
        settings.detection.recovery_cycles = self.recovery_cycles.value()
        settings.detection.comparison_mode = self.comparison_mode.currentData()
        settings.detection.relative_ratio_percent = self.relative_ratio.value()
        settings.detection.minimum_cluster_active_clients = self.minimum_total.value()
        settings.detection.minimum_peer_median = self.minimum_peer.value()
        settings.detection.missing_cycles = self.missing_cycles.value()
        settings.notifications.notify_new_incidents = self.notify_new.isChecked()
        settings.notifications.repeat_unacknowledged = self.repeat_unack.isChecked()
        settings.notifications.repeat_interval_minutes = self.repeat_minutes.value()
        settings.notifications.sound_enabled = self.sound_enabled.isChecked()
        settings.notifications.recovery_notifications = self.recovery_notifications.isChecked()
        settings.ui.always_on_top = self.always_on_top.isChecked()
        settings.ui.opacity_percent = self.opacity.value()
        settings.ssh_debug_logging = self.ssh_debug_logging.isChecked()

        if save_credentials:
            # Validate every non-secret field before touching Credential
            # Manager. Invalid IPs/forms must have zero credential side effects.
            settings.validate()
            credential_fields = (
                [self.shared_fields]
                if self.shared_credentials.isChecked()
                else [self.mm_fields, self.cluster_fields]
            )
            if self.session_only.isChecked() != self._initial_session_only and not all(
                field.has_new_value() for field in credential_fields
            ):
                raise RuntimeError(
                    "자격 증명 저장 방식을 변경하려면 해당 계정의 사용자 ID와 비밀번호를 다시 입력하세요."
                )
            self._stage_credentials(settings)
            if self.session_only.isChecked() and any(
                field.has_new_value()
                for field in (
                    [self.shared_fields]
                    if self.shared_credentials.isChecked()
                    else [self.mm_fields, self.cluster_fields]
                )
            ):
                # A session credential identifier cannot be resolved after a
                # process restart, so never persist an automatic-start intent.
                settings.polling.automatic_enabled = False
        return settings

    def _stage_credentials(self, settings: AppSettings) -> None:
        self.rollback_staged_credentials()
        try:
            self._save_credentials(settings)
        except Exception:
            self.rollback_staged_credentials()
            raise

    def _save_credentials(self, settings: AppSettings) -> None:
        fields = [self.shared_fields] if self.shared_credentials.isChecked() else [self.mm_fields, self.cluster_fields]
        if not any(field.has_new_value() for field in fields):
            return
        if self.credential_service is None:
            raise RuntimeError("자격 증명 저장소를 사용할 수 없습니다. 입력값은 저장되지 않았습니다.")
        if self.shared_credentials.isChecked():
            current_id = settings.credentials.shared_credential_id or None
            credential_id = self.credential_service.save(
                self._credential_with_current(self.shared_fields, current_id),
                session_only=self.session_only.isChecked(),
                credential_id=None,
            )
            self._record_staged_credential(credential_id, current_id)
            settings.credentials.shared_credential_id = credential_id
            return
        if self.mm_fields.has_new_value():
            current_id = settings.mobility_master.credential_id or None
            new_id = self.credential_service.save(
                self._credential_with_current(self.mm_fields, current_id),
                session_only=self.session_only.isChecked(),
                credential_id=None,
            )
            self._record_staged_credential(new_id, current_id)
            settings.mobility_master.credential_id = new_id
        if self.cluster_fields.has_new_value():
            current_id = settings.cluster.credential_id or None
            new_id = self.credential_service.save(
                self._credential_with_current(self.cluster_fields, current_id),
                session_only=self.session_only.isChecked(),
                credential_id=None,
            )
            self._record_staged_credential(new_id, current_id)
            settings.cluster.credential_id = new_id

    def _credential_with_current(
        self,
        fields: _CredentialFields,
        credential_id: str | None,
    ) -> DeviceCredential:
        current: DeviceCredential | None = None
        if not credential_id:
            return fields.credential()
        try:
            current = self.credential_service.get(credential_id)
        except (CredentialError, ValueError):
            # Session-only IDs deliberately outlive their in-memory values in
            # the non-secret settings file.  Credential Manager can also be
            # policy-blocked. A complete fresh username/password pair must be
            # able to replace either case (especially in session-only mode).
            if not fields.username.text().strip() or not fields.password.text():
                raise RuntimeError(
                    "저장된 세션 전용 자격 증명을 찾을 수 없습니다. "
                    "사용자 ID와 비밀번호를 다시 입력해 주세요."
                )
        return fields.credential(current)

    def _configured_session_only(self) -> bool:
        if self.settings.credentials.session_only:
            return True
        if self.credential_service is None or not hasattr(self.credential_service, "is_session"):
            return False
        if self.settings.credentials.use_shared_credentials:
            identifiers = [self.settings.credentials.shared_credential_id]
        else:
            identifiers = [
                self.settings.mobility_master.credential_id,
                self.settings.cluster.credential_id,
            ]
        try:
            return any(
                identifier and self.credential_service.is_session(identifier)
                for identifier in identifiers
            )
        except (CredentialError, ValueError):
            LOGGER.warning("Invalid or unavailable credential reference in settings", exc_info=True)
            return bool(self.settings.credentials.session_only)

    def _record_staged_credential(self, new_id: str, old_id: str | None) -> None:
        self._staged_new_credential_ids.append(new_id)
        if old_id:
            self._staged_old_credential_ids.append(old_id)

    def rollback_staged_credentials(self) -> None:
        if self.credential_service is not None:
            for credential_id in reversed(self._staged_new_credential_ids):
                try:
                    self.credential_service.delete(credential_id)
                except Exception:
                    # Preserve the original settings/credential reference even
                    # if cleanup of an unreferenced staged value is unavailable.
                    LOGGER.warning("Staged credential rollback failed", exc_info=True)
        self._staged_new_credential_ids.clear()
        self._staged_old_credential_ids.clear()

    def commit_staged_credentials(self) -> None:
        referenced = {
            self.settings.credentials.shared_credential_id,
            self.settings.mobility_master.credential_id,
            self.settings.cluster.credential_id,
        }
        if self.credential_service is not None:
            for credential_id in dict.fromkeys(self._staged_old_credential_ids):
                if credential_id and credential_id not in referenced:
                    try:
                        self.credential_service.delete(credential_id)
                    except Exception:
                        LOGGER.warning("Old credential cleanup failed", exc_info=True)
        self._staged_new_credential_ids.clear()
        self._staged_old_credential_ids.clear()
