"""Pre-authentication SSH fingerprint discovery and app-only trust store."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from ..errors import ERROR_MESSAGES
from .cancellable_socket import (
    SocketConnectCancelledError,
    close_socket_quietly,
    open_cancellable_ipv4_socket,
)


class SshHostKeyError(RuntimeError):
    default_code = "SSH_HOST_KEY_SCAN_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class SshHostKeyUntrustedError(SshHostKeyError):
    default_code = "SSH_HOST_KEY_UNKNOWN"


class SshHostKeyMismatchError(SshHostKeyError):
    default_code = "SSH_HOST_KEY_MISMATCH"


class SshHostKeyCancelledError(SshHostKeyError):
    default_code = "CANCELLED"


class SshHostKeyAlgorithmError(SshHostKeyError):
    default_code = "SSH_ALGORITHM_INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class ScannedHostKey:
    host: str
    port: int
    algorithm: str
    fingerprint: str
    key: object

    @property
    def target(self) -> str:
        return known_hosts_target(self.host, self.port)


@dataclass(frozen=True, slots=True)
class HostKeyCheck:
    status: str
    scanned: ScannedHostKey
    expected_fingerprints: tuple[str, ...] = ()


def known_hosts_target(host: str, port: int) -> str:
    normalized = str(host).strip()
    return normalized if int(port) == 22 else f"[{normalized}]:{int(port)}"


def sha256_fingerprint(key: object) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def scan_ssh_host_key(
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    cancel_event: threading.Event | None = None,
) -> ScannedHostKey:
    """Discover a key without sending a user name or credential."""

    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - packaged dependency
        raise SshHostKeyError("SSH host-key 기능을 사용할 수 없습니다.") from exc
    bounded_timeout = max(1.0, min(float(timeout), 60.0))
    if cancel_event is not None and cancel_event.is_set():
        raise SshHostKeyCancelledError("SSH 지문 확인이 취소되었습니다.")
    sock: socket.socket | None = None
    transport = None
    try:
        sock = open_cancellable_ipv4_socket(
            str(host).strip(),
            int(port),
            bounded_timeout,
            cancel_event,
        )
        transport = paramiko.Transport(sock)
        completed = threading.Event()
        transport.start_client(event=completed, timeout=bounded_timeout)
        deadline = monotonic() + bounded_timeout
        while not completed.wait(0.1):
            if cancel_event is not None and cancel_event.is_set():
                raise SshHostKeyCancelledError("SSH 지문 확인이 취소되었습니다.")
            if monotonic() >= deadline:
                raise TimeoutError("SSH host-key exchange timed out")
        exception = transport.get_exception()
        if exception is not None:
            raise exception
        key = transport.get_remote_server_key()
        return ScannedHostKey(
            host=str(host).strip(),
            port=int(port),
            algorithm=key.get_name(),
            fingerprint=sha256_fingerprint(key),
            key=key,
        )
    except SshHostKeyError:
        raise
    except SocketConnectCancelledError:
        raise SshHostKeyCancelledError("SSH 지문 확인이 취소되었습니다.") from None
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise SshHostKeyCancelledError("SSH 지문 확인이 취소되었습니다.") from None
        if _is_algorithm_incompatibility(exc):
            raise SshHostKeyAlgorithmError(
                ERROR_MESSAGES["SSH_ALGORITHM_INCOMPATIBLE"]
            ) from exc
        raise SshHostKeyError("SSH 서버 지문을 확인하지 못했습니다.") from exc
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                # A cleanup failure must not replace the sanitized scan result
                # or primary error. Close the underlying socket directly as a
                # final leak-prevention fallback.
                close_socket_quietly(sock)
        else:
            close_socket_quietly(sock)


def check_scanned_host_key(scanned: ScannedHostKey, known_hosts_path: Path) -> HostKeyCheck:
    host_keys = _load_host_keys(known_hosts_path)
    existing = host_keys.lookup(scanned.target)
    if not existing:
        return HostKeyCheck("unregistered", scanned)
    expected = existing.get(scanned.algorithm)
    if expected is None:
        return HostKeyCheck(
            "unregistered_algorithm",
            scanned,
            tuple(sorted(sha256_fingerprint(key) for key in existing.values())),
        )
    if expected == scanned.key:
        return HostKeyCheck("verified", scanned)
    return HostKeyCheck("mismatch", scanned, (sha256_fingerprint(expected),))


def register_scanned_host_key(scanned: ScannedHostKey, known_hosts_path: Path) -> Path:
    """Atomically register an explicitly approved key; never replace a key."""

    return register_scanned_host_keys((scanned,), known_hosts_path)


def register_scanned_host_keys(
    scanned_keys: Iterable[ScannedHostKey],
    known_hosts_path: Path,
) -> Path:
    """Atomically register an approved batch without replacing existing keys.

    Every conflict is checked before the temporary file is written, so an
    approval covering several MM/WLC endpoints is committed all-or-nothing.
    """

    destination = Path(known_hosts_path)
    host_keys = _load_host_keys(destination)
    approved: dict[tuple[str, str], ScannedHostKey] = {}
    for scanned in scanned_keys:
        identity = (scanned.target, scanned.algorithm)
        duplicate = approved.get(identity)
        if duplicate is not None and duplicate.key != scanned.key:
            raise SshHostKeyMismatchError(
                "같은 SSH 대상에 서로 다른 키가 제시되어 승인하지 않았습니다."
            )
        approved[identity] = scanned
        existing = host_keys.lookup(scanned.target)
        if existing and scanned.algorithm in existing and existing[scanned.algorithm] != scanned.key:
            raise SshHostKeyMismatchError(
                "저장된 SSH 키와 새 키가 달라 자동 교체하지 않았습니다."
            )
    if not approved:
        raise ValueError("승인할 SSH 호스트 키가 없습니다.")
    for scanned in approved.values():
        host_keys.add(scanned.target, scanned.algorithm, scanned.key)
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(name)
        host_keys.save(str(temporary))
        os.replace(temporary, destination)
        return destination
    except OSError as exc:
        raise SshHostKeyError("승인된 SSH 키를 저장하지 못했습니다.") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Preserve the actionable registration failure. A same-folder
                # temporary file contains only the public host key and can be
                # retried/cleaned on the next explicit approval.
                pass


def ensure_approved_host_key(
    host: str,
    port: int,
    known_hosts_path: Path,
    *,
    timeout: float = 10.0,
    approve_unknown=None,
    cancel_event: threading.Event | None = None,
) -> HostKeyCheck:
    scanned = scan_ssh_host_key(host, port, timeout=timeout, cancel_event=cancel_event)
    check = check_scanned_host_key(scanned, known_hosts_path)
    if check.status == "verified":
        return check
    if check.status == "mismatch":
        raise SshHostKeyMismatchError("SSH 서버 키가 이전 승인 값과 다릅니다.")
    if approve_unknown is None or not bool(approve_unknown(check)):
        raise SshHostKeyUntrustedError("승인되지 않은 SSH 서버 키입니다.")
    register_scanned_host_key(scanned, known_hosts_path)
    return HostKeyCheck("registered", scanned)


def has_approved_host_key(host: str, port: int, known_hosts_path: Path) -> bool:
    return bool(_load_host_keys(known_hosts_path).lookup(known_hosts_target(host, port)))


def _load_host_keys(path: Path):
    import paramiko

    host_keys = paramiko.HostKeys()
    candidate = Path(path)
    if candidate.is_file():
        try:
            host_keys.load(str(candidate))
        except (OSError, ValueError) as exc:
            raise SshHostKeyError("앱 전용 known_hosts 파일을 읽을 수 없습니다.") from exc
    return host_keys


def _is_algorithm_incompatibility(exc: BaseException) -> bool:
    class_name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    return (
        "incompatiblepeer" in class_name
        or "incompatible ssh peer" in message
        or "no acceptable" in message and "algorithm" in message
    )
