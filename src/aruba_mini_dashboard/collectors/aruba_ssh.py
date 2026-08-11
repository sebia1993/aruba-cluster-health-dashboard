"""Strict read-only Netmiko adapter for ArubaOS devices."""

from __future__ import annotations

import logging
import re
import socket
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from ..credentials import DeviceCredential
from .base import (
    NO_PAGING,
    PAGER_CONTINUE,
    READ_ONLY_COMMANDS,
    SshConnectionOptions,
    SshOperationError,
)


MAX_COMMAND_OUTPUT_CHARACTERS = 4 * 1024 * 1024
MAX_COMMAND_OUTPUT_LINES = 50_000
MAX_PAGER_CONTINUATIONS = 1_000
PAGING_MARKERS = ("--more--", "-- more --", "press any key", "press <space>", "<--- more --->")
COMMAND_REJECTION_MARKERS = (
    "unknown command",
    "invalid input",
    "incomplete command",
    "permission denied",
    "not authorized",
    "authorization failed",
)
_PAGING_MARKER_PATTERN = re.compile(
    r"(?i)(?:--\s*more\s*--|press\s+(?:any\s+key|<space>)[^\r\n]*|<---\s*more\s*--->)"
)


class ArubaSshAdapter:
    def __init__(
        self,
        options: SshConnectionOptions,
        credential: DeviceCredential,
        *,
        connection_factory: Callable[..., object] | None = None,
        cancel_event: threading.Event | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.options = options
        self._credential = credential
        self._connection_factory = connection_factory
        self._cancel_event = cancel_event
        self._logger = logger or logging.getLogger(__name__)
        self._connection: object | None = None
        self._paging_disabled = False

    def connect(self) -> None:
        self._check_cancelled()
        if self._connection is not None:
            return
        if self.options.enable_required and not self._credential.enable_secret:
            raise SshOperationError(
                "ENABLE_SECRET_MISSING",
                "Enable 비밀번호가 필요하지만 입력되지 않았습니다.",
                retryable=False,
                operation="enable",
            )
        known_hosts = Path(self.options.known_hosts_path)
        try:
            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            # Netmiko/Paramiko requires a real path for alt_host_keys. Empty
            # means no host is trusted and ssh_strict fails before login.
            known_hosts.touch(exist_ok=True)
        except OSError as exc:
            raise SshOperationError(
                "SSH_KNOWN_HOSTS_UNAVAILABLE",
                "앱 전용 SSH known_hosts 파일을 준비할 수 없습니다.",
                retryable=False,
                operation="connect",
            ) from exc
        factory = self._connection_factory
        deferred_netmiko_session = factory is None
        if factory is None:
            try:
                from netmiko import ConnectHandler
            except ImportError as exc:  # pragma: no cover - packaged dependency
                raise SshOperationError(
                    "SSH_RUNTIME_MISSING", "SSH 실행 모듈을 찾을 수 없습니다.", retryable=False, operation="connect"
                ) from exc
            factory = ConnectHandler
        params = {
            "device_type": self.options.device_type,
            "host": self.options.host,
            "port": self.options.port,
            "username": self._credential.username,
            "password": self._credential.password,
            "secret": self._credential.enable_secret or None,
            "timeout": self.options.connect_timeout_seconds,
            "conn_timeout": self.options.connect_timeout_seconds,
            "auth_timeout": self.options.connect_timeout_seconds,
            "banner_timeout": self.options.connect_timeout_seconds,
            "read_timeout_override": self.options.command_timeout_seconds,
            "fast_cli": False,
            "ssh_strict": True,
            "system_host_keys": False,
            "alt_host_keys": True,
            "alt_key_file": str(known_hosts),
            "use_keys": False,
            "allow_agent": False,
        }
        if deferred_netmiko_session:
            # ArubaOsSSH.session_preparation() always calls enable().  Create
            # the standard driver without opening it so this adapter can apply
            # the configured optional-enable policy before any CLI command is
            # sent.  Netmiko explicitly supports auto_connect=False.
            params["auto_connect"] = False
        try:
            self._connection = factory(**params)
            if deferred_netmiko_session:
                self._open_netmiko_session(self._connection)
            self._check_cancelled()
            if self.options.enable_required and not deferred_netmiko_session:
                self._connection.enable()
            self._disable_paging()
        except SshOperationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise classify_ssh_exception(exc, operation="connect") from exc

    def _open_netmiko_session(self, connection: object) -> None:
        """Open Netmiko transport while replacing Aruba's forced-enable prep."""

        if not _is_netmiko_connection(connection):
            raise SshOperationError(
                "SSH_RUNTIME_INCOMPATIBLE",
                "SSH 실행 모듈이 필요한 세션 준비 기능을 제공하지 않습니다.",
                retryable=False,
                operation="connect",
            )
        modify_params = getattr(connection, "_modify_connection_params", None)
        establish_connection = getattr(connection, "establish_connection", None)
        test_channel = getattr(connection, "_test_channel_read", None)
        set_base_prompt = getattr(connection, "set_base_prompt", None)
        enable = getattr(connection, "enable", None)
        if not all(callable(value) for value in (modify_params, establish_connection, test_channel, set_base_prompt)):
            raise SshOperationError(
                "SSH_RUNTIME_INCOMPATIBLE",
                "SSH 실행 모듈이 필요한 세션 준비 기능을 제공하지 않습니다.",
                retryable=False,
                operation="connect",
            )
        if self.options.enable_required and not callable(enable):
            raise SshOperationError(
                "SSH_RUNTIME_INCOMPATIBLE",
                "SSH 실행 모듈이 Enable 세션 준비 기능을 제공하지 않습니다.",
                retryable=False,
                operation="enable",
            )

        try:
            modify_params()
            self._check_cancelled()
            establish_connection()
            self._check_cancelled()

            # Match the safe parts of Netmiko 4.6 ArubaOsSSH preparation.  The
            # read_timeout_override supplied above bounds each prompt read.
            # ANSI cleanup is enabled before reading the first device prompt.
            setattr(connection, "ansi_escape_codes", True)
            test_channel(pattern=r"[>#]")
            self._check_cancelled()
            set_base_prompt()
            self._check_cancelled()
            if self.options.enable_required:
                enable()
                self._check_cancelled()
        except SshOperationError:
            self._abort_netmiko_transport(connection)
            raise
        except Exception:
            self._abort_netmiko_transport(connection)
            raise

    def _disable_paging(self) -> None:
        connection = self._require_connection()
        try:
            if _is_netmiko_connection(connection):
                output = self._run_bounded_netmiko_prompt(connection, NO_PAGING)
            else:
                output = connection.send_command_timing(
                    command_string=NO_PAGING,
                    strip_prompt=False,
                    strip_command=False,
                    cmd_verify=False,
                    read_timeout=self.options.command_timeout_seconds,
                )
            validate_bounded_output(output, allow_empty=True)
            if command_was_rejected(output):
                raise SshOperationError(
                    "PAGING_SETUP_REJECTED",
                    "장비가 페이징 해제 명령을 허용하지 않았습니다.",
                    retryable=False,
                    operation="paging",
                )
            self._paging_disabled = True
        except Exception as exc:
            # Older Aruba releases and restricted read-only roles may reject
            # ``no paging``.  Continue with an explicitly bounded marker/Space
            # loop; never treat the failure as device health evidence.
            self._paging_disabled = False
            self._logger.info("Session paging command unavailable; using bounded pager handling (%s)", type(exc).__name__)

    def run_read_only(self, command: str) -> str:
        validate_read_only_command(command)
        self._check_cancelled()
        connection = self._require_connection()
        started = monotonic()
        try:
            if _is_netmiko_connection(connection):
                output = self._run_bounded_netmiko_prompt(connection, command)
            elif self._paging_disabled:
                output = connection.send_command(
                    command_string=command,
                    strip_prompt=False,
                    strip_command=False,
                    cmd_verify=False,
                    auto_find_prompt=False,
                    read_timeout=self.options.command_timeout_seconds,
                )
            else:
                output = self._run_bounded_pager_command(connection, command)
            validate_bounded_output(output)
            if contains_paging_marker(output):
                raise SshOperationError(
                    "PAGING_INCOMPLETE",
                    "명령 출력에 처리되지 않은 페이징 표시가 있어 결과를 사용하지 않습니다.",
                    retryable=True,
                    operation="command",
                )
            self._logger.debug("Read-only SSH command completed in %.3fs", monotonic() - started)
            return output
        except SshOperationError:
            raise
        except Exception as exc:
            raise classify_ssh_exception(exc, operation="command") from exc

    def _run_bounded_netmiko_prompt(self, connection: object, command: str) -> str:
        """Stream one Netmiko buffer at a time under prompt/size/deadline gates."""

        channel = _stored_attribute(connection, "channel")
        read_buffer = getattr(channel, "read_buffer", None) if channel is not None else None
        prompt_handler = getattr(connection, "_prompt_handler", None)
        normalize_cmd = getattr(connection, "normalize_cmd", None)
        write_channel = getattr(connection, "write_channel", None)
        if not all(callable(value) for value in (read_buffer, prompt_handler, normalize_cmd, write_channel)):
            self._abort_netmiko_transport(connection)
            raise SshOperationError(
                "BOUNDED_READ_UNAVAILABLE",
                "현재 SSH 모듈에서 안전한 출력 제한 기능을 사용할 수 없습니다.",
                retryable=False,
                operation="command",
            )
        try:
            prompt_pattern = prompt_handler(False)
            if not isinstance(prompt_pattern, str) or not prompt_pattern:
                raise ValueError("prompt boundary is empty")
            re.compile(prompt_pattern)
            buffered = _take_stored_read_buffer(connection)
            stale_parts: list[str] = []
            if buffered:
                stale_parts.append(buffered)
            # Drain only immediately available stale data. A permanently busy
            # pre-command channel is rejected rather than read without bound.
            for index in range(128):
                chunk = read_buffer()
                if not chunk:
                    break
                if not isinstance(chunk, str):
                    raise TypeError("SSH channel returned non-text data")
                stale_parts.append(chunk)
                _validate_stream_parts(stale_parts)
            else:
                raise SshOperationError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "명령 전 SSH 채널 데이터가 안전 한도를 초과했습니다.",
                    retryable=False,
                    operation="command",
                )
            write_channel(normalize_cmd(command))
        except SshOperationError:
            self._abort_netmiko_transport(connection)
            raise
        except Exception as exc:
            self._abort_netmiko_transport(connection)
            raise classify_ssh_exception(exc, operation="command") from exc

        deadline = monotonic() + self.options.command_timeout_seconds
        output_parts: list[str] = []
        search_window = ""
        pages = 0
        while monotonic() < deadline:
            self._check_cancelled()
            try:
                chunk = read_buffer()
            except Exception as exc:
                self._abort_netmiko_transport(connection)
                raise classify_ssh_exception(exc, operation="command") from exc
            if not isinstance(chunk, str):
                self._abort_netmiko_transport(connection)
                raise SshOperationError(
                    "INVALID_OUTPUT",
                    "장비가 올바르지 않은 형식의 응답을 반환했습니다.",
                    retryable=True,
                    operation="command",
                )
            if not chunk:
                sleep(0.025)
                continue
            output_parts.append(chunk)
            try:
                _validate_stream_parts(output_parts)
                normalized_window = _normalize_netmiko_output(connection, (search_window + chunk)[-65536:])
            except SshOperationError:
                self._abort_netmiko_transport(connection)
                raise
            except Exception as exc:
                self._abort_netmiko_transport(connection)
                raise classify_ssh_exception(exc, operation="command") from exc
            if contains_paging_marker(normalized_window):
                pages += 1
                if pages > MAX_PAGER_CONTINUATIONS:
                    self._abort_netmiko_transport(connection)
                    raise SshOperationError(
                        "PAGING_INCOMPLETE",
                        "명령 출력의 페이지 수가 안전 한도를 초과했습니다.",
                        retryable=False,
                        operation="command",
                    )
                write_channel(PAGER_CONTINUE)
                search_window = ""
                continue
            search_window = normalized_window
            if re.search(prompt_pattern, normalized_window):
                output = _normalize_netmiko_output(connection, "".join(output_parts))
                return strip_paging_markers(validate_bounded_output(output, allow_empty=(command == NO_PAGING)))
        self._abort_netmiko_transport(connection)
        raise SshOperationError(
            "COMMAND_TIMEOUT", "명령 실행 시간이 초과되었습니다.", retryable=True, operation="command"
        )

    def _run_bounded_pager_command(self, connection: object, command: str) -> str:
        deadline = monotonic() + self.options.command_timeout_seconds
        output = self._timing_read(connection, command, deadline=deadline, normalize=True)
        pages = 0
        while contains_paging_marker(output):
            self._check_cancelled()
            pages += 1
            if pages > MAX_PAGER_CONTINUATIONS:
                raise SshOperationError(
                    "PAGING_INCOMPLETE",
                    "명령 출력의 페이지 수가 안전 한도를 초과했습니다.",
                    retryable=False,
                    operation="command",
                )
            output = strip_paging_markers(output)
            next_chunk = self._timing_read(connection, PAGER_CONTINUE, deadline=deadline, normalize=False)
            if not next_chunk:
                raise SshOperationError(
                    "PAGING_INCOMPLETE",
                    "장비의 다음 출력 페이지를 받지 못했습니다.",
                    retryable=True,
                    operation="command",
                )
            output += next_chunk
            validate_bounded_output(output)
        output = strip_paging_markers(output)
        base_prompt = str(getattr(connection, "base_prompt", "") or "").strip()
        if base_prompt and base_prompt not in output[-4096:]:
            raise SshOperationError(
                "PROMPT_NOT_FOUND",
                "장비 명령 프롬프트를 확인하지 못해 결과를 사용하지 않습니다.",
                retryable=True,
                operation="command",
            )
        return output

    def _timing_read(self, connection: object, command: str, *, deadline: float, normalize: bool) -> str:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SshOperationError(
                "COMMAND_TIMEOUT", "명령 실행 시간이 초과되었습니다.", retryable=True, operation="command"
            )
        output = connection.send_command_timing(
            command_string=command,
            strip_prompt=False,
            strip_command=False,
            cmd_verify=False,
            normalize=normalize,
            last_read=min(1.0, max(0.1, remaining)),
            read_timeout=max(0.1, remaining),
        )
        return validate_bounded_output(output, allow_empty=(command == PAGER_CONTINUE))

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._paging_disabled = False
        if connection is not None:
            try:
                if _is_netmiko_connection(connection):
                    # Netmiko's generic disconnect path writes ``exit``.  The
                    # application command boundary intentionally permits only
                    # the three show commands plus session ``no paging`` and
                    # pager Space, so close the authenticated transport without
                    # sending another CLI command.
                    transport_client = getattr(connection, "remote_conn_pre", None)
                    if transport_client is None or not callable(getattr(transport_client, "close", None)):
                        raise RuntimeError("Netmiko raw transport close is unavailable")
                    transport_client.close()
                else:
                    connection.disconnect()
            except Exception:
                self._logger.debug("SSH disconnect failed", exc_info=True)

    def _abort_netmiko_transport(self, connection: object) -> None:
        attributes = _stored_attributes(connection)
        transport_client = None if attributes is None else attributes.get("remote_conn_pre")
        if transport_client is not None and callable(getattr(transport_client, "close", None)):
            try:
                transport_client.close()
            except Exception:
                self._logger.debug("SSH transport abort failed", exc_info=True)
        if attributes is not None:
            attributes["remote_conn"] = None
            attributes["remote_conn_pre"] = None

    def _require_connection(self):
        if self._connection is None:
            raise SshOperationError(
                "SSH_NOT_CONNECTED", "SSH 연결이 설정되지 않았습니다.", retryable=True, operation="command"
            )
        return self._connection

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise SshOperationError("CANCELLED", "점검이 취소되었습니다.", retryable=False, operation="cancel")

    def __enter__(self) -> "ArubaSshAdapter":
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def validate_read_only_command(command: str) -> str:
    if command != command.strip() or command not in READ_ONLY_COMMANDS:
        raise SshOperationError(
            "COMMAND_NOT_ALLOWED",
            "허용되지 않은 장비 명령은 실행할 수 없습니다.",
            retryable=False,
            operation="command",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in command):
        raise SshOperationError(
            "COMMAND_NOT_ALLOWED", "허용되지 않은 장비 명령은 실행할 수 없습니다.", retryable=False, operation="command"
        )
    return command


