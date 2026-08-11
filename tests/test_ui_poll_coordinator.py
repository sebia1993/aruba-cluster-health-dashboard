from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.services.poll_coordinator import PollCoordinator


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _app().processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return False


def test_poll_runs_off_gui_thread_and_emits_result() -> None:
    _app()
    gui_thread = threading.get_ident()
    observed: list[int] = []
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect() -> dict[str, bool]:
        observed.append(threading.get_ident())
        return {"ok": True}

    coordinator = PollCoordinator(collect, thread_pool=pool)
    completed = QSignalSpy(coordinator.cycle_finished)
    coordinator.check_now()
    assert _wait_until(lambda: completed.count() == 1)
    assert observed and observed[0] != gui_thread
    assert completed.at(0)[0] == {"ok": True}
    assert not coordinator.busy
    assert coordinator.shutdown()


def test_manual_requests_while_busy_are_coalesced() -> None:
    _app()
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect(cancel_event) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            release.wait(2)
        return calls

    coordinator = PollCoordinator(collect, thread_pool=pool)
    completed = QSignalSpy(coordinator.cycle_finished)
    queued = QSignalSpy(coordinator.manual_poll_queued)
    coordinator.check_now()
    assert entered.wait(1)
    coordinator.check_now()
    coordinator.check_now()
    assert queued.count() == 1
    release.set()
    assert _wait_until(lambda: completed.count() == 2)
    assert calls == 2
    assert coordinator.shutdown()


def test_automatic_tick_is_skipped_while_busy() -> None:
    _app()
    entered = threading.Event()
    release = threading.Event()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect() -> None:
        entered.set()
        release.wait(2)

    coordinator = PollCoordinator(collect, thread_pool=pool)
    skipped = QSignalSpy(coordinator.scheduled_poll_skipped)
    coordinator.check_now()
    assert entered.wait(1)
    coordinator.start_automatic()
    coordinator._on_automatic_timeout()
    assert skipped.count() == 1
    assert "건너뛰" in skipped.at(0)[0]
    release.set()
    assert _wait_until(lambda: not coordinator.busy)
    coordinator.pause_automatic()
    assert coordinator.shutdown()


def test_optional_cancellation_argument_is_passed_during_shutdown() -> None:
    _app()
    entered = threading.Event()
    received: list[threading.Event] = []
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect(cancel_event=None) -> str:
        received.append(cancel_event)
        entered.set()
        cancel_event.wait(2)
        return "cancelled"

    coordinator = PollCoordinator(collect, thread_pool=pool)
    coordinator.check_now()
    assert entered.wait(1)
    assert coordinator.shutdown(2000)
    assert received and isinstance(received[0], threading.Event)
    assert received[0].is_set()


def test_connection_test_runs_in_worker_thread() -> None:
    _app()
    gui_thread = threading.get_ident()
    worker_threads: list[int] = []
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def tester(role, settings, cancel_event):
        worker_threads.append(threading.get_ident())
        return {"role": role, "status": "success"}

    coordinator = PollCoordinator(
        lambda: None,
        thread_pool=pool,
        connection_tester=tester,
    )
    finished = QSignalSpy(coordinator.connection_test_finished)
    coordinator.test_connection("mm", {"host": "192.0.2.1"})
    assert _wait_until(lambda: finished.count() == 1)
    assert worker_threads[0] != gui_thread
    assert finished.at(0)[0] == "mm"
    assert finished.at(0)[1]["status"] == "success"
    assert coordinator.shutdown()
