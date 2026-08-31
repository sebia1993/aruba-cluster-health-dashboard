from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import pytest

from aruba_mini_dashboard.ui.models import (
    DeviceFilterModel,
    DevicePageModel,
    DeviceTableModel,
    IpRole,
    PageFilterModel,
)
from aruba_mini_dashboard.ui.models.device_table_model import DeviceTableColumn
from aruba_mini_dashboard.ui.view_models import DashboardView, DeviceView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _device(
    ip: str,
    *,
    alias: str,
    hostname: str,
    status_key: str,
    status: str,
    active: str = "0",
    last_seen: datetime | None = None,
) -> DeviceView:
    observed = last_seen or datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    return DeviceView(
        source={"ip": ip, "last_seen": observed},
        ip=ip,
        alias=alias,
        hostname=hostname,
        mm_status="Up",
        active_clients=active,
        standby_clients="0",
        connection_type="COMMANDER",
        status=status,
        status_key=status_key,
        last_seen=observed.strftime("%Y-%m-%d %H:%M:%S"),
        controller_state="up",
        controller_status="Up",
        distribution_state="normal",
        distribution_status="정상",
    )


def _dashboard(devices: list[DeviceView], scope: list[str]) -> DashboardView:
    return DashboardView(
        source={},
        status="주의",
        status_key="attention",
        devices=devices,
        problem_ips=[],
        reasons=[],
        checked_at="2026-08-31 01:00:00",
        checked_at_short="01:00:00",
        monitoring_scope_ips=scope,
    )


def _fixture() -> tuple[DeviceTableModel, DeviceFilterModel, list[DeviceView]]:
    devices = [
        _device(
            "192.0.2.10",
            alias="Edge Alpha",
            hostname="controller-a.example",
            status_key="normal",
            status="정상",
            active="10",
        ),
        _device(
            "192.0.2.20",
            alias="",
            hostname="Core-B.example",
            status_key="critical",
            status="장애",
            active="2",
        ),
        _device(
            "192.0.2.30",
            alias="Gamma",
            hostname="controller-c.example",
            status_key="attention",
            status="주의",
            active="100",
        ),
        _device(
            "192.0.2.40",
            alias="Delta",
            hostname="controller-d.example",
            status_key="unknown",
            status="확인 불가",
            active="20",
        ),
    ]
    source = DeviceTableModel(
        _dashboard(devices, [devices[0].ip, devices[1].ip, devices[3].ip]),
        active_incident_ips=[devices[1].ip, devices[2].ip],
    )
    return source, DeviceFilterModel(source), devices


def _visible_ips(proxy: DeviceFilterModel) -> list[str]:
    return [
        proxy.data(proxy.index(row, 0), IpRole)
        for row in range(proxy.rowCount())
    ]


def test_search_matches_ip_alias_and_hidden_hostname_case_insensitively() -> None:
    _app()
    _source, proxy, devices = _fixture()

    for query, expected in (
        (".10", devices[0].ip),
        ("ALPHA", devices[0].ip),
        ("core-b.EXAMPLE", devices[1].ip),
    ):
        proxy.set_search_text(query)
        assert _visible_ips(proxy) == [expected]

    proxy.set_search_query("")
    assert proxy.rowCount() == len(devices)


def test_status_incident_and_monitoring_filters_are_composable() -> None:
    _app()
    source, proxy, devices = _fixture()

    proxy.set_search_text("core-b")
    proxy.set_status_filter("critical")
    proxy.set_incident_only(True)
    proxy.set_monitoring_only(True)
    assert _visible_ips(proxy) == [devices[1].ip]

    # A non-monitored attention device keeps the legacy effective unknown
    # presentation, so status filtering agrees with the visible table.
    proxy.set_search_text("gamma")
    proxy.set_status_filter("unknown")
    proxy.set_monitoring_only(False)
    assert _visible_ips(proxy) == [devices[2].ip]

    proxy.clear_filters()
    assert proxy.search_text == ""
    assert proxy.status_filter == "all"
    assert proxy.incident_only is False
    assert proxy.monitoring_only is False
    assert proxy.rowCount() == len(devices)

    proxy.set_incident_only(True)
    assert set(_visible_ips(proxy)) == {devices[1].ip, devices[2].ip}
    source.set_active_incident_ips([devices[0].ip])
    assert _visible_ips(proxy) == [devices[0].ip]


