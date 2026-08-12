from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QComboBox, QSlider, QSpinBox


CLICK_TO_ENABLE_WHEEL_TOOLTIP = (
    "항목을 클릭한 후에만 마우스 휠로 변경할 수 있습니다."
)


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
