from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..resources import status_icon
from ..theme import (
    RADIUS,
    SIZES,
    SPACING,
    STATUS_LABELS,
    normalize_status_key,
    presentation_palette,
    semantic_palette,
    status_colors,
)
from ._palette_aware import PaletteAwareWidgetMixin
from .empty_state import EmptyState


@dataclass(frozen=True, slots=True)
class RecentEvent:
    timestamp: str
    summary: str
    status: str = "unknown"
    detail: str = ""


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    return str(value)


def _timestamp_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%H:%M") if value.tzinfo else value.strftime("%H:%M")
    rendered = _text(value)
    if "T" in rendered:
        rendered = rendered.split("T", 1)[1]
    elif " " in rendered:
        rendered = rendered.rsplit(" ", 1)[-1]
    return rendered[:5]


def _coerce_event(source: Any) -> RecentEvent:
    if isinstance(source, RecentEvent):
        return RecentEvent(
            _timestamp_text(source.timestamp),
            _text(source.summary),
            normalize_status_key(source.status),
            _text(source.detail),
        )
    if isinstance(source, str):
        return RecentEvent("", source)
    timestamp = _value(
        source,
        "timestamp",
        _value(source, "created_at", _value(source, "occurred_at", _value(source, "time", ""))),
    )
    summary = _value(
        source,
        "summary",
        _value(source, "message", _value(source, "title", _value(source, "reason", ""))),
    )
    status = _value(
        source,
        "status",
        _value(source, "severity", _value(source, "level", "unknown")),
    )
    detail = _value(source, "detail", _value(source, "description", ""))
    return RecentEvent(
        _timestamp_text(timestamp),
        _text(summary),
        normalize_status_key(status),
        _text(detail),
    )


class _EventRow(QFrame):
    def __init__(self, event: RecentEvent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.event_data = event
        self.setObjectName("recentEventRow")
        root = QHBoxLayout(self)
        root.setContentsMargins(SPACING.sm, SPACING.sm, SPACING.sm, SPACING.sm)
        root.setSpacing(SPACING.sm)

        icon = QLabel(self)
        icon.setPixmap(status_icon(event.status).pixmap(QSize(SIZES.icon_sm, SIZES.icon_sm)))
        icon.setAccessibleName(f"{STATUS_LABELS[event.status]} 이벤트 상태 아이콘")
        root.addWidget(icon, 0, Qt.AlignTop)

        self.time_label = QLabel(event.timestamp or "--:--", self)
        self.time_label.setTextFormat(Qt.PlainText)
        self.time_label.setFixedWidth(44)
        root.addWidget(self.time_label, 0, Qt.AlignTop)

        self.summary_label = QLabel(event.summary or "내용 없음", self)
        self.summary_label.setTextFormat(Qt.PlainText)
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label, 1)

        self.setAccessibleName(
            " ".join(part for part in (event.timestamp, event.summary) if part) or "이벤트"
        )
        self.setAccessibleDescription(event.detail)
        self.refresh_presentation()

    def refresh_presentation(self) -> None:
        palette = presentation_palette(self)
        semantic = semantic_palette(palette)
        status = status_colors(self.event_data.status, palette)
        self.setStyleSheet(
            "QFrame#recentEventRow {"
            f"background: {semantic.surface.name()};"
            f"border-bottom: 1px solid {semantic.border.name()};"
            f"border-radius: {RADIUS.sm}px;"
            "}"
        )
        self.time_label.setStyleSheet(f"color: {semantic.text_secondary.name()};")
        self.summary_label.setStyleSheet(f"color: {status.foreground.name()};")


class EventList(PaletteAwareWidgetMixin, QWidget):
    """Bounded, storage-agnostic recent-event summary list."""

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        events: Iterable[Any] | None = None,
        parent: QWidget | None = None,
        *,
        maximum_events: int = 10,
        title: str = "최근 이벤트",
    ) -> None:
        super().__init__(parent)
        try:
            self._maximum_events = min(10, max(1, int(maximum_events)))
        except (TypeError, ValueError, OverflowError):
            self._maximum_events = 10
        self._events: list[RecentEvent] = []
        self._event_rows: list[_EventRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING.xs)
        self.title_label = QLabel(str(title), self)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        root.addWidget(self.title_label)

        self.rows_container = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(SPACING.xs)
        root.addWidget(self.rows_container)

        self.empty_state = EmptyState(
            "최근 이벤트가 없습니다",
            "새 이벤트가 발생하면 여기에 요약이 표시됩니다.",
            self,
        )
        root.addWidget(self.empty_state)
        self.setAccessibleName(str(title))
        self.set_events(events)
        self._initialize_palette_awareness()

    @property
    def events(self) -> tuple[RecentEvent, ...]:
        return tuple(self._events)

    @property
    def maximum_events(self) -> int:
        return self._maximum_events

    @property
    def event_rows(self) -> tuple[QWidget, ...]:
        return tuple(self._event_rows)

    def set_events(self, events: Iterable[Any] | None) -> None:
        rendered: list[RecentEvent] = []
        if events is not None:
            if isinstance(events, (str, bytes, Mapping, RecentEvent)):
                iterator = iter((events,))
            else:
                try:
                    iterator = iter(events)
                except TypeError:
                    iterator = iter((events,))
            try:
                for source in iterator:
                    event = _coerce_event(source)
                    if event.summary:
                        rendered.append(event)
                    if len(rendered) >= self._maximum_events:
                        break
            except Exception:
                # Recent-event presentation is supplementary; retain the valid
                # prefix if an external iterable is malformed.
                pass
        self._events = rendered
        self._rebuild_rows()

    def prepend_event(self, event: Any) -> None:
        candidate = _coerce_event(event)
        if not candidate.summary:
            return
        self._events = [candidate, *self._events][0 : self._maximum_events]
        self._rebuild_rows()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS and hasattr(self, "_event_rows"):
            self._refresh_presentation()

    def _rebuild_rows(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._event_rows = [_EventRow(event, self.rows_container) for event in self._events]
        for row in self._event_rows:
            self.rows_layout.addWidget(row)
        self.rows_container.setVisible(bool(self._event_rows))
        self.empty_state.setVisible(not self._event_rows)
        self.setAccessibleDescription(f"최근 이벤트 {len(self._event_rows)}개")
        self._refresh_presentation()

    def _refresh_presentation(self) -> None:
        semantic = semantic_palette(presentation_palette(self))
        self.title_label.setStyleSheet(f"color: {semantic.text_primary.name()};")
        for row in self._event_rows:
            row.refresh_presentation()


RecentEventList = EventList
