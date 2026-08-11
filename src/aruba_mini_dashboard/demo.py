from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collectors.base import SHOW_CLIENT_DISTRIBUTION, SHOW_GROUP_MEMBERSHIP, SHOW_SWITCHES
from .models import PollCycleResult
from .parsers import parse_group_membership, parse_load_distribution, parse_show_switches


@dataclass(frozen=True, slots=True)
class DemoStage:
    name: str
    mm_fixture: str
    load_fixture: str
    membership_fixture: str


DEMO_STAGES = (
    DemoStage("전체 정상", "mm_show_switches_normal.txt", "cluster_load_normal.txt", "group_membership_initial.txt"),
    DemoStage("WLC-02 Client 일시 저하 (1/3)", "mm_show_switches_normal.txt", "cluster_load_abnormal.txt", "group_membership_initial.txt"),
    DemoStage("WLC-02 Client 연속 저하 (2/3)", "mm_show_switches_normal.txt", "cluster_load_abnormal.txt", "group_membership_initial.txt"),
    DemoStage("WLC-02 Client 분배 이상 (3/3)", "mm_show_switches_normal.txt", "cluster_load_abnormal.txt", "group_membership_initial.txt"),
    DemoStage("WLC-02 Connection-Type 변화", "mm_show_switches_normal.txt", "cluster_load_abnormal.txt", "group_membership_changed.txt"),
    DemoStage("WLC-02 MM Status Down", "mm_show_switches_down.txt", "cluster_load_abnormal.txt", "group_membership_changed.txt"),
    DemoStage("복구 확인 (1/2)", "mm_show_switches_normal.txt", "cluster_load_normal.txt", "group_membership_changed.txt"),
    DemoStage("전체 복구 (2/2)", "mm_show_switches_normal.txt", "cluster_load_normal.txt", "group_membership_changed.txt"),
)


def demo_fixture_directory(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if path.is_dir():
            return path
        raise FileNotFoundError(f"데모 fixture 폴더를 찾을 수 없습니다: {path}")

    candidates: list[Path] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.extend(
            [
                Path(bundled) / "tests" / "fixtures",
                Path(bundled) / "fixtures",
                Path(sys.executable).resolve().parent / "fixtures",
            ]
        )
    module = Path(__file__).resolve()
    candidates.extend(parent / "tests" / "fixtures" for parent in module.parents)
    for candidate in candidates:
        if all((candidate / name).is_file() for name in _required_fixture_names()):
            return candidate
    checked = "\n".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"데모 fixture를 찾을 수 없습니다. 확인한 경로:\n{checked}")


class DemoPoller:
    """Read sanitized fixtures in sequence and run the real correlation engine."""

    def __init__(
        self,
        correlation_engine: Any,
        *,
        fixture_dir: str | Path | None = None,
    ) -> None:
        self.engine = correlation_engine
        self.fixture_dir = demo_fixture_directory(fixture_dir)
        self.index = 0
        self.last_stage: DemoStage | None = None

    def __call__(self, cancellation_event: Any | None = None) -> Any:
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("데모 점검이 취소되었습니다.")
        if self.index >= len(DEMO_STAGES):
            self.reset()
        stage_position = self.index
        stage = DEMO_STAGES[stage_position]
        self.index = stage_position + 1
        self.last_stage = stage
        mm_raw = self._read(stage.mm_fixture)
        load_raw = self._read(stage.load_fixture)
        membership_raw = self._read(stage.membership_fixture)
        mm_result = parse_show_switches(mm_raw)
        load_result = parse_load_distribution(load_raw)
        membership_result = parse_group_membership(membership_raw)
        expected = {
            row.ip: f"WLC-{position:02d}" for position, row in enumerate(load_result.rows, start=1)
        }
        # The demo represents the operator acknowledging the one-time
        # Connection-Type event before the following MM-down stage. This keeps
        # the final recovery stage visibly normal without changing production
        # acknowledgement semantics.
        if stage_position == 5 and hasattr(self.engine, "acknowledge_ip"):
            pending_engine = getattr(self.engine, "engine", None)
            pending = pending_engine.pending_connection_changes() if pending_engine is not None else ()
            for change in pending:
                self.engine.acknowledge_ip(change.member_ip)
        collector_ip = next(iter(expected), None)
        cycle = PollCycleResult(
            checked_at=datetime.now(timezone.utc),
            expected_cluster_members=expected,
            mm_result=mm_result,
            load_result=load_result,
            membership_result=membership_result,
            requested_cluster_controller_ip=collector_ip,
            actual_cluster_controller_ip=collector_ip,
            raw_outputs={
                SHOW_SWITCHES: mm_raw,
                SHOW_CLIENT_DISTRIBUTION: load_raw,
                SHOW_GROUP_MEMBERSHIP: membership_raw,
            },
        )
        health = self.engine.correlate(cycle)
        note = f"데모 단계 {stage_position + 1}/{len(DEMO_STAGES)}: {stage.name}"
        health.notes.insert(0, note)
        for device in health.devices:
            device.observations.append(note)
        return health

    def reset(self) -> None:
        resetter = getattr(self.engine, "reset_demo_state", None)
        if callable(resetter):
            resetter()
        self.index = 0
        self.last_stage = None

    def _read(self, filename: str) -> str:
        return (self.fixture_dir / filename).read_text(encoding="utf-8")


def _required_fixture_names() -> set[str]:
    names: set[str] = set()
    for stage in DEMO_STAGES:
        names.update((stage.mm_fixture, stage.load_fixture, stage.membership_fixture))
    return names
