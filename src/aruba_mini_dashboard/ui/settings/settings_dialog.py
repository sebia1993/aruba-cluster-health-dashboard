"""New namespace for the existing settings-dialog public API.

The implementation remains in the legacy module so existing imports,
monkeypatch targets, and frozen-package entry points continue to work.
"""

from ..settings_dialog import ConnectionTestRequest, CredentialOverride, SettingsDialog

__all__ = ["ConnectionTestRequest", "CredentialOverride", "SettingsDialog"]
