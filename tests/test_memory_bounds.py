from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import aruba_mini_dashboard.lazy_text_mapping as lazy_text_mapping
import aruba_mini_dashboard.main as runtime_main
from aruba_mini_dashboard.config import AppPaths, AppSettings
from aruba_mini_dashboard.credentials import CredentialService, SessionCredentialStore
from aruba_mini_dashboard.lazy_text_mapping import (
    LazyCompressedTextMapping,
    snapshot_raw_outputs,
)
from aruba_mini_dashboard.logging_setup import setup_logging
from aruba_mini_dashboard.main import RuntimePoller
from aruba_mini_dashboard.models import (
    HealthSignal,
    IncidentType,
    OverallHealth,
    PollCycleResult,
    Severity,
)
from aruba_mini_dashboard.services.notification_service import (
    NotificationEvent,
    NotificationService,
)
from aruba_mini_dashboard.storage import SQLiteStorage, StorageBusyError


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[object, ...]] = []

    def isVisible(self) -> bool:
        return True

    def showMessage(self, *args: object) -> None:
        self.messages.append(args)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _notification(index: int, *, active: bool = True) -> NotificationEvent:
    return NotificationEvent(
        ip=f"192.0.2.{index}",
        issue_type=f"mm_down:{index}",
        title="test",
        message="test",
        detected_at=datetime(2026, 8, 12, 9, 0, 0),
        active=active,
    )


def test_large_raw_output_is_lazy_lossless_and_mapping_compatible(monkeypatch) -> None:
    large = "Aruba output line\r\n" * 20_000
    source = {"small": "ok", "large": large}
    calls = 0
    original_decompressobj = lazy_text_mapping.zlib.decompressobj

    def counted_decompressobj():
        nonlocal calls
        calls += 1
        return original_decompressobj()

    monkeypatch.setattr(lazy_text_mapping.zlib, "decompressobj", counted_decompressobj)
    result = LazyCompressedTextMapping(source)

    assert list(result) == ["small", "large"]
    assert len(result) == 2
    assert "large" in result
    assert result.compressed_count == 1
    assert result.stored_size_bytes < result.original_size_bytes
    assert calls == 0
    assert result["small"] == "ok"
    assert calls == 0
    assert result["large"] == large
    assert calls == 1
    assert dict(result) == source


def test_raw_output_snapshot_only_compresses_in_low_spec_mode() -> None:
    source = {"show test": "x" * (256 * 1024)}

    normal = snapshot_raw_outputs(source, low_spec_mode=False)
    low_spec = snapshot_raw_outputs(source, low_spec_mode=True)

    assert type(normal) is dict
    assert isinstance(low_spec, LazyCompressedTextMapping)
    assert dict(normal) == source
    assert dict(low_spec) == source


def test_runtime_poller_uses_lazy_raw_outputs_in_low_spec_mode(tmp_path: Path) -> None:
    settings = AppSettings.default()
    settings.performance.low_spec_mode = True
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(":memory:")
    runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        setup_logging(paths, low_spec_mode=True),
    )
    raw = "x" * (256 * 1024)
    cycle = PollCycleResult(
        checked_at=datetime.now().astimezone(),
        expected_cluster_members={},
        raw_outputs={"show test": raw},
    )

    try:
        snapshot = runtime.correlate(cycle)
        assert isinstance(snapshot.raw_outputs, LazyCompressedTextMapping)
        assert snapshot.raw_outputs["show test"] == raw
    finally:
        storage.close()


