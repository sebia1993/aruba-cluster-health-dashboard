from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.ui.settings.settings_dialog import (
    SettingsDialog as NamespacedSettingsDialog,
)
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_split_settings_pages_preserve_the_legacy_dialog_contract() -> None:
    _app()
    dialog = SettingsDialog(AppSettings.default())

    assert NamespacedSettingsDialog is SettingsDialog
    assert [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())] == [
        "장비·자격 증명",
        "운영",
        "알림",
    ]
    assert dialog._build_devices_tab.__module__.endswith("settings.device_settings")
    assert dialog._build_polling_tab.__module__.endswith("settings.monitoring_settings")
    assert dialog._build_notifications_tab.__module__.endswith(
        "settings.notification_settings"
    )
    for public_control in (
        "mm_ip",
        "member_ips",
        "primary_ip",
        "shared_fields",
        "poll_interval",
        "low_spec_mode",
        "notify_new",
        "sound_test_button",
    ):
        assert hasattr(dialog, public_control)

    dialog.close()
