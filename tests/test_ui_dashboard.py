from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from aruba_mini_dashboard.config import (
    AppSettings,
    SettingsStore,
    settings_fingerprint,
)
from aruba_mini_dashboard.credentials import (
    CredentialNotFoundError,
    CredentialService,
    DeviceCredential,
    SessionCredentialStore,
)
from aruba_mini_dashboard.models import (
    ClientDistributionRow,
    DeviceHealth,
    HealthSignal,
    IncidentType,
    OverallHealth,
    ParseIssue,
    ParseResult,
    ParseStatus,
    Severity,
)
from aruba_mini_dashboard.main import restore_persisted_preferences
from aruba_mini_dashboard.storage import SQLiteStorage
from aruba_mini_dashboard.ui.settings_dialog import SettingsDialog
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.main_window import HostKeyApprovalDialog, MainWindow
from aruba_mini_dashboard.ui.widgets import NoWheelSlider


class FakeCoordinator(QObject):
    cycle_started = Signal(str, object)
    cycle_finished = Signal(object)
    cycle_failed = Signal(object)
    busy_changed = Signal(bool)
    automatic_changed = Signal(bool)
    next_check_changed = Signal(object)
    scheduled_poll_skipped = Signal(str)
    manual_poll_queued = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.automatic = False
        self.interval = 60
        self.shutdown_requested = False

    @property
    def shutting_down(self) -> bool:
        return self.shutdown_requested

    def check_now(self) -> None:
        pass

    def start_automatic(self) -> None:
        self.automatic = True
        self.automatic_changed.emit(True)

    def pause_automatic(self) -> None:
        self.automatic = False
        self.automatic_changed.emit(False)

    def set_interval(self, seconds: int) -> None:
        self.interval = seconds

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _health() -> OverallHealth:
    now = datetime.now(timezone.utc)
    return OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL,
        problem_ips=["192.0.2.12"],
        primary_problem_ip="192.0.2.12",
        summary="WLC-02에서 복합 이상 신호가 감지되었습니다.",
        devices=[
            DeviceHealth(
                ip="192.0.2.11",
                alias="WLC-01",
                mm_status="Up",
                active_clients=250,
                standby_clients=260,
                connection_type="Type-A",
                last_seen=now,
                severity=Severity.NORMAL,
            ),
            DeviceHealth(
                ip="192.0.2.12",
                alias="WLC-02",
                mm_status="Down",
                active_clients=0,
                standby_clients=4,
                connection_type="Type-B",
                previous_connection_type="Type-A",
                load_anomaly=True,
                load_anomaly_streak=3,
                issue_reasons=["MM Status Down", "Client 분배 이상"],
                last_seen=now,
                severity=Severity.CRITICAL,
            ),
        ],
    )


def test_shutdown_pause_does_not_overwrite_next_launch_automatic_preference() -> None:
    _app()
    settings = AppSettings.default()
    settings.polling.automatic_enabled = True
    coordinator = FakeCoordinator()
    coordinator.automatic = True
    window = MainWindow(coordinator, settings)

    window._quitting = True
    window._automatic_changed(False)

    assert window.settings.polling.automatic_enabled is True
    window.tray_icon.hide()
    window.deleteLater()


def test_external_shutdown_pause_does_not_overwrite_automatic_preference() -> None:
    _app()
    settings = AppSettings.default()
    settings.polling.automatic_enabled = True
    coordinator = FakeCoordinator()
    coordinator.automatic = True
    coordinator.shutdown_requested = True
    window = MainWindow(coordinator, settings)

    window._automatic_changed(False)

    assert window._quitting is False
    assert window.settings.polling.automatic_enabled is True
    window.tray_icon.hide()
    window.deleteLater()


def test_declining_host_key_approval_discards_transient_connection_request(monkeypatch) -> None:
    _app()
    coordinator = FakeCoordinator()
    discarded: list[str] = []
    coordinator.discard_connection_test = discarded.append
    window = MainWindow(coordinator, AppSettings.default())
    monkeypatch.setattr(
        "aruba_mini_dashboard.ui.main_window.HostKeyApprovalDialog.exec",
        lambda _self: QDialog.Rejected,
    )

    window._connection_test_finished(
        "mm",
        {
            "status": "approval_required",
            "host": "192.0.2.1",
            "port": 22,
            "fingerprint": "SHA256:fixture",
            "algorithm": "ssh-ed25519",
        },
    )

    assert discarded == ["mm"]
    window.tray_icon.hide()
    window.deleteLater()


