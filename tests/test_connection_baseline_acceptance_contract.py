from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_main_window_exposes_explicit_connection_type_baseline_action() -> None:
    source = (ROOT / "src/aruba_mini_dashboard/ui/main_window.py").read_text(
        encoding="utf-8"
    )
    assert "connection_type_baseline_requested = Signal(str)" in source
    assert "Connection-Type 정상 기준 설정" in source
    assert "같은 IP의 Client 분배 등 다른 장애 알림은 확인 처리하지 않습니다." in source


def test_runtime_connects_the_explicit_baseline_action() -> None:
    source = (ROOT / "src/aruba_mini_dashboard/main.py").read_text(encoding="utf-8")
    assert "def accept_connection_type_baseline" in source
    assert "runtime.accept_connection_type_baseline" in source
    assert "drain_connection_change_resolutions" in source


def test_generic_acknowledgement_does_not_accept_connection_baseline() -> None:
    source = (ROOT / "src/aruba_mini_dashboard/main.py").read_text(encoding="utf-8")
    start = source.index("    def acknowledge_ip(self, ip: str) -> None:")
    end = source.index("    def acknowledge_global(self) -> None:", start)
    body = source[start:end]
    assert "acknowledge_connection_change" not in body
    assert "IncidentType.CONNECTION_TYPE_CHANGED" in body
