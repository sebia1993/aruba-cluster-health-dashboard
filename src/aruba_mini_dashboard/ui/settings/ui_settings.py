from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

from ..widgets import ClickArmedSpinBox


class SettingsPresentationMixin:
    """Shared widget helpers for the existing three-tab settings dialog.

    ``UiSettings`` values such as opacity and window geometry deliberately stay
    on the main dashboard.  This module is a presentation-helper boundary, not
    a fourth settings tab.
    """

    @staticmethod
    def _spin(
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "",
    ) -> ClickArmedSpinBox:
        widget = ClickArmedSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _describe(widget: QWidget, name: str, description: str) -> QWidget:
        wheel_note = widget.toolTip()
        tooltip = description + (f"\n\n{wheel_note}" if wheel_note else "")
        widget.setToolTip(tooltip)
        widget.setStatusTip(description)
        widget.setAccessibleName(name)
        widget.setAccessibleDescription(description)
        return widget

    @classmethod
    def _add_row(
        cls,
        form: QFormLayout,
        label_text: str,
        widget: QWidget,
        description: str,
    ) -> None:
        cls._describe(widget, label_text, description)
        label = QLabel(label_text)
        label.setBuddy(widget)
        label.setToolTip(description)
        form.addRow(label, widget)