def test_batch_host_key_approval_dialog_shows_every_unknown_identity() -> None:
    _app()
    dialog = HostKeyApprovalDialog(
        {
            "details": (
                {
                    "role": "mm",
                    "host": "192.0.2.1",
                    "port": 22,
                    "status": "approval_required",
                    "algorithm": "ssh-ed25519",
                    "fingerprint": "SHA256:mm",
                },
                {
                    "role": "cluster",
                    "host": "192.0.2.11",
                    "port": 22,
                    "status": "approval_required",
                    "algorithm": "ssh-rsa",
                    "fingerprint": "SHA256:wlc",
                },
                {
                    "role": "cluster",
                    "host": "192.0.2.12",
                    "port": 22,
                    "status": "verified",
                },
            )
        }
    )

    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 0).text() == "MM"
    assert dialog.table.item(1, 0).text() == "Controller"
    assert dialog.table.item(1, 3).text() == "SHA256:wlc"
    dialog.close()


def test_dashboard_renders_summary_and_device_rows() -> None:
    app = _app()
    window = MainWindow(FakeCoordinator(), AppSettings.default())
    window.resize(1000, 500)
    window.show()
    app.processEvents()
    window.update_snapshot(_health())
    assert window.status_label.text() == "장애"
    assert "192.0.2.12" in window.problem_label.text()
    assert window.table.rowCount() == 2
    assert window.table.item(1, 1).text() == "WLC-02"
    assert window.table.item(1, 6).text() == "장애"
    window._quitting = True
    window.close()


def test_explicit_empty_problem_list_does_not_promote_unknown_devices() -> None:
    _app()
    now = datetime.now(timezone.utc)
    health = OverallHealth(
        checked_at=now,
        severity=Severity.UNKNOWN,
        devices=[
            DeviceHealth(
                ip="192.0.2.12",
                alias="WLC-02",
                severity=Severity.UNKNOWN,
            )
        ],
        problem_ips=[],
        summary="최종 판단: 확인 불가 (수집 실패)",
    )
    window = MainWindow(FakeCoordinator(), AppSettings.default())
    window.update_snapshot(health)
    assert window.status_label.text() == "확인 불가"
    assert window.problem_label.text() == "문제 IP: 없음"
    window._quitting = True
    window.close()


def test_opacity_clamps_and_always_on_top_preserves_geometry() -> None:
    _app()
    settings = AppSettings.default()
    window = MainWindow(FakeCoordinator(), settings)
    window.setGeometry(80, 90, 500, 360)
    before = window.geometry()
    window.set_opacity_percent(10)
    assert window.opacity_slider.value() == 40
    assert window.opacity_number.text() == "40%"
    window.set_always_on_top(True)
    assert window.always_on_top_action.isChecked()
    assert window.geometry().size() == before.size()
    window.reset_window_options()
    assert window.opacity_slider.value() == 100
    assert not window.always_on_top_action.isChecked()
    window._quitting = True
    window.close()


def test_quick_window_options_are_debounced_and_force_flushed(tmp_path) -> None:
    _app()
    storage = SQLiteStorage(tmp_path / "app.db")
    window = MainWindow(FakeCoordinator(), AppSettings.default(), storage=storage)
    window.set_opacity_percent(65)
    window.set_always_on_top(True)
    assert storage.get_setting("ui.opacity_percent") is None
    window._flush_preference_mirror()
    assert storage.get_setting("ui.opacity_percent") == 65
    assert storage.get_setting("ui.always_on_top") is True
    window._quitting = True
    window.close()
    storage.close()


