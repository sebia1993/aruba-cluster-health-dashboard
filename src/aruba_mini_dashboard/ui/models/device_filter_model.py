from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
)

from ..theme import normalize_status_key
from ..view_models import DeviceView
from .device_table_model import DeviceTableModel


class DeviceFilterModel(QSortFilterProxyModel):
    """Composable search, status, incident, and monitoring filter."""

    ALL_STATUSES = "all"

    def __init__(
        self,
        source_model: DeviceTableModel | QObject | None = None,
        parent: QObject | None = None,
    ) -> None:
        if isinstance(source_model, QObject) and not isinstance(
            source_model,
            QAbstractItemModel,
        ):
            if parent is not None:
                raise TypeError("parent was supplied twice")
            parent = source_model
            source_model = None
        super().__init__(parent)
        self._search_text = ""
        self._status_filter = self.ALL_STATUSES
        self._incident_only = False
        self._monitoring_only = False
        self.setDynamicSortFilter(True)
        self.setSortRole(DeviceTableModel.SortRole)
        if source_model is not None:
            self.setSourceModel(source_model)

    @property
    def search_text(self) -> str:
        return self._search_text

    @property
    def status_filter(self) -> str:
        return self._status_filter

    @property
    def incident_only(self) -> bool:
        return self._incident_only

    @property
    def monitoring_only(self) -> bool:
        return self._monitoring_only

    def set_search_text(self, text: str | None) -> None:
        normalized = str(text or "").strip().casefold()
        if normalized == self._search_text:
            return
        self.beginFilterChange()
        self._search_text = normalized
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_search_query(self, text: str | None) -> None:
        """Alias used by search controls that call their value a query."""

        self.set_search_text(text)

    def set_status_filter(self, status: str | None) -> None:
        raw = str(status or "").strip().casefold()
        normalized = (
            self.ALL_STATUSES
            if raw in {"", "all", "전체", "*"}
            else normalize_status_key(raw)
        )
        if normalized == self._status_filter:
            return
        self.beginFilterChange()
        self._status_filter = normalized
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_incident_only(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._incident_only:
            return
        self.beginFilterChange()
        self._incident_only = enabled
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_monitoring_only(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._monitoring_only:
            return
        self.beginFilterChange()
        self._monitoring_only = enabled
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def clear_filters(self) -> None:
        if (
            not self._search_text
            and self._status_filter == self.ALL_STATUSES
            and not self._incident_only
            and not self._monitoring_only
        ):
            return
        self.beginFilterChange()
        self._search_text = ""
        self._status_filter = self.ALL_STATUSES
        self._incident_only = False
        self._monitoring_only = False
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(  # noqa: N802
        self,
        source_row: int,
        source_parent: QModelIndex,
    ) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False

        device = model.data(index, DeviceTableModel.DeviceRole)
        if not isinstance(device, DeviceView):
            return False
        if self._search_text and not self._matches_search(device):
            return False
        if (
            self._status_filter != self.ALL_STATUSES
            and model.data(index, DeviceTableModel.StatusKeyRole) != self._status_filter
        ):
            return False
        if self._incident_only and not bool(
            model.data(index, DeviceTableModel.ActiveIncidentRole)
        ):
            return False
        if self._monitoring_only and not bool(
            model.data(index, DeviceTableModel.MonitoringRole)
        ):
            return False
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return super().lessThan(left, right)

        left_value = model.data(left, DeviceTableModel.SortRole)
        right_value = model.data(right, DeviceTableModel.SortRole)
        if left.column() == 0:
            left_key: Any = DeviceTableModel._ip_key(str(left_value))
            right_key: Any = DeviceTableModel._ip_key(str(right_value))
        else:
            left_key = self._comparable(left_value)
            right_key = self._comparable(right_value)
        if left_key != right_key:
            return left_key < right_key

        # Keep equal primary values deterministic. Qt reverses lessThan for a
        # descending sort, so reverse the tie comparison here to leave IPs in
        # ascending order in either direction.
        left_ip = DeviceTableModel._ip_key(
            str(model.data(left, DeviceTableModel.IpRole))
        )
        right_ip = DeviceTableModel._ip_key(
            str(model.data(right, DeviceTableModel.IpRole))
        )
        if left_ip == right_ip:
            return left.row() < right.row()
        if self.sortOrder() == Qt.SortOrder.DescendingOrder:
            return left_ip > right_ip
        return left_ip < right_ip

    def device_at(self, proxy_row: int) -> DeviceView | None:
        index = self.index(proxy_row, 0)
        if not index.isValid():
            return None
        device = self.data(index, DeviceTableModel.DeviceRole)
        return device if isinstance(device, DeviceView) else None

    def row_for_ip(self, ip: str) -> int:
        target = str(ip).strip()
        for row in range(self.rowCount()):
            if str(self.data(self.index(row, 0), DeviceTableModel.IpRole)).strip() == target:
                return row
        return -1

    def _matches_search(self, device: DeviceView) -> bool:
        return any(
            self._search_text in str(candidate or "").casefold()
            for candidate in (device.ip, device.alias, device.hostname)
        )

    @staticmethod
    def _comparable(value: Any) -> tuple[int, Any]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (0, value)
        return (1, str(value or "").casefold())
