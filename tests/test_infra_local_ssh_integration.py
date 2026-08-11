from __future__ import annotations

import socket
import threading
from pathlib import Path

import paramiko
import pytest

from aruba_mini_dashboard.collectors.aruba_ssh import ArubaSshAdapter
from aruba_mini_dashboard.collectors.base import SHOW_SWITCHES, SshConnectionOptions
from aruba_mini_dashboard.collectors.ssh_host_keys import (
    ScannedHostKey,
    register_scanned_host_key,
    sha256_fingerprint,
)
from aruba_mini_dashboard.credentials import DeviceCredential


class _ServerInterface(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.shell_requested = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        return (
            paramiko.AUTH_SUCCESSFUL
            if (username, password) == ("integration-user", "integration-password")
            else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, _username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, _channel_id: int) -> int:
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *_args) -> bool:
        return True

    def check_channel_shell_request(self, _channel) -> bool:
        self.shell_requested.set()
        return True


class LocalArubaSshServer:
    """Small local SSH peer; it accepts only the read-only test dialogue."""

    prompt = "(controller) #"

    def __init__(self) -> None:
        self.host_key = paramiko.RSAKey.generate(2048)
        self.commands: list[str] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.25)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name="local-aruba-ssh", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        transport = None
        channel = None
        try:
            while not self._stop.is_set():
                try:
                    client, _address = self._listener.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            server = _ServerInterface()
            transport.start_server(server=server)
            channel = transport.accept(timeout=5)
            if channel is None or not server.shell_requested.wait(5):
                raise RuntimeError("shell was not requested")
            channel.settimeout(0.25)
            channel.send(("ArubaOS local integration peer\r\n" + self.prompt).encode("utf-8"))
            pending = ""
            while not self._stop.is_set() and transport.is_active():
                try:
                    data = channel.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                pending += data.decode("utf-8", errors="replace")
                while True:
                    positions = [position for marker in ("\r", "\n") if (position := pending.find(marker)) >= 0]
                    if not positions:
                        break
                    position = min(positions)
                    command = pending[:position].strip()
                    pending = pending[position + 1 :].lstrip("\r\n")
                    self._respond(channel, command)
        except BaseException as exc:
            if not self._stop.is_set():
                self.errors.append(exc)
        finally:
            if channel is not None:
                channel.close()
            if transport is not None:
                transport.close()

    def _respond(self, channel: paramiko.Channel, command: str) -> None:
        if not command:
            channel.send(("\r\n" + self.prompt).encode("utf-8"))
            return
        self.commands.append(command)
        if command == "no paging":
            response = f"{command}\r\n{self.prompt}"
        elif command == SHOW_SWITCHES:
            response = (
                f"{command}\r\n"
                "Switch IP       Name       Status\r\n"
                "--------------- ---------- ------\r\n"
                "192.0.2.11      WLC-01     Up\r\n"
                f"{self.prompt}"
            )
        else:
            response = f"{command}\r\nUnknown command\r\n{self.prompt}"
        channel.send(response.encode("utf-8"))


@pytest.mark.integration
def test_real_netmiko_adapter_against_local_paramiko_server(tmp_path: Path) -> None:
    server = LocalArubaSshServer()
    known_hosts = tmp_path / "known_hosts"
    scanned = ScannedHostKey(
        host="127.0.0.1",
        port=server.port,
        algorithm=server.host_key.get_name(),
        fingerprint=sha256_fingerprint(server.host_key),
        key=server.host_key,
    )
    register_scanned_host_key(scanned, known_hosts)
    server.start()
    adapter = ArubaSshAdapter(
        SshConnectionOptions(
            host="127.0.0.1",
            port=server.port,
            connect_timeout_seconds=5,
            command_timeout_seconds=5,
            known_hosts_path=known_hosts,
        ),
        DeviceCredential("integration-user", "integration-password"),
    )
    try:
        adapter.connect()
        output = adapter.run_read_only(SHOW_SWITCHES)
    finally:
        adapter.close()
        server.close()

    assert "192.0.2.11" in output
    assert SHOW_SWITCHES in server.commands
    assert server.commands.count("no paging") >= 1
    assert set(server.commands) <= {"no paging", SHOW_SWITCHES}
    assert server.errors == []