def test_sqlite_preferences_override_json_settings(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "app.db")
    settings = AppSettings.default()
    settings.ui.opacity_percent = 90
    storage.set_preferences(
        {
            "ui.opacity_percent": 55,
            "polling.interval_seconds": 30,
            "polling.automatic_enabled": True,
            "_base_config_fingerprint": settings_fingerprint(settings),
        }
    )
    restored = restore_persisted_preferences(settings, storage)
    assert restored.ui.opacity_percent == 55
    assert restored.polling.interval_seconds == 30
    assert restored.polling.automatic_enabled is True
    storage.close()


def test_sqlite_boolean_preferences_never_use_string_truthiness(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "app.db")
    settings = AppSettings.default()
    storage.set_preferences(
        {
            "polling.automatic_enabled": "false",
            "notifications.sound_enabled": "true",
            "ui.always_on_top": 1,
            "ui.opacity_percent": "40",
            "_base_config_fingerprint": settings_fingerprint(settings),
        }
    )

    restored = restore_persisted_preferences(settings, storage)

    assert restored.polling.automatic_enabled is False
    assert restored.notifications.sound_enabled is False
    assert restored.ui.always_on_top is False
    assert restored.ui.opacity_percent == 100
    storage.close()


def test_invalid_sqlite_boolean_mirror_does_not_hide_valid_json_true(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "app.db")
    settings = AppSettings.default()
    settings.notifications.recovery_notifications = True
    storage.set_preferences(
        {
            "notifications.recovery_notifications": "false",
            "_base_config_fingerprint": settings_fingerprint(settings),
        }
    )

    restored = restore_persisted_preferences(settings, storage)

    assert restored.notifications.recovery_notifications is True
    storage.close()


def test_stale_sqlite_mirror_never_overrides_newer_json_settings(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "app.db")
    old = AppSettings.default()
    storage.set_preferences(
        {
            "polling.interval_seconds": 30,
            "_base_config_fingerprint": settings_fingerprint(old),
        }
    )
    edited_json = AppSettings.default()
    edited_json.polling.interval_seconds = 120

    restored = restore_persisted_preferences(edited_json, storage)

    assert restored.polling.interval_seconds == 120
    storage.close()


def test_no_wheel_slider_ignores_wheel_input() -> None:
    _app()
    slider = NoWheelSlider()

    class Event:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

    event = Event()
    slider.wheelEvent(event)
    assert event.ignored


def test_detail_dialog_masks_password_prompt() -> None:
    _app()
    device = {
        "ip": "192.0.2.12",
        "alias": "WLC-02",
        "severity": "critical",
        "raw_output": "show switches\npassword: should-not-appear\nWLC 192.0.2.12 Down",
    }
    dialog = DetailDialog(device)
    assert not hasattr(dialog.tabs.widget(2), "toPlainText")
    dialog.tabs.setCurrentIndex(2)
    raw_editor = dialog.tabs.widget(2)
    assert "should-not-appear" not in raw_editor.toPlainText()
    assert "[REDACTED]" in raw_editor.toPlainText()
    dialog.close()


def test_detail_dialog_shows_ip_filtered_parse_context_and_change_time() -> None:
    _app()
    device = DeviceHealth(
        ip="192.0.2.12",
        alias="WLC-02",
        connection_type="Type-B",
        previous_connection_type="Type-A",
        connection_type_changed=True,
        severity=Severity.WARNING,
        signals=[
            HealthSignal(
                IncidentType.CONNECTION_TYPE_CHANGED,
                Severity.WARNING,
                "Connection-Type 변경",
                ip="192.0.2.12",
                details={"first_detected_at": "2026-08-11T10:31:00+09:00"},
            )
        ],
    )
    parsed = ParseResult(
        ParseStatus.PARTIAL,
        rows=[
            ClientDistributionRow("192.0.2.11", 250, 260),
            ClientDistributionRow("192.0.2.12", 0, 4),
        ],
        issues=[ParseIssue("ROW_SKIPPED", "깨진 행을 건너뜀")],
    )
    dialog = DetailDialog(
        device,
        raw_outputs={"show test": "password: do-not-show\n192.0.2.12 0 4"},
        parsed_results={"show test": parsed},
    )
    summary_text = "\n".join(label.text() for label in dialog.findChildren(type(dialog._selectable(""))))
    expected_change_time = datetime.fromisoformat("2026-08-11T10:31:00+09:00").astimezone()
    assert expected_change_time.strftime("%Y-%m-%d %H:%M:%S") in summary_text
    dialog.tabs.setCurrentIndex(1)
    parsed_text = dialog.tabs.widget(1).toPlainText()
    assert "192.0.2.12" in parsed_text
    assert "192.0.2.11" not in parsed_text
    assert "ROW_SKIPPED" in parsed_text
    dialog.tabs.setCurrentIndex(2)
    raw_text = dialog.tabs.widget(2).toPlainText()
    assert "do-not-show" not in raw_text
    assert "[REDACTED]" in raw_text
    dialog.close()


