from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.main import RuntimeSnapshot
from aruba_mini_dashboard.models import (
    CollectionError,
    ControllerState,
    DeviceHealth,
    DistributionState,
    Incident,
    IncidentType,
    OverallHealth,
    Severity,
)
from aruba_mini_dashboard.ui.main_window import MainWindow


NOW = datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
ALPHA_IP = "192.0.2.10"
BETA_IP = "192.0.2.20"
GAMMA_IP = "192.0.2.30"
PARTIAL_IP = "192.0.2.40"
IPV6_IP = "2001:db8::50"
LONG_HOSTNAME = "edge-" + "very-long-segment-" * 16 + "example.test"
MONITORED_IPS = (ALPHA_IP, BETA_IP, GAMMA_IP, PARTIAL_IP)


class Coordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    busy = False
    automatic = False

    def __init__(self) -> None:
        super().__init__()
        self.interval = 60

    def check_now(self) -> None:
        return None

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, seconds: int) -> None:
        self.interval = seconds


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _settings() -> AppSettings:
    settings = AppSettings.default()
    settings.cluster.members = [
        ClusterMemberSettings(ALPHA_IP, "CORE-ALPHA"),
        ClusterMemberSettings(BETA_IP, "BRANCH-BETA"),
        ClusterMemberSettings(GAMMA_IP, "FAILURE-GAMMA"),
        ClusterMemberSettings(PARTIAL_IP, "PARTIAL-UNKNOWN"),
    ]
    return settings


def _devices(
    *,
    alpha_hostname: str = "alpha-core.example.test",
    beta_hostname: str = "searchable-beta.example.test",
) -> list[DeviceHealth]:
    partial_error = CollectionError(
        source="mm",
        code="SSH_TIMEOUT",
        user_message="문서용 수집 시간 초과",
        target_ip=PARTIAL_IP,
        occurred_at=NOW,
    )
    return [
        DeviceHealth(
            ip=ALPHA_IP,
            alias="CORE-ALPHA",
            hostname=alpha_hostname,
            controller_state=ControllerState.UP,
            distribution_state=DistributionState.NORMAL,
            mm_status="Up",
            active_clients=120,
            standby_clients=80,
            connection_type="L2-Connected",
            last_seen=NOW,
            severity=Severity.NORMAL,
        ),
        DeviceHealth(
            ip=BETA_IP,
            alias="BRANCH-BETA",
            hostname=beta_hostname,
            controller_state=ControllerState.UP,
            distribution_state=DistributionState.OBSERVING,
            mm_status="Up",
            active_clients=8,
            standby_clients=72,
            connection_type="L2-Connected",
            load_anomaly_streak=2,
            last_seen=NOW,
            severity=Severity.WARNING,
        ),
        DeviceHealth(
            ip=GAMMA_IP,
            alias="FAILURE-GAMMA",
            hostname="gamma-outage.example.test",
            controller_state=ControllerState.DOWN,
            distribution_state=DistributionState.ANOMALOUS,
            mm_status="Down",
            active_clients=0,
            standby_clients=2,
            connection_type="L3-Connected",
            load_anomaly=True,
            load_anomaly_streak=3,
            issue_reasons=["MM Status Down"],
            last_seen=NOW,
            severity=Severity.CRITICAL,
        ),
        DeviceHealth(
            ip=PARTIAL_IP,
            alias="PARTIAL-UNKNOWN",
            hostname="partial-collector.example.test",
            controller_state=ControllerState.UNKNOWN,
            distribution_state=DistributionState.UNKNOWN,
            mm_present=False,
            load_present=False,
            collection_errors=[partial_error],
            issue_reasons=["일부 명령 수집 확인 불가"],
            severity=Severity.UNKNOWN,
        ),
        DeviceHealth(
            ip=IPV6_IP,
            hostname=LONG_HOSTNAME,
            is_registered=False,
            controller_state=ControllerState.UP,
            distribution_state=DistributionState.NORMAL,
            mm_status="Up",
            active_clients=3,
            standby_clients=4,
            connection_type="L2-Connected",
            last_seen=NOW,
            severity=Severity.NORMAL,
        ),
    ]


def _snapshot(
    *,
    alpha_hostname: str = "alpha-core.example.test",
    beta_hostname: str = "searchable-beta.example.test",
) -> RuntimeSnapshot:
    devices = _devices(
        alpha_hostname=alpha_hostname,
        beta_hostname=beta_hostname,
    )
    partial_error = devices[3].collection_errors[0]
    health = OverallHealth(
        checked_at=NOW,
        severity=Severity.CRITICAL,
        devices=devices,
        monitoring_scope_ips=MONITORED_IPS,
        problem_ips=[GAMMA_IP],
        primary_problem_ip=GAMMA_IP,
        summary="문서용 복합 상태 snapshot",
        collection_errors=[partial_error],
        partial=True,
    )
    incident = Incident(
        incident_id="fixture-mm-down",
        incident_type=IncidentType.MM_DOWN,
        severity=Severity.CRITICAL,
        reason="MM Status Down",
        first_detected_at=NOW,
        last_seen_at=NOW,
        ip=GAMMA_IP,
    )
    return RuntimeSnapshot(health, [], active_incidents=[incident])


@pytest.fixture
def window() -> Iterator[MainWindow]:
    app = _app()
    dashboard = MainWindow(Coordinator(), _settings())
    dashboard.resize(1180, 720)
    dashboard.show()
    app.processEvents()
    assert dashboard._dashboard_mode == dashboard.FULL_MODE
    dashboard.update_snapshot(_snapshot())
    app.processEvents()
    try:
        yield dashboard
    finally:
        dashboard.tray_icon.hide()
        dashboard._quitting = True
        dashboard.close()
        dashboard.deleteLater()
        app.processEvents()


