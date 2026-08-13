from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aruba_mini_dashboard.collectors.aruba_ssh import (
    ArubaSshAdapter,
    classify_ssh_exception,
)
from aruba_mini_dashboard.collectors.base import (
    SHOW_SWITCHES,
    CollectionBundle,
    CommandResult,
    SshConnectionOptions,
    SshOperationError,
)
from aruba_mini_dashboard.collectors.ssh_host_keys import (
    ScannedHostKey,
    SshHostKeyError,
    register_scanned_host_key,
    scan_ssh_host_key,
)
from aruba_mini_dashboard.credentials import DeviceCredential
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.models import ParseStatus
from aruba_mini_dashboard.parsers import parse_load_distribution, parse_show_switches
from aruba_mini_dashboard.parsers.common import ParserCancelledError, parse_nonnegative_int


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0", 0),
        ("1,234", 1234),
        ("12,345,678", 12345678),
        ("1,,234", None),
        ("12,34", None),
        (",123", None),
        ("123,", None),
        ("9" * 21, None),
    ),
)
def test_nonnegative_integer_parser_rejects_ambiguous_or_oversized_values(
    value: str,
    expected: int | None,
) -> None:
    assert parse_nonnegative_int(value) == expected


def test_load_parser_contains_python_large_decimal_conversion_failure() -> None:
    output = (
        "IP Address       Active Clients    Standby Clients\n"
        "192.0.2.11       1                 "
        + ("9" * 5_000)
        + "\n"
        "192.0.2.12       2                 3\n"
    )

    result = parse_load_distribution(output)

    assert result.status is ParseStatus.PARTIAL
    assert [row.ip for row in result.rows] == ["192.0.2.12"]
    assert any(issue.code == "INVALID_CLIENT_COUNT" for issue in result.issues)


def test_oversized_unbroken_table_row_is_skipped_without_hiding_valid_rows() -> None:
    output = (
        "IP Address       Active Clients    Standby Clients\n"
        + ("192.0.2.11 " + "9" * 9_000)
        + "\n"
        "192.0.2.12       2                 3\n"
    )

    result = parse_load_distribution(output)

    assert result.status is ParseStatus.PARTIAL
    assert [row.ip for row in result.rows] == ["192.0.2.12"]
    assert any(issue.code == "PARSE_ROW_TOO_LONG" for issue in result.issues)


def test_oversized_preamble_does_not_block_a_later_valid_header() -> None:
    output = (
        ("X" * 100_000)
        + "\nSwitch IP       Name       Status\n"
        "192.0.2.11      WLC-01     Up\n"
    )

    result = parse_show_switches(output)

    assert result.status is ParseStatus.COMPLETE
    assert [(row.ip, row.status) for row in result.rows] == [("192.0.2.11", "Up")]


def test_malformed_reported_total_marks_load_result_partial() -> None:
    output = (
        "IP Address       Active Clients    Standby Clients\n"
        "192.0.2.11       10                20\n"
        "Total: Active Clients 1,,000\n"
    )

    result = parse_load_distribution(output)

    assert result.status is ParseStatus.PARTIAL
    assert result.metadata["reported_total_active"] is None
    assert any(issue.code == "INVALID_TOTAL_ACTIVE" for issue in result.issues)