def validate_bounded_output(output: object, *, allow_empty: bool = False) -> str:
    if not isinstance(output, str):
        raise SshOperationError(
            "INVALID_OUTPUT", "장비가 올바르지 않은 형식의 응답을 반환했습니다.", retryable=True, operation="command"
        )
    if len(output) > MAX_COMMAND_OUTPUT_CHARACTERS or output.count("\n") + 1 > MAX_COMMAND_OUTPUT_LINES:
        raise SshOperationError(
            "OUTPUT_LIMIT_EXCEEDED",
            "명령 결과가 안전한 최대 크기를 초과했습니다.",
            retryable=False,
            operation="command",
        )
    if not allow_empty and not output.strip():
        raise SshOperationError("EMPTY_OUTPUT", "장비가 빈 명령 결과를 반환했습니다.", retryable=True, operation="command")
    return output


def contains_paging_marker(output: str) -> bool:
    return bool(_PAGING_MARKER_PATTERN.search(output))


def strip_paging_markers(output: str) -> str:
    return _PAGING_MARKER_PATTERN.sub("", output).replace("\x08", "")


def command_was_rejected(output: str) -> bool:
    normalized = output.casefold()
    return any(marker in normalized for marker in COMMAND_REJECTION_MARKERS)


def _is_netmiko_connection(connection: object) -> bool:
    return any(
        isinstance(getattr(cls, "__module__", None), str)
        and (cls.__module__ == "netmiko" or cls.__module__.startswith("netmiko."))
        for cls in type(connection).__mro__
    )


