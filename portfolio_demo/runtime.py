"""Session-owned production demo, correlation and incident lifecycle."""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aruba_mini_dashboard.demo import DEMO_STAGES, DemoPoller
from aruba_mini_dashboard.models import CollectionError, IncidentType, PollCycleResult
from aruba_mini_dashboard.services.correlation_engine import CorrelationEngine
from aruba_mini_dashboard.services.incident_manager import IncidentManager
from aruba_mini_dashboard.services.anomaly_detector import AnomalyDetector
from aruba_mini_dashboard.collectors.base import (
    SHOW_SWITCHES,
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
)

TOPOLOGY = {f"192.0.2.{i}": f"DEMO-MD-{i - 10:02}" for i in range(11, 15)}


class PollBridge:
    """Expose correlation only, not DemoPoller's scripted baseline acceptor.

    The desktop demonstration auto-accepts at stage 5. In the interactive
    public console only an explicit operator action may change the baseline.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    def correlate(self, cycle):
        cycle.expected_cluster_members = dict(TOPOLOGY)
        self.runtime.raw = dict(cycle.raw_outputs)
        trace = self.runtime.execution
        parsed = (cycle.mm_result, cycle.load_result, cycle.membership_result)
        counts = [
            len(result.rows) if result and result.is_complete else None
            for result in parsed
        ]
        trace.record(
            "parser",
            "Production Parser",
            "success" if all(n is not None for n in counts) else "warning",
            "MM / Client / Membership 관측 행: "
            + " / ".join(str(n) if n is not None else "확인 불가" for n in counts),
            {"rows": counts},
        )
        with trace.step("correlation", "IP 기준 상관분석 / Detector") as step:
            health = self.runtime.engine.correlate(cycle)
            unknown = sum(d.severity.value == "unknown" for d in health.devices)
            step.status = "warning" if unknown else "success"
            step.detail = f"대상 {len(health.devices)}대 · 확인 불가 {unknown}대 · {health.summary}"
            step.evidence = {"controllers": len(health.devices), "unknown": unknown}
        return health


from portfolio_demo.execution_trace import ExecutionTrace, traced


class EvidencePoller(DemoPoller):
    def _read(self, filename):
        trace = self.engine.runtime.execution
        stage = self.last_stage
        label = next(
            (
                label
                for name, label in (
                    (stage.mm_fixture, "MM Controller 상태 수집"),
                    (stage.load_fixture, "Client 분배 수집"),
                    (stage.membership_fixture, "Cluster Membership 수집"),
                )
                if filename == name
            ),
            "합성 CLI 수집",
        )
        with trace.step("collect", label, filename) as step:
            output = super()._read(filename)
            command = {
                stage.mm_fixture: SHOW_SWITCHES,
                stage.load_fixture: SHOW_CLIENT_DISTRIBUTION,
                stage.membership_fixture: SHOW_GROUP_MEMBERSHIP,
            }[filename]
            step.detail = f"{command} · {filename} · {len(output.splitlines())}줄 수집"
            step.evidence = {
                "fixture": filename,
                "command": command,
                "lines": len(output.splitlines()),
            }
            return output


class EvidenceDetector(AnomalyDetector):
    """Observe the real evaluation, including recovery before counters reset."""

    def evaluate_client_distribution(self, *args, **kwargs):
        self.before = self.dump_state()
        self.evaluations = super().evaluate_client_distribution(*args, **kwargs)
        return self.evaluations


class DemoRuntime:
    def __init__(self, settings=None):
        self.execution = ExecutionTrace()
        self.engine = CorrelationEngine(detector=EvidenceDetector(settings))
        self.incidents = IncidentManager()
        self.poller = EvidencePoller(PollBridge(self))
        self.health = None
        self.raw = {}
        self.history = []
        self.transitions = []
        self.running = False
        self.poll_count = 0
        self.stage = "아직 점검하지 않음"

    @traced("Controller 점검")
    def poll(self, failure=False, *, stage_index=None):
        if self.poll_count >= 100:
            self.running = False
            raise ValueError(
                "100 Poll 체험을 마쳤습니다. Demo Reset으로 새로 시작하세요."
            )
        if failure:
            self.raw = {}
            for source in ("MM Controller 상태", "Client 분배", "Cluster Membership"):
                self.execution.record(
                    "collect", source, "failure", "CLI Timeout · 관측 확인 불가"
                )
            self.health = PollBridge(self).correlate(
                PollCycleResult(
                    checked_at=datetime.now(timezone.utc),
                    expected_cluster_members=dict(TOPOLOGY),
                    collection_errors=[
                        CollectionError(
                            source=s, code="TIMEOUT", user_message="합성 CLI Timeout"
                        )
                        for s in (
                            "mm.show_switches",
                            "cluster.load_distribution",
                            "cluster.group_membership",
                        )
                    ],
                )
            )
            self.stage = "수집 Timeout · 단계 진행 보류"
        else:
            if stage_index is not None:
                if not 0 <= stage_index < len(DEMO_STAGES):
                    raise ValueError("유효하지 않은 합성 입력 단계입니다.")
                self.poller.index = stage_index
            # Hold the final stage instead of silently resetting the engine.
            self.poller.index = min(self.poller.index, len(DEMO_STAGES) - 1)
            self.health = self.poller()
            self.stage = self.poller.last_stage.name
        detector = self.engine.detector
        evaluations = {ip: asdict(value) for ip, value in detector.evaluations.items()}
        counts = " / ".join(
            f"{TOPOLOGY.get(ip, ip)} 이상 {e['anomaly_streak']}/{detector.settings.anomaly_confirmations}, "
            f"복구 {int(detector.before.get('load|' + ip, {}).get('recovery_streak', 0)) + 1 if e['recovered'] else e['recovery_streak']}/{detector.settings.recovery_confirmations}"
            + (" (복구 확정, 내부 counter는 0으로 초기화)" if e["recovered"] else "")
            for ip, e in evaluations.items()
            if e["anomaly_streak"] or e["recovery_streak"] or e["recovered"]
        )
        self.execution.record(
            "detector",
            "Detector 연속 관측 판정",
            "warning"
            if any(
                e["active"] or e["deferred"] or e["condition_met"]
                for e in evaluations.values()
            )
            else "success",
            counts
            or (
                "관측 불완전 · 판정 보류" if failure else "확정된 Client 분배 이상 없음"
            ),
            {
                "evaluations": evaluations,
                "settings": asdict(detector.settings),
                "before": detector.before,
                "after": detector.dump_state(),
            },
        )
        self.poll_count += 1
        with self.execution.step("incident", "Incident 판정") as step:
            transitions = self.incidents.process(self.health)
            self.transitions.extend(transitions)
            active = self.incidents.active_incidents()
            step.status = (
                "warning" if self.health.severity.value != "normal" else "success"
            )
            step.detail = f"활성 Incident {len(active)}건 · 이번 전이 {len(transitions)}건 · {self.health.summary}"
            step.evidence = {
                "active": len(active),
                "transitions": len(transitions),
                "events": [asdict(t) for t in transitions],
            }
            if transitions:
                step.detail += " · " + ", ".join(t.kind.value for t in transitions)
        self.history.append(
            {
                "Poll": self.poll_count,
                "Stage": self.stage,
                "Status": self.health.severity.value,
                "Reason": self.health.summary,
                "Active incidents": len(self.incidents.active_incidents()),
            }
        )
        return self.health

    def acknowledge(self, ip):
        # Match the desktop's generic ACK: Connection-Type acceptance is separate.
        for incident in self.incidents.active_incidents():
            if (
                incident.ip == ip
                and incident.incident_type is not IncidentType.CONNECTION_TYPE_CHANGED
            ):
                transition = self.incidents.acknowledge(incident.incident_id)
                if transition:
                    self.transitions.append(transition)

    def accept_baseline(self, ip):
        if not self.engine.acknowledge_connection_change(ip):
            return False
        for incident in self.incidents.active_incidents():
            if (
                incident.ip == ip
                and incident.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
            ):
                transition = self.incidents.acknowledge(incident.incident_id)
                if transition:
                    self.transitions.append(transition)
        return True

    def rows(self):
        if not self.health:
            return []
        return [
            {
                "IP": d.ip,
                "Alias": d.display_name,
                "MM": d.mm_status or "확인 불가",
                "Active": d.active_clients,
                "Standby": d.standby_clients,
                "Connection-Type": d.connection_type or "확인 불가",
                "Severity": d.severity.value,
                "Incident / ACK": ", ".join(
                    ("ACK" if i.acknowledged else "미확인")
                    + ":"
                    + i.incident_type.value
                    for i in self.incidents.active_incidents()
                    if i.ip == d.ip
                ),
                "Reason": " / ".join(d.issue_reasons),
            }
            for d in self.health.devices
        ]

    def incident_rows(self):
        return [
            {
                **asdict(i),
                "severity": i.severity.value,
                "incident_type": i.incident_type.value,
            }
            for i in self.incidents.events()
        ]
