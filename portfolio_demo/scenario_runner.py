"""Orchestrate real DemoRuntime polls; never synthesize health or incidents."""

from copy import deepcopy
from dataclasses import dataclass, field
from time import perf_counter

from portfolio_demo.execution_trace import ExecutionTrace
from portfolio_demo.runtime import DemoRuntime

SCENARIOS = {
    "incident_recovery": "단말 분배 이상 → 복구",
    "normal": "정상 상태",
    "collection_failure": "CLI 수집 실패",
    "connection_change": "Connection-Type 변화",
}


@dataclass(frozen=True)
class ScenarioStep:
    stage_index: int = 0
    failure: bool = False


@dataclass
class PollSnapshot:
    poll: int
    title: str
    explanation: str
    health: object
    devices: list
    detector: dict
    incidents: list
    transitions: list
    pending: list
    baselines: dict
    trace: ExecutionTrace
    active_total: int | None
    up: int | None
    focus: list
    anomaly_count: int
    recovery_count: int


@dataclass
class ScenarioRun:
    key: str
    name: str
    steps: list
    snapshots: list = field(default_factory=list)
    current_index: int = -1
    completed: bool = False
    error: str = ""
    elapsed_ms: float | None = None
    summary: str = ""


class ScenarioRunner:
    def __init__(self, settings=None):
        self.settings = settings
        self.runtime = DemoRuntime(settings)
        self.run = None

    def reset(self):
        self.runtime = DemoRuntime(self.settings)
        self.run = None

    def play(self, key, on_change=None):
        if key not in SCENARIOS:
            raise ValueError("지원하지 않는 시나리오입니다.")
        self.reset()
        settings = self.runtime.engine.detector.settings
        normal = ScenarioStep(0)
        plans = {
            "incident_recovery": [normal]
            + [ScenarioStep(1)] * settings.anomaly_confirmations
            + [normal] * settings.recovery_confirmations,
            "normal": [normal]
            * max(settings.anomaly_confirmations, settings.recovery_confirmations),
            "collection_failure": [normal, ScenarioStep(failure=True)],
            "connection_change": [normal, ScenarioStep(4)],
        }
        steps = plans[key]
        if len(steps) > 100:
            raise ValueError("설정된 기준이 100 Poll 체험 한도를 초과합니다.")
        self.run = ScenarioRun(key, SCENARIOS[key], steps)
        started = perf_counter()
        if on_change:
            self.runtime.execution.on_change = lambda: on_change(self)
        try:
            for index, step in enumerate(steps):
                self.run.current_index = index
                if on_change:
                    on_change(self)
                start_transition = len(self.runtime.transitions)
                self.runtime.poll(failure=step.failure, stage_index=step.stage_index)
                snapshot = self._snapshot(start_transition)
                self.runtime.stage = snapshot.title
                self.runtime.history[-1]["Stage"] = snapshot.title
                self.run.snapshots.append(snapshot)
                if on_change:
                    on_change(self)
            self.run.completed = True
            self.run.summary = self.run.snapshots[-1].explanation
        except Exception as exc:
            self.run.error = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            self.runtime.execution.on_change = None
            self.run.elapsed_ms = (perf_counter() - started) * 1000
            if on_change:
                on_change(self)
        return self.run

    def _snapshot(self, start_transition):
        r = self.runtime
        settings = r.engine.detector.settings
        evaluation = r.engine.detector.evaluations
        pending = r.engine.pending_connection_changes()
        transitions = r.transitions[start_transition:]
        affected = [
            d
            for d in r.health.devices
            if (
                evaluation[d.ip].condition_met
                or evaluation[d.ip].active
                or evaluation[d.ip].recovered
                or d.connection_type_changed
            )
        ]
        focus = [
            {
                "ip": d.ip,
                "alias": d.display_name,
                "active": d.active_clients,
                "standby": d.standby_clients,
            }
            for d in affected
        ]
        anomaly = max((v.anomaly_streak for v in evaluation.values()), default=0)
        recovery = max((v.recovery_streak for v in evaluation.values()), default=0)
        recovered = any(v.recovered for v in evaluation.values())
        if recovered:
            # Production resets confirmed recovery counters to zero. Display the
            # last retained streak + this trusted recovery observation, not a preset.
            recovery = max(
                int(
                    r.engine.detector.before.get("load|" + ip, {}).get(
                        "recovery_streak", 0
                    )
                )
                + 1
                for ip, value in evaluation.items()
                if value.recovered
            )
        names = ", ".join(f"{d.display_name} ({d.ip})" for d in affected)
        if r.health.severity.value == "unknown":
            title = "수집 실패 · 확인 불가"
            explanation = "일부 정보를 수집하지 못했으므로 정상/장애를 단정할 수 없습니다. 장비 Down으로 추정하지 않고 확인 불가 상태로 유지합니다. Collection Failure는 수집 경로의 문제 기록입니다."
        elif pending:
            title = "Connection-Type 변화 · 기준 수용 대기"
            explanation = (
                "기준 상태와 다른 Connection-Type이 관측되었습니다. "
                + "; ".join(
                    f"{p.member_ip}: {p.previous_value} → {p.current_value}"
                    for p in pending
                )
                + ". 정상 기준은 자동 변경하지 않습니다."
            )
        elif recovered:
            title = "복구 완료"
            explanation = f"{names}에서 연속 정상 관측 {recovery}/{settings.recovery_confirmations}회가 확인되어 Incident가 종료되었습니다."
        elif recovery:
            title = "복구 관측 · 이상 기록 유지"
            explanation = f"{names}의 정상 값이 다시 관측됐지만 복구 기준 {recovery}/{settings.recovery_confirmations}회로, 아직 Incident를 유지합니다."
        elif any(v.active for v in evaluation.values()):
            title = "이상 확정 · 주의 기록 생성"
            explanation = f"{names}의 Client 분배 이상이 연속 {anomaly}/{settings.anomaly_confirmations}회 확인되어 주의 Incident를 생성했습니다. 장비 Down을 뜻하지는 않습니다."
        elif anomaly:
            title = "이상 징후 최초 관측" if anomaly == 1 else "이상 지속 · 확정 전"
            explanation = f"{names}의 Client 분배 이상이 연속 {anomaly}/{settings.anomaly_confirmations}회 관측되었습니다. 확정 기준에 도달하지 않아 Incident를 생성하지 않습니다."
        else:
            title = "정상 상태"
            explanation = "여러 관측값을 종합한 결과 현재 확정된 장애 징후가 없습니다."
        trace = ExecutionTrace()
        trace.steps = deepcopy(r.execution.steps)
        trace.label = f"Poll #{r.poll_count} · {title}"
        trace.elapsed_ms = r.execution.elapsed_ms
        values = [d.active_clients for d in r.health.devices]
        return PollSnapshot(
            r.poll_count,
            title,
            explanation,
            deepcopy(r.health),
            deepcopy(r.rows()),
            deepcopy(r.engine.detector.dump_state()),
            deepcopy(r.incidents.events()),
            deepcopy(transitions),
            deepcopy(pending),
            {
                d.ip: deepcopy(r.engine.baseline_store.get(d.ip))
                for d in r.health.devices
            },
            trace,
            sum(values) if all(v is not None for v in values) else None,
            sum(d.mm_status.lower() == "up" for d in r.health.devices)
            if all(d.mm_status is not None for d in r.health.devices)
            else None,
            focus,
            anomaly,
            recovery,
        )

    def run_normal(self, on_change=None):
        return self.play("normal", on_change)

    def run_incident_recovery(self, on_change=None):
        return self.play("incident_recovery", on_change)

    def run_collection_failure(self, on_change=None):
        return self.play("collection_failure", on_change)

    def run_connection_change(self, on_change=None):
        return self.play("connection_change", on_change)
