from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ..theme import RADIUS, SIZES, SPACING, presentation_palette, semantic_palette
from ._palette_aware import PaletteAwareWidgetMixin
from .sparkline import SparklineWidget


class MetricCard(PaletteAwareWidgetMixin, QFrame):
    """Compact KPI card with an optional in-memory sparkline."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        title: str,
        value: Any = "-",
        subtitle: str = "",
        parent: QWidget | None = None,
        *,
        samples: Iterable[Any] | None = None,
        show_sparkline: bool = False,
        status: Any = "normal",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setMinimumWidth(SIZES.card_minimum_width)
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.md, SPACING.lg, SPACING.md)
        root.setSpacing(SPACING.xs)

        self.title_label = QLabel(str(title), self)
        self.title_label.setTextFormat(Qt.PlainText)
        root.addWidget(self.title_label)

        self.value_label = QLabel(self)
        self.value_label.setTextFormat(Qt.PlainText)
        value_font = self.value_label.font()
        value_font.setBold(True)
        value_font.setPointSize(max(value_font.pointSize() + 6, 15))
        self.value_label.setFont(value_font)
        root.addWidget(self.value_label)

        self.subtitle_label = QLabel(str(subtitle), self)
        self.subtitle_label.setTextFormat(Qt.PlainText)
        self.subtitle_label.setWordWrap(True)
        root.addWidget(self.subtitle_label)

        self.sparkline = SparklineWidget(
            samples,
            self,
            status=status,
            accessible_name=f"{title} 최근 추세",
        )
        self.sparkline.setVisible(show_sparkline or samples is not None)
        root.addWidget(self.sparkline)

        self._style_refreshing = False
        self.setAccessibleName(str(title))
        self.set_value(value)
        self._refresh_presentation()
        self._initialize_palette_awareness()

    @property
    def value(self) -> str:
        return self.value_label.text()

    def set_value(self, value: Any) -> None:
        rendered = "-" if value is None or value == "" else str(value)
        self.value_label.setText(rendered)
        self._refresh_accessibility()

    def set_subtitle(self, subtitle: Any) -> None:
        self.subtitle_label.setText("" if subtitle is None else str(subtitle))
        self._refresh_accessibility()

    def set_samples(self, samples: Iterable[Any] | None) -> None:
        self.sparkline.set_samples(samples)
        self.sparkline.setVisible(samples is not None)

    def append_sample(self, sample: Any) -> None:
        self.sparkline.append_sample(sample)
        self.sparkline.show()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS and hasattr(self, "title_label"):
            self._refresh_presentation()

    def _refresh_accessibility(self) -> None:
        description = f"현재 값 {self.value_label.text()}"
        if self.subtitle_label.text():
            description += f". {self.subtitle_label.text()}"
        self.setAccessibleDescription(description)

    def _refresh_presentation(self) -> None:
        if self._style_refreshing:
            return
        self._style_refreshing = True
        try:
            colors = semantic_palette(presentation_palette(self))
            style_sheet = (
                "QFrame#metricCard {"
                f"background: {colors.surface.name()};"
                f"border: 1px solid {colors.border.name()};"
                f"border-radius: {RADIUS.md}px;"
                "}"
            )
            if self.styleSheet() != style_sheet:
                self.setStyleSheet(style_sheet)
            title_style = f"color: {colors.text_secondary.name()};"
            value_style = f"color: {colors.text_primary.name()};"
            if self.title_label.styleSheet() != title_style:
                self.title_label.setStyleSheet(title_style)
            if self.value_label.styleSheet() != value_style:
                self.value_label.setStyleSheet(value_style)
            if self.subtitle_label.styleSheet() != title_style:
                self.subtitle_label.setStyleSheet(title_style)
        finally:
            self._style_refreshing = False
