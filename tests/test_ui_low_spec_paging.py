from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.models import DeviceHealth, OverallHealth, Severity
from aruba_mini_dashboard.ui.main_window import MainWindow


class Coordinator(QObject):
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

    def __init__(self) -> None:
        super().__init__()
        self.interval = 60

    def check_now(self) -> None:
        return None

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, seconds: int) -> None:
        self.interval = seconds


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _ip(index: int) -> str:
    return f"198.51.{index // 254}.{index % 254 + 1}"


def _snapshot(
    count: int,
    *,
    aliases: dict[int, str] | None = None,
    problem_index: int | None = None,
) -> OverallHealth:
    now = datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc)
    aliases = aliases or {}
    devices = [
        DeviceHealth(
            ip=_ip(index),
            alias=aliases.get(index, f"WLC-{index:03d}"),
            mm_status="Down" if index == problem_index else "Up",
            active_clients=index,
            standby_clients=count - index,
            severity=Severity.CRITICAL if index == problem_index else Severity.NORMAL,
            issue_reasons=["MM Status Down"] if index == problem_index else [],
            last_seen=now,
        )
        for index in range(count)
    ]
    problem_ips = [_ip(problem_index)] if problem_index is not None else []
    return OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL if problem_ips else Severity.NORMAL,
        devices=devices,
        problem_ips=problem_ips,
        primary_problem_ip=problem_ips[0] if problem_ips else None,
    )


def _low_settings() -> AppSettings:
    settings = AppSettings.default()
    settings.performance.low_spec_mode = True
    return settings


def _full_window(settings: AppSettings | None = None) -> MainWindow:
    app = _app()
    window = MainWindow(Coordinator(), settings or _low_settings())
    window.resize(1100, 650)
    window.show()
    app.processEvents()
    assert window._dashboard_mode == window.FULL_MODE
    return window


def _row_for_ip(window: MainWindow, ip: str) -> int:
    for row in range(window.table.rowCount()):
        if window.table.item(row, 0).data(Qt.UserRole) == ip:
            return row
    raise AssertionError(f"row not found: {ip}")


@pytest.mark.parametrize("count", [0, 250])
def test_low_spec_full_table_does_not_page_at_or_below_threshold(count: int) -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(count))

    assert window.table.rowCount() == count
    assert not window.full_page_bar.isVisible()
    window._quitting = True
    window.close()


@pytest.mark.parametrize(
    ("count", "last_page_rows", "last_range", "page_count"),
    [
        (251, 1, "251–251 / 전체 251대", "2 / 2"),
        (501, 1, "501–501 / 전체 501대", "3 / 3"),
    ],
)
def test_low_spec_full_table_pages_large_snapshots_without_loss(
    count: int,
    last_page_rows: int,
    last_range: str,
    page_count: str,
) -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(count))

    assert window.table.rowCount() == 250
    assert window.full_page_bar.isVisible()
    assert window.full_page_range_label.text() == f"1–250 / 전체 {count}대"
    while window.full_next_button.isEnabled():
        window.full_next_button.click()
    assert window.table.rowCount() == last_page_rows
    assert window.full_page_range_label.text() == last_range
    assert window.full_page_count_label.text() == page_count
    visible_ips = {
        window.table.item(row, 0).data(Qt.UserRole) for row in range(window.table.rowCount())
    }
    assert len(visible_ips) == last_page_rows
    window._quitting = True
    window.close()


def test_normal_mode_keeps_all_devices_and_hides_page_controls() -> None:
    settings = AppSettings.default()
    settings.performance.low_spec_mode = False
    window = _full_window(settings)
    window.update_snapshot(_snapshot(501))

    assert window.table.rowCount() == 501
    assert not window.full_page_bar.isVisible()
    window._quitting = True
    window.close()


def test_global_sort_moves_selected_ip_to_its_new_page_and_preserves_header() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(501))
    selected_ip = _ip(0)
    window.table.selectRow(_row_for_ip(window, selected_ip))

    window.table.sortItems(1, Qt.DescendingOrder)
    _app().processEvents()

    assert window._full_page_index == 2
    assert window._selected_ip() == selected_ip
    assert window.table.horizontalHeader().sortIndicatorSection() == 1
    assert window.table.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder
    assert window.table.item(0, 1).text() == "WLC-000"

    updated = _snapshot(501, aliases={0: "ZZZ-SELECTED"})
    window.update_snapshot(updated)
    assert window._full_page_index == 0
    assert window._selected_ip() == selected_ip
    assert window.table.item(0, 1).text() == "ZZZ-SELECTED"
    window._quitting = True
    window.close()


