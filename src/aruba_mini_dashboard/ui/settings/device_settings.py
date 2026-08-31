from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings

from ..widgets import ClickArmedComboBox, CollapsibleSection


class DeviceSettingsPresentationMixin:
    """Construct the existing device, SSH, and credential presentation."""

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
        self.shared_fields = self._create_credential_fields("공통 계정")
        self.mm_fields = self._create_credential_fields("MM 계정")
        self.cluster_fields = self._create_credential_fields("WLC 계정")
        credentials_layout.addWidget(self.shared_fields)
        credentials_layout.addWidget(self.mm_fields)
        credentials_layout.addWidget(self.cluster_fields)
        self.shared_credentials.toggled.connect(self._update_credential_mode)
        self._update_credential_mode(self.shared_credentials.isChecked())
        outer.addWidget(credentials_box)

        diagnostic_content = QWidget(contents)
        diagnostic_layout = QVBoxLayout(diagnostic_content)
        diagnostic_layout.setContentsMargins(12, 2, 0, 4)
        self.connection_diagnostic_button = QPushButton("연결 다시 확인", diagnostic_content)
        self._describe(
            self.connection_diagnostic_button,
            "연결 다시 확인",
            "MM과 모든 Controller의 SSH 호스트 키를 먼저 확인한 뒤 로그인을 한 번에 진단합니다.",
        )
        self.connection_diagnostic_button.clicked.connect(
            lambda: self._emit_connection_test("all")
        )
        diagnostic_layout.addWidget(self.connection_diagnostic_button)
        diagnostic_note = QLabel(
            "일반 저장은 연결 설정이 바뀐 경우에만 같은 점검을 자동 실행합니다. "
            "이미 승인된 동일 지문은 다시 묻지 않으며, 변경된 지문은 저장을 차단합니다.",
            diagnostic_content,
        )
        diagnostic_note.setWordWrap(True)
        diagnostic_layout.addWidget(diagnostic_note)
        self.connection_diagnostic_section = CollapsibleSection(
            "고급 진단", diagnostic_content, contents
        )
        outer.addWidget(self.connection_diagnostic_section)
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

    def _update_credential_mode(self, shared: bool) -> None:
        self.shared_fields.setVisible(shared)
        self.mm_fields.setVisible(not shared)
        self.cluster_fields.setVisible(not shared)
