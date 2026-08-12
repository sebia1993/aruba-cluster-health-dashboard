from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aruba_mini_dashboard.collectors.aruba_ssh import (
    MAX_COMMAND_OUTPUT_CHARACTERS,
    MAX_COMMAND_OUTPUT_LINES,
    ArubaSshAdapter,
    _BoundedOutputAccumulator,
    validate_bounded_output,
    validate_read_only_command,
)
from aruba_mini_dashboard.collectors.base import (
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    SHOW_SWITCHES,
    SshConnectionOptions,
    SshOperationError,
)
from aruba_mini_dashboard.collectors.cluster_collector import ClusterCollector
from aruba_mini_dashboard.collectors.mm_collector import MmCollector
from aruba_mini_dashboard.collectors.ssh_host_keys import scan_ssh_host_key
from aruba_mini_dashboard.config import ClusterSettings, MobilityMasterSettings
from aruba_mini_dashboard.credentials import DeviceCredential


class ScriptedAdapter:
    def __init__(self, script: dict):
        self.script = script
        self.closed = False
        self.commands: list[str] = []

    def connect(self) -> None:
        failure = self.script.get("connect_error")
        if failure:
            raise failure

    def run_read_only(self, command: str) -> str:
        self.commands.append(command)
        result = self.script.get(command, f"output for {command}")
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


class ScriptedFactory:
    def __init__(self, scripts_by_host: dict[str, list[dict]]):
        self.scripts_by_host = scripts_by_host
        self.calls: list[str] = []
        self.adapters: list[ScriptedAdapter] = []

    def __call__(self, options, _credential):
        self.calls.append(options.host)
        script = self.scripts_by_host[options.host].pop(0)
        adapter = ScriptedAdapter(script)
        self.adapters.append(adapter)
        return adapter


class RecordingCancelEvent:
    def __init__(self, *, cancel_on_wait: int | None = None) -> None:
        self.waits: list[float] = []
        self._set = False
        self._cancel_on_wait = cancel_on_wait

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self._cancel_on_wait == len(self.waits):
            self._set = True
        return self._set


def retryable(code: str = "TCP_TIMEOUT") -> SshOperationError:
    return SshOperationError(code, "sanitized failure", retryable=True, operation="connect")


def fatal(code: str = "AUTH_FAILED") -> SshOperationError:
    return SshOperationError(code, "sanitized failure", retryable=False, operation="connect")


def credential() -> DeviceCredential:
    return DeviceCredential("operator", "password")


def test_mm_collector_retries_transport_failure_and_never_turns_it_into_down(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {"192.0.2.10": [{"connect_error": retryable()}, {SHOW_SWITCHES: "192.0.2.11 Up"}]}
    )
    settings = MobilityMasterSettings(management_ip="192.0.2.10", retries=2)
    cancel_event = RecordingCancelEvent()
    result = MmCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=factory,
        cancel_event=cancel_event,
    ).collect(
        settings, credential()
    )

    assert result.complete is True
    assert result.commands[SHOW_SWITCHES].output == "192.0.2.11 Up"
    assert [attempt.success for attempt in result.attempts] == [False, True]
    assert all(adapter.closed for adapter in factory.adapters)
    assert cancel_event.waits == [0.5]


def test_mm_retry_backoff_is_capped_and_cancelable(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.10": [
                {"connect_error": retryable()},
                {"connect_error": retryable()},
                {"connect_error": retryable()},
                {SHOW_SWITCHES: "192.0.2.11 Up"},
            ]
        }
    )
    cancel_event = RecordingCancelEvent()
    result = MmCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=factory,
        cancel_event=cancel_event,
    ).collect(MobilityMasterSettings(management_ip="192.0.2.10", retries=3), credential())

    assert result.complete is True
    assert cancel_event.waits == [0.5, 1.0, 2.0]

    cancelled_factory = ScriptedFactory(
        {"192.0.2.10": [{"connect_error": retryable()}]}
    )
    cancelling_event = RecordingCancelEvent(cancel_on_wait=1)
    cancelled = MmCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=cancelled_factory,
        cancel_event=cancelling_event,
    ).collect(MobilityMasterSettings(management_ip="192.0.2.10", retries=3), credential())

    assert cancelled.complete is False
    assert cancelled.terminal_error_code == "CANCELLED"
    assert cancelled_factory.calls == ["192.0.2.10"]
    assert cancelling_event.waits == [0.5]


