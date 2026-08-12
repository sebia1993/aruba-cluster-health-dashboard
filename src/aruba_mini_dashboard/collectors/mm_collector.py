"""Mobility Master ``show switches`` collection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
import threading

from ..config import MobilityMasterSettings
from ..credentials import DeviceCredential
from .aruba_ssh import ArubaSshAdapter, wait_for_retry_backoff
from .base import (
    SHOW_SWITCHES,
    AdapterFactory,
    CollectionAttempt,
    CollectionBundle,
    CommandResult,
    SshConnectionOptions,
    SshOperationError,
)


class MmCollector:
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

    def collect(self, settings: MobilityMasterSettings, credential: DeviceCredential) -> CollectionBundle:
        endpoint = settings.management_ip.strip()
        bundle = CollectionBundle(source="mm", requested_controller_ip=endpoint)
        options = SshConnectionOptions(
            host=endpoint,
            port=settings.ssh_port,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            command_timeout_seconds=settings.command_timeout_seconds,
            known_hosts_path=self.known_hosts_path,
            enable_required=settings.enable_required,
        )
        for attempt_number in range(1, settings.retries + 2):
            if self.cancel_event is not None and self.cancel_event.is_set():
                bundle.terminal_error_code = "CANCELLED"
                bundle.terminal_error_message = "점검이 취소되었습니다."
                break
            adapter = self.adapter_factory(options, credential)
            started = monotonic()
            try:
                adapter.connect()
                output = adapter.run_read_only(SHOW_SWITCHES)
                duration = int((monotonic() - started) * 1000)
                bundle.actual_controller_ip = endpoint
                bundle.collected_at = datetime.now(timezone.utc)
                bundle.commands[SHOW_SWITCHES] = CommandResult(
                    SHOW_SWITCHES, True, output=output, duration_ms=duration
                )
                bundle.attempts.append(CollectionAttempt(endpoint, attempt_number, True))
                return bundle
            except SshOperationError as exc:
                bundle.commands[SHOW_SWITCHES] = CommandResult(
                    SHOW_SWITCHES,
                    False,
                    error_code=exc.code,
                    error_message=exc.user_message,
                    duration_ms=int((monotonic() - started) * 1000),
                )
                bundle.attempts.append(
                    CollectionAttempt(endpoint, attempt_number, False, exc.code, exc.user_message)
                )
                bundle.terminal_error_code = exc.code
                bundle.terminal_error_message = exc.user_message
                if not exc.retryable:
                    break
            finally:
                adapter.close()
            if attempt_number <= settings.retries and wait_for_retry_backoff(
                self.cancel_event,
                attempt_number,
            ):
                bundle.terminal_error_code = "CANCELLED"
                bundle.terminal_error_message = "점검이 취소되었습니다."
                break
        return bundle


def collect_mm(
    settings: MobilityMasterSettings,
    credential: DeviceCredential,
    *,
    known_hosts_path: Path,
    adapter_factory: AdapterFactory | None = None,
    cancel_event: threading.Event | None = None,
) -> CollectionBundle:
    return MmCollector(
        known_hosts_path=known_hosts_path, adapter_factory=adapter_factory, cancel_event=cancel_event
    ).collect(settings, credential)
