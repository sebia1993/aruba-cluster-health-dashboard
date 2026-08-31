from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..resources import status_icon
from ..theme import (
    SIZES,
    SPACING,
    normalize_status_key,
    presentation_palette,
    semantic_palette,
)
from ._palette_aware import PaletteAwareWidgetMixin


class EmptyState(PaletteAwareWidgetMixin, QWidget):
    """Small icon-and-text placeholder for empty dashboard regions."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        title: str = "표시할 항목이 없습니다",
        description: str = "",
        parent: QWidget | None = None,
        *,
        status: Any = "unknown",
    ) -> None:
        super().__init__(parent)
        self._status_key = normalize_status_key(status)
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.xl, SPACING.lg, SPACING.xl)
        root.setSpacing(SPACING.sm)
        root.setAlignment(Qt.AlignCenter)

        self.icon_label = QLabel(self)
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setPixmap(
            status_icon(self._status_key).pixmap(QSize(SIZES.icon_md, SIZES.icon_md))
        )
        root.addWidget(self.icon_label)

        self.title_label = QLabel(str(title), self)
        self.title_label.setAlignment(Qt.AlignCenter)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        root.addWidget(self.title_label)

        self.description_label = QLabel(str(description), self)
        self.description_label.setAlignment(Qt.AlignCenter)
        self.description_label.setWordWrap(True)
        root.addWidget(self.description_label)

        self.setAccessibleName(str(title))
        self.setAccessibleDescription(str(description))
        self._refresh_presentation()
        self._initialize_palette_awareness()

    def set_content(self, title: Any, description: Any = "") -> None:
        rendered_title = str(title)
        rendered_description = "" if description is None else str(description)
        self.title_label.setText(rendered_title)
        self.description_label.setText(rendered_description)
        self.setAccessibleName(rendered_title)
        self.setAccessibleDescription(rendered_description)

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS and hasattr(self, "title_label"):
            self._refresh_presentation()

    def _refresh_presentation(self) -> None:
        colors = semantic_palette(presentation_palette(self))
        self.title_label.setStyleSheet(f"color: {colors.text_primary.name()};")
        self.description_label.setStyleSheet(f"color: {colors.text_secondary.name()};")
