from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
)

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.models import (
    ControllerState,
    DeviceHealth,
    DistributionState,
    OverallHealth,
    Severity,
)
from aruba_mini_dashboard.ui.main_window import MainWindow
from aruba_mini_dashboard.ui.device_table_view import DeviceTableView
from aruba_mini_dashboard.ui.resources import status_icon
from aruba_mini_dashboard.ui.widgets import (
    SubtleSelectionTableWidget,
    _contrast_ratio,
)


class FakeCoordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    busy = False
    automatic = False

    def check_now(self) -> None:
        return None

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, _seconds: int) -> None:
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _settings() -> AppSettings:
    settings = AppSettings.default()
    settings.cluster.members = [
        ClusterMemberSettings("192.0.2.11", "WLC-01"),
        ClusterMemberSettings("192.0.2.12", "WLC-02"),
    ]
    return settings


def _snapshot(*, active_clients: int = 10) -> OverallHealth:
    checked_at = datetime(2026, 8, 12, 1, 30, tzinfo=timezone.utc)
    return OverallHealth(
        checked_at=checked_at,
        severity=Severity.CRITICAL,
        devices=[
            DeviceHealth(
                ip="192.0.2.11",
                alias="WLC-01",
                controller_state=ControllerState.UP,
                distribution_state=DistributionState.NORMAL,
                mm_status="Up",
                active_clients=active_clients,
                standby_clients=8,
                severity=Severity.NORMAL,
                last_seen=checked_at,
            ),
            DeviceHealth(
                ip="192.0.2.12",
                alias="WLC-02",
                controller_state=ControllerState.DOWN,
                distribution_state=DistributionState.ANOMALOUS,
                mm_status="Down",
                severity=Severity.CRITICAL,
                issue_reasons=["MM Status Down"],
                last_seen=checked_at,
            ),
        ],
        monitoring_scope_ips=("192.0.2.11", "192.0.2.12"),
        problem_ips=["192.0.2.12"],
        primary_problem_ip="192.0.2.12",
    )


def _row_for_ip(table: QTableWidget, ip: str) -> int:
    for row in range(table.rowCount()):
        if table.item(row, 0).data(Qt.UserRole) == ip:
            return row
    raise AssertionError(f"row not found: {ip}")


def test_dashboard_uses_subtle_selection_for_full_and_compact_tables() -> None:
    _app()
    window = MainWindow(FakeCoordinator(), _settings())

    assert isinstance(window.table, DeviceTableView)
    assert isinstance(window.compact_table, SubtleSelectionTableWidget)
    for table in (window.table, window.compact_table):
        assert table.selectionBehavior() == QTableWidget.SelectRows
        assert table.selectionMode() == QTableWidget.SingleSelection
        assert table.selection_style_revision > 0
        assert table.selection_style_colors["active_background"]

    window._quitting = True
    window.close()


def test_selection_delegate_uses_neutral_fill_and_keeps_status_presentation() -> None:
    _app()
    statuses = (
        ("normal", QColor("#176B42")),
        ("attention", QColor("#805500")),
        ("failure", QColor("#8A1C1C")),
        ("unknown", QColor("#374151")),
    )
    table = SubtleSelectionTableWidget(len(statuses), 1)
    icon_keys: dict[int, int] = {}
    for row, (status_key, status_color) in enumerate(statuses):
        item = QTableWidgetItem(status_key)
        item.setForeground(status_color)
        item.setIcon(status_icon(status_key))
        icon_keys[row] = item.icon().cacheKey()
        table.setItem(row, 0, item)

    delegate = table.itemDelegate()
    colors = table.selection_style_colors
    for group, prefix, active in (
        (QPalette.Active, "active", True),
        (QPalette.Inactive, "inactive", False),
    ):
        for row, (_status_key, status_color) in enumerate(statuses):
            option = QStyleOptionViewItem()
            option.initFrom(table.viewport())
            option.state |= QStyle.State_Selected | QStyle.State_HasFocus
            if active:
                option.state |= QStyle.State_Active
            else:
                option.state &= ~QStyle.State_Active
            styled, boundary, had_focus = delegate._selection_option(  # type: ignore[attr-defined]
                option,
                table.model().index(row, 0),
            )

            assert styled.palette.color(group, QPalette.Highlight).name() == colors[
                f"{prefix}_background"
            ]
            assert colors[f"{prefix}_background"] != table.palette().color(
                group,
                QPalette.Highlight,
            ).name()
            assert (
                styled.palette.color(group, QPalette.HighlightedText).name()
                == status_color.name()
            )
            assert table.item(row, 0).icon().cacheKey() == icon_keys[row]
            assert had_focus is True
            assert not styled.state & QStyle.State_HasFocus
            assert (
                _contrast_ratio(
                    boundary,
                    QColor(colors[f"{prefix}_background"]),
                )
                >= 3.0
            )
    table.close()