def test_global_sort_uses_ip_as_stable_tie_breaker_before_page_slice() -> None:
    window = _full_window()
    aliases = {index: "SAME" for index in range(501)}
    window.update_snapshot(_snapshot(501, aliases=aliases))

    window.table.sortItems(1, Qt.DescendingOrder)
    _app().processEvents()

    visible_ips = [
        window.table.item(row, 0).data(Qt.UserRole) for row in range(window.table.rowCount())
    ]
    assert visible_ips == sorted(_ip(index) for index in range(501))[:250]
    window._quitting = True
    window.close()


def test_header_click_changes_global_sort_order_when_native_page_sort_is_disabled() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(251))
    header = window.table.horizontalHeader()
    x = header.sectionViewportPosition(1) + header.sectionSize(1) // 2

    QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=QPoint(x, 5))
    assert window._full_sort_column == 1
    first_order = window._full_sort_order
    QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=QPoint(x, 5))
    assert window._full_sort_order != first_order
    expected_first = "WLC-250" if window._full_sort_order == Qt.DescendingOrder else "WLC-000"
    assert window.table.item(0, 1).text() == expected_first
    window._quitting = True
    window.close()


def test_page_navigation_clears_hidden_selection_and_actions_target_visible_row() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(501, problem_index=300))
    window.full_next_button.click()
    problem_ip = _ip(300)
    window.table.selectRow(_row_for_ip(window, problem_ip))
    spy = QSignalSpy(window.acknowledge_requested)
    window._acknowledge_selected()
    assert spy.count() == 1
    assert spy.at(0)[0] == problem_ip
    window._open_detail_for_item(window.table.item(_row_for_ip(window, problem_ip), 0))
    assert problem_ip in window._detail_windows
    window._detail_windows[problem_ip].close()

    window.full_previous_button.click()
    assert not window.table.selectedItems()
    assert not window.compact_table.selectedItems()
    assert window._selected_ip() == ""
    window._quitting = True
    window.close()


def test_snapshot_shrink_clamps_last_page_to_available_rows() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(501))
    window.full_next_button.click()
    window.full_next_button.click()
    assert window._full_page_index == 2

    window.update_snapshot(_snapshot(10))
    assert window._full_page_index == 0
    assert window.table.rowCount() == 10
    assert not window.full_page_bar.isVisible()
    window._quitting = True
    window.close()


def test_compact_selection_opens_matching_full_page_and_survives_mode_changes() -> None:
    settings = _low_settings()
    target_index = 500
    configured = [target_index, 1, 2, 3]
    settings.cluster.members = [
        ClusterMemberSettings(_ip(index), f"REGISTERED-{index}") for index in configured
    ]
    app = _app()
    window = MainWindow(Coordinator(), settings)
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot(501))
    assert window.compact_table.rowCount() == 4
    target_ip = _ip(target_index)
    compact_row = next(
        row
        for row in range(window.compact_table.rowCount())
        if window.compact_table.item(row, 0).data(Qt.UserRole) == target_ip
    )
    window.compact_table.selectRow(compact_row)

    window.resize(1100, 650)
    app.processEvents()
    expected_page = sorted(_ip(index) for index in range(501)).index(target_ip) // 250
    assert window._full_page_index == expected_page
    assert window._selected_ip() == target_ip

    window.resize(899, 650)
    app.processEvents()
    assert window._selected_ip() == target_ip
    window._quitting = True
    window.close()


def test_compact_off_page_selection_repages_an_already_rendered_full_table() -> None:
    settings = _low_settings()
    target_index = 500
    configured = [target_index, 1, 2, 3]
    settings.cluster.members = [
        ClusterMemberSettings(_ip(index), f"REGISTERED-{index}") for index in configured
    ]
    app = _app()
    window = MainWindow(Coordinator(), settings)
    window.resize(1100, 650)
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot(501))
    assert window._dashboard_mode == window.FULL_MODE
    assert window._table_dirty[window.FULL_MODE] is False

    window.resize(899, 650)
    app.processEvents()
    target_ip = _ip(target_index)
    compact_row = next(
        row
        for row in range(window.compact_table.rowCount())
        if window.compact_table.item(row, 0).data(Qt.UserRole) == target_ip
    )
    window.compact_table.selectRow(compact_row)

    window.resize(1100, 650)
    app.processEvents()
    expected_page = sorted(_ip(index) for index in range(501)).index(target_ip) // 250
    assert window._full_page_index == expected_page
    assert window._selected_ip() == target_ip
    assert _row_for_ip(window, target_ip) >= 0
    assert window._table_dirty[window.FULL_MODE] is False
    window._quitting = True
    window.close()


