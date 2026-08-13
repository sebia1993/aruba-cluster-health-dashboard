from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aruba_mini_dashboard.main import _try_close_runtime_resources


class _CloseRecorder:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _UnlockRecorder:
    def __init__(self) -> None:
        self.unlock_calls = 0

    def unlock(self) -> None:
        self.unlock_calls += 1


class _Coordinator:
    def __init__(self) -> None:
        self.results = iter((False, True))
        self.timeouts: list[int] = []

    def shutdown(self, timeout_ms: int) -> bool:
        self.timeouts.append(timeout_ms)
        return next(self.results)


def test_external_shutdown_retries_without_closing_live_worker_resources() -> None:
    inspector = _CloseRecorder()
    coordinator = _Coordinator()
    credentials = _CloseRecorder()
    storage = _CloseRecorder()
    logging_context = _CloseRecorder()
    instance_lock = _UnlockRecorder()

    stopped = _try_close_runtime_resources(
        inspector,
        coordinator,
        credentials,
        storage,
        logging_context,
        instance_lock,
        timeout_ms=0,
    )

    assert stopped is False
    assert credentials.close_calls == 0
    assert storage.close_calls == 0
    assert logging_context.close_calls == 0
    assert instance_lock.unlock_calls == 0

    stopped = _try_close_runtime_resources(
        inspector,
        coordinator,
        credentials,
        storage,
        logging_context,
        instance_lock,
        timeout_ms=5000,
    )

    assert stopped is True
    assert coordinator.timeouts == [0, 5000]
    assert inspector.close_calls == 2
    assert credentials.close_calls == 1
    assert storage.close_calls == 1
    assert logging_context.close_calls == 1
    assert instance_lock.unlock_calls == 1


def test_local_blocked_ssh_quit_exits_process_without_waiting_for_network_timeout(
    tmp_path: Path,
) -> None:
    probe = r'''
import os
import socket
import sys
import threading

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import netmiko  # prewarm the lazy driver registry outside the measured quit path
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from aruba_mini_dashboard.collectors.aruba_ssh import ArubaSshAdapter
from aruba_mini_dashboard.collectors.base import SshConnectionOptions
from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.credentials import DeviceCredential
from aruba_mini_dashboard.services.poll_coordinator import PollCoordinator
from aruba_mini_dashboard.ui.main_window import MainWindow

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
host, port = listener.getsockname()
accepted = threading.Event()
release = threading.Event()
active = []

def server():
    connection, _ = listener.accept()
    accepted.set()
    release.wait(10)
    connection.close()

threading.Thread(target=server, daemon=True).start()

def collect(cancel_event):
    adapter = ArubaSshAdapter(
        SshConnectionOptions(host, port, 30, 30, sys.argv[1]),
        DeviceCredential("fixture-user", "fixture-password"),
        cancel_event=cancel_event,
    )
    active.append(adapter)
    try:
        adapter.connect()
    finally:
        adapter.close()

def cancel_active():
    for adapter in tuple(active):
        adapter.abort()

app = QApplication([])
pool = QThreadPool(app)
pool.setMaxThreadCount(1)
coordinator = PollCoordinator(
    collect,
    thread_pool=pool,
    cancel_active_work=cancel_active,
)
window = MainWindow(coordinator, AppSettings.default())
window.show()
coordinator.check_now()

def request_quit_when_connected():
    if accepted.is_set():
        window.request_quit()
    else:
        QTimer.singleShot(10, request_quit_when_connected)

QTimer.singleShot(10, request_quit_when_connected)
QTimer.singleShot(8000, lambda: app.exit(9))
exit_code = app.exec()
stopped = coordinator.shutdown(2000)
release.set()
listener.close()
window.tray_icon.hide()
print("LOCAL_BLOCKED_SSH_SHUTDOWN_OK", flush=True)
raise SystemExit(0 if exit_code == 0 and stopped else 7)
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path / "known_hosts")],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCAL_BLOCKED_SSH_SHUTDOWN_OK" in result.stdout