def test_domain_persistence_retry_state_is_bounded_during_long_storage_outage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A persistently locked DB must not turn lifecycle history into a leak."""

    retry_limit = 8
    monkeypatch.setattr(runtime_main, "MAX_PENDING_DOMAIN_TRANSITIONS", retry_limit)
    paths = AppPaths.from_environment(tmp_path).ensure()
    storage = SQLiteStorage(":memory:")
    logging_context = setup_logging(paths)
    runtime = RuntimePoller(
        AppSettings.default(),
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        storage,
        logging_context,
    )
    original_flush = runtime.baseline_store.flush
    base = datetime(2026, 8, 13, 9, 0, 0)
    member_ip = "192.0.2.11"

    def lifecycle_health(index: int, active: bool) -> OverallHealth:
        return OverallHealth(
            checked_at=base + timedelta(minutes=index),
            severity=Severity.WARNING if active else Severity.NORMAL,
            devices=[],
            monitoring_scope_ips=(member_ip,),
            signals=(
                [
                    HealthSignal(
                        incident_type=IncidentType.CLIENT_DISTRIBUTION,
                        severity=Severity.WARNING,
                        reason="fixture anomaly",
                        ip=member_ip,
                        source="fixture",
                    )
                ]
                if active
                else []
            ),
        )

    initial = lifecycle_health(0, True)
    runtime._persist_incidents(
        runtime.incident_manager.process(initial, now=initial.checked_at)
    )
    durable_incident_id = runtime.incident_manager.active_incidents()[0].incident_id
    assert storage.load_domain_incidents(active_only=True)[0].incident_id == (
        durable_incident_id
    )

    def locked(*_args: object, **_kwargs: object) -> None:
        raise StorageBusyError("locked")

    monkeypatch.setattr(runtime.baseline_store, "flush", locked)
    for index in range(1, 23):
        active = index % 2 == 0
        health = lifecycle_health(index, active)
        transitions = runtime.incident_manager.process(health, now=health.checked_at)
        runtime._persist_incidents(transitions)
        assert len(runtime._pending_persistence_transitions) <= retry_limit
        assert len(runtime.incident_manager.events()) <= retry_limit + 1

    assert len(runtime._pending_persistence_transitions) == retry_limit
    active_before_retry = runtime.incident_manager.active_incidents()
    assert len(active_before_retry) == 1

    monkeypatch.setattr(runtime.baseline_store, "flush", original_flush)
    runtime._persist_incidents([])

    assert runtime._pending_persistence_transitions == []
    assert runtime.incident_manager.active_incidents()[0].incident_id == (
        active_before_retry[0].incident_id
    )
    assert runtime.incident_manager.events() == runtime.incident_manager.active_incidents()
    assert len(storage.list_events()) == retry_limit + 1
    persisted_active = storage.load_domain_incidents(active_only=True)
    assert [item.incident_id for item in persisted_active] == [
        active_before_retry[0].incident_id
    ]
    durable_incident = next(
        item
        for item in storage.load_domain_incidents()
        if item.incident_id == durable_incident_id
    )
    assert durable_incident.active is False

    logging_context.close()
    storage.close()


def test_unencodable_diagnostic_text_is_retained_exactly() -> None:
    source = {"show test": "prefix\ud800suffix"}

    result = LazyCompressedTextMapping(source, threshold_bytes=1)

    assert result["show test"] == source["show test"]
    assert result.compressed_count == 0


def test_notification_lifecycle_cache_is_bounded() -> None:
    _app()
    tray = _FakeTray()
    service = NotificationService(tray, max_lifecycle_entries=3)
    start = datetime(2026, 8, 12, 9, 0, 0)

    for index in range(1, 7):
        assert service.notify(_notification(index), now=start + timedelta(minutes=index))

    expected = {("192.0.2.4", "mm_down:4"), ("192.0.2.5", "mm_down:5"), ("192.0.2.6", "mm_down:6")}
    assert set(service._last_active) == expected
    assert set(service._last_shown) == expected
    assert len(service._acknowledged) <= 3


def test_recovery_keeps_only_bounded_marker_and_allows_fresh_activation() -> None:
    _app()
    tray = _FakeTray()
    service = NotificationService(
        tray,
        repeat_enabled=False,
        recovery_enabled=True,
        max_lifecycle_entries=3,
    )
    start = datetime(2026, 8, 12, 9, 0, 0)
    active = _notification(1)
    recovered = _notification(1, active=False)

    assert service.notify(active, now=start)
    service.acknowledge(active.ip, active.issue_type)
    assert service.notify(recovered, now=start + timedelta(minutes=1))
    assert active.key not in service._last_shown
    assert active.key not in service._acknowledged
    assert service._last_active[active.key] is False
    assert not service.notify(recovered, now=start + timedelta(minutes=2))
    assert service.notify(active, now=start + timedelta(minutes=3))


def test_batch_summary_does_not_leave_a_synthetic_cache_entry() -> None:
    _app()
    tray = _FakeTray()
    service = NotificationService(tray, max_lifecycle_entries=10)

    assert service.notify_many([_notification(1), _notification(2)])

    assert set(service._last_active) == {
        ("192.0.2.1", "mm_down:1"),
        ("192.0.2.2", "mm_down:2"),
    }
    assert all(key[0] != "*" for key in service._last_shown)