def _stored_attributes(value: object) -> dict[str, object] | None:
    try:
        attributes = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    return attributes if isinstance(attributes, dict) else None


def _stored_attribute(value: object, name: str) -> object:
    attributes = _stored_attributes(value)
    return None if attributes is None else attributes.get(name)


def _take_stored_read_buffer(connection: object) -> str:
    attributes = _stored_attributes(connection)
    if attributes is None:
        return ""
    buffered = attributes.get("_read_buffer", "")
    attributes["_read_buffer"] = ""
    if not isinstance(buffered, str):
        raise TypeError("Netmiko stored read buffer is not text")
    return buffered


def _normalize_netmiko_output(connection: object, output: str) -> str:
    attributes = _stored_attributes(connection)
    if attributes is None:
        raise RuntimeError("Netmiko transform state is unavailable")
    if attributes.get("disable_lf_normalization") is False:
        normalizer = getattr(connection, "normalize_linefeeds", None)
        if not callable(normalizer):
            raise RuntimeError("Netmiko linefeed normalizer is unavailable")
        output = normalizer(output)
    if attributes.get("ansi_escape_codes") is True:
        stripper = getattr(connection, "strip_ansi_escape_codes", None)
        if not callable(stripper):
            raise RuntimeError("Netmiko ANSI normalizer is unavailable")
        output = stripper(output)
    if not isinstance(output, str):
        raise TypeError("Netmiko output transform returned non-text")
    return output


