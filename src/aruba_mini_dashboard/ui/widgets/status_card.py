from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..theme import (
    RADIUS,
    SPACING,
    STATUS_LABELS,
    normalize_status_key,
    presentation_palette,
    status_colors,
)
from ._palette_aware import PaletteAwareWidgetMixin
from .status_badge import StatusBadge


class StatusCard(PaletteAwareWidgetMixin, QFrame):
    """Reusable summary card for a status and one concise explanation."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        title: str,
        status: Any = "unknown",
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("reusableStatusCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.md, SPACING.lg, SPACING.md)
        root.setSpacing(SPACING.sm)

        heading = QHBoxLayout()
        self.title_label = QLabel(str(title), self)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        heading.addWidget(self.title_label, 1)
        self.badge = StatusBadge(status, parent=self, accessible_name=str(title))
        heading.addWidget(self.badge, 0, Qt.AlignRight | Qt.AlignVCenter)
        root.addLayout(heading)

        self.detail_label = QLabel(str(detail), self)
        self.detail_label.setTextFormat(Qt.PlainText)
        self.detail_label.setWordWrap(True)
        root.addWidget(self.detail_label)

        self._status_key = normalize_status_key(status)
        self._style_refreshing = False
        self.setAccessibleName(str(title))
        self._refresh_accessibility()
        self._refresh_presentation()
        self._initialize_palette_awareness()

    @property
    def status_key(self) -> str:
        return self._status_key

    def set_title(self, title: str) -> None:
        rendered = str(title)
        self.title_label.setText(rendered)
        self.setAccessibleName(rendered)
        self.badge.set_accessible_prefix(rendered)
        self._refresh_accessibility()

    def set_status(self, status: Any, text: str | None = None) -> None:
        self._status_key = normalize_status_key(status)
        self.badge.set_status(self._status_key, text)
        self._refresh_accessibility()
        self._refresh_presentation()

    def set_detail(self, detail: Any) -> None:
        self.detail_label.setText("" if detail is None else str(detail))
        self._refresh_accessibility()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS and hasattr(self, "_status_key"):
            self._refresh_presentation()

    def _refresh_accessibility(self) -> None:
        description = f"{STATUS_LABELS[self._status_key]} 상태"
        if self.detail_label.text():
            description += f". {self.detail_label.text()}"
        self.setAccessibleDescription(description)

    def _refresh_presentation(self) -> None:
        if self._style_refreshing:
            return
        self._style_refreshing = True
        try:
            colors = status_colors(self._status_key, presentation_palette(self))
            style_sheet = (
                "QFrame#reusableStatusCard {"
                f"background: {colors.background.name()};"
                f"border: 1px solid {colors.accent.name()};"
                f"border-left: 5px solid {colors.accent.name()};"
                f"border-radius: {RADIUS.md}px;"
                "}"
                "QFrame#reusableStatusCard > QLabel {"
                f"color: {colors.foreground.name()};"
                "}"
            )
            if self.styleSheet() != style_sheet:
                self.setStyleSheet(style_sheet)
        finally:
            self._style_refreshing = False
