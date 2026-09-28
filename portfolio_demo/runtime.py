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
            step.detail = f"{filename} · {len(output.splitlines())}줄 수집"
            step.evidence = {"fixture": filename, "lines": len(output.splitlines())}
            return output


class DemoRuntime:
    def __init__(self):
        self.execution = ExecutionTrace()
        self.engine = CorrelationEngine()
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
    def poll(self, failure=False):
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
            # Hold the final stage instead of silently resetting the engine.
            self.poller.index = min(self.poller.index, len(DEMO_STAGES) - 1)
            self.health = self.poller()
            self.stage = self.poller.last_stage.name
        self.poll_count += 1
        with self.execution.step("incident", "Incident 판정") as step:
            transitions = self.incidents.process(self.health)
            self.transitions.extend(transitions)
            active = self.incidents.active_incidents()
            step.status = (
                "warning" if self.health.severity.value != "normal" else "success"
            )
            step.detail = f"활성 Incident {len(active)}건 · 이번 전이 {len(transitions)}건 · {self.health.summary}"
            step.evidence = {"active": len(active), "transitions": len(transitions)}
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
