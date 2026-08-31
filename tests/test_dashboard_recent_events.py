from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.ui.main_window import MainWindow


class _Coordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    busy = False
    automatic = False

    def check_now(self) -> None:
        return None

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, _seconds: int) -> None:
        return None


class _Storage:
    def __init__(self, events: list[dict[str, object]] | None = None) -> None:
        self.events = events or []
        self.limits: list[int] = []

    def list_events(self, *, limit: int = 500) -> list[dict[str, object]]:
        self.limits.append(limit)
        return self.events[:limit]


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot() -> dict[str, object]:
    return {
        "severity": "warning",
        "checked_at": "2026-09-01T12:30:00+09:00",
        "monitoring_scope_ips": ["192.0.2.11"],
        "devices": [
            {
                "ip": "192.0.2.11",
                "alias": "TEST-WLC-01",
                "hostname": "controller-01.example",
                "controller_state": "up",
                "distribution_state": "normal",
                "active_clients": 10,
                "standby_clients": 8,
                "severity": "warning",
            }
        ],
    }


def test_main_window_reads_public_recent_event_api_and_presents_lifecycle() -> None:
    _app()
    storage = _Storage(
        [
            {
                "event_type": "recovered",
                "ip": "192.0.2.11",
                "occurred_at": "2026-09-01T12:30:00+09:00",
                "payload": {"severity": "critical", "reason": "복구"},
            },
            {
                "event_type": "acknowledged",
                "ip": "192.0.2.11",
                "occurred_at": "2026-09-01T12:20:00+09:00",
                "payload": {"severity": "critical", "reason": "확인"},
            },
        ]
    )
    window = MainWindow(_Coordinator(), AppSettings.default(), storage=storage)

    window.update_snapshot(_snapshot())

    assert storage.limits == [10]
    events = window.overview_page.recent_events.events
    assert [event.summary for event in events] == [
        "TEST-WLC-01 복구",
        "TEST-WLC-01 알림 확인",
    ]
    assert [event.status for event in events] == ["normal", "unknown"]
    window._quitting = True
    window.close()


def test_recent_event_read_failure_is_isolated_from_dashboard_snapshot() -> None:
    _app()

    class _BrokenStorage:
        def list_events(self, *, limit: int = 500) -> list[dict[str, object]]:
            raise RuntimeError("supplementary read failed")

    window = MainWindow(_Coordinator(), AppSettings.default(), storage=_BrokenStorage())

    window.update_snapshot(_snapshot())

    assert window.status_label.text() == "주의"
    assert window.overview_page.recent_events.events == ()
    assert "불러오기 지연" in window.overview_page.recent_events.title_label.text()
    assert "모니터링 상태에는 영향이 없습니다" in (
        window.overview_page.recent_events.accessibleDescription()
    )
    window._quitting = True
    window.close()
