from __future__ import annotations

import os
import gc
import weakref
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget

from aruba_mini_dashboard.config import AppSettings, ClusterMemberSettings
from aruba_mini_dashboard.main import RuntimeSnapshot
from aruba_mini_dashboard.models import DeviceHealth, OverallHealth, Severity
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.main_window import MainWindow
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog


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
        self.check_count = 0
        self.interval = 60

    def check_now(self) -> None:
        self.check_count += 1

    def start_automatic(self) -> None:
        self.automatic = True

    def pause_automatic(self) -> None:
        self.automatic = False

    def set_interval(self, seconds: int) -> None:
        self.interval = seconds


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(active: int = 10) -> OverallHealth:
    now = datetime.now(timezone.utc)
    return OverallHealth(
        checked_at=now,
        severity=Severity.NORMAL,
        devices=[
            DeviceHealth(
                ip="192.0.2.11",
                alias="WLC-01",
                active_clients=active,
                standby_clients=20,
                mm_status="Up",
                severity=Severity.NORMAL,
                last_seen=now,
            )
        ],
    )


def test_only_visible_table_is_rendered_and_hidden_page_gets_latest_snapshot() -> None:
    app = _app()
    window = MainWindow(Coordinator(), AppSettings.default())
    window.show()
    app.processEvents()
    assert window._dashboard_mode == window.COMPACT_MODE

    window.update_snapshot(_snapshot(10))
    assert window.compact_table.rowCount() == 1
    assert window.table.rowCount() == 0

    compact_item = window.compact_table.item(0, 0)
    window.update_snapshot(_snapshot(99))
    assert window.compact_table.item(0, 0) is compact_item
    assert window.table.rowCount() == 0

    window.resize(1000, 500)
    app.processEvents()
    assert window.table.rowCount() == 1
    assert window.table.item(0, 3).text() == "99"
    window._quitting = True
    window.close()


def test_snapshot_does_not_repaint_tables_while_hidden_to_tray() -> None:
    app = _app()
    window = MainWindow(Coordinator(), AppSettings.default())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot(10))
    previous = window.compact_table.item(0, 0)

    window._hidden_to_tray = True
    window.hide()
    window.update_snapshot(_snapshot(88))
    assert window.compact_table.item(0, 0) is previous

    window.show_dashboard()
    app.processEvents()
    assert window.compact_table.rowCount() == 1
    window._quitting = True
    window.close()


def test_unchanged_large_full_table_skips_per_cell_icon_and_style_work(monkeypatch) -> None:
    app = _app()
    now = datetime.now(timezone.utc)
    health = OverallHealth(
        checked_at=now,
        severity=Severity.NORMAL,
        devices=[
            DeviceHealth(
                ip=f"198.51.{index // 250}.{index % 250 + 1}",
                alias=f"WLC-{index:03d}",
                mm_status="Up",
                active_clients=100,
                standby_clients=100,
                severity=Severity.NORMAL,
                last_seen=now,
            )
            for index in range(500)
        ],
    )
    calls = 0
    from aruba_mini_dashboard.ui import main_window as main_window_module

    real_status_icon = main_window_module.status_icon

    def counted_status_icon(key: str):
        nonlocal calls
        calls += 1
        return real_status_icon(key)

    monkeypatch.setattr(main_window_module, "status_icon", counted_status_icon)
    window = MainWindow(Coordinator(), AppSettings.default())
    window.resize(1100, 600)
    window.show()
    app.processEvents()

    window.update_snapshot(health)
    first_calls = calls
    window.update_snapshot(health)

    assert window.table.rowCount() == 500
    assert first_calls >= 501  # one status cell per row plus window/tray icon
    assert calls - first_calls == 1  # only the overall window/tray icon remains
    window._quitting = True
    window.close()


def test_first_visible_show_offers_existing_settings_without_starting_poll(monkeypatch) -> None:
    app = _app()
    coordinator = Coordinator()
    window = MainWindow(
        coordinator,
        AppSettings.default(),
        setup_readiness_check=lambda _settings: (False, "MM 관리 IP가 필요합니다."),
    )
    calls: list[bool] = []
    monkeypatch.setattr(window, "open_settings", lambda *, initial_setup=False: calls.append(initial_setup))

    window.show()
    app.processEvents()
    assert calls == [True]
    assert window.settings_button.text() == "설정 시작"
    assert window.compact_more_button.text() == "설정 시작"
    assert coordinator.check_count == 0

    window.hide()
    window.show()
    app.processEvents()
    assert calls == [True]
    window._quitting = True
    window.close()


