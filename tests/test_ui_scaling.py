from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from PySide6.QtCore import QRect, QSize

from aruba_mini_dashboard.ui.widgets import bounded_window_geometry


def test_restored_window_geometry_is_fully_bounded_on_negative_origin_monitor() -> None:
    available = QRect(-1920, 0, 1920, 1040)
    restored = QRect(-200, 980, 2400, 1400)

    bounded = bounded_window_geometry(
        restored,
        available,
        minimum_size=QSize(360, 260),
        margin=8,
    )

    assert available.contains(bounded)
    assert bounded.left() >= available.left() + 8
    assert bounded.top() >= available.top() + 8
    assert bounded.width() == available.width() - 16
    assert bounded.height() == available.height() - 16


@pytest.mark.gui
@pytest.mark.parametrize("scale", ["1.0", "1.25", "1.5"])
def test_dashboard_core_controls_are_not_clipped_at_supported_scales(scale: str) -> None:
    root = Path(__file__).resolve().parents[1]
    script = r'''
from PySide6.QtCore import QObject, Signal
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
w.show(); app.processEvents()
widgets=[w.status_label,w.problem_label,w.check_now_button,w.start_button,w.pause_button,w.settings_button]
assert all(x.height() >= x.minimumSizeHint().height() for x in widgets)
assert w.width() >= 420 and w.height() >= 320
w._quitting=True; w.close()
print('UI_SCALE_OK')
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
    assert "UI_SCALE_OK" in completed.stdout


@pytest.mark.gui
@pytest.mark.parametrize("scale", ["1.5", "2.0"])
def test_dashboard_restores_oversized_disconnected_monitor_geometry_safely(
    scale: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = r'''
from PySide6.QtCore import QObject, Signal
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
settings=AppSettings.default()
settings.ui.window_x=5000; settings.ui.window_y=3000
settings.ui.window_width=2200; settings.ui.window_height=1400
w=MainWindow(C(), settings)
w.show(); app.processEvents()
available=w.screen().availableGeometry()
assert available.contains(w.frameGeometry()), (available, w.frameGeometry())
w._quitting=True; w.close()
print('RESTORED_GEOMETRY_OK')
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
    assert "RESTORED_GEOMETRY_OK" in completed.stdout


@pytest.mark.gui
@pytest.mark.parametrize("scale", ["1.0", "1.25", "1.5", "2.0"])
def test_simplified_settings_fit_supported_windows_scales(scale: str) -> None:
    root = Path(__file__).resolve().parents[1]
    script = r'''
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.developer_inspector import (
    DeveloperInspectorCatalogDialog,
    DeveloperInspectorDetailDialog,
)
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog

app=QApplication([])
d=SettingsDialog(AppSettings.default(), initial_setup=True)
d.show(); app.processEvents()
assert d.width() >= d.minimumWidth() and d.height() >= d.minimumHeight()
assert d.tabs.count() == 3
tab_bar=d.tabs.tabBar()
for index in range(d.tabs.count()):
    rect=tab_bar.tabRect(index)
    assert rect.width() >= tab_bar.fontMetrics().horizontalAdvance(d.tabs.tabText(index)) + 12
    assert rect.height() >= tab_bar.fontMetrics().height() + 4
for button in (
    d.buttons.button(QDialogButtonBox.Save),
    d.buttons.button(QDialogButtonBox.Cancel),
    d.mm_test_button,
    d.cluster_test_button,
):
    assert button.height() >= button.minimumSizeHint().height()
assert d.mm_ip.toolTip()
assert d.primary_ip.toolTip()
assert d.low_spec_mode.toolTip()
assert d.performance_logging.toolTip()
dialogs = (
    d,
    DetailDialog({"ip": "192.0.2.1"}),
    DeveloperInspectorCatalogDialog(),
    DeveloperInspectorDetailDialog("0.3.6"),
)
for dialog in dialogs:
    dialog.show(); app.processEvents()
    available = dialog.screen().availableGeometry()
    assert available.contains(dialog.frameGeometry()), (
        type(dialog).__name__, available, dialog.frameGeometry()
    )
    dialog.close()
print('SETTINGS_SCALE_OK')
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
    assert "SETTINGS_SCALE_OK" in completed.stdout