def test_ssh_debug_setting_round_trips_through_dialog() -> None:
    _app()
    settings = AppSettings.default()
    dialog = SettingsDialog(settings)
    dialog.ssh_debug_logging.setChecked(True)
    collected = dialog._collect_settings(save_credentials=False)
    reloaded = AppSettings.from_dict(collected.to_dict())
    assert reloaded.ssh_debug_logging is True
    dialog.close()


def test_separate_credential_update_preserves_omitted_values_and_other_role() -> None:
    _app()
    service = CredentialService(persistent=SessionCredentialStore())
    mm_id = service.save(
        DeviceCredential("mm-user", "old-mm-password", "old-enable"),
        session_only=False,
    )
    cluster_id = service.save(
        DeviceCredential("cluster-user", "cluster-password"),
        session_only=False,
    )
    settings = AppSettings.default()
    settings.credentials.use_shared_credentials = False
    settings.mobility_master.credential_id = mm_id
    settings.cluster.credential_id = cluster_id

    dialog = SettingsDialog(settings, service)
    dialog.mm_fields.password.setText("new-mm-password")
    collected = dialog._collect_settings(save_credentials=True)

    new_mm_id = collected.mobility_master.credential_id
    assert new_mm_id != mm_id
    assert service.get(new_mm_id) == DeviceCredential("mm-user", "new-mm-password", "old-enable")
    assert service.get(mm_id) == DeviceCredential("mm-user", "old-mm-password", "old-enable")
    assert collected.cluster.credential_id == cluster_id
    assert service.get(cluster_id) == DeviceCredential("cluster-user", "cluster-password")
    dialog.settings = collected
    dialog.commit_staged_credentials()
    with pytest.raises(CredentialNotFoundError):
        service.get(mm_id)
    dialog.close()


def test_invalid_non_secret_form_has_no_credential_side_effect() -> None:
    _app()
    persistent = SessionCredentialStore()
    service = CredentialService(persistent=persistent)
    original_id = service.save(DeviceCredential("operator", "original"), session_only=False)
    settings = AppSettings.default()
    settings.credentials.shared_credential_id = original_id
    dialog = SettingsDialog(settings, service)
    dialog.mm_ip.setText("not-an-ip")
    dialog.shared_fields.username.setText("operator")
    dialog.shared_fields.password.setText("replacement")

    with pytest.raises(Exception, match="MM 관리 IP"):
        dialog._collect_settings(save_credentials=True)

    assert service.get(original_id).password == "original"
    assert list(persistent._credentials) == [original_id]
    dialog.close()


def test_initial_save_requests_integrated_connection_before_storing_credentials() -> None:
    _app()
    persistent = SessionCredentialStore()
    service = CredentialService(persistent=persistent)
    dialog = SettingsDialog(AppSettings.default(), service, initial_setup=True)
    dialog.mm_ip.setText("192.0.2.1")
    for edit, ip in zip(
        dialog.member_ips,
        ("192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14"),
        strict=True,
    ):
        edit.setText(ip)
    dialog.primary_ip.setCurrentIndex(dialog.primary_ip.findData("192.0.2.11"))
    dialog.shared_fields.username.setText("operator")
    dialog.shared_fields.password.setText("temporary-password")
    requested = QSignalSpy(dialog.connection_test_requested)

    dialog._apply()

    assert requested.count() == 1
    assert requested.at(0)[0] == "all"
    request = requested.at(0)[1]
    assert request.purpose == "save"
    assert request.credential is None
    assert request.credential_overrides["mm"].password == "temporary-password"
    assert persistent._credentials == {}
    assert dialog.connection_request_pending is True
    assert dialog.tabs.isEnabled() is False
    dialog.complete_connection_request(False)
    assert dialog.result() == QDialog.Rejected
    assert dialog.tabs.isEnabled() is True
    dialog.close()