def _validate_stream_parts(parts: list[str]) -> None:
    character_count = sum(len(part) for part in parts)
    if character_count > MAX_COMMAND_OUTPUT_CHARACTERS:
        raise SshOperationError(
            "OUTPUT_LIMIT_EXCEEDED",
            "명령 결과가 안전한 최대 크기를 초과했습니다.",
            retryable=False,
            operation="command",
        )
    line_count = 1
    previous_ended_with_cr = False
    for part in parts:
        breaks = part.count("\n") + part.count("\r") - part.count("\r\n")
        if previous_ended_with_cr and part.startswith("\n"):
            breaks -= 1
        line_count += breaks
        previous_ended_with_cr = part.endswith("\r")
    if line_count > MAX_COMMAND_OUTPUT_LINES:
        raise SshOperationError(
            "OUTPUT_LIMIT_EXCEEDED",
            "명령 결과가 안전한 최대 줄 수를 초과했습니다.",
            retryable=False,
            operation="command",
        )


def classify_ssh_exception(exc: BaseException, *, operation: str) -> SshOperationError:
    class_name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if "authentication" in class_name or "authentication" in message or "auth fail" in message:
        return SshOperationError(
            "AUTH_FAILED", "장비 로그인에 실패했습니다. 등록된 사용자 ID와 비밀번호를 확인하세요.", retryable=False, operation=operation
        )
    if "badhostkey" in class_name or "host key for server" in message and "does not match" in message:
        return SshOperationError(
            "SSH_HOST_KEY_MISMATCH", "SSH 서버 키가 이전 승인 값과 다릅니다.", retryable=False, operation=operation
        )
    if "not found in known_hosts" in message or "known_hosts" in message and "not found" in message:
        return SshOperationError(
            "SSH_HOST_KEY_UNKNOWN", "승인되지 않은 SSH 서버 키입니다. 설정에서 지문을 확인하세요.", retryable=False, operation=operation
        )
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timeout" in class_name or "timed out" in message:
        code = "COMMAND_TIMEOUT" if operation == "command" else "TCP_TIMEOUT"
        text = "명령 실행 시간이 초과되었습니다." if operation == "command" else "장비 연결 시간이 초과되었습니다."
        return SshOperationError(code, text, retryable=True, operation=operation)
    if "prompt" in message or "readtimeout" in class_name:
        return SshOperationError(
            "PROMPT_NOT_FOUND", "장비 명령 프롬프트를 확인하지 못했습니다.", retryable=True, operation=operation
        )
    code = "COMMAND_FAILED" if operation == "command" else "SSH_CONNECTION_FAILED"
    text = "장비 명령을 실행하지 못했습니다." if operation == "command" else "장비 SSH 연결에 실패했습니다."
    return SshOperationError(code, text, retryable=True, operation=operation)


def netmiko_adapter_factory(options: SshConnectionOptions, credential: DeviceCredential) -> ArubaSshAdapter:
    return ArubaSshAdapter(options, credential)
