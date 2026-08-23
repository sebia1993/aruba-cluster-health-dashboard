from __future__ import annotations

import logging
import time
import weakref
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
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

from .developer_inspector import DeveloperInspectorController, UiElementMetadata
from .detail_dialog import DetailDialog
from .resources import status_icon
from .settings_dialog import SettingsDialog
from .view_models import DashboardView, DeviceView, display, sequence, severity_key, value
from .widgets import (
    NoWheelSlider,
    SubtleSelectionTableWidget,
    fit_window_to_available_screen,
)


LOGGER = logging.getLogger(__name__)
PERFORMANCE_LOGGER = logging.getLogger("aruba_mini_dashboard.performance")


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


class HostKeyApprovalDialog(QDialog):
    """Show every newly discovered host identity in one fail-safe decision."""

    def __init__(self, result: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("최초 SSH 호스트 키 일괄 승인")
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "장비 관리자에게 받은 지문과 모두 일치할 때만 승인하세요. "
            "승인하면 아래 공개 키를 앱 전용 known_hosts에 한 번에 저장한 뒤 "
            "로그인을 자동으로 확인합니다.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        candidates = [
            item
            for item in sequence(result, "details")
            if display(value(item, "status", "")) == "approval_required"
        ]
        if not candidates:
            candidates = [result]
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in candidates:
            identity = (
                display(value(item, "host", ""), ""),
                display(value(item, "port", ""), ""),
                display(value(item, "algorithm", ""), ""),
                display(value(item, "fingerprint", ""), ""),
            )
            group = grouped.setdefault(identity, {"item": item, "roles": []})
            role = display(value(item, "role", ""))
            role_label = "MM" if role == "mm" else "Controller" if role == "cluster" else "장비"
            if role_label not in group["roles"]:
                group["roles"].append(role_label)
        rows = tuple(grouped.values())
        self.table = QTableWidget(len(rows), 4, self)
        self.table.setHorizontalHeaderLabels(("역할", "대상", "키 형식", "SHA-256 지문"))
        self.table.setAccessibleName("최초 연결 SSH 호스트 키 지문 목록")
        self.table.setAccessibleDescription(
            "장비 관리자에게 확인한 값과 비교한 뒤 전체 키를 한 번에 승인합니다."
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        for row_index, group in enumerate(rows):
            item = group["item"]
            host = display(value(item, "host", ""), "")
            port = display(value(item, "port", ""), "")
            values = (
                "/".join(group["roles"]),
                f"{host}:{port}" if port else host,
                display(value(item, "algorithm", ""), ""),
                display(value(item, "fingerprint", ""), ""),
            )
            for column, text_ in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(text_))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        approve = buttons.addButton("모두 승인하고 연결", QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("취소", QDialogButtonBox.RejectRole)
        approve.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        cancel.setDefault(True)
        layout.addWidget(buttons)
        fit_window_to_available_screen(
            self,
            QSize(760, 380),
            minimum_size=QSize(560, 280),
            center_on_parent=True,
        )


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
    LOW_SPEC_FULL_PAGE_SIZE = 250

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
        setup_readiness_check: Callable[[AppSettings], Any] | None = None,
        startup_issue: bool = False,
        developer_inspector: DeveloperInspectorController | None = None,
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
        self.setup_readiness_check = setup_readiness_check
        self.startup_issue = startup_issue
        self.developer_inspector = developer_inspector
        self._developer_catalog_actions: list[QAction] = []
        self._quitting = False
        self._active_settings_dialog: SettingsDialog | None = None
        self._current_view: DashboardView | None = None
        self._current_devices: list[Any] = []
        self._raw_outputs: Any = {}
        self._parse_results: Any = {}
        self._previous_devices: dict[str, Any] = {}
        self._active_incidents: list[Any] = []
        self._devices_by_ip: dict[str, Any] = {}
        self._pending_scope_refresh_ips: set[str] = set()
        self._base_settings_fingerprint = settings_fingerprint(settings)
        self._detail_windows: dict[str, DetailDialog] = {}
        self._dashboard_mode: str | None = None
        self._latest_display_devices: list[DeviceView] = []
        self._table_dirty = {self.COMPACT_MODE: True, self.FULL_MODE: True}
        self._full_row_signatures: dict[str, tuple[Any, ...]] = {}
        self._compact_row_signatures: dict[str, tuple[Any, ...]] = {}
        self._full_page_index = 0
        self._full_sort_column = 0
        self._full_sort_order = Qt.AscendingOrder
        self._updating_full_sort = False
        self._pending_full_selection_ip = ""
        self._hidden_to_tray = False
        self._initial_setup_offer_scheduled = False
        self._initial_setup_offered = False
        self._setup_required = False
        self._pending_preference_save = False
        self._preference_save_timer = QTimer(self)
        self._preference_save_timer.setSingleShot(True)
        self._preference_save_timer.setInterval(750)
        self._preference_save_timer.timeout.connect(self._flush_preference_mirror)

        self.setWindowTitle("Aruba 네트워크 상태 미니보드" + (" — 데모" if demo_mode else ""))
        self.setMinimumSize(360, 260)
        self.resize(settings.ui.window_width, settings.ui.window_height)
        self._build_ui()
        self._create_tray()
        self._register_developer_inspector()
        self._connect_coordinator()
        self._restore_ui_settings()
        self._set_empty_state()
        self._refresh_performance_indicator()
        self._apply_responsive_mode(force=True)
        self._refresh_setup_state()

    def _build_ui(self) -> None:
        self.central_root = QWidget(self)
        self.central_root_layout = QVBoxLayout(self.central_root)
        self.central_root_layout.setContentsMargins(0, 0, 0, 0)
        self.central_root_layout.setSpacing(0)
        self.setCentralWidget(self.central_root)

        self.dashboard_stack = QStackedWidget(self.central_root)
        self.full_page = self._build_full_page()
        self.compact_page = self._build_compact_page()
        self.dashboard_stack.addWidget(self.compact_page)
        self.dashboard_stack.addWidget(self.full_page)
        self.central_root_layout.addWidget(self.dashboard_stack, 1)

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

        self.full_page_bar = QWidget(page)
        self.full_page_bar.setAccessibleName("전체 장비 페이지 이동")
        page_layout = QHBoxLayout(self.full_page_bar)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(5)
        self.full_page_range_label = QLabel("", self.full_page_bar)
        self.full_page_range_label.setAccessibleName("현재 표시 장비 범위")
        page_layout.addWidget(self.full_page_range_label)
        page_layout.addStretch(1)
        self.full_previous_button = QPushButton("이전", self.full_page_bar)
        self.full_previous_button.setAccessibleName("이전 장비 페이지")
        self.full_previous_button.setToolTip("이전 250대 장비를 표시합니다.")
        self.full_page_count_label = QLabel("", self.full_page_bar)
        self.full_page_count_label.setAlignment(Qt.AlignCenter)
        self.full_page_count_label.setAccessibleName("현재 페이지")
        self.full_next_button = QPushButton("다음", self.full_page_bar)
        self.full_next_button.setAccessibleName("다음 장비 페이지")
        self.full_next_button.setToolTip("다음 250대 장비를 표시합니다.")
        page_layout.addWidget(self.full_previous_button)
        page_layout.addWidget(self.full_page_count_label)
        page_layout.addWidget(self.full_next_button)
        self.full_page_bar.hide()
        root.addWidget(self.full_page_bar)

        self.table = SubtleSelectionTableWidget(0, len(self.COLUMNS), page)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self._configure_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        # The complete DeviceView list is sorted before a page slice is taken.
        # Native QTableWidget sorting would only reorder the visible page.
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._full_sort_changed)
        self.full_previous_button.clicked.connect(lambda: self._change_full_page(-1))
        self.full_next_button.clicked.connect(lambda: self._change_full_page(1))
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

        self.compact_table = SubtleSelectionTableWidget(0, len(self.COMPACT_COLUMNS), page)
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
        self.compact_more_menu = QMenu(self)
        self.compact_settings_action = self.compact_more_menu.addAction("설정", self.open_settings)
        self.compact_ack_action = self.compact_more_menu.addAction("알림 확인", self._acknowledge_selected)
        self.compact_more_menu.addSeparator()
        self.compact_screen_menu = self.compact_more_menu.addMenu("화면")
        self.compact_always_on_top_action = QAction("항상 위에 표시", self, checkable=True)
        self.compact_always_on_top_action.toggled.connect(self.set_always_on_top)
        self.compact_screen_menu.addAction(self.compact_always_on_top_action)
        opacity_container = QWidget(self.compact_screen_menu)
        opacity_layout = QHBoxLayout(opacity_container)
        opacity_layout.setContentsMargins(10, 4, 10, 4)
        opacity_layout.addWidget(QLabel("투명도"))
        self.compact_opacity_slider = NoWheelSlider(Qt.Horizontal, opacity_container)
        self.compact_opacity_slider.setRange(40, 100)
        self.compact_opacity_slider.setMinimumWidth(100)
        self.compact_opacity_number = QLabel("100%")
        opacity_layout.addWidget(self.compact_opacity_slider, 1)
        opacity_layout.addWidget(self.compact_opacity_number)
        self.compact_opacity_action = QWidgetAction(self.compact_screen_menu)
        self.compact_opacity_action.setDefaultWidget(opacity_container)
        self.compact_screen_menu.addAction(self.compact_opacity_action)
        self.compact_reset_action = self.compact_screen_menu.addAction(
            "화면 설정 기본값 복원", self.reset_window_options
        )
        self.compact_full_view_action = self.compact_more_menu.addAction("전체 보기", self.showMaximized)
        self.compact_more_menu.addSeparator()
        self.compact_quit_action = self.compact_more_menu.addAction("종료", self.request_quit)
        self.compact_opacity_slider.valueChanged.connect(self.set_opacity_percent)
        self.compact_more_button.setMenu(self.compact_more_menu)

    def _build_options_menu(self) -> None:
        self.options_menu = QMenu(self)
        self.always_on_top_action = QAction("항상 위에 표시", self, checkable=True)
        self.always_on_top_action.toggled.connect(self.set_always_on_top)
        self.options_menu.addAction(self.always_on_top_action)
        opacity_container = QWidget(self.options_menu)
        opacity_layout = QHBoxLayout(opacity_container)
        opacity_layout.setContentsMargins(10, 4, 10, 4)
        opacity_layout.addWidget(QLabel("투명도"))
        self.opacity_slider = NoWheelSlider(Qt.Horizontal, opacity_container)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.setMinimumWidth(110)
        self.opacity_number = QLabel("100%")
        opacity_layout.addWidget(self.opacity_slider, 1)
        opacity_layout.addWidget(self.opacity_number)
        self.opacity_action = QWidgetAction(self.options_menu)
        self.opacity_action.setDefaultWidget(opacity_container)
        self.options_menu.addAction(self.opacity_action)
        self.options_reset_action = QAction("화면 설정 기본값 복원", self)
        self.options_reset_action.triggered.connect(self.reset_window_options)
        self.options_menu.addAction(self.options_reset_action)
        self.opacity_slider.valueChanged.connect(self.set_opacity_percent)
        self.options_button.setMenu(self.options_menu)

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
        if target == self.FULL_MODE and selected_ip:
            self._move_full_page_to_ip(selected_ip)
        self._render_active_table_if_needed()
        if selected_ip:
            selected = self._select_ip(self._active_table(), selected_ip)
            if target == self.COMPACT_MODE and not selected:
                # An unregistered full-view inventory row must not remain an
                # actionable hidden selection after entering compact mode.
                self.table.clearSelection()
                self.compact_table.clearSelection()
                self._selection_changed()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._hidden_to_tray = False
        self._render_active_table_if_needed()
        if (
            self._setup_required
            and not self._initial_setup_offer_scheduled
            and not self._initial_setup_offered
            and not self.demo_mode
            and not self.startup_issue
        ):
            self._initial_setup_offer_scheduled = True
            QTimer.singleShot(0, self._offer_initial_setup)

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
        self.tray_menu = QMenu()
        self.tray_open_action = self.tray_menu.addAction("대시보드 열기")
        self.tray_open_action.triggered.connect(self.show_dashboard)
        self.tray_menu.addSeparator()
        self.tray_check_now_action = self.tray_menu.addAction("지금 점검", self.coordinator.check_now)
        self.tray_start_action = self.tray_menu.addAction("자동 점검 시작", self.coordinator.start_automatic)
        self.tray_pause_action = self.tray_menu.addAction("자동 점검 일시정지", self.coordinator.pause_automatic)
        self.tray_settings_action = self.tray_menu.addAction("설정", self.open_settings)
        self.tray_menu.addSeparator()
        self.tray_quit_action = self.tray_menu.addAction("종료")
        self.tray_quit_action.triggered.connect(self.request_quit)
        self.tray_icon.setContextMenu(self.tray_menu)
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

    def _register_developer_inspector(self) -> None:
        inspector = self.developer_inspector
        if inspector is None:
            return

        source = "src/aruba_mini_dashboard/ui/main_window.py"

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

        def register_action(
            action: QAction,
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> None:
            inspector.register_action(
                action,
                metadata(name, stable_id, screen_path, purpose),
            )

        def register_menu(
            menu: QMenu,
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> None:
            inspector.register_menu(
                menu,
                metadata(name, stable_id, screen_path, purpose),
            )

        def register_virtual(
            name: str,
            stable_id: str,
            screen_path: str,
            purpose: str,
        ) -> None:
            action = QAction(name, self)
            self._developer_catalog_actions.append(action)
            register_action(action, name, stable_id, screen_path, purpose)

        inspector.attach_host_layout(self.central_root, self.central_root_layout)
        register_widget(
            self,
            "메인 창",
            "MAIN-WINDOW",
            "메인 화면",
            "Aruba 장비 상태와 점검 작업을 한 화면에서 제공합니다.",
        )
        register_widget(
            self.statusBar(),
            "메인 상태 표시줄",
            "MAIN-STATUS-BAR",
            "메인 화면 > 상태 표시줄",
            "최근 작업 결과와 운영 안내를 표시합니다.",
        )

        full_path = "메인 화면 > 전체 보기"
        register_widget(
            self.full_page,
            "전체 보기",
            "MAIN-FULL-VIEW",
            full_path,
            "상세 상태와 전체 장비 열을 표시하는 넓은 화면입니다.",
        )
        full_widgets = (
            (self.status_card, "전체 상태 카드", "MAIN-FULL-STATUS-CARD", "전체 상태 영역입니다."),
            (self.status_label, "전체 상태", "MAIN-FULL-STATUS", "현재 종합 상태를 표시합니다."),
            (self.busy_label, "점검 및 저사양 상태", "MAIN-FULL-POLL-STATE", "점검 진행과 저사양 모드 상태를 표시합니다."),
            (self.problem_label, "문제 장비 안내", "MAIN-FULL-PROBLEM", "문제가 감지된 장비 범위를 표시합니다."),
            (self.reason_label, "판단 근거", "MAIN-FULL-REASON", "종합 상태의 고정된 표시 영역입니다."),
            (self.last_check_label, "마지막 점검 시간", "MAIN-FULL-LAST-CHECK", "마지막 점검 완료 시각을 표시합니다."),
            (self.next_check_label, "다음 점검 시간", "MAIN-FULL-NEXT-CHECK", "다음 자동 점검 예정 시각을 표시합니다."),
            (self.check_now_button, "지금 점검 버튼", "MAIN-FULL-CHECK-NOW", "읽기 전용 상태 점검을 즉시 요청합니다."),
            (self.start_button, "자동 점검 시작 버튼", "MAIN-FULL-AUTO-START", "자동 점검을 시작합니다."),
            (self.pause_button, "자동 점검 일시정지 버튼", "MAIN-FULL-AUTO-PAUSE", "자동 점검을 일시정지합니다."),
            (self.ack_button, "알림 확인 버튼", "MAIN-FULL-ACKNOWLEDGE", "선택한 장애 알림을 확인 처리합니다."),
            (self.settings_button, "설정 버튼", "MAIN-FULL-SETTINGS", "설정 창을 엽니다."),
            (self.options_button, "화면 옵션 버튼", "MAIN-FULL-OPTIONS-BUTTON", "전체 보기의 화면 옵션 메뉴를 엽니다."),
        )
        for widget, name, stable_id, purpose in full_widgets:
            register_widget(widget, name, stable_id, full_path, purpose)

        options_path = full_path + " > 화면 옵션"
        register_menu(
            self.options_menu,
            "전체 보기 화면 옵션 메뉴",
            "MAIN-FULL-OPTIONS-MENU",
            options_path,
            "창 고정과 투명도 옵션을 제공합니다.",
        )
        register_action(
            self.always_on_top_action,
            "항상 위에 표시 항목",
            "MAIN-FULL-OPTIONS-ALWAYS-ON-TOP",
            options_path,
            "메인 창을 다른 창 위에 표시할지 전환합니다.",
        )
        opacity_metadata = metadata(
            "투명도 조절 항목",
            "MAIN-FULL-OPTIONS-OPACITY",
            options_path,
            "메인 창의 투명도를 조절합니다.",
        )
        inspector.register_action(self.opacity_action, opacity_metadata)
        inspector.register_widget(self.opacity_slider, opacity_metadata)
        register_widget(
            self.opacity_number,
            "투명도 값",
            "MAIN-FULL-OPTIONS-OPACITY-VALUE",
            options_path,
            "선택한 창 투명도 비율을 표시합니다.",
        )
        register_action(
            self.options_reset_action,
            "화면 설정 기본값 복원 항목",
            "MAIN-FULL-OPTIONS-RESET",
            options_path,
            "창 고정과 투명도를 기본값으로 복원합니다.",
        )

        paging_path = full_path + " > 페이지 이동"
        paging_widgets = (
            (self.full_page_bar, "장비표 페이지 이동 영역", "MAIN-FULL-PAGING", "대용량 장비표의 페이지를 이동합니다."),
            (self.full_page_range_label, "현재 장비 범위", "MAIN-FULL-PAGING-RANGE", "현재 페이지에 표시되는 장비 범위를 표시합니다."),
            (self.full_previous_button, "이전 페이지 버튼", "MAIN-FULL-PAGING-PREVIOUS", "이전 장비 페이지를 표시합니다."),
            (self.full_page_count_label, "현재 페이지 번호", "MAIN-FULL-PAGING-COUNT", "현재 페이지와 전체 페이지 수를 표시합니다."),
            (self.full_next_button, "다음 페이지 버튼", "MAIN-FULL-PAGING-NEXT", "다음 장비 페이지를 표시합니다."),
        )
        for widget, name, stable_id, purpose in paging_widgets:
            register_widget(widget, name, stable_id, paging_path, purpose)

        table_path = full_path + " > 장비 상태 표"
        register_widget(
            self.table,
            "전체 보기 장비표",
            "MAIN-FULL-DEVICE-TABLE",
            table_path,
            "등록 장비의 상세 상태 열을 표시합니다.",
        )
        register_widget(
            self.table.viewport(),
            "전체 보기 장비표 본문",
            "MAIN-FULL-DEVICE-TABLE-BODY",
            table_path,
            "장비별 상태 행이 표시되는 표 본문입니다.",
        )
        register_widget(
            self.table.horizontalHeader(),
            "전체 보기 장비표 머리글",
            "MAIN-FULL-DEVICE-TABLE-HEADER",
            table_path,
            "장비표 열 이름과 정렬 조작을 제공합니다.",
        )
        register_virtual(
            "전체 보기 장비표의 선택된 행",
            "MAIN-FULL-DEVICE-TABLE-SELECTION",
            table_path,
            "현재 작업 대상으로 선택된 장비 행을 표시합니다.",
        )

        compact_path = "메인 화면 > 작은 보기"
        compact_widgets = (
            (self.compact_page, "작은 보기", "MAIN-COMPACT-VIEW", "핵심 상태와 등록 장비만 간결하게 표시합니다."),
            (self.compact_status_card, "작은 보기 상태 카드", "MAIN-COMPACT-STATUS-CARD", "작은 보기의 종합 상태 영역입니다."),
            (self.compact_status_label, "작은 보기 전체 상태", "MAIN-COMPACT-STATUS", "현재 종합 상태를 간단히 표시합니다."),
            (self.compact_busy_label, "작은 보기 점검 및 저사양 상태", "MAIN-COMPACT-POLL-STATE", "점검 진행과 저사양 모드 상태를 표시합니다."),
            (self.compact_last_check_label, "작은 보기 마지막 점검 시간", "MAIN-COMPACT-LAST-CHECK", "마지막 점검 시각을 간단히 표시합니다."),
            (self.compact_check_now_button, "작은 보기 지금 점검 버튼", "MAIN-COMPACT-CHECK-NOW", "읽기 전용 상태 점검을 즉시 요청합니다."),
            (self.compact_auto_button, "작은 보기 자동 점검 버튼", "MAIN-COMPACT-AUTO", "자동 점검을 시작하거나 일시정지합니다."),
            (self.compact_more_button, "작은 보기 더보기 버튼", "MAIN-COMPACT-MORE-BUTTON", "추가 작업 메뉴를 엽니다."),
        )
        for widget, name, stable_id, purpose in compact_widgets:
            register_widget(widget, name, stable_id, compact_path, purpose)

        more_path = compact_path + " > 더보기"
        register_menu(
            self.compact_more_menu,
            "작은 보기 더보기 메뉴",
            "MAIN-COMPACT-MORE-MENU",
            more_path,
            "설정, 알림 확인, 화면 옵션과 종료 작업을 제공합니다.",
        )
        compact_actions = (
            (self.compact_settings_action, "설정 항목", "MAIN-COMPACT-MORE-SETTINGS", "설정 창을 엽니다."),
            (self.compact_ack_action, "알림 확인 항목", "MAIN-COMPACT-MORE-ACKNOWLEDGE", "선택한 장애 알림을 확인 처리합니다."),
            (self.compact_always_on_top_action, "항상 위에 표시 항목", "MAIN-COMPACT-MORE-ALWAYS-ON-TOP", "메인 창을 다른 창 위에 표시할지 전환합니다."),
            (self.compact_reset_action, "화면 설정 기본값 복원 항목", "MAIN-COMPACT-MORE-RESET", "창 고정과 투명도를 기본값으로 복원합니다."),
            (self.compact_full_view_action, "전체 보기 항목", "MAIN-COMPACT-MORE-FULL-VIEW", "메인 창을 전체 보기 크기로 전환합니다."),
            (self.compact_quit_action, "종료 항목", "MAIN-COMPACT-MORE-QUIT", "프로그램 종료 절차를 시작합니다."),
        )
        for action, name, stable_id, purpose in compact_actions:
            register_action(action, name, stable_id, more_path, purpose)
        register_menu(
            self.compact_screen_menu,
            "작은 보기 화면 하위 메뉴",
            "MAIN-COMPACT-MORE-SCREEN-MENU",
            more_path + " > 화면",
            "창 고정과 투명도 조절 항목을 묶어 제공합니다.",
        )
        compact_opacity_metadata = metadata(
            "작은 보기 투명도 조절 항목",
            "MAIN-COMPACT-MORE-OPACITY",
            more_path + " > 화면",
            "메인 창의 투명도를 조절합니다.",
        )
        inspector.register_action(self.compact_opacity_action, compact_opacity_metadata)
        inspector.register_widget(self.compact_opacity_slider, compact_opacity_metadata)
        register_widget(
            self.compact_opacity_number,
            "작은 보기 투명도 값",
            "MAIN-COMPACT-MORE-OPACITY-VALUE",
            more_path + " > 화면",
            "선택한 창 투명도 비율을 표시합니다.",
        )

        compact_table_path = compact_path + " > 등록 컨트롤러 상태 표"
        register_widget(
            self.compact_table,
            "등록 컨트롤러 상태 표",
            "MAIN-COMPACT-DEVICE-TABLE",
            compact_table_path,
            "등록한 컨트롤러의 핵심 상태와 분배 상태를 표시합니다.",
        )
        register_widget(
            self.compact_table.viewport(),
            "등록 컨트롤러 상태 표 본문",
            "MAIN-COMPACT-DEVICE-TABLE-BODY",
            compact_table_path,
            "컨트롤러별 상태 행이 표시되는 표 본문입니다.",
        )
        register_widget(
            self.compact_table.horizontalHeader(),
            "등록 컨트롤러 상태 표 머리글",
            "MAIN-COMPACT-DEVICE-TABLE-HEADER",
            compact_table_path,
            "작은 보기 장비표의 열 이름을 표시합니다.",
        )
        register_virtual(
            "등록 컨트롤러 상태 표의 선택된 행",
            "MAIN-COMPACT-DEVICE-TABLE-SELECTION",
            compact_table_path,
            "현재 작업 대상으로 선택된 컨트롤러 행을 표시합니다.",
        )

        tray_path = "Windows 알림 영역 > Aruba 미니 대시보드"
        register_virtual(
            "알림 영역 아이콘",
            "TRAY-ICON",
            tray_path,
            "대시보드 상태를 표시하고 알림 영역 메뉴를 엽니다.",
        )
        register_menu(
            self.tray_menu,
            "알림 영역 메뉴",
            "TRAY-MENU",
            tray_path,
            "창 열기, 점검, 설정과 종료 작업을 제공합니다.",
        )
        tray_actions = (
            (self.tray_open_action, "대시보드 열기 항목", "TRAY-OPEN", "숨겨진 대시보드 창을 표시합니다."),
            (self.tray_check_now_action, "지금 점검 항목", "TRAY-CHECK-NOW", "읽기 전용 상태 점검을 즉시 요청합니다."),
            (self.tray_start_action, "자동 점검 시작 항목", "TRAY-AUTO-START", "자동 점검을 시작합니다."),
            (self.tray_pause_action, "자동 점검 일시정지 항목", "TRAY-AUTO-PAUSE", "자동 점검을 일시정지합니다."),
            (self.tray_settings_action, "설정 항목", "TRAY-SETTINGS", "설정 창을 엽니다."),
            (self.tray_quit_action, "종료 항목", "TRAY-QUIT", "프로그램 종료 절차를 시작합니다."),
        )
        for action, name, stable_id, purpose in tray_actions:
            register_action(action, name, stable_id, tray_path, purpose)

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
                    f"{'MM' if role == 'mm' else '클러스터' if role == 'cluster' else '전체 장비'} "
                    "SSH 연결을 확인하고 있습니다…"
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
        restored_position = (
            QPoint(ui.window_x, ui.window_y)
            if ui.window_x is not None and ui.window_y is not None
            else None
        )
        fit_window_to_available_screen(
            self,
            QSize(ui.window_width, ui.window_height),
            preferred_position=restored_position,
            minimum_size=QSize(360, 260),
            margin=8,
            center_on_parent=restored_position is None,
        )
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

    def _readiness(self) -> tuple[bool, str]:
        if self.setup_readiness_check is None:
            return True, ""
        try:
            result = self.setup_readiness_check(self.settings)
        except Exception:
            LOGGER.exception("Initial setup readiness check failed")
            return False, "장비와 자격 증명 설정을 확인해 주세요."
        if isinstance(result, tuple):
            ready = bool(result[0]) if result else False
            reason = str(result[1]) if len(result) > 1 and result[1] else ""
            return ready, reason
        return bool(result), ""

    def _refresh_setup_state(self) -> bool:
        ready, reason = self._readiness()
        self._setup_required = not ready
        self.settings_button.setText("설정 시작" if self._setup_required else "설정")
        self.compact_settings_action.setText("설정 시작" if self._setup_required else "설정")
        self.compact_more_button.setText("설정 시작" if self._setup_required else "더보기")
        if self._setup_required and self._current_view is None:
            self.status_label.setText("설정 필요")
            self.compact_status_label.setText("설정 필요")
            self.problem_label.setText("문제 IP: 점검 전")
            self.reason_label.setText(
                "먼저 장비와 자격 증명을 등록해 주세요. "
                + (reason or "저장 후 ‘지금 점검’을 눌러 상태를 확인할 수 있습니다.")
            )
            self.statusBar().showMessage("처음 사용하려면 ‘설정 시작’을 눌러 장비 정보를 등록하세요.")
        if self.tray_icon.isVisible():
            tooltip = (
                "Aruba 미니보드 — 설정 필요"
                if self._setup_required
                else f"Aruba 미니보드 — {self.status_label.text()}"
            )
            self.tray_icon.setToolTip(tooltip)
        self._update_monitoring_action_availability()
        return ready

    def _update_monitoring_action_availability(self) -> None:
        ready = not self._setup_required
        busy = bool(self.coordinator.busy)
        automatic = bool(self.coordinator.automatic)
        check_enabled = ready and not busy
        start_enabled = ready and not busy and not automatic
        pause_enabled = ready and automatic
        self.check_now_button.setEnabled(check_enabled)
        self.start_button.setEnabled(start_enabled)
        self.pause_button.setEnabled(pause_enabled)
        self.compact_check_now_button.setEnabled(check_enabled)
        self.compact_auto_button.setEnabled(pause_enabled or start_enabled)
        for name, enabled in (
            ("tray_check_now_action", check_enabled),
            ("tray_start_action", start_enabled),
            ("tray_pause_action", pause_enabled),
        ):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(enabled)

    @Slot()
    def _offer_initial_setup(self) -> None:
        self._initial_setup_offer_scheduled = False
        if self._initial_setup_offered or not self._setup_required:
            return
        self._initial_setup_offered = True
        self.open_settings(initial_setup=True)

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
        self._refresh_open_detail()
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
        self._latest_display_devices = display_devices
        for device in display_devices:
            self._devices_by_ip.setdefault(device.ip, device.source)
        self._table_dirty[self.FULL_MODE] = True
        self._table_dirty[self.COMPACT_MODE] = True
        if selected_ip:
            self._move_full_page_to_ip(selected_ip)
        else:
            self._clamp_full_page()
        if not self._hidden_to_tray and self.isVisible():
            self._render_active_table_if_needed()
        if selected_ip:
            self._select_ip(self._active_table(), selected_ip)

    def _render_active_table_if_needed(self) -> None:
        mode = self._dashboard_mode
        if mode is None or self._hidden_to_tray or not self._table_dirty.get(mode, False):
            return
        started = time.perf_counter() if PERFORMANCE_LOGGER.isEnabledFor(logging.INFO) else None
        if mode == self.FULL_MODE:
            self._populate_full_table(self._latest_display_devices)
            row_count = len(self._latest_display_devices)
        else:
            compact_devices = self._compact_devices(self._latest_display_devices)
            self._populate_compact_table(compact_devices)
            row_count = len(compact_devices)
        self._table_dirty[mode] = False
        if started is not None:
            PERFORMANCE_LOGGER.info(
                "ui_render duration_ms=%d mode=%s rows=%d",
                round((time.perf_counter() - started) * 1000),
                mode,
                row_count,
            )

    def _populate_full_table(self, devices: list[DeviceView]) -> None:
        rows: list[tuple[DeviceView, bool, str, str, tuple[str, ...]]] = []
        for device in devices:
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
            rows.append((device, registered, display_status, display_status_key, values))

        rows = self._sort_full_rows(rows)
        total = len(rows)
        if self._pending_full_selection_ip and self._full_paging_enabled(total):
            selected_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row[0].ip == self._pending_full_selection_ip
                ),
                None,
            )
            if selected_index is not None:
                self._full_page_index = selected_index // self.LOW_SPEC_FULL_PAGE_SIZE
        self._pending_full_selection_ip = ""
        page_count = self._full_page_count(total)
        self._full_page_index = min(self._full_page_index, max(0, page_count - 1))
        paged = self._full_paging_enabled(total)
        if paged:
            start = self._full_page_index * self.LOW_SPEC_FULL_PAGE_SIZE
            visible_rows = rows[start : start + self.LOW_SPEC_FULL_PAGE_SIZE]
        else:
            start = 0
            visible_rows = rows
        self._update_full_page_bar(total, start, len(visible_rows), page_count, paged)

        signatures = {
            device.ip: (*values, registered, display_status_key)
            for device, registered, _display_status, display_status_key, values in visible_rows
        }
        identities = [device.ip for device, *_rest in visible_rows]
        shape_changed = self._ensure_table_shape(self.table, identities, len(self.COLUMNS))
        if not shape_changed and signatures == self._full_row_signatures:
            return
        self.table.setUpdatesEnabled(False)
        self._updating_full_sort = True
        try:
            for row, (device, registered, _display_status, display_status_key, values) in enumerate(
                visible_rows
            ):
                if (
                    not shape_changed
                    and self._full_row_signatures.get(device.ip) == signatures[device.ip]
                ):
                    continue
                foreground, _background, _accent = STATUS_STYLES.get(
                    display_status_key, STATUS_STYLES["unknown"]
                )
                for column, text in enumerate(values):
                    item = self.table.item(row, column) or QTableWidgetItem()
                    if item.text() != text:
                        item.setText(text)
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
                    if self.table.item(row, column) is None:
                        self.table.setItem(row, column, item)
            self.table.horizontalHeader().setSortIndicator(
                self._full_sort_column,
                self._full_sort_order,
            )
        finally:
            self._updating_full_sort = False
            self.table.setUpdatesEnabled(True)
        self._full_row_signatures = signatures

    def _sort_full_rows(
        self,
        rows: list[tuple[DeviceView, bool, str, str, tuple[str, ...]]],
    ) -> list[tuple[DeviceView, bool, str, str, tuple[str, ...]]]:
        """Sort the complete device set before applying a low-spec page slice."""

        column = min(max(self._full_sort_column, 0), len(self.COLUMNS) - 1)
        ordered = sorted(rows, key=lambda row: row[0].ip.casefold())
        ordered.sort(
            key=lambda row: str(row[4][column]).casefold(),
            reverse=self._full_sort_order == Qt.DescendingOrder,
        )
        return ordered

    def _full_paging_enabled(self, total: int | None = None) -> bool:
        if total is None:
            total = len(self._latest_display_devices)
        performance = getattr(self.settings, "performance", None)
        return bool(
            getattr(performance, "low_spec_mode", False)
            and total > self.LOW_SPEC_FULL_PAGE_SIZE
        )

    def _full_page_count(self, total: int | None = None) -> int:
        if total is None:
            total = len(self._latest_display_devices)
        if not self._full_paging_enabled(total):
            return 1
        return max(1, (total + self.LOW_SPEC_FULL_PAGE_SIZE - 1) // self.LOW_SPEC_FULL_PAGE_SIZE)

    def _clamp_full_page(self) -> None:
        self._full_page_index = min(
            max(0, self._full_page_index),
            self._full_page_count() - 1,
        )

    def _update_full_page_bar(
        self,
        total: int,
        start: int,
        visible_count: int,
        page_count: int,
        paged: bool,
    ) -> None:
        self.full_page_bar.setVisible(paged)
        if not paged:
            self.full_page_range_label.clear()
            self.full_page_count_label.clear()
            return
        end = start + visible_count
        self.full_page_range_label.setText(f"{start + 1}–{end} / 전체 {total}대")
        self.full_page_count_label.setText(f"{self._full_page_index + 1} / {page_count}")
        self.full_previous_button.setEnabled(self._full_page_index > 0)
        self.full_next_button.setEnabled(self._full_page_index + 1 < page_count)
        self.full_page_bar.setAccessibleDescription(
            f"전체 {total}대 중 {start + 1}대부터 {end}대까지 표시"
        )

    @Slot(int)
    def _change_full_page(self, offset: int) -> None:
        page_count = self._full_page_count()
        target = min(max(0, self._full_page_index + offset), page_count - 1)
        if target == self._full_page_index:
            return
        self.table.clearSelection()
        self.compact_table.clearSelection()
        self._full_page_index = target
        self._table_dirty[self.FULL_MODE] = True
        self._render_active_table_if_needed()
        self._selection_changed()

    @Slot(int, Qt.SortOrder)
    def _full_sort_changed(self, column: int, order: Qt.SortOrder) -> None:
        if self._updating_full_sort:
            return
        selected_ip = self._selected_ip()
        self._full_sort_column = column
        self._full_sort_order = order
        if selected_ip:
            self._move_full_page_to_ip(selected_ip)
        else:
            self._clamp_full_page()
        self._table_dirty[self.FULL_MODE] = True
        self._render_active_table_if_needed()
        if selected_ip:
            self._select_ip(self.table, selected_ip)

    def _move_full_page_to_ip(self, ip: str) -> None:
        if not self._full_paging_enabled():
            self._full_page_index = 0
            self._pending_full_selection_ip = ""
            return
        # Resolve the selected IP while the already-required full render sorts
        # the complete dataset. Hidden/tray snapshot updates therefore avoid a
        # second full-device transformation merely to calculate a page.
        self._pending_full_selection_ip = ip
        self._table_dirty[self.FULL_MODE] = True

    def _populate_compact_table(self, devices: list[DeviceView]) -> None:
        rows: list[tuple[DeviceView, str, tuple[str, ...], str, str]] = []
        signatures: dict[str, tuple[Any, ...]] = {}
        for device in devices:
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
            signatures[device.ip] = (*values, controller_key, distribution_key)
            rows.append((device, name, values, controller_key, distribution_key))
        identities = [device.ip for device in devices]
        shape_changed = self._ensure_table_shape(
            self.compact_table,
            identities,
            len(self.COMPACT_COLUMNS),
        )
        if not shape_changed and signatures == self._compact_row_signatures:
            return
        self.compact_table.setUpdatesEnabled(False)
        for row, (device, name, values, controller_key, distribution_key) in enumerate(rows):
            if not shape_changed and self._compact_row_signatures.get(device.ip) == signatures[device.ip]:
                continue
            for column, text in enumerate(values):
                item = self.compact_table.item(row, column) or QTableWidgetItem()
                if item.text() != text:
                    item.setText(text)
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
                if self.compact_table.item(row, column) is None:
                    self.compact_table.setItem(row, column, item)
        self.compact_table.setUpdatesEnabled(True)
        self._compact_row_signatures = signatures

    @staticmethod
    def _ensure_table_shape(table: QTableWidget, identities: list[str], columns: int) -> bool:
        existing = [
            display(table.item(row, 0).data(Qt.UserRole), "")
            if table.item(row, 0) is not None
            else ""
            for row in range(table.rowCount())
        ]
        if existing != identities or table.columnCount() != columns:
            table.clearContents()
            table.setRowCount(len(identities))
            return True
        return False

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
        self.tray_icon.setToolTip(
            "Aruba 미니보드 — 설정 필요"
            if self._setup_required
            else f"Aruba 미니보드 — {self.status_label.text()}"
        )

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
        self._refresh_performance_indicator(busy=busy)
        self.settings_button.setEnabled(not busy)
        self.compact_settings_action.setEnabled(not busy)
        if hasattr(self, "tray_settings_action"):
            self.tray_settings_action.setEnabled(not busy)
        self._update_monitoring_action_availability()
        self._selection_changed()
        if self._quitting and not busy:
            self._complete_quit()

    @Slot(bool)
    def _automatic_changed(self, automatic: bool) -> None:
        self.compact_auto_button.setText("일시정지" if automatic else "자동 시작")
        self._update_monitoring_action_availability()
        if not automatic:
            self.next_check_label.setText("다음 점검: 일시정지")
        if self._quitting or bool(getattr(self.coordinator, "shutting_down", False)):
            # Explicit quit and QApplication/aboutToQuit paths both pause the
            # coordinator internally.  Do not turn that transient shutdown
            # pause into a different next-launch preference.
            return
        self.settings.polling.automatic_enabled = automatic
        self._persist_preference("polling.automatic_enabled", automatic)

    @Slot(object)
    def _next_check_changed(self, next_check: datetime | None) -> None:
        self.next_check_label.setText(
            "다음 점검: " + (next_check.strftime("%Y-%m-%d %H:%M:%S") if next_check else "일시정지")
        )

    @Slot()
    def open_settings(self, *, initial_setup: bool = False) -> None:
        inspector_options = (
            {"developer_inspector": self.developer_inspector}
            if self.developer_inspector is not None
            else {}
        )
        if initial_setup:
            dialog = SettingsDialog(
                self.settings,
                self.credential_service,
                self,
                initial_setup=True,
                **inspector_options,
            )
        else:
            dialog = SettingsDialog(
                self.settings,
                self.credential_service,
                self,
                **inspector_options,
            )
        dialog.connection_test_requested.connect(self._connection_test_requested)
        if self.notification_service is not None:
            dialog.sound_test_requested.connect(self.notification_service.test_sound)
            dialog.notification_test_requested.connect(self.notification_service.test_notification)
        self._active_settings_dialog = dialog
        # A scheduled poll can begin while this modal dialog is open.  Keep the
        # same dialog (and the operator's typed values) available when applying
        # is temporarily rejected, instead of closing it and discarding the
        # form.  Staged credentials are committed only after every settings
        # layer accepted the candidate, or rolled back when the operator exits.
        try:
            while True:
                if dialog.exec() != QDialog.Accepted:
                    dialog.rollback_staged_credentials()
                    return
                if self.apply_settings(dialog.settings):
                    dialog.commit_staged_credentials()
                    self._refresh_setup_state()
                    return
        finally:
            if self._active_settings_dialog is dialog:
                self._active_settings_dialog = None
            # QDialog.close()/exec() only hides a parent-owned dialog. Delete it
            # after credential commit/rollback so repeated Settings use cannot
            # retain whole forms and inspector registrations for the process.
            delete_later = getattr(dialog, "deleteLater", None)
            if callable(delete_later):
                delete_later()

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
        settings_update = None
        runtime_update = None
        if self.settings_store is not None:
            try:
                settings.validate()
                begin_update = getattr(self.settings_store, "begin_update", None)
                if callable(begin_update):
                    settings_update = begin_update(settings)
                else:
                    self.settings_store.save(settings)
            except Exception as exc:
                QMessageBox.warning(self, "설정 저장 실패", str(exc))
                return False
        if self.settings_apply_handler is not None:
            try:
                staged_runtime = self.settings_apply_handler(settings)
                if (
                    staged_runtime is not None
                    and callable(getattr(staged_runtime, "commit", None))
                    and callable(getattr(staged_runtime, "rollback", None))
                ):
                    runtime_update = staged_runtime
            except Exception as exc:
                if settings_update is not None:
                    try:
                        settings_update.rollback()
                    except Exception:
                        LOGGER.critical("Durable settings rollback deferred to next startup", exc_info=True)
                elif self.settings_store is not None:
                    try:
                        self.settings_store.save(previous_settings)
                    except Exception:
                        LOGGER.critical("Settings rollback failed", exc_info=True)
                QMessageBox.warning(self, "설정 적용 실패", str(exc))
                return False
        if settings_update is not None:
            try:
                settings_update.commit()
            except Exception as exc:
                runtime_rollback_failed = False
                if runtime_update is not None:
                    try:
                        runtime_update.rollback()
                    except Exception:
                        runtime_rollback_failed = True
                        LOGGER.critical("Staged runtime settings rollback failed", exc_info=True)
                elif self.settings_apply_handler is not None:
                    try:
                        self.settings_apply_handler(previous_settings)
                    except Exception:
                        runtime_rollback_failed = True
                        LOGGER.critical("Runtime settings rollback failed", exc_info=True)
                try:
                    settings_update.rollback()
                except Exception:
                    LOGGER.critical("Durable settings rollback deferred to next startup", exc_info=True)
                message = str(exc)
                if runtime_rollback_failed:
                    message += " 프로그램을 다시 시작해 이전 설정을 복구하세요."
                    self.coordinator.pause_automatic()
                QMessageBox.warning(self, "설정 확정 실패", message)
                return False
        runtime_commit_failed = False
        if runtime_update is not None:
            try:
                runtime_update.commit()
            except Exception:
                # The authoritative JSON is already committed. Keep the
                # candidate active and stop automatic polling rather than
                # presenting the old UI against the new startup settings.
                LOGGER.critical("Committed runtime settings cleanup was deferred", exc_info=True)
                runtime_commit_failed = True
                self.coordinator.pause_automatic()
                QMessageBox.warning(
                    self,
                    "설정 후속 저장 지연",
                    "설정은 저장했지만 이전 감시 상태 정리를 마치지 못했습니다. "
                    "프로그램을 다시 시작한 뒤 점검 상태를 확인하세요.",
                )
        previous_auto = self.coordinator.automatic
        self.settings = settings
        current_member_ips = tuple(
            member.ip.strip() for member in settings.cluster.members if member.ip.strip()
        )
        self._base_settings_fingerprint = settings_fingerprint(settings)
        effective_interval = getattr(
            settings,
            "effective_poll_interval_seconds",
            settings.polling.interval_seconds,
        )
        self.coordinator.set_interval(effective_interval)
        self._refresh_performance_indicator()
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
        if (
            not runtime_commit_failed
            and settings.polling.automatic_enabled
            and not previous_auto
        ):
            self.coordinator.start_automatic()
        elif not settings.polling.automatic_enabled and previous_auto:
            self.coordinator.pause_automatic()
        self.settings_saved.emit(settings)
        if self.demo_mode:
            message = "Demo 모드: 설정을 영구 저장하지 않고 이번 실행에만 적용했습니다."
        else:
            message = "설정을 저장했습니다."
        if getattr(getattr(settings, "performance", None), "low_spec_mode", False):
            message += (
                f" · 저사양 모드 자동 점검 {effective_interval}초 적용"
                " · ‘지금 점검’은 즉시 실행"
            )
        self.statusBar().showMessage(message, 8000)
        return True

    def _refresh_performance_indicator(self, *, busy: bool | None = None) -> None:
        if busy is None:
            busy = bool(getattr(self.coordinator, "busy", False))
        low_spec = bool(
            getattr(getattr(self.settings, "performance", None), "low_spec_mode", False)
        )
        effective_interval = getattr(
            self.settings,
            "effective_poll_interval_seconds",
            self.settings.polling.interval_seconds,
        )
        if busy:
            text = "● 점검 중 · 저사양" if low_spec else "● 점검 중"
        else:
            text = f"저사양 · 자동 {effective_interval}초" if low_spec else ""
        description = (
            f"저사양 모드 사용 중, 자동 점검 {effective_interval}초"
            if low_spec
            else "현재 점검 상태"
        )
        for label in (self.busy_label, self.compact_busy_label):
            label.setText(text)
            label.setAccessibleName("점검 및 저사양 모드 상태")
            label.setAccessibleDescription(description)
            label.setToolTip(description if low_spec else "")

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
            self._complete_active_settings_connection(False)
            return
        tester(role, settings)

    @Slot(str, object)
    def _connection_test_finished(self, role: str, result: Any) -> None:
        status = display(value(result, "status", "failed"))
        host = display(value(result, "host", ""), "")
        port = display(value(result, "port", ""), "")
        fingerprint = display(value(result, "fingerprint", ""), "")
        message = display(value(result, "message", "연결 테스트가 완료되었습니다."))
        purpose = display(value(result, "purpose", "diagnostic"))
        report = self._connection_result_report(result)
        if status == "approval_required":
            approval = HostKeyApprovalDialog(result, self)
            if approval.exec() != QDialog.Accepted:
                discard = getattr(self.coordinator, "discard_connection_test", None)
                if callable(discard):
                    discard(role)
                self._complete_active_settings_connection(False)
                self.statusBar().showMessage("SSH 호스트 키 승인을 취소했습니다.", 5000)
                return
            try:
                self.coordinator.approve_host_key(value(result, "scanned"))
                self.coordinator.retry_connection_test(role)
            except Exception as exc:
                discard = getattr(self.coordinator, "discard_connection_test", None)
                if callable(discard):
                    discard(role)
                self._complete_active_settings_connection(False)
                QMessageBox.critical(self, "SSH 호스트 키 저장 실패", str(exc))
            return
        if status == "mismatch":
            expected = ", ".join(value(result, "expected_fingerprints", ())) or "저장된 값 확인 필요"
            QMessageBox.critical(
                self,
                "SSH 호스트 키 불일치",
                f"{message}\n\n대상: {host}:{port}\n현재 지문: {fingerprint}\n"
                f"저장된 지문: {expected}{report}",
            )
            self._complete_active_settings_connection(False)
            self.statusBar().showMessage("SSH 키 불일치로 연결을 차단했습니다.", 10000)
            return
        if status != "success":
            QMessageBox.warning(
                self,
                "연결 확인 실패",
                message + report,
            )
            self._complete_active_settings_connection(False)
            self.statusBar().showMessage("SSH 연결 확인에 실패해 설정을 저장하지 않았습니다.", 8000)
            return

        pending_fallbacks = int(value(result, "pending_fallbacks", 0) or 0)
        if purpose != "save" or pending_fallbacks:
            presenter = QMessageBox.warning if pending_fallbacks else QMessageBox.information
            presenter(
                self,
                "연결 확인 완료" if not pending_fallbacks else "연결 확인 완료 — 주의",
                message + report,
            )
        completed = self._complete_active_settings_connection(True)
        if purpose == "save" and not completed:
            self.statusBar().showMessage("자격 증명 저장 단계에서 설정 저장을 완료하지 못했습니다.", 8000)
            return
        self.statusBar().showMessage(
            "SSH 연결을 확인하고 설정을 저장했습니다."
            if purpose == "save"
            else "SSH 연결 진단을 완료했습니다.",
            8000,
        )

    @Slot(str, object)
    def _connection_test_failed(self, role: str, error: BaseException) -> None:
        QMessageBox.warning(
            self,
            "연결 확인 실패",
            str(error) or "SSH 연결을 확인하지 못했습니다. 설정과 로그를 확인하세요.",
        )
        self._complete_active_settings_connection(False)
        self.statusBar().showMessage("SSH 연결 확인에 실패했습니다.", 5000)

    def _complete_active_settings_connection(self, success: bool) -> bool:
        dialog = self._active_settings_dialog
        if dialog is None or not bool(getattr(dialog, "connection_request_pending", False)):
            return True
        complete = getattr(dialog, "complete_connection_request", None)
        if callable(complete):
            result = complete(success)
            return True if result is None else bool(result)
        return False

    @staticmethod
    def _connection_result_report(result: Any) -> str:
        details = sequence(result, "details")
        if not details:
            return ""
        status_labels = {
            "authenticated": "로그인 성공",
            "verified": "지문 확인",
            "approval_required": "승인 필요",
            "mismatch": "지문 불일치",
            "scan_failed": "지문 확인 실패",
            "auth_failed": "로그인 실패",
        }
        lines = ["", "장비별 결과:"]
        for item in details:
            role = display(value(item, "role", ""))
            role_label = "MM" if role == "mm" else "WLC"
            item_host = display(value(item, "host", ""), "")
            item_port = display(value(item, "port", ""), "")
            item_status = display(value(item, "status", ""))
            item_message = display(value(item, "message", ""), "")
            label = status_labels.get(item_status, item_status or "확인 실패")
            lines.append(
                f"- {role_label} {item_host}:{item_port} — {label}"
                + (f" ({item_message})" if item_message and item_status in {"scan_failed", "auth_failed"} else "")
            )
        return "\n" + "\n".join(lines)

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
        existing = self._detail_windows.get(ip)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        # Keep one operational detail window. Besides reducing window clutter,
        # this prevents several dialogs from retaining separate multi-megabyte
        # command snapshots across polling cycles.
        for previous_ip, previous_dialog in list(self._detail_windows.items()):
            self._detail_windows.pop(previous_ip, None)
            previous_dialog.close()
        detail_options: dict[str, Any] = {
            "previous_device": self._previous_devices.get(ip),
            "raw_outputs": self._raw_outputs,
            "parsed_results": self._parse_results,
        }
        if self.developer_inspector is not None:
            detail_options["developer_inspector"] = self.developer_inspector
        dialog = DetailDialog(source, self, **detail_options)
        self._detail_windows[ip] = dialog
        dialog_ref = weakref.ref(dialog)

        def forget_destroyed_dialog(*_args: object) -> None:
            if self._detail_windows.get(ip) is dialog_ref():
                self._detail_windows.pop(ip, None)

        dialog.destroyed.connect(forget_destroyed_dialog)
        dialog.show()

    def _refresh_open_detail(self) -> None:
        """Keep the one operational detail window on the current poll cycle."""

        for ip, dialog in list(self._detail_windows.items()):
            source = self._devices_by_ip.get(ip)
            if source is None:
                self._detail_windows.pop(ip, None)
                dialog.close()
                continue
            dialog.update_snapshot(
                source,
                raw_outputs=self._raw_outputs,
                parsed_results=self._parse_results,
                previous_device=self._previous_devices.get(ip),
            )

    @Slot()
    def _selection_changed(self) -> None:
        if self._current_view is None or self.coordinator.busy:
            self.ack_button.setEnabled(False)
            self.compact_ack_action.setEnabled(False)
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
        if self.coordinator.busy:
            self.statusBar().showMessage("점검이 끝난 뒤 알림을 확인 처리하세요.", 5000)
            return
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
        self._hidden_to_tray = False
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
            self._hidden_to_tray = True
            self.hide()
            self.tray_icon.showMessage(
                "Aruba 미니 대시보드",
                (
                    "설정이 필요합니다. 트레이에서 대시보드를 다시 열 수 있습니다."
                    if self._setup_required
                    else "모니터링은 시스템 트레이에서 계속 실행됩니다."
                ),
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
        current_fingerprint = settings_fingerprint(self.settings)
        changed = current_fingerprint != self._base_settings_fingerprint
        if self.settings_store is not None and changed:
            try:
                self.settings_store.save(self.settings)
            except Exception:
                LOGGER.exception("Could not persist window state")
                return
        self._base_settings_fingerprint = current_fingerprint
        if changed:
            self._mirror_all_settings()
        else:
            # A debounced quick option may still be pending even though the
            # authoritative in-memory fingerprint already matches.
            self._preference_save_timer.stop()
            self._flush_preference_mirror()

    def _persist_preference(self, key: str, value_: Any) -> None:
        if self.storage is None:
            return
        self._pending_preference_save = True
        self._preference_save_timer.start()

    @Slot()
    def _flush_preference_mirror(self) -> None:
        if self.storage is None or not self._pending_preference_save:
            return
        try:
            timed_batch_setter = getattr(self.storage, "try_set_preferences", None)
            if callable(timed_batch_setter):
                timed_batch_setter(self._settings_preference_values(), lock_timeout_ms=50)
                self._pending_preference_save = False
                return
            batch_setter = getattr(self.storage, "set_preferences", None)
            if callable(batch_setter):
                # Rewrite the complete mirror so advancing its fingerprint can
                # never make unrelated stale preference rows authoritative.
                batch_setter(self._settings_preference_values())
                self._pending_preference_save = False
                return
            setter = getattr(self.storage, "set_setting", None)
            if callable(setter):
                for setting_key, setting_value in self._settings_preference_values().items():
                    setter(setting_key, setting_value)
                self._pending_preference_save = False
        except Exception:
            LOGGER.exception("Could not persist UI preference mirror")

    def _mirror_all_settings(self) -> None:
        self._preference_save_timer.stop()
        self._pending_preference_save = True
        self._flush_preference_mirror()

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
        performance = getattr(self.settings, "performance", None)
        if performance is not None:
            values["performance.low_spec_mode"] = bool(
                getattr(performance, "low_spec_mode", False)
            )
            values["performance.performance_logging"] = bool(
                getattr(performance, "performance_logging", False)
            )
        values["_base_config_fingerprint"] = self._base_settings_fingerprint
        return values
