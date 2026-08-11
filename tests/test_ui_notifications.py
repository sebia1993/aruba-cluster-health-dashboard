from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QSignalSpy

from aruba_mini_dashboard.services.notification_service import NotificationEvent, NotificationService
from aruba_mini_dashboard.config import AppSettings
from aruba_mini_dashboard.main import RuntimeSnapshot
from aruba_mini_dashboard.models import DeviceHealth, Incident, IncidentType, OverallHealth, Severity
from aruba_mini_dashboard.ui.main_window import MainWindow
from test_ui_dashboard import FakeCoordinator


class FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple] = []
        self.icon = None

    def isVisible(self) -> bool:
        return True

    def showMessage(self, *args) -> None:
        self.messages.append(args)

    def setIcon(self, icon) -> None:
        self.icon = icon


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _event(*, active: bool = True) -> NotificationEvent:
    return NotificationEvent(
        ip="192.0.2.12",
        issue_type="mm_down",
        title="Aruba 장애 감지" if active else "Aruba 복구 알림",
        message="MM Status Down" if active else "MM Status Up 복구",
        detected_at=datetime.now(),
        active=active,
        severity="critical",
    )


def test_duplicate_notification_and_repeat_interval() -> None:
    _app()
    tray = FakeTray()
    service = NotificationService(tray, repeat_enabled=True, repeat_minutes=10)
    start = datetime(2026, 8, 11, 10, 0, 0)
    assert service.notify(_event(), now=start)
    assert not service.notify(_event(), now=start + timedelta(minutes=9))
    assert service.notify(_event(), now=start + timedelta(minutes=10))
    assert len(tray.messages) == 2


def test_unavailable_tray_does_not_mark_notification_as_delivered() -> None:
    _app()
    service = NotificationService(None)
    shown = QSignalSpy(service.notification_shown)
    failed = QSignalSpy(service.notification_failed)

    assert service.notify(_event()) is False
    assert shown.count() == 0
    assert failed.count() == 1
    assert service._last_shown == {}


def test_acknowledgement_suppresses_active_repeat_but_allows_recovery() -> None:
    _app()
    tray = FakeTray()
    service = NotificationService(tray, repeat_enabled=True, recovery_enabled=True)
    start = datetime(2026, 8, 11, 10, 0, 0)
    service.notify(_event(), now=start)
    service.acknowledge_ip("192.0.2.12")
    assert not service.notify(_event(), now=start + timedelta(hours=1))
    assert service.notify(_event(active=False), now=start + timedelta(hours=1))


def test_unacknowledged_recovery_is_distinct_and_not_repeated() -> None:
    _app()
    tray = FakeTray()
    service = NotificationService(tray, repeat_enabled=False, recovery_enabled=True)
    start = datetime(2026, 8, 11, 10, 0, 0)
    assert service.notify(_event(), now=start)
    assert service.notify(_event(active=False), now=start + timedelta(minutes=1))
    assert not service.notify(_event(active=False), now=start + timedelta(minutes=2))


def test_new_activation_after_recovery_is_not_suppressed_by_old_lifecycle() -> None:
    _app()
    tray = FakeTray()
    service = NotificationService(tray, repeat_enabled=False, recovery_enabled=True)
    start = datetime(2026, 8, 11, 10, 0, 0)
    assert service.notify(_event(), now=start)
    service.acknowledge_ip("192.0.2.12")
    assert service.notify(_event(active=False), now=start + timedelta(minutes=1))
    assert service.notify(_event(active=True), now=start + timedelta(minutes=2))
    assert len(tray.messages) == 3


def test_multiple_collection_failures_are_not_labeled_as_device_outage() -> None:
    _app()
    tray = FakeTray()
    service = NotificationService(tray)
    now = datetime.now()
    failures = [
        NotificationEvent(
            ip=ip,
            issue_type=f"collection_failure:{source}",
            title="Aruba 수집 확인 불가",
            message=f"{source} 수집 실패",
            detected_at=now,
            severity="unknown",
        )
        for ip, source in (("192.0.2.1", "mm"), ("192.0.2.11", "cluster"))
    ]

    assert service.notify_many(failures)
    assert tray.messages[-1][0] == "Aruba 수집 확인 불가"
    assert "장애" not in tray.messages[-1][0]


