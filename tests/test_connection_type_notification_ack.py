from __future__ import annotations

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

    service.acknowledge_ip("192.0.2.12")

    assert connection.key not in service._acknowledged
    assert distribution.key in service._acknowledged

    service.acknowledge_connection_type("192.0.2.12")

    assert connection.key in service._acknowledged
    assert distribution.key in service._acknowledged
