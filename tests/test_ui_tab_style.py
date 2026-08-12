from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog
from aruba_mini_dashboard.ui.widgets import SubtleTabWidget, _contrast_ratio


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _selected_rule(widget: SubtleTabWidget) -> str:
    return widget.styleSheet().split("QTabBar::tab:selected {", 1)[1].split("}", 1)[0]


def test_settings_and_detail_dialogs_use_subtle_palette_tabs() -> None:
    _app()
    dialogs = (
        SettingsDialog(AppSettings.default()),
        DetailDialog({"ip": "192.0.2.11"}),
    )
    for dialog in dialogs:
        assert isinstance(dialog.tabs, SubtleTabWidget)
        colors = dialog.tabs.tab_style_colors
        selected_rule = _selected_rule(dialog.tabs)
        assert f"background-color: {colors['base']}" in selected_rule
        assert f"border-bottom: 2px solid {colors['accent']}" in selected_rule
        assert f"background-color: {colors['accent']}" not in selected_rule
        assert f"background-color: {colors['focus']}" not in selected_rule
        assert "font-weight: bold" in selected_rule
        assert _contrast_ratio(QColor(colors["accent"]), QColor(colors["base"])) >= 3.0
        dialog.close()


def test_settings_tabs_support_mouse_and_keyboard_navigation() -> None:
    app = _app()
    dialog = SettingsDialog(AppSettings.default())
    dialog.show()
    app.processEvents()
    tab_bar = dialog.tabs.tabBar()

    QTest.mouseClick(tab_bar, Qt.LeftButton, pos=tab_bar.tabRect(1).center())
    assert dialog.tabs.currentIndex() == 1

    tab_bar.setFocus(Qt.TabFocusReason)
    QTest.keyClick(tab_bar, Qt.Key_Right)
    assert dialog.tabs.currentIndex() == 2
    assert tab_bar.hasFocus()
    dialog.close()


def test_detail_tabs_keep_lazy_materialization_during_mouse_and_keyboard_navigation() -> None:
    app = _app()
    dialog = DetailDialog(
        {"ip": "192.0.2.11"},
        parsed_results={"show switches": {"rows": []}},
        raw_outputs={"show switches": "sample"},
    )
    dialog.show()
    app.processEvents()
    tab_bar = dialog.tabs.tabBar()
    assert dialog._parsed_editor is None
    assert dialog._raw_editor is None

    QTest.mouseClick(tab_bar, Qt.LeftButton, pos=tab_bar.tabRect(1).center())
    assert dialog.tabs.currentIndex() == 1
    assert dialog._parsed_editor is not None
    assert dialog._raw_editor is None
    parsed_editor = dialog._parsed_editor

    tab_bar.setFocus(Qt.TabFocusReason)
    QTest.keyClick(tab_bar, Qt.Key_Right)
    assert dialog.tabs.currentIndex() == 2
    assert dialog._raw_editor is not None
    assert dialog._parsed_editor is parsed_editor
    dialog.close()


def test_tab_style_refreshes_for_palette_and_style_changes() -> None:
    app = _app()
    tabs = SubtleTabWidget()
    initial_revision = tabs.tab_style_revision
    palette = QPalette(tabs.palette())
    palette.setColor(QPalette.Active, QPalette.Base, QColor("#f4f1ea"))

    tabs.setPalette(palette)
    app.processEvents()
    assert tabs.tab_style_revision > initial_revision
    assert tabs.tab_style_colors["base"] == "#f4f1ea"
    assert "background-color: #f4f1ea" in _selected_rule(tabs)

    palette_revision = tabs.tab_style_revision
    QApplication.sendEvent(tabs, QEvent(QEvent.StyleChange))
    assert tabs.tab_style_revision > palette_revision
    tabs.close()


def test_low_contrast_palette_is_adjusted_without_reintroducing_a_fill() -> None:
    app = _app()
    tabs = SubtleTabWidget()
    palette = QPalette(tabs.palette())
    palette.setColor(QPalette.Active, QPalette.Base, QColor("#f8f8f8"))
    palette.setColor(QPalette.Active, QPalette.Highlight, QColor("#eeeeee"))
    palette.setColor(QPalette.Active, QPalette.Mid, QColor("#e8e8e8"))
    palette.setColor(QPalette.Active, QPalette.WindowText, QColor("#202020"))

    tabs.setPalette(palette)
    app.processEvents()
    colors = tabs.tab_style_colors
    selected_rule = _selected_rule(tabs)
    assert _contrast_ratio(QColor(colors["accent"]), QColor(colors["base"])) >= 3.0
    assert f"background-color: {colors['base']}" in selected_rule
    assert f"background-color: {colors['accent']}" not in selected_rule
    tabs.close()