def test_successful_integrated_connection_stages_credentials_and_accepts_save() -> None:
    _app()
    persistent = SessionCredentialStore()
    service = CredentialService(persistent=persistent)
    dialog = SettingsDialog(AppSettings.default(), service, initial_setup=True)
    dialog.mm_ip.setText("192.0.2.1")
    for edit, ip in zip(
        dialog.member_ips,
        ("192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14"),
        strict=True,
    ):
        edit.setText(ip)
    dialog.primary_ip.setCurrentIndex(dialog.primary_ip.findData("192.0.2.11"))
    dialog.shared_fields.username.setText("operator")
    dialog.shared_fields.password.setText("verified-password")

    dialog._apply()
    assert dialog.complete_connection_request(True) is True

    assert dialog.result() == QDialog.Accepted
    credential_id = dialog.settings.credentials.shared_credential_id
    assert credential_id
    assert service.get(credential_id).password == "verified-password"
    dialog.rollback_staged_credentials()
    dialog.close()


def test_non_ssh_settings_save_skips_network_validation() -> None:
    _app()
    settings = AppSettings.default()
    dialog = SettingsDialog(settings)
    dialog.poll_interval.setValue(90)
    requested = QSignalSpy(dialog.connection_test_requested)

    dialog._apply()

    assert requested.count() == 0
    assert dialog.result() == QDialog.Accepted
    assert dialog.settings.polling.interval_seconds == 90
    dialog.close()


def test_settings_write_failure_leaves_active_ui_and_coordinator_unchanged(monkeypatch) -> None:
    _app()

    class FailingStore:
        def save(self, _settings) -> None:
            raise OSError("disk full")

    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.warning", lambda *args: None)
    coordinator = FakeCoordinator()
    original = AppSettings.default()
    window = MainWindow(coordinator, original, settings_store=FailingStore())
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30
    candidate.ui.opacity_percent = 55

    assert window.apply_settings(candidate) is False
    assert window.settings.polling.interval_seconds == 60
    assert coordinator.interval == 60
    assert window.opacity_slider.value() == 100
    window._quitting = True
    window.close()


def test_settings_dialog_is_reused_when_poll_starts_before_apply(monkeypatch) -> None:
    _app()

    class FakeSignal:
        def connect(self, _slot) -> None:
            return None

    coordinator = FakeCoordinator()
    coordinator.busy = True
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    class FakeDialog:
        created = 0

        def __init__(self, *_args) -> None:
            type(self).created += 1
            self.settings = candidate
            self.connection_test_requested = FakeSignal()
            self.sound_test_requested = FakeSignal()
            self.notification_test_requested = FakeSignal()
            self.exec_calls = 0
            self.commits = 0
            self.rollbacks = 0

        def exec(self) -> int:
            self.exec_calls += 1
            if self.exec_calls == 2:
                coordinator.busy = False
            return QDialog.Accepted

        def commit_staged_credentials(self) -> None:
            self.commits += 1

        def rollback_staged_credentials(self) -> None:
            self.rollbacks += 1

    created: list[FakeDialog] = []

    def factory(*args):
        dialog = FakeDialog(*args)
        created.append(dialog)
        return dialog

    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.SettingsDialog", factory)
    monkeypatch.setattr(
        "aruba_mini_dashboard.ui.main_window.QMessageBox.information", lambda *args: None
    )
    window = MainWindow(coordinator, AppSettings.default())

    window._busy_changed(True)
    assert window.settings_button.isEnabled() is False
    window._busy_changed(False)
    coordinator.busy = True

    window.open_settings()

    assert FakeDialog.created == 1
    assert created[0].exec_calls == 2
    assert created[0].commits == 1
    assert created[0].rollbacks == 0
    assert window.settings.polling.interval_seconds == 30
    window.close()


