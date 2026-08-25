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


replace_once(
    "src/aruba_mini_dashboard/services/notification_service.py",
    '''    @Slot(str)
    def acknowledge_ip(self, ip: str) -> None:
        """Suppress repeats for every currently known reason on one device."""

        for key in self._last_shown:
            if key[0] == ip:
                self._remember_acknowledgement(key)
''',
    '''    @Slot(str)
    def acknowledge_ip(self, ip: str) -> None:
        """Suppress non-Connection-Type reasons currently known for a device.

        Connection-Type is deliberately excluded because accepting it changes
        the durable normal baseline and therefore requires the explicit
        confirmation path.
        """

        prefix = "connection_type_changed:"
        for key in tuple(self._last_shown):
            if key[0] != ip:
                continue
            if key[1] == "connection_type_changed" or key[1].startswith(prefix):
                continue
            self._remember_acknowledgement(key)
''',
)

replace_once(
    "tests/test_connection_type_notification_ack.py",
    '''    service.acknowledge_connection_type("192.0.2.12")

    assert connection.key in service._acknowledged
    assert distribution.key not in service._acknowledged
''',
    '''    service.acknowledge_ip("192.0.2.12")

    assert connection.key not in service._acknowledged
    assert distribution.key in service._acknowledged

    service.acknowledge_connection_type("192.0.2.12")

    assert connection.key in service._acknowledged
    assert distribution.key in service._acknowledged
''',
)

Path(__file__).unlink()
