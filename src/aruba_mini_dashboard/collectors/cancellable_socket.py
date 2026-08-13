"""Bounded, cancellation-aware IPv4 TCP connection helper."""

from __future__ import annotations

import errno
import select
import socket
import threading
from collections.abc import Callable
from time import monotonic


class SocketConnectCancelledError(OSError):
    """Raised when an operator cancellation interrupts TCP connection setup."""


_CONNECT_IN_PROGRESS = {
    errno.EINPROGRESS,
    errno.EALREADY,
    errno.EWOULDBLOCK,
    getattr(errno, "WSAEINPROGRESS", 10036),
    getattr(errno, "WSAEALREADY", 10037),
    getattr(errno, "WSAEWOULDBLOCK", 10035),
}


def open_cancellable_ipv4_socket(
    host: str,
    port: int,
    timeout: float,
    cancel_event: threading.Event | None = None,
    *,
    socket_factory: Callable[..., socket.socket] = socket.socket,
    select_fn: Callable[..., tuple[list[object], list[object], list[object]]] = select.select,
    clock: Callable[[], float] = monotonic,
    register_socket: Callable[[socket.socket], None] | None = None,
) -> socket.socket:
    """Connect without leaving a blocking ``socket.connect`` cancellation gap.

    The registered raw socket can also be closed by a runtime shutdown hook to
    interrupt a later SSH banner/authentication/channel wait.  The caller owns
    the returned socket; all failure and cancellation paths close it here.
    """

    bounded_timeout = max(0.1, float(timeout))
    sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    handed_off = False
    try:
        if register_socket is not None:
            register_socket(sock)
        if cancel_event is not None and cancel_event.is_set():
            raise SocketConnectCancelledError("TCP connection was cancelled")
        sock.setblocking(False)
        result = int(sock.connect_ex((str(host).strip(), int(port))))
        if result not in (0, errno.EISCONN) and result not in _CONNECT_IN_PROGRESS:
            raise OSError(result, "TCP connection failed")
        deadline = clock() + bounded_timeout
        while result not in (0, errno.EISCONN):
            if cancel_event is not None and cancel_event.is_set():
                raise SocketConnectCancelledError("TCP connection was cancelled")
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("TCP connection timed out")
            _readable, writable, exceptional = select_fn(
                [], [sock], [sock], min(0.05, remaining)
            )
            if not writable and not exceptional:
                continue
            result = int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR))
            if result:
                raise OSError(result, "TCP connection failed")
        if cancel_event is not None and cancel_event.is_set():
            raise SocketConnectCancelledError("TCP connection was cancelled")
        sock.setblocking(True)
        sock.settimeout(bounded_timeout)
        handed_off = True
        return sock
    finally:
        if not handed_off:
            _close_socket(sock)


def close_socket_quietly(sock: socket.socket | None) -> None:
    if sock is not None:
        _close_socket(sock)


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