def test_settings_are_not_saved_or_applied_during_active_poll(monkeypatch) -> None:
    _app()

    class CaptureStore:
        def __init__(self) -> None:
            self.saved = []

        def save(self, settings) -> None:
            self.saved.append(settings)

    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.information", lambda *args: None)
    coordinator = FakeCoordinator()
    coordinator.busy = True
    store = CaptureStore()
    applied = []
    original = AppSettings.default()
    window = MainWindow(
        coordinator,
        original,
        settings_store=store,
        settings_apply_handler=applied.append,
    )
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    assert window.apply_settings(candidate) is False
    assert store.saved == []
    assert applied == []
    assert window.settings.polling.interval_seconds == 60
    window._quitting = True
    window.close()


def test_runtime_settings_failure_rolls_back_authoritative_json_and_ui(
    monkeypatch,
    tmp_path,
) -> None:
    _app()
    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.warning", lambda *args: None)
    store = SettingsStore(tmp_path / "settings.json")
    original = AppSettings.default()
    store.save(original)

    def fail_apply(_settings) -> None:
        raise RuntimeError("simulated runtime apply failure")

    coordinator = FakeCoordinator()
    window = MainWindow(
        coordinator,
        original,
        settings_store=store,
        settings_apply_handler=fail_apply,
    )
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30
    candidate.ui.opacity_percent = 55

    assert window.apply_settings(candidate) is False
    assert store.load().polling.interval_seconds == 60
    assert window.settings.polling.interval_seconds == 60
    assert coordinator.interval == 60
    assert window.opacity_slider.value() == 100
    window._quitting = True
    window.close()


def test_runtime_and_immediate_file_rollback_failure_recovers_on_restart(
    monkeypatch,
    tmp_path,
) -> None:
    _app()
    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.warning", lambda *args: None)

    class DeferredRollbackStore(SettingsStore):
        def begin_update(self, settings):
            update = super().begin_update(settings)

            class Wrapper:
                def commit(self):
                    return update.commit()

                def rollback(self):
                    raise OSError("simulated rollback write failure")

            return Wrapper()

    path = tmp_path / "settings.json"
    seed = SettingsStore(path)
    original = AppSettings.default()
    original.polling.interval_seconds = 90
    seed.save(original)
    store = DeferredRollbackStore(path)

    def fail_apply(_settings) -> None:
        raise RuntimeError("simulated runtime apply failure")

    window = MainWindow(
        FakeCoordinator(),
        original,
        settings_store=store,
        settings_apply_handler=fail_apply,
    )
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    assert window.apply_settings(candidate) is False
    assert window.settings.polling.interval_seconds == 90
    # The candidate may still be the visible file, but the durable marker makes
    # the next startup restore the exact authoritative original first.
    assert SettingsStore(path).load().polling.interval_seconds == 90
    window._quitting = True
    window.close()


def test_transactional_runtime_cleanup_commits_only_after_json_commit(
    monkeypatch,
    tmp_path,
) -> None:
    _app()
    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.warning", lambda *args: None)
    events: list[str] = []

    class OrderedStore(SettingsStore):
        def begin_update(self, settings):
            events.append("json-stage")
            update = super().begin_update(settings)

            class OrderedUpdate:
                def commit(self):
                    events.append("json-commit")
                    update.commit()

                def rollback(self):
                    events.append("json-rollback")
                    update.rollback()

            return OrderedUpdate()

    class RuntimeUpdate:
        def commit(self):
            events.append("runtime-commit")

        def rollback(self):
            events.append("runtime-rollback")

    def stage_runtime(_settings):
        events.append("runtime-stage")
        return RuntimeUpdate()

    store = OrderedStore(tmp_path / "settings.json")
    original = AppSettings.default()
    store.save(original)
    window = MainWindow(
        FakeCoordinator(),
        original,
        settings_store=store,
        settings_apply_handler=stage_runtime,
    )
    candidate = AppSettings.default()
    candidate.polling.interval_seconds = 30

    assert window.apply_settings(candidate) is True
    assert events[:4] == ["json-stage", "runtime-stage", "json-commit", "runtime-commit"]
    assert store.load() == candidate
    window._quitting = True
    window.close()


