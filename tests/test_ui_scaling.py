from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


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
