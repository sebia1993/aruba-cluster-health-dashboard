from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import SPACING, normalize_status_key
from ..view_models import DashboardView, DeviceView, value
from ..widgets import EventList, MetricCard, StatusCard


def _active_client_value(device: DeviceView) -> int | None:
    raw = value(device.source, "active_clients", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        result = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _incident_is_active(incident: Any) -> bool:
    """Return lifecycle activity without treating ACK as recovery."""

    return bool(value(incident, "active", True))


class OverviewPage(QWidget):
    """Operations overview composed from existing dashboard view models.

    This widget owns presentation-only aggregation and a bounded session trend.
    It deliberately has no storage, collector, parser, or incident-manager
    dependency, so a rendering failure cannot influence monitoring state.
    """

    HISTORY_LIMIT = 60
    MAX_CONTROLLER_CARDS = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._active_client_history: deque[int | None] = deque(maxlen=self.HISTORY_LIMIT)
        self._controller_cards: list[StatusCard] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING.sm)

        self.kpi_layout = QGridLayout()
        self.kpi_layout.setContentsMargins(0, 0, 0, 0)
        self.kpi_layout.setHorizontalSpacing(SPACING.sm)
        self.kpi_layout.setVerticalSpacing(SPACING.sm)

        self.overall_card = StatusCard("전체 상태", "unknown", "점검 전", self)
        self.controller_card = MetricCard("Controller Up", "- / -", parent=self)
        self.active_client_card = MetricCard(
            "전체 Active Client",
            "-",
            "세션 내 최근 60회",
            self,
            show_sparkline=True,
        )
        self.incident_card = MetricCard("활성 Incident", "0", parent=self)
        for column, card in enumerate(
            (
                self.overall_card,
                self.controller_card,
                self.active_client_card,
                self.incident_card,
            )
        ):
            self.kpi_layout.addWidget(card, 0, column)
            self.kpi_layout.setColumnStretch(column, 1)
        root.addLayout(self.kpi_layout)

        lower = QHBoxLayout()
        lower.setContentsMargins(0, 0, 0, 0)
        lower.setSpacing(SPACING.sm)

        self.controller_region = QWidget(self)
        controller_root = QVBoxLayout(self.controller_region)
        controller_root.setContentsMargins(0, 0, 0, 0)
        controller_root.setSpacing(SPACING.xs)
        self.controller_title = QLabel("등록 Controller", self.controller_region)
        controller_font = self.controller_title.font()
        controller_font.setBold(True)
        self.controller_title.setFont(controller_font)
        controller_root.addWidget(self.controller_title)
        self.controller_grid = QGridLayout()
        self.controller_grid.setContentsMargins(0, 0, 0, 0)
        self.controller_grid.setHorizontalSpacing(SPACING.xs)
        self.controller_grid.setVerticalSpacing(SPACING.xs)
        controller_root.addLayout(self.controller_grid)
        self.controller_overflow_label = QLabel("", self.controller_region)
        self.controller_overflow_label.setWordWrap(True)
        self.controller_overflow_label.setAccessibleName("추가 Controller 안내")
        controller_root.addWidget(self.controller_overflow_label)
        lower.addWidget(self.controller_region, 3)

        self.recent_events = EventList(maximum_events=5, parent=self)
        self.recent_events.setMinimumWidth(260)
        lower.addWidget(self.recent_events, 2)
        root.addLayout(lower)

        self.setAccessibleName("운영 상태 개요")
        self.setAccessibleDescription("전체 상태, Controller, Client, Incident와 최근 이벤트 요약")

    @property
    def active_client_history(self) -> tuple[int | None, ...]:
        return tuple(self._active_client_history)

    @property
    def controller_cards(self) -> tuple[StatusCard, ...]:
        return tuple(self._controller_cards)

    def update_dashboard(
        self,
        dashboard: DashboardView,
        devices: Iterable[DeviceView],
        active_incidents: Iterable[Any],
    ) -> None:
        monitored = list(devices)
        up_count = sum(device.controller_state == "up" for device in monitored)
        active_values = [_active_client_value(device) for device in monitored]
        known_values = [item for item in active_values if item is not None]
        total_active = sum(known_values) if known_values else None
        active_incident_count = sum(_incident_is_active(item) for item in active_incidents)

        self.overall_card.set_status(dashboard.status_key, dashboard.status)
        self.overall_card.set_detail(
            "이상 신호 없음" if not dashboard.reasons else " / ".join(dashboard.reasons[:2])
        )
        self.controller_card.set_value(f"{up_count} / {len(monitored)}")
        self.controller_card.set_subtitle("등록·감시 대상 기준")
        self.active_client_card.set_value(total_active)
        if known_values and len(known_values) != len(monitored):
            self.active_client_card.set_subtitle(
                f"{len(known_values)}/{len(monitored)}대 확인 · 세션 내 최근 60회"
            )
        elif known_values:
            self.active_client_card.set_subtitle("세션 내 최근 60회")
        else:
            self.active_client_card.set_subtitle("확인 가능한 값 없음 · 세션 내 최근 60회")
        self._active_client_history.append(total_active)
        self.active_client_card.set_samples(self._active_client_history)
        self.active_client_card.sparkline.set_status(dashboard.status_key)
        self.incident_card.set_value(active_incident_count)
        self.incident_card.set_subtitle("ACK 여부와 무관한 활성 건수")
        self._rebuild_controller_cards(monitored)

    def set_recent_events(self, events: Iterable[Any] | None, *, stale: bool = False) -> None:
        self.recent_events.title_label.setText(
            "최근 이벤트 · 불러오기 지연" if stale else "최근 이벤트"
        )
        self.recent_events.set_events(events)
        if stale:
            self.recent_events.setAccessibleDescription(
                "최근 이벤트를 불러오지 못했습니다. 모니터링 상태에는 영향이 없습니다."
            )

    def _rebuild_controller_cards(self, devices: list[DeviceView]) -> None:
        while self.controller_grid.count():
            item = self.controller_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._controller_cards = []

        visible_devices = devices[: self.MAX_CONTROLLER_CARDS]
        for index, device in enumerate(visible_devices):
            name = device.alias or device.hostname or device.ip or "Controller"
            detail_parts = [
                f"Active {device.active_clients}",
                f"Standby {device.standby_clients}",
                f"Connection {device.connection_type}",
            ]
            card = StatusCard(
                name,
                normalize_status_key(device.status_key),
                " · ".join(detail_parts),
                self.controller_region,
            )
            card.badge.set_status(device.status_key, device.status)
            card.setToolTip(f"{name}\nIP: {device.ip}")
            card.setAccessibleDescription(
                f"{device.status}. Active {device.active_clients}, "
                f"Standby {device.standby_clients}, Connection-Type {device.connection_type}"
            )
            self.controller_grid.addWidget(card, index // 2, index % 2)
            self._controller_cards.append(card)

        self.controller_region.setVisible(bool(self._controller_cards))
        self.controller_title.setText(
            f"등록 Controller {len(devices)}대"
            if devices
            else "등록 Controller 없음"
        )
        hidden_count = max(0, len(devices) - len(visible_devices))
        self.controller_overflow_label.setText(
            f"그 외 {hidden_count}대는 아래 장비표에서 확인하세요."
            if hidden_count
            else ""
        )
        self.controller_overflow_label.setVisible(bool(hidden_count))