def test_runtime_cleanup_commit_failure_keeps_automatic_polling_paused(
    monkeypatch,
    tmp_path,
) -> None:
    _app()
    monkeypatch.setattr("aruba_mini_dashboard.ui.main_window.QMessageBox.warning", lambda *args: None)

    class FailingRuntimeUpdate:
        def commit(self):
            raise RuntimeError("injected cleanup failure")

        def rollback(self):
            raise AssertionError("JSON already committed; runtime must not roll back")

    store = SettingsStore(tmp_path / "settings.json")
    original = AppSettings.default()
    store.save(original)
    coordinator = FakeCoordinator()
    window = MainWindow(
        coordinator,
        original,
        settings_store=store,
        settings_apply_handler=lambda _settings: FailingRuntimeUpdate(),
    )
    candidate = AppSettings.default()
    candidate.polling.automatic_enabled = True

    assert window.apply_settings(candidate) is True
    assert coordinator.automatic is False
    assert store.load().polling.automatic_enabled is True
    window._quitting = True
    window.close()


def test_session_credential_mode_is_restored_and_requires_reentry_to_migrate() -> None:
    _app()
    service = CredentialService(persistent=SessionCredentialStore())
    session_id = service.save(DeviceCredential("operator", "temporary"), session_only=True)
    settings = AppSettings.default()
    settings.credentials.shared_credential_id = session_id
    settings.credentials.session_only = True
    dialog = SettingsDialog(settings, service)
    assert dialog.session_only.isChecked()

    dialog.session_only.setChecked(False)
    with pytest.raises(RuntimeError, match="다시 입력"):
        dialog._collect_settings(save_credentials=True)

    dialog.shared_fields.username.setText("operator")
    dialog.shared_fields.password.setText("persistent")
    collected = dialog._collect_settings(save_credentials=True)
    assert collected.credentials.session_only is False
    assert service.is_session(collected.credentials.shared_credential_id) is False
    dialog.rollback_staged_credentials()
    assert service.get(session_id).password == "temporary"
    dialog.close()


def test_expired_session_credential_id_can_be_replaced_after_restart() -> None:
    _app()
    old_service = CredentialService(persistent=SessionCredentialStore())
    stale_id = old_service.save(
        DeviceCredential("operator", "old-session-secret"),
        session_only=True,
    )
    settings = AppSettings.default()
    settings.credentials.shared_credential_id = stale_id
    settings.credentials.session_only = True

    # A new process has no knowledge or value for the old session-only ID.
    restarted_service = CredentialService(persistent=SessionCredentialStore())
    dialog = SettingsDialog(settings, restarted_service)
    dialog.mm_ip.setText("192.0.2.1")
    for edit, ip in zip(
        dialog.member_ips,
        ("192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14"),
        strict=True,
    ):
        edit.setText(ip)
    dialog.primary_ip.setCurrentIndex(dialog.primary_ip.findData("192.0.2.11"))
    dialog.shared_fields.username.setText("operator")
    dialog.shared_fields.password.setText("fresh-session-secret")
    test_requests = QSignalSpy(dialog.connection_test_requested)
    dialog._emit_connection_test("all")
    assert test_requests.count() == 1
    assert test_requests.at(0)[1].credential is None
    assert test_requests.at(0)[1].credential_overrides["mm"].password == "fresh-session-secret"
    assert test_requests.at(0)[1].credential_overrides["cluster"].password == "fresh-session-secret"
    dialog.complete_connection_request(False)
    collected = dialog._collect_settings(save_credentials=True)

    replacement_id = collected.credentials.shared_credential_id
    assert replacement_id != stale_id
    assert restarted_service.is_session(replacement_id)
    assert restarted_service.get(replacement_id).password == "fresh-session-secret"
    dialog.rollback_staged_credentials()
    dialog.close()


