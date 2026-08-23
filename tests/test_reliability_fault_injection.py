from __future__ import annotations

import copy
import errno
import logging
import os
import random
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.collectors.cancellable_socket import (
    SocketConnectCancelledError,
    open_cancellable_ipv4_socket,
)
from aruba_mini_dashboard.config import AppSettings, SettingsStore
from aruba_mini_dashboard.parsers import (
    parse_group_membership,
    parse_load_distribution,
    parse_show_switches,
)
from aruba_mini_dashboard.services.poll_coordinator import PollCoordinator


DEFAULT_RELIABILITY_CYCLES = 120
MAX_RELIABILITY_CYCLES = 10_000


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _cycles() -> int:
    raw = os.environ.get("ARUBA_RELIABILITY_CYCLES", "").strip()
    if not raw:
        return DEFAULT_RELIABILITY_CYCLES
    try:
        value = int(raw)
    except ValueError as exc:
        raise AssertionError("ARUBA_RELIABILITY_CYCLES must be an integer") from exc
    if not 25 <= value <= MAX_RELIABILITY_CYCLES:
        raise AssertionError(
            f"ARUBA_RELIABILITY_CYCLES must be between 25 and {MAX_RELIABILITY_CYCLES}"
        )
    return value


def _wait_until(predicate, timeout_ms: int = 5000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _app().processEvents()
        if predicate():
            return True
        QTest.qWait(1)
    return False


@pytest.mark.reliability
def test_poll_worker_fault_soak_never_overlaps_or_retains_cycle_state() -> None:
    """Repeat timeout/disconnect/unexpected failures with one queued request."""

    _app()
    cycles = _cycles()
    pool = QThreadPool()
    pool.setMaxThreadCount(4)
    state_lock = threading.Lock()
    calls = 0
    active = 0
    maximum_active = 0

    def collect(_cancel_event) -> int:
        nonlocal calls, active, maximum_active
        with state_lock:
            calls += 1
            call_number = calls
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            remainder = call_number % 13
            if remainder == 3:
                raise TimeoutError("injected SSH timeout")
            if remainder == 8:
                raise ConnectionResetError("injected SSH disconnect")
            if remainder == 12:
                raise RuntimeError("injected collector failure")
            return call_number
        finally:
            with state_lock:
                active -= 1

    coordinator = PollCoordinator(collect, thread_pool=pool)
    completed = QSignalSpy(coordinator.cycle_finished)
    failed = QSignalSpy(coordinator.cycle_failed)
    busy_changed = QSignalSpy(coordinator.busy_changed)

    for _index in range(cycles):
        expected_total = completed.count() + failed.count() + 2
        coordinator.check_now()
        coordinator.check_now()
        coordinator.check_now()
        assert _wait_until(
            lambda: completed.count() + failed.count() == expected_total
            and not coordinator.busy
            and not coordinator._workers
        )
        assert coordinator._manual_pending is False
        assert coordinator._connection_workers == {}

    expected_calls = cycles * 2
    expected_failures = sum(
        call_number % 13 in {3, 8, 12}
        for call_number in range(1, expected_calls + 1)
    )
    assert calls == expected_calls
    assert failed.count() == expected_failures
    assert completed.count() == expected_calls - expected_failures
    assert maximum_active == 1
    assert active == 0
    assert pool.waitForDone(1000)
    assert pool.activeThreadCount() == 0
    assert busy_changed.count() == expected_calls * 2
    assert [busy_changed.at(index)[0] for index in range(busy_changed.count())] == [
        value
        for _call in range(expected_calls)
        for value in (True, False)
    ]
    assert coordinator.shutdown()


@pytest.mark.reliability
def test_worker_submission_rejection_soak_releases_every_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejecting Qt pool must not retain workers or credential overrides."""

    _app()
    caplog.set_level(
        logging.CRITICAL,
        logger="aruba_mini_dashboard.services.poll_coordinator",
    )

    class RejectingThreadPool:
        def __init__(self) -> None:
            self.start_calls = 0

        def start(self, _worker) -> None:
            self.start_calls += 1
            raise RuntimeError("injected private scheduler detail")

        def waitForDone(self, _timeout_ms: int) -> bool:
            return True

    pool = RejectingThreadPool()
    coordinator = PollCoordinator(
        lambda: None,
        thread_pool=pool,
        connection_tester=lambda *_args: None,
    )
    cycle_failed = QSignalSpy(coordinator.cycle_failed)
    connection_failed = QSignalSpy(coordinator.connection_test_failed)
    busy_changed = QSignalSpy(coordinator.busy_changed)

    for index in range(_cycles()):
        coordinator.check_now()
        assert cycle_failed.count() == index + 1
        assert "private scheduler" not in str(cycle_failed.at(index)[0])
        assert coordinator._workers == set()
        assert coordinator._manual_pending is False
        assert not coordinator.busy

        transient_request = {"credential_override": object()}
        coordinator.test_connection("all", transient_request)
        assert connection_failed.count() == index + 1
        assert "private scheduler" not in str(connection_failed.at(index)[1])
        assert coordinator._connection_test_settings == {}
        assert coordinator._connection_workers == {}
        assert coordinator._workers == set()
        assert coordinator._manual_pending is False
        assert not coordinator.busy

    assert pool.start_calls == _cycles() * 2
    assert busy_changed.count() == _cycles() * 4
    assert coordinator.shutdown()


@pytest.mark.reliability
def test_connection_fault_and_approval_soak_releases_every_one_shot_request() -> None:
    """Exercise timeout, disconnect, approval decline, and approval retry."""

    _app()
    cycles = _cycles()
    pool = QThreadPool()
    pool.setMaxThreadCount(4)
    calls = 0

    class Request:
        def __init__(self, mode: int) -> None:
            self.mode = mode
            self.attempts = 0

    class Result:
        def __init__(self, status: str) -> None:
            self.status = status

    def tester(_role, request: Request, _cancel_event) -> Result:
        nonlocal calls
        calls += 1
        request.attempts += 1
        if request.mode == 0:
            raise TimeoutError("injected SSH timeout")
        if request.mode == 1:
            raise ConnectionResetError("injected SSH disconnect")
        if request.attempts == 1:
            return Result("approval_required")
        return Result("success")

    coordinator = PollCoordinator(
        lambda: None,
        thread_pool=pool,
        connection_tester=tester,
    )
    finished = QSignalSpy(coordinator.connection_test_finished)
    failed = QSignalSpy(coordinator.connection_test_failed)
    busy_changed = QSignalSpy(coordinator.busy_changed)

    for index in range(cycles):
        request = Request(index % 4)
        expected_events = finished.count() + failed.count() + 1
        coordinator.test_connection("all", request)
        assert _wait_until(
            lambda: finished.count() + failed.count() == expected_events
            and not coordinator.busy
            and not coordinator._workers
        )

        if request.mode in {0, 1}:
            assert request.attempts == 1
        else:
            assert coordinator._connection_test_settings["all"] is request
            if request.mode == 2:
                coordinator.discard_connection_test("all")
            else:
                expected_finished = finished.count() + 1
                coordinator.retry_connection_test("all")
                assert _wait_until(
                    lambda: finished.count() == expected_finished
                    and not coordinator.busy
                    and not coordinator._workers
                )
                assert request.attempts == 2

        assert coordinator._connection_test_settings == {}
        assert coordinator._connection_workers == {}
        assert coordinator._workers == set()
        assert not coordinator.busy

    retry_count = sum(index % 4 == 3 for index in range(cycles))
    assert calls == cycles + retry_count
    assert busy_changed.count() == calls * 2
    assert pool.waitForDone(1000)
    assert pool.activeThreadCount() == 0
    assert coordinator.shutdown()


@pytest.mark.reliability
def test_connect_failure_soak_closes_every_registered_socket() -> None:
    """Cancel, timeout, and connect errors must always close the raw socket."""

    class PendingSocket:
        def __init__(self, result: int) -> None:
            self.result = result
            self.shutdown_calls = 0
            self.close_calls = 0

        def setblocking(self, _value: bool) -> None:
            return None

        def settimeout(self, _value: float) -> None:
            return None

        def connect_ex(self, _address: tuple[str, int]) -> int:
            return self.result

        def getsockopt(self, *_args: object) -> int:
            return 0

        def shutdown(self, _how: int) -> None:
            self.shutdown_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    closed = 0
    for index in range(_cycles()):
        mode = index % 4
        cancel_event = threading.Event()
        result = errno.EINPROGRESS if mode != 2 else errno.ECONNREFUSED
        pending = PendingSocket(result)
        registered: list[PendingSocket] = []

        if mode == 0:
            cancel_event.set()

        def select_fn(*_args):
            if mode == 1:
                cancel_event.set()
            return [], [], []

        clock_values = iter((0.0, 1.0))
        clock = clock_values.__next__ if mode == 3 else (lambda: 0.0)
        expected_error = {
            0: SocketConnectCancelledError,
            1: SocketConnectCancelledError,
            2: OSError,
            3: TimeoutError,
        }[mode]
        with pytest.raises(expected_error):
            open_cancellable_ipv4_socket(
                "192.0.2.10",
                22,
                0.1,
                cancel_event,
                socket_factory=lambda *_args: pending,
                select_fn=select_fn,
                clock=clock,
                register_socket=registered.append,
            )

        assert registered == [pending]
        assert pending.shutdown_calls == 1
        assert pending.close_calls == 1
        closed += pending.close_calls

    assert closed == _cycles()


@pytest.mark.reliability
def test_settings_transaction_soak_recovers_commit_rollback_and_crash(
    tmp_path: Path,
) -> None:
    """Repeated real fsync/replace transactions must never leave a partial file."""

    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    expected = AppSettings.default()
    store.save(expected)
    transaction_cycles = max(30, _cycles() // 3)

    for index in range(transaction_cycles):
        candidate = copy.deepcopy(expected)
        candidate.polling.interval_seconds = 10 + ((index * 37) % 3591)
        candidate.ui.opacity_percent = 40 + (index % 61)
        update = store.begin_update(candidate)
        mode = index % 3
        if mode == 0:
            update.commit()
            expected = candidate
        elif mode == 1:
            update.rollback()
        else:
            # Simulate process loss after the candidate reached disk but before
            # the cross-layer commit marker was removed.
            store = SettingsStore(path)

        store = SettingsStore(path)
        assert store.load() == expected
        assert not path.with_name(f".{path.name}.update-pending").exists()
        assert not path.with_name(f".{path.name}.rollback").exists()


@pytest.mark.reliability
def test_malformed_output_fault_soak_is_bounded_and_never_escapes() -> None:
    """Feed deterministic terminal noise and malformed table data to every parser."""

    rng = random.Random(0xA4BA7240)
    parsers = (
        parse_show_switches,
        parse_load_distribution,
        parse_group_membership,
    )
    alphabet = " abcXYZ0123456789.-_=:#>\t\r\n\x00\x08\x1b[]가나다"

    for index in range(_cycles() * 3):
        mode = index % 7
        if mode == 0:
            payload: str | bytes | None = None
        elif mode == 1:
            payload = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 256)))
        elif mode == 2:
            payload = "\x1b]unterminated" * rng.randrange(1, 40)
        elif mode == 3:
            payload = "9" * 9_000
        elif mode == 4:
            payload = "password: must-not-survive\r\n" + "-- MORE --\x08" * 20
        elif mode == 5:
            payload = (
                "Switch IP  Active Clients  Standby Clients  Connection-Type\n"
                + "\n".join(
                    f"999.999.{rng.randrange(999)}.{rng.randrange(999)}  -1  NaN  ?"
                    for _ in range(12)
                )
            )
        else:
            payload = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 2048)))

        for parser in parsers:
            result = parser(payload)
            assert len(result.output_excerpt) <= 2_048
            assert all(len(issue.snippet) <= 240 for issue in result.issues)