def test_setup_required_disables_monitoring_actions_and_marks_tray() -> None:
    _app()
    window = MainWindow(
        Coordinator(),
        AppSettings.default(),
        setup_readiness_check=lambda _settings: (False, "MM 관리 IP가 필요합니다."),
    )
    window.tray_icon.show()
    window._refresh_setup_state()

    assert not window.check_now_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert not window.compact_check_now_button.isEnabled()
    assert not window.tray_check_now_action.isEnabled()
    assert not window.tray_start_action.isEnabled()
    assert window.tray_settings_action.isEnabled()
    assert "설정 필요" in window.tray_icon.toolTip()
    window._quitting = True
    window.close()


def test_unchanged_window_state_does_not_rewrite_settings() -> None:
    _app()

    class Store:
        def __init__(self) -> None:
            self.saved = 0

        def save(self, _settings: AppSettings) -> None:
            self.saved += 1

    store = Store()
    window = MainWindow(Coordinator(), AppSettings.default(), settings_store=store)
    window._save_window_state()
    first_count = store.saved
    window._save_window_state()

    assert first_count >= 1
    assert store.saved == first_count
    window._quitting = True
    window.close()


def test_demo_and_startup_issue_suppress_first_run_modal(monkeypatch) -> None:
    app = _app()
    for kwargs in ({"demo_mode": True}, {"startup_issue": True}):
        window = MainWindow(
            Coordinator(),
            AppSettings.default(),
            setup_readiness_check=lambda _settings: False,
            **kwargs,
        )
        calls: list[bool] = []
        monkeypatch.setattr(
            window,
            "open_settings",
            lambda *, initial_setup=False, target=calls: target.append(initial_setup),
        )
        window.show()
        app.processEvents()
        assert calls == []
        window._quitting = True
        window.close()


def test_settings_primary_and_fallback_are_derived_from_registered_order() -> None:
    _app()
    settings = AppSettings.default()
    settings.cluster.members = [
        ClusterMemberSettings(f"192.0.2.{index}", f"WLC-{index:02d}")
        for index in range(11, 15)
    ]
    settings.cluster.primary_controller_ip = "192.0.2.12"
    settings.cluster.fallback_controller_ips = ["192.0.2.14"]
    dialog = SettingsDialog(settings)

    assert not hasattr(dialog, "mm_name")
    assert not hasattr(dialog, "cluster_name")
    assert not hasattr(dialog, "fallback_ips")
    assert not hasattr(dialog, "auto_start")
    assert not hasattr(dialog, "opacity")
    assert dialog.tabs.count() == 3
    collected = dialog._collect_settings(save_credentials=False)
    assert collected.cluster.primary_controller_ip == "192.0.2.12"
    assert collected.cluster.fallback_controller_ips == [
        "192.0.2.11",
        "192.0.2.13",
        "192.0.2.14",
    ]
    assert collected.mobility_master.display_name == settings.mobility_master.display_name
    assert collected.cluster.name == settings.cluster.name
    dialog.close()


def test_low_spec_and_performance_logging_round_trip_and_tooltips() -> None:
    _app()
    settings = AppSettings.default()
    dialog = SettingsDialog(settings)
    dialog.low_spec_mode.setChecked(True)
    dialog.performance_logging.setChecked(True)
    collected = dialog._collect_settings(save_credentials=False)

    assert collected.performance.low_spec_mode is True
    assert collected.performance.performance_logging is True
    assert "최소 120초" in dialog.low_spec_mode.toolTip()
    assert "‘지금 점검’은 즉시" in dialog.low_spec_mode.toolTip()
    assert "IP" in dialog.performance_logging.toolTip()
    assert "원본 명령 출력" in dialog.performance_logging.toolTip()
    dialog.close()


