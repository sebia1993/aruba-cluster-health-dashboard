from __future__ import annotations

import errno
import socket
import threading
import time

import pytest

from aruba_mini_dashboard.collectors.aruba_ssh import ArubaSshAdapter
from aruba_mini_dashboard.collectors.base import SshConnectionOptions, SshOperationError
from aruba_mini_dashboard.collectors.cancellable_socket import (
    SocketConnectCancelledError,
    open_cancellable_ipv4_socket,
)
from aruba_mini_dashboard.collectors.ssh_host_keys import (
    SshHostKeyCancelledError,
    scan_ssh_host_key,
)
from aruba_mini_dashboard.credentials import DeviceCredential


class _PendingSocket:
    def __init__(self) -> None:
        self.closed = False
        self.blocking: list[bool] = []

    def setblocking(self, value: bool) -> None:
        self.blocking.append(value)

    def settimeout(self, _value: float) -> None:
        return None

    def connect_ex(self, _address: tuple[str, int]) -> int:
        return errno.EINPROGRESS

    def getsockopt(self, *_args: object) -> int:
        return 0

    def shutdown(self, _how: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_pending_tcp_connect_is_cancelled_and_closed_without_busy_loop() -> None:
    cancel_event = threading.Event()
    pending = _PendingSocket()
    select_calls = 0

    def select_fn(*_args):
        nonlocal select_calls
        select_calls += 1
        cancel_event.set()
        return [], [], []

    with pytest.raises(SocketConnectCancelledError):
        open_cancellable_ipv4_socket(
            "192.0.2.10",
            22,
            600,
            cancel_event,
            socket_factory=lambda *_args: pending,
            select_fn=select_fn,
        )

    assert select_calls == 1
    assert pending.closed is True


def test_local_pending_ssh_banner_cancel_returns_promptly() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    accepted = threading.Event()
    release = threading.Event()

    def server() -> None:
        connection, _address = listener.accept()
        accepted.set()
        release.wait(2)
        connection.close()

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()
    cancel_event = threading.Event()
    failures: list[BaseException] = []

    def scan() -> None:
        try:
            scan_ssh_host_key(host, port, timeout=5, cancel_event=cancel_event)
        except BaseException as exc:  # asserted below
            failures.append(exc)

    scan_thread = threading.Thread(target=scan)
    scan_thread.start()
    assert accepted.wait(5)

    started = time.monotonic()
    cancel_event.set()
    scan_thread.join(1)
    assert not scan_thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert len(failures) == 1
    assert isinstance(failures[0], SshHostKeyCancelledError)

    release.set()
    server_thread.join(1)
    listener.close()
    assert not server_thread.is_alive()


def test_adapter_abort_interrupts_real_netmiko_banner_wait(
    tmp_path,
) -> None:
    # Keep this regression about a blocked network banner rather than the
    # one-time lazy import cost of the complete Netmiko driver registry.
    import netmiko  # noqa: F401

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    accepted = threading.Event()
    release = threading.Event()

    def server() -> None:
        connection, _address = listener.accept()
        accepted.set()
        release.wait(3)
        connection.close()

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()
    cancel_event = threading.Event()
    adapter = ArubaSshAdapter(
        SshConnectionOptions(
            host,
            port,
            5,
            5,
            tmp_path / "known_hosts",
        ),
        DeviceCredential("operator", "fixture-password"),
        cancel_event=cancel_event,
    )
    failures: list[BaseException] = []

    def connect() -> None:
        try:
            adapter.connect()
        except BaseException as exc:  # asserted below
            failures.append(exc)

    client_thread = threading.Thread(target=connect)
    client_thread.start()
    assert accepted.wait(5)
    started = time.monotonic()
    cancel_event.set()
    adapter.abort()
    client_thread.join(2)
    assert not client_thread.is_alive()
    assert time.monotonic() - started < 2.0
    assert len(failures) == 1
    assert isinstance(failures[0], SshOperationError)
    assert failures[0].code == "CANCELLED"

    adapter.close()
    release.set()
    server_thread.join(1)
    listener.close()
