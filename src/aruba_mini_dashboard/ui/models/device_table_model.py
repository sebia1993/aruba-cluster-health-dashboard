from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QBrush, QFont

from ..resources import status_icon
from ..theme import normalize_status_key, status_colors
from ..view_models import DashboardView, DeviceView, value


class DeviceTableColumn(IntEnum):
    """Stable column indexes for the full controller table."""

    IP = 0
    NAME = 1
    MM_STATUS = 2
    ACTIVE_CLIENTS = 3
    STANDBY_CLIENTS = 4
    CONNECTION_TYPE = 5
    OVERALL_STATUS = 6
    LAST_SEEN = 7
    MONITORING_SCOPE = 8
    DISTRIBUTION_STATUS = 9


# Keep Qt.UserRole compatible with the former QTableWidget implementation,
# which stored the controller IP on every cell at that role.
IpRole = int(Qt.ItemDataRole.UserRole)
DeviceRole = IpRole + 1
StatusKeyRole = IpRole + 2
ActiveIncidentRole = IpRole + 3
MonitoringRole = IpRole + 4
SortRole = IpRole + 5
IP_ROLE = IpRole
DEVICE_ROLE = DeviceRole
STATUS_KEY_ROLE = StatusKeyRole
ACTIVE_INCIDENT_ROLE = ActiveIncidentRole
MONITORING_ROLE = MonitoringRole
SORT_ROLE = SortRole


@dataclass(frozen=True, slots=True)
class _DeviceRow:
    device: DeviceView
    monitored: bool
    has_active_incident: bool
    status_key: str
    values: tuple[str, ...]


_MONITORING_SCOPE_UNSET = object()


