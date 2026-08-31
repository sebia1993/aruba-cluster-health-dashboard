from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QIcon, QPalette
from PySide6.QtWidgets import QAbstractItemView, QTableView

from .widgets.legacy import (
    _SubtleSelectionItemDelegate,
    _blend_colors,
    _ensure_minimum_contrast,
)


class ModelCell:
    """Small read-only compatibility view over a model index.

    The dashboard historically exposed ``QTableWidgetItem`` objects through
    ``MainWindow.table.item()``.  Keeping this adapter while the full device
    table moves to Qt Model/View avoids coupling callers and accessibility
    tooling to the storage mechanism.  Production rendering still comes
    exclusively from the model; no table items are allocated.
    """

    __slots__ = ("_column", "_row", "_table")

    def __init__(self, table: "DeviceTableView", row: int, column: int) -> None:
        self._table = table
        self._row = row
        self._column = column

    def index(self) -> QModelIndex:
        model = self._table.model()
        if model is None:
            return QModelIndex()
        return model.index(self._row, self._column)

    def row(self) -> int:
        return self._row

    def column(self) -> int:
        return self._column

    def text(self) -> str:
        value = self.data(Qt.DisplayRole)
        return "" if value is None else str(value)

    def data(self, role: int = Qt.UserRole) -> Any:
        index = self.index()
        return index.data(role) if index.isValid() else None

    def toolTip(self) -> str:  # noqa: N802 - Qt compatibility API
        value = self.data(Qt.ToolTipRole)
        return "" if value is None else str(value)

    def icon(self) -> QIcon:
        value = self.data(Qt.DecorationRole)
        return value if isinstance(value, QIcon) else QIcon()

    def foreground(self) -> QBrush:
        value = self.data(Qt.ForegroundRole)
        return value if isinstance(value, QBrush) else QBrush()

    def background(self) -> QBrush:
        value = self.data(Qt.BackgroundRole)
        return value if isinstance(value, QBrush) else QBrush()

    def font(self) -> QFont:
        value = self.data(Qt.FontRole)
        return value if isinstance(value, QFont) else QFont(self._table.font())


class DeviceTableView(QTableView):
    """Model/View device table with a narrow legacy read API."""

    itemDoubleClicked = Signal(object)
    itemSelectionChanged = Signal()
    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cell_adapters: dict[tuple[int, int], ModelCell] = {}
        self._selection_style_refreshing = False
        self._selection_style_revision = 0
        self._selection_style_colors: dict[str, str] = {}
        self.refresh_selection_style()
        self._subtle_selection_delegate = _SubtleSelectionItemDelegate(self)
        self.setItemDelegate(self._subtle_selection_delegate)
        self.doubleClicked.connect(self._emit_item_double_clicked)

    @property
    def selection_style_revision(self) -> int:
        return self._selection_style_revision

    @property
    def selection_style_colors(self) -> dict[str, str]:
        return dict(self._selection_style_colors)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if (
            event.type() in self._STYLE_CHANGE_EVENTS
            and hasattr(self, "_selection_style_refreshing")
        ):
            self.refresh_selection_style()

    def refresh_selection_style(self) -> None:
        """Keep row selection neutral while retaining semantic status colours."""

        if self._selection_style_refreshing:
            return
        self._selection_style_refreshing = True
        try:
            palette = self.palette()
            colors: dict[str, str] = {}
            for group, prefix, fill_weight, boundary_weight in (
                (QPalette.Active, "active", 0.12, 0.55),
                (QPalette.Inactive, "inactive", 0.08, 0.50),
            ):
                base = palette.color(group, QPalette.Base)
                text = palette.color(group, QPalette.Text)
                fallback_text = palette.color(group, QPalette.WindowText)
                background = _blend_colors(base, text, fill_weight)
                readable_text = _ensure_minimum_contrast(
                    text,
                    background,
                    fallback_text,
                    minimum=4.5,
                )
                boundary = _blend_colors(base, readable_text, boundary_weight)
                boundary = _ensure_minimum_contrast(
                    boundary,
                    background,
                    readable_text,
                    minimum=3.0,
                )
                colors[f"{prefix}_background"] = background.name()
                colors[f"{prefix}_text"] = readable_text.name()
                colors[f"{prefix}_boundary"] = boundary.name()
            self._selection_style_colors = colors
            self._selection_style_revision += 1
            self.viewport().update()
        finally:
            self._selection_style_refreshing = False

    def setModel(self, model) -> None:  # noqa: N802 - Qt API
        previous = self.selectionModel()
        if previous is not None:
            try:
                previous.selectionChanged.disconnect(self._emit_selection_changed)
            except (RuntimeError, TypeError):
                pass
        super().setModel(model)
        selection = self.selectionModel()
        if selection is not None:
            selection.selectionChanged.connect(self._emit_selection_changed)

    def rowCount(self) -> int:  # noqa: N802 - QTableWidget compatibility
        model = self.model()
        return 0 if model is None else model.rowCount()

    def columnCount(self) -> int:  # noqa: N802 - QTableWidget compatibility
        model = self.model()
        return 0 if model is None else model.columnCount()

    def item(self, row: int, column: int) -> ModelCell | None:
        model = self.model()
        if model is None or not model.index(row, column).isValid():
            return None
        key = (row, column)
        item = self._cell_adapters.get(key)
        if item is None:
            item = ModelCell(self, row, column)
            self._cell_adapters[key] = item
        return item

    def selectedItems(self) -> list[ModelCell]:  # noqa: N802 - compatibility
        selection = self.selectionModel()
        if selection is None:
            return []
        result: list[ModelCell] = []
        for index in selection.selectedIndexes():
            item = self.item(index.row(), index.column())
            if item is not None:
                result.append(item)
        return result

    def sortItems(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:  # noqa: N802
        self.sortByColumn(column, order)

    def visualItemRect(self, item: ModelCell | None) -> QRect:  # noqa: N802
        return QRect() if item is None else self.visualRect(item.index())

    def _emit_item_double_clicked(self, index: QModelIndex) -> None:
        item = self.item(index.row(), index.column())
        if item is not None:
            self.itemDoubleClicked.emit(item)

    def _emit_selection_changed(self, *_args: object) -> None:
        self.itemSelectionChanged.emit()

    def configure_for_dashboard(self) -> None:
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalHeader().setVisible(False)