def test_large_table_parser_observes_cooperative_cancellation() -> None:
    output = (
        "IP Address       Active Clients    Standby Clients\n"
        + "192.0.2.11       1                 2\n" * 1_000
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(ParserCancelledError):
        parse_load_distribution(output, cancel_event=cancel_event)


def test_runtime_propagates_parser_cancellation_without_collection_error() -> None:
    bundle = CollectionBundle(
        source="mm",
        requested_controller_ip="192.0.2.10",
        commands={SHOW_SWITCHES: CommandResult(SHOW_SWITCHES, True, output="bounded output")},
    )
    cancel_event = threading.Event()
    cancel_event.set()
    errors = []
    runtime = object.__new__(RuntimePoller)

    with pytest.raises(RuntimeError, match="취소"):
        runtime._parse_command(
            bundle,
            SHOW_SWITCHES,
            parse_show_switches,
            errors,
            cancellation_event=cancel_event,
        )

    assert errors == []


def test_allowed_show_command_rejection_is_not_returned_as_success(tmp_path: Path) -> None:
    class RejectedCommandConnection:
        def send_command_timing(self, *, command_string: str, **_kwargs):
            assert command_string == "no paging"
            return ""

        def send_command(self, *, command_string: str, **_kwargs):
            assert command_string == SHOW_SWITCHES
            return "% Invalid input detected\ncontroller#"

        def disconnect(self) -> None:
            return None

    adapter = ArubaSshAdapter(
        SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts"),
        DeviceCredential("operator", "password"),
        connection_factory=lambda **_kwargs: RejectedCommandConnection(),
    )
    adapter.connect()
    try:
        with pytest.raises(SshOperationError) as exc_info:
            adapter.run_read_only(SHOW_SWITCHES)
    finally:
        adapter.close()

    assert exc_info.value.code == "COMMAND_REJECTED"
    assert exc_info.value.retryable is False
    assert "Invalid input" not in exc_info.value.user_message


def test_banner_failure_has_stable_sanitized_diagnostic_code() -> None:
    error = classify_ssh_exception(
        RuntimeError("Error reading SSH protocol banner: private peer detail"),
        operation="connect",
    )

    assert error.code == "SSH_BANNER_MISSING"
    assert error.retryable is True
    assert "private" not in error.user_message.casefold()


def test_paging_setup_transport_error_after_cancel_is_canonical_cancelled(
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()

    class CancelledPagingConnection:
        def send_command_timing(self, **_kwargs):
            cancel_event.set()
            raise OSError("raw private transport detail")

        def disconnect(self) -> None:
            return None

    adapter = ArubaSshAdapter(
        SshConnectionOptions("192.0.2.10", 22, 10, 20, tmp_path / "known_hosts"),
        DeviceCredential("operator", "password"),
        connection_factory=lambda **_kwargs: CancelledPagingConnection(),
        cancel_event=cancel_event,
    )

    with pytest.raises(SshOperationError) as exc_info:
        adapter.connect()

    assert exc_info.value.code == "CANCELLED"
    assert exc_info.value.retryable is False
    assert "private" not in exc_info.value.user_message.casefold()


def test_connect_does_not_rewrite_existing_known_hosts_metadata(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    os.utime(known_hosts, (1_700_000_000, 1_700_000_000))
    previous_mtime = known_hosts.stat().st_mtime_ns

    class Connection:
        def send_command_timing(self, **_kwargs):
            return ""

        def disconnect(self) -> None:
            return None

    adapter = ArubaSshAdapter(
        SshConnectionOptions("192.0.2.10", 22, 10, 20, known_hosts),
        DeviceCredential("operator", "password"),
        connection_factory=lambda **_kwargs: Connection(),
    )
    adapter.connect()
    adapter.close()

    assert known_hosts.stat().st_mtime_ns == previous_mtime


def test_known_hosts_directory_is_rejected_before_connection_retry(
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.mkdir()
    factory_called = False

    def factory(**_kwargs):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("connection factory must not run")

    adapter = ArubaSshAdapter(
        SshConnectionOptions("192.0.2.10", 22, 10, 20, known_hosts),
        DeviceCredential("operator", "password"),
        connection_factory=factory,
    )

    with pytest.raises(SshOperationError) as exc_info:
        adapter.connect()

    assert exc_info.value.code == "SSH_KNOWN_HOSTS_UNAVAILABLE"
    assert exc_info.value.retryable is False
    assert factory_called is False


def test_unexpected_parser_exception_is_isolated_without_raw_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "RAW-DEVICE-OUTPUT-CANARY"
    bundle = CollectionBundle(
        source="mm",
        requested_controller_ip="192.0.2.10",
        actual_controller_ip="192.0.2.10",
        commands={SHOW_SWITCHES: CommandResult(SHOW_SWITCHES, True, output="bounded output")},
    )

    def broken_parser(_output: str):
        raise ValueError(canary)

    errors = []
    runtime = object.__new__(RuntimePoller)
    with caplog.at_level(logging.ERROR):
        parsed = runtime._parse_command(bundle, SHOW_SWITCHES, broken_parser, errors)

    assert parsed is None
    assert len(errors) == 1
    assert errors[0].code == "PARSE_FAILED"
    assert errors[0].target_ip == "192.0.2.10"
    assert canary not in caplog.text


def test_host_key_registration_path_failure_is_sanitized(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    scanned = ScannedHostKey(
        host="192.0.2.10",
        port=22,
        algorithm="ssh-ed25519",
        fingerprint="SHA256:public-fingerprint",
        key=object(),
    )

    with pytest.raises(SshHostKeyError) as exc_info:
        register_scanned_host_key(scanned, parent_file / "known_hosts")

    assert str(parent_file) not in str(exc_info.value)


def test_host_key_transport_cleanup_failure_closes_raw_socket_without_masking_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class FakeKey:
        def get_name(self) -> str:
            return "ssh-ed25519"

        def asbytes(self) -> bytes:
            return b"public-key-only"

    class FailingCloseTransport:
        def __init__(self, _sock: FakeSocket) -> None:
            return None

        def start_client(self, *, event, timeout: float) -> None:
            assert timeout == 5
            event.set()

        def get_exception(self):
            return None

        def get_remote_server_key(self) -> FakeKey:
            return FakeKey()

        def close(self) -> None:
            raise OSError("simulated transport cleanup failure")

    raw_socket = FakeSocket()
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(Transport=FailingCloseTransport))
    monkeypatch.setattr(
        "aruba_mini_dashboard.collectors.ssh_host_keys.open_cancellable_ipv4_socket",
        lambda *_args, **_kwargs: raw_socket,
    )

    scanned = scan_ssh_host_key("192.0.2.10", 22, timeout=5)

    assert scanned.algorithm == "ssh-ed25519"
    assert raw_socket.closed is True
