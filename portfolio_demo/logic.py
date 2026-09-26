"""Fixture transport adapter; all health decisions belong to the production engine."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aruba_mini_dashboard.models import CollectionError, PollCycleResult
from aruba_mini_dashboard.parsers import (
    parse_show_switches,
    parse_load_distribution,
    parse_group_membership,
)
from aruba_mini_dashboard.services.correlation_engine import CorrelationEngine

SCENARIOS = {
    "normal": "정상",
    "warning": "주의 · Client 분배 불균형",
    "critical": "장애 · MD Down",
    "collection_failed": "확인 불가 · CLI Timeout",
    "parse_failed": "확인 불가 · Parsing Failure",
    "partial": "부분 수집 · Membership 누락",
}
DATA = ROOT / "portfolio_demo" / "demo_data"


def run_demo(scenario: str):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")

    def read(name):
        return (DATA / name).read_text(encoding="utf-8")

    raw = {
        "show switches": read("mm_show_switches_normal.txt"),
        "show lc-cluster load distribution client": read("cluster_load_normal.txt"),
        "show lc-cluster group-membership": read("group_membership_initial.txt"),
    }
    engine = CorrelationEngine()
    expected = {f"192.0.2.{n}": f"DEMO-MD-{n - 10:02}" for n in range(11, 15)}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def cycle(outputs, index, errors=()):
        values = list(outputs.values())
        return PollCycleResult(
            checked_at=start + timedelta(seconds=index * 5),
            expected_cluster_members=expected,
            mm_result=parse_show_switches(values[0]) if values[0] else None,
            load_result=parse_load_distribution(values[1]) if values[1] else None,
            membership_result=parse_group_membership(values[2]) if values[2] else None,
            collection_errors=list(errors),
            requested_cluster_controller_ip="192.0.2.11",
            actual_cluster_controller_ip="192.0.2.11",
            raw_outputs=outputs,
        )

    engine.correlate(cycle(raw, 0))
    errors = []
    if scenario == "warning":
        raw["show lc-cluster load distribution client"] = read(
            "cluster_load_abnormal.txt"
        )
    elif scenario == "critical":
        raw["show switches"] = read("mm_show_switches_down.txt")
        raw["show lc-cluster group-membership"] = read("group_membership_changed.txt")
    elif scenario == "collection_failed":
        raw = {k: "" for k in raw}
        errors = [
            CollectionError(source=s, code="TIMEOUT", user_message="합성 CLI Timeout")
            for s in [
                "mm.show_switches",
                "cluster.load_distribution",
                "cluster.group_membership",
            ]
        ]
    elif scenario == "parse_failed":
        raw = {k: "unrecognized truncated CLI output" for k in raw}
    elif scenario == "partial":
        raw["show lc-cluster group-membership"] = ""
    timeline = []
    for index in range(1, 4):
        result = engine.correlate(cycle(raw, index, errors))
        timeline.append(
            {"Poll": index, "Status": result.severity.value, "Reason": result.summary}
        )
    return result, raw, timeline
