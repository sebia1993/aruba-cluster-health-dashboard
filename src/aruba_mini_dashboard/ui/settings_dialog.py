from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSize, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
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
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.credentials import CredentialError, DeviceCredential

from .developer_inspector import DeveloperInspectorController, UiElementMetadata


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, repr=False)
class ConnectionTestRequest:
    settings: AppSettings
    credential: DeviceCredential | None = None

    def __repr__(self) -> str:
        return "ConnectionTestRequest(settings=[NON_SECRET], credential=[REDACTED])"

from .widgets import (
    ClickArmedComboBox,
    ClickArmedSpinBox,
    CollapsibleSection,
    SubtleTabWidget,
    fit_window_to_available_screen,
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


class SettingsDialog(QDialog):
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

        self.root_layout = QVBoxLayout(self)
        self.initial_setup_guide: QLabel | None = None
        if initial_setup:
            self.initial_setup_guide = QLabel(
                "처음 사용하려면 MM과 컨트롤러 4대, Primary 및 자격 증명을 등록하세요. "
                "저장 후 메인 화면에서 ‘지금 점검’을 눌러 확인합니다.",
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
    def _spin(
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "",
    ) -> ClickArmedSpinBox:
        widget = ClickArmedSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _describe(widget: QWidget, name: str, description: str) -> QWidget:
        wheel_note = widget.toolTip()
        tooltip = description + (f"\n\n{wheel_note}" if wheel_note else "")
        widget.setToolTip(tooltip)
        widget.setStatusTip(description)
        widget.setAccessibleName(name)
        widget.setAccessibleDescription(description)
        return widget

    @classmethod
    def _add_row(
        cls,
        form: QFormLayout,
        label_text: str,
        widget: QWidget,
        description: str,
    ) -> None:
        cls._describe(widget, label_text, description)
        label = QLabel(label_text)
        label.setBuddy(widget)
        label.setToolTip(description)
        form.addRow(label, widget)

    def _build_devices_tab(self) -> None:
        contents = QWidget(self)
        outer = QVBoxLayout(contents)

        mm_box = QGroupBox("Mobility Master", contents)
        mm_form = QFormLayout(mm_box)
        mm = self.settings.mobility_master
        self.mm_ip = QLineEdit(mm.management_ip)
        self._add_row(
            mm_form,
            "관리 IP",
            self.mm_ip,
            "show switches 명령을 실행할 Mobility Master 관리 IP입니다.",
        )
        outer.addWidget(mm_box)

        cluster_box = QGroupBox("Aruba 7240XM 컨트롤러", contents)
        cluster_layout = QVBoxLayout(cluster_box)
        cluster = self.settings.cluster
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
            self._describe(
                ip_edit,
                f"컨트롤러 {index} IP",
                "감시할 Aruba 7240XM 구성원의 관리 IP입니다.",
            )
            self._describe(
                alias_edit,
                f"컨트롤러 {index} 별칭",
                "대시보드에서 IP와 함께 표시할 알아보기 쉬운 이름입니다.",
            )
            self.member_ips.append(ip_edit)
            self.member_aliases.append(alias_edit)
            member_grid.addWidget(QLabel(str(index)), index, 0)
            member_grid.addWidget(ip_edit, index, 1)
            member_grid.addWidget(alias_edit, index, 2)
            ip_edit.textChanged.connect(self._refresh_primary_choices)
        cluster_layout.addLayout(member_grid)

        endpoint_form = QFormLayout()
        self.primary_ip = ClickArmedComboBox()
        self._configured_primary_ip = cluster.primary_controller_ip
        self._refresh_primary_choices()
        self._add_row(
            endpoint_form,
            "Primary Controller",
            self.primary_ip,
            "클러스터 명령을 먼저 수집할 컨트롤러입니다. 등록한 4대 중에서 선택합니다.",
        )
        self.fallback_note = QLabel(
            "Primary 연결 실패 시 나머지 등록 컨트롤러를 위의 등록 순서대로 자동 시도합니다."
        )
        self.fallback_note.setWordWrap(True)
        self.fallback_note.setAccessibleName("대체 컨트롤러 자동 선택 안내")
        endpoint_form.addRow(self.fallback_note)
        cluster_layout.addLayout(endpoint_form)
        outer.addWidget(cluster_box)

        connection_content = QWidget(contents)
        connection_layout = QVBoxLayout(connection_content)
        connection_layout.setContentsMargins(12, 2, 0, 4)
        mm_connection = QGroupBox("MM SSH", connection_content)
        mm_connection_form = QFormLayout(mm_connection)
        self.mm_port = self._spin(1, 65535, mm.ssh_port)
        self.mm_connect_timeout = self._spin(1, 600, mm.connect_timeout_seconds, "초")
        self.mm_command_timeout = self._spin(1, 600, mm.command_timeout_seconds, "초")
        self.mm_retries = self._spin(0, 10, mm.retries, "회")
        self.mm_enable = QCheckBox("Enable 진입 필요")
        self.mm_enable.setChecked(mm.enable_required)
        for label, widget, description in (
            ("SSH 포트", self.mm_port, "MM SSH 포트입니다. 기본값 22, 허용 범위 1~65535입니다."),
            ("연결 제한시간", self.mm_connect_timeout, "MM TCP/SSH 연결을 기다리는 최대 시간입니다. 기본값 10초입니다."),
            ("명령 제한시간", self.mm_command_timeout, "MM 명령 응답을 기다리는 최대 시간입니다. 기본값 20초입니다."),
            ("재시도 횟수", self.mm_retries, "일시적인 연결 실패 시 추가 시도 횟수입니다. 기본값 2회입니다."),
        ):
            self._add_row(mm_connection_form, label, widget, description)
        self._describe(self.mm_enable, "MM Enable 진입", "MM 계정이 show 명령 전에 Enable 진입을 요구할 때 사용합니다.")
        mm_connection_form.addRow(self.mm_enable)
        connection_layout.addWidget(mm_connection)

        cluster_connection = QGroupBox("컨트롤러 SSH", connection_content)
        cluster_connection_form = QFormLayout(cluster_connection)
        self.cluster_port = self._spin(1, 65535, cluster.ssh_port)
        self.cluster_connect_timeout = self._spin(1, 600, cluster.connect_timeout_seconds, "초")
        self.cluster_command_timeout = self._spin(1, 600, cluster.command_timeout_seconds, "초")
        self.cluster_retries = self._spin(0, 10, cluster.retries, "회")
        self.cluster_enable = QCheckBox("Enable 진입 필요")
        self.cluster_enable.setChecked(cluster.enable_required)
        for label, widget, description in (
            ("SSH 포트", self.cluster_port, "컨트롤러 SSH 포트입니다. 기본값 22, 허용 범위 1~65535입니다."),
            ("연결 제한시간", self.cluster_connect_timeout, "컨트롤러 연결을 기다리는 최대 시간입니다. 기본값 10초입니다."),
            ("명령 제한시간", self.cluster_command_timeout, "클러스터 명령 응답을 기다리는 최대 시간입니다. 기본값 20초입니다."),
            ("재시도 횟수", self.cluster_retries, "동일 컨트롤러의 추가 시도 횟수입니다. 이후 자동으로 다음 등록 장비를 시도합니다."),
        ):
            self._add_row(cluster_connection_form, label, widget, description)
        self._describe(self.cluster_enable, "컨트롤러 Enable 진입", "컨트롤러가 show 명령 전에 Enable 진입을 요구할 때 사용합니다.")
        cluster_connection_form.addRow(self.cluster_enable)
        connection_layout.addWidget(cluster_connection)
        self.connection_reset_button = QPushButton("연결 기본값 복원", connection_content)
        self._describe(
            self.connection_reset_button,
            "연결 기본값 복원",
            "SSH 포트, 제한시간, 재시도와 Enable 옵션만 기본값으로 돌립니다.",
        )
        self.connection_reset_button.clicked.connect(self._reset_connection_defaults)
        connection_layout.addWidget(self.connection_reset_button)
        self.connection_section = CollapsibleSection(
            "고급 연결 설정", connection_content, contents
        )
        outer.addWidget(self.connection_section)

        credentials_box = QGroupBox("접속 계정", contents)
        credentials_layout = QVBoxLayout(credentials_box)
        self.shared_credentials = QCheckBox("MM과 WLC에서 같은 계정 사용")
        self.shared_credentials.setChecked(self.settings.credentials.use_shared_credentials)
        self.session_only = QCheckBox("세션 전용 자격 증명 (프로그램 종료 시 삭제)")
        self._initial_session_only = self._configured_session_only()
        self.session_only.setChecked(self._initial_session_only)
        self._describe(
            self.shared_credentials,
            "공통 계정 사용",
            "MM과 컨트롤러가 같은 로그인 계정을 사용할 때 선택합니다.",
        )
        self._describe(
            self.session_only,
            "세션 전용 자격 증명",
            "선택하면 자격 증명을 메모리에만 보관하고 프로그램 종료 시 삭제합니다.",
        )
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
        self._describe(
            self.mm_test_button,
            "MM 연결 테스트",
            "저장 전에 입력한 MM 설정으로 읽기 전용 SSH 호스트 키와 로그인을 확인합니다.",
        )
        self._describe(
            self.cluster_test_button,
            "클러스터 연결 테스트",
            "Primary 및 자동 Fallback 순서로 읽기 전용 SSH 호스트 키와 로그인을 확인합니다.",
        )
        self.mm_test_button.clicked.connect(lambda: self._emit_connection_test("mm"))
        self.cluster_test_button.clicked.connect(lambda: self._emit_connection_test("cluster"))
        tests.addWidget(self.mm_test_button)
        tests.addWidget(self.cluster_test_button)
        tests.addStretch(1)
        outer.addLayout(tests)
        outer.addStretch(1)

        self.devices_scroll = QScrollArea(self)
        self.devices_scroll.setWidgetResizable(True)
        self.devices_scroll.setWidget(contents)
        self.tabs.addTab(self.devices_scroll, "장비·자격 증명")

    @Slot()
    def _refresh_primary_choices(self) -> None:
        if not hasattr(self, "primary_ip"):
            return
        selected = self.primary_ip.currentData() or getattr(self, "_configured_primary_ip", "")
        choices = [edit.text().strip() for edit in self.member_ips if edit.text().strip()]
        self.primary_ip.blockSignals(True)
        self.primary_ip.clear()
        self.primary_ip.addItem("선택하세요", "")
        for index, ip in enumerate(choices, start=1):
            self.primary_ip.addItem(f"{index}. {ip}", ip)
        self.primary_ip.setCurrentIndex(max(0, self.primary_ip.findData(selected)))
        self.primary_ip.blockSignals(False)

    def _reset_connection_defaults(self) -> None:
        defaults = AppSettings.default()
        for widget, value_ in (
            (self.mm_port, defaults.mobility_master.ssh_port),
            (self.mm_connect_timeout, defaults.mobility_master.connect_timeout_seconds),
            (self.mm_command_timeout, defaults.mobility_master.command_timeout_seconds),
            (self.mm_retries, defaults.mobility_master.retries),
            (self.cluster_port, defaults.cluster.ssh_port),
            (self.cluster_connect_timeout, defaults.cluster.connect_timeout_seconds),
            (self.cluster_command_timeout, defaults.cluster.command_timeout_seconds),
            (self.cluster_retries, defaults.cluster.retries),
        ):
            widget.setValue(value_)
        self.mm_enable.setChecked(defaults.mobility_master.enable_required)
        self.cluster_enable.setChecked(defaults.cluster.enable_required)

    def _build_detection_section(self, parent: QWidget) -> CollapsibleSection:
        content = QWidget(parent)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 2, 0, 4)
        form_widget = QWidget(content)
        form = QFormLayout(form_widget)
        d = self.settings.detection
        self.low_threshold = self._spin(0, 1_000_000, d.low_client_threshold)
        self.anomaly_cycles = self._spin(1, 100, d.anomaly_cycles, "회")
        self.recovery_cycles = self._spin(1, 100, d.recovery_cycles, "회")
        self.comparison_mode = ClickArmedComboBox()
        self.comparison_mode.addItem("절대값과 상대 비교 함께 사용", "absolute_and_relative")
        self.comparison_mode.addItem("절대값만 사용", "absolute_only")
        self.comparison_mode.setCurrentIndex(max(0, self.comparison_mode.findData(d.comparison_mode)))
        self.relative_ratio = self._spin(1, 100, d.relative_ratio_percent, "%")
        self.minimum_total = self._spin(0, 1_000_000, d.minimum_cluster_active_clients)
        self.minimum_peer = self._spin(0, 1_000_000, d.minimum_peer_median)
        self.missing_cycles = self._spin(1, 100, d.missing_cycles, "회")
        rows = (
            ("Low Client Threshold", self.low_threshold, "Active와 Standby가 모두 이 값 이하인지 확인합니다. 기본값 10입니다."),
            ("연속 이상 감지", self.anomaly_cycles, "Client 저하가 이 횟수만큼 연속될 때 장애를 활성화합니다. 기본값 3회입니다."),
            ("복구 확인", self.recovery_cycles, "정상 값이 이 횟수만큼 연속된 뒤 복구로 판단합니다. 기본값 2회입니다."),
            ("감지 모드", self.comparison_mode, "절대 기준만 또는 다른 장비 중앙값과의 상대 비교를 함께 사용합니다."),
            ("상대 비교 기준", self.relative_ratio, "정상 Peer 중앙값 대비 이 비율 이하인지 확인합니다. 기본값 25%입니다."),
            ("클러스터 최소 전체 Active", self.minimum_total, "전체 사용량이 낮을 때 특정 장비 장애로 오판하지 않는 하한입니다. 기본값 50입니다."),
            ("Peer 중앙값 최소", self.minimum_peer, "다른 장비가 충분한 Client를 보유했는지 확인하는 하한입니다. 기본값 30입니다."),
            ("행 누락 활성화", self.missing_cycles, "구성원 행이 이 횟수만큼 연속 누락될 때 경고합니다. 기본값 3회입니다."),
        )
        for label, widget, description in rows:
            self._add_row(form, label, widget, description)
        layout.addWidget(form_widget)
        self.detection_reset_button = QPushButton("감지 기본값 복원", content)
        self._describe(
            self.detection_reset_button,
            "감지 기본값 복원",
            "고급 감지 기준만 안전한 기본값으로 돌립니다.",
        )
        self.detection_reset_button.clicked.connect(self._reset_detection_defaults)
        layout.addWidget(self.detection_reset_button)
        return CollapsibleSection("고급 감지 기준", content, parent)

    def _reset_detection_defaults(self) -> None:
        defaults = AppSettings.default().detection
        self.low_threshold.setValue(defaults.low_client_threshold)
        self.anomaly_cycles.setValue(defaults.anomaly_cycles)
        self.recovery_cycles.setValue(defaults.recovery_cycles)
        self.comparison_mode.setCurrentIndex(
            max(0, self.comparison_mode.findData(defaults.comparison_mode))
        )
        self.relative_ratio.setValue(defaults.relative_ratio_percent)
        self.minimum_total.setValue(defaults.minimum_cluster_active_clients)
        self.minimum_peer.setValue(defaults.minimum_peer_median)
        self.missing_cycles.setValue(defaults.missing_cycles)

    def _build_polling_tab(self) -> None:
        self.polling_page = QWidget(self)
        layout = QVBoxLayout(self.polling_page)
        form_widget = QWidget(self.polling_page)
        form = QFormLayout(form_widget)
        self.poll_interval = self._spin(10, 3600, self.settings.polling.interval_seconds, "초")
        self._add_row(
            form,
            "점검 주기",
            self.poll_interval,
            "자동 점검 간격입니다. 10~3600초이며 기본값은 60초입니다.",
        )
        performance = getattr(self.settings, "performance", None)
        self.low_spec_mode = QCheckBox("저사양 모드")
        self.low_spec_mode.setChecked(bool(getattr(performance, "low_spec_mode", False)))
        self._describe(
            self.low_spec_mode,
            "저사양 모드",
            "자동 점검은 최소 120초 간격으로 실행하고 ‘지금 점검’은 즉시 실행됩니다. "
            "MM과 클러스터 수집은 최대 2개까지 병렬 실행합니다. 대용량 원본 출력은 내용에 "
            "따라 압축 여부를 판단하고, 전체 화면 장비표는 250대씩 표시하며, 운영 로그는 "
            "파일당 최대 2MB와 백업 2개로 제한합니다. 같은 명령과 감지 기준을 사용하므로 "
            "결과 정확성은 바뀌지 않습니다.",
        )
        form.addRow(self.low_spec_mode)
        self.performance_logging = QCheckBox("선택적 성능 로그")
        self.performance_logging.setChecked(
            bool(getattr(performance, "performance_logging", False))
        )
        self._describe(
            self.performance_logging,
            "선택적 성능 로그",
            "시작·수집·저장·화면 처리 시간과 개수만 별도 회전 로그에 기록합니다. "
            "IP, 사용자 ID, 자격 증명과 원본 명령 출력은 기록하지 않습니다. 기본값은 꺼짐입니다.",
        )
        form.addRow(self.performance_logging)
        self.polling_note = QLabel(
            "점검 중 예약 시각이 도래하면 해당 회차는 건너뛰며, 중복 SSH 작업을 실행하지 않습니다."
        )
        self.polling_note.setWordWrap(True)
        form.addRow(self.polling_note)
        layout.addWidget(form_widget)
        self.detection_section = self._build_detection_section(self.polling_page)
        layout.addWidget(self.detection_section)
        layout.addStretch(1)
        self.tabs.addTab(self.polling_page, "운영")

    def _build_notifications_tab(self) -> None:
        self.notifications_page = QWidget(self)
        layout = QVBoxLayout(self.notifications_page)
        form_widget = QWidget(self.notifications_page)
        form = QFormLayout(form_widget)
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
        for widget, name, description in (
            (self.notify_new, "신규 장애 즉시 알림", "새 장애가 활성화될 때 Windows 알림을 표시합니다."),
            (self.sound_enabled, "알림음 사용", "새 장애 알림과 함께 로컬 알림음을 재생합니다."),
            (self.recovery_notifications, "복구 알림", "활성 장애가 정상으로 복구될 때 알림을 표시합니다."),
        ):
            self._describe(widget, name, description)
            form.addRow(widget)
        test_row = QWidget(self.notifications_page)
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        self.sound_test_button = QPushButton("알림음 테스트")
        self.windows_test_button = QPushButton("Windows 알림 테스트")
        self.sound_test_button.clicked.connect(self.sound_test_requested)
        self.windows_test_button.clicked.connect(self.notification_test_requested)
        self._describe(
            self.sound_test_button,
            "알림음 테스트",
            "현재 선택과 무관하게 로컬 알림음을 한 번 시험합니다.",
        )
        self._describe(
            self.windows_test_button,
            "Windows 알림 테스트",
            "현재 알림 설정으로 Windows 알림 표시 가능 여부를 시험합니다.",
        )
        test_layout.addWidget(self.sound_test_button)
        test_layout.addWidget(self.windows_test_button)
        test_layout.addStretch(1)
        form.addRow("테스트", test_row)
        layout.addWidget(form_widget)

        repeat_content = QWidget(self.notifications_page)
        repeat_layout = QVBoxLayout(repeat_content)
        repeat_layout.setContentsMargins(12, 2, 0, 4)
        repeat_form_widget = QWidget(repeat_content)
        repeat_form = QFormLayout(repeat_form_widget)
        self._describe(
            self.repeat_unack,
            "미확인 장애 반복 알림",
            "운영자가 확인 처리하지 않은 활성 장애를 설정 간격으로 다시 알립니다.",
        )
        repeat_form.addRow(self.repeat_unack)
        self._add_row(
            repeat_form,
            "반복 알림 간격",
            self.repeat_minutes,
            "반복 알림 간격입니다. 1~1440분이며 기본값은 10분입니다.",
        )
        self.ssh_debug_logging = QCheckBox("SSH 디버그 로그 사용")
        self.ssh_debug_logging.setChecked(self.settings.ssh_debug_logging)
        self._describe(
            self.ssh_debug_logging,
            "SSH 디버그 로그",
            "파싱 문제 분석용 상세 로그입니다. 장비 출력 일부가 포함될 수 있어 필요한 기간에만 사용합니다.",
        )
        repeat_form.addRow(self.ssh_debug_logging)
        repeat_layout.addWidget(repeat_form_widget)
        self.notification_warning = QLabel(
            "SSH 디버그 로그를 공유하기 전에 반드시 민감한 장비 정보를 확인하세요."
        )
        self.notification_warning.setWordWrap(True)
        self.notification_warning.setStyleSheet("color: #8A5A00;")
        repeat_layout.addWidget(self.notification_warning)
        self.notification_reset_button = QPushButton("알림 고급값 복원", repeat_content)
        self._describe(
            self.notification_reset_button,
            "알림 고급값 복원",
            "반복 알림 간격과 SSH 디버그 로그 옵션만 기본값으로 돌립니다.",
        )
        self.notification_reset_button.clicked.connect(self._reset_notification_defaults)
        repeat_layout.addWidget(self.notification_reset_button)
        self.notification_advanced_section = CollapsibleSection(
            "고급 반복 알림·진단", repeat_content, self.notifications_page
        )
        layout.addWidget(self.notification_advanced_section)
        layout.addStretch(1)
        self.tabs.addTab(self.notifications_page, "알림")

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
            self.mm_test_button,
            "MM 연결 테스트 버튼",
            "SETTINGS-CONNECTION-TEST-MM",
            devices_path + " > 연결 테스트",
            "저장 전에 입력한 MM 연결 설정을 읽기 전용으로 확인합니다.",
        )
        register_widget(
            self.cluster_test_button,
            "클러스터 연결 테스트 버튼",
            "SETTINGS-CONNECTION-TEST-WLC",
            devices_path + " > 연결 테스트",
            "저장 전에 입력한 클러스터 연결 설정을 읽기 전용으로 확인합니다.",
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
            "입력값을 검증한 뒤 설정 적용을 요청합니다.",
        )
        register_widget(
            self.buttons.button(QDialogButtonBox.Cancel),
            "설정 취소 버튼",
            "SETTINGS-CANCEL",
            "설정 > 하단 작업",
            "변경을 적용하지 않고 설정 창을 닫습니다.",
        )

    def _reset_notification_defaults(self) -> None:
        defaults = AppSettings.default().notifications
        self.repeat_unack.setChecked(defaults.repeat_unacknowledged)
        self.repeat_minutes.setValue(defaults.repeat_interval_minutes)
        self.ssh_debug_logging.setChecked(False)

    def _update_credential_mode(self, shared: bool) -> None:
        self.shared_fields.setVisible(shared)
        self.mm_fields.setVisible(not shared)
        self.cluster_fields.setVisible(not shared)

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
            if self.initial_setup:
                candidate.validate_for_monitoring()
            else:
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