def _visible_ips(window: MainWindow) -> list[str]:
    return [
        str(window.table.item(row, 0).data(Qt.UserRole))
        for row in range(window.table.rowCount())
    ]


def _row_for_ip(window: MainWindow, ip: str) -> int:
    try:
        return _visible_ips(window).index(ip)
    except ValueError as exc:
        raise AssertionError(f"row not found: {ip}") from exc


def _process_events() -> None:
    _app().processEvents()


def _set_status_filter(window: MainWindow, key: str) -> None:
    index = window.status_filter_combo.findData(key)
    assert index >= 0
    window.status_filter_combo.setCurrentIndex(index)
    _process_events()


def test_search_matches_ip_alias_hostname_and_ipv6(window: MainWindow) -> None:
    cases = (
        ("192.0.2.20", [BETA_IP]),
        ("core-alpha", [ALPHA_IP]),
        ("SEARCHABLE-BETA", [BETA_IP]),
        ("2001:DB8::50", [IPV6_IP]),
    )

    for query, expected in cases:
        window.search_input.setText(query)
        _process_events()
        assert _visible_ips(window) == expected

    window.search_input.setText("no-such-fixture-device")
    _process_events()
    assert window.table.rowCount() == 0
    assert window.device_filter_model.search_text == "no-such-fixture-device"


def test_status_incident_and_monitoring_filters_compose(window: MainWindow) -> None:
    window.monitoring_only_toggle.setChecked(True)
    window.problem_only_toggle.setChecked(True)
    _set_status_filter(window, "failure")
    assert _visible_ips(window) == [GAMMA_IP]

    _set_status_filter(window, "normal")
    assert window.table.rowCount() == 0

    window.problem_only_toggle.setChecked(False)
    _process_events()
    assert _visible_ips(window) == [ALPHA_IP]

    _set_status_filter(window, "unknown")
    assert _visible_ips(window) == [PARTIAL_IP]

    window.monitoring_only_toggle.setChecked(False)
    _process_events()
    assert set(_visible_ips(window)) == {PARTIAL_IP, IPV6_IP}


def test_filter_control_clears_selection_when_row_is_excluded(window: MainWindow) -> None:
    window.table.selectRow(_row_for_ip(window, GAMMA_IP))
    _process_events()
    assert window._selected_ip() == GAMMA_IP

    _set_status_filter(window, "normal")

    assert _visible_ips(window) == [ALPHA_IP]
    assert not window.table.selectedItems()
    assert not window.compact_table.selectedItems()
    assert window._selected_ip() == ""


def test_manual_deselection_does_not_leave_a_hidden_action_target(window: MainWindow) -> None:
    window.table.selectRow(_row_for_ip(window, GAMMA_IP))
    _process_events()
    assert window._selected_ip() == GAMMA_IP

    window.table.clearSelection()
    _process_events()

    assert not window.table.selectedItems()
    assert window._selected_ip() == ""


def test_snapshot_refresh_keeps_active_filter_and_selected_ip(window: MainWindow) -> None:
    window.search_input.setText("alpha-core")
    _process_events()
    window.table.selectRow(_row_for_ip(window, ALPHA_IP))
    _process_events()

    window.update_snapshot(
        _snapshot(beta_hostname="alpha-core-shadow.example.test")
    )
    _process_events()

    assert window.search_input.text() == "alpha-core"
    assert window.device_filter_model.search_text == "alpha-core"
    assert set(_visible_ips(window)) == {ALPHA_IP, BETA_IP}
    assert window._selected_ip() == ALPHA_IP
    assert window._actual_selected_ip() == ALPHA_IP


def test_snapshot_refresh_clears_selection_when_selected_ip_leaves_filter(
    window: MainWindow,
) -> None:
    window.search_input.setText("alpha-core")
    _process_events()
    window.table.selectRow(_row_for_ip(window, ALPHA_IP))
    _process_events()
    assert window._selected_ip() == ALPHA_IP

    window.update_snapshot(
        _snapshot(
            alpha_hostname="renamed-controller.example.test",
            beta_hostname="alpha-core-shadow.example.test",
        )
    )
    _process_events()

    assert _visible_ips(window) == [BETA_IP]
    assert not window.table.selectedItems()
    assert not window.compact_table.selectedItems()
    assert window._selected_ip() == ""


def test_unknown_partial_long_hostname_ipv6_and_filter_accessibility(
    window: MainWindow,
) -> None:
    assert window.device_filter_bar.accessibleName() == "장비 검색 및 필터"
    assert window.search_input.accessibleName() == "장비 검색"
    assert window.status_filter_combo.accessibleName() == "상태 필터"

    columns = {name: index for index, name in enumerate(window.COLUMNS)}
    partial_row = _row_for_ip(window, PARTIAL_IP)
    assert window.table.item(partial_row, columns["MM 보고 상태"]).text() == "누락"
    assert window.table.item(partial_row, columns["Active"]).text() == "-"
    assert window.table.item(partial_row, columns["Standby"]).text() == "-"
    assert window.table.item(partial_row, columns["종합 상태"]).text() == "확인 불가"
    assert window.table.item(partial_row, columns["분배 상태"]).text() == "행 누락"

    ipv6_row = _row_for_ip(window, IPV6_IP)
    assert window.table.item(ipv6_row, columns["IP"]).text() == IPV6_IP
    assert window.table.item(ipv6_row, columns["장비명"]).text() == LONG_HOSTNAME
    assert window.table.item(ipv6_row, columns["장비명"]).toolTip() == LONG_HOSTNAME
    assert window.table.item(ipv6_row, columns["감시 범위"]).text() == "미등록 · 감시 제외"

    _process_events()
    assert not window.table.viewport().grab().isNull()
