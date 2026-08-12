from __future__ import annotations

import json
from pathlib import Path

import pytest

import aruba_mini_dashboard.config as config_module
from aruba_mini_dashboard.config import (
    MAX_SETTINGS_ARRAY_ITEMS,
    MAX_SETTINGS_FILE_BYTES,
    MAX_SETTINGS_OBJECT_MEMBERS,
    MAX_SETTINGS_UPDATE_MARKER_BYTES,
    AppSettings,
    SettingsCorruptError,
    SettingsStore,
    SettingsValidationError,
)


def test_oversized_settings_file_is_rejected_before_json_decode_and_preserved(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    oversized = b"{" + (b"x" * MAX_SETTINGS_FILE_BYTES)
    settings_path.write_bytes(oversized)

    def fail_if_decoded(_raw: str):
        raise AssertionError("oversized settings must not reach json.loads")

    monkeypatch.setattr(config_module.json, "loads", fail_if_decoded)

    with pytest.raises(SettingsCorruptError, match="허용 크기"):
        SettingsStore(settings_path).load()

    assert settings_path.read_bytes() == oversized


def test_settings_save_refuses_payload_larger_than_load_ceiling(tmp_path: Path) -> None:
    settings = AppSettings.default()
    settings.mobility_master.display_name = "x" * MAX_SETTINGS_FILE_BYTES
    settings_path = tmp_path / "settings.json"

    with pytest.raises(SettingsValidationError, match="최대 크기"):
        SettingsStore(settings_path).save(settings)

    assert not settings_path.exists()


@pytest.mark.parametrize(
    "oversized_value",
    (
        {f"field-{index}": index for index in range(MAX_SETTINGS_OBJECT_MEMBERS + 1)},
        list(range(MAX_SETTINGS_ARRAY_ITEMS + 1)),
    ),
)
def test_untrusted_settings_shape_is_bounded_before_schema_construction(
    oversized_value: object,
) -> None:
    payload = AppSettings.default().to_dict()
    payload["unexpected"] = oversized_value

    with pytest.raises(SettingsCorruptError, match="구조"):
        AppSettings.from_dict(payload)


def test_cluster_member_expansion_is_rejected_before_dataclass_construction() -> None:
    payload = AppSettings.default().to_dict()
    payload["cluster"]["members"] = [
        {"ip": f"192.0.2.{index}", "alias": f"WLC-{index}"}
        for index in range(1, 6)
    ]

    with pytest.raises(SettingsCorruptError, match="최대 4개"):
        AppSettings.from_dict(payload)


def test_default_settings_still_fit_all_new_limits(tmp_path: Path) -> None:
    path = tmp_path / "한글 설정 폴더" / "settings file.json"
    store = SettingsStore(path)

    store.save(AppSettings.default())

    assert path.stat().st_size < MAX_SETTINGS_FILE_BYTES
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert store.load() == AppSettings.default()


def test_interrupted_settings_update_recovers_exact_previous_json_on_next_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    original = AppSettings.default()
    original.polling.interval_seconds = 90
    store.save(original)
    original_bytes = path.read_bytes()
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    store.begin_update(candidate)  # Simulate a crash before runtime commit.
    assert SettingsStore(path).load() == original
    assert path.read_bytes() == original_bytes


def test_committed_settings_update_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings.default())
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    update = store.begin_update(candidate)
    update.commit()

    assert SettingsStore(path).load() == candidate
    assert not path.with_name(f".{path.name}.rollback").exists()


def test_committed_update_removes_prior_topology_from_rollback_sidecar(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    original = AppSettings.default()
    original.mobility_master.management_ip = "192.0.2.77"
    store.save(original)
    candidate = AppSettings.default()

    update = store.begin_update(candidate)
    rollback = path.with_name(f".{path.name}.rollback")
    assert b"192.0.2.77" in rollback.read_bytes()
    update.commit()

    assert not rollback.exists()


def test_settings_update_reads_original_without_path_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings.default())
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    def unbounded_read_is_forbidden(_path: Path) -> bytes:
        raise AssertionError("transaction files must use bounded reads")

    monkeypatch.setattr(Path, "read_bytes", unbounded_read_is_forbidden)
    update = store.begin_update(candidate)
    update.rollback()


def test_oversized_pending_marker_is_rejected_without_replacing_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    original = AppSettings.default()
    store.save(original)
    original_bytes = path.read_bytes()
    marker = path.with_name(f".{path.name}.update-pending")
    marker.write_bytes(b"x" * (MAX_SETTINGS_UPDATE_MARKER_BYTES + 1))

    with pytest.raises(SettingsCorruptError, match="중단된 설정 변경"):
        store.load()

    assert path.read_bytes() == original_bytes
    assert marker.exists()


def test_oversized_rollback_is_rejected_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings.default())
    marker = path.with_name(f".{path.name}.update-pending")
    rollback = path.with_name(f".{path.name}.rollback")
    marker.write_text('{"version":1,"original_exists":true}', encoding="ascii")
    rollback.write_bytes(b"x" * (MAX_SETTINGS_FILE_BYTES + 1))

    with pytest.raises(SettingsCorruptError, match="중단된 설정 변경"):
        store.load()

    assert marker.exists()
    assert rollback.stat().st_size == MAX_SETTINGS_FILE_BYTES + 1


@pytest.mark.parametrize("marker_payload", ("[]", "null", "1", '"text"', '{"version":true,"original_exists":true}'))
def test_non_object_or_wrong_type_pending_marker_is_reported_as_corrupt(
    tmp_path: Path,
    marker_payload: str,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings.default())
    marker = path.with_name(f".{path.name}.update-pending")
    marker.write_text(marker_payload, encoding="ascii")

    with pytest.raises(SettingsCorruptError, match="중단된 설정 변경"):
        store.load()

    assert marker.read_text(encoding="ascii") == marker_payload
