"""Presentation modules used by the settings dialog.

The authoritative dialog lifecycle remains available from
``aruba_mini_dashboard.ui.settings_dialog``.  These modules only construct the
three existing settings pages and intentionally do not own validation,
credential persistence, or cross-layer commit/rollback behavior.
"""

from .device_settings import DeviceSettingsPresentationMixin
from .monitoring_settings import MonitoringSettingsPresentationMixin
from .notification_settings import NotificationSettingsPresentationMixin
from .ui_settings import SettingsPresentationMixin

__all__ = [
    "DeviceSettingsPresentationMixin",
    "MonitoringSettingsPresentationMixin",
    "NotificationSettingsPresentationMixin",
    "SettingsPresentationMixin",
]
