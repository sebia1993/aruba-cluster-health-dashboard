from __future__ import annotations

from pathlib import Path

import pytest

from aruba_mini_dashboard.collectors.ssh_host_keys import HostKeyCheck, ScannedHostKey
from aruba_mini_dashboard.config import AppPaths, AppSettings
from aruba_mini_dashboard.credentials import (
    CredentialService,
    DeviceCredential,
    SessionCredentialStore,
)
from aruba_mini_dashboard.logging_setup import SecretRedactor, setup_logging
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.errors import app_error
from aruba_mini_dashboard.storage import SQLiteStorage
from aruba_mini_dashboard.ui.settings_dialog import ConnectionTestRequest


class _FakeKey:
    def asbytes(self) -> bytes:
        return b"redaction-lifecycle-test-key"


def test_replacing_current_credentials_does_not_retain_history() -> None:
    redactor = SecretRedactor(["fixed-sentinel"])

    for index in range(1_000):
        redactor.replace_current(
            (f"operator-{index}", f"password-{index}", f"enable-{index}")
        )

    assert redactor.tracked_value_count == 4
    assert redactor.redact("password-999") == "[REDACTED]"
    assert redactor.redact("password-0") == "password-0"
    assert redactor.redact("fixed-sentinel") == "[REDACTED]"


def test_active_scope_survives_current_credential_replacement() -> None:
    redactor = SecretRedactor()
    redactor.replace_current(("old-password",))

    with redactor.scoped(("old-password", "connection-test-password")):
        redactor.replace_current(("new-password",))
        assert redactor.redact("old-password") == "[REDACTED]"
        assert redactor.redact("connection-test-password") == "[REDACTED]"
        assert redactor.redact("new-password") == "[REDACTED]"

    assert redactor.tracked_value_count == 1
    assert redactor.redact("old-password") == "old-password"
    assert redactor.redact("connection-test-password") == "connection-test-password"
    assert redactor.redact("new-password") == "[REDACTED]"


def test_transient_scope_is_released_after_failure() -> None:
    redactor = SecretRedactor()

    with pytest.raises(RuntimeError, match="simulated failure"):
        with redactor.scoped(("temporary-password",)):
            assert redactor.redact("temporary-password") == "[REDACTED]"
            raise RuntimeError("simulated failure")

    assert redactor.tracked_value_count == 0
    assert redactor.redact("temporary-password") == "temporary-password"


def test_app_error_repr_does_not_incidentally_expose_technical_detail() -> None:
    sentinel = "technical-secret-must-not-be-shown"
    error = app_error("AUTH_FAILED", sentinel)

    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert sentinel not in repr(error.args)
    assert error.technical_message == sentinel


def test_connection_test_secret_is_scoped_to_the_active_authentication(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    logging_context = setup_logging(paths)
    storage = SQLiteStorage(":memory:")
    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        logging_context,
    )
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    credential = DeviceCredential("typed-user", "typed-password", "typed-enable")
    scanned = ScannedHostKey(
        "192.0.2.1",
        22,
        "ssh-ed25519",
        "SHA256:redaction-test",
        _FakeKey(),
    )

    class _Adapter:
        def __init__(self, _options, received, **_kwargs) -> None:
            assert received is credential

        def connect(self) -> None:
            protected = logging_context.redactor.redact(
                "typed-user typed-password typed-enable"
            )
            assert "typed-user" not in protected
            assert "typed-password" not in protected
            assert "typed-enable" not in protected

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "aruba_mini_dashboard.main.scan_ssh_host_key", lambda *_args, **_kwargs: scanned
    )
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda *_args, **_kwargs: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", _Adapter)

    try:
        result = runtime.test_connection("mm", ConnectionTestRequest(settings, credential))
        assert result.status == "success"
        assert logging_context.redactor.tracked_value_count == 0
        assert logging_context.redactor.redact("typed-password") == "typed-password"
    finally:
        storage.close()