def test_toggling_low_spec_reflows_current_snapshot_without_polling() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(251))
    assert window.table.rowCount() == 250

    settings = AppSettings.default()
    settings.performance.low_spec_mode = False
    assert window.apply_settings(settings)
    assert window.table.rowCount() == 251
    assert not window.full_page_bar.isVisible()

    settings.performance.low_spec_mode = True
    assert window.apply_settings(settings)
    assert window.table.rowCount() == 250
    assert window.full_page_bar.isVisible()
    assert "자동 점검 120초 적용" in window.statusBar().currentMessage()
    assert window.busy_label.text() == "저사양 · 자동 120초"
    assert window.compact_busy_label.text() == "저사양 · 자동 120초"
    window._busy_changed(True)
    assert window.busy_label.text() == "● 점검 중 · 저사양"
    assert window.compact_busy_label.text() == "● 점검 중 · 저사양"
    window._quitting = True
    window.close()


def test_hidden_low_spec_window_renders_only_latest_active_page_on_restore() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(501))
    first_item = window.table.item(0, 0)
    window._hidden_to_tray = True
    window.hide()
    window.update_snapshot(_snapshot(501, aliases={0: "LATEST"}))
    assert window.table.item(0, 0) is first_item

    window.show_dashboard()
    _app().processEvents()
    assert window.table.rowCount() == 250
    assert any(
        window.table.item(row, 1).text() == "LATEST" for row in range(window.table.rowCount())
    )
    window._quitting = True
    window.close()


def test_page_controls_expose_accessible_names_and_descriptions() -> None:
    window = _full_window()
    window.update_snapshot(_snapshot(251))
    for widget in (
        window.full_page_bar,
        window.full_page_range_label,
        window.full_page_count_label,
        window.full_previous_button,
        window.full_next_button,
    ):
        assert widget.accessibleName().strip()
    assert window.full_page_bar.accessibleDescription().strip()
    assert window.full_previous_button.toolTip().strip()
    assert window.full_next_button.toolTip().strip()
    window._quitting = True
    window.close()


@pytest.mark.gui
@pytest.mark.parametrize("scale", ["1.0", "1.25", "1.5"])
def test_low_spec_page_bar_fits_supported_windows_scales(scale: str) -> None:
    root = Path(__file__).resolve().parents[1]
    script = r'''
from datetime import datetime, timezone
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.models import DeviceHealth, OverallHealth, Severity
from aruba_mini_dashboard.ui.main_window import MainWindow

class C(QObject):
    cycle_started=Signal(str,object); cycle_finished=Signal(object); cycle_failed=Signal(object)
    busy_changed=Signal(bool); automatic_changed=Signal(bool); next_check_changed=Signal(object)
    scheduled_poll_skipped=Signal(str); manual_poll_queued=Signal()
    busy=False; automatic=False
    def check_now(self): pass
    def start_automatic(self): pass
    def pause_automatic(self): pass
    def set_interval(self, value): pass

app=QApplication([])
s=AppSettings.default(); s.performance.low_spec_mode=True
w=MainWindow(C(), s); w.resize(1000,500); w.show(); app.processEvents()
now=datetime.now(timezone.utc)
devices=[DeviceHealth(ip=f'198.51.{i//254}.{i%254+1}',alias=f'WLC-{i:03d}',mm_status='Up',severity=Severity.NORMAL,last_seen=now) for i in range(251)]
w.update_snapshot(OverallHealth(checked_at=now,severity=Severity.NORMAL,devices=devices)); app.processEvents()
assert w.full_page_bar.isVisible()
for control in (w.full_previous_button,w.full_page_count_label,w.full_next_button):
    assert control.height() >= control.minimumSizeHint().height()
assert w.table.rowCount() == 250
w._quitting=True; w.close()
print('LOW_SPEC_PAGE_SCALE_OK')
'''
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_SCALE_FACTOR"] = scale
    env["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "LOW_SPEC_PAGE_SCALE_OK" in completed.stdout


def test_low_spec_tooltip_describes_effective_resource_limits_without_sequential_claim() -> None:
    from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(_low_settings())
    tooltip = dialog.low_spec_mode.toolTip()
    assert "최소 120초" in tooltip
    assert "내용에 따라 압축 여부" in tooltip
    assert "최대 2개" in tooltip
    assert "250대씩" in tooltip
    assert "2MB" in tooltip
    assert "백업 2개" in tooltip
    assert "같은 명령" in tooltip
    assert "결과 정확성" in tooltip
    assert "순차" not in tooltip
    dialog.close()


def test_demo_save_message_states_current_run_only_and_effective_interval() -> None:
    window = _full_window()
    window.demo_mode = True
    settings = _low_settings()
    settings.polling.interval_seconds = 30
    assert window.apply_settings(settings)
    message = window.statusBar().currentMessage()
    assert "영구 저장하지 않고 이번 실행에만 적용" in message
    assert "자동 점검 120초 적용" in message
    window._quitting = True
    window.close()
