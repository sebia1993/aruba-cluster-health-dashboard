from __future__ import annotations

from pathlib import Path

import pytest

from aruba_mini_dashboard.collectors.base import SshOperationError
from aruba_mini_dashboard.collectors.ssh_host_keys import (
    HostKeyCheck,
    ScannedHostKey,
    SshHostKeyError,
    SshHostKeyMismatchError,
    register_scanned_host_keys,
)
from aruba_mini_dashboard.config import AppPaths, AppSettings
from aruba_mini_dashboard.credentials import CredentialService, DeviceCredential, SessionCredentialStore
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.storage import SQLiteStorage
from aruba_mini_dashboard.ui.settings_dialog import ConnectionTestRequest, CredentialOverride


class FakeKey:
    def asbytes(self) -> bytes:
        return b"sanitized-demo-key"


def _runtime(tmp_path: Path) -> RuntimePoller:
    paths = AppPaths.from_environment(tmp_path).ensure()
    return RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        SQLiteStorage(":memory:"),
        setup_logging(paths),
    )


def test_first_host_key_requires_approval_before_credentials(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    scanned = ScannedHostKey("192.0.2.1", 22, "ssh-ed25519", "SHA256:demo", FakeKey())
    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", lambda *args, **kwargs: scanned)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda *args, **kwargs: HostKeyCheck("unregistered", scanned),
    )
    result = runtime.test_connection("mm", settings)
    assert result.status == "approval_required"
    assert result.fingerprint == "SHA256:demo"
    assert result.scanned is scanned


def test_changed_host_key_is_fail_closed(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    scanned = ScannedHostKey("192.0.2.1", 22, "ssh-ed25519", "SHA256:new", FakeKey())
    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", lambda *args, **kwargs: scanned)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda *args, **kwargs: HostKeyCheck("mismatch", scanned, ("SHA256:old",)),
    )
    result = runtime.test_connection("mm", settings)
    assert result.status == "mismatch"
    assert result.expected_fingerprints == ("SHA256:old",)


def test_cluster_connection_test_onboards_every_fallback_before_credentials(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12", "192.0.2.13"]
    scans: list[str] = []

    def scan(host, port, **_kwargs):
        scans.append(host)
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    def check(scanned, _path):
        return HostKeyCheck("verified" if scanned.host == "192.0.2.11" else "unregistered", scanned)

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr("aruba_mini_dashboard.main.check_scanned_host_key", check)
    result = runtime.test_connection("cluster", settings)
    assert result.status == "approval_required"
    assert result.host == "192.0.2.12"
    assert scans == ["192.0.2.11", "192.0.2.12"]


def test_unreachable_primary_does_not_block_fallback_fingerprint_approval(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    scans: list[str] = []
    fallback = ScannedHostKey(
        "192.0.2.12",
        22,
        "ssh-ed25519",
        "SHA256:fallback",
        FakeKey(),
    )

    def scan(host, _port, **_kwargs):
        scans.append(host)
        if host == "192.0.2.11":
            raise SshHostKeyError("primary unreachable")
        return fallback

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("unregistered", scanned),
    )

    result = runtime.test_connection("cluster", settings)

    assert result.status == "approval_required"
    assert result.host == "192.0.2.12"
    assert result.scanned is fallback
    assert scans == ["192.0.2.11", "192.0.2.12"]


def test_typed_session_credential_is_used_only_after_pre_auth_key_check(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    credential = DeviceCredential("typed-user", "typed-password")
    events: list[str] = []
    scanned = ScannedHostKey("192.0.2.1", 22, "ssh-ed25519", "SHA256:demo", FakeKey())

    def scan(*_args, **_kwargs):
        events.append("scan")
        return scanned

    class Adapter:
        def __init__(self, _options, received, **_kwargs) -> None:
            assert received is credential
            events.append("credential-used")

        def connect(self) -> None:
            events.append("auth")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda *_args, **_kwargs: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)
    result = runtime.test_connection("mm", ConnectionTestRequest(settings, credential))
    assert result.status == "success"
    assert events == ["scan", "credential-used", "auth", "close"]


def test_cluster_authentication_host_key_change_is_not_hidden_by_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    credential = DeviceCredential("typed-user", "typed-password")
    attempts: list[str] = []

    def scan(host, port, **_kwargs):
        return ScannedHostKey(
            host,
            port,
            "ssh-ed25519",
            f"SHA256:{host}",
            FakeKey(),
        )

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            attempts.append(self.host)
            if self.host == "192.0.2.11":
                raise SshOperationError(
                    "SSH_HOST_KEY_MISMATCH",
                    "SSH server key changed after discovery.",
                    retryable=False,
                    operation="connect",
                )

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    with pytest.raises(SshOperationError) as exc_info:
        runtime.test_connection(
            "cluster",
            ConnectionTestRequest(settings, credential),
        )

    assert exc_info.value.code == "SSH_HOST_KEY_MISMATCH"
    assert attempts == ["192.0.2.11"]


def test_cluster_connection_success_reports_the_authenticated_fallback_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    credential = DeviceCredential("typed-user", "typed-password")

    def scan(host, port, **_kwargs):
        algorithm = "ssh-rsa" if host == "192.0.2.11" else "ssh-ed25519"
        return ScannedHostKey(host, port, algorithm, f"SHA256:{host}", FakeKey())

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            if self.host == "192.0.2.11":
                raise SshOperationError(
                    "AUTH_FAILED",
                    "Authentication failed.",
                    retryable=False,
                    operation="connect",
                )

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    result = runtime.test_connection(
        "cluster",
        ConnectionTestRequest(settings, credential),
    )

    assert result.status == "success"
    assert result.host == "192.0.2.12"
    assert result.fingerprint == "SHA256:192.0.2.12"
    assert result.algorithm == "ssh-ed25519"


def test_integrated_onboarding_scans_every_unique_endpoint_before_approval_or_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12", "192.0.2.11"]
    events: list[str] = []

    def scan(host, port, **_kwargs):
        events.append(f"scan:{host}")
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("unregistered", scanned),
    )
    monkeypatch.setattr(
        runtime.credential_service,
        "get",
        lambda _credential_id: pytest.fail("credential store read before approval"),
    )

    request = ConnectionTestRequest(
        settings,
        credential_overrides={
            "mm": CredentialOverride("operator", "password"),
            "cluster": CredentialOverride("operator", "password"),
        },
        purpose="save",
    )
    result = runtime.test_connection("all", request)

    assert result.status == "approval_required"
    assert result.purpose == "save"
    assert [item.host for item in result.scanned] == [
        "192.0.2.1",
        "192.0.2.11",
        "192.0.2.12",
    ]
    assert events == [
        "scan:192.0.2.1",
        "scan:192.0.2.11",
        "scan:192.0.2.12",
    ]
    assert [item.status for item in result.details] == [
        "approval_required",
        "approval_required",
        "approval_required",
    ]


