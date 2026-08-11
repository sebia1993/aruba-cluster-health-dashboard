from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot


LOGGER = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    succeeded = Signal(object, object)
    failed = Signal(object, object)


class _PollWorker(QRunnable):
    def __init__(
        self,
        collect_cycle: Callable[..., Any],
        cancellation_event: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._collect_cycle = collect_cycle
        self._cancellation_event = cancellation_event
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            signature = inspect.signature(self._collect_cycle)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            accepts_variadic = any(
                parameter.kind is parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            )
            if positional or accepts_variadic:
                result = self._collect_cycle(self._cancellation_event)
            else:
                result = self._collect_cycle()
        except BaseException as exc:  # keep an unexpected collector error out of Qt
            self.signals.failed.emit(self, exc)
            return
        self.signals.succeeded.emit(self, result)


class PollCoordinator(QObject):
    """Schedule non-overlapping polling work outside the GUI thread.

    Automatic ticks that arrive during a manual poll are skipped. Repeated manual
    requests made while busy are coalesced into one pending poll.
    """

    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()
    connection_test_started = Signal(str)
    connection_test_finished = Signal(str, object)
    connection_test_failed = Signal(str, object)
    automatic_start_rejected = Signal(str)

    MIN_INTERVAL_SECONDS = 10
    MAX_INTERVAL_SECONDS = 3600

    def __init__(
        self,
        collect_cycle: Callable[..., Any],
        interval_seconds: int = 60,
        *,
        thread_pool: QThreadPool | None = None,
        clock: Callable[[], datetime] | None = None,
        connection_tester: Callable[..., Any] | None = None,
        host_key_approver: Callable[[Any], Any] | None = None,
        start_guard: Callable[[], tuple[bool, str]] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._collect_cycle = collect_cycle
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._clock = clock or datetime.now
        self._connection_tester = connection_tester
        self._host_key_approver = host_key_approver
        self._start_guard = start_guard
        self._interval_seconds = self._validated_interval(interval_seconds)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_automatic_timeout)
        self._automatic = False
        self._busy = False
        self._manual_pending = False
        self._next_check: datetime | None = None
        self._cancel_event = threading.Event()
        self._workers: set[_PollWorker] = set()
        self._connection_workers: dict[_PollWorker, str] = {}
        self._connection_test_settings: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def automatic(self) -> bool:
        return self._automatic

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def next_check(self) -> datetime | None:
        return self._next_check

    def set_interval(self, seconds: int) -> None:
        self._interval_seconds = self._validated_interval(seconds)
        if self._automatic:
            self._schedule_next()

    @classmethod
    def _validated_interval(cls, seconds: int) -> int:
        seconds = int(seconds)
        if not cls.MIN_INTERVAL_SECONDS <= seconds <= cls.MAX_INTERVAL_SECONDS:
            raise ValueError("점검 주기는 10초에서 3,600초 사이여야 합니다.")
        return seconds

    @Slot()
    def start_automatic(self) -> None:
        if self._automatic:
            return
        if self._start_guard is not None:
            try:
                allowed, reason = self._start_guard()
            except Exception as exc:
                allowed, reason = False, str(exc) or "자동 점검 준비 상태를 확인하지 못했습니다."
            if not allowed:
                self.automatic_changed.emit(False)
                self.automatic_start_rejected.emit(reason)
                return
        self._automatic = True
        self.automatic_changed.emit(True)
        self._schedule_next()

    @Slot()
    def pause_automatic(self) -> None:
        if not self._automatic and not self._timer.isActive():
            return
        self._automatic = False
        self._timer.stop()
        self._set_next_check(None)
        self.automatic_changed.emit(False)

    @Slot()
    def check_now(self) -> None:
        if self._busy:
            if not self._manual_pending:
                self._manual_pending = True
                self.manual_poll_queued.emit()
            return
        self._start_cycle("manual")

    def test_connection(self, role: str, settings: Any) -> None:
        """Run host-key discovery and authentication without touching baselines."""

        if self._connection_tester is None:
            self.connection_test_failed.emit(role, RuntimeError("연결 테스트 기능을 사용할 수 없습니다."))
            return
        if self._busy:
            self.connection_test_failed.emit(role, RuntimeError("현재 점검이 끝난 뒤 다시 시도하세요."))
            return
        self._busy = True
        self.busy_changed.emit(True)
        self.connection_test_started.emit(role)
        self._cancel_event = threading.Event()

        def run_test(cancel_event: threading.Event) -> Any:
            return self._connection_tester(role, settings, cancel_event)

        worker = _PollWorker(run_test, self._cancel_event)
        self._workers.add(worker)
        self._connection_workers[worker] = role
        self._connection_test_settings[role] = settings
        worker.signals.succeeded.connect(self._on_connection_test_succeeded)
        worker.signals.failed.connect(self._on_connection_test_failed)
        self._thread_pool.start(worker)

    def approve_host_key(self, scanned: Any) -> Any:
        if self._host_key_approver is None:
            raise RuntimeError("SSH 호스트 키 승인 기능을 사용할 수 없습니다.")
        return self._host_key_approver(scanned)

    def retry_connection_test(self, role: str) -> None:
        settings = self._connection_test_settings.get(role)
        if settings is None:
            self.connection_test_failed.emit(role, RuntimeError("다시 시도할 연결 설정이 없습니다."))
            return
        self.test_connection(role, settings)

    @Slot()
    def _on_automatic_timeout(self) -> None:
        self._set_next_check(None)
        if not self._automatic:
            return
        if self._busy:
            message = "이전 점검이 진행 중이어서 예약 점검을 건너뛰었습니다."
            LOGGER.info("POLL_SKIPPED_BUSY: %s", message)
            self.scheduled_poll_skipped.emit(message)
            self._schedule_next()
            return
        self._start_cycle("automatic")

    def _start_cycle(self, trigger: str) -> None:
        self._busy = True
        self.busy_changed.emit(True)
        started_at = self._clock()
        self.cycle_started.emit(trigger, started_at)
        self._cancel_event = threading.Event()
        worker = _PollWorker(self._collect_cycle, self._cancel_event)
        self._workers.add(worker)
        worker.signals.succeeded.connect(self._on_worker_succeeded)
        worker.signals.failed.connect(self._on_worker_failed)
        self._thread_pool.start(worker)

    @Slot(object, object)
    def _on_worker_succeeded(self, worker: _PollWorker, result: Any) -> None:
        self._finish_cycle(worker, result, None)

    @Slot(object, object)
    def _on_worker_failed(self, worker: _PollWorker, error: BaseException) -> None:
        self._finish_cycle(worker, None, error)

    @Slot(object, object)
    def _on_connection_test_succeeded(self, worker: _PollWorker, result: Any) -> None:
        role = self._connection_workers.pop(worker, "unknown")
        self._workers.discard(worker)
        self._busy = False
        self.busy_changed.emit(False)
        if getattr(result, "status", "") != "approval_required":
            self._connection_test_settings.pop(role, None)
        self.connection_test_finished.emit(role, result)
        if self._automatic and not self._timer.isActive():
            self._schedule_next()

    @Slot(object, object)
    def _on_connection_test_failed(self, worker: _PollWorker, error: BaseException) -> None:
        role = self._connection_workers.pop(worker, "unknown")
        self._workers.discard(worker)
        self._busy = False
        self.busy_changed.emit(False)
        self._connection_test_settings.pop(role, None)
        self.connection_test_failed.emit(role, error)
        if self._automatic and not self._timer.isActive():
            self._schedule_next()

    @Slot()
    def _finish_cycle(
        self,
        worker: _PollWorker,
        result: Any,
        error: BaseException | None,
    ) -> None:
        self._workers.discard(worker)
        self._busy = False
        self.busy_changed.emit(False)
        if error is None:
            self.cycle_finished.emit(result)
        else:
            LOGGER.exception(
                "Unhandled poll cycle failure", exc_info=(type(error), error, error.__traceback__)
            )
            self.cycle_failed.emit(error)

        if self._manual_pending and not self._cancel_event.is_set():
            self._manual_pending = False
            self._start_cycle("manual-pending")
            return
        if self._automatic and not self._timer.isActive():
            self._schedule_next()

    def _schedule_next(self) -> None:
        if not self._automatic:
            return
        self._timer.start(self._interval_seconds * 1000)
        self._set_next_check(self._clock() + timedelta(seconds=self._interval_seconds))

    def _set_next_check(self, value: datetime | None) -> None:
        self._next_check = value
        self.next_check_changed.emit(value)

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """Stop new work, request cancellation, and wait briefly for workers."""

        self.request_shutdown()
        return self._thread_pool.waitForDone(max(0, int(timeout_ms)))

    def request_shutdown(self) -> None:
        """Cancel future/current work without blocking the GUI thread."""

        self.pause_automatic()
        self._manual_pending = False
        self._cancel_event.set()
