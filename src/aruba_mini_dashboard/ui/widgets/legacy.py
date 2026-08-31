from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTabWidget,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import (
    blend_colors as _blend_colors,
    contrast_ratio as _contrast_ratio,
    ensure_minimum_contrast as _ensure_minimum_contrast,
)


CLICK_TO_ENABLE_WHEEL_TOOLTIP = (
    "항목을 클릭한 후에만 마우스 휠로 변경할 수 있습니다."
)


def bounded_window_geometry(
    preferred: QRect,
    available: QRect,
    *,
    minimum_size: QSize = QSize(320, 240),
    margin: int = 16,
) -> QRect:
    """Return a usable logical-pixel window rectangle within one screen.

    Windows can restore a geometry saved on a larger or disconnected monitor.
    Qt also keeps an explicitly requested dialog size even when DPI scaling
    makes it larger than the current screen.  Keep both size and position
    inside the screen's *available* geometry so the title bar and action
    buttons remain reachable without relying on a particular monitor layout.
    """

    if available.isEmpty():
        return QRect(preferred)
    safe_margin = max(0, int(margin))
    horizontal_margin = min(safe_margin, max(0, (available.width() - 1) // 2))
    vertical_margin = min(safe_margin, max(0, (available.height() - 1) // 2))
    usable = available.adjusted(
        horizontal_margin,
        vertical_margin,
        -horizontal_margin,
        -vertical_margin,
    )
    if usable.isEmpty():
        usable = QRect(available)

    minimum_width = min(max(1, minimum_size.width()), usable.width())
    minimum_height = min(max(1, minimum_size.height()), usable.height())
    width = min(max(preferred.width(), minimum_width), usable.width())
    height = min(max(preferred.height(), minimum_height), usable.height())
    maximum_x = usable.left() + usable.width() - width
    maximum_y = usable.top() + usable.height() - height
    x = min(max(preferred.x(), usable.left()), maximum_x)
    y = min(max(preferred.y(), usable.top()), maximum_y)
    return QRect(QPoint(x, y), QSize(width, height))


def available_screen_geometry(
    widget: QWidget,
    preferred: QRect | None = None,
) -> QRect:
    """Choose the best available monitor for a window or restored rectangle."""

    screens = QApplication.screens()
    if preferred is not None and not preferred.isEmpty() and screens:
        intersections = []
        for screen in screens:
            intersection = screen.availableGeometry().intersected(preferred)
            area = (
                0
                if intersection.isEmpty()
                else intersection.width() * intersection.height()
            )
            intersections.append((area, screen))
        area, screen = max(intersections, key=lambda item: item[0])
        if area > 0:
            return screen.availableGeometry()

    parent = widget.parentWidget()
    if parent is not None:
        parent_screen = parent.screen()
        if parent_screen is not None:
            return parent_screen.availableGeometry()
    screen = widget.screen() or QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else QRect(0, 0, 800, 600)


def fit_window_to_available_screen(
    widget: QWidget,
    preferred_size: QSize,
    *,
    preferred_position: QPoint | None = None,
    minimum_size: QSize = QSize(320, 240),
    margin: int = 16,
    center_on_parent: bool = False,
) -> QRect:
    """Apply a DPI- and multi-monitor-safe initial/restored window geometry."""

    parent = widget.parentWidget()
    provisional = QRect(preferred_position or widget.pos(), preferred_size)
    available = available_screen_geometry(widget, provisional if preferred_position else None)
    if preferred_position is None and center_on_parent:
        center = (
            parent.frameGeometry().center()
            if parent is not None and parent.isVisible()
            else available.center()
        )
        provisional.moveCenter(center)
    bounded = bounded_window_geometry(
        provisional,
        available,
        minimum_size=minimum_size,
        margin=margin,
    )
    # An explicit minimum larger than a high-DPI screen prevents resize() from
    # honoring the bounded rectangle. Cap it to the space actually available.
    widget.setMinimumSize(
        min(minimum_size.width(), bounded.width()),
        min(minimum_size.height(), bounded.height()),
    )
    widget.setGeometry(bounded)
    return bounded


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


class _SubtleSelectionItemDelegate(QStyledItemDelegate):
    """Paint a neutral selection while retaining each item's foreground and icon."""

    def __init__(self, table: "SubtleSelectionTableWidget") -> None:
        super().__init__(table)
        self._table = table

    @staticmethod
    def _foreground_brush(
        option: QStyleOptionViewItem,
        index: Any,
        group: QPalette.ColorGroup,
        fallback: QColor,
    ) -> QBrush:
        foreground = index.data(Qt.ForegroundRole)
        if isinstance(foreground, QBrush) and foreground.style() != Qt.NoBrush:
            return QBrush(foreground)
        if isinstance(foreground, QColor) and foreground.isValid():
            return QBrush(foreground)
        # The semantic fallback was contrast-corrected against the neutral
        # selection fill. The source palette's Text brush may be the same
        # low-contrast value that required correction.
        return QBrush(fallback)

    def _selection_option(
        self,
        option: QStyleOptionViewItem,
        index: Any,
    ) -> tuple[QStyleOptionViewItem, QColor, bool]:
        styled = QStyleOptionViewItem(option)
        active = bool(option.state & QStyle.State_Active)
        group = QPalette.Active if active else QPalette.Inactive
        prefix = "active" if active else "inactive"
        colors = self._table.selection_style_colors
        background = QColor(colors[f"{prefix}_background"])
        fallback_text = QColor(colors[f"{prefix}_text"])
        boundary = QColor(colors[f"{prefix}_boundary"])
        palette = QPalette(styled.palette)
        palette.setBrush(group, QPalette.Highlight, QBrush(background))
        palette.setBrush(
            group,
            QPalette.HighlightedText,
            self._foreground_brush(styled, index, group, fallback_text),
        )
        palette.setCurrentColorGroup(group)
        styled.palette = palette
        had_focus = bool(styled.state & QStyle.State_HasFocus)
        # Native focus painting can reintroduce the Windows accent color. Draw
        # an explicit neutral focus boundary after the normal item instead.
        styled.state &= ~QStyle.State_HasFocus
        return styled, boundary, had_focus

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: Any,
    ) -> None:
        if not option.state & QStyle.State_Selected:
            super().paint(painter, option, index)
            return

        styled, boundary, had_focus = self._selection_option(option, index)
        super().paint(painter, styled, index)

        painter.save()
        try:
            rect = option.rect.adjusted(0, 0, -1, -1)
            painter.setPen(QPen(boundary, 1))
            painter.drawLine(rect.topLeft(), rect.topRight())
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
            if had_focus:
                painter.setPen(QPen(boundary, 1, Qt.DotLine))
                painter.drawRect(rect)
        finally:
            painter.restore()


class SubtleSelectionTableWidget(QTableWidget):
    """Palette-aware table with a light neutral row-selection treatment."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        rows: int = 0,
        columns: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(rows, columns, parent)
        self._selection_style_refreshing = False
        self._selection_style_revision = 0
        self._selection_style_colors: dict[str, str] = {}
        self.refresh_selection_style()
        self._subtle_selection_delegate = _SubtleSelectionItemDelegate(self)
        self.setItemDelegate(self._subtle_selection_delegate)

    @property
    def selection_style_revision(self) -> int:
        """Expose refreshes for deterministic palette/style regression tests."""

        return self._selection_style_revision

    @property
    def selection_style_colors(self) -> dict[str, str]:
        """Return semantic active and inactive selection colors."""

        return dict(self._selection_style_colors)

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if (
            event.type() in self._STYLE_CHANGE_EVENTS
            and hasattr(self, "_selection_style_refreshing")
        ):
            self.refresh_selection_style()

    def refresh_selection_style(self) -> None:
        """Rebuild neutral selection colors from the active Windows/Qt palette."""

        if self._selection_style_refreshing:
            return
        self._selection_style_refreshing = True
        try:
            palette = self.palette()
            colors: dict[str, str] = {}
            group_specs = (
                (QPalette.Active, "active", 0.12, 0.55),
                (QPalette.Inactive, "inactive", 0.08, 0.50),
            )
            for group, prefix, fill_weight, boundary_weight in group_specs:
                base = palette.color(group, QPalette.Base)
                text = palette.color(group, QPalette.Text)
                fallback_text = palette.color(group, QPalette.WindowText)
                background = _blend_colors(base, text, fill_weight)
                readable_text = _ensure_minimum_contrast(
                    text,
                    background,
                    fallback_text,
                    minimum=4.5,
                )
                boundary = _blend_colors(base, readable_text, boundary_weight)
                boundary = _ensure_minimum_contrast(
                    boundary,
                    background,
                    readable_text,
                    minimum=3.0,
                )
                colors[f"{prefix}_background"] = background.name()
                colors[f"{prefix}_text"] = readable_text.name()
                colors[f"{prefix}_boundary"] = boundary.name()

            self._selection_style_colors = colors
            self._selection_style_revision += 1
            self.viewport().update()
        finally:
            self._selection_style_refreshing = False


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
