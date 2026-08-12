"""Bounded rotating logs with centralized sensitive-value redaction."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from .config import AppPaths, default_app_paths


MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOW_SPEC_MAX_LOG_BYTES = 2 * 1024 * 1024
LOW_SPEC_LOG_BACKUP_COUNT = 2
PERFORMANCE_MAX_LOG_BYTES = 1 * 1024 * 1024
PERFORMANCE_LOG_BACKUP_COUNT = 2


class SecretRedactor:
    def __init__(self, values: Iterable[str] = ()) -> None:
        self._lock = threading.RLock()
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str) -> None:
        candidate = str(value)
        if candidate:
            with self._lock:
                self._values.add(candidate)

    def remove(self, value: str) -> None:
        with self._lock:
            self._values.discard(str(value))

    def redact(self, text: object) -> str:
        result = str(text)
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            result = result.replace(value, "[REDACTED]")
        # Defense in depth for accidental key/value logging.  Runtime code
        # must still register actual secrets because free-form exception text
        # cannot be recognized reliably from field names alone.
        result = re.sub(
            r"(?i)(password|passwd|enable[_ -]?secret|credentialblob|token)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            result,
        )
        result = re.sub(
            r'(?i)([\"\'](?:password|passwd|enable_secret|token)[\"\']\s*:\s*)[\"\'][^\"\']*[\"\']',
            r'\1"[REDACTED]"',
            result,
        )
        return result


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args: object, redactor: SecretRedactor, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))


@dataclass(slots=True)
class LoggingContext:
    logger: logging.Logger
    ssh_logger: logging.Logger
    performance_logger: logging.Logger
    redactor: SecretRedactor
    app_log: Path
    ssh_debug_log: Path
    performance_log: Path
    _formatter: logging.Formatter
    _ssh_debug_enabled: bool
    _performance_logging_enabled: bool
    _low_spec_mode: bool

    def register_secret(self, value: str) -> None:
        self.redactor.add(value)

    def set_ssh_debug_enabled(self, enabled: bool) -> None:
        """Enable or disable the sensitive diagnostic log at runtime.

        Existing rotated files are deliberately retained; disabling only stops
        future writes. Every handler continues to use the shared redactor.
        """

        self._ssh_debug_enabled = bool(enabled)
        self.ssh_logger.setLevel(logging.DEBUG if enabled else logging.CRITICAL + 1)
        handlers = (
            [
                _rotating_handler(
                    self.ssh_debug_log,
                    self._formatter,
                    max_bytes=self._log_size,
                    backup_count=self._backup_count,
                )
            ]
            if enabled
            else []
        )
        _replace_handlers(self.ssh_logger, handlers)

    @property
    def _log_size(self) -> int:
        return LOW_SPEC_MAX_LOG_BYTES if self._low_spec_mode else MAX_LOG_BYTES

    @property
    def _backup_count(self) -> int:
        return LOW_SPEC_LOG_BACKUP_COUNT if self._low_spec_mode else LOG_BACKUP_COUNT

    @property
    def performance_logging_enabled(self) -> bool:
        """Whether aggregate timing/counter collection should run."""

        return self._performance_logging_enabled

    def set_performance_logging_enabled(self, enabled: bool) -> None:
        """Toggle the sanitized aggregate performance log without a restart."""

        self._performance_logging_enabled = bool(enabled)
        self.performance_logger.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)
        handlers = (
            [
                _rotating_handler(
                    self.performance_log,
                    self._formatter,
                    max_bytes=PERFORMANCE_MAX_LOG_BYTES,
                    backup_count=PERFORMANCE_LOG_BACKUP_COUNT,
                )
            ]
            if enabled
            else []
        )
        _replace_handlers(self.performance_logger, handlers)

    def set_low_spec_mode(self, enabled: bool) -> None:
        """Apply bounded log limits selected for the current resource mode."""

        enabled = bool(enabled)
        if enabled == self._low_spec_mode:
            return
        self._low_spec_mode = enabled
        _replace_handlers(
            self.logger,
            [
                _rotating_handler(
                    self.app_log,
                    self._formatter,
                    max_bytes=self._log_size,
                    backup_count=self._backup_count,
                )
            ],
        )
        self.set_ssh_debug_enabled(self._ssh_debug_enabled)


def setup_logging(
    paths: AppPaths | None = None,
    *,
    ssh_debug_enabled: bool = False,
    low_spec_mode: bool = False,
    performance_logging_enabled: bool = False,
    redaction_values: Iterable[str] = (),
) -> LoggingContext:
    paths = (paths or default_app_paths()).ensure()
    redactor = SecretRedactor(redaction_values)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        redactor=redactor,
    )

    logger = logging.getLogger("aruba_mini_dashboard")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_size = LOW_SPEC_MAX_LOG_BYTES if low_spec_mode else MAX_LOG_BYTES
    backup_count = LOW_SPEC_LOG_BACKUP_COUNT if low_spec_mode else LOG_BACKUP_COUNT
    _replace_handlers(
        logger,
        [
            _rotating_handler(
                paths.app_log,
                formatter,
                max_bytes=log_size,
                backup_count=backup_count,
            )
        ],
    )

    ssh_logger = logging.getLogger("aruba_mini_dashboard.ssh_debug")
    ssh_logger.setLevel(logging.DEBUG if ssh_debug_enabled else logging.CRITICAL + 1)
    ssh_logger.propagate = False
    _replace_handlers(
        ssh_logger,
        [
            _rotating_handler(
                paths.ssh_debug_log,
                formatter,
                max_bytes=log_size,
                backup_count=backup_count,
            )
        ]
        if ssh_debug_enabled
        else [],
    )

    performance_logger = logging.getLogger("aruba_mini_dashboard.performance")
    performance_logger.setLevel(
        logging.INFO if performance_logging_enabled else logging.CRITICAL + 1
    )
    performance_logger.propagate = False
    _replace_handlers(
        performance_logger,
        [
            _rotating_handler(
                paths.performance_log,
                formatter,
                max_bytes=PERFORMANCE_MAX_LOG_BYTES,
                backup_count=PERFORMANCE_LOG_BACKUP_COUNT,
            )
        ]
        if performance_logging_enabled
        else [],
    )

    return LoggingContext(
        logger=logger,
        ssh_logger=ssh_logger,
        performance_logger=performance_logger,
        redactor=redactor,
        app_log=paths.app_log,
        ssh_debug_log=paths.ssh_debug_log,
        performance_log=paths.performance_log,
        _formatter=formatter,
        _ssh_debug_enabled=bool(ssh_debug_enabled),
        _performance_logging_enabled=bool(performance_logging_enabled),
        _low_spec_mode=bool(low_spec_mode),
    )


configure_logging = setup_logging


def _rotating_handler(
    path: Path,
    formatter: logging.Formatter,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(formatter)
    return handler


def _replace_handlers(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    for existing in logger.handlers[:]:
        logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            logging.getLogger(__name__).debug("Log handler close failed", exc_info=True)
    for handler in handlers:
        logger.addHandler(handler)


def current_process_metrics() -> dict[str, int]:
    """Return aggregate process counters without adding a profiler dependency."""

    metrics = {"python_threads": threading.active_count()}
    if os.name != "nt":
        return metrics
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            metrics["working_set_bytes"] = int(counters.WorkingSetSize)
            metrics["private_bytes"] = int(counters.PrivateUsage)
        handle_count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(process, ctypes.byref(handle_count)):
            metrics["handles"] = int(handle_count.value)
    except (AttributeError, OSError, ValueError):
        # Performance diagnostics must never affect monitoring availability.
        logging.getLogger(__name__).debug("Windows process metrics are unavailable", exc_info=True)
    return metrics
