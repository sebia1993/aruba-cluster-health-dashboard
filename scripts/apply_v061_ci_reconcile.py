from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, before: str, after: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(before)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    file.write_text(text.replace(before, after, 1), encoding="utf-8")


# The demo deliberately accepts the changed Connection-Type before it begins
# the MM-down/recovery stages. Use the explicit production acceptance API; a
# raw CorrelationEngine demo falls back to the engine-level equivalent.
replace_once(
    "src/aruba_mini_dashboard/demo.py",
    '''        # The demo represents the operator acknowledging the one-time
        # Connection-Type event before the following MM-down stage. This keeps
        # the final recovery stage visibly normal without changing production
        # acknowledgement semantics.
        if stage_position == 5 and hasattr(self.engine, "acknowledge_ip"):
            pending_engine = getattr(self.engine, "engine", None)
            pending = pending_engine.pending_connection_changes() if pending_engine is not None else ()
            for change in pending:
                self.engine.acknowledge_ip(change.member_ip)
''',
    '''        # The demo represents the operator explicitly accepting the
        # one-time Connection-Type event as the new normal before the following
        # MM-down stage. Generic incident acknowledgement must not change a
        # Connection-Type baseline.
        if stage_position == 5:
            pending_engine = getattr(self.engine, "engine", self.engine)
            pending_reader = getattr(pending_engine, "pending_connection_changes", None)
            pending = pending_reader() if callable(pending_reader) else ()
            acceptor = getattr(self.engine, "accept_connection_type_baseline", None)
            if not callable(acceptor):
                acceptor = getattr(pending_engine, "acknowledge_connection_change", None)
            if callable(acceptor):
                for change in pending:
                    acceptor(change.member_ip)
''',
)

# Suppress repeats only for the accepted Connection-Type lifecycle, not every
# other active reason on the same controller.
replace_once(
    "src/aruba_mini_dashboard/services/notification_service.py",
    '''    @Slot(str)
    def acknowledge_ip(self, ip: str) -> None:
        """Suppress repeats for every currently known reason on one device."""

        for key in self._last_shown:
            if key[0] == ip:
                self._remember_acknowledgement(key)

    def clear_resolved(self, ip: str, issue_type: str) -> None:
''',
    '''    @Slot(str)
    def acknowledge_ip(self, ip: str) -> None:
        """Suppress repeats for every currently known reason on one device."""

        for key in self._last_shown:
            if key[0] == ip:
                self._remember_acknowledgement(key)

    @Slot(str)
    def acknowledge_connection_type(self, ip: str) -> None:
        """Suppress only the accepted Connection-Type notification lifecycle."""

        prefix = "connection_type_changed:"
        for key in tuple(self._last_shown):
            if key[0] == ip and (
                key[1] == "connection_type_changed" or key[1].startswith(prefix)
            ):
                self._remember_acknowledgement(key)

    def clear_resolved(self, ip: str, issue_type: str) -> None:
''',
)

replace_once(
    "src/aruba_mini_dashboard/main.py",
    '''    window.acknowledge_requested.connect(notifications.acknowledge_ip)
    window.connection_type_baseline_requested.connect(notifications.acknowledge_ip)
''',
    '''    window.acknowledge_requested.connect(notifications.acknowledge_ip)
    window.connection_type_baseline_requested.connect(
        notifications.acknowledge_connection_type
    )
''',
)

replace_once(
    "src/aruba_mini_dashboard/ui/main_window.py",
    '''        text = (
            "Connection-Type 정상 기준 설정"
            if connection_type_change
            else "알림 확인"
        )
''',
    '''        text = (
            "현재 Connection-Type 정상 기준 설정"
            if connection_type_change
            else "알림 확인"
        )
''',
)

# Existing integration tests encoded the pre-v0.6.1 auto-promotion behavior.
# Reconcile them with an operator-accepted baseline lifecycle.
test_path = "tests/test_ui_demo_runtime.py"
replace_once(
    test_path,
    '''    assert storage.get("192.0.2.12").display_value == "Type-B"
    pending = storage.load_pending_connection_changes()
''',
    '''    # The changed value is durable as a pending candidate, while the
    # operator-accepted Type-A baseline remains authoritative.
    assert storage.get("192.0.2.12").display_value == "Type-A"
    pending = storage.load_pending_connection_changes()
''',
)
replace_once(
    test_path,
    '''    monkeypatch.setattr(storage, "save_cycle_domain_state", locked)
    runtime.acknowledge_ip("192.0.2.12")
    assert runtime._pending_connection_acknowledgements == {"192.0.2.12"}
''',
    '''    monkeypatch.setattr(storage, "save_cycle_domain_state", locked)
    assert runtime.accept_connection_type_baseline("192.0.2.12") is True
    assert runtime._pending_connection_acknowledgements == {"192.0.2.12"}
''',
)