class DeviceTableModel(QAbstractTableModel):
    """Read-only presentation model for an existing dashboard snapshot.

    The model deliberately consumes :class:`DeviceView` values instead of
    deriving health from raw collector output.  Its only transformations are
    presentation concerns already used by the full dashboard table: column
    formatting, monitoring-scope labelling, icons, colours, and sort keys.
    """

    COLUMNS = (
        "IP",
        "장비명",
        "MM 보고 상태",
        "Active",
        "Standby",
        "Connection-Type",
        "종합 상태",
        "마지막 확인",
        "감시 범위",
        "분배 상태",
    )

    IpRole = IpRole
    DeviceRole = DeviceRole
    StatusKeyRole = StatusKeyRole
    ActiveIncidentRole = ActiveIncidentRole
    MonitoringRole = MonitoringRole
    RegisteredRole = MonitoringRole
    SortRole = SortRole

    def __init__(
        self,
        snapshot: DashboardView | Iterable[DeviceView] | QObject | None = None,
        parent: QObject | None = None,
        *,
        active_incident_ips: Iterable[str] | None = None,
        monitoring_scope_ips: Iterable[str] | None | object = _MONITORING_SCOPE_UNSET,
    ) -> None:
        if isinstance(snapshot, QObject) and not isinstance(snapshot, DashboardView):
            if parent is not None:
                raise TypeError("parent was supplied twice")
            parent = snapshot
            snapshot = None
        super().__init__(parent)
        self._dashboard: DashboardView | None = None
        self._devices: tuple[DeviceView, ...] = ()
        self._active_incident_ips: frozenset[str] = frozenset()
        self._monitoring_scope_ips: frozenset[str] | None = None
        self._rows: tuple[_DeviceRow, ...] = ()
        self._signature: tuple[Any, ...] = ((), frozenset(), None)
        if snapshot is not None:
            self.set_snapshot(
                snapshot,
                active_incident_ips=active_incident_ips,
                monitoring_scope_ips=monitoring_scope_ips,
            )

    @property
    def dashboard(self) -> DashboardView | None:
        return self._dashboard

    @property
    def devices(self) -> tuple[DeviceView, ...]:
        return self._devices

    @property
    def active_incident_ips(self) -> frozenset[str]:
        return self._active_incident_ips

    @property
    def monitoring_scope_ips(self) -> frozenset[str] | None:
        """Explicit scope, or ``None`` when each DeviceView owns that flag."""

        return self._monitoring_scope_ips

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.COLUMNS):
            if role in {
                int(Qt.ItemDataRole.DisplayRole),
                int(Qt.ItemDataRole.AccessibleTextRole),
            }:
                return self.COLUMNS[section]
            if role == int(Qt.ItemDataRole.TextAlignmentRole):
                return Qt.AlignmentFlag.AlignCenter
        if (
            orientation == Qt.Orientation.Vertical
            and 0 <= section < len(self._rows)
            and role == int(Qt.ItemDataRole.DisplayRole)
        ):
            return section + 1
        return None

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802
        names = dict(super().roleNames())
        names.update(
            {
                self.IpRole: b"deviceIp",
                self.DeviceRole: b"device",
                self.StatusKeyRole: b"statusKey",
                self.ActiveIncidentRole: b"hasActiveIncident",
                self.MonitoringRole: b"isMonitored",
                self.SortRole: b"sortValue",
            }
        )
        return names

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(
        self,
        index: QModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if (
            not index.isValid()
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(self.COLUMNS)
        ):
            return None

        row = self._rows[index.row()]
        column = index.column()
        text = row.values[column]

        if role == int(Qt.ItemDataRole.DisplayRole):
            return text
        if role == self.IpRole:
            return row.device.ip
        if role == self.DeviceRole:
            return row.device
        if role == self.StatusKeyRole:
            return row.status_key
        if role == self.ActiveIncidentRole:
            return row.has_active_incident
        if role == self.MonitoringRole:
            return row.monitored
        if role == self.SortRole:
            return self._sort_value(row, column)
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return text
        if role == int(Qt.ItemDataRole.AccessibleTextRole):
            return f"{self.COLUMNS[column]}: {text}"
        if role == int(Qt.ItemDataRole.TextAlignmentRole):
            return self._alignment(column)

        presentation_status = row.status_key
        if role == int(Qt.ItemDataRole.DecorationRole) and column == DeviceTableColumn.OVERALL_STATUS:
            return status_icon(presentation_status)
        if role == int(Qt.ItemDataRole.ForegroundRole):
            if column == DeviceTableColumn.OVERALL_STATUS:
                return QBrush(status_colors(presentation_status).foreground)
            if column == DeviceTableColumn.MONITORING_SCOPE and not row.monitored:
                return QBrush(status_colors("unknown").foreground)
        if role == int(Qt.ItemDataRole.BackgroundRole) and column == DeviceTableColumn.OVERALL_STATUS:
            return QBrush(status_colors(presentation_status).background)
        if role == int(Qt.ItemDataRole.FontRole) and column == DeviceTableColumn.OVERALL_STATUS:
            font = QFont()
            font.setBold(True)
            return font
        return None

    def set_snapshot(
        self,
        snapshot: DashboardView | Iterable[DeviceView],
        *,
        active_incident_ips: Iterable[str] | None = None,
        monitoring_scope_ips: Iterable[str] | None | object = _MONITORING_SCOPE_UNSET,
    ) -> None:
        """Atomically replace the displayed snapshot.

        A model reset makes every old proxy/persistent index invalid before the
        new rows become visible.  Consumers can restore selection by reading
        ``IpRole`` before the update and looking the IP up afterwards.
        """

        if isinstance(snapshot, DashboardView):
            dashboard: DashboardView | None = snapshot
            devices = tuple(snapshot.devices)
            if monitoring_scope_ips is _MONITORING_SCOPE_UNSET:
                # An empty DashboardView scope historically means "use each
                # DeviceView's registration flag", not "monitor nothing".
                monitoring_scope_ips = (
                    snapshot.monitoring_scope_ips or _MONITORING_SCOPE_UNSET
                )
        else:
            dashboard = None
            devices = tuple(snapshot)

        self._validate_devices(devices)
        scope = self._normalize_scope(monitoring_scope_ips)
        incidents = self._normalize_ips(active_incident_ips or ())
        rows = self._make_rows(devices, incidents, scope)
        signature = self._snapshot_signature(rows, incidents, scope)
        if signature == self._signature:
            # Refresh object references without emitting a repaint for an
            # equivalent snapshot. DeviceRole remains current while the table
            # keeps its selection and scroll position intact.
            self._dashboard = dashboard
            self._devices = devices
            self._active_incident_ips = incidents
            self._monitoring_scope_ips = scope
            self._rows = rows
            return

        self.beginResetModel()
        try:
            self._dashboard = dashboard
            self._devices = devices
            self._active_incident_ips = incidents
            self._monitoring_scope_ips = scope
            self._rows = rows
            self._signature = signature
        finally:
            self.endResetModel()

    def set_devices(
        self,
        devices: Iterable[DeviceView],
        *,
        active_incident_ips: Iterable[str] | None = None,
        monitoring_scope_ips: Iterable[str] | None | object = _MONITORING_SCOPE_UNSET,
    ) -> None:
        """Convenience alias for callers that already hold DeviceView rows."""

        self.set_snapshot(
            devices,
            active_incident_ips=active_incident_ips,
            monitoring_scope_ips=monitoring_scope_ips,
        )

    def set_active_incident_ips(self, ips: Iterable[str]) -> None:
        incidents = self._normalize_ips(ips)
        if incidents == self._active_incident_ips:
            return
        self._active_incident_ips = incidents
        self._rows = tuple(
            replace(row, has_active_incident=row.device.ip.strip() in incidents)
            for row in self._rows
        )
        self._signature = self._snapshot_signature(
            self._rows,
            incidents,
            self._monitoring_scope_ips,
        )
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, len(self.COLUMNS) - 1),
                [self.ActiveIncidentRole],
            )

    def set_monitoring_scope_ips(self, ips: Iterable[str] | None) -> None:
        scope = None if ips is None else self._normalize_ips(ips)
        if scope == self._monitoring_scope_ips:
            return
        self.beginResetModel()
        try:
            self._monitoring_scope_ips = scope
            self._rows = self._make_rows(
                self._devices,
                self._active_incident_ips,
                scope,
            )
            self._signature = self._snapshot_signature(
                self._rows,
                self._active_incident_ips,
                scope,
            )
        finally:
            self.endResetModel()

    def device_at(self, row: int) -> DeviceView | None:
        if 0 <= row < len(self._rows):
            return self._rows[row].device
        return None

    def row_for_ip(self, ip: str) -> int:
        target = str(ip).strip()
        return next(
            (
                row
                for row, item in enumerate(self._rows)
                if item.device.ip.strip() == target
            ),
            -1,
        )

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        """Provide deterministic sorting when the model is used without a proxy."""

        if not 0 <= column < len(self.COLUMNS) or len(self._rows) < 2:
            return
        rows = sorted(self._rows, key=lambda row: self._ip_key(row.device.ip))
        rows.sort(
            key=lambda row: self._comparable_sort_value(row, column),
            reverse=order == Qt.SortOrder.DescendingOrder,
        )
        if tuple(rows) == self._rows:
            return
        self.beginResetModel()
        try:
            self._rows = tuple(rows)
            self._devices = tuple(row.device for row in rows)
            self._signature = self._snapshot_signature(
                self._rows,
                self._active_incident_ips,
                self._monitoring_scope_ips,
            )
        finally:
            self.endResetModel()

    @classmethod
    def _make_rows(
        cls,
        devices: Sequence[DeviceView],
        active_incident_ips: frozenset[str],
        monitoring_scope_ips: frozenset[str] | None,
    ) -> tuple[_DeviceRow, ...]:
        rows: list[_DeviceRow] = []
        for device in devices:
            ip = device.ip.strip()
            monitored = (
                bool(device.is_registered)
                if monitoring_scope_ips is None
                else ip in monitoring_scope_ips
            )
            effective_status_key = (
                normalize_status_key(device.status_key) if monitored else "unknown"
            )
            values = (
                device.ip,
                device.alias or device.hostname or "-",
                device.controller_status,
                device.active_clients,
                device.standby_clients,
                device.connection_type,
                device.status if monitored else "감시 제외",
                device.last_seen,
                "등록" if monitored else "미등록 · 감시 제외",
                device.distribution_status,
            )
            rows.append(
                _DeviceRow(
                    device=device,
                    monitored=monitored,
                    has_active_incident=ip in active_incident_ips,
                    status_key=effective_status_key,
                    values=tuple(str(item) for item in values),
                )
            )
        return tuple(rows)

    @staticmethod
    def _validate_devices(devices: Sequence[DeviceView]) -> None:
        invalid = next((item for item in devices if not isinstance(item, DeviceView)), None)
        if invalid is not None:
            raise TypeError("DeviceTableModel accepts DashboardView or DeviceView values")

    @staticmethod
    def _normalize_ips(ips: Iterable[str]) -> frozenset[str]:
        return frozenset(
            normalized
            for ip in ips
            if (normalized := str(ip).strip())
        )

    @classmethod
    def _normalize_scope(
        cls,
        scope: Iterable[str] | None | object,
    ) -> frozenset[str] | None:
        if scope is _MONITORING_SCOPE_UNSET or scope is None:
            return None
        return cls._normalize_ips(scope)

    @staticmethod
    def _snapshot_signature(
        rows: Sequence[_DeviceRow],
        active_incident_ips: frozenset[str],
        monitoring_scope_ips: frozenset[str] | None,
    ) -> tuple[Any, ...]:
        row_signatures = tuple(
            (
                row.values,
                row.monitored,
                row.has_active_incident,
                row.status_key,
                row.device.ip,
                row.device.alias,
                row.device.hostname,
                row.device.mm_status,
                row.device.active_clients,
                row.device.standby_clients,
                row.device.connection_type,
                row.device.status,
                row.device.status_key,
                row.device.last_seen,
                row.device.is_registered,
                row.device.controller_state,
                row.device.controller_status,
                row.device.distribution_state,
                row.device.distribution_status,
                row.device.load_anomaly_streak,
                tuple(row.device.issue_reasons),
            )
            for row in rows
        )
        return (row_signatures, active_incident_ips, monitoring_scope_ips)

    @staticmethod
    def _alignment(column: int) -> Qt.AlignmentFlag | None:
        if column in {
            DeviceTableColumn.MM_STATUS,
            DeviceTableColumn.ACTIVE_CLIENTS,
            DeviceTableColumn.STANDBY_CLIENTS,
            DeviceTableColumn.CONNECTION_TYPE,
            DeviceTableColumn.OVERALL_STATUS,
            DeviceTableColumn.LAST_SEEN,
            DeviceTableColumn.MONITORING_SCOPE,
            DeviceTableColumn.DISTRIBUTION_STATUS,
        }:
            return Qt.AlignmentFlag.AlignCenter
        return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

    @classmethod
    def _sort_value(cls, row: _DeviceRow, column: int) -> Any:
        if column in {
            DeviceTableColumn.ACTIVE_CLIENTS,
            DeviceTableColumn.STANDBY_CLIENTS,
        }:
            return cls._integer_sort_value(row.values[column])
        if column == DeviceTableColumn.LAST_SEEN:
            source_value = value(row.device.source, "last_seen", row.device.last_seen)
            return cls._timestamp_sort_value(source_value, row.device.last_seen)
        return row.values[column].casefold()

    @classmethod
    def _comparable_sort_value(cls, row: _DeviceRow, column: int) -> tuple[int, Any]:
        sort_value = cls._sort_value(row, column)
        if isinstance(sort_value, (int, float)) and not isinstance(sort_value, bool):
            return (0, sort_value)
        return (1, str(sort_value).casefold())

    @staticmethod
    def _integer_sort_value(raw: Any) -> int:
        try:
            return int(str(raw).strip().replace(",", ""))
        except (TypeError, ValueError):
            return -1

    @classmethod
    def _timestamp_sort_value(cls, raw: Any, rendered: str) -> float:
        if isinstance(raw, datetime):
            candidate = raw
        else:
            text = str(raw or rendered or "").strip()
            if text in {"", "-"}:
                return -math.inf
            try:
                candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                try:
                    candidate = datetime.strptime(rendered, "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    return -math.inf
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        try:
            return candidate.timestamp()
        except (OSError, OverflowError, ValueError):
            return -math.inf

    @staticmethod
    def _ip_key(raw: str) -> str:
        """Match the dashboard's established case-insensitive lexical order."""

        return str(raw).casefold()