def test_integrated_onboarding_reads_credentials_only_after_every_identity_is_verified(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    credential_id = runtime.credential_service.save(
        DeviceCredential("stored-user", "stored-password"),
        session_only=False,
    )
    settings = AppSettings.default()
    settings.credentials.shared_credential_id = credential_id
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    events: list[str] = []
    original_get = runtime.credential_service.get

    def scan(host, port, **_kwargs):
        events.append(f"scan:{host}")
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    def get_credential(identifier):
        events.append("credential-read")
        return original_get(identifier)

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            events.append(f"auth:{self.host}")

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr(runtime.credential_service, "get", get_credential)
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    result = runtime.test_connection("all", ConnectionTestRequest(settings))

    assert result.status == "success"
    last_scan = max(index for index, event in enumerate(events) if event.startswith("scan:"))
    first_credential_read = events.index("credential-read")
    assert first_credential_read > last_scan


def test_integrated_onboarding_blocks_any_existing_key_mismatch_before_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    scanned_hosts: list[str] = []

    def scan(host, port, **_kwargs):
        scanned_hosts.append(host)
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    def check(scanned, _path):
        if scanned.host == "192.0.2.12":
            return HostKeyCheck("mismatch", scanned, ("SHA256:approved",))
        return HostKeyCheck("verified", scanned)

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr("aruba_mini_dashboard.main.check_scanned_host_key", check)
    monkeypatch.setattr(
        runtime.credential_service,
        "get",
        lambda _credential_id: pytest.fail("credential store read after key mismatch"),
    )

    result = runtime.test_connection("all", ConnectionTestRequest(settings))

    assert result.status == "mismatch"
    assert result.host == "192.0.2.12"
    assert result.expected_fingerprints == ("SHA256:approved",)
    assert scanned_hosts == ["192.0.2.1", "192.0.2.11", "192.0.2.12"]


def test_integrated_onboarding_does_not_offer_partial_approval_without_mm_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"

    def scan(host, port, **_kwargs):
        if host == "192.0.2.1":
            raise SshHostKeyError("SSH 서버 지문을 확인하지 못했습니다.")
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("unregistered", scanned),
    )
    monkeypatch.setattr(
        runtime.credential_service,
        "get",
        lambda _credential_id: pytest.fail("credential store read without MM identity"),
    )

    result = runtime.test_connection("all", ConnectionTestRequest(settings))

    assert result.status == "failed"
    assert result.scanned is None
    assert [item.status for item in result.details] == [
        "scan_failed",
        "approval_required",
    ]


