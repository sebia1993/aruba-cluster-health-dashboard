from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from aruba_mini_dashboard.collectors.base import (
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    SHOW_SWITCHES,
    CollectionBundle,
    CommandResult,
)
from aruba_mini_dashboard.config import AppPaths, AppSettings, SettingsError, SettingsStore
from aruba_mini_dashboard.credentials import (
    CredentialService,
    DeviceCredential,
    SessionCredentialStore,
    new_credential_id,
)
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.parsers import parse_show_switches
from aruba_mini_dashboard.storage import SQLiteStorage


class CaptureLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str, *args) -> None:
        self.messages.append(message % args)


def test_string_false_cannot_enable_ssh_debug_excerpt_logging(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    payload = AppSettings.default().to_dict()
    payload["ssh_debug_logging"] = "false"
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps(payload), encoding="utf-8")

    try:
        loaded = SettingsStore(paths).load()
    except SettingsError:
        loaded = AppSettings.default()
    context = setup_logging(paths, ssh_debug_enabled=loaded.ssh_debug_logging)

    assert loaded.ssh_debug_logging is False
    assert context.ssh_logger.handlers == []
    assert not paths.ssh_debug_log.exists()


def test_parser_failure_logs_reason_and_redacted_excerpt(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    context = setup_logging(paths, ssh_debug_enabled=True)
    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        SQLiteStorage(":memory:"),
        context,
    )
    capture = CaptureLogger()
    runtime.logging_context.ssh_logger = capture
    bundle = CollectionBundle(
        source="mm",
        requested_controller_ip="192.0.2.1",
        actual_controller_ip="192.0.2.1",
        commands={
            SHOW_SWITCHES: CommandResult(
                SHOW_SWITCHES,
                True,
                output="password: super-secret\nthis is not an Aruba table",
            )
        },
    )
    errors = []
    parsed = runtime._parse_command(bundle, SHOW_SWITCHES, parse_show_switches, errors)
    assert parsed.status.value == "failed"
    debug_text = "\n".join(capture.messages)
    assert "PARSE_FAILED" in debug_text
    assert "[REDACTED]" in debug_text
    assert "super-secret" not in debug_text
    for handler in context.logger.handlers:
        handler.flush()
    app_log = paths.app_log.read_text(encoding="utf-8")
    assert "PARSE_FAILED" in app_log
    assert "super-secret" not in app_log


def test_smoke_output_file_works_without_console(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(tmp_path / "data"))
    marker = tmp_path / "smoke" / "result.txt"
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.QApplication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("smoke must not create a QApplication")
        ),
    )
    from aruba_mini_dashboard.main import main

    assert main(["--smoke", "--smoke-output", str(marker)]) == 0
    smoke_markers = set(marker.read_text(encoding="utf-8").splitlines())
    assert {
        "ARUBA_MINI_DASHBOARD_SMOKE_OK",
        "NETMIKO_OK",
        "PARAMIKO_OK",
        "FIXTURE_DISCOVERY_OK",
        "DEMO_CORRELATION_OK",
    } <= smoke_markers
    if os.name == "nt":
        assert "WIN32CRED_OK" in smoke_markers


def test_ui_smoke_runs_dashboard_worker_and_cleans_up_without_runtime_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(tmp_path / "must-not-exist"))
    marker = tmp_path / "ui-smoke" / "result.txt"
    from aruba_mini_dashboard.main import main

    assert main(["--ui-smoke", "--smoke-output", str(marker)]) == 0
    assert marker.read_text(encoding="utf-8") == (
        "WINDOWS_QT_UI_OK\nWINDOWS_LIFECYCLE_OK\n"
    )
    assert not (tmp_path / "must-not-exist").exists()


def test_missing_mm_credential_does_not_discard_successful_cluster_collection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    persistent = SessionCredentialStore()
    credentials = CredentialService(persistent=persistent)
    cluster_id = credentials.save(
        DeviceCredential("cluster-operator", "cluster-secret"),
        session_only=False,
    )
    settings = AppSettings.default()
    settings.credentials.use_shared_credentials = False
    settings.mobility_master.credential_id = new_credential_id()  # deliberately absent
    settings.cluster.credential_id = cluster_id
    settings.mobility_master.management_ip = "192.0.2.1"
    for index, member in enumerate(settings.cluster.members, start=11):
        member.ip = f"192.0.2.{index}"
        member.alias = f"WLC-{index - 10:02d}"
    settings.cluster.primary_controller_ip = "192.0.2.11"

    fixture_dir = Path(__file__).parent / "fixtures"

    def collect_cluster(_self, _settings, _credential):
        return CollectionBundle(
            source="cluster",
            requested_controller_ip="192.0.2.11",
            actual_controller_ip="192.0.2.11",
            commands={
                SHOW_CLIENT_DISTRIBUTION: CommandResult(
                    SHOW_CLIENT_DISTRIBUTION,
                    True,
                    output=(fixture_dir / "cluster_load_normal.txt").read_text(encoding="utf-8"),
                ),
                SHOW_GROUP_MEMBERSHIP: CommandResult(
                    SHOW_GROUP_MEMBERSHIP,
                    True,
                    output=(fixture_dir / "group_membership_initial.txt").read_text(encoding="utf-8"),
                ),
            },
        )

    monkeypatch.setattr("aruba_mini_dashboard.main.ClusterCollector.collect", collect_cluster)
    monkeypatch.setattr(
        "aruba_mini_dashboard.main.MmCollector.collect",
        lambda *_args: (_ for _ in ()).throw(AssertionError("MM collector must not run")),
    )
    storage = SQLiteStorage(":memory:")
    runtime = RuntimePoller(settings, paths, credentials, storage, setup_logging(paths))

    snapshot = runtime()

    assert snapshot.partial is True
    assert any(error.code == "CREDENTIAL_NOT_FOUND" for error in snapshot.collection_errors)
    wlc_one = snapshot.device_by_ip("192.0.2.11")
    assert (wlc_one.active_clients, wlc_one.standby_clients) == (250, 260)
    assert wlc_one.connection_type == "Type-A"
    storage.close()