def test_every_retained_setting_exposes_hover_and_accessibility_help() -> None:
    _app()
    dialog = SettingsDialog(AppSettings.default())
    controls = [
        dialog.mm_ip,
        *dialog.member_ips,
        *dialog.member_aliases,
        dialog.primary_ip,
        dialog.mm_port,
        dialog.mm_connect_timeout,
        dialog.mm_command_timeout,
        dialog.mm_retries,
        dialog.mm_enable,
        dialog.cluster_port,
        dialog.cluster_connect_timeout,
        dialog.cluster_command_timeout,
        dialog.cluster_retries,
        dialog.cluster_enable,
        dialog.shared_credentials,
        dialog.session_only,
        dialog.mm_test_button,
        dialog.cluster_test_button,
        dialog.poll_interval,
        dialog.low_spec_mode,
        dialog.performance_logging,
        dialog.low_threshold,
        dialog.anomaly_cycles,
        dialog.recovery_cycles,
        dialog.comparison_mode,
        dialog.relative_ratio,
        dialog.minimum_total,
        dialog.minimum_peer,
        dialog.missing_cycles,
        dialog.notify_new,
        dialog.repeat_unack,
        dialog.repeat_minutes,
        dialog.sound_enabled,
        dialog.recovery_notifications,
        dialog.ssh_debug_logging,
    ]
    for fields in (dialog.shared_fields, dialog.mm_fields, dialog.cluster_fields):
        controls.extend((fields.username, fields.password, fields.enable_secret))

    for control in controls:
        assert control.toolTip().strip(), control.accessibleName() or type(control).__name__
        assert control.accessibleDescription().strip(), control.accessibleName() or type(control).__name__
    dialog.close()


def test_initial_setup_save_never_preserves_automatic_poll_intent() -> None:
    _app()
    settings = AppSettings.default()
    settings.polling.automatic_enabled = True
    dialog = SettingsDialog(settings, initial_setup=True)
    collected = dialog._collect_settings(save_credentials=False)
    assert collected.polling.automatic_enabled is False
    dialog.close()


def test_detail_tabs_are_lazy_singletons_and_release_large_references() -> None:
    _app()
    raw = {"show switches": "x" * 100_000}
    parsed = {"show switches": {"rows": []}}
    dialog = DetailDialog({"ip": "192.0.2.11"}, raw_outputs=raw, parsed_results=parsed)
    assert isinstance(dialog.tabs.widget(1), QWidget)
    assert not hasattr(dialog.tabs.widget(1), "toPlainText")
    assert dialog._parsed_editor is None
    assert dialog._raw_editor is None

    dialog.tabs.setCurrentIndex(2)
    editor = dialog.tabs.widget(2)
    dialog.tabs.setCurrentIndex(0)
    dialog.tabs.setCurrentIndex(2)
    assert dialog.tabs.widget(2) is editor
    dialog.close()
    assert dialog._device is None
    assert dialog._raw_outputs is None
    assert dialog._parsed_results is None


def test_main_window_reuses_one_detail_window_per_ip() -> None:
    app = _app()
    window = MainWindow(Coordinator(), AppSettings.default())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())
    item = window.compact_table.item(0, 0)
    window._open_detail_for_item(item)
    first = window._detail_windows["192.0.2.11"]
    window._open_detail_for_item(item)
    assert window._detail_windows["192.0.2.11"] is first
    assert len(window._detail_windows) == 1
    first.close()
    app.processEvents()
    window._quitting = True
    window.close()


def test_open_detail_does_not_pin_an_old_raw_output_snapshot() -> None:
    app = _app()

    class WeakMapping(dict):
        pass

    window = MainWindow(Coordinator(), AppSettings.default())
    window.show()
    app.processEvents()
    window.update_snapshot(_snapshot())
    old_raw = WeakMapping({"show switches": "x" * 300_000})
    old_ref = weakref.ref(old_raw)
    window._raw_outputs = old_raw
    window._open_detail_for_item(window.compact_table.item(0, 0))

    new_raw = WeakMapping({"show switches": "new-cycle"})
    window.update_snapshot(RuntimeSnapshot(_snapshot(77), [], new_raw, {}))
    del old_raw
    gc.collect()

    assert old_ref() is None
    dialog = window._detail_windows["192.0.2.11"]
    summary = "\n".join(label.text() for label in dialog.tabs.widget(0).findChildren(QLabel))
    assert "77" in summary
    dialog.tabs.setCurrentIndex(2)
    assert "new-cycle" in dialog._raw_editor.toPlainText()
    dialog.close()
    app.processEvents()
    window._quitting = True
    window.close()
