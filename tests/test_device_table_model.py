from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QIcon
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.ui.models import (
    ActiveIncidentRole,
    DeviceRole,
    DeviceTableColumn,
    DeviceTableModel,
    IpRole,
    MonitoringRole,
    SortRole,
    StatusKeyRole,
)
from aruba_mini_dashboard.ui.models.device_filter_model import DeviceFilterModel
from aruba_mini_dashboard.ui.view_models import DashboardView, DeviceView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _device(
    ip: str,
    *,
    alias: str = "",
    hostname: str = "",
    status_key: str = "normal",
    status: str = "정상",
    active: str = "10",
    standby: str = "8",
    last_seen: str = "2026-08-31 12:30:00",
    raw_last_seen: object | None = None,
    registered: bool = True,
) -> DeviceView:
    source = {
        "ip": ip,
        "last_seen": raw_last_seen if raw_last_seen is not None else last_seen,
        "marker": object(),
    }
    return DeviceView(
        source=source,
        ip=ip,
        alias=alias,
        hostname=hostname,
        mm_status="Up",
        active_clients=active,
        standby_clients=standby,
        connection_type="COMMANDER",
        status=status,
        status_key=status_key,
        last_seen=last_seen,
        is_registered=registered,
        controller_state="up",
        controller_status="Up",
        distribution_state="normal",
        distribution_status="정상",
    )


def _dashboard(
    devices: list[DeviceView],
    *,
    scope: list[str] | None = None,
) -> DashboardView:
    return DashboardView(
        source={"devices": [item.source for item in devices]},
        status="정상",
        status_key="normal",
        devices=devices,
        problem_ips=[],
        reasons=[],
        checked_at="2026-08-31 12:30:00",
        checked_at_short="12:30:00",
        monitoring_scope_ips=list(scope or []),
    )


