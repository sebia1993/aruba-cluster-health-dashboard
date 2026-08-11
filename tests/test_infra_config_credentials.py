from __future__ import annotations

import json
from pathlib import Path

import pytest

from aruba_mini_dashboard.config import (
    AppPaths,
    AppSettings,
    SettingsCorruptError,
    SettingsError,
    SettingsStore,
    SettingsValidationError,
)
from aruba_mini_dashboard.credentials import (
    CredentialNotFoundError,
    CredentialService,
    DeviceCredential,
    SessionCredentialStore,
    WindowsCredentialStore,
    new_credential_id,
)


class FakeCredentialApi:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def CredWrite(self, record: dict, _flags: int) -> None:
        self.records[record["TargetName"]] = dict(record)

    def CredRead(self, target: str, _kind: int, _flags: int) -> dict:
        if target not in self.records:
            exc = OSError("not found")
            exc.winerror = 1168
            raise exc
        return dict(self.records[target])

    def CredDelete(self, target: str, _kind: int, _flags: int) -> None:
        if target not in self.records:
            exc = OSError("not found")
            exc.winerror = 1168
            raise exc
        del self.records[target]


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_environment(tmp_path)


def test_data_directory_override_is_used_directly(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "portable-state"
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(override))
    paths = AppPaths.from_environment()
    assert paths.root == override
    assert paths.database == override / "app.db"


def test_settings_round_trip_contains_only_opaque_credential_ids(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    settings = AppSettings.default()
    credential_id = new_credential_id()
    settings.credentials.shared_credential_id = credential_id
    settings.mobility_master.management_ip = "192.0.2.10"
    settings.cluster.members[0].ip = "192.0.2.11"

    SettingsStore(paths).save(settings)
    raw = paths.settings.read_text(encoding="utf-8")
    loaded = SettingsStore(paths).load()

    assert loaded.credentials.shared_credential_id == credential_id
    assert loaded.mobility_master.management_ip == "192.0.2.10"
    assert "password" not in raw.casefold()
    assert "enable_secret" not in raw.casefold()
    assert json.loads(raw)["credentials"]["shared_credential_id"] == credential_id


def test_settings_store_preserves_corrupt_file(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.settings.parent.mkdir(parents=True)
    paths.settings.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SettingsCorruptError):
        SettingsStore(paths).load()

    assert paths.settings.read_text(encoding="utf-8") == "{not-json"


_MALFORMED_SCHEMA_VALUES = (
    (("schema_version",), "1"),
    (("mobility_master", "management_ip"), 123),
    (("mobility_master", "display_name"), False),
    (("mobility_master", "ssh_port"), "22"),
    (("mobility_master", "credential_id"), []),
    (("mobility_master", "enable_required"), "false"),
    (("cluster", "name"), 7240),
    (("cluster", "members"), {}),
    (("cluster", "members", 0, "ip"), 123),
    (("cluster", "members", 0, "alias"), []),
    (("cluster", "primary_controller_ip"), True),
    (("cluster", "fallback_controller_ips"), "192.0.2.12"),
    (("cluster", "fallback_controller_ips"), [123]),
    (("cluster", "ssh_port"), False),
    (("cluster", "credential_id"), 1),
    (("cluster", "enable_required"), 1),
    (("credentials", "use_shared_credentials"), "false"),
    (("credentials", "shared_credential_id"), 123),
    (("credentials", "session_only"), 0),
    (("polling", "interval_seconds"), True),
    (("polling", "automatic_enabled"), "false"),
    (("polling", "busy_policy"), False),
    (("detection", "low_client_threshold"), "10"),
    (("detection", "comparison_mode"), 0),
    (("notifications", "notify_new_incidents"), "true"),
    (("notifications", "repeat_unacknowledged"), 1),
    (("notifications", "repeat_interval_minutes"), True),
    (("notifications", "sound_enabled"), "false"),
    (("notifications", "recovery_notifications"), None),
    (("ui", "always_on_top"), "false"),
    (("ui", "opacity_percent"), True),
    (("ui", "window_x"), "10"),
    (("ui", "window_y"), False),
    (("ui", "window_width"), "420"),
    (("ssh_debug_logging",), "false"),
)


def _replace_nested(payload: object, path: tuple[object, ...], value: object) -> None:
    current = payload
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]