def test_dashboard_surfaces_low_usage_and_maps_multiple_ip_reasons() -> None:
    _app()
    now = datetime.now(timezone.utc)
    health = OverallHealth(
        checked_at=now,
        severity=Severity.CRITICAL,
        devices=[
            DeviceHealth(
                ip="192.0.2.12",
                alias="WLC-02",
                severity=Severity.CRITICAL,
                issue_reasons=["MM Status Down", "Client 분배 이상"],
            ),
            DeviceHealth(
                ip="192.0.2.13",
                alias="WLC-03",
                severity=Severity.WARNING,
                issue_reasons=["Connection-Type 변경"],
            ),
        ],
        problem_ips=["192.0.2.12", "192.0.2.13"],
        summary="복수 문제 IP 감지",
        notes=["낮은 전체 사용량: Client 분배 장애 판단을 보류했습니다."],
    )
    window = MainWindow(FakeCoordinator(), AppSettings.default())
    window.update_snapshot(health)
    assert "192.0.2.12: MM Status Down, Client 분배 이상" in window.problem_label.text()
    assert "192.0.2.13: Connection-Type 변경" in window.problem_label.text()
    assert "낮은 전체 사용량" in window.reason_label.text()
    assert window.ack_button.isEnabled() is False
    window._quitting = True
    window.close()


def test_ack_button_never_acknowledges_selected_normal_row() -> None:
    app = _app()
    window = MainWindow(FakeCoordinator(), AppSettings.default())
    window.resize(1000, 500)
    window.show()
    app.processEvents()
    window.update_snapshot(_health())
    spy = QSignalSpy(window.acknowledge_requested)
    window.table.selectRow(0)  # 192.0.2.11 is normal
    window._selection_changed()
    assert window.ack_button.isEnabled() is False
    window._acknowledge_selected()
    assert spy.count() == 0
    window.table.selectRow(1)
    window._selection_changed()
    assert window.ack_button.isEnabled() is True
    window._acknowledge_selected()
    assert spy.count() == 1
    assert spy.at(0)[0] == "192.0.2.12"
    window._quitting = True
    window.close()


def test_previous_collection_values_are_visible_in_detail() -> None:
    _app()
    current = DeviceHealth(
        ip="192.0.2.12",
        mm_status="Down",
        active_clients=0,
        standby_clients=4,
        connection_type="Type-B",
    )
    previous = DeviceHealth(
        ip="192.0.2.12",
        mm_status="Up",
        active_clients=250,
        standby_clients=260,
        connection_type="Type-A",
        last_seen=datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc),
    )
    dialog = DetailDialog(current, previous_device=previous)
    text = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "이전 수집값" in text
    assert "MM Up / Active 250 / Standby 260 / Connection-Type Type-A" in text
    dialog.close()


def test_tray_unavailable_close_requests_safe_application_shutdown(monkeypatch) -> None:
    _app()
    coordinator = FakeCoordinator()
    window = MainWindow(coordinator, AppSettings.default())
    window.tray_icon.hide()
    completed: list[bool] = []
    monkeypatch.setattr(window, "_complete_quit", lambda: completed.append(True))
    window.close()
    assert coordinator.shutdown_requested is True
    assert completed == [True]
    window._quitting = True
    window.close()


def test_ip_scoped_collection_failure_can_be_acknowledged_without_problem_ip() -> None:
    _app()
    now = datetime.now(timezone.utc)
    health = OverallHealth(
        checked_at=now,
        severity=Severity.UNKNOWN,
        devices=[],
        problem_ips=[],
    )
    incident = {
        "incident_id": "collection-mm",
        "incident_type": IncidentType.COLLECTION_FAILURE,
        "severity": Severity.UNKNOWN,
        "ip": "192.0.2.1",
        "active": True,
        "acknowledged": False,
    }
    window = MainWindow(FakeCoordinator(), AppSettings.default())
    global_spy = QSignalSpy(window.acknowledge_global_requested)
    window.update_snapshot(
        {
            "health": health,
            "checked_at": now,
            "severity": Severity.UNKNOWN,
            "devices": [],
            "problem_ips": [],
            "active_incidents": [incident],
        }
    )
    assert window.ack_button.isEnabled()
    window._acknowledge_selected()
    assert global_spy.count() == 1
    window._quitting = True
    window.close()