def test_proxy_sorts_client_counts_numerically_and_timestamps_chronologically() -> None:
    _app()
    oldest = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    middle = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    newest = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    devices = [
        _device(
            "192.0.2.10",
            alias="SAME",
            hostname="a",
            status_key="normal",
            status="정상",
            active="10",
            last_seen=newest,
        ),
        _device(
            "192.0.2.2",
            alias="SAME",
            hostname="b",
            status_key="normal",
            status="정상",
            active="2",
            last_seen=oldest,
        ),
        _device(
            "192.0.2.30",
            alias="SAME",
            hostname="c",
            status_key="normal",
            status="정상",
            active="100",
            last_seen=middle,
        ),
    ]
    source = DeviceTableModel(devices)
    proxy = DeviceFilterModel(source)

    proxy.sort(DeviceTableColumn.ACTIVE_CLIENTS, Qt.AscendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.2", "192.0.2.10", "192.0.2.30"]
    proxy.sort(DeviceTableColumn.ACTIVE_CLIENTS, Qt.DescendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.30", "192.0.2.10", "192.0.2.2"]

    proxy.sort(DeviceTableColumn.LAST_SEEN, Qt.AscendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.2", "192.0.2.30", "192.0.2.10"]

    proxy.sort(DeviceTableColumn.NAME, Qt.DescendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.10", "192.0.2.2", "192.0.2.30"]
    assert proxy.device_at(0) is devices[0]
    assert proxy.row_for_ip(devices[0].ip) == 0

    proxy.sort(DeviceTableColumn.IP, Qt.AscendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.10", "192.0.2.2", "192.0.2.30"]
    proxy.sort(DeviceTableColumn.IP, Qt.DescendingOrder)
    assert _visible_ips(proxy) == ["192.0.2.30", "192.0.2.2", "192.0.2.10"]


def test_page_proxy_slices_after_global_filter_and_sort() -> None:
    _app()
    active_counts = [50, 1, 40, 10, 30, 20]
    devices = [
        _device(
            f"192.0.2.{index + 1}",
            alias=f"MATCH-{index}",
            hostname=f"controller-{index}",
            status_key="normal" if index < 5 else "failure",
            status="정상" if index < 5 else "장애",
            active=str(active),
        )
        for index, active in enumerate(active_counts)
    ]
    source = DeviceTableModel(devices)
    filtered = DeviceFilterModel(source)
    filtered.set_search_text("match")
    filtered.set_status_filter("normal")
    page = DevicePageModel(filtered, enabled=True, page_size=2)

    assert PageFilterModel is DevicePageModel
    assert page.filtered_row_count() == 5
    assert page.page_count() == 3

    # Sorting the top proxy delegates to the filter proxy, so every page is a
    # slice of one global numeric ordering rather than a locally sorted page.
    page.sort(DeviceTableColumn.ACTIVE_CLIENTS, Qt.AscendingOrder)
    assert _visible_ips(page) == [devices[1].ip, devices[3].ip]
    assert page.page_for_ip(devices[4].ip) == 1
    assert page.page_for_source_row(4) == 2

    page.set_page(1)
    assert page.current_page == 1
    assert _visible_ips(page) == [devices[4].ip, devices[2].ip]
    assert page.row_for_ip(devices[4].ip) == 0
    assert page.row_for_ip(devices[1].ip) == -1
    assert page.page_start() == 2
    assert page.page_end() == 4

    page.next_page()
    assert page.current_page == 2
    assert _visible_ips(page) == [devices[0].ip]
    page.previous_page()
    assert page.current_page == 1


def test_page_proxy_clamps_when_filter_shrinks_and_can_be_disabled() -> None:
    _app()
    source, filtered, devices = _fixture()
    page = DevicePageModel(filtered, enabled=True, page_size=1)
    page.set_page(3)
    assert page.current_page == 3
    assert _visible_ips(page) == [devices[3].ip]

    filtered.set_search_text("alpha")
    assert page.filtered_row_count() == 1
    assert page.page_count() == 1
    assert page.current_page == 0
    assert _visible_ips(page) == [devices[0].ip]

    filtered.clear_filters()
    page.set_paging(False, 2)
    assert page.paging_enabled is False
    assert page.page_count() == 1
    assert page.rowCount() == len(devices)
    assert page.page_for_ip(devices[3].ip) == 0

    with pytest.raises(ValueError, match="page_size"):
        page.set_paging(True, 0)
