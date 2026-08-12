from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.models import (
    ControllerState,
    DeviceHealth,
    DistributionState,
    OverallHealth,
    Severity,
)
from aruba_mini_dashboard.ui.main_window import MainWindow


class FakeCoordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.automatic = False
        self.check_count = 0

    def check_now(self) -> None:
        self.check_count += 1

    def start_automatic(self) -> None:
        self.automatic = True
        self.automatic_changed.emit(True)

    def pause_automatic(self) -> None:
        self.automatic = False
        self.automatic_changed.emit(False)

    def set_interval(self, _seconds: int) -> None:
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _settings() -> AppSettings:
    settings = AppSettings.default()
    settings.cluster.members = [
        ClusterMemberSettings("192.0.2.13", "WLC-03"),
        ClusterMemberSettings("192.0.2.11", "WLC-01"),
        ClusterMemberSettings("192.0.2.12", "WLC-02"),
        ClusterMemberSettings("192.0.2.14", "WLC-04"),
    ]
    return settings


def _snapshot() -> OverallHealth:
    now = datetime(2026, 8, 11, 1, 30, 5, tzinfo=timezone.utc)
    devices = [
        DeviceHealth(
            ip="192.0.2.99",
            alias="LAB-WLC",
            is_registered=False,
            controller_state=ControllerState.DOWN,
            distribution_state=DistributionState.UNKNOWN,
            mm_status="Down",
            severity=Severity.CRITICAL,
            last_seen=now,
        ),
        DeviceHealth(
            ip="192.0.2.11",
            alias="WLC-01",
            controller_state=ControllerState.UP,
            distribution_state=DistributionState.NORMAL,
            mm_status="Up",
            active_clients=250,
            standby_clients=260,
            severity=Severity.NORMAL,
            last_seen=now,
        ),
        DeviceHealth(
            ip="192.0.2.12",
            alias="WLC-02",
            controller_state=ControllerState.DOWN,
            distribution_state=DistributionState.ANOMALOUS,
            mm_status="Down",
            active_clients=0,
            standby_clients=4,
            load_anomaly=True,
            load_anomaly_streak=3,
            severity=Severity.CRITICAL,
            issue_reasons=["MM Status Down", "Client 분배 이상"],
            last_seen=now,
        ),
        DeviceHealth(
            ip="192.0.2.13",
            alias="WLC-03",
            controller_state=ControllerState.UP,
            distribution_state=DistributionState.OBSERVING,
            load_anomaly_streak=2,
            mm_status="Up",
            severity=Severity.NORMAL,
            last_seen=now,
        ),
        DeviceHealth(
            ip="192.0.2.14",
            alias="WLC-04",
            controller_state=ControllerState.MISSING,
            distribution_state=DistributionState.LOW_USAGE,
            severity=Severity.WARNING,
            last_seen=now,
        ),
    ]
    return OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL,
        devices=devices,
        monitoring_scope_ips=("192.0.2.13", "192.0.2.11", "192.0.2.12", "192.0.2.14"),
        problem_ips=["192.0.2.12"],
        primary_problem_ip="192.0.2.12",
    )


def _row_for_ip(table, ip: str) -> int:
    for row in range(table.rowCount()):
        if table.item(row, 0).data(Qt.UserRole) == ip:
            return row
    raise AssertionError(f"row not found: {ip}")


def test_compact_mode_shows_only_registered_controllers_in_configured_order() -> None:
    app = _app()
    window = MainWindow(FakeCoordinator(), _settings())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())
    app.processEvents()

    assert window._dashboard_mode == window.COMPACT_MODE
    assert window.dashboard_stack.currentWidget() is window.compact_page
    assert window.compact_table.rowCount() == 4
    assert [
        window.compact_table.item(row, 0).data(Qt.UserRole) for row in range(4)
    ] == ["192.0.2.13", "192.0.2.11", "192.0.2.12", "192.0.2.14"]
    assert window.compact_table.item(0, 2).text() == "관찰 2회"
    assert window.compact_table.item(2, 1).text() == "Down"
    assert window.compact_table.item(2, 2).text() == "이상"
    assert window.compact_table.item(3, 1).text() == "누락"
    assert window.compact_last_check_label.text().startswith("마지막: ")
    assert len(window.compact_last_check_label.text().removeprefix("마지막: ")) == 8
    assert window.compact_table.horizontalScrollBar().maximum() == 0

    window._quitting = True
    window.close()


