from __future__ import annotations

import re
from time import monotonic, sleep

from aruba_mini_dashboard.collectors.aruba_ssh import (
    ArubaSshAdapter,
    _is_netmiko_connection,
    command_was_rejected,
    contains_paging_marker,
    validate_bounded_output,
)
from aruba_mini_dashboard.collectors.base import SshOperationError

from .models import ActionCommandResult, ActionResultCode


RELOAD_FORCE_COMMAND = "reload force"
REBALANCE_COMMAND = "cluster-debug bucketmap rebalance"
REBALANCE_SUCCESS_MESSAGE = "Cluster rebalance triggered"
ACTION_COMMANDS = frozenset({RELOAD_FORCE_COMMAND, REBALANCE_COMMAND})
_MAX_EXCERPT = 2048
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def validate_action_command(command: str) -> str:
    if command != command.strip() or command not in ACTION_COMMANDS:
        raise SshOperationError(
            "ACTION_COMMAND_NOT_ALLOWED",
            "허용되지 않은 장애조치 명령은 실행할 수 없습니다.",
            retryable=False,
            operation="action",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in command):
        raise SshOperationError(
            "ACTION_COMMAND_NOT_ALLOWED",
            "허용되지 않은 장애조치 명령은 실행할 수 없습니다.",
            retryable=False,
            operation="action",
        )
    return command


def rebalance_output_confirmed(output: str) -> bool:
    return any(line.strip() == REBALANCE_SUCCESS_MESSAGE for line in output.splitlines())


def _excerpt(output: str) -> str:
    cleaned = _ANSI.sub("", str(output)).replace("\x08", "")
    return cleaned[-_MAX_EXCERPT:]


def _connection_is_alive(connection: object) -> bool | None:
    checker = getattr(connection, "is_alive", None)
    if not callable(checker):
        return None
    try:
        value = checker()
    except Exception:
        return False
    if isinstance(value, dict):
        candidate = value.get("is_alive")
        return bool(candidate) if candidate is not None else None
    if isinstance(value, bool):
        return value
    return None


