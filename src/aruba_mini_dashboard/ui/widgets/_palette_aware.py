from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent
from PySide6.QtGui import QGuiApplication


class PaletteAwareWidgetMixin:
    """Refresh semantic presentation for app and ancestor palette changes.

    A Qt style sheet gives its widget an explicit resolved palette, which can
    stop normal palette inheritance notifications. Listening at the native
    ancestor and application boundaries keeps semantic widgets synchronized
    with OS theme changes without requiring page-specific wiring.
    """

    _PALETTE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def _initialize_palette_awareness(self) -> None:
        current = self.parentWidget()
        while current is not None:
            current.installEventFilter(self)
            current = current.parentWidget()
        application = QGuiApplication.instance()
        if application is not None:
            application.paletteChanged.connect(self._application_palette_changed)

    def _application_palette_changed(self, _palette: Any) -> None:
        self._refresh_presentation()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        if event.type() in self._PALETTE_EVENTS:
            self._refresh_presentation()
        return super().eventFilter(watched, event)

    def _refresh_presentation(self) -> None:
        raise NotImplementedError