def test_low_contrast_active_and_inactive_palettes_get_readable_neutral_boundaries() -> None:
    app = _app()
    table = SubtleSelectionTableWidget(1, 1)
    table.setItem(0, 0, QTableWidgetItem("default foreground"))
    initial_revision = table.selection_style_revision
    palette = QPalette(table.palette())
    for group in (QPalette.Active, QPalette.Inactive):
        palette.setColor(group, QPalette.Base, QColor("#f8f8f8"))
        palette.setColor(group, QPalette.Text, QColor("#f2f2f2"))
        palette.setColor(group, QPalette.WindowText, QColor("#202020"))
        palette.setColor(group, QPalette.Highlight, QColor("#1784d1"))

    table.setPalette(palette)
    app.processEvents()
    assert table.selection_style_revision > initial_revision

    colors = table.selection_style_colors
    for group, prefix in (
        (QPalette.Active, "active"),
        (QPalette.Inactive, "inactive"),
    ):
        background = QColor(colors[f"{prefix}_background"])
        text = QColor(colors[f"{prefix}_text"])
        boundary = QColor(colors[f"{prefix}_boundary"])
        assert background.name() != "#1784d1"
        assert _contrast_ratio(text, background) >= 4.5
        assert _contrast_ratio(boundary, background) >= 3.0

        option = QStyleOptionViewItem()
        option.initFrom(table.viewport())
        option.state |= QStyle.State_Selected
        if prefix == "active":
            option.state |= QStyle.State_Active
        else:
            option.state &= ~QStyle.State_Active
        styled, _boundary, _had_focus = table.itemDelegate()._selection_option(  # type: ignore[attr-defined]
            option,
            table.model().index(0, 0),
        )
        assert styled.palette.color(group, QPalette.HighlightedText).name() == text.name()

    palette_revision = table.selection_style_revision
    QApplication.sendEvent(table, QEvent(QEvent.StyleChange))
    assert table.selection_style_revision > palette_revision
    table.close()


def test_row_mouse_keyboard_ack_detail_and_refresh_selection_flow_is_preserved() -> None:
    app = _app()
    window = MainWindow(FakeCoordinator(), _settings())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())
    app.processEvents()

    problem_row = _row_for_ip(window.compact_table, "192.0.2.12")
    problem_rect = window.compact_table.visualItemRect(
        window.compact_table.item(problem_row, 0)
    )
    QTest.mouseClick(
        window.compact_table.viewport(),
        Qt.LeftButton,
        pos=problem_rect.center(),
    )
    assert window._selected_ip() == "192.0.2.12"

    ack_spy = QSignalSpy(window.acknowledge_requested)
    window.compact_ack_action.trigger()
    assert ack_spy.count() == 1
    assert ack_spy.at(0)[0] == "192.0.2.12"

    window.compact_table.setFocus(Qt.TabFocusReason)
    QTest.keyClick(window.compact_table, Qt.Key_Up)
    assert window._selected_ip() == "192.0.2.11"
    QTest.keyClick(window.compact_table, Qt.Key_Down)
    assert window._selected_ip() == "192.0.2.12"

    QTest.mouseDClick(
        window.compact_table.viewport(),
        Qt.LeftButton,
        pos=problem_rect.center(),
    )
    app.processEvents()
    assert "192.0.2.12" in window._detail_windows

    window.update_snapshot(_snapshot(active_clients=11))
    app.processEvents()
    assert window._selected_ip() == "192.0.2.12"

    window.resize(window.FULL_ENTER_WIDTH, 500)
    app.processEvents()
    assert window._dashboard_mode == window.FULL_MODE
    assert window._selected_ip() == "192.0.2.12"
    assert window.table.selectedItems()

    window.table.sortItems(1, Qt.DescendingOrder)
    assert window._selected_ip() == "192.0.2.12"

    window._quitting = True
    window.close()