def test_responsive_breakpoints_are_hysteretic_and_do_not_start_a_poll() -> None:
    app = _app()
    coordinator = FakeCoordinator()
    window = MainWindow(coordinator, _settings())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())

    window.resize(1000, 500)
    app.processEvents()
    assert window._dashboard_mode == window.FULL_MODE
    window.resize(950, 500)
    app.processEvents()
    assert window._dashboard_mode == window.FULL_MODE
    window.resize(899, 500)
    app.processEvents()
    assert window._dashboard_mode == window.COMPACT_MODE
    window.resize(950, 500)
    app.processEvents()
    assert window._dashboard_mode == window.COMPACT_MODE
    window.showMaximized()
    app.processEvents()
    assert window.isMaximized()
    assert window._dashboard_mode == window.FULL_MODE
    assert coordinator.check_count == 0

    window._quitting = True
    window.close()


def test_maximized_state_is_saved_and_restored_as_full_mode() -> None:
    app = _app()
    settings = _settings()
    window = MainWindow(FakeCoordinator(), settings)
    window.showMaximized()
    app.processEvents()

    window._save_window_state()
    assert settings.ui.window_maximized is True
    window._quitting = True
    window.close()

    restored = MainWindow(FakeCoordinator(), settings)
    restored.show()
    app.processEvents()
    assert restored.isMaximized()
    assert restored._dashboard_mode == restored.FULL_MODE
    restored._quitting = True
    restored.close()


def test_full_mode_includes_unregistered_device_as_neutral_and_preserves_ip_selection() -> None:
    app = _app()
    window = MainWindow(FakeCoordinator(), _settings())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())
    window.resize(1000, 500)
    app.processEvents()

    assert window.table.rowCount() == 5
    unregistered = _row_for_ip(window.table, "192.0.2.99")
    assert window.table.item(unregistered, 6).text() == "감시 제외"
    assert window.table.item(unregistered, 8).text() == "미등록 · 감시 제외"
    assert window.table.item(unregistered, 2).text() == "Down"

    problem_row = _row_for_ip(window.table, "192.0.2.12")
    window.table.selectRow(problem_row)
    spy = QSignalSpy(window.acknowledge_requested)
    window.table.sortItems(1, Qt.DescendingOrder)
    window._acknowledge_selected()
    assert spy.count() == 1
    assert spy.at(0)[0] == "192.0.2.12"

    window.resize(899, 500)
    app.processEvents()
    assert window._selected_ip() == "192.0.2.12"

    window._quitting = True
    window.close()


def test_member_edit_immediately_reclassifies_cached_snapshot_without_polling() -> None:
    app = _app()
    coordinator = FakeCoordinator()
    window = MainWindow(coordinator, _settings())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())

    changed = _settings()
    changed.cluster.members[2] = ClusterMemberSettings("192.0.2.99", "WLC-NEW")
    assert window.apply_settings(changed)
    app.processEvents()

    compact_ips = [
        window.compact_table.item(row, 0).data(Qt.UserRole)
        for row in range(window.compact_table.rowCount())
    ]
    assert compact_ips == ["192.0.2.13", "192.0.2.11", "192.0.2.99", "192.0.2.14"]
    new_row = _row_for_ip(window.compact_table, "192.0.2.99")
    assert window.compact_table.item(new_row, 0).text().startswith("WLC-NEW")
    assert window.compact_table.item(new_row, 1).text() == "확인 불가"
    assert window.compact_status_label.text() == "확인 불가"
    assert coordinator.check_count == 0

    window.resize(1000, 500)
    app.processEvents()
    removed_row = _row_for_ip(window.table, "192.0.2.12")
    assert window.table.item(removed_row, 8).text() == "미등록 · 감시 제외"

    window._quitting = True
    window.close()


@pytest.mark.gui
@pytest.mark.parametrize("scale", ["1.0", "1.25", "1.5"])
def test_compact_dashboard_fits_supported_windows_scales(scale: str) -> None:
    root = Path(__file__).resolve().parents[1]
    script = r'''
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication
from aruba_mini_dashboard.config import AppSettings
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
w=MainWindow(C(), AppSettings.default())
w.resize(360,260); w.show(); app.processEvents()
assert w._dashboard_mode == w.COMPACT_MODE
assert w.dashboard_stack.currentWidget() is w.compact_page
for widget in (w.compact_status_label,w.compact_check_now_button,w.compact_auto_button,w.compact_more_button):
    assert widget.height() >= widget.minimumSizeHint().height()
assert w.compact_table.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
w._quitting=True; w.close()
print('COMPACT_SCALE_OK')
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
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "COMPACT_SCALE_OK" in completed.stdout
