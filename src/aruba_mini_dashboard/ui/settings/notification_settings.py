from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings

from ..widgets import CollapsibleSection


class NotificationSettingsPresentationMixin:
    """Construct notification and diagnostic presentation."""

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

    def _reset_notification_defaults(self) -> None:
        defaults = AppSettings.default().notifications
        self.repeat_unack.setChecked(defaults.repeat_unacknowledged)
        self.repeat_minutes.setValue(defaults.repeat_interval_minutes)
        self.ssh_debug_logging.setChecked(False)