def test_mm_auth_failure_is_not_retried(tmp_path: Path) -> None:
    factory = ScriptedFactory({"192.0.2.10": [{"connect_error": fatal()}]})
    result = MmCollector(known_hosts_path=tmp_path / "known_hosts", adapter_factory=factory).collect(
        MobilityMasterSettings(management_ip="192.0.2.10", retries=2), credential()
    )
    assert result.complete is False
    assert result.terminal_error_code == "AUTH_FAILED"
    assert factory.calls == ["192.0.2.10"]


def test_cluster_failover_returns_both_commands_from_same_fallback(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [{"connect_error": retryable()}],
            "192.0.2.12": [
                {
                    SHOW_CLIENT_DISTRIBUTION: "load-from-fallback",
                    SHOW_GROUP_MEMBERSHIP: "membership-from-fallback",
                }
            ],
        }
    )
    settings = ClusterSettings(
        primary_controller_ip="192.0.2.11",
        fallback_controller_ips=["192.0.2.12"],
        retries=0,
    )
    cancel_event = RecordingCancelEvent()
    result = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=factory,
        cancel_event=cancel_event,
    ).collect(
        settings, credential()
    )

    assert result.complete is True
    assert result.actual_controller_ip == "192.0.2.12"
    assert result.primary_failed is True
    assert result.failover_at is not None
    assert {item.output for item in result.commands.values()} == {
        "load-from-fallback",
        "membership-from-fallback",
    }
    assert cancel_event.waits == []


def test_cluster_retries_same_endpoint_with_backoff_and_cancel_stops_failover(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [
                {"connect_error": retryable()},
                {"connect_error": retryable()},
                {
                    SHOW_CLIENT_DISTRIBUTION: "primary-load",
                    SHOW_GROUP_MEMBERSHIP: "primary-membership",
                },
            ],
            "192.0.2.12": [{"connect_error": AssertionError("fallback must not run")}],
        }
    )
    cancel_event = RecordingCancelEvent()
    result = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=factory,
        cancel_event=cancel_event,
    ).collect(
        ClusterSettings(
            primary_controller_ip="192.0.2.11",
            fallback_controller_ips=["192.0.2.12"],
            retries=2,
        ),
        credential(),
    )

    assert result.complete is True
    assert factory.calls == ["192.0.2.11", "192.0.2.11", "192.0.2.11"]
    assert cancel_event.waits == [0.5, 1.0]

    cancelling_factory = ScriptedFactory(
        {
            "192.0.2.11": [{"connect_error": retryable()}],
            "192.0.2.12": [{}],
        }
    )
    cancelling_event = RecordingCancelEvent(cancel_on_wait=1)
    cancelled = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts",
        adapter_factory=cancelling_factory,
        cancel_event=cancelling_event,
    ).collect(
        ClusterSettings(
            primary_controller_ip="192.0.2.11",
            fallback_controller_ips=["192.0.2.12"],
            retries=2,
        ),
        credential(),
    )

    assert cancelled.terminal_error_code == "CANCELLED"
    assert cancelling_factory.calls == ["192.0.2.11"]
    assert cancelling_event.waits == [0.5]


def test_cluster_primary_host_key_mismatch_aborts_before_fallback_or_commands(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [{"connect_error": fatal("SSH_HOST_KEY_MISMATCH")}],
            "192.0.2.12": [
                {
                    SHOW_CLIENT_DISTRIBUTION: "must-not-run",
                    SHOW_GROUP_MEMBERSHIP: "must-not-run",
                }
            ],
        }
    )
    settings = ClusterSettings(
        primary_controller_ip="192.0.2.11",
        fallback_controller_ips=["192.0.2.12"],
        retries=2,
    )

    result = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts", adapter_factory=factory
    ).collect(settings, credential())

    assert result.complete is False
    assert result.actual_controller_ip == "192.0.2.11"
    assert result.terminal_error_code == "SSH_HOST_KEY_MISMATCH"
    assert factory.calls == ["192.0.2.11"]
    assert factory.adapters[0].commands == []
    assert all(
        command.error_code == "SSH_HOST_KEY_MISMATCH" and not command.success
        for command in result.commands.values()
    )


