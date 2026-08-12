from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon


def resource_path(name: str) -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates = [
            Path(bundled) / "aruba_mini_dashboard" / "ui" / "resources" / name,
            Path(bundled) / "ui" / "resources" / name,
            Path(bundled) / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return Path(__file__).resolve().parent / "resources" / name


@lru_cache(maxsize=4)
def _status_icon_for_filename(filename: str) -> QIcon:
    """Load each immutable status icon once for the lifetime of the UI."""

    return QIcon(str(resource_path(filename)))


def status_icon(status: str) -> QIcon:
    key = str(status).casefold()
    if key in {"normal", "ok", "healthy", "정상"}:
        filename = "status_normal.svg"
    elif key in {"attention", "warning", "degraded", "주의"}:
        filename = "status_attention.svg"
    elif key in {"failure", "critical", "down", "장애"}:
        filename = "status_failure.svg"
    else:
        filename = "status_unknown.svg"
    return _status_icon_for_filename(filename)
