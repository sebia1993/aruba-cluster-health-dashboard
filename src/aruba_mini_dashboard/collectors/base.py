"""Transport-neutral collector contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..credentials import DeviceCredential


SHOW_SWITCHES = "show switches"
SHOW_CLIENT_DISTRIBUTION = "show lc-cluster load distribution client"
SHOW_GROUP_MEMBERSHIP = "show lc-cluster group-membership"
NO_PAGING = "no paging"
PAGER_CONTINUE = " "
READ_ONLY_COMMANDS = frozenset({SHOW_SWITCHES, SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP})
SESSION_COMMANDS = frozenset({NO_PAGING, PAGER_CONTINUE})


@dataclass(frozen=True, slots=True)
class SshConnectionOptions:
    host: str
    port: int
    connect_timeout_seconds: int
    command_timeout_seconds: int
    known_hosts_path: Path
    enable_required: bool = False
    device_type: str = "aruba_os"


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    success: bool
    output: str = ""
    error_code: str = ""
    error_message: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class CollectionAttempt:
    controller_ip: str
    attempt: int
    success: bool
    error_code: str = ""
    error_message: str = ""


@dataclass(slots=True)
class CollectionBundle:
    source: str
    requested_controller_ip: str
    actual_controller_ip: str = ""
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    commands: dict[str, CommandResult] = field(default_factory=dict)
    attempts: list[CollectionAttempt] = field(default_factory=list)
    primary_failed: bool = False
    failover_at: datetime | None = None
    terminal_error_code: str = ""
    terminal_error_message: str = ""

    @property
    def successful_command_count(self) -> int:
        return sum(1 for result in self.commands.values() if result.success)

    @property
    def complete(self) -> bool:
        return bool(self.commands) and all(result.success for result in self.commands.values())

    @property
    def partial(self) -> bool:
        return 0 < self.successful_command_count < len(self.commands)

    @property
    def success(self) -> bool:
        return self.complete


class SshOperationError(RuntimeError):
    """Sanitized, stable failure returned by the SSH boundary."""

    def __init__(self, code: str, user_message: str, *, retryable: bool, operation: str) -> None:
        self.code = code
        self.user_message = user_message
        self.retryable = retryable
        self.operation = operation
        super().__init__(user_message)


class ReadOnlySshAdapter(Protocol):
    def connect(self) -> None: ...

    def run_read_only(self, command: str) -> str: ...

    def close(self) -> None: ...


class AdapterFactory(Protocol):
    def __call__(self, options: SshConnectionOptions, credential: DeviceCredential) -> ReadOnlySshAdapter: ...
