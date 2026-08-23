from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from aruba_mini_dashboard.config import AppSettings, settings_fingerprint
from aruba_mini_dashboard.main import build_parser
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.developer_inspector import (
    DeveloperInspectorBar,
    DeveloperInspectorController,
)
from aruba_mini_dashboard.ui.main_window import MainWindow
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog


class _Coordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.automatic = False

    def check_now(self) -> None:
        return None

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, _seconds: int) -> None:
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _ids(controller: DeveloperInspectorController) -> set[str]:
    return {metadata.stable_id for metadata in controller.catalog}


def _close_window(window: MainWindow) -> None:
    window._quitting = True
    window.tray_icon.hide()
    window.close()


def test_no_cli_or_settings_activation_path_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.default()
    baseline = settings_fingerprint(settings)
    monkeypatch.setenv("ARUBA_UI_INSPECTOR", "1")

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--ui-inspector"])

    assert exc_info.value.code == 2
    serialized = repr(settings.to_dict()).lower()
    assert "inspector" not in serialized
    assert "developer" not in serialized
    assert settings_fingerprint(settings) == baseline


def test_main_window_registers_static_surfaces_and_f12_controls_shared_bar() -> None:
    app = _app()
    controller = DeveloperInspectorController(app, "v0.3.6")
    window = MainWindow(
        _Coordinator(),
        AppSettings.default(),
        developer_inspector=controller,
    )
    window.show()
    app.processEvents()

    try:
        bar = window.findChild(DeveloperInspectorBar)
        assert bar is not None
        assert controller.enabled is False
        assert bar.isVisible() is False
        assert window.property("uiInspectorId") == "MAIN-WINDOW"
        assert window.table.viewport().property("uiInspectorId") == (
            "MAIN-FULL-DEVICE-TABLE-BODY"
        )
        assert window.compact_table.horizontalHeader().property("uiInspectorId") == (
            "MAIN-COMPACT-DEVICE-TABLE-HEADER"
        )
        assert window.options_menu.property("uiInspectorId") == (
            "MAIN-FULL-OPTIONS-MENU"
        )
        assert window.tray_quit_action.property("uiInspectorId") == "TRAY-QUIT"

        expected = {
            "MAIN-WINDOW",
            "MAIN-STATUS-BAR",
            "MAIN-FULL-VIEW",
            "MAIN-FULL-CHECK-NOW",
            "MAIN-FULL-AUTO-START",
            "MAIN-FULL-AUTO-PAUSE",
            "MAIN-FULL-ACKNOWLEDGE",
            "MAIN-FULL-SETTINGS",
            "MAIN-FULL-PAGING",
            "MAIN-FULL-DEVICE-TABLE",
            "MAIN-FULL-DEVICE-TABLE-BODY",
            "MAIN-FULL-DEVICE-TABLE-HEADER",
            "MAIN-FULL-DEVICE-TABLE-SELECTION",
            "MAIN-COMPACT-VIEW",
            "MAIN-COMPACT-CHECK-NOW",
            "MAIN-COMPACT-AUTO",
            "MAIN-COMPACT-MORE-MENU",
            "MAIN-COMPACT-DEVICE-TABLE",
            "MAIN-COMPACT-DEVICE-TABLE-BODY",
            "MAIN-COMPACT-DEVICE-TABLE-HEADER",
            "MAIN-COMPACT-DEVICE-TABLE-SELECTION",
            "TRAY-ICON",
            "TRAY-MENU",
            "TRAY-OPEN",
            "TRAY-CHECK-NOW",
            "TRAY-AUTO-START",
            "TRAY-AUTO-PAUSE",
            "TRAY-SETTINGS",
            "TRAY-QUIT",
        }
        assert expected <= _ids(controller)

        QTest.keyClick(window, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is True
        assert bar.isVisible() is True
        QTest.keyClick(window, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is False
        assert bar.isVisible() is False
    finally:
        _close_window(window)
        controller.close()
        app.processEvents()


def test_settings_dialog_uses_only_static_metadata_for_sensitive_fields() -> None:
    app = _app()
    controller = DeveloperInspectorController(app, "v0.3.6")
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.210"
    dialog = SettingsDialog(settings, developer_inspector=controller)
    dialog.shared_fields.username.setText("runtime-admin")
    dialog.shared_fields.password.setText("runtime-password")
    dialog.shared_fields.enable_secret.setText("runtime-enable")
    dialog.show()
    app.processEvents()

    try:
        bar = dialog.findChild(DeveloperInspectorBar)
        assert bar is not None
        assert bar.isVisible() is False
        assert dialog.shared_fields.password.property("uiInspectorId") == (
            "SETTINGS-CREDENTIAL-SHARED-PASSWORD"
        )
        assert dialog.buttons.button(QDialogButtonBox.Save).property("uiInspectorId") == (
            "SETTINGS-SAVE"
        )
        expected = {
            "SETTINGS-DIALOG",
            "SETTINGS-TABS",
            "SETTINGS-TAB-DEVICES",
            "SETTINGS-TAB-OPERATIONS",
            "SETTINGS-TAB-NOTIFICATIONS",
            "SETTINGS-MM-IP",
            "SETTINGS-WLC-1-IP",
            "SETTINGS-WLC-4-ALIAS",
            "SETTINGS-PRIMARY-CONTROLLER",
            "SETTINGS-CONNECTION-SECTION",
            "SETTINGS-MM-CONNECT-TIMEOUT",
            "SETTINGS-WLC-COMMAND-TIMEOUT",
            "SETTINGS-CREDENTIAL-SHARED-USERNAME",
            "SETTINGS-CREDENTIAL-SHARED-PASSWORD",
            "SETTINGS-CREDENTIAL-MM-ENABLE-SECRET",
            "SETTINGS-CREDENTIAL-WLC-PASSWORD",
            "SETTINGS-CONNECTION-DIAGNOSTIC-SECTION",
            "SETTINGS-CONNECTION-DIAGNOSTIC",
            "SETTINGS-POLL-INTERVAL",
            "SETTINGS-LOW-SPEC-MODE",
            "SETTINGS-DETECTION-COMPARISON-MODE",
            "SETTINGS-DETECTION-MISSING-CYCLES",
            "SETTINGS-NOTIFY-NEW",
            "SETTINGS-NOTIFY-REPEAT-INTERVAL",
            "SETTINGS-NOTIFY-SSH-DEBUG",
            "SETTINGS-SAVE",
            "SETTINGS-CANCEL",
        }
        assert expected <= _ids(controller)

        fixed_catalog = "\n".join(
            "|".join(
                (
                    metadata.name_ko,
                    metadata.stable_id,
                    metadata.screen_path,
                    metadata.source_path,
                    metadata.purpose,
                    controller.request_text(metadata),
                )
            )
            for metadata in controller.catalog
        )
        assert "192.0.2.210" not in fixed_catalog
        assert "runtime-admin" not in fixed_catalog
        assert "runtime-password" not in fixed_catalog
        assert "runtime-enable" not in fixed_catalog
    finally:
        dialog.close()
        controller.close()
        app.processEvents()


def test_detail_dialog_re_registers_summary_and_lazy_output_widgets() -> None:
    app = _app()
    controller = DeveloperInspectorController(app, "v0.3.6")
    first = {
        "ip": "192.0.2.220",
        "alias": "runtime-controller",
        "status": "정상",
    }
    dialog = DetailDialog(
        first,
        raw_outputs={"show switches": "runtime-raw-output"},
        parsed_results={"show switches": None},
        developer_inspector=controller,
    )
    dialog.show()
    app.processEvents()

    try:
        assert dialog.findChild(DeveloperInspectorBar) is not None
        assert dialog._summary_fields["IP"].property("uiInspectorId") == (
            "DETAIL-SUMMARY-IP"
        )
        assert dialog._parsed_placeholder.property("uiInspectorId") == (
            "DETAIL-TAB-PARSED"
        )

        dialog.tabs.setCurrentIndex(1)
        app.processEvents()
        assert dialog._parsed_editor is not None
        assert dialog._parsed_editor.property("uiInspectorId") == "DETAIL-PARSED-OUTPUT"
        dialog.tabs.setCurrentIndex(2)
        app.processEvents()
        assert dialog._raw_editor is not None
        assert dialog._raw_editor.property("uiInspectorId") == "DETAIL-RAW-OUTPUT"

        dialog.update_snapshot(
            {"ip": "192.0.2.221", "alias": "second-runtime-controller"},
            raw_outputs={"show switches": "second-runtime-output"},
            parsed_results={"show switches": None},
        )
        app.processEvents()
        assert dialog._summary_fields["IP"].property("uiInspectorId") == (
            "DETAIL-SUMMARY-IP"
        )
        assert dialog._raw_editor is not None
        assert dialog._raw_editor.property("uiInspectorId") == "DETAIL-RAW-OUTPUT"
        expected = {
            "DETAIL-DIALOG",
            "DETAIL-TABS",
            "DETAIL-TAB-SUMMARY",
            "DETAIL-TAB-PARSED",
            "DETAIL-TAB-RAW",
            "DETAIL-SUMMARY",
            "DETAIL-SUMMARY-IP",
            "DETAIL-SUMMARY-ALIAS",
            "DETAIL-SUMMARY-STATUS",
            "DETAIL-SUMMARY-MM-STATUS",
            "DETAIL-SUMMARY-CLIENTS",
            "DETAIL-SUMMARY-CONNECTION-TYPE",
            "DETAIL-SUMMARY-ANOMALY-STREAK",
            "DETAIL-SUMMARY-LAST-CHECK",
            "DETAIL-SUMMARY-PREVIOUS",
            "DETAIL-SUMMARY-CONNECTION-CHANGE-TIME",
            "DETAIL-SUMMARY-REASONS",
            "DETAIL-SUMMARY-ERRORS",
            "DETAIL-PARSED-OUTPUT",
            "DETAIL-RAW-OUTPUT",
            "DETAIL-CLOSE",
        }
        assert expected <= _ids(controller)
        fixed_catalog = "\n".join(
            metadata.name_ko + metadata.screen_path + metadata.purpose
            for metadata in controller.catalog
        )
        assert "192.0.2.220" not in fixed_catalog
        assert "runtime-controller" not in fixed_catalog
        assert "runtime-raw-output" not in fixed_catalog
    finally:
        dialog.close()
        controller.close()
        app.processEvents()


def test_existing_constructors_remain_inspector_free_and_compatible() -> None:
    app = _app()
    window = MainWindow(_Coordinator(), AppSettings.default())
    settings_dialog = SettingsDialog(AppSettings.default())
    detail_dialog = DetailDialog({"ip": "192.0.2.230"})
    try:
        assert window.developer_inspector is None
        assert settings_dialog.developer_inspector is None
        assert detail_dialog.developer_inspector is None
        assert window.findChild(DeveloperInspectorBar) is None
        assert settings_dialog.findChild(DeveloperInspectorBar) is None
        assert detail_dialog.findChild(DeveloperInspectorBar) is None
    finally:
        settings_dialog.close()
        detail_dialog.close()
        _close_window(window)
        app.processEvents()


def test_one_controller_drives_bars_in_main_settings_and_detail_windows() -> None:
    app = _app()
    controller = DeveloperInspectorController(app, "v0.3.6")
    window = MainWindow(
        _Coordinator(),
        AppSettings.default(),
        developer_inspector=controller,
    )
    settings_dialog = SettingsDialog(
        AppSettings.default(),
        parent=window,
        developer_inspector=controller,
    )
    detail_dialog = DetailDialog(
        {"ip": "192.0.2.240"},
        parent=window,
        developer_inspector=controller,
    )
    window.show()
    settings_dialog.show()
    detail_dialog.show()
    app.processEvents()

    try:
        bars = [
            window.findChild(DeveloperInspectorBar),
            settings_dialog.findChild(DeveloperInspectorBar),
            detail_dialog.findChild(DeveloperInspectorBar),
        ]
        assert all(bar is not None for bar in bars)
        assert all(not bar.isVisible() for bar in bars if bar is not None)

        QTest.keyClick(settings_dialog, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is True
        assert all(bar.isVisible() for bar in bars if bar is not None)

        QTest.keyClick(detail_dialog, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is False
        assert all(not bar.isVisible() for bar in bars if bar is not None)
    finally:
        settings_dialog.close()
        detail_dialog.close()
        _close_window(window)
        controller.close()
        app.processEvents()


def test_repeated_settings_dialogs_are_deleted_after_cancel() -> None:
    app = _app()
    controller = DeveloperInspectorController(app, "v0.3.6")
    window = MainWindow(
        _Coordinator(),
        AppSettings.default(),
        developer_inspector=controller,
    )
    window.show()
    app.processEvents()

    try:
        for _ in range(12):
            def reject_active_dialog() -> None:
                modal = app.activeModalWidget()
                assert isinstance(modal, SettingsDialog)
                modal.reject()

            QTimer.singleShot(0, reject_active_dialog)
            window.open_settings()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()

        assert window.findChildren(SettingsDialog) == []
        assert window.findChildren(DeveloperInspectorBar) == [
            window.findChild(DeveloperInspectorBar)
        ]
        assert len(controller._bars) <= 2
    finally:
        _close_window(window)
        controller.close()
        app.processEvents()
