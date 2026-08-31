from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.credentials import CredentialError, DeviceCredential

from .developer_inspector import DeveloperInspectorController, UiElementMetadata
from .settings import (
    DeviceSettingsPresentationMixin,
    MonitoringSettingsPresentationMixin,
    NotificationSettingsPresentationMixin,
    SettingsPresentationMixin,
)
from .widgets import SubtleTabWidget, fit_window_to_available_screen


LOGGER = logging.getLogger(__name__)
_TRANSIENT_CREDENTIAL_ID = "00000000-0000-4000-8000-000000000000"


@dataclass(slots=True, repr=False)
class ConnectionTestRequest:
    settings: AppSettings
    credential: DeviceCredential | None = None
    credential_overrides: dict[str, "CredentialOverride"] = field(default_factory=dict)
    purpose: str = "diagnostic"

    def __repr__(self) -> str:
        return (
            "ConnectionTestRequest(settings=[NON_SECRET], credential=[REDACTED], "
            "credential_overrides=[REDACTED], purpose="
            f"{self.purpose!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CredentialOverride:
    """Unsaved field changes kept opaque until host identities are checked."""

    username: str | None = None
    password: str | None = None
    enable_secret: str | None = None

    @property
    def has_complete_login(self) -> bool:
        return bool(self.username and self.password)

    def __repr__(self) -> str:
        return (
            "CredentialOverride(username=[REDACTED], password=[REDACTED], "
            "enable_secret=[REDACTED])"
        )


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
        descriptions = (
            (self.username, "장비 로그인에 사용하는 사용자 ID입니다."),
            (self.password, "저장된 값을 바꿀 때만 입력합니다. 화면이나 설정 파일에 평문 저장되지 않습니다."),
            (self.enable_secret, "장비가 Enable 진입을 요구할 때만 입력합니다."),
        )
        for widget, description in descriptions:
            widget.setToolTip(description)
            widget.setStatusTip(description)
            widget.setAccessibleDescription(description)
        for text, widget, description in (
            ("사용자 ID", self.username, descriptions[0][1]),
            ("비밀번호", self.password, descriptions[1][1]),
            ("Enable 비밀번호", self.enable_secret, descriptions[2][1]),
        ):
            label = QLabel(text, self)
            label.setBuddy(widget)
            label.setToolTip(description)
            label.setAccessibleDescription(description)
            widget.setAccessibleName(text)
            layout.addRow(label, widget)

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

    def override(self) -> CredentialOverride | None:
        if not self.has_new_value():
            return None
        return CredentialOverride(
            username=self.username.text().strip() or None,
            password=self.password.text() or None,
            enable_secret=self.enable_secret.text() or None,
        )


class SettingsDialog(
    DeviceSettingsPresentationMixin,
    MonitoringSettingsPresentationMixin,
    NotificationSettingsPresentationMixin,
    SettingsPresentationMixin,
    QDialog,
):
    connection_test_requested = Signal(str, object)
    sound_test_requested = Signal()
    notification_test_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        credential_service: Any | None = None,
        parent: QWidget | None = None,
        *,
        initial_setup: bool = False,
        developer_inspector: DeveloperInspectorController | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aruba 미니 대시보드 설정")
        self.settings = copy.deepcopy(settings)
        self.credential_service = credential_service
        self.initial_setup = initial_setup
        self.developer_inspector = developer_inspector
        self._developer_catalog_actions: list[QAction] = []
        self._staged_new_credential_ids: list[str] = []
        self._staged_old_credential_ids: list[str] = []
        self._connection_pending = False
        self._pending_connection_purpose = ""

        self.root_layout = QVBoxLayout(self)
        self.initial_setup_guide: QLabel | None = None
        if initial_setup:
            self.initial_setup_guide = QLabel(
                "처음 사용하려면 MM과 컨트롤러 4대, Primary 및 자격 증명을 등록하세요. "
                "저장을 누르면 SSH 지문을 한 화면에서 확인하고 로그인까지 자동으로 점검합니다.",
                self,
            )
            self.initial_setup_guide.setWordWrap(True)
            self.initial_setup_guide.setObjectName("initialSetupGuide")
            self.initial_setup_guide.setStyleSheet(
                "QLabel#initialSetupGuide { background: #EAF3FF; border: 1px solid #7AA7D9; "
                "border-radius: 5px; padding: 8px; color: #163A5F; }"
            )
            self.initial_setup_guide.setAccessibleName("첫 실행 설정 안내")
            self.root_layout.addWidget(self.initial_setup_guide)
        self.tabs = SubtleTabWidget(self)
        self.root_layout.addWidget(self.tabs)
        self._build_devices_tab()
        self._build_polling_tab()
        self._build_notifications_tab()

        self.connection_progress_label = QLabel("", self)
        self.connection_progress_label.setWordWrap(True)
        self.connection_progress_label.setObjectName("connectionProgress")
        self.connection_progress_label.setStyleSheet(
            "QLabel#connectionProgress { background: #FFF7E0; border: 1px solid #D8A93A; "
            "border-radius: 5px; padding: 7px; color: #5F4100; }"
        )
        self.connection_progress_label.setAccessibleName("저장 전 연결 확인 상태")
        self.connection_progress_label.setVisible(False)
        self.root_layout.addWidget(self.connection_progress_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.Cancel).setText(
            "나중에 설정" if initial_setup else "취소"
        )
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        self.root_layout.addWidget(self.buttons)
        self._register_developer_inspector()
        fit_window_to_available_screen(
            self,
            QSize(700, 650),
            minimum_size=QSize(420, 320),
            center_on_parent=True,
        )

    @staticmethod
    def _create_credential_fields(title: str) -> _CredentialFields:
        return _CredentialFields(title)

    def _register_developer_inspector(self) -> None:
        inspector = self.developer_inspector
        if inspector is None:
            return

        source = "src/aruba_mini_dashboard/ui/settings_dialog.py"

        def metadata(
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> UiElementMetadata:
            return UiElementMetadata(name, stable_id, screen_path, source, purpose)

        def register_widget(
            widget: QWidget,
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> None:
            inspector.register_widget(
                widget,
                metadata(name, stable_id, screen_path, purpose),
            )

        def register_virtual(
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> UiElementMetadata:
            item_metadata = metadata(name, stable_id, screen_path, purpose)
            action = QAction(name, self)
            self._developer_catalog_actions.append(action)
            inspector.register_action(action, item_metadata)
            return item_metadata

        inspector.attach_host_layout(self, self.root_layout)
        register_widget(
            self,
            "설정 창",
            "SETTINGS-DIALOG",
            "설정",
            "장비, 운영과 알림 설정을 편집합니다.",
        )
        initial_metadata = register_virtual(
            "첫 실행 설정 안내",
            "SETTINGS-INITIAL-GUIDE",
            "설정 > 첫 실행 안내",
            "최초 설정에 필요한 입력 순서를 안내합니다.",
        )
        if self.initial_setup_guide is not None:
            inspector.register_widget(self.initial_setup_guide, initial_metadata)
        register_widget(
            self.tabs.tabBar(),
            "설정 탭",
            "SETTINGS-TABS",
            "설정",
            "장비, 운영과 알림 설정 화면을 전환합니다.",
        )

        tab_definitions = (
            (
                self.devices_scroll,
                "장비·자격 증명 탭",
                "SETTINGS-TAB-DEVICES",
                "설정 > 장비·자격 증명",
                "장비 주소, 연결 방식과 자격 증명 설정을 제공합니다.",
            ),
            (
                self.polling_page,
                "운영 탭",
                "SETTINGS-TAB-OPERATIONS",
                "설정 > 운영",
                "점검 주기, 성능과 감지 기준 설정을 제공합니다.",
            ),
            (
                self.notifications_page,
                "알림 탭",
                "SETTINGS-TAB-NOTIFICATIONS",
                "설정 > 알림",
                "장애, 반복, 복구와 진단 알림 설정을 제공합니다.",
            ),
        )
        for page, name, stable_id, screen_path, purpose in tab_definitions:
            tab_metadata = register_virtual(name, stable_id, screen_path, purpose)
            inspector.register_widget(page, tab_metadata)

        devices_path = "설정 > 장비·자격 증명"
        register_widget(
            self.mm_ip,
            "Mobility Master 관리 IP 입력란",
            "SETTINGS-MM-IP",
            devices_path + " > Mobility Master",
            "상태 명령을 실행할 Mobility Master 관리 주소를 입력합니다.",
        )
        for index, (ip_edit, alias_edit) in enumerate(
            zip(self.member_ips, self.member_aliases, strict=True),
            start=1,
        ):
            register_widget(
                ip_edit,
                f"컨트롤러 {index} IP 입력란",
                f"SETTINGS-WLC-{index}-IP",
                devices_path + " > 컨트롤러 구성원",
                f"감시할 컨트롤러 {index}의 관리 주소를 입력합니다.",
            )
            register_widget(
                alias_edit,
                f"컨트롤러 {index} 별칭 입력란",
                f"SETTINGS-WLC-{index}-ALIAS",
                devices_path + " > 컨트롤러 구성원",
                f"컨트롤러 {index}을 구분할 표시 이름을 입력합니다.",
            )
        register_widget(
            self.primary_ip,
            "Primary Controller 선택란",
            "SETTINGS-PRIMARY-CONTROLLER",
            devices_path + " > 컨트롤러 구성원",
            "클러스터 명령을 먼저 수집할 등록 컨트롤러를 선택합니다.",
        )
        register_widget(
            self.fallback_note,
            "대체 컨트롤러 자동 선택 안내",
            "SETTINGS-FALLBACK-NOTE",
            devices_path + " > 컨트롤러 구성원",
            "Primary 연결 실패 시 사용할 자동 대체 순서를 안내합니다.",
        )

        connection_path = devices_path + " > 고급 연결 설정"
        register_widget(
            self.connection_section,
            "고급 연결 설정 영역",
            "SETTINGS-CONNECTION-SECTION",
            connection_path,
            "SSH 포트, 제한시간, 재시도와 Enable 옵션을 묶어 제공합니다.",
        )
        connection_widgets = (
            (self.mm_port, "MM SSH 포트", "SETTINGS-MM-PORT", "Mobility Master SSH 포트를 설정합니다."),
            (self.mm_connect_timeout, "MM 연결 제한시간", "SETTINGS-MM-CONNECT-TIMEOUT", "Mobility Master 연결 대기 제한시간을 설정합니다."),
            (self.mm_command_timeout, "MM 명령 제한시간", "SETTINGS-MM-COMMAND-TIMEOUT", "Mobility Master 명령 응답 제한시간을 설정합니다."),
            (self.mm_retries, "MM 재시도 횟수", "SETTINGS-MM-RETRIES", "Mobility Master 연결 재시도 횟수를 설정합니다."),
            (self.mm_enable, "MM Enable 진입", "SETTINGS-MM-ENABLE", "Mobility Master에서 Enable 진입이 필요한지 설정합니다."),
            (self.cluster_port, "컨트롤러 SSH 포트", "SETTINGS-WLC-PORT", "컨트롤러 SSH 포트를 설정합니다."),
            (self.cluster_connect_timeout, "컨트롤러 연결 제한시간", "SETTINGS-WLC-CONNECT-TIMEOUT", "컨트롤러 연결 대기 제한시간을 설정합니다."),
            (self.cluster_command_timeout, "컨트롤러 명령 제한시간", "SETTINGS-WLC-COMMAND-TIMEOUT", "컨트롤러 명령 응답 제한시간을 설정합니다."),
            (self.cluster_retries, "컨트롤러 재시도 횟수", "SETTINGS-WLC-RETRIES", "동일 컨트롤러의 연결 재시도 횟수를 설정합니다."),
            (self.cluster_enable, "컨트롤러 Enable 진입", "SETTINGS-WLC-ENABLE", "컨트롤러에서 Enable 진입이 필요한지 설정합니다."),
            (self.connection_reset_button, "연결 기본값 복원 버튼", "SETTINGS-CONNECTION-RESET", "고급 연결 값을 안전한 기본값으로 복원합니다."),
        )
        for widget, name, stable_id, purpose in connection_widgets:
            register_widget(widget, name, stable_id, connection_path, purpose)

        credential_path = devices_path + " > 접속 계정"
        credential_widgets = (
            (self.shared_credentials, "공통 계정 사용 선택란", "SETTINGS-CREDENTIAL-SHARED", "MM과 컨트롤러에서 같은 계정을 사용할지 설정합니다."),
            (self.session_only, "세션 전용 자격 증명 선택란", "SETTINGS-CREDENTIAL-SESSION-ONLY", "자격 증명을 이번 실행의 메모리에만 보관할지 설정합니다."),
        )
        for widget, name, stable_id, purpose in credential_widgets:
            register_widget(widget, name, stable_id, credential_path, purpose)
        credential_groups = (
            (self.shared_fields, "공통 계정 입력 영역", "SHARED", "MM과 컨트롤러가 함께 사용할 계정을 입력합니다."),
            (self.mm_fields, "MM 계정 입력 영역", "MM", "Mobility Master에 사용할 계정을 입력합니다."),
            (self.cluster_fields, "WLC 계정 입력 영역", "WLC", "컨트롤러에 사용할 계정을 입력합니다."),
        )
        for fields, name, role_id, purpose in credential_groups:
            group_metadata = metadata(
                name,
                f"SETTINGS-CREDENTIAL-{role_id}-GROUP",
                credential_path,
                purpose,
            )
            inspector.register_widget(fields, group_metadata)
            field_definitions = (
                (fields.username, "사용자 ID 입력란", "USERNAME", "장비 로그인 사용자 ID를 입력합니다."),
                (fields.password, "비밀번호 입력란", "PASSWORD", "변경할 장비 로그인 비밀번호를 입력합니다."),
                (fields.enable_secret, "Enable 비밀번호 입력란", "ENABLE-SECRET", "필요한 경우 Enable 비밀번호를 입력합니다."),
            )
            for field, field_name, field_id, field_purpose in field_definitions:
                register_widget(
                    field,
                    f"{name} {field_name}",
                    f"SETTINGS-CREDENTIAL-{role_id}-{field_id}",
                    credential_path + " > " + name,
                    field_purpose,
                )
        register_widget(
            self.connection_diagnostic_section,
            "고급 연결 진단 영역",
            "SETTINGS-CONNECTION-DIAGNOSTIC-SECTION",
            devices_path + " > 고급 진단",
            "MM과 모든 컨트롤러의 SSH 지문 및 로그인 재확인 작업을 제공합니다.",
        )
        register_widget(
            self.connection_diagnostic_button,
            "연결 다시 확인 버튼",
            "SETTINGS-CONNECTION-DIAGNOSTIC",
            devices_path + " > 고급 진단",
            "입력한 모든 장비의 SSH 지문과 로그인을 한 번에 다시 확인합니다.",
        )

        operations_path = "설정 > 운영"
        operation_widgets = (
            (self.poll_interval, "점검 주기 입력란", "SETTINGS-POLL-INTERVAL", "자동 점검 간격을 설정합니다."),
            (self.low_spec_mode, "저사양 모드 선택란", "SETTINGS-LOW-SPEC-MODE", "저사양 환경용 수집과 표시 제한을 적용합니다."),
            (self.performance_logging, "선택적 성능 로그 선택란", "SETTINGS-PERFORMANCE-LOGGING", "민감한 값 없이 처리 시간과 개수만 기록할지 설정합니다."),
            (self.polling_note, "중복 점검 방지 안내", "SETTINGS-POLL-SKIP-NOTE", "진행 중인 점검과 예약 회차 처리 방식을 안내합니다."),
            (self.detection_section, "고급 감지 기준 영역", "SETTINGS-DETECTION-SECTION", "클라이언트 저하와 누락 감지 기준을 묶어 제공합니다."),
        )
        for widget, name, stable_id, purpose in operation_widgets:
            register_widget(widget, name, stable_id, operations_path, purpose)
        detection_path = operations_path + " > 고급 감지 기준"
        detection_widgets = (
            (self.low_threshold, "Low Client Threshold 입력란", "SETTINGS-DETECTION-LOW-THRESHOLD", "낮은 Client 수의 절대 기준을 설정합니다."),
            (self.anomaly_cycles, "연속 이상 감지 횟수", "SETTINGS-DETECTION-ANOMALY-CYCLES", "장애 활성화에 필요한 연속 이상 횟수를 설정합니다."),
            (self.recovery_cycles, "복구 확인 횟수", "SETTINGS-DETECTION-RECOVERY-CYCLES", "복구 판단에 필요한 연속 정상 횟수를 설정합니다."),
            (self.comparison_mode, "감지 모드 선택란", "SETTINGS-DETECTION-COMPARISON-MODE", "절대 기준과 상대 비교의 사용 방식을 선택합니다."),
            (self.relative_ratio, "상대 비교 기준 입력란", "SETTINGS-DETECTION-RELATIVE-RATIO", "정상 Peer 중앙값 대비 비율 기준을 설정합니다."),
            (self.minimum_total, "클러스터 최소 전체 Active 입력란", "SETTINGS-DETECTION-MINIMUM-TOTAL", "특정 장비 장애 판단에 사용할 전체 사용량 하한을 설정합니다."),
            (self.minimum_peer, "Peer 중앙값 최소 입력란", "SETTINGS-DETECTION-MINIMUM-PEER", "상대 비교에 사용할 Peer 사용량 하한을 설정합니다."),
            (self.missing_cycles, "행 누락 활성화 횟수", "SETTINGS-DETECTION-MISSING-CYCLES", "구성원 누락 경고에 필요한 연속 횟수를 설정합니다."),
            (self.detection_reset_button, "감지 기본값 복원 버튼", "SETTINGS-DETECTION-RESET", "고급 감지 기준을 기본값으로 복원합니다."),
        )
        for widget, name, stable_id, purpose in detection_widgets:
            register_widget(widget, name, stable_id, detection_path, purpose)

        notification_path = "설정 > 알림"
        notification_widgets = (
            (self.notify_new, "신규 장애 즉시 알림 선택란", "SETTINGS-NOTIFY-NEW", "새 장애가 활성화될 때 Windows 알림을 표시할지 설정합니다."),
            (self.sound_enabled, "알림음 사용 선택란", "SETTINGS-NOTIFY-SOUND", "새 장애 알림과 함께 로컬 알림음을 재생할지 설정합니다."),
            (self.recovery_notifications, "복구 알림 선택란", "SETTINGS-NOTIFY-RECOVERY", "장애 복구 시 알림을 표시할지 설정합니다."),
            (self.sound_test_button, "알림음 테스트 버튼", "SETTINGS-NOTIFY-SOUND-TEST", "로컬 알림음을 한 번 시험합니다."),
            (self.windows_test_button, "Windows 알림 테스트 버튼", "SETTINGS-NOTIFY-WINDOWS-TEST", "Windows 알림 표시 가능 여부를 시험합니다."),
            (self.notification_advanced_section, "고급 반복 알림·진단 영역", "SETTINGS-NOTIFY-ADVANCED-SECTION", "반복 알림과 SSH 진단 옵션을 묶어 제공합니다."),
        )
        for widget, name, stable_id, purpose in notification_widgets:
            register_widget(widget, name, stable_id, notification_path, purpose)
        advanced_notification_path = notification_path + " > 고급 반복 알림·진단"
        advanced_notification_widgets = (
            (self.repeat_unack, "미확인 장애 반복 알림 선택란", "SETTINGS-NOTIFY-REPEAT", "확인되지 않은 장애 알림을 반복할지 설정합니다."),
            (self.repeat_minutes, "반복 알림 간격 입력란", "SETTINGS-NOTIFY-REPEAT-INTERVAL", "미확인 장애 알림의 반복 간격을 설정합니다."),
            (self.ssh_debug_logging, "SSH 디버그 로그 선택란", "SETTINGS-NOTIFY-SSH-DEBUG", "파싱 진단용 상세 SSH 로그를 사용할지 설정합니다."),
            (self.notification_warning, "SSH 디버그 로그 주의 안내", "SETTINGS-NOTIFY-SSH-WARNING", "상세 로그 공유 전 민감 정보 확인 필요성을 안내합니다."),
            (self.notification_reset_button, "알림 고급값 복원 버튼", "SETTINGS-NOTIFY-RESET", "반복 알림과 진단 옵션을 기본값으로 복원합니다."),
        )
        for widget, name, stable_id, purpose in advanced_notification_widgets:
            register_widget(
                widget,
                name,
                stable_id,
                advanced_notification_path,
                purpose,
            )

        register_widget(
            self.buttons.button(QDialogButtonBox.Save),
            "설정 저장 버튼",
            "SETTINGS-SAVE",
            "설정 > 하단 작업",
            "연결 변경 시 SSH 지문과 로그인을 먼저 확인한 뒤 설정 적용을 요청합니다.",
        )
        register_widget(
            self.buttons.button(QDialogButtonBox.Cancel),
            "설정 취소 버튼",
            "SETTINGS-CANCEL",
            "설정 > 하단 작업",
            "변경을 적용하지 않고 설정 창을 닫습니다.",
        )

    def _emit_connection_test(self, role: str = "all") -> None:
        if self._connection_pending:
            return
        try:
            candidate = self._collect_settings(save_credentials=False)
            request = self._connection_request(candidate, purpose="diagnostic")
            self._validate_connection_request(candidate, request)
        except Exception as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        self._set_connection_pending("diagnostic")
        self.connection_test_requested.emit(role, request)

    def _apply(self) -> None:
        if self._connection_pending:
            return
        try:
            candidate = self._collect_settings(save_credentials=False)
            candidate.validate()
            if self._requires_connection_check(candidate):
                request = self._connection_request(candidate, purpose="save")
                self._validate_connection_request(candidate, request)
                self._set_connection_pending("save")
                self.connection_test_requested.emit("all", request)
                return
        except Exception as exc:
            self.rollback_staged_credentials()
            QMessageBox.warning(self, "설정을 저장할 수 없음", str(exc))
            return
        self._finish_save()

    def _finish_save(self) -> bool:
        try:
            candidate = self._collect_settings(save_credentials=True)
            if self.initial_setup:
                candidate.validate_for_monitoring()
            else:
                candidate.validate()
        except Exception as exc:
            self.rollback_staged_credentials()
            self._clear_connection_pending()
            QMessageBox.warning(self, "설정을 저장할 수 없음", str(exc))
            return False
        self._clear_connection_pending()
        self.settings = candidate
        self.accept()
        return True

    def complete_connection_request(self, success: bool) -> bool:
        """Resume the modal form after its asynchronous SSH check finishes."""

        if not self._connection_pending:
            return False
        purpose = self._pending_connection_purpose
        if success and purpose == "save":
            return self._finish_save()
        self._clear_connection_pending()
        return success

    def reject(self) -> None:
        if self._connection_pending and not self._parent_is_quitting():
            self.connection_progress_label.setText(
                "SSH 연결 확인이 끝난 뒤 취소할 수 있습니다. 새 자격 증명은 아직 저장하지 않았습니다."
            )
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._connection_pending and not self._parent_is_quitting():
            self.connection_progress_label.setText(
                "SSH 연결 확인이 끝난 뒤 창을 닫을 수 있습니다. 새 자격 증명은 아직 저장하지 않았습니다."
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _parent_is_quitting(self) -> bool:
        parent = self.parent()
        coordinator = getattr(parent, "coordinator", None)
        return bool(
            getattr(parent, "_quitting", False)
            or getattr(coordinator, "shutting_down", False)
        )

    @property
    def connection_request_pending(self) -> bool:
        return self._connection_pending

    @property
    def pending_connection_purpose(self) -> str:
        return self._pending_connection_purpose

    def _set_connection_pending(self, purpose: str) -> None:
        self._connection_pending = True
        self._pending_connection_purpose = purpose
        self.tabs.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(False)
        self.connection_progress_label.setText(
            "저장 전 SSH 지문과 로그인을 확인하고 있습니다…"
            if purpose == "save"
            else "MM과 컨트롤러의 SSH 지문과 로그인을 다시 확인하고 있습니다…"
        )
        self.connection_progress_label.setVisible(True)

    def _clear_connection_pending(self) -> None:
        self._connection_pending = False
        self._pending_connection_purpose = ""
        self.tabs.setEnabled(True)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(True)
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(True)
        self.connection_progress_label.clear()
        self.connection_progress_label.setVisible(False)

    def _connection_request(
        self,
        candidate: AppSettings,
        *,
        purpose: str,
    ) -> ConnectionTestRequest:
        overrides: dict[str, CredentialOverride] = {}
        if self.shared_credentials.isChecked():
            override = self.shared_fields.override()
            if override is not None:
                overrides = {"mm": override, "cluster": override}
        else:
            for role, fields in (("mm", self.mm_fields), ("cluster", self.cluster_fields)):
                override = fields.override()
                if override is not None:
                    overrides[role] = override
        return ConnectionTestRequest(
            settings=candidate,
            credential_overrides=overrides,
            purpose=purpose,
        )

    def _validate_connection_request(
        self,
        candidate: AppSettings,
        request: ConnectionTestRequest,
    ) -> None:
        candidate.validate()
        fields = (
            [self.shared_fields]
            if self.shared_credentials.isChecked()
            else [self.mm_fields, self.cluster_fields]
        )
        if self.session_only.isChecked() != self._initial_session_only and not all(
            item.username.text().strip() and item.password.text() for item in fields
        ):
            raise RuntimeError(
                "자격 증명 저장 방식을 변경하려면 해당 계정의 사용자 ID와 비밀번호를 다시 입력하세요."
            )

        validation_copy = copy.deepcopy(candidate)
        missing_roles: list[str] = []
        for role in ("mm", "cluster"):
            if validation_copy.credentials.effective_id(role, validation_copy):
                continue
            override = request.credential_overrides.get(role)
            if override is None or not override.has_complete_login:
                missing_roles.append("MM" if role == "mm" else "클러스터")
                continue
            if validation_copy.credentials.use_shared_credentials:
                validation_copy.credentials.shared_credential_id = _TRANSIENT_CREDENTIAL_ID
            elif role == "mm":
                validation_copy.mobility_master.credential_id = _TRANSIENT_CREDENTIAL_ID
            else:
                validation_copy.cluster.credential_id = _TRANSIENT_CREDENTIAL_ID
        if missing_roles:
            raise RuntimeError(f"{', '.join(missing_roles)} 자격 증명의 사용자 ID와 비밀번호가 필요합니다.")
        validation_copy.validate_for_monitoring()

    def _requires_connection_check(self, candidate: AppSettings) -> bool:
        if self.initial_setup:
            return True
        credential_fields = (
            [self.shared_fields]
            if self.shared_credentials.isChecked()
            else [self.mm_fields, self.cluster_fields]
        )
        return (
            self._connection_signature(candidate) != self._connection_signature(self.settings)
            or any(item.has_new_value() for item in credential_fields)
        )

    @staticmethod
    def _connection_signature(settings: AppSettings) -> tuple[object, ...]:
        mm = settings.mobility_master
        cluster = settings.cluster
        credentials = settings.credentials
        return (
            mm.management_ip.strip(),
            mm.ssh_port,
            mm.connect_timeout_seconds,
            mm.command_timeout_seconds,
            mm.retries,
            mm.enable_required,
            tuple(member.ip.strip() for member in cluster.members),
            cluster.primary_controller_ip.strip(),
            tuple(item.strip() for item in cluster.fallback_controller_ips),
            cluster.ssh_port,
            cluster.connect_timeout_seconds,
            cluster.command_timeout_seconds,
            cluster.retries,
            cluster.enable_required,
            credentials.use_shared_credentials,
            credentials.session_only,
            credentials.shared_credential_id,
            mm.credential_id,
            cluster.credential_id,
        )

    def _collect_settings(self, *, save_credentials: bool) -> AppSettings:
        settings = copy.deepcopy(self.settings)
        mm = settings.mobility_master
        mm.management_ip = self.mm_ip.text().strip()
        mm.ssh_port = self.mm_port.value()
        mm.connect_timeout_seconds = self.mm_connect_timeout.value()
        mm.command_timeout_seconds = self.mm_command_timeout.value()
        mm.retries = self.mm_retries.value()
        mm.enable_required = self.mm_enable.isChecked()

        cluster = settings.cluster
        cluster.members = [
            ClusterMemberSettings(ip=ip.text().strip(), alias=alias.text().strip())
            for ip, alias in zip(self.member_ips, self.member_aliases, strict=True)
        ]
        cluster.primary_controller_ip = str(self.primary_ip.currentData() or "").strip()
        cluster.fallback_controller_ips = [
            member.ip
            for member in cluster.members
            if member.ip and member.ip != cluster.primary_controller_ip
        ]
        cluster.ssh_port = self.cluster_port.value()
        cluster.connect_timeout_seconds = self.cluster_connect_timeout.value()
        cluster.command_timeout_seconds = self.cluster_command_timeout.value()
        cluster.retries = self.cluster_retries.value()
        cluster.enable_required = self.cluster_enable.isChecked()

        settings.credentials.use_shared_credentials = self.shared_credentials.isChecked()
        settings.credentials.session_only = self.session_only.isChecked()
        settings.polling.interval_seconds = self.poll_interval.value()
        if self.initial_setup:
            # First-run saving prepares the app only. The operator explicitly
            # starts the first live SSH collection from the dashboard.
            settings.polling.automatic_enabled = False
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
        settings.ssh_debug_logging = self.ssh_debug_logging.isChecked()
        performance = getattr(settings, "performance", None)
        if performance is not None:
            performance.low_spec_mode = self.low_spec_mode.isChecked()
            performance.performance_logging = self.performance_logging.isChecked()

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