def test_integrated_onboarding_allows_non_core_fallback_failures_with_warning(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12", "192.0.2.13"]
    credential = DeviceCredential("typed-user", "typed-password")
    attempts: list[str] = []

    def scan(host, port, **_kwargs):
        if host == "192.0.2.13":
            raise SshHostKeyError("SSH 서버 지문을 확인하지 못했습니다.")
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            attempts.append(self.host)
            if self.host == "192.0.2.11":
                raise SshOperationError(
                    "AUTH_FAILED",
                    "장비 로그인에 실패했습니다.",
                    retryable=False,
                    operation="connect",
                )

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    result = runtime.test_connection(
        "all",
        ConnectionTestRequest(settings, credential, purpose="save"),
    )

    assert result.status == "success"
    assert result.core_ready is True
    assert result.pending_fallbacks == 2
    assert "Fallback 준비 미완료 2대" in result.message
    assert attempts == ["192.0.2.1", "192.0.2.11", "192.0.2.12"]


def test_integrated_onboarding_requires_mm_and_one_cluster_login(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    credential = DeviceCredential("typed-user", "typed-password")

    def scan(host, port, **_kwargs):
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            if self.host == "192.0.2.1":
                raise SshOperationError(
                    "AUTH_FAILED",
                    "장비 로그인에 실패했습니다.",
                    retryable=False,
                    operation="connect",
                )

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    result = runtime.test_connection("all", ConnectionTestRequest(settings, credential))

    assert result.status == "failed"
    assert result.core_ready is False
    assert any(item.role == "cluster" and item.status == "authenticated" for item in result.details)


def test_integrated_onboarding_blocks_a_key_change_during_any_authentication(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    settings = AppSettings.default()
    settings.mobility_master.management_ip = "192.0.2.1"
    settings.cluster.primary_controller_ip = "192.0.2.11"
    settings.cluster.fallback_controller_ips = ["192.0.2.12"]
    credential = DeviceCredential("typed-user", "typed-password")

    def scan(host, port, **_kwargs):
        return ScannedHostKey(host, port, "ssh-ed25519", f"SHA256:{host}", FakeKey())

    class Adapter:
        def __init__(self, options, _credential, **_kwargs) -> None:
            self.host = options.host

        def connect(self) -> None:
            if self.host == "192.0.2.12":
                raise SshOperationError(
                    "SSH_HOST_KEY_MISMATCH",
                    "SSH 서버 키가 이전 승인 값과 다릅니다.",
                    retryable=False,
                    operation="connect",
                )

        def close(self) -> None:
            return None

    monkeypatch.setattr("aruba_mini_dashboard.main.scan_ssh_host_key", scan)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.check_scanned_host_key",
        lambda scanned, _path: HostKeyCheck("verified", scanned),
    )
    monkeypatch.setattr("aruba_mini_dashboard.main.ArubaSshAdapter", Adapter)

    result = runtime.test_connection("all", ConnectionTestRequest(settings, credential))

    assert result.status == "mismatch"
    assert result.host == "192.0.2.12"


@pytest.mark.reliability
def test_batch_host_key_registration_is_atomic_on_any_conflict(tmp_path: Path) -> None:
    paramiko = pytest.importorskip("paramiko")
    known_hosts = tmp_path / "known_hosts"
    approved_key = paramiko.RSAKey.generate(1024)
    replacement_key = paramiko.RSAKey.generate(1024)
    unrelated_key = paramiko.RSAKey.generate(1024)
    approved = ScannedHostKey(
        "192.0.2.1",
        22,
        approved_key.get_name(),
        "SHA256:approved",
        approved_key,
    )
    register_scanned_host_keys((approved,), known_hosts)

    with pytest.raises(SshHostKeyMismatchError):
        register_scanned_host_keys(
            (
                ScannedHostKey(
                    "192.0.2.99",
                    22,
                    unrelated_key.get_name(),
                    "SHA256:unrelated",
                    unrelated_key,
                ),
                ScannedHostKey(
                    "192.0.2.1",
                    22,
                    replacement_key.get_name(),
                    "SHA256:replacement",
                    replacement_key,
                ),
            ),
            known_hosts,
        )

    stored = paramiko.HostKeys(str(known_hosts))
    assert stored.lookup("192.0.2.1")[approved_key.get_name()] == approved_key
    assert stored.lookup("192.0.2.99") is None
