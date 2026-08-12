from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QVBoxLayout,
    QWidget,
)

from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog
from aruba_mini_dashboard.ui.widgets import (
    CLICK_TO_ENABLE_WHEEL_TOOLTIP,
    ClickArmedComboBox,
    ClickArmedSpinBox,
    NoWheelSlider,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wheel(widget: QWidget, delta: int = 120) -> None:
    top_level = widget.window()
    window_handle = top_level.windowHandle()
    assert window_handle is not None
    position = widget.mapTo(top_level, widget.rect().center())
    QTest.wheelEvent(window_handle, position, QPoint(0, delta))
    QApplication.processEvents()


def _hosted(*widgets: QWidget) -> QWidget:
    host = QWidget()
    layout = QVBoxLayout(host)
    for widget in widgets:
        layout.addWidget(widget)
    host.show()
    QApplication.processEvents()
    return host


def test_every_numeric_setting_and_comparison_mode_uses_click_armed_input() -> None:
    _app()
    dialog = SettingsDialog(AppSettings.default())
    spin_boxes = dialog.findChildren(QSpinBox)
    combo_boxes = dialog.findChildren(QComboBox)

    assert len(spin_boxes) == 17
    assert all(isinstance(widget, ClickArmedSpinBox) for widget in spin_boxes)
    assert set(combo_boxes) == {dialog.primary_ip, dialog.comparison_mode}
    assert isinstance(dialog.primary_ip, ClickArmedComboBox)
    assert isinstance(dialog.comparison_mode, ClickArmedComboBox)
    assert all(CLICK_TO_ENABLE_WHEEL_TOOLTIP in widget.toolTip() for widget in spin_boxes)
    assert CLICK_TO_ENABLE_WHEEL_TOOLTIP in dialog.primary_ip.toolTip()
    assert CLICK_TO_ENABLE_WHEEL_TOOLTIP in dialog.comparison_mode.toolTip()
    dialog.close()


def test_spinbox_wheel_requires_click_and_disarms_on_focus_out() -> None:
    _app()
    spin = ClickArmedSpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    other = QLineEdit()
    host = _hosted(spin, other)

    _wheel(spin)
    assert spin.value() == 5
    assert not spin.wheel_armed

    other.setFocus(Qt.OtherFocusReason)
    spin.setFocus(Qt.OtherFocusReason)
    QApplication.processEvents()
    _wheel(spin)
    assert spin.value() == 5
    assert not spin.wheel_armed

    spin.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    _wheel(spin)
    assert spin.value() == 5
    assert not spin.wheel_armed

    QTest.mouseClick(spin.lineEdit(), Qt.LeftButton)
    QApplication.processEvents()
    assert spin.wheel_armed
    _wheel(spin)
    assert spin.value() == 6

    other.setFocus(Qt.OtherFocusReason)
    QApplication.processEvents()
    assert not spin.wheel_armed
    _wheel(spin)
    assert spin.value() == 6
    host.close()


def test_spinbox_buttons_and_keyboard_keep_their_normal_behavior() -> None:
    _app()
    spin = ClickArmedSpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    other = QLineEdit()
    host = _hosted(spin, other)

    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    up_button = spin.style().subControlRect(
        QStyle.CC_SpinBox,
        option,
        QStyle.SC_SpinBoxUp,
        spin,
    )
    QTest.mouseClick(spin, Qt.LeftButton, pos=up_button.center())
    QApplication.processEvents()
    assert spin.value() == 6
    assert spin.wheel_armed

    other.setFocus(Qt.OtherFocusReason)
    spin.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    assert not spin.wheel_armed
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "7")
    QTest.keyClick(spin.lineEdit(), Qt.Key_Return)
    assert spin.value() == 7
    QTest.keyClick(spin, Qt.Key_Up)
    assert spin.value() == 8
    assert not spin.wheel_armed
    host.close()


def test_combobox_wheel_requires_click_and_popup_does_not_cancel_arming() -> None:
    _app()
    combo = ClickArmedComboBox()
    combo.addItems(["절대값", "상대 비교", "사용 안 함"])
    combo.setCurrentIndex(1)
    other = QLineEdit()
    host = _hosted(combo, other)

    _wheel(combo)
    assert combo.currentIndex() == 1

    combo.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    _wheel(combo)
    assert combo.currentIndex() == 1
    assert not combo.wheel_armed

    QTest.mouseClick(combo, Qt.LeftButton)
    QApplication.processEvents()
    assert combo.view().isVisible()
    assert combo.wheel_armed
    combo.hidePopup()
    QApplication.processEvents()
    assert combo.wheel_armed
    _wheel(combo)
    assert combo.currentIndex() == 0

    other.setFocus(Qt.OtherFocusReason)
    QApplication.processEvents()
    assert not combo.wheel_armed
    _wheel(combo, -120)
    assert combo.currentIndex() == 0
    host.close()


def test_blocked_spinbox_wheel_scrolls_the_parent_page() -> None:
    _app()
    area = QScrollArea()
    area.resize(300, 150)
    contents = QWidget()
    layout = QVBoxLayout(contents)
    spin = ClickArmedSpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    layout.addWidget(spin)
    for index in range(30):
        layout.addWidget(QLabel(f"설정 설명 {index}"))
    area.setWidget(contents)
    area.setWidgetResizable(True)
    area.show()
    QApplication.processEvents()

    spin.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    assert area.verticalScrollBar().value() == 0
    _wheel(spin, -120)

    assert spin.value() == 5
    assert area.verticalScrollBar().value() > 0
    area.close()


def test_opacity_slider_remains_wheel_disabled_after_click() -> None:
    _app()
    slider = NoWheelSlider(Qt.Horizontal)
    slider.setRange(40, 100)
    slider.setValue(70)
    host = _hosted(slider)

    QTest.mouseClick(slider, Qt.LeftButton)
    QApplication.processEvents()
    value_after_click = slider.value()
    _wheel(slider)
    assert slider.value() == value_after_click
    host.close()