def test_low_spec_collection_is_sequential_but_preserves_results_and_request_counts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fixture_dir = Path(__file__).parent / "fixtures"
    mm_output = (fixture_dir / "mm_show_switches_normal.txt").read_text(encoding="utf-8")
    load_output = (fixture_dir / "cluster_load_normal.txt").read_text(encoding="utf-8")
    membership_output = (fixture_dir / "group_membership_initial.txt").read_text(
        encoding="utf-8"
    )

    def exercise(low_spec_mode: bool):
        paths = AppPaths.from_environment(tmp_path / ("low" if low_spec_mode else "normal")).ensure()
        persistent = SessionCredentialStore()
        credentials = CredentialService(persistent=persistent)
        credential_id = credentials.save(
            DeviceCredential("operator", "session-secret"),
            session_only=False,
        )
        settings = AppSettings.default()
        settings.credentials.shared_credential_id = credential_id
        settings.mobility_master.management_ip = "192.0.2.10"
        for index, member in enumerate(settings.cluster.members, start=11):
            member.ip = f"192.0.2.{index}"
            member.alias = f"WLC-{index - 10:02d}"
        settings.cluster.primary_controller_ip = "192.0.2.11"
        settings.performance.low_spec_mode = low_spec_mode
        calls: list[str] = []
        concurrency_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def enter(source: str) -> None:
            nonlocal active, maximum_active
            with concurrency_lock:
                calls.append(source)
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)

        def leave() -> None:
            nonlocal active
            with concurrency_lock:
                active -= 1

        def collect_mm(_self, _settings, _credential):
            enter("mm")
            try:
                return CollectionBundle(
                    source="mm",
                    requested_controller_ip="192.0.2.10",
                    actual_controller_ip="192.0.2.10",
                    commands={SHOW_SWITCHES: CommandResult(SHOW_SWITCHES, True, output=mm_output)},
                )
            finally:
                leave()

        def collect_cluster(_self, _settings, _credential):
            enter("cluster")
            try:
                return CollectionBundle(
                    source="cluster",
                    requested_controller_ip="192.0.2.11",
                    actual_controller_ip="192.0.2.11",
                    commands={
                        SHOW_CLIENT_DISTRIBUTION: CommandResult(
                            SHOW_CLIENT_DISTRIBUTION, True, output=load_output
                        ),
                        SHOW_GROUP_MEMBERSHIP: CommandResult(
                            SHOW_GROUP_MEMBERSHIP, True, output=membership_output
                        ),
                    },
                )
            finally:
                leave()

        monkeypatch.setattr("aruba_mini_dashboard.main.MmCollector.collect", collect_mm)
        monkeypatch.setattr("aruba_mini_dashboard.main.ClusterCollector.collect", collect_cluster)
        storage = SQLiteStorage(paths)
        runtime = RuntimePoller(settings, paths, credentials, storage, setup_logging(paths))
        writes = 0
        original_write = storage._write

        def counted_write(operation):
            nonlocal writes
            writes += 1
            return original_write(operation)

        monkeypatch.setattr(storage, "_write", counted_write)
        snapshot = runtime()
        result = {
            device.ip: (
                device.mm_status,
                device.active_clients,
                device.standby_clients,
                device.connection_type,
                device.severity,
            )
            for device in snapshot.devices
        }
        storage.close()
        credentials.close()
        return result, calls, maximum_active, writes

    normal, normal_calls, _normal_concurrency, normal_writes = exercise(False)
    low, low_calls, low_concurrency, low_writes = exercise(True)

    assert low == normal
    assert sorted(low_calls) == sorted(normal_calls) == ["cluster", "mm"]
    assert low_calls == ["mm", "cluster"]
    assert low_concurrency == 1
    assert normal_writes <= 2
    assert low_writes <= 2