def test_cluster_unapproved_host_key_is_also_fail_closed(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [{"connect_error": fatal("SSH_HOST_KEY_UNKNOWN")}],
            "192.0.2.12": [{}],
        }
    )
    result = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts", adapter_factory=factory
    ).collect(
        ClusterSettings(
            primary_controller_ip="192.0.2.11",
            fallback_controller_ips=["192.0.2.12"],
            retries=0,
        ),
        credential(),
    )

    assert result.terminal_error_code == "SSH_HOST_KEY_UNKNOWN"
    assert factory.calls == ["192.0.2.11"]
    assert factory.adapters[0].commands == []


def test_cluster_auth_failure_still_uses_approved_fallback(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [{"connect_error": fatal("AUTH_FAILED")}],
            "192.0.2.12": [
                {
                    SHOW_CLIENT_DISTRIBUTION: "fallback-load",
                    SHOW_GROUP_MEMBERSHIP: "fallback-membership",
                }
            ],
        }
    )
    result = ClusterCollector(
        known_hosts_path=tmp_path / "known_hosts", adapter_factory=factory
    ).collect(
        ClusterSettings(
            primary_controller_ip="192.0.2.11",
            fallback_controller_ips=["192.0.2.12"],
            retries=2,
        ),
        credential(),
    )

    assert result.complete is True
    assert result.actual_controller_ip == "192.0.2.12"
    assert factory.calls == ["192.0.2.11", "192.0.2.12"]


def test_cluster_does_not_merge_partial_results_between_controllers(tmp_path: Path) -> None:
    factory = ScriptedFactory(
        {
            "192.0.2.11": [
                {
                    SHOW_CLIENT_DISTRIBUTION: "primary-load",
                    SHOW_GROUP_MEMBERSHIP: retryable("COMMAND_TIMEOUT"),
                }
            ],
            "192.0.2.12": [
                {
                    SHOW_CLIENT_DISTRIBUTION: retryable("COMMAND_TIMEOUT"),
                    SHOW_GROUP_MEMBERSHIP: "would-not-run",
                }
            ],
        }
    )
    settings = ClusterSettings(
        primary_controller_ip="192.0.2.11", fallback_controller_ips=["192.0.2.12"], retries=0
    )
    result = ClusterCollector(known_hosts_path=tmp_path / "known_hosts", adapter_factory=factory).collect(
        settings, credential()
    )

    assert result.partial is True
    assert result.actual_controller_ip == "192.0.2.11"
    assert result.commands[SHOW_CLIENT_DISTRIBUTION].output == "primary-load"
    assert result.commands[SHOW_GROUP_MEMBERSHIP].success is False
    assert "would-not-run" not in {item.output for item in result.commands.values()}


def test_adapter_enforces_strict_app_known_hosts_and_read_only_allowlist(tmp_path: Path) -> None:
    captured: dict = {}

    class FakeConnection:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def send_command_timing(self, *, command_string: str, **_kwargs):
            self.commands.append(command_string)
            return ""

        def send_command(self, *, command_string: str, **_kwargs):
            self.commands.append(command_string)
            return "valid output"

        def disconnect(self):
            return None

    connection = FakeConnection()

    def factory(**params):
        captured.update(params)
        return connection

    options = SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts")
    with ArubaSshAdapter(options, credential(), connection_factory=factory) as adapter:
        assert adapter.run_read_only(SHOW_SWITCHES) == "valid output"
        with pytest.raises(SshOperationError, match="허용되지 않은"):
            adapter.run_read_only("configure terminal")

    assert captured["ssh_strict"] is True
    assert captured["system_host_keys"] is False
    assert captured["alt_host_keys"] is True
    assert captured["use_keys"] is False
    assert captured["allow_agent"] is False
    assert connection.commands == ["no paging", SHOW_SWITCHES]


def test_adapter_observes_cancellation_after_blocking_timing_read(tmp_path: Path) -> None:
    cancel_event = threading.Event()

    class CancelAfterReadConnection:
        def send_command_timing(self, *, command_string: str, **_kwargs):
            assert command_string == "no paging"
            cancel_event.set()
            return "controller#"

        def disconnect(self):
            return None

    options = SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts")
    adapter = ArubaSshAdapter(
        options,
        credential(),
        connection_factory=lambda **_kwargs: CancelAfterReadConnection(),
        cancel_event=cancel_event,
    )

    with pytest.raises(SshOperationError) as exc_info:
        adapter.connect()

    assert exc_info.value.code == "CANCELLED"


