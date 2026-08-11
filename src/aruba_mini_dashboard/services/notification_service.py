from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


@dataclass(slots=True)
class NotificationEvent:
    ip: str
    issue_type: str
    title: str
    message: str
    detected_at: datetime
    active: bool = True
    acknowledged: bool = False
    severity: str = "warning"
    incident_id: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.ip, self.issue_type


class NotificationService(QObject):
    """System-tray notifications with local duplicate/repeat suppression."""

    notification_shown = Signal(object)
    notification_failed = Signal(str)

    def __init__(
        self,
        tray_icon: QSystemTrayIcon | None = None,
        *,
        sound_enabled: bool = False,
        repeat_enabled: bool = False,
        repeat_minutes: int = 10,
        recovery_enabled: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.tray_icon = tray_icon
        self.sound_enabled = sound_enabled
        self.repeat_enabled = repeat_enabled
        self.repeat_minutes = max(1, int(repeat_minutes))
        self.recovery_enabled = recovery_enabled
        self._last_shown: dict[tuple[str, str], datetime] = {}
        self._last_active: dict[tuple[str, str], bool] = {}
        self._acknowledged: set[tuple[str, str]] = set()

    def configure(
        self,
        *,
        sound_enabled: bool | None = None,
        repeat_enabled: bool | None = None,
        repeat_minutes: int | None = None,
        recovery_enabled: bool | None = None,
    ) -> None:
        if sound_enabled is not None:
            self.sound_enabled = bool(sound_enabled)
        if repeat_enabled is not None:
            self.repeat_enabled = bool(repeat_enabled)
        if repeat_minutes is not None:
            self.repeat_minutes = max(1, int(repeat_minutes))
        if recovery_enabled is not None:
            self.recovery_enabled = bool(recovery_enabled)

    def should_notify(self, event: NotificationEvent, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not event.active:
            if not self.recovery_enabled:
                return False
            # A recovery is a distinct transition from the active alert, but a
            # repeated recovered snapshot must not create another notification.
            return self._last_active.get(event.key) is not False
        if self._last_active.get(event.key) is False:
            # A fresh activation after a recovered lifecycle must not inherit
            # the old event's acknowledgement or duplicate-suppression state.
            return True
        if event.acknowledged or event.key in self._acknowledged:
            return False
        last = self._last_shown.get(event.key)
        if last is None:
            return True
        return self.repeat_enabled and now - last >= timedelta(minutes=self.repeat_minutes)

    def notify(self, event: NotificationEvent, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not self.should_notify(event, now):
            return False
        if self.tray_icon is None or not self.tray_icon.isVisible():
            self.notification_failed.emit("시스템 트레이 알림을 사용할 수 없습니다.")
            return False
        icon = self._message_icon(event.severity, event.active)
        if event.active and self._last_active.get(event.key) is False:
            self._acknowledged.discard(event.key)
        self.tray_icon.showMessage(event.title, event.message, icon, 10000)
        self._last_shown[event.key] = now
        self._last_active[event.key] = event.active
        if self.sound_enabled:
            QApplication.beep()
        self.notification_shown.emit(event)
        return True

    def notify_many(self, events: list[NotificationEvent]) -> bool:
        pending = [event for event in events if self.should_notify(event)]
        if not pending:
            return False
        if len(pending) == 1:
            return self.notify(pending[0])
        active = [event for event in pending if event.active]
        active_collection_failures = [
            event
            for event in active
            if event.severity.casefold() == "unknown"
            or event.issue_type.casefold().startswith("collection_failure")
        ]
        if not active:
            title = "Aruba 복구 알림"
        elif len(active_collection_failures) == len(active):
            title = "Aruba 수집 확인 불가"
        elif any(event.severity.casefold() in {"critical", "error", "failure"} for event in active):
            title = "Aruba 장애 감지"
        else:
            title = "Aruba 주의 감지"
        lines = [f"{event.ip or '수집'}: {event.message}" for event in pending[:5]]
        if len(pending) > 5:
            lines.append(f"외 {len(pending) - 5}건")
        summary = NotificationEvent(
            ip="*",
            issue_type="cycle-summary:" + ",".join(
                sorted(f"{event.ip}/{event.issue_type}" for event in pending)
            ),
            title=title,
            message="\n".join(lines),
            detected_at=datetime.now(),
            active=bool(active),
            severity=(
                "critical"
                if any(event.severity.casefold() == "critical" for event in pending)
                else "unknown"
                if active and len(active_collection_failures) == len(active)
                else "warning"
            ),
        )
        shown = self.notify(summary)
        if shown:
            now = datetime.now()
            for event in pending:
                self._last_shown[event.key] = now
                self._last_active[event.key] = event.active
                self.notification_shown.emit(event)
        return shown

    @Slot(str, str)
    def acknowledge(self, ip: str, issue_type: str) -> None:
        self._acknowledged.add((ip, issue_type))

    @Slot(str)
    def acknowledge_ip(self, ip: str) -> None:
        """Suppress repeats for every currently known reason on one device."""

        for key in self._last_shown:
            if key[0] == ip:
                self._acknowledged.add(key)

    def clear_resolved(self, ip: str, issue_type: str) -> None:
        key = (ip, issue_type)
        self._acknowledged.discard(key)
        self._last_shown.pop(key, None)
        self._last_active.pop(key, None)

    @Slot()
    def test_sound(self) -> None:
        QApplication.beep()

    @Slot()
    def test_notification(self) -> bool:
        event = NotificationEvent(
            ip="테스트",
            issue_type=f"test-{datetime.now().timestamp()}",
            title="Aruba 알림 테스트",
            message="Windows 알림이 정상적으로 표시되었습니다.",
            detected_at=datetime.now(),
            severity="information",
        )
        return self.notify(event)

    @staticmethod
    def _message_icon(severity: str, active: bool) -> QSystemTrayIcon.MessageIcon:
        if not active:
            return QSystemTrayIcon.Information
        severity = str(severity).lower()
        if severity in {"critical", "error", "failure"}:
            return QSystemTrayIcon.Critical
        if severity in {"warning", "attention"}:
            return QSystemTrayIcon.Warning
        return QSystemTrayIcon.Information

    def set_icon(self, icon: QIcon) -> None:
        if self.tray_icon is not None:
            self.tray_icon.setIcon(icon)
