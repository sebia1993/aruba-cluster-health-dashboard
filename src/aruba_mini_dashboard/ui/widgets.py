from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


CLICK_TO_ENABLE_WHEEL_TOOLTIP = (
    "항목을 클릭한 후에만 마우스 휠로 변경할 수 있습니다."
)


def _blend_colors(first: QColor, second: QColor, second_weight: float) -> QColor:
    """Return a stable palette-derived color without introducing a theme dependency."""

    first_weight = 1.0 - second_weight
    return QColor(
        round(first.red() * first_weight + second.red() * second_weight),
        round(first.green() * first_weight + second.green() * second_weight),
        round(first.blue() * first_weight + second.blue() * second_weight),
    )


def _relative_luminance(color: QColor) -> float:
    """Return WCAG relative luminance for an opaque sRGB color."""

    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * linear(color.red())
        + 0.7152 * linear(color.green())
        + 0.0722 * linear(color.blue())
    )


def _contrast_ratio(first: QColor, second: QColor) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _ensure_minimum_contrast(
    foreground: QColor,
    background: QColor,
    fallback: QColor,
    minimum: float = 3.0,
) -> QColor:
    """Move a palette color toward readable text until it reaches the target."""

    if _contrast_ratio(foreground, background) >= minimum:
        return foreground
    if _contrast_ratio(fallback, background) < minimum:
        black = QColor("#000000")
        white = QColor("#ffffff")
        fallback = max((black, white), key=lambda color: _contrast_ratio(color, background))

    best = QColor(fallback)
    low, high = 0.0, 1.0
    for _ in range(16):
        weight = (low + high) / 2.0
        candidate = _blend_colors(foreground, fallback, weight)
        if _contrast_ratio(candidate, background) >= minimum:
            best = candidate
            high = weight
        else:
            low = weight
    return best


class SubtleTabWidget(QTabWidget):
    """Palette-aware tabs with a compact selection line instead of a blue fill."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tab_style_refreshing = False
        self._tab_style_revision = 0
        self._tab_style_colors: dict[str, str] = {}
        self.refresh_tab_style()

    @property
    def tab_style_revision(self) -> int:
        """Expose refreshes for deterministic palette/style regression tests."""

        return self._tab_style_revision

    @property
    def tab_style_colors(self) -> dict[str, str]:
        """Return the current semantic colors used by the local tab style."""

        return dict(self._tab_style_colors)

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if (
            event.type() in self._STYLE_CHANGE_EVENTS
            and hasattr(self, "_tab_style_refreshing")
        ):
            self.refresh_tab_style()

    def refresh_tab_style(self) -> None:
        """Rebuild the local stylesheet from the active Windows/Qt palette."""

        if self._tab_style_refreshing:
            return
        self._tab_style_refreshing = True
        try:
            palette = self.palette()
            base = palette.color(QPalette.Active, QPalette.Base)
            window = palette.color(QPalette.Active, QPalette.Window)
            text = palette.color(QPalette.Active, QPalette.WindowText)
            disabled_text = palette.color(QPalette.Disabled, QPalette.WindowText)
            border = palette.color(QPalette.Active, QPalette.Mid)
            highlight = palette.color(QPalette.Active, QPalette.Highlight)
            hover = _blend_colors(window, text, 0.08)
            accent = _blend_colors(highlight, border, 0.35)
            accent = _ensure_minimum_contrast(accent, base, text)
            focus = _ensure_minimum_contrast(highlight, base, text)
            colors = {
                "base": base.name(),
                "window": window.name(),
                "text": text.name(),
                "disabled_text": disabled_text.name(),
                "border": border.name(),
                "hover": hover.name(),
                "accent": accent.name(),
                "focus": focus.name(),
            }
            style_sheet = f"""
QTabWidget::pane {{
    background-color: {colors['base']};
    border: 1px solid {colors['border']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {colors['window']};
    color: {colors['text']};
    border: 1px solid transparent;
    border-bottom: 1px solid {colors['border']};
    padding: 7px 14px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {colors['base']};
    color: {colors['text']};
    font-weight: bold;
    border: 1px solid {colors['border']};
    border-bottom: 2px solid {colors['accent']};
}}
QTabBar::tab:!selected:hover {{
    background-color: {colors['hover']};
}}
QTabBar::tab:disabled {{
    color: {colors['disabled_text']};
}}
QTabBar::tab:selected:focus {{
    background-color: {colors['base']};
    border: 1px dotted {colors['focus']};
    border-bottom: 2px solid {colors['accent']};
}}
""".strip()
            self._tab_style_colors = colors
            if self.styleSheet() != style_sheet:
                self.setStyleSheet(style_sheet)
            self._tab_style_revision += 1
        finally:
            self._tab_style_refreshing = False


class _ClickArmedWheelMixin:
    """Allow wheel edits only after a direct click, until focus is lost.

    Ignoring the wheel event lets Qt offer it to a parent scroll area, so a
    settings page keeps scrolling when the pointer happens to be over an input.
    """

    _wheel_armed: bool

    def _initialize_click_armed_wheel(self) -> None:
        self._wheel_armed = False
        self.setToolTip(CLICK_TO_ENABLE_WHEEL_TOOLTIP)

    @property
    def wheel_armed(self) -> bool:
        """Expose the interaction state for accessibility and focused tests."""

        return self._wheel_armed

    def _arm_wheel_from_mouse(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self._wheel_armed = True

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._should_disarm_for_focus_out(event):
            self._wheel_armed = False
        super().focusOutEvent(event)

    def _should_disarm_for_focus_out(self, event: Any) -> bool:
        return True

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self._arm_wheel_from_mouse(event)
        super().mousePressEvent(event)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if not self._wheel_armed:
            event.ignore()
            return
        super().wheelEvent(event)


class ClickArmedSpinBox(_ClickArmedWheelMixin, QSpinBox):
    """Spin box whose wheel editing requires an explicit mouse click."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._initialize_click_armed_wheel()
        # Text-area clicks are delivered to QAbstractSpinBox's private line
        # edit, while +/- clicks are delivered to the spin box itself.
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        if watched is self.lineEdit() and event.type() == QEvent.MouseButtonPress:
            self._arm_wheel_from_mouse(event)
        return super().eventFilter(watched, event)


class ClickArmedComboBox(_ClickArmedWheelMixin, QComboBox):
    """Combo box whose wheel selection requires an explicit mouse click."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._initialize_click_armed_wheel()

    def _should_disarm_for_focus_out(self, event: Any) -> bool:
        # Opening QComboBox's popup temporarily produces PopupFocusReason and
        # OtherFocusReason events. That popup is part of the same explicit
        # click interaction, not a move to another settings field. Other focus
        # reasons still disarm it even while the popup is visible (for example,
        # switching to another window).
        popup_transition = event.reason() in {
            Qt.PopupFocusReason,
            Qt.OtherFocusReason,
        }
        return not (self.view().isVisible() and popup_transition)


class NoWheelSlider(QSlider):
    """A slider that ignores wheel input to prevent accidental opacity edits."""

    wheel_ignored = Signal()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()
        self.wheel_ignored.emit()


class CollapsibleSection(QWidget):
    """Small native disclosure section used for infrequently changed settings."""

    def __init__(
        self,
        title: str,
        content: QWidget,
        parent: QWidget | None = None,
        *,
        expanded: bool = False,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle.setAccessibleName(title)
        self.content = content
        self.content.setVisible(expanded)
        self.toggle.toggled.connect(self._set_expanded)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content.setVisible(expanded)