def test_output_and_command_guards_fail_closed() -> None:
    with pytest.raises(SshOperationError) as command_error:
        validate_read_only_command("show switches\nconfigure terminal")
    assert command_error.value.code == "COMMAND_NOT_ALLOWED"
    with pytest.raises(SshOperationError) as output_error:
        validate_bounded_output("x" * (MAX_COMMAND_OUTPUT_CHARACTERS + 1))
    assert output_error.value.code == "OUTPUT_LIMIT_EXCEEDED"
    with pytest.raises(SshOperationError) as empty_error:
        validate_bounded_output("  \n")
    assert empty_error.value.code == "EMPTY_OUTPUT"


def test_stream_accumulator_preserves_exact_size_and_split_crlf_line_limits() -> None:
    characters = _BoundedOutputAccumulator()
    characters.append("x" * (MAX_COMMAND_OUTPUT_CHARACTERS - 1))
    characters.append("y")
    assert characters.character_count == MAX_COMMAND_OUTPUT_CHARACTERS
    assert len(characters.build()) == MAX_COMMAND_OUTPUT_CHARACTERS
    with pytest.raises(SshOperationError) as character_error:
        characters.append("z")
    assert character_error.value.code == "OUTPUT_LIMIT_EXCEEDED"

    lines = _BoundedOutputAccumulator()
    lines.append("x\r\n" * (MAX_COMMAND_OUTPUT_LINES - 2) + "x\r")
    lines.append("\nx")
    assert lines.line_count == MAX_COMMAND_OUTPUT_LINES
    with pytest.raises(SshOperationError) as line_error:
        lines.append("\n")
    assert line_error.value.code == "OUTPUT_LIMIT_EXCEEDED"

    assert validate_bounded_output("x\r\n" * (MAX_COMMAND_OUTPUT_LINES - 1) + "x")
    with pytest.raises(SshOperationError) as direct_line_error:
        validate_bounded_output("x\r" * MAX_COMMAND_OUTPUT_LINES)
    assert direct_line_error.value.code == "OUTPUT_LIMIT_EXCEEDED"


def test_no_paging_failure_uses_bounded_space_pager_fallback(tmp_path: Path) -> None:
    class PagingConnection:
        base_prompt = "controller#"

        def __init__(self) -> None:
            self.inputs: list[tuple[str, bool]] = []

        def send_command_timing(self, *, command_string: str, normalize: bool = True, **_kwargs):
            self.inputs.append((command_string, normalize))
            if command_string == "no paging":
                raise RuntimeError("not permitted")
            if command_string == SHOW_SWITCHES:
                return "header\n192.0.2.11 Up\n--More--"
            if command_string == " ":
                return "\n192.0.2.12 Down\ncontroller#"
            raise AssertionError(command_string)

        def disconnect(self):
            return None

    connection = PagingConnection()
    options = SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts")
    with ArubaSshAdapter(options, credential(), connection_factory=lambda **_kwargs: connection) as adapter:
        output = adapter.run_read_only(SHOW_SWITCHES)

    assert "--More--" not in output
    assert "192.0.2.11 Up" in output and "192.0.2.12 Down" in output
    assert connection.inputs == [("no paging", True), (SHOW_SWITCHES, True), (" ", False)]


def test_pager_fallback_requires_known_base_prompt_when_available(tmp_path: Path) -> None:
    class PartialConnection:
        base_prompt = "controller#"

        def send_command_timing(self, *, command_string: str, **_kwargs):
            if command_string == "no paging":
                raise RuntimeError("not permitted")
            return "quiet but incomplete output"

        def disconnect(self):
            return None

    options = SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts")
    with ArubaSshAdapter(options, credential(), connection_factory=lambda **_kwargs: PartialConnection()) as adapter:
        with pytest.raises(SshOperationError) as exc_info:
            adapter.run_read_only(SHOW_SWITCHES)
    assert exc_info.value.code == "PROMPT_NOT_FOUND"


def test_no_paging_rejection_text_selects_pager_fallback(tmp_path: Path) -> None:
    class RejectedPagingConnection:
        base_prompt = "controller#"

        def send_command_timing(self, *, command_string: str, **_kwargs):
            if command_string == "no paging":
                return "Unknown command\ncontroller#"
            return "show output\ncontroller#"

        def disconnect(self):
            return None

    options = SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts")
    with ArubaSshAdapter(
        options, credential(), connection_factory=lambda **_kwargs: RejectedPagingConnection()
    ) as adapter:
        assert adapter.run_read_only(SHOW_SWITCHES) == "show output\ncontroller#"


