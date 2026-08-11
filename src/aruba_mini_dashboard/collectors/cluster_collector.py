"""Aruba cluster collection with same-controller primary/fallback semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
import threading

from ..config import ClusterSettings
from ..credentials import DeviceCredential
from .aruba_ssh import ArubaSshAdapter
from .base import (
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    AdapterFactory,
    CollectionAttempt,
    CollectionBundle,
    CommandResult,
    SshConnectionOptions,
    SshOperationError,
)


CLUSTER_COMMANDS = (SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP)

# Host-key trust failures are not controller-availability failures.  Continuing
# to another controller would hide a changed or unapproved identity behind an
# otherwise successful failover result.  Both codes are emitted by the strict
# app-owned known_hosts boundary in ``aruba_ssh.classify_ssh_exception``.
HOST_KEY_TRUST_ERROR_CODES = frozenset(
    {
        "SSH_HOST_KEY_MISMATCH",
        "SSH_HOST_KEY_UNKNOWN",
    }
)


class ClusterCollector:
    def __init__(
        self,
        *,
        known_hosts_path: Path,
        adapter_factory: AdapterFactory | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.known_hosts_path = Path(known_hosts_path)
        self.cancel_event = cancel_event
        self.adapter_factory = adapter_factory or (
            lambda options, credential: ArubaSshAdapter(options, credential, cancel_event=self.cancel_event)
        )

    def collect(self, settings: ClusterSettings, credential: DeviceCredential) -> CollectionBundle:
        primary = settings.primary_controller_ip.strip()
        controllers = _ordered_unique([primary, *settings.fallback_controller_ips])
        all_attempts: list[CollectionAttempt] = []
        best: CollectionBundle | None = None
        primary_error_code = ""
        for controller_index, controller_ip in enumerate(controllers):
            if self.cancel_event is not None and self.cancel_event.is_set():
                break
            options = SshConnectionOptions(
                host=controller_ip,
                port=settings.ssh_port,
                connect_timeout_seconds=settings.connect_timeout_seconds,
                command_timeout_seconds=settings.command_timeout_seconds,
                known_hosts_path=self.known_hosts_path,
                enable_required=settings.enable_required,
            )
            for attempt_number in range(1, settings.retries + 2):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    break
                candidate, operation_error = self._collect_from_controller(
                    primary, controller_ip, attempt_number, options, credential
                )
                all_attempts.extend(candidate.attempts)
                candidate.attempts = list(all_attempts)
                candidate.primary_failed = controller_index > 0 or not candidate.complete
                if controller_index > 0 and candidate.successful_command_count:
                    candidate.failover_at = candidate.collected_at
                if controller_index == 0 and not candidate.complete:
                    primary_error_code = candidate.terminal_error_code or primary_error_code
                if best is None or candidate.successful_command_count > best.successful_command_count:
                    best = candidate
                if operation_error is not None and operation_error.code in HOST_KEY_TRUST_ERROR_CODES:
                    # Fail closed for every configured endpoint.  In
                    # particular, never let a fallback success conceal a
                    # changed Primary key that can indicate replacement or a
                    # man-in-the-middle endpoint.
                    return candidate
                if candidate.complete:
                    candidate.primary_failed = controller_index > 0
                    return candidate
                if operation_error is not None and not operation_error.retryable:
                    break
        if best is None:
            best = CollectionBundle(source="cluster", requested_controller_ip=primary)
        best.attempts = all_attempts
        best.primary_failed = True
        if not best.terminal_error_code:
            best.terminal_error_code = primary_error_code or "CLUSTER_COLLECTION_FAILED"
            best.terminal_error_message = "모든 Cluster 수집 Controller에서 명령 결과를 가져오지 못했습니다."
        return best

    def _collect_from_controller(
        self,
        primary: str,
        controller_ip: str,
        attempt_number: int,
        options: SshConnectionOptions,
        credential: DeviceCredential,
    ) -> tuple[CollectionBundle, SshOperationError | None]:
        candidate = CollectionBundle(
            source="cluster",
            requested_controller_ip=primary,
            actual_controller_ip=controller_ip,
        )
        adapter = self.adapter_factory(options, credential)
        operation_error: SshOperationError | None = None
        try:
            adapter.connect()
            for command in CLUSTER_COMMANDS:
                started = monotonic()
                try:
                    output = adapter.run_read_only(command)
                    candidate.commands[command] = CommandResult(
                        command, True, output=output, duration_ms=int((monotonic() - started) * 1000)
                    )
                except SshOperationError as exc:
                    operation_error = exc
                    candidate.commands[command] = CommandResult(
                        command,
                        False,
                        error_code=exc.code,
                        error_message=exc.user_message,
                        duration_ms=int((monotonic() - started) * 1000),
                    )
                    # A broken transport cannot provide a trustworthy second
                    # command.  Preserve a same-source partial bundle only.
                    break
            for command in CLUSTER_COMMANDS:
                candidate.commands.setdefault(
                    command,
                    CommandResult(
                        command,
                        False,
                        error_code=(operation_error.code if operation_error else "COMMAND_NOT_RUN"),
                        error_message=(operation_error.user_message if operation_error else "명령을 실행하지 못했습니다."),
                    ),
                )
            candidate.collected_at = datetime.now(timezone.utc)
            candidate.attempts.append(
                CollectionAttempt(
                    controller_ip,
                    attempt_number,
                    candidate.complete,
                    "" if candidate.complete else (operation_error.code if operation_error else "PARTIAL_COLLECTION"),
                    "" if candidate.complete else (operation_error.user_message if operation_error else "일부 명령만 수집했습니다."),
                )
            )
        except SshOperationError as exc:
            operation_error = exc
            candidate.terminal_error_code = exc.code
            candidate.terminal_error_message = exc.user_message
            candidate.commands = {
                command: CommandResult(command, False, error_code=exc.code, error_message=exc.user_message)
                for command in CLUSTER_COMMANDS
            }
            candidate.attempts.append(CollectionAttempt(controller_ip, attempt_number, False, exc.code, exc.user_message))
        finally:
            adapter.close()
        if operation_error is not None:
            candidate.terminal_error_code = operation_error.code
            candidate.terminal_error_message = operation_error.user_message
        return candidate, operation_error


def collect_cluster(
    settings: ClusterSettings,
    credential: DeviceCredential,
    *,
    known_hosts_path: Path,
    adapter_factory: AdapterFactory | None = None,
    cancel_event: threading.Event | None = None,
) -> CollectionBundle:
    return ClusterCollector(
        known_hosts_path=known_hosts_path, adapter_factory=adapter_factory, cancel_event=cancel_event
    ).collect(settings, credential)


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value).strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result
