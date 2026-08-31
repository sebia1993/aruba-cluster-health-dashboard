from __future__ import annotations

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    Signal,
)

from .device_table_model import DeviceTableModel


class DevicePageModel(QSortFilterProxyModel):
    """Expose one contiguous page from an already filtered and sorted model.

    This proxy never performs a page-local sort. A view's sort request is
    delegated to the source filter proxy first, after which this model slices
    the globally ordered source rows.
    """

    paginationChanged = Signal()
    pageChanged = Signal(int)

    def __init__(
        self,
        source_model: QAbstractItemModel | QObject | None = None,
        parent: QObject | None = None,
        *,
        enabled: bool = False,
        page_size: int = 250,
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
        self._paging_enabled = bool(enabled)
        self._page_size = self._validate_page_size(page_size)
        self._page_index = 0
        self._connected_source: QAbstractItemModel | None = None
        self.setDynamicSortFilter(True)
        if source_model is not None:
            self.setSourceModel(source_model)

    @property
    def paging_enabled(self) -> bool:
        return self._paging_enabled

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def current_page(self) -> int:
        return self._page_index

    def setSourceModel(self, source_model: QAbstractItemModel | None) -> None:  # noqa: N802
        self._disconnect_source()
        super().setSourceModel(source_model)
        self._connected_source = source_model
        if source_model is not None:
            source_model.modelReset.connect(self._source_changed)
            source_model.rowsInserted.connect(self._source_changed)
            source_model.rowsRemoved.connect(self._source_changed)
            source_model.layoutChanged.connect(self._source_changed)
        self._source_changed()

    def set_paging(self, enabled: bool, page_size: int | None = None) -> None:
        new_enabled = bool(enabled)
        new_page_size = (
            self._page_size
            if page_size is None
            else self._validate_page_size(page_size)
        )
        if (
            new_enabled == self._paging_enabled
            and new_page_size == self._page_size
        ):
            return
        old_page = self._page_index
        self.beginFilterChange()
        self._paging_enabled = new_enabled
        self._page_size = new_page_size
        self._page_index = min(self._page_index, self.page_count() - 1)
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        if self._page_index != old_page:
            self.pageChanged.emit(self._page_index)
        self.paginationChanged.emit()

    def set_page(self, index: int) -> None:
        target = min(max(0, int(index)), self.page_count() - 1)
        if target == self._page_index:
            return
        self.beginFilterChange()
        self._page_index = target
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        self.pageChanged.emit(target)
        self.paginationChanged.emit()

    def next_page(self) -> None:
        self.set_page(self._page_index + 1)

    def previous_page(self) -> None:
        self.set_page(self._page_index - 1)

    def filtered_row_count(self) -> int:
        source = self.sourceModel()
        return 0 if source is None else source.rowCount()

    def page_count(self) -> int:
        total = self.filtered_row_count()
        if not self._paging_enabled:
            return 1
        return max(1, (total + self._page_size - 1) // self._page_size)

    def page_start(self) -> int:
        if not self._paging_enabled:
            return 0
        return self._page_index * self._page_size

    def page_end(self) -> int:
        return min(self.filtered_row_count(), self.page_start() + self.rowCount())

    def page_for_source_row(self, source_row: int) -> int:
        row = int(source_row)
        if not 0 <= row < self.filtered_row_count():
            return -1
        return 0 if not self._paging_enabled else row // self._page_size

    def page_for_ip(self, ip: str) -> int:
        source_row = self.source_row_for_ip(ip)
        return self.page_for_source_row(source_row)

    def source_row_for_ip(self, ip: str) -> int:
        source = self.sourceModel()
        if source is None:
            return -1
        target = str(ip).strip()
        row_lookup = getattr(source, "row_for_ip", None)
        if callable(row_lookup):
            return int(row_lookup(target))
        for row in range(source.rowCount()):
            index = source.index(row, 0)
            if str(source.data(index, DeviceTableModel.IpRole)).strip() == target:
                return row
        return -1

    def row_for_ip(self, ip: str) -> int:
        source = self.sourceModel()
        if source is None:
            return -1
        source_row = self.source_row_for_ip(ip)
        if source_row < 0:
            return -1
        proxy_index = self.mapFromSource(source.index(source_row, 0))
        return proxy_index.row() if proxy_index.isValid() else -1

    def filterAcceptsRow(  # noqa: N802
        self,
        source_row: int,
        source_parent: QModelIndex,
    ) -> bool:
        if source_parent.isValid() or not self._paging_enabled:
            return not source_parent.isValid()
        start = self._page_index * self._page_size
        return start <= source_row < start + self._page_size

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        source = self.sourceModel()
        if source is None:
            return
        source.sort(column, order)

    def _source_changed(self, *_args: object) -> None:
        old_page = self._page_index
        self.beginFilterChange()
        self._page_index = min(self._page_index, self.page_count() - 1)
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        if self._page_index != old_page:
            self.pageChanged.emit(self._page_index)
        self.paginationChanged.emit()

    def _disconnect_source(self) -> None:
        source = self._connected_source
        if source is None:
            return
        for signal in (
            source.modelReset,
            source.rowsInserted,
            source.rowsRemoved,
            source.layoutChanged,
        ):
            try:
                signal.disconnect(self._source_changed)
            except (RuntimeError, TypeError):
                pass
        self._connected_source = None

    @staticmethod
    def _validate_page_size(page_size: int) -> int:
        value = int(page_size)
        if value <= 0:
            raise ValueError("page_size must be greater than zero")
        return value


# A concise alias for code that describes the proxy by its pipeline role.
PageFilterModel = DevicePageModel