def test_domain_incident_types_create_distinct_notification_keys() -> None:
    _app()

    class CaptureService:
        tray_icon = None

        def __init__(self) -> None:
            self.events = []

        def notify_many(self, events) -> None:
            self.events = events

    now = datetime.now()
    health = OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL,
        devices=[DeviceHealth(ip="192.0.2.12", severity=Severity.CRITICAL)],
        problem_ips=["192.0.2.12"],
    )
    incidents = [
        Incident("one", IncidentType.MM_DOWN, Severity.CRITICAL, "MM Down", now, now, ip="192.0.2.12"),
        Incident(
            "two",
            IncidentType.CLIENT_DISTRIBUTION,
            Severity.WARNING,
            "Client 이상",
            now,
            now,
            ip="192.0.2.12",
        ),
    ]
    capture = CaptureService()
    window = MainWindow(FakeCoordinator(), AppSettings.default(), notification_service=capture)
    window.update_snapshot(RuntimeSnapshot(health, incidents))
    keys = {event.issue_type for event in capture.events}
    assert keys == {"mm_down:one", "client_distribution:two"}
    assert all("IP: 192.0.2.12" in event.message for event in capture.events)
    assert all("감지 시각:" in event.message for event in capture.events)
    window._quitting = True
    window.close()


def test_same_ip_combined_signals_escalate_new_warning_event_to_outage_notification() -> None:
    _app()

    class CaptureService:
        tray_icon = None

        def __init__(self) -> None:
            self.events = []

        def notify_many(self, events) -> None:
            self.events = events

    now = datetime.now()
    health = OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL,
        devices=[
            DeviceHealth(
                ip="192.0.2.12",
                alias="WLC-02",
                severity=Severity.CRITICAL,
                issue_reasons=[
                    "Client 분배 이상",
                    "Connection-Type 변경: Type-A → Type-B",
                ],
            )
        ],
        problem_ips=["192.0.2.12"],
    )
    change = Incident(
        "connection-change",
        IncidentType.CONNECTION_TYPE_CHANGED,
        Severity.WARNING,
        "Connection-Type 변경",
        now,
        now,
        ip="192.0.2.12",
    )
    capture = CaptureService()
    window = MainWindow(FakeCoordinator(), AppSettings.default(), notification_service=capture)
    window.update_snapshot(RuntimeSnapshot(health, [change]))

    event = capture.events[0]
    assert event.severity == "critical"
    assert event.title == "Aruba 장애 감지"
    assert "Client 분배 이상" in event.message
    assert "Connection-Type 변경: Type-A → Type-B" in event.message
    assert "장비명: WLC-02" in event.message
    window._quitting = True
    window.close()


def test_collection_failure_and_recovery_have_truthful_korean_notification_text() -> None:
    _app()

    class CaptureService:
        tray_icon = None

        def __init__(self) -> None:
            self.events = []

        def notify_many(self, events) -> None:
            self.events = events

    now = datetime.now()
    health = OverallHealth(now, Severity.UNKNOWN, [], problem_ips=[])
    failure = Incident(
        "collection-one",
        IncidentType.COLLECTION_FAILURE,
        Severity.UNKNOWN,
        "MM 로그인 실패로 상태를 확인할 수 없습니다.",
        now,
        now,
        ip=None,
    )
    capture = CaptureService()
    window = MainWindow(FakeCoordinator(), AppSettings.default(), notification_service=capture)
    window.update_snapshot(RuntimeSnapshot(health, [failure]))
    assert capture.events[0].title == "Aruba 수집 확인 불가"
    assert "장애" not in capture.events[0].title
    assert "IP: 특정 불가" in capture.events[0].message

    failure.active = False
    failure.recovered_at = now + timedelta(minutes=1)
    window.update_snapshot(RuntimeSnapshot(health, [failure]))
    assert capture.events[0].title == "Aruba 복구 알림"
    assert "이전 원인 해제" in capture.events[0].message
    window._quitting = True
    window.close()