@pytest.mark.parametrize(("path", "invalid"), _MALFORMED_SCHEMA_VALUES)
def test_settings_schema_types_are_fail_closed(
    tmp_path: Path,
    path: tuple[object, ...],
    invalid: object,
) -> None:
    payload = AppSettings.default().to_dict()
    _replace_nested(payload, path, invalid)
    encoded = json.dumps(payload, ensure_ascii=False)

    with pytest.raises(SettingsCorruptError):
        AppSettings.from_dict(payload)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(encoded, encoding="utf-8")
    with pytest.raises(SettingsError):
        SettingsStore(settings_path).load()
    assert settings_path.read_text(encoding="utf-8") == encoded


@pytest.mark.parametrize(
    "field_path",
    (
        ("mobility_master", "enable_required"),
        ("cluster", "enable_required"),
        ("credentials", "use_shared_credentials"),
        ("credentials", "session_only"),
        ("polling", "automatic_enabled"),
        ("notifications", "notify_new_incidents"),
        ("notifications", "repeat_unacknowledged"),
        ("notifications", "sound_enabled"),
        ("notifications", "recovery_notifications"),
        ("ui", "always_on_top"),
        (None, "ssh_debug_logging"),
    ),
)
def test_programmatic_boolean_settings_require_exact_bool(
    field_path: tuple[str | None, str],
) -> None:
    settings = AppSettings.default()
    section_name, field_name = field_path
    owner = settings if section_name is None else getattr(settings, section_name)
    setattr(owner, field_name, "false")

    with pytest.raises(SettingsValidationError, match="JSON 불리언"):
        settings.validate()


def test_plaintext_secret_keys_are_rejected_without_echo_and_file_is_preserved(
    tmp_path: Path,
) -> None:
    payload = AppSettings.default().to_dict()
    secret_value = "do-not-repeat-this-secret"
    payload["mobility_master"]["password"] = secret_value
    encoded = json.dumps(payload, ensure_ascii=False)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(encoded, encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        SettingsStore(settings_path).load()

    assert secret_value not in str(raised.value)
    assert settings_path.read_text(encoding="utf-8") == encoded


@pytest.mark.parametrize("key", ("password", "enable_secret", "api-token", "access_token"))
def test_unknown_nested_secret_keys_are_never_ignored(key: str) -> None:
    payload = AppSettings.default().to_dict()
    payload["legacy"] = {key: "sensitive-value"}

    with pytest.raises(SettingsValidationError) as raised:
        AppSettings.from_dict(payload)

    assert "sensitive-value" not in str(raised.value)


def test_invalid_credential_identifier_is_rejected_as_a_settings_error(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    settings = AppSettings.default()
    payload = settings.to_dict()
    payload["credentials"]["shared_credential_id"] = "invalid-id"
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SettingsValidationError, match="자격 증명 식별자"):
        SettingsStore(paths).load()


def test_settings_validate_bounds_and_monitoring_completeness() -> None:
    settings = AppSettings.default()
    settings.polling.interval_seconds = 9
    with pytest.raises(SettingsValidationError, match="점검 주기"):
        settings.validate()

    settings.polling.interval_seconds = 60
    with pytest.raises(SettingsValidationError, match="MM 관리 IP"):
        settings.validate_for_monitoring()


def test_windows_credential_store_round_trip_uses_generic_blob() -> None:
    api = FakeCredentialApi()
    store = WindowsCredentialStore(api)
    identifier = new_credential_id()
    credential = DeviceCredential("operator", "pāssword", "enable-value")

    store.save(identifier, credential)
    loaded = store.get(identifier)

    record = next(iter(api.records.values()))
    assert record["Type"] == api.CRED_TYPE_GENERIC
    assert isinstance(record["CredentialBlob"], str)
    assert loaded == credential

    # Real pywin32 returns Unicode CredWrite blobs as UTF-16LE bytes.
    record["CredentialBlob"] = record["CredentialBlob"].encode("utf-16-le")
    api.records[record["TargetName"]] = record
    assert store.get(identifier) == credential
    assert "pāssword" not in repr(credential)
    store.delete(identifier)
    with pytest.raises(CredentialNotFoundError):
        store.get(identifier)


def test_session_credentials_are_cleared_without_touching_persistent_store() -> None:
    persistent_api = FakeCredentialApi()
    service = CredentialService(
        persistent=WindowsCredentialStore(persistent_api),
        session=SessionCredentialStore(),
    )
    identifier = service.save(DeviceCredential("operator", "temporary"), session_only=True)
    assert service.get(identifier).password == "temporary"

    service.close()

    with pytest.raises(CredentialNotFoundError):
        service.get(identifier)
    assert persistent_api.records == {}