old_reverted = '''def test_reverted_connection_type_supersedes_old_event_without_recovery_across_restart(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    first_runtime.correlate(
        membership_cycle("group_membership_initial.txt", initial_at)
    )
    changed = first_runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=1),
        )
    )
    original = changed.active_incidents[0]
    assert original.details["previous"] == "Type-A"
    assert original.details["current"] == "Type-B"

    reverted = first_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=2),
        )
    )

    assert len(reverted.notification_events) == 1
    replacement = reverted.notification_events[0]
    assert replacement.incident_id != original.incident_id
    assert replacement.details["previous"] == "Type-B"
    assert replacement.details["current"] == "Type-A"
    assert [item.incident_id for item in reverted.active_incidents] == [
        replacement.incident_id
    ]

    persisted = first_storage.load_domain_incidents()
    persisted_by_id = {item.incident_id: item for item in persisted}
    superseded = persisted_by_id[original.incident_id]
    assert superseded.active is False
    assert superseded.acknowledged is False
    assert superseded.recovered_at is None
    assert persisted_by_id[replacement.incident_id].active is True
    pending = first_storage.load_pending_connection_changes()
    assert len(pending) == 1
    assert (pending[0].previous_value, pending[0].current_value) == (
        "Type-B",
        "Type-A",
    )
    event_types = [
        item["event_type"] for item in reversed(first_storage.list_events())
    ]
    assert event_types == ["activated", "activated", "superseded"]
    assert "recovered" not in event_types
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    reconfirmed = reopened_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=3),
        )
    )

    assert reconfirmed.notification_events == []
    assert [item.incident_id for item in reconfirmed.active_incidents] == [
        replacement.incident_id
    ]
    restarted_events = [
        item["event_type"] for item in reversed(reopened_storage.list_events())
    ]
    assert restarted_events == [*event_types, "updated"]
    assert restarted_events.count("activated") == 2
    assert restarted_events.count("superseded") == 1
    assert "recovered" not in restarted_events
    reopened_storage.close()
'''
new_reverted = '''def test_unaccepted_connection_type_return_to_baseline_recovers_across_restart(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_environment(tmp_path).ensure()
    settings = AppSettings.default()
    first_storage = SQLiteStorage(paths)
    first_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        first_storage,
        setup_logging(paths),
    )
    initial_at = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    first_runtime.correlate(
        membership_cycle("group_membership_initial.txt", initial_at)
    )
    changed = first_runtime.correlate(
        membership_cycle(
            "group_membership_changed.txt",
            initial_at + timedelta(minutes=1),
        )
    )
    original = changed.active_incidents[0]
    assert original.details["previous"] == "Type-A"
    assert original.details["current"] == "Type-B"

    reverted = first_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=2),
        )
    )

    assert len(reverted.notification_events) == 1
    recovered = reverted.notification_events[0]
    assert recovered.incident_id == original.incident_id
    assert recovered.active is False
    assert recovered.recovered_at == initial_at + timedelta(minutes=2)
    assert reverted.active_incidents == []
    assert first_storage.get("192.0.2.12").display_value == "Type-A"
    assert first_storage.load_pending_connection_changes() == []

    persisted = first_storage.load_domain_incidents()
    assert len(persisted) == 1
    stored = persisted[0]
    assert stored.incident_id == original.incident_id
    assert stored.active is False
    assert stored.acknowledged is False
    assert stored.recovered_at == initial_at + timedelta(minutes=2)
    event_types = [
        item["event_type"] for item in reversed(first_storage.list_events())
    ]
    assert event_types == ["activated", "recovered"]
    first_storage.close()

    reopened_storage = SQLiteStorage(paths)
    reopened_runtime = RuntimePoller(
        settings,
        paths,
        CredentialService(persistent=SessionCredentialStore()),
        reopened_storage,
        setup_logging(paths),
    )
    reconfirmed = reopened_runtime.correlate(
        membership_cycle(
            "group_membership_initial.txt",
            initial_at + timedelta(minutes=3),
        )
    )

    assert reconfirmed.notification_events == []
    assert reconfirmed.active_incidents == []
    assert reopened_storage.get("192.0.2.12").display_value == "Type-A"
    assert reopened_storage.load_pending_connection_changes() == []
    restarted_events = [
        item["event_type"] for item in reversed(reopened_storage.list_events())
    ]
    assert restarted_events == event_types
    reopened_storage.close()
'''
replace_once(test_path, old_reverted, new_reverted)

# Validate that explicit Connection-Type acceptance does not suppress another
# notification lifecycle on the same controller.
notification_test = ROOT / "tests/test_connection_type_notification_ack.py"
notification_test.write_text(
    '''from __future__ import annotations

from datetime import datetime, timezone

from aruba_mini_dashboard.services.notification_service import (
    NotificationEvent,
    NotificationService,
)


def test_connection_type_acknowledgement_is_scoped_to_that_lifecycle() -> None:
    service = NotificationService()
    now = datetime.now(timezone.utc)
    connection = NotificationEvent(
        ip="192.0.2.12",
        issue_type="connection_type_changed:event-token",
        title="Connection-Type",
        message="changed",
        detected_at=now,
    )
    distribution = NotificationEvent(
        ip="192.0.2.12",
        issue_type="client_distribution:incident-id",
        title="Client distribution",
        message="abnormal",
        detected_at=now,
    )
    service._last_shown[connection.key] = now
    service._last_shown[distribution.key] = now

    service.acknowledge_connection_type("192.0.2.12")

    assert connection.key in service._acknowledged
    assert distribution.key not in service._acknowledged
''',
    encoding="utf-8",
)

Path(__file__).unlink()
