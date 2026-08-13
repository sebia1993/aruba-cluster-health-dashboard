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


def test_shutdown_aborts_blocking_active_work_off_the_gui_thread() -> None:
    _app()
    gui_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    cancel_threads: list[int] = []
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect(_cancel_event) -> str:
        entered.set()
        assert release.wait(2)
        return "transport-closed"

    def cancel_active() -> None:
        cancel_threads.append(threading.get_ident())
        release.set()

    coordinator = PollCoordinator(
        collect,
        thread_pool=pool,
        cancel_active_work=cancel_active,
    )
    coordinator.check_now()
    assert entered.wait(1)

    assert coordinator.shutdown(2000)
    assert cancel_threads and cancel_threads[0] != gui_thread
    assert release.is_set()


def test_shutdown_timeout_also_bounds_a_stuck_abort_callback() -> None:
    _app()
    release = threading.Event()

    coordinator = PollCoordinator(
        lambda: None,
        cancel_active_work=lambda: release.wait(2),
    )
    started = time.monotonic()
    assert coordinator.shutdown(50) is False
    assert time.monotonic() - started < 0.5

    release.set()
    assert coordinator.shutdown(1000) is True


def test_shutdown_suppresses_late_poll_and_connection_result_signals() -> None:
    _app()
    poll_entered = threading.Event()
    test_entered = threading.Event()
    release = threading.Event()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def collect(_cancel_event) -> str:
        poll_entered.set()
        release.wait(2)
        return "late-result"

    coordinator = PollCoordinator(collect, thread_pool=pool)
    poll_results = QSignalSpy(coordinator.cycle_finished)
    poll_failures = QSignalSpy(coordinator.cycle_failed)
    coordinator.check_now()
    assert poll_entered.wait(1)
    coordinator.request_shutdown()
    release.set()
    assert _wait_until(lambda: not coordinator.busy)
    assert poll_results.count() == 0
    assert poll_failures.count() == 0
    assert coordinator.shutdown()

    release.clear()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)

    def tester(_role, _settings, _cancel_event) -> str:
        test_entered.set()
        release.wait(2)
        return "late-test-result"

    coordinator = PollCoordinator(
        lambda: None,
        thread_pool=pool,
        connection_tester=tester,
    )
    test_results = QSignalSpy(coordinator.connection_test_finished)
    test_failures = QSignalSpy(coordinator.connection_test_failed)
    coordinator.test_connection("mm", {})
    assert test_entered.wait(1)
    coordinator.request_shutdown()
    release.set()
    assert _wait_until(lambda: not coordinator.busy)
    assert test_results.count() == 0
    assert test_failures.count() == 0
    assert coordinator.shutdown()


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


def test_approval_pending_connection_request_is_one_shot_and_cleared_on_shutdown() -> None:
    _app()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    secret_request = object()

    class ApprovalRequired:
        status = "approval_required"

    coordinator = PollCoordinator(
        lambda: None,
        thread_pool=pool,
        connection_tester=lambda *_args: ApprovalRequired(),
    )
    finished = QSignalSpy(coordinator.connection_test_finished)
    coordinator.test_connection("mm", secret_request)
    assert _wait_until(lambda: finished.count() == 1)
    assert coordinator._connection_test_settings["mm"] is secret_request

    coordinator.discard_connection_test("mm")
    assert "mm" not in coordinator._connection_test_settings

    coordinator._connection_test_settings["cluster"] = secret_request
    coordinator.request_shutdown()
    assert coordinator._connection_test_settings == {}
    assert coordinator.shutdown()


def test_retry_consumes_approval_pending_request_even_when_coordinator_is_busy() -> None:
    _app()
    coordinator = PollCoordinator(lambda: None, connection_tester=lambda *_args: None)
    coordinator._connection_test_settings["mm"] = object()
    coordinator._busy = True

    coordinator.retry_connection_test("mm")

    assert "mm" not in coordinator._connection_test_settings
    coordinator._busy = False
    assert coordinator.shutdown()


def test_shutdown_prevents_new_poll_and_connection_test_submissions() -> None:
    _app()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    poll_calls: list[bool] = []
    connection_calls: list[str] = []

    def collect() -> None:
        poll_calls.append(True)

    def tester(role, _settings, _cancel_event) -> None:
        connection_calls.append(role)

    coordinator = PollCoordinator(
        collect,
        thread_pool=pool,
        connection_tester=tester,
    )
    connection_failed = QSignalSpy(coordinator.connection_test_failed)
    automatic_rejected = QSignalSpy(coordinator.automatic_start_rejected)

    coordinator.request_shutdown()
    coordinator.check_now()
    coordinator.test_connection("mm", {"host": "192.0.2.1"})
    coordinator.start_automatic()
    _app().processEvents()

    assert poll_calls == []
    assert connection_calls == []
    assert not coordinator.busy
    assert not coordinator.automatic
    assert connection_failed.count() == 0
    assert automatic_rejected.count() == 0
    assert coordinator.shutdown()
