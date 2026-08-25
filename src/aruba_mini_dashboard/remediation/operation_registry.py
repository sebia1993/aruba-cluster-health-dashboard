"""One cancellation authority for every remediation SSH transport."""

from __future__ import annotations

import threading
from typing import Any


class OperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: set[Any] = set()

    def register(self, adapter: Any) -> Any:
        with self._lock:
            self._adapters.add(adapter)
        return adapter

    def unregister(self, adapter: Any) -> None:
        with self._lock:
            self._adapters.discard(adapter)

    def abort_all(self) -> None:
        with self._lock:
            adapters = tuple(self._adapters)
        for adapter in adapters:
            abort = getattr(adapter, "abort", None)
            if callable(abort):
                try:
                    abort()
                except Exception:
                    pass

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._adapters)
