from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from ..resources import status_icon
from ..theme import (
    RADIUS,
    SIZES,
    SPACING,
    STATUS_LABELS,
    normalize_status_key,
    presentation_palette,
    status_colors,
)
from ._palette_aware import PaletteAwareWidgetMixin


class StatusBadge(PaletteAwareWidgetMixin, QFrame):
    """Accessible status indicator combining local icon, text, and color."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        status: Any = "unknown",
        text: str | None = None,
        parent: QWidget | None = None,
        *,
        accessible_name: str = "상태",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("semanticStatusBadge")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING.sm, SPACING.xs, SPACING.sm, SPACING.xs)
        layout.setSpacing(SPACING.xs)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(SIZES.icon_sm, SIZES.icon_sm)
        self.icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon_label)

        self.text_label = QLabel(self)
        self.text_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.text_label)

        self._accessible_prefix = accessible_name.strip() or "상태"
        self._status_key = "unknown"
        self._explicit_text: str | None = None
        self._semantic_colors: dict[str, str] = {}
        self._style_refreshing = False
        self.set_status(status, text)
        self._initialize_palette_awareness()

    @property
    def status_key(self) -> str:
        return self._status_key

    @property
    def status_text(self) -> str:
        return self.text_label.text()

    @property
    def semantic_colors(self) -> dict[str, str]:
        return dict(self._semantic_colors)

    def set_status(self, status: Any, text: str | None = None) -> None:
        self._status_key = normalize_status_key(status)
        self._explicit_text = None if text is None else str(text)
        rendered = self._explicit_text or STATUS_LABELS[self._status_key]
        self.text_label.setText(rendered)
        self.setAccessibleName(f"{self._accessible_prefix}: {rendered}")
        self.setAccessibleDescription(
            f"{STATUS_LABELS[self._status_key]} 상태를 "
            "아이콘과 텍스트로 표시합니다."
        )
        self.icon_label.setAccessibleName(f"{STATUS_LABELS[self._status_key]} 상태 아이콘")
        self._refresh_presentation()

    def set_accessible_prefix(self, accessible_name: str) -> None:
        self._accessible_prefix = accessible_name.strip() or "상태"
        self.setAccessibleName(f"{self._accessible_prefix}: {self.status_text}")

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS and hasattr(self, "_status_key"):
            self._refresh_presentation()

    def _refresh_presentation(self) -> None:
        if self._style_refreshing:
            return
        self._style_refreshing = True
        try:
            colors = status_colors(self._status_key, presentation_palette(self))
            self._semantic_colors = {
                "foreground": colors.foreground.name(),
                "background": colors.background.name(),
                "accent": colors.accent.name(),
            }
            style_sheet = (
                "QFrame#semanticStatusBadge {"
                f"background: {colors.background.name()};"
                f"border: 1px solid {colors.accent.name()};"
                f"border-radius: {RADIUS.pill}px;"
                "}"
                "QFrame#semanticStatusBadge QLabel {"
                f"color: {colors.foreground.name()};"
                "font-weight: 600;"
                "}"
            )
            if self.styleSheet() != style_sheet:
                self.setStyleSheet(style_sheet)
            icon_size = QSize(SIZES.icon_sm, SIZES.icon_sm)
            self.icon_label.setPixmap(status_icon(self._status_key).pixmap(icon_size))
        finally:
            self._style_refreshing = False
