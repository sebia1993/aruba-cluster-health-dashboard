from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QSlider


class NoWheelSlider(QSlider):
    """A slider that ignores wheel input to prevent accidental opacity edits."""

    wheel_ignored = Signal()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()
        self.wheel_ignored.emit()
