from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from aruba_mini_dashboard.config import AppSettings, settings_fingerprint

from .detail_dialog import DetailDialog
from .resources import status_icon
from .settings_dialog import SettingsDialog
from .view_models import DashboardView, DeviceView, display, sequence, severity_key, value
from .widgets import NoWheelSlider


LOGGER = logging.getLogger(__name__)


STATUS_STYLES = {
    "normal": ("#176B42", "#E8F7EF", "#2AA56A"),
    "ok": ("#176B42", "#E8F7EF", "#2AA56A"),
    "healthy": ("#176B42", "#E8F7EF", "#2AA56A"),
    "attention": ("#805500", "#FFF5D8", "#E7A900"),
    "warning": ("#805500", "#FFF5D8", "#E7A900"),
    "degraded": ("#805500", "#FFF5D8", "#E7A900"),
    "failure": ("#8A1C1C", "#FDECEC", "#D33A3A"),
    "critical": ("#8A1C1C", "#FDECEC", "#D33A3A"),
    "down": ("#8A1C1C", "#FDECEC", "#D33A3A"),
    "unknown": ("#374151", "#F1F3F5", "#77808D"),
}


class MainWindow(QMainWindow):
    acknowledge_requested = Signal(str)
    acknowledge_global_requested = Signal()
    quit_requested = Signal()
    settings_saved = Signal(object)

    # Keep the long-standing first eight columns stable for operators, tests,
    # and assistive tooling, then add the explicit monitoring-scope fields.
    COLUMNS = (
        "IP",
        "장비명",
        "MM 보고 상태",
        "Active",
        "Standby",
        "Connection-Type",
        "종합 상태",
        "마지막 확인",
        "감시 범위",
        "분배 상태",
    )
    COMPACT_COLUMNS = ("컨트롤러", "상태", "클러스터 분배")
    COMPACT_MODE = "compact"
    FULL_MODE = "full"
    FULL_ENTER_WIDTH = 1000
    COMPACT_ENTER_WIDTH = 900

    def __init__(
        self,
        coordinator: Any,
        settings: AppSettings,
        *,
        settings_store: Any | None = None,
        credential_service: Any | None = None,
        notification_service: Any | None = None,
        storage: Any | None = None,
        settings_apply_handler: Any | None = None,
        demo_mode: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.settings = settings
        self.settings_store = settings_store
        self.credential_service = credential_service
        self.notification_service = notification_service
        self.storage = storage
        self.settings_apply_handler = settings_apply_handler
        self.demo_mode = demo_mode
        self._quitting = False
        self._current_view: DashboardView | None = None
        self._current_devices: list[Any] = []
        self._raw_outputs: Any = {}
        self._parse_results: Any = {}
        self._previous_devices: dict[str, Any] = {}
        self._active_incidents: list[Any] = []
        self._devices_by_ip: dict[str, Any] = {}
        self._pending_scope_refresh_ips: set[str] = set()
        self._base_settings_fingerprint = settings_fingerprint(settings)
        self._detail_windows: list[DetailDialog] = []
        self._dashboard_mode: str | None = None

        self.setWindowTitle("Aruba 네트워크 상태 미니보드" + (" — 데모" if demo_mode else ""))
        self.setMinimumSize(360, 260)
        self.resize(settings.ui.window_width, settings.ui.window_height)
        self._build_ui()
        self._create_tray()
        self._connect_coordinator()
        self._restore_ui_settings()
        self._set_empty_state()
        self._apply_responsive_mode(force=True)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        self.dashboard_stack = QStackedWidget(central)
        self.full_page = self._build_full_page()
        self.compact_page = self._build_compact_page()
        self.dashboard_stack.addWidget(self.compact_page)
        self.dashboard_stack.addWidget(self.full_page)
        root.addWidget(self.dashboard_stack, 1)

        self.check_now_button.clicked.connect(self.coordinator.check_now)
        self.start_button.clicked.connect(self.coordinator.start_automatic)
        self.pause_button.clicked.connect(self.coordinator.pause_automatic)
        self.settings_button.clicked.connect(self.open_settings)
        self.ack_button.clicked.connect(self._acknowledge_selected)
        self.table.itemDoubleClicked.connect(self._open_detail_for_item)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.compact_check_now_button.clicked.connect(self.coordinator.check_now)
        self.compact_auto_button.clicked.connect(self._toggle_automatic)
        self.compact_table.itemDoubleClicked.connect(self._open_detail_for_item)
        self.compact_table.itemSelectionChanged.connect(self._selection_changed)

        self.statusBar().showMessage("대기 중")

    def _build_full_page(self) -> QWidget:
        page = QWidget(self)
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 9, 10, 9)
        root.setSpacing(7)

        self.status_card = QFrame(page)
        self.status_card.setObjectName("statusCard")
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(12, 8, 12, 8)
        status_layout.setSpacing(3)
        top_line = QHBoxLayout()
        self.status_label = QLabel("확인 불가")
        font = self.status_label.font()
        font.setPointSize(max(font.pointSize() + 6, 15))
        font.setBold(True)
        self.status_label.setFont(font)
        self.status_label.setAccessibleName("전체 상태")
        top_line.addWidget(self.status_label)
        top_line.addStretch(1)
        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("color: #52606D;")
        top_line.addWidget(self.busy_label)
        status_layout.addLayout(top_line)
        self.problem_label = QLabel("문제 IP: 확인 전")
        self.problem_label.setWordWrap(True)
        self.problem_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_layout.addWidget(self.problem_label)
        self.reason_label = QLabel("점검을 실행하면 판단 근거가 표시됩니다.")
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_layout.addWidget(self.reason_label)
        root.addWidget(self.status_card)

        time_row = QHBoxLayout()
        self.last_check_label = QLabel("마지막 점검: -")
        self.next_check_label = QLabel("다음 점검: 일시정지")
        self.next_check_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        time_row.addWidget(self.last_check_label, 1)
        time_row.addWidget(self.next_check_label, 1)
        root.addLayout(time_row)

        controls = QGridLayout()
        controls.setSpacing(5)
        self.check_now_button = QPushButton("지금 점검")
        self.start_button = QPushButton("자동 시작")
        self.pause_button = QPushButton("일시정지")
        self.ack_button = QPushButton("알림 확인")
        self.settings_button = QPushButton("설정")
        self.options_button = QToolButton()
        self.options_button.setText("화면")
        self.options_button.setPopupMode(QToolButton.InstantPopup)
        self._build_options_menu()
        for index, button in enumerate((
            self.check_now_button,
            self.start_button,
            self.pause_button,
            self.ack_button,
            self.settings_button,
            self.options_button,
        )):
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            controls.addWidget(button, index // 3, index % 3)
        for column in range(3):
            controls.setColumnStretch(column, 1)
        root.addLayout(controls)

        self.table = QTableWidget(0, len(self.COLUMNS), page)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self._configure_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        root.addWidget(self.table, 1)
        return page

    def _build_compact_page(self) -> QWidget:
        page = QWidget(self)
        root = QVBoxLayout(page)
        root.setContentsMargins(8, 7, 8, 7)
        root.setSpacing(5)

        self.compact_status_card = QFrame(page)
        self.compact_status_card.setObjectName("compactStatusCard")
        status_layout = QHBoxLayout(self.compact_status_card)
        status_layout.setContentsMargins(10, 6, 10, 6)
        status_layout.setSpacing(6)
        self.compact_status_label = QLabel("확인 불가", self.compact_status_card)
        font = self.compact_status_label.font()
        font.setPointSize(max(font.pointSize() + 4, 13))
        font.setBold(True)
        self.compact_status_label.setFont(font)
        self.compact_status_label.setAccessibleName("전체 상태")
        status_layout.addWidget(self.compact_status_label)
        self.compact_busy_label = QLabel("", self.compact_status_card)
        self.compact_busy_label.setStyleSheet("color: #52606D;")
        status_layout.addWidget(self.compact_busy_label)
        status_layout.addStretch(1)
        self.compact_last_check_label = QLabel("마지막: -", self.compact_status_card)
        self.compact_last_check_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.compact_last_check_label.setAccessibleName("마지막 점검 시각")
        status_layout.addWidget(self.compact_last_check_label)
        root.addWidget(self.compact_status_card)

        controls = QHBoxLayout()
        controls.setSpacing(5)
        self.compact_check_now_button = QPushButton("지금 점검", page)
        self.compact_auto_button = QPushButton("자동 시작", page)
        self.compact_more_button = QToolButton(page)
        self.compact_more_button.setText("더보기")
        self.compact_more_button.setPopupMode(QToolButton.InstantPopup)
        self._build_compact_more_menu()
        for button in (
            self.compact_check_now_button,
            self.compact_auto_button,
            self.compact_more_button,
        ):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            controls.addWidget(button, 1)
        root.addLayout(controls)

        self.compact_table = QTableWidget(0, len(self.COMPACT_COLUMNS), page)
        self.compact_table.setHorizontalHeaderLabels(self.COMPACT_COLUMNS)
        self._configure_table(self.compact_table)
        self.compact_table.setSortingEnabled(False)
        self.compact_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.compact_table.setTextElideMode(Qt.ElideMiddle)
        self.compact_table.verticalHeader().setDefaultSectionSize(27)
        header = self.compact_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(1, 78)
        header.resizeSection(2, 126)
        root.addWidget(self.compact_table, 1)
        return page

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        table.setVerticalScrollMode(QTableWidget.ScrollPerPixel)

    def _build_compact_more_menu(self) -> None:
        menu = QMenu(self)
        self.compact_ack_action = menu.addAction("알림 확인", self._acknowledge_selected)
        self.compact_settings_action = menu.addAction("설정", self.open_settings)
        menu.addSeparator()
        screen_menu = menu.addMenu("화면")
        self.compact_always_on_top_action = QAction("항상 위에 표시", self, checkable=True)
        self.compact_always_on_top_action.toggled.connect(self.set_always_on_top)
        screen_menu.addAction(self.compact_always_on_top_action)
        opacity_container = QWidget(screen_menu)
        opacity_layout = QHBoxLayout(opacity_container)
        opacity_layout.setContentsMargins(10, 4, 10, 4)
        opacity_layout.addWidget(QLabel("투명도"))
        self.compact_opacity_slider = NoWheelSlider(Qt.Horizontal, opacity_container)
        self.compact_opacity_slider.setRange(40, 100)
        self.compact_opacity_slider.setMinimumWidth(100)
        self.compact_opacity_number = QLabel("100%")
        opacity_layout.addWidget(self.compact_opacity_slider, 1)
        opacity_layout.addWidget(self.compact_opacity_number)
        opacity_action = QWidgetAction(screen_menu)
        opacity_action.setDefaultWidget(opacity_container)
        screen_menu.addAction(opacity_action)
        screen_menu.addAction("화면 설정 기본값 복원", self.reset_window_options)
        menu.addAction("전체 보기", self.showMaximized)
        menu.addSeparator()
        menu.addAction("종료", self.request_quit)
        self.compact_opacity_slider.valueChanged.connect(self.set_opacity_percent)
        self.compact_more_button.setMenu(menu)

    def _build_options_menu(self) -> None:
        menu = QMenu(self)
        self.always_on_top_action = QAction("항상 위에 표시", self, checkable=True)
        self.always_on_top_action.toggled.connect(self.set_always_on_top)
        menu.addAction(self.always_on_top_action)
        opacity_container = QWidget(menu)
        opacity_layout = QHBoxLayout(opacity_container)
        opacity_layout.setContentsMargins(10, 4, 10, 4)
        opacity_layout.addWidget(QLabel("투명도"))
        self.opacity_slider = NoWheelSlider(Qt.Horizontal, opacity_container)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.setMinimumWidth(110)
        self.opacity_number = QLabel("100%")
        opacity_layout.addWidget(self.opacity_slider, 1)
        opacity_layout.addWidget(self.opacity_number)
        action = QWidgetAction(menu)
        action.setDefaultWidget(opacity_container)
        menu.addAction(action)
        reset = QAction("화면 설정 기본값 복원", self)
        reset.triggered.connect(self.reset_window_options)
        menu.addAction(reset)
        self.opacity_slider.valueChanged.connect(self.set_opacity_percent)
        self.options_button.setMenu(menu)

    @Slot()
    def _toggle_automatic(self) -> None:
        if self.coordinator.automatic:
            self.coordinator.pause_automatic()
        else:
            self.coordinator.start_automatic()

    def _active_table(self) -> QTableWidget:
        if self._dashboard_mode == self.FULL_MODE:
            return self.table
        return self.compact_table

    def _apply_responsive_mode(self, *, force: bool = False) -> None:
        if not hasattr(self, "dashboard_stack"):
            return
        previous = self._dashboard_mode
        if self.isMaximized() or self.width() >= self.FULL_ENTER_WIDTH:
            target = self.FULL_MODE
        elif self.width() < self.COMPACT_ENTER_WIDTH:
            target = self.COMPACT_MODE
        elif previous is None:
            target = self.COMPACT_MODE
        else:
            target = previous
        if not force and target == previous:
            return
        selected_ip = self._selected_ip()
        self._dashboard_mode = target
        target_page = self.full_page if target == self.FULL_MODE else self.compact_page
        self.dashboard_stack.setCurrentWidget(target_page)
        if selected_ip:
            self._select_ip(self._active_table(), selected_ip)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_responsive_mode()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._apply_responsive_mode(force=True)

    def _create_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(status_icon("unknown"), self)
        self.tray_icon.setToolTip("Aruba 네트워크 상태 미니보드")
        menu = QMenu()
        show_action = menu.addAction("대시보드 열기")
        show_action.triggered.connect(self.show_dashboard)
        menu.addSeparator()
        menu.addAction("지금 점검", self.coordinator.check_now)
        menu.addAction("자동 점검 시작", self.coordinator.start_automatic)
        menu.addAction("자동 점검 일시정지", self.coordinator.pause_automatic)
        menu.addAction("설정", self.open_settings)
        menu.addSeparator()
        quit_action = menu.addAction("종료")
        quit_action.triggered.connect(self.request_quit)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()
        else:
            LOGGER.warning("TRAY_UNAVAILABLE: system tray is not available")
        if self.notification_service is not None:
            self.notification_service.tray_icon = self.tray_icon
            failed_signal = getattr(self.notification_service, "notification_failed", None)
            if failed_signal is not None:
                failed_signal.connect(self._notification_failed)

    def _connect_coordinator(self) -> None:
        self.coordinator.cycle_started.connect(self._cycle_started)
        self.coordinator.cycle_finished.connect(self.update_snapshot)
        self.coordinator.cycle_failed.connect(self._cycle_failed)
        self.coordinator.busy_changed.connect(self._busy_changed)
        self.coordinator.automatic_changed.connect(self._automatic_changed)
        self.coordinator.next_check_changed.connect(self._next_check_changed)
        self.coordinator.scheduled_poll_skipped.connect(self.statusBar().showMessage)
        self.coordinator.manual_poll_queued.connect(
            lambda: self.statusBar().showMessage("현재 점검 후 한 번 더 점검합니다.", 5000)
        )
        if hasattr(self.coordinator, "connection_test_started"):
            self.coordinator.connection_test_started.connect(
                lambda role: self.statusBar().showMessage(
                    f"{'MM' if role == 'mm' else '클러스터'} SSH 연결을 확인하고 있습니다…"
                )
            )
            self.coordinator.connection_test_finished.connect(self._connection_test_finished)
            self.coordinator.connection_test_failed.connect(self._connection_test_failed)
        if hasattr(self.coordinator, "automatic_start_rejected"):
            self.coordinator.automatic_start_rejected.connect(self._automatic_start_rejected)

    def _restore_ui_settings(self) -> None:
        ui = self.settings.ui
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(max(40, min(100, ui.opacity_percent)))
        self.opacity_slider.blockSignals(False)
        self.set_opacity_percent(self.opacity_slider.value(), persist=False)
        self.always_on_top_action.blockSignals(True)
        self.always_on_top_action.setChecked(ui.always_on_top)
        self.always_on_top_action.blockSignals(False)
        self.set_always_on_top(ui.always_on_top, persist=False)
        if ui.window_x is not None and ui.window_y is not None:
            self.move(self._visible_position(QPoint(ui.window_x, ui.window_y), self.size()))
        if ui.window_maximized:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)
        self.start_button.setEnabled(not self.coordinator.automatic and not self.coordinator.busy)
        self.pause_button.setEnabled(self.coordinator.automatic)
        self.compact_auto_button.setText("일시정지" if self.coordinator.automatic else "자동 시작")
        self.compact_auto_button.setEnabled(
            self.coordinator.automatic or not self.coordinator.busy
        )
        self.compact_settings_action.setEnabled(not self.coordinator.busy)
        if not self.coordinator.automatic:
            self.next_check_label.setText("다음 점검: 일시정지")

    def _set_empty_state(self) -> None:
        self._apply_status_style("unknown")
        self.status_label.setText("확인 불가")
        self.compact_status_label.setText("확인 불가")
        self.problem_label.setText("문제 IP: 확인 전")
        self.reason_label.setText("점검을 실행하면 판단 근거가 표시됩니다.")
        self.ack_button.setEnabled(False)
        self.compact_ack_action.setEnabled(False)

    @Slot(object)
    def update_snapshot(self, result: Any) -> None:
        view = DashboardView.from_source(result)
        self._pending_scope_refresh_ips.clear()
        self._current_view = view
        self._current_devices = [device.source for device in view.devices]
        self._devices_by_ip = {device.ip: device.source for device in view.devices if device.ip}
        self._raw_outputs = value(result, "raw_outputs", {})
        self._parse_results = value(result, "parse_results", {})
        self._previous_devices = dict(value(result, "previous_devices", {}) or {})
        self._active_incidents = sequence(result, "active_incidents")
        self.status_label.setText(view.status)
        self.compact_status_label.setText(view.status)
        self._apply_status_style(view.status_key)
        if not view.problem_ips:
            self.problem_label.setText("문제 IP: 없음")
        elif len(view.problem_ips) == 1:
            device = next((item for item in view.devices if item.ip == view.problem_ips[0]), None)
            alias = f" / {device.alias or device.hostname}" if device and (device.alias or device.hostname) else ""
            self.problem_label.setText(f"주요 문제 IP: {view.problem_ips[0]}{alias}")
        else:
            by_ip = {item.ip: item for item in view.devices}
            problem_lines = []
            for ip in view.problem_ips:
                device = by_ip.get(ip)
                reasons = [] if device is None else device.issue_reasons[:2]
                cause = ", ".join(reasons) if reasons else (device.status if device else "이상 감지")
                problem_lines.append(f"• {ip}: {cause}")
            self.problem_label.setText(
                f"문제 IP {len(view.problem_ips)}개 감지\n" + "\n".join(problem_lines)
            )
        self.reason_label.setText(
            "판단 근거: " + (" / ".join(view.reasons[:4]) if view.reasons else "이상 신호 없음")
        )
        self.last_check_label.setText(f"마지막 점검: {view.checked_at}")
        self.compact_last_check_label.setText(f"마지막: {view.checked_at_short}")
        self.compact_last_check_label.setToolTip(f"마지막 점검: {view.checked_at}")
        self._populate_table(view.devices)
        self._update_icons(view.status_key)
        self._notify_from_result(result)
        hidden_count = max(0, len(view.devices) - len(self._compact_devices(view.devices)))
        message = "점검 완료"
        if self._dashboard_mode == self.COMPACT_MODE and hidden_count:
            message += f" · 미등록 장비 {hidden_count}대 숨김"
        self.statusBar().showMessage(message, 5000)
        self._selection_changed()

    def _populate_table(self, devices: list[DeviceView]) -> None:
        selected_ip = self._selected_ip()
        display_devices = self._scope_adjusted_devices(devices)
        for device in display_devices:
            self._devices_by_ip.setdefault(device.ip, device.source)
        self._populate_full_table(display_devices)
        compact_devices = self._compact_devices(display_devices)
        self._populate_compact_table(compact_devices)
        if selected_ip:
            self._select_ip(self._active_table(), selected_ip)

    def _populate_full_table(self, devices: list[DeviceView]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            registered = self._device_is_registered(device)
            display_status = device.status if registered else "감시 제외"
            display_status_key = device.status_key if registered else "unknown"
            values = (
                device.ip,
                device.alias or device.hostname or "-",
                device.controller_status,
                device.active_clients,
                device.standby_clients,
                device.connection_type,
                display_status,
                device.last_seen,
                "등록" if registered else "미등록 · 감시 제외",
                device.distribution_status,
            )
            foreground, _background, _accent = STATUS_STYLES.get(
                display_status_key, STATUS_STYLES["unknown"]
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, device.ip)
                item.setToolTip(str(text))
                if column == 6:
                    item.setForeground(QColor(foreground))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setIcon(status_icon(display_status_key))
                elif column == 8 and not registered:
                    item.setForeground(QColor(STATUS_STYLES["unknown"][0]))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)

    def _populate_compact_table(self, devices: list[DeviceView]) -> None:
        self.compact_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            name = device.alias or device.hostname or "컨트롤러"
            values = (
                f"{name} · {device.ip}",
                device.controller_status,
                device.distribution_status,
            )
            controller_key = {
                "up": "normal",
                "down": "failure",
                "missing": "attention",
            }.get(device.controller_state, "unknown")
            distribution_key = {
                "normal": "normal",
                "observing": "attention",
                "anomalous": "attention",
                "recovering": "attention",
                "low_usage": "attention",
                "missing": "attention",
            }.get(device.distribution_state, "unknown")
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, device.ip)
                item.setToolTip(f"{name}\nIP: {device.ip}" if column == 0 else str(text))
                if column in {1, 2}:
                    style_key = controller_key if column == 1 else distribution_key
                    foreground = STATUS_STYLES.get(style_key, STATUS_STYLES["unknown"])[0]
                    item.setForeground(QColor(foreground))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setIcon(status_icon(style_key))
                self.compact_table.setItem(row, column, item)

    def _compact_devices(self, devices: list[DeviceView]) -> list[DeviceView]:
        by_ip = {device.ip: device for device in devices}
        scope = [member.ip.strip() for member in self.settings.cluster.members if member.ip.strip()]
        if not scope:
            scope = list(self._current_view.monitoring_scope_ips if self._current_view else ())
        if not scope:
            scope = [device.ip for device in devices if device.is_registered]
        ordered = [by_ip[ip] for ip in scope if ip in by_ip and self._device_is_registered(by_ip[ip])]
        included = {device.ip for device in ordered}
        ordered.extend(
            device for device in devices if self._device_is_registered(device) and device.ip not in included
        )
        return ordered

    def _scope_adjusted_devices(self, devices: list[DeviceView]) -> list[DeviceView]:
        """Reclassify the cached snapshot against the current member settings.

        Applying a member-list edit does not run SSH implicitly. Newly added
        controllers therefore appear immediately with unknown values until the
        next explicit or scheduled poll, while removed controllers remain as
        informational inventory in the full view.
        """

        configured = {
            member.ip.strip(): member.alias.strip()
            for member in self.settings.cluster.members
            if member.ip.strip()
        }
        if not configured:
            return devices
        adjusted: list[DeviceView] = []
        seen: set[str] = set()
        for device in devices:
            registered = device.ip in configured
            alias = configured.get(device.ip) or device.alias
            if device.ip in self._pending_scope_refresh_ips:
                adjusted.append(
                    replace(
                        device,
                        alias=alias,
                        is_registered=True,
                        controller_state="unknown",
                        controller_status="확인 불가",
                        distribution_state="unknown",
                        distribution_status="확인 불가",
                        status="확인 불가",
                        status_key="unknown",
                        issue_reasons=[],
                    )
                )
            else:
                adjusted.append(replace(device, alias=alias, is_registered=registered))
            seen.add(device.ip)
        for ip, alias in configured.items():
            if ip in seen:
                continue
            adjusted.append(
                DeviceView.from_source(
                    {
                        "ip": ip,
                        "alias": alias,
                        "is_registered": True,
                        "controller_state": "unknown",
                        "distribution_state": "unknown",
                        "severity": "unknown",
                    }
                )
            )
        return adjusted

    def _device_is_registered(self, device: DeviceView) -> bool:
        configured = {member.ip.strip() for member in self.settings.cluster.members if member.ip.strip()}
        if configured:
            return device.ip in configured
        if self._current_view and self._current_view.monitoring_scope_ips:
            return device.ip in self._current_view.monitoring_scope_ips
        return device.is_registered

    @staticmethod
    def _select_ip(table: QTableWidget, ip: str) -> bool:
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and display(item.data(Qt.UserRole), "") == ip:
                table.selectRow(row)
                return True
        return False

    def _apply_status_style(self, key: str) -> None:
        foreground, background, accent = STATUS_STYLES.get(key, STATUS_STYLES["unknown"])
        self.status_card.setStyleSheet(
            "QFrame#statusCard {"
            f"background: {background}; border: 1px solid {accent}; border-left: 5px solid {accent};"
            "border-radius: 6px;}"
        )
        self.compact_status_card.setStyleSheet(
            "QFrame#compactStatusCard {"
            f"background: {background}; border: 1px solid {accent}; border-left: 5px solid {accent};"
            "border-radius: 6px;}"
        )
        self.status_label.setStyleSheet(f"color: {foreground};")
        self.compact_status_label.setStyleSheet(f"color: {foreground};")

    def _update_icons(self, key: str) -> None:
        icon = status_icon(key)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip(f"Aruba 미니보드 — {self.status_label.text()}")

    def _notify_from_result(self, result: Any) -> None:
        if self.notification_service is None:
            return
        candidates = sequence(result, "notification_events", "new_incidents", "events", "signals")
        if not candidates:
            return
        try:
            from aruba_mini_dashboard.services.notification_service import NotificationEvent

            events = []
            devices_by_ip = {
                display(value(device, "ip", ""), ""): device
                for device in sequence(result, "devices")
            }
            for incident in candidates:
                details = value(incident, "details", {})
                recovered = bool(
                    value(incident, "recovered", False)
                    or value(incident, "recovered_at", None)
                    or value(details, "recovered", False)
                    or value(details, "transition", "") == "recovered"
                )
                active = bool(value(incident, "active", not recovered))
                incident_type = display(
                    value(
                        incident,
                        "incident_type",
                        value(incident, "issue_type", value(incident, "type", "unknown")),
                    )
                )
                event_token = display(value(incident, "event_token", ""), "")
                incident_id = display(value(incident, "incident_id", ""), "")
                first_detected = display(value(incident, "first_detected_at", ""), "")
                identity = event_token or incident_id or first_detected
                notification_key = f"{incident_type}:{identity}" if identity else incident_type
                ip = display(value(incident, "ip", ""), "")
                device = devices_by_ip.get(ip)
                alias = display(value(incident, "alias", ""), "")
                if not alias and device is not None:
                    alias = display(
                        value(device, "alias", value(device, "hostname", "")),
                        "",
                    )
                reason = display(
                    value(incident, "message", value(incident, "reason", "상태 변화가 감지되었습니다."))
                )
                severity = display(
                    value(device, "severity", value(incident, "severity", "warning"))
                    if device is not None
                    else value(incident, "severity", "warning")
                )
                is_collection_failure = incident_type == "collection_failure" or severity == "unknown"
                if device is not None and not is_collection_failure and active and not recovered:
                    current_reasons = sequence(device, "issue_reasons")
                    if current_reasons:
                        reason = " / ".join(display(item) for item in current_reasons)
                if recovered or not active:
                    title = "Aruba 복구 알림"
                    cause = f"이전 원인 해제: {reason}"
                elif is_collection_failure:
                    title = "Aruba 수집 확인 불가"
                    cause = reason
                elif severity == "critical":
                    title = "Aruba 장애 감지"
                    cause = reason
                else:
                    title = "Aruba 주의 감지"
                    cause = reason
                message = (
                    f"IP: {ip or '특정 불가'}\n"
                    f"장비명: {alias or '-'}\n"
                    f"원인: {cause}\n"
                    f"감지 시각: {first_detected or display(datetime.now())}"
                )
                event = NotificationEvent(
                    ip=ip,
                    issue_type=notification_key,
                    title=title,
                    message=message,
                    detected_at=value(
                        incident,
                        "detected_at",
                        value(incident, "first_detected_at", datetime.now()),
                    ),
                    active=active and not recovered,
                    acknowledged=bool(value(incident, "acknowledged", False)),
                    severity=severity,
                    incident_id=incident_id,
                )
                events.append(event)
            self.notification_service.notify_many(events)
        except Exception:
            LOGGER.exception("NOTIFICATION_UNAVAILABLE: notification dispatch failed")

    @Slot(str, object)
    def _cycle_started(self, trigger: str, started_at: datetime) -> None:
        self.statusBar().showMessage("장비 상태를 점검하고 있습니다…")

    @Slot(object)
    def _cycle_failed(self, error: BaseException) -> None:
        LOGGER.exception("Poll cycle failed", exc_info=(type(error), error, error.__traceback__))
        self.status_label.setText("확인 불가")
        self.compact_status_label.setText("확인 불가")
        self.problem_label.setText("최종 판단: 확인 불가")
        from aruba_mini_dashboard.collectors.base import SshOperationError
        from aruba_mini_dashboard.config import SettingsError
        from aruba_mini_dashboard.credentials import CredentialError
        from aruba_mini_dashboard.errors import AppError
        from aruba_mini_dashboard.storage import StorageError

        safe_types = (AppError, SettingsError, CredentialError, SshOperationError, StorageError)
        reason = str(error) if isinstance(error, safe_types) else "점검 처리 중 오류가 발생했습니다. 로그를 확인하세요."
        self.reason_label.setText("원인: " + reason)
        self._apply_status_style("unknown")
        self._update_icons("unknown")
        self.statusBar().showMessage("점검 실패", 5000)

    @Slot(str)
    def _automatic_start_rejected(self, reason: str) -> None:
        self.settings.polling.automatic_enabled = False
        self.next_check_label.setText("다음 점검: 일시정지")
        self.compact_auto_button.setText("자동 시작")
        self._persist_preference("polling.automatic_enabled", False)
        QMessageBox.warning(self, "자동 점검 시작 불가", reason)
        self.statusBar().showMessage("자동 점검 일시정지: " + reason, 10000)

    @Slot(bool)
    def _busy_changed(self, busy: bool) -> None:
        self.busy_label.setText("● 점검 중" if busy else "")
        self.compact_busy_label.setText("● 점검 중" if busy else "")
        self.start_button.setEnabled(not busy and not self.coordinator.automatic)
        self.pause_button.setEnabled(self.coordinator.automatic)
        self.settings_button.setEnabled(not busy)
        self.compact_check_now_button.setEnabled(not busy)
        self.compact_auto_button.setEnabled(self.coordinator.automatic or not busy)
        self.compact_settings_action.setEnabled(not busy)
        if self._quitting and not busy:
            self._complete_quit()

    @Slot(bool)
    def _automatic_changed(self, automatic: bool) -> None:
        self.start_button.setEnabled(not automatic and not self.coordinator.busy)
        self.pause_button.setEnabled(automatic)
        self.compact_auto_button.setText("일시정지" if automatic else "자동 시작")
        self.compact_auto_button.setEnabled(automatic or not self.coordinator.busy)
        if not automatic:
            self.next_check_label.setText("다음 점검: 일시정지")
        self.settings.polling.automatic_enabled = automatic
        self._persist_preference("polling.automatic_enabled", automatic)

    @Slot(object)
    def _next_check_changed(self, next_check: datetime | None) -> None:
        self.next_check_label.setText(
            "다음 점검: " + (next_check.strftime("%Y-%m-%d %H:%M:%S") if next_check else "일시정지")
        )

    @Slot()
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.credential_service, self)
        dialog.connection_test_requested.connect(self._connection_test_requested)
        if self.notification_service is not None:
            dialog.sound_test_requested.connect(self.notification_service.test_sound)
            dialog.notification_test_requested.connect(self.notification_service.test_notification)
        # A scheduled poll can begin while this modal dialog is open.  Keep the
        # same dialog (and the operator's typed values) available when applying
        # is temporarily rejected, instead of closing it and discarding the
        # form.  Staged credentials are committed only after every settings
        # layer accepted the candidate, or rolled back when the operator exits.
        while True:
            if dialog.exec() != QDialog.Accepted:
                dialog.rollback_staged_credentials()
                return
            if self.apply_settings(dialog.settings):
                dialog.commit_staged_credentials()
                return

    @Slot(object)
    def apply_settings(self, settings: AppSettings) -> bool:
        if self.coordinator.busy:
            QMessageBox.information(
                self,
                "설정 적용 대기",
                "현재 점검이 진행 중입니다. 점검이 끝난 뒤 설정을 다시 저장해 주세요.",
            )
            return False
        # Persist first. A failed atomic JSON write must leave the active UI,
        # coordinator, runtime and SQLite preference mirror unchanged.
        previous_settings = self.settings
        previous_member_ips = tuple(
            member.ip.strip() for member in previous_settings.cluster.members if member.ip.strip()
        )
        if self.settings_store is not None:
            try:
                settings.validate()
                self.settings_store.save(settings)
            except Exception as exc:
                QMessageBox.warning(self, "설정 저장 실패", str(exc))
                return False
        if self.settings_apply_handler is not None:
            try:
                self.settings_apply_handler(settings)
            except Exception as exc:
                if self.settings_store is not None:
                    try:
                        self.settings_store.save(previous_settings)
                    except Exception:
                        LOGGER.critical("Settings rollback failed", exc_info=True)
                QMessageBox.warning(self, "설정 적용 실패", str(exc))
                return False
        previous_auto = self.coordinator.automatic
        self.settings = settings
        current_member_ips = tuple(
            member.ip.strip() for member in settings.cluster.members if member.ip.strip()
        )
        self._base_settings_fingerprint = settings_fingerprint(settings)
        self.coordinator.set_interval(settings.polling.interval_seconds)
        self.set_opacity_percent(settings.ui.opacity_percent, persist=False)
        self.set_always_on_top(settings.ui.always_on_top, persist=False)
        if self._current_view is not None:
            if current_member_ips != previous_member_ips:
                self._mark_scope_change_pending(current_member_ips, previous_member_ips)
            self._populate_table(self._current_view.devices)
        if self.notification_service is not None:
            self.notification_service.configure(
                sound_enabled=settings.notifications.sound_enabled,
                repeat_enabled=settings.notifications.repeat_unacknowledged,
                repeat_minutes=settings.notifications.repeat_interval_minutes,
                recovery_enabled=settings.notifications.recovery_notifications,
            )
        self._mirror_all_settings()
        if settings.polling.automatic_enabled and not previous_auto:
            self.coordinator.start_automatic()
        elif not settings.polling.automatic_enabled and previous_auto:
            self.coordinator.pause_automatic()
        self.settings_saved.emit(settings)
        self.statusBar().showMessage("설정을 저장했습니다.", 5000)
        return True

    def _mark_scope_change_pending(
        self,
        member_ips: tuple[str, ...],
        previous_member_ips: tuple[str, ...],
    ) -> None:
        """Make a changed monitoring scope explicit until the next poll."""

        if self._current_view is None:
            return
        self._pending_scope_refresh_ips = set(member_ips) - set(previous_member_ips)
        self._current_view.monitoring_scope_ips = list(member_ips)
        self._current_view.problem_ips = []
        self._current_view.status = "확인 불가"
        self._current_view.status_key = "unknown"
        self._current_view.reasons = ["구성원 설정이 변경되었습니다. 다음 점검 결과를 기다립니다."]
        self.status_label.setText("확인 불가")
        self.compact_status_label.setText("확인 불가")
        self.problem_label.setText("문제 IP: 확인 전")
        self.reason_label.setText("판단 근거: 구성원 설정 변경 후 점검 대기")
        self._apply_status_style("unknown")
        self.ack_button.setEnabled(False)
        self.compact_ack_action.setEnabled(False)

    @Slot(str, object)
    def _connection_test_requested(self, role: str, settings: Any) -> None:
        tester = getattr(self.coordinator, "test_connection", None)
        if not callable(tester):
            QMessageBox.information(self, "연결 테스트", "실행 중인 수집기에서 연결 테스트를 제공하지 않습니다.")
            return
        tester(role, settings)

    @Slot(str, object)
    def _connection_test_finished(self, role: str, result: Any) -> None:
        status = display(value(result, "status", "failed"))
        host = display(value(result, "host", ""), "")
        port = display(value(result, "port", ""), "")
        fingerprint = display(value(result, "fingerprint", ""), "")
        algorithm = display(value(result, "algorithm", ""), "")
        message = display(value(result, "message", "연결 테스트가 완료되었습니다."))
        if status == "approval_required":
            details = (
                f"대상: {host}:{port}\n"
                f"키 형식: {algorithm}\n"
                f"SHA-256 지문: {fingerprint}\n\n"
                "장비 관리자에게 받은 지문과 일치할 때만 승인하세요. "
                "승인하면 앱 전용 known_hosts에 저장한 뒤 로그인을 다시 확인합니다."
            )
            answer = QMessageBox.question(
                self,
                "최초 SSH 호스트 키 승인",
                details,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.statusBar().showMessage("SSH 호스트 키 승인을 취소했습니다.", 5000)
                return
            try:
                self.coordinator.approve_host_key(value(result, "scanned"))
                self.coordinator.retry_connection_test(role)
            except Exception as exc:
                QMessageBox.critical(self, "SSH 호스트 키 저장 실패", str(exc))
            return
        if status == "mismatch":
            expected = ", ".join(value(result, "expected_fingerprints", ())) or "저장된 값 확인 필요"
            QMessageBox.critical(
                self,
                "SSH 호스트 키 불일치",
                f"{message}\n\n대상: {host}:{port}\n현재 지문: {fingerprint}\n저장된 지문: {expected}",
            )
            self.statusBar().showMessage("SSH 키 불일치로 연결을 차단했습니다.", 10000)
            return
        QMessageBox.information(
            self,
            "연결 테스트 성공",
            f"{message}\n\n대상: {host}:{port}\nSHA-256 지문: {fingerprint}",
        )
        self.statusBar().showMessage("SSH 연결 테스트를 완료했습니다.", 5000)

    @Slot(str, object)
    def _connection_test_failed(self, role: str, error: BaseException) -> None:
        QMessageBox.warning(
            self,
            "연결 테스트 실패",
            str(error) or "SSH 연결을 확인하지 못했습니다. 설정과 로그를 확인하세요.",
        )
        self.statusBar().showMessage("SSH 연결 테스트에 실패했습니다.", 5000)

    @Slot(str)
    def _notification_failed(self, message: str) -> None:
        LOGGER.warning("NOTIFICATION_UNAVAILABLE: %s", message)
        self.statusBar().showMessage(message, 10000)

    @Slot(object)
    def _open_detail_for_item(self, item: QTableWidgetItem) -> None:
        ip = display(item.data(Qt.UserRole), "")
        source = self._devices_by_ip.get(ip)
        if source is None:
            return
        dialog = DetailDialog(
            source,
            self,
            raw_outputs=self._raw_outputs,
            parsed_results=self._parse_results,
            previous_device=self._previous_devices.get(ip),
        )
        self._detail_windows.append(dialog)
        dialog.destroyed.connect(lambda: self._detail_windows.remove(dialog) if dialog in self._detail_windows else None)
        dialog.show()

    @Slot()
    def _selection_changed(self) -> None:
        if self._current_view is None:
            self.ack_button.setEnabled(False)
            return
        selected_ip = self._selected_ip()
        if selected_ip:
            selected_device = next(
                (device for device in self._current_view.devices if device.ip == selected_ip),
                None,
            )
            enabled = bool(
                selected_device is not None
                and self._device_is_registered(selected_device)
                and (
                    selected_ip in self._current_view.problem_ips
                    or self._has_active_incident_for_ip(selected_ip)
                )
            )
        else:
            enabled = (
                len(self._current_view.problem_ips) == 1
                or self._has_active_collection_incident()
            )
        self.ack_button.setEnabled(enabled)
        self.compact_ack_action.setEnabled(enabled)

    @Slot()
    def _acknowledge_selected(self) -> None:
        ip = self._selected_ip()
        if ip:
            selected_device = next(
                (device for device in self._current_view.devices if device.ip == ip),
                None,
            ) if self._current_view else None
            if self._current_view and selected_device is not None and self._device_is_registered(
                selected_device
            ) and (ip in self._current_view.problem_ips or self._has_active_incident_for_ip(ip)):
                self.acknowledge_requested.emit(ip)
                self.statusBar().showMessage(f"{ip}의 현재 알림을 확인 처리했습니다.", 5000)
                return
            self.statusBar().showMessage("선택한 행에는 확인 처리할 활성 문제가 없습니다.", 5000)
            return
        if self._current_view and len(self._current_view.problem_ips) == 1:
            ip = self._current_view.problem_ips[0]
            self.acknowledge_requested.emit(ip)
            self.statusBar().showMessage(f"{ip}의 현재 알림을 확인 처리했습니다.", 5000)
            return
        if self._has_active_collection_incident():
            self.acknowledge_global_requested.emit()
            self.statusBar().showMessage("현재 수집 오류 알림을 확인 처리했습니다.", 5000)
            return
        self.statusBar().showMessage("확인 처리할 문제 IP를 선택하세요.", 5000)

    def _selected_ip(self) -> str:
        tables = (self._active_table(), self.table, self.compact_table)
        seen: set[int] = set()
        for table in tables:
            if id(table) in seen:
                continue
            seen.add(id(table))
            selected = table.selectedItems()
            if selected:
                return display(selected[0].data(Qt.UserRole), "")
        return ""

    @staticmethod
    def _incident_is_active(incident: Any) -> bool:
        return bool(value(incident, "active", True)) and not bool(
            value(incident, "acknowledged", False)
        )

    def _has_active_incident_for_ip(self, ip: str) -> bool:
        return any(
            self._incident_is_active(incident)
            and display(value(incident, "ip", ""), "") == ip
            for incident in self._active_incidents
        )

    def _has_active_collection_incident(self) -> bool:
        return any(
            self._incident_is_active(incident)
            and display(value(incident, "incident_type", ""), "").casefold()
            == "collection_failure"
            for incident in self._active_incidents
        )

    @Slot(bool)
    def set_always_on_top(self, enabled: bool, *, persist: bool = True) -> None:
        geometry = self.geometry()
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(enabled))
        self.setGeometry(geometry)
        if was_visible:
            self.show()
        self.always_on_top_action.blockSignals(True)
        self.always_on_top_action.setChecked(bool(enabled))
        self.always_on_top_action.blockSignals(False)
        self.compact_always_on_top_action.blockSignals(True)
        self.compact_always_on_top_action.setChecked(bool(enabled))
        self.compact_always_on_top_action.blockSignals(False)
        if persist:
            self.settings.ui.always_on_top = bool(enabled)
            self._persist_preference("ui.always_on_top", bool(enabled))

    @Slot(int)
    def set_opacity_percent(self, percent: int, *, persist: bool = True) -> None:
        percent = max(40, min(100, int(percent)))
        self.setWindowOpacity(percent / 100.0)
        self.opacity_number.setText(f"{percent}%")
        if self.opacity_slider.value() != percent:
            self.opacity_slider.blockSignals(True)
            self.opacity_slider.setValue(percent)
            self.opacity_slider.blockSignals(False)
        self.compact_opacity_number.setText(f"{percent}%")
        if self.compact_opacity_slider.value() != percent:
            self.compact_opacity_slider.blockSignals(True)
            self.compact_opacity_slider.setValue(percent)
            self.compact_opacity_slider.blockSignals(False)
        if persist:
            self.settings.ui.opacity_percent = percent
            self._persist_preference("ui.opacity_percent", percent)

    @Slot()
    def reset_window_options(self) -> None:
        self.set_always_on_top(False)
        self.set_opacity_percent(100)

    @Slot()
    def show_dashboard(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick}:
            self.show_dashboard()

    @Slot()
    def request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._save_window_state()
        self.tray_icon.hide()
        self.quit_requested.emit()
        requester = getattr(self.coordinator, "request_shutdown", None)
        if callable(requester):
            requester()
        if self.coordinator.busy:
            self.show_dashboard()
            self.statusBar().showMessage("진행 중인 SSH 작업을 취소하고 안전하게 종료하는 중입니다…")
            self.setEnabled(False)
            return
        self._complete_quit()

    def _complete_quit(self) -> None:
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._save_window_state()
        if not self._quitting and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Aruba 미니 대시보드",
                "모니터링은 시스템 트레이에서 계속 실행됩니다.",
                QSystemTrayIcon.Information,
                4000,
            )
            return
        if not self._quitting:
            event.ignore()
            self.request_quit()
            return
        if self.coordinator.busy:
            event.ignore()
            return
        event.accept()

    def _save_window_state(self) -> None:
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        ui = self.settings.ui
        ui.window_x = geometry.x()
        ui.window_y = geometry.y()
        ui.window_width = max(360, geometry.width())
        ui.window_height = max(260, geometry.height())
        ui.window_maximized = self.isMaximized()
        ui.opacity_percent = self.opacity_slider.value()
        ui.always_on_top = self.always_on_top_action.isChecked()
        self.settings.polling.automatic_enabled = self.coordinator.automatic
        if self.settings_store is not None:
            try:
                self.settings_store.save(self.settings)
                self._base_settings_fingerprint = settings_fingerprint(self.settings)
            except Exception:
                LOGGER.exception("Could not persist window state")
                return
        else:
            self._base_settings_fingerprint = settings_fingerprint(self.settings)
        self._mirror_all_settings()

    def _persist_preference(self, key: str, value_: Any) -> None:
        if self.storage is None:
            return
        try:
            batch_setter = getattr(self.storage, "set_preferences", None)
            if callable(batch_setter):
                # Rewrite the complete mirror so advancing its fingerprint can
                # never make unrelated stale preference rows authoritative.
                batch_setter(self._settings_preference_values())
                return
            setter = getattr(self.storage, "set_setting", None)
            if callable(setter):
                setter(key, value_)
        except Exception:
            LOGGER.exception("Could not persist UI preference %s", key)

    def _mirror_all_settings(self) -> None:
        values = self._settings_preference_values()
        batch_setter = getattr(self.storage, "set_preferences", None) if self.storage is not None else None
        if callable(batch_setter):
            try:
                batch_setter(values)
            except Exception:
                LOGGER.exception("Could not persist settings preference mirror")
            return
        for key, value_ in values.items():
            self._persist_preference(key, value_)

    def _settings_preference_values(self) -> dict[str, Any]:
        polling = self.settings.polling
        detection = self.settings.detection
        notifications = self.settings.notifications
        ui = self.settings.ui
        values = {
            "polling.interval_seconds": polling.interval_seconds,
            "polling.automatic_enabled": polling.automatic_enabled,
            "detection.low_client_threshold": detection.low_client_threshold,
            "detection.anomaly_cycles": detection.anomaly_cycles,
            "detection.recovery_cycles": detection.recovery_cycles,
            "detection.comparison_mode": detection.comparison_mode,
            "detection.relative_ratio_percent": detection.relative_ratio_percent,
            "detection.minimum_cluster_active_clients": detection.minimum_cluster_active_clients,
            "detection.minimum_peer_median": detection.minimum_peer_median,
            "detection.missing_cycles": detection.missing_cycles,
            "notifications.notify_new_incidents": notifications.notify_new_incidents,
            "notifications.repeat_unacknowledged": notifications.repeat_unacknowledged,
            "notifications.repeat_interval_minutes": notifications.repeat_interval_minutes,
            "notifications.sound_enabled": notifications.sound_enabled,
            "notifications.recovery_notifications": notifications.recovery_notifications,
            "ui.always_on_top": ui.always_on_top,
            "ui.opacity_percent": ui.opacity_percent,
            "ui.window_maximized": ui.window_maximized,
            "ui.window_x": ui.window_x,
            "ui.window_y": ui.window_y,
            "ui.window_width": ui.window_width,
            "ui.window_height": ui.window_height,
        }
        values["_base_config_fingerprint"] = self._base_settings_fingerprint
        return values

    @staticmethod
    def _visible_position(position: QPoint, size: QSize) -> QPoint:
        rect = QRect(position, size)
        screens = QApplication.screens()
        if any(screen.availableGeometry().intersects(rect) for screen in screens):
            return position
        primary = QApplication.primaryScreen()
        if primary is None:
            return QPoint(50, 50)
        available = primary.availableGeometry()
        return QPoint(available.left() + 30, available.top() + 30)