class ArubaActionSshAdapter(ArubaSshAdapter):
    """Mutating SSH boundary restricted to exactly two reviewed commands."""

    def run_reload_force(self) -> ActionCommandResult:
        command = validate_action_command(RELOAD_FORCE_COMMAND)
        self._check_cancelled()
        connection = self._require_connection()
        started = monotonic()
        sent = False
        output_parts: list[str] = []
        try:
            normalize_cmd = getattr(connection, "normalize_cmd", None)
            write_channel = getattr(connection, "write_channel", None)
            channel = getattr(connection, "channel", None)
            read_buffer = getattr(channel, "read_buffer", None) if channel is not None else None
            if not callable(write_channel):
                # Fake/alternate connection factories used by tests may expose
                # only send_command_timing.  It still represents a single
                # dispatch attempt and is never retried after returning/raising.
                timing = getattr(connection, "send_command_timing", None)
                if not callable(timing):
                    raise SshOperationError(
                        "ACTION_RUNTIME_INCOMPATIBLE",
                        "현재 SSH 모듈에서 재부팅 명령을 안전하게 전송할 수 없습니다.",
                        retryable=False,
                        operation="action",
                    )
                sent = True
                try:
                    output = timing(
                        command_string=command,
                        strip_prompt=False,
                        strip_command=False,
                        cmd_verify=False,
                        read_timeout=min(5, self.options.command_timeout_seconds),
                    )
                except Exception:
                    return ActionCommandResult(
                        command=command,
                        code=ActionResultCode.RESULT_UNKNOWN_AFTER_SEND,
                        sent=True,
                        accepted=None,
                        duration_ms=int((monotonic() - started) * 1000),
                        message="재부팅 명령 전송 후 SSH 응답이 종료되어 수락 여부를 상태 관찰로 확인합니다.",
                    )
                output = validate_bounded_output(output, allow_empty=True)
                if command_was_rejected(output):
                    return ActionCommandResult(
                        command=command,
                        code=ActionResultCode.RELOAD_REJECTED,
                        sent=True,
                        accepted=False,
                        output_excerpt=_excerpt(output),
                        duration_ms=int((monotonic() - started) * 1000),
                        message="장비가 reload force 명령을 거부했습니다.",
                    )
                return ActionCommandResult(
                    command=command,
                    code=ActionResultCode.RELOAD_DISPATCHED,
                    sent=True,
                    accepted=None,
                    output_excerpt=_excerpt(output),
                    duration_ms=int((monotonic() - started) * 1000),
                    message="reload force 명령을 전송했습니다. MM 상태로 복구를 확인합니다.",
                )

            normalized = normalize_cmd(command) if callable(normalize_cmd) else command + "\n"
            write_channel(normalized)
            sent = True
            deadline = monotonic() + min(5.0, float(self.options.command_timeout_seconds))
            while monotonic() < deadline:
                if self._is_cancelled():
                    return ActionCommandResult(
                        command=command,
                        code=ActionResultCode.RESULT_UNKNOWN_AFTER_SEND,
                        sent=True,
                        accepted=None,
                        output_excerpt=_excerpt("".join(output_parts)),
                        duration_ms=int((monotonic() - started) * 1000),
                        message="재부팅 명령 전송 후 조치 중단 요청을 받아 추가 확인을 중단했습니다.",
                    )
                chunk = ""
                try:
                    if callable(read_buffer):
                        chunk = read_buffer()
                    else:
                        read_channel = getattr(connection, "read_channel", None)
                        if callable(read_channel):
                            chunk = read_channel()
                except Exception:
                    return ActionCommandResult(
                        command=command,
                        code=ActionResultCode.EXPECTED_DISCONNECT,
                        sent=True,
                        accepted=True,
                        output_excerpt=_excerpt("".join(output_parts)),
                        duration_ms=int((monotonic() - started) * 1000),
                        message="reload force 전송 후 SSH 연결 종료를 확인했습니다.",
                    )
                if chunk:
                    output_parts.append(str(chunk))
                    joined = "".join(output_parts)
                    validate_bounded_output(joined, allow_empty=True)
                    if command_was_rejected(joined):
                        return ActionCommandResult(
                            command=command,
                            code=ActionResultCode.RELOAD_REJECTED,
                            sent=True,
                            accepted=False,
                            output_excerpt=_excerpt(joined),
                            duration_ms=int((monotonic() - started) * 1000),
                            message="장비가 reload force 명령을 거부했습니다.",
                        )
                alive = _connection_is_alive(connection)
                if alive is False:
                    return ActionCommandResult(
                        command=command,
                        code=ActionResultCode.EXPECTED_DISCONNECT,
                        sent=True,
                        accepted=True,
                        output_excerpt=_excerpt("".join(output_parts)),
                        duration_ms=int((monotonic() - started) * 1000),
                        message="reload force 전송 후 SSH 연결 종료를 확인했습니다.",
                    )
                sleep(0.1)
            return ActionCommandResult(
                command=command,
                code=ActionResultCode.RESULT_UNKNOWN_AFTER_SEND,
                sent=True,
                accepted=None,
                output_excerpt=_excerpt("".join(output_parts)),
                duration_ms=int((monotonic() - started) * 1000),
                message="reload force 명령은 전송했으나 즉시 수락 여부는 확인되지 않아 MM 상태로 복구를 확인합니다.",
            )
        except SshOperationError:
            raise
        except Exception as exc:
            if sent:
                return ActionCommandResult(
                    command=command,
                    code=ActionResultCode.RESULT_UNKNOWN_AFTER_SEND,
                    sent=True,
                    accepted=None,
                    output_excerpt=_excerpt("".join(output_parts)),
                    duration_ms=int((monotonic() - started) * 1000),
                    message="재부팅 명령 전송 후 SSH 결과를 확인하지 못했습니다. 명령을 재전송하지 않습니다.",
                )
            raise SshOperationError(
                "ACTION_NOT_SENT",
                "재부팅 명령을 장비에 전송하지 못했습니다.",
                retryable=False,
                operation="action",
            ) from exc

    def run_rebalance(self) -> ActionCommandResult:
        command = validate_action_command(REBALANCE_COMMAND)
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
                output = connection.send_command_timing(
                    command_string=command,
                    strip_prompt=False,
                    strip_command=False,
                    cmd_verify=False,
                    read_timeout=self.options.command_timeout_seconds,
                )
            self._check_cancelled()
            output = validate_bounded_output(output, allow_empty=True)
            if contains_paging_marker(output):
                return ActionCommandResult(
                    command=command,
                    code=ActionResultCode.REBALANCE_UNCONFIRMED,
                    sent=True,
                    accepted=None,
                    output_excerpt=_excerpt(output),
                    duration_ms=int((monotonic() - started) * 1000),
                    message="재분배 출력에 미처리된 페이징 표시가 있어 정상 응답을 확인하지 못했습니다.",
                )
            if command_was_rejected(output):
                return ActionCommandResult(
                    command=command,
                    code=ActionResultCode.REBALANCE_REJECTED,
                    sent=True,
                    accepted=False,
                    output_excerpt=_excerpt(output),
                    duration_ms=int((monotonic() - started) * 1000),
                    message="장비가 클러스터 재분배 명령을 거부했습니다.",
                )
            if rebalance_output_confirmed(output):
                return ActionCommandResult(
                    command=command,
                    code=ActionResultCode.REBALANCE_TRIGGERED,
                    sent=True,
                    accepted=True,
                    output_excerpt=_excerpt(output),
                    duration_ms=int((monotonic() - started) * 1000),
                    message="Cluster rebalance triggered 정상 메시지를 확인했습니다.",
                )
            return ActionCommandResult(
                command=command,
                code=ActionResultCode.REBALANCE_UNCONFIRMED,
                sent=True,
                accepted=None,
                output_excerpt=_excerpt(output),
                duration_ms=int((monotonic() - started) * 1000),
                message="재분배 명령 출력에서 정확한 정상 메시지를 확인하지 못했습니다.",
            )
        except SshOperationError as exc:
            if exc.code == "CANCELLED":
                raise
            return ActionCommandResult(
                command=command,
                code=ActionResultCode.REBALANCE_RESULT_UNKNOWN,
                sent=True,
                accepted=None,
                duration_ms=int((monotonic() - started) * 1000),
                message="재분배 명령 전송 후 결과를 확인하지 못했습니다. 자동 재실행하지 않습니다.",
            )
        except Exception:
            return ActionCommandResult(
                command=command,
                code=ActionResultCode.REBALANCE_RESULT_UNKNOWN,
                sent=True,
                accepted=None,
                duration_ms=int((monotonic() - started) * 1000),
                message="재분배 명령 전송 후 SSH 결과를 확인하지 못했습니다. 자동 재실행하지 않습니다.",
            )