def test_full_table_columns_and_qt_roles_preserve_device_view_meaning() -> None:
    _app()
    device = _device(
        "192.0.2.11",
        alias="WLC-01",
        hostname="controller-01",
        status_key="critical",
        status="장애",
        active="123",
        standby="45",
    )
    model = DeviceTableModel(
        _dashboard([device], scope=[device.ip]),
        active_incident_ips=[device.ip],
    )

    assert model.COLUMNS == (
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
    assert model.rowCount() == 1
    assert model.columnCount() == 10
    assert [
        model.headerData(column, Qt.Horizontal, Qt.DisplayRole)
        for column in range(model.columnCount())
    ] == list(model.COLUMNS)
    assert [
        model.data(model.index(0, column), Qt.DisplayRole)
        for column in range(model.columnCount())
    ] == [
        "192.0.2.11",
        "WLC-01",
        "Up",
        "123",
        "45",
        "COMMANDER",
        "장애",
        "2026-08-31 12:30:00",
        "등록",
        "정상",
    ]

    for column in range(model.columnCount()):
        index = model.index(0, column)
        assert model.data(index, Qt.UserRole) == device.ip
        assert model.data(index, IpRole) == device.ip
        assert model.data(index, DeviceRole) is device
        assert model.data(index, StatusKeyRole) == "failure"
        assert model.data(index, ActiveIncidentRole) is True
        assert model.data(index, MonitoringRole) is True
        assert model.data(index, Qt.ToolTipRole) == model.data(index, Qt.DisplayRole)
        assert model.data(index, Qt.AccessibleTextRole).startswith(
            f"{model.COLUMNS[column]}: "
        )
        assert model.data(index, Qt.TextAlignmentRole) is not None

    status_index = model.index(0, DeviceTableColumn.OVERALL_STATUS)
    assert isinstance(model.data(status_index, Qt.DecorationRole), QIcon)
    assert not model.data(status_index, Qt.DecorationRole).isNull()
    assert isinstance(model.data(status_index, Qt.ForegroundRole), QBrush)
    assert isinstance(model.data(status_index, Qt.BackgroundRole), QBrush)
    assert model.data(status_index, Qt.FontRole).bold()
    assert model.flags(status_index) == Qt.ItemIsEnabled | Qt.ItemIsSelectable
    assert model.roleNames()[IpRole] == b"deviceIp"
    assert model.device_at(0) is device
    assert model.row_for_ip(device.ip) == 0


def test_unmonitored_rows_keep_existing_full_table_labels_and_effective_unknown_status() -> None:
    _app()
    device = _device(
        "192.0.2.50",
        alias="OUT-OF-SCOPE",
        status_key="attention",
        status="주의",
        registered=True,
    )
    model = DeviceTableModel(
        _dashboard([device]),
        monitoring_scope_ips=[],
    )

    assert model.data(model.index(0, DeviceTableColumn.OVERALL_STATUS)) == "감시 제외"
    assert model.data(model.index(0, DeviceTableColumn.MONITORING_SCOPE)) == (
        "미등록 · 감시 제외"
    )
    assert model.data(model.index(0, 0), StatusKeyRole) == "unknown"
    assert model.data(model.index(0, 0), MonitoringRole) is False
    assert isinstance(
        model.data(model.index(0, DeviceTableColumn.MONITORING_SCOPE), Qt.ForegroundRole),
        QBrush,
    )
    # The presentation layer must not rewrite the domain-derived DeviceView.
    assert device.status_key == "attention"
    assert device.status == "주의"


def test_sort_roles_use_numbers_and_safe_timestamps() -> None:
    _app()
    older = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    devices = [
        _device("192.0.2.10", active="10", raw_last_seen=newer),
        _device("192.0.2.2", active="2", raw_last_seen=older),
        _device(
            "192.0.2.3",
            active="1,000",
            last_seen="-",
            raw_last_seen="not-a-date",
        ),
    ]
    model = DeviceTableModel(devices)

    assert [
        model.data(model.index(row, DeviceTableColumn.ACTIVE_CLIENTS), SortRole)
        for row in range(3)
    ] == [10, 2, 1000]
    last_seen_keys = [
        model.data(model.index(row, DeviceTableColumn.LAST_SEEN), SortRole)
        for row in range(3)
    ]
    assert last_seen_keys[0] > last_seen_keys[1]
    assert last_seen_keys[2] == float("-inf")

    model.sort(DeviceTableColumn.ACTIVE_CLIENTS, Qt.AscendingOrder)
    assert [model.data(model.index(row, 0), IpRole) for row in range(3)] == [
        "192.0.2.2",
        "192.0.2.10",
        "192.0.2.3",
    ]


def test_equivalent_snapshot_is_a_noop_but_changed_snapshot_resets_atomically() -> None:
    _app()
    original = _device("192.0.2.11", alias="WLC-01")
    model = DeviceTableModel(
        _dashboard([original], scope=[original.ip]),
        active_incident_ips=[original.ip],
    )
    reset_spy = QSignalSpy(model.modelReset)
    persistent = QPersistentModelIndex(model.index(0, 0))

    equivalent = _device("192.0.2.11", alias="WLC-01")
    model.set_snapshot(
        _dashboard([equivalent], scope=[equivalent.ip]),
        active_incident_ips=[equivalent.ip],
    )

    assert reset_spy.count() == 0
    assert persistent.isValid()
    assert model.data(model.index(0, 0), DeviceRole) is equivalent

    replacement = _device("192.0.2.12", alias="WLC-02")
    model.set_snapshot(
        _dashboard([replacement], scope=[replacement.ip]),
        active_incident_ips=[],
    )

    assert reset_spy.count() == 1
    assert not persistent.isValid()
    assert model.data(model.index(0, 0), IpRole) == replacement.ip


def test_proxy_index_becomes_invalid_safely_when_filtered_snapshot_is_replaced() -> None:
    _app()
    first = _device("192.0.2.11", alias="MATCH-FIRST")
    source = DeviceTableModel(
        _dashboard([first], scope=[first.ip]),
        active_incident_ips=[first.ip],
    )
    proxy = DeviceFilterModel(source)
    proxy.set_search_text("match")
    proxy.set_incident_only(True)
    assert proxy.rowCount() == 1
    old_proxy_index = QPersistentModelIndex(proxy.index(0, 0))

    replacement = _device("192.0.2.12", alias="MATCH-SECOND")
    source.set_snapshot(
        _dashboard([replacement], scope=[replacement.ip]),
        active_incident_ips=[replacement.ip],
    )

    assert not old_proxy_index.isValid()
    assert proxy.rowCount() == 1
    assert proxy.data(proxy.index(0, 0), IpRole) == replacement.ip
