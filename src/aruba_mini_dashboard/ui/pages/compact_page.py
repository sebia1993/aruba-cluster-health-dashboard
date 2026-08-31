from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..resources import status_icon
from ..theme import status_colors
from ..view_models import DeviceView, display
from ..widgets import SubtleSelectionTableWidget


class CompactPage(QWidget):
    """Small-window dashboard presentation and compact-row renderer.

    Window lifecycle, coordinator wiring, menus, and responsive switching stay
    in ``MainWindow``. This page owns only its widgets and the presentation
    work needed to render already-derived ``DeviceView`` values.
    """

    COLUMNS = ("컨트롤러", "상태", "클러스터 분배")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row_signatures: dict[str, tuple[Any, ...]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 7, 8, 7)
        root.setSpacing(5)

        self.status_card = QFrame(self)
        self.status_card.setObjectName("compactStatusCard")
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(10, 6, 10, 6)
        status_layout.setSpacing(6)
        self.status_label = QLabel("확인 불가", self.status_card)
        font = self.status_label.font()
        font.setPointSize(max(font.pointSize() + 4, 13))
        font.setBold(True)
        self.status_label.setFont(font)
        self.status_label.setAccessibleName("전체 상태")
        status_layout.addWidget(self.status_label)
        self.busy_label = QLabel("", self.status_card)
        self.busy_label.setStyleSheet("color: #52606D;")
        status_layout.addWidget(self.busy_label)
        status_layout.addStretch(1)
        self.last_check_label = QLabel("마지막: -", self.status_card)
        self.last_check_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.last_check_label.setAccessibleName("마지막 점검 시각")
        status_layout.addWidget(self.last_check_label)
        root.addWidget(self.status_card)

        controls = QHBoxLayout()
        controls.setSpacing(5)
        self.check_now_button = QPushButton("지금 점검", self)
        self.auto_button = QPushButton("자동 시작", self)
        self.more_button = QToolButton(self)
        self.more_button.setText("더보기")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        for button in (self.check_now_button, self.auto_button, self.more_button):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            controls.addWidget(button, 1)
        root.addLayout(controls)

        self.table = SubtleSelectionTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setSortingEnabled(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().setDefaultSectionSize(27)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(1, 78)
        header.resizeSection(2, 126)
        root.addWidget(self.table, 1)

        self.setAccessibleName("Compact 운영 대시보드")

    def populate_devices(self, devices: Sequence[DeviceView]) -> None:
        rows: list[tuple[DeviceView, str, tuple[str, ...], str, str]] = []
        signatures: dict[str, tuple[Any, ...]] = {}
        for device in devices:
            name = device.alias or device.hostname or "컨트롤러"
            values = (
                f"{name} · {device.ip}",
                device.controller_status,
                device.distribution_status,
            )
            controller_key = {
                "up": "normal",
                "down": "failure",
                "missing": "attention",
            }.get(device.controller_state, "unknown")
            distribution_key = {
                "normal": "normal",
                "observing": "attention",
                "anomalous": "attention",
                "recovering": "attention",
                "low_usage": "attention",
                "missing": "attention",
            }.get(device.distribution_state, "unknown")
            signatures[device.ip] = (*values, controller_key, distribution_key)
            rows.append((device, name, values, controller_key, distribution_key))

        identities = [device.ip for device in devices]
        shape_changed = self._ensure_table_shape(identities)
        if not shape_changed and signatures == self._row_signatures:
            return

        self.table.setUpdatesEnabled(False)
        try:
            for row, (device, name, values, controller_key, distribution_key) in enumerate(rows):
                if not shape_changed and self._row_signatures.get(device.ip) == signatures[device.ip]:
                    continue
                for column, text in enumerate(values):
                    item = self.table.item(row, column) or QTableWidgetItem()
                    if item.text() != text:
                        item.setText(text)
                    item.setData(Qt.UserRole, device.ip)
                    item.setToolTip(f"{name}\nIP: {device.ip}" if column == 0 else str(text))
                    if column in {1, 2}:
                        style_key = controller_key if column == 1 else distribution_key
                        foreground = status_colors(style_key, self.table.palette()).foreground
                        item.setForeground(QColor(foreground))
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setIcon(status_icon(style_key))
                    if self.table.item(row, column) is None:
                        self.table.setItem(row, column, item)
        finally:
            self.table.setUpdatesEnabled(True)
        self._row_signatures = signatures

    def _ensure_table_shape(self, identities: list[str]) -> bool:
        existing = [
            display(self.table.item(row, 0).data(Qt.UserRole), "")
            if self.table.item(row, 0) is not None
            else ""
            for row in range(self.table.rowCount())
        ]
        if existing == identities and self.table.columnCount() == len(self.COLUMNS):
            return False
        self.table.clearContents()
        self.table.setRowCount(len(identities))
        return True