def test_streaming_netmiko_path_aborts_transport_at_output_limit(tmp_path: Path) -> None:
    class FakeChannel:
        def __init__(self):
            self.chunks: list[str] = []

        def read_buffer(self):
            return self.chunks.pop(0) if self.chunks else ""

    class FakeTransportClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeNetmiko:
        __module__ = "netmiko.fake"

        def __init__(self):
            self.channel = FakeChannel()
            self.remote_conn_pre = FakeTransportClient()
            self.remote_conn = object()
            self.disable_lf_normalization = True
            self.ansi_escape_codes = False
            self._read_buffer = ""

        def _prompt_handler(self, _auto_find_prompt):
            return r"controller\#"

        @staticmethod
        def normalize_cmd(command):
            return command + "\n"

        def write_channel(self, command):
            if command == "no paging\n":
                self.channel.chunks = ["no paging\ncontroller#"]
            elif command == SHOW_SWITCHES + "\n":
                self.channel.chunks = ["x" * (MAX_COMMAND_OUTPUT_CHARACTERS + 1)]

    connection = FakeNetmiko()
    options = SshConnectionOptions("192.0.2.10", 22, 10, 1, tmp_path / "known_hosts")
    adapter = ArubaSshAdapter(options, credential(), connection_factory=lambda **_kwargs: connection)
    adapter.connect()
    with pytest.raises(SshOperationError) as exc_info:
        adapter.run_read_only(SHOW_SWITCHES)
    assert exc_info.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert connection.remote_conn_pre is None


def test_streaming_netmiko_detects_pager_marker_split_across_chunks(tmp_path: Path) -> None:
    class FakeChannel:
        def __init__(self):
            self.chunks: list[str] = []

        def read_buffer(self):
            return self.chunks.pop(0) if self.chunks else ""

    class FakeTransportClient:
        def close(self):
            return None

    class FakeNetmiko:
        __module__ = "netmiko.fake"

        def __init__(self):
            self.channel = FakeChannel()
            self.remote_conn_pre = FakeTransportClient()
            self.remote_conn = object()
            self.disable_lf_normalization = True
            self.ansi_escape_codes = False
            self._read_buffer = ""
            self.writes: list[str] = []

        def _prompt_handler(self, _auto_find_prompt):
            return r"controller\#"

        @staticmethod
        def normalize_cmd(command):
            return command + "\n"

        def write_channel(self, command):
            self.writes.append(command)
            if command == "no paging\n":
                self.channel.chunks = ["no paging\ncontroller#"]
            elif command == SHOW_SWITCHES + "\n":
                self.channel.chunks = ["header\n--Mo", "re--", "\nrow\ncontroller#"]

    connection = FakeNetmiko()
    options = SshConnectionOptions("192.0.2.10", 22, 10, 1, tmp_path / "known_hosts")
    adapter = ArubaSshAdapter(options, credential(), connection_factory=lambda **_kwargs: connection)
    adapter.connect()
    try:
        output = adapter.run_read_only(SHOW_SWITCHES)
    finally:
        adapter.close()

    assert output == "header\n\nrow\ncontroller#"
    assert connection.writes == ["no paging\n", SHOW_SWITCHES + "\n", " "]


def test_host_key_scan_is_pre_authentication_and_has_no_credential_argument(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeKey:
        def get_name(self):
            return "ssh-ed25519"

        def asbytes(self):
            return b"public-key-only"

    class FakeTransport:
        def __init__(self, sock):
            calls["transport_socket"] = sock

        def start_client(self, *, event, timeout):
            calls["timeout"] = timeout
            event.set()

        def get_exception(self):
            return None

        def get_remote_server_key(self):
            return FakeKey()

        def close(self):
            calls["closed"] = True

    fake_socket = SimpleNamespace(close=lambda: None)

    def create_connection(address, timeout):
        calls["socket_address"] = address
        calls["socket_timeout"] = timeout
        return fake_socket

    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(Transport=FakeTransport))
    monkeypatch.setattr("socket.create_connection", create_connection)

    scanned = scan_ssh_host_key("192.0.2.10", 2222, timeout=5)

    assert scanned.host == "192.0.2.10"
    assert scanned.algorithm == "ssh-ed25519"
    assert calls["socket_address"] == ("192.0.2.10", 2222)
    assert "username" not in calls and "password" not in calls
    assert calls["closed"] is True
