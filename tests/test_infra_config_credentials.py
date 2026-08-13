from __future__ import annotations

import json
import threading
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
    MAX_CREDENTIAL_BLOB_BYTES,
    CredentialError,
    CredentialNotFoundError,
    CredentialService,
    DeviceCredential,
    SessionCredentialStore,
    WindowsCredentialStore,
    credential_target,
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
    (("performance", "low_spec_mode"), "false"),
    (("performance", "performance_logging"), 1),
    (("ui", "always_on_top"), "false"),
    (("ui", "window_maximized"), "false"),
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
        ("performance", "low_spec_mode"),
        ("performance", "performance_logging"),
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


@pytest.mark.parametrize(
    "key",
    (
        "password",
        "enable_secret",
        "api-token",
        "access_token",
        "clientSecret",
        "apiKey",
        "private-key",
        "Authorization",
        "password_value",
        "token_value",
        "access_token_value",
        "secret_key",
        "credentialBlobValue",
        "apiKeyValue",
        "private_key_value",
        "authorizationHeader",
        "clientsecret",
        "userpassword",
        "dbpassword",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
    ),
)
def test_unknown_nested_secret_keys_are_never_ignored(key: str) -> None:
    payload = AppSettings.default().to_dict()
    payload["legacy"] = {key: "sensitive-value"}

    with pytest.raises(SettingsValidationError) as raised:
        AppSettings.from_dict(payload)

    assert "sensitive-value" not in str(raised.value)


def test_secret_field_rejection_does_not_echo_untrusted_key_text() -> None:
    canary = "CANARY-SECRET-IN-KEY"
    payload = AppSettings.default().to_dict()
    payload["legacy"] = {f"password_{canary}": "ordinary-value"}

    with pytest.raises(SettingsValidationError) as raised:
        AppSettings.from_dict(payload)

    assert canary not in str(raised.value)


@pytest.mark.parametrize(
    "key",
    ("tokenizer", "event_tokens", "secretary_name", "passwordless", "tokenization_mode"),
)
def test_non_secret_token_like_unknown_keys_are_not_false_positives(key: str) -> None:
    payload = AppSettings.default().to_dict()
    payload["legacy"] = {key: "ordinary-value"}

    AppSettings.from_dict(payload)


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


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("window_x", -(2**31) - 1),
        ("window_x", 2**31),
        ("window_y", -(2**63)),
        ("window_y", 2**63 - 1),
    ),
)
def test_window_coordinates_are_bounded_before_qpoint_construction(
    field_name: str,
    value: int,
) -> None:
    settings = AppSettings.default()
    setattr(settings.ui, field_name, value)

    with pytest.raises(SettingsValidationError, match="창 [XY] 좌표"):
        settings.validate()


def test_performance_settings_are_additive_and_low_mode_has_effective_interval() -> None:
    legacy = AppSettings.default().to_dict()
    legacy.pop("performance")
    loaded = AppSettings.from_dict(legacy)

    assert loaded.performance.low_spec_mode is False
    assert loaded.performance.performance_logging is False
    assert loaded.effective_poll_interval_seconds == 60

    loaded.performance.low_spec_mode = True
    assert loaded.polling.interval_seconds == 60
    assert loaded.effective_poll_interval_seconds == 120
    loaded.polling.interval_seconds = 300
    assert loaded.effective_poll_interval_seconds == 300


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


def test_windows_credential_store_rejects_corrupt_types_and_oversized_blob() -> None:
    api = FakeCredentialApi()
    store = WindowsCredentialStore(api)
    identifier = new_credential_id()
    target = credential_target(identifier)
    sentinel = "malformed-secret-must-not-be-repeated"

    invalid_records = (
        {
            "UserName": "operator",
            "CredentialBlob": json.dumps(
                {"password": {"nested": sentinel}, "enable_secret": ""}
            ),
        },
        {
            "UserName": 123,
            "CredentialBlob": json.dumps({"password": "valid", "enable_secret": ""}),
        },
        {
            "UserName": "operator",
            "CredentialBlob": json.dumps(
                {"password": "x" * MAX_CREDENTIAL_BLOB_BYTES, "enable_secret": ""}
            ),
        },
        {
            "UserName": "operator",
            "CredentialBlob": '{"password":"first","password":"second"}',
        },
        {
            "UserName": "operator",
            "CredentialBlob": '{"password":"valid","enable_secret":"","diagnostic":NaN}',
        },
        {
            "UserName": "operator",
            "CredentialBlob": '{"password":"valid","enable_secret":"","diagnostic":Infinity}',
        },
        {
            "UserName": "operator",
            "CredentialBlob": '{"password":"valid","enable_secret":"","diagnostic":1e999}',
        },
    )
    for record in invalid_records:
        api.records[target] = record
        with pytest.raises(CredentialError) as raised:
            store.get(identifier)
        assert sentinel not in str(raised.value)

    with pytest.raises(ValueError, match="형식"):
        DeviceCredential(123, "password")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Windows 저장 한도"):
        DeviceCredential("u" * 514, "password")


def test_credential_service_does_not_expose_half_published_session_value() -> None:
    saved = threading.Event()
    release = threading.Event()

    class BlockingSessionStore(SessionCredentialStore):
        def save(self, credential_id: str, credential: DeviceCredential) -> str:
            result = super().save(credential_id, credential)
            saved.set()
            if not release.wait(timeout=2):
                raise AssertionError("test did not release session save")
            return result

    session = BlockingSessionStore()
    service = CredentialService(
        persistent=SessionCredentialStore(),
        session=session,
    )
    identifier = new_credential_id()
    write_errors: list[BaseException] = []
    read_results: list[DeviceCredential] = []
    read_errors: list[BaseException] = []
    reader_started = threading.Event()

    def write() -> None:
        try:
            service.save(
                DeviceCredential("operator", "temporary"),
                session_only=True,
                credential_id=identifier,
            )
        except BaseException as exc:
            write_errors.append(exc)

    def read() -> None:
        reader_started.set()
        try:
            read_results.append(service.get(identifier))
        except BaseException as exc:
            read_errors.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    reader = threading.Thread(target=read, daemon=True)
    writer.start()
    assert saved.wait(timeout=2)
    reader.start()
    assert reader_started.wait(timeout=2)
    try:
        reader.join(timeout=0.1)
        assert reader.is_alive(), "reader observed the store before session routing committed"
    finally:
        release.set()
        writer.join(timeout=2)
        reader.join(timeout=2)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert write_errors == []
    assert read_errors == []
    assert read_results == [DeviceCredential("operator", "temporary")]


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
