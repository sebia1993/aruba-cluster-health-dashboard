"""Bounded rotating logs with centralized sensitive-value redaction."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from .config import AppPaths, default_app_paths


MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


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
    redactor: SecretRedactor
    app_log: Path
    ssh_debug_log: Path
    _formatter: logging.Formatter

    def register_secret(self, value: str) -> None:
        self.redactor.add(value)

    def set_ssh_debug_enabled(self, enabled: bool) -> None:
        """Enable or disable the sensitive diagnostic log at runtime.

        Existing rotated files are deliberately retained; disabling only stops
        future writes. Every handler continues to use the shared redactor.
        """

        self.ssh_logger.setLevel(logging.DEBUG if enabled else logging.CRITICAL + 1)
        handlers = (
            [_rotating_handler(self.ssh_debug_log, self._formatter)] if enabled else []
        )
        _replace_handlers(self.ssh_logger, handlers)


def setup_logging(
    paths: AppPaths | None = None,
    *,
    ssh_debug_enabled: bool = False,
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
    _replace_handlers(logger, [_rotating_handler(paths.app_log, formatter)])

    ssh_logger = logging.getLogger("aruba_mini_dashboard.ssh_debug")
    ssh_logger.setLevel(logging.DEBUG if ssh_debug_enabled else logging.CRITICAL + 1)
    ssh_logger.propagate = False
    _replace_handlers(ssh_logger, [_rotating_handler(paths.ssh_debug_log, formatter)] if ssh_debug_enabled else [])

    return LoggingContext(
        logger=logger,
        ssh_logger=ssh_logger,
        redactor=redactor,
        app_log=paths.app_log,
        ssh_debug_log=paths.ssh_debug_log,
        _formatter=formatter,
    )


configure_logging = setup_logging


def _rotating_handler(path: Path, formatter: logging.Formatter) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
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
