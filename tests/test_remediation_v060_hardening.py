from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from aruba_mini_dashboard.remediation.models import (
    ActionCommandResult,
    ActionResultCode,
    ClusterMemberObservation,
    ClusterObservation,
    DispatchPhase,
    MmObservation,
    RebalanceGateObservation,
    RemediationCandidate,
    RemediationEvent,
    RemediationOutcome,
    RemediationRun,
    RemediationStage,
)
from aruba_mini_dashboard.remediation.report import render_report, write_report
from aruba_mini_dashboard.remediation.repository import RemediationRepository
from aruba_mini_dashboard.remediation.settings import RemediationSettings
from aruba_mini_dashboard.remediation.timebase import KST, as_kst
from aruba_mini_dashboard.remediation.workflow import RemediationWorkflow


IPS = ("192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14")
LEADER, TARGET = IPS[0], IPS[1]


def _run(run_id: str = "run-v060") -> RemediationRun:
    return RemediationRun(
        run_id=run_id,
        incident_key="incident-v060",
        target_ip=TARGET,
        target_alias="WLC-02",
        cluster_name="cluster",
        expected_member_ips=IPS,
        started_at=datetime.now(timezone.utc),
        configuration_fingerprint="f" * 64,
    )


def _mm(target_state: str = "up") -> MmObservation:
    return MmObservation(
        datetime.now(timezone.utc),
        {ip: (target_state if ip == TARGET else "up") for ip in IPS},
        complete=True,
        source_ip="192.0.2.1",
    )


def _cluster() -> ClusterObservation:
    return ClusterObservation(
        datetime.now(timezone.utc),
        LEADER,
        {
            ip: ClusterMemberObservation(
                ip=ip,
                status="CONNECTED (Leader)" if ip == LEADER else "CONNECTED (Member)",
                is_connected=True,
                is_leader=ip == LEADER,
                active_clients=250,
                standby_clients=250,
            )
            for ip in IPS
        },
        complete=True,
        membership_complete=True,
        distribution_complete=True,
    )


def test_report_time_is_always_fixed_korea_standard_time() -> None:
    utc = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    localized = as_kst(utc)
    assert localized.utcoffset() == timedelta(hours=9)
    assert localized.tzname() == "KST"
    assert localized.hour == 15

    run = _run()
    run.ended_at = run.started_at
    run.outcome = RemediationOutcome.COMPLETED
    html = render_report(run)
    assert "KST, UTC+09:00" in html
    assert " KST" in html


def test_report_filename_is_bounded_and_atomic(tmp_path: Path) -> None:
    run = _run()
    run.target_alias = "매우긴장비이름" * 40
    run.ended_at = run.started_at
    path = write_report(run, tmp_path)
    assert path.is_file()
    assert len(path.name) < 150


def test_atomic_transition_persists_run_event_and_lock_together() -> None:
    repository = RemediationRepository(":memory:")
    run = _run()
    repository.create_run(run)
    assert repository.claim_target(run.target_ip, run.incident_key, run.run_id)
    run.reload_reserved = True
    run.reload_dispatch_phase = DispatchPhase.RESERVED
    event = RemediationEvent(
        1,
        datetime.now(timezone.utc),
        RemediationStage.RELOAD,
        "reload_dispatch_reserved",
        "RELOAD_DISPATCH_RESERVED",
        "reserved",
        endpoint_ip=run.target_ip,
    )
    repository.commit_transition(run, event=event)
    loaded = repository.load_run(run.run_id)
    assert loaded.reload_reserved is True
    assert loaded.reload_dispatch_phase is DispatchPhase.RESERVED
    assert loaded.events[-1].result_code == "RELOAD_DISPATCH_RESERVED"
    assert repository.is_target_claimed(run.target_ip)
    repository.close()


def test_stale_target_lock_requires_three_trusted_recovery_cycles() -> None:
    repository = RemediationRepository(":memory:")
    run = _run()
    repository.create_run(run)
    assert repository.claim_target(run.target_ip, run.incident_key, run.run_id)
    assert not repository.observe_target_recovery(run.target_ip, True, 3)
    assert repository.is_target_claimed(run.target_ip)
    assert not repository.observe_target_recovery(run.target_ip, True, 3)
    assert repository.is_target_claimed(run.target_ip)
    assert repository.observe_target_recovery(run.target_ip, True, 3)
    assert not repository.is_target_claimed(run.target_ip)
    repository.close()


def test_circuit_breaker_enforces_cluster_cooldown_and_daily_target_limit() -> None:
    repository = RemediationRepository(":memory:")
    now = datetime.now(timezone.utc)
    for index in range(2):
        run = _run(f"run-{index}")
        run.started_at = now - timedelta(minutes=10 - index)
        run.ended_at = now - timedelta(minutes=9 - index)
        run.reload_reserved = True
        run.outcome = RemediationOutcome.COMPLETED
        run.stage = RemediationStage.COMPLETED
        repository.create_run(run)
    reason = repository.circuit_breaker_reason(
        TARGET,
        cooldown_seconds=1800,
        max_actions_24h=2,
        now=now,
    )
    assert "24시간" in reason
    repository.close()


class _ActionSession:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run_reload_force(self) -> ActionCommandResult:
        self.calls.append("reload")
        return ActionCommandResult(
            "reload force",
            ActionResultCode.EXPECTED_DISCONNECT,
            True,
            None,
            message="disconnect",
            dispatch_phase=DispatchPhase.WRITE_RETURNED,
        )

    def run_rebalance(self) -> ActionCommandResult:
        self.calls.append("rebalance")
        return ActionCommandResult(
            "cluster-debug bucketmap rebalance",
            ActionResultCode.REBALANCE_TRIGGERED,
            True,
            True,
            message="triggered",
            dispatch_phase=DispatchPhase.RESPONSE_OBSERVED,
        )

    def close(self) -> None:
        pass


class _GateBackend:
    configuration_fingerprint = "f" * 64

    def __init__(self) -> None:
        self.mm_calls = 0
        self.calls: list[str] = []
        self.gate_session: _ActionSession | None = None

    def collect_mm(self) -> MmObservation:
        self.mm_calls += 1
        return _mm("down" if self.mm_calls == 1 else "up")

    def collect_cluster(self, controller_ip: str | None = None) -> ClusterObservation:
        return _cluster()

    def open_action_session(self, controller_ip: str) -> _ActionSession:
        return _ActionSession(self.calls)

    def final_rebalance_gate(
        self,
        session: _ActionSession,
        *,
        leader_ip: str,
        target_ip: str,
        expected_ips: tuple[str, ...],
    ) -> RebalanceGateObservation:
        self.gate_session = session
        return RebalanceGateObservation(
            leader_ip,
            _mm("up"),
            _cluster(),
            False,
            "LEADER_CHANGED_BEFORE_REBALANCE",
            "leader changed",
        )


def test_final_same_session_gate_blocks_rebalance_after_reload(tmp_path: Path) -> None:
    backend = _GateBackend()
    repository = RemediationRepository(":memory:")
    settings = RemediationSettings()
    settings.mm_poll_interval_seconds = 0
    settings.membership_poll_interval_seconds = 0
    settings.post_poll_interval_seconds = 0
    settings.mm_up_timeout_seconds = 2
    settings.membership_timeout_seconds = 2
    settings.post_timeout_seconds = 2
    settings.membership_confirmations = 1
    candidate = RemediationCandidate(
        "incident-gate",
        TARGET,
        "WLC-02",
        "cluster",
        IPS,
        datetime.now(timezone.utc),
        {"members": {}},
        backend.configuration_fingerprint,
    )
    result = RemediationWorkflow(
        candidate,
        backend,  # type: ignore[arg-type]
        repository,
        settings,
        reports_dir=tmp_path,
        app_version="v0.6.0",
    ).run_workflow()
    assert result.outcome is RemediationOutcome.FAILED
    assert backend.calls == ["reload"]
    assert backend.gate_session is not None
    repository.close()


def test_v1_remediation_settings_are_migrated_to_v2_defaults() -> None:
    settings = RemediationSettings.from_dict(
        {
            "schema_version": 1,
            "enabled": False,
            "ssh_max_attempts": 3,
            "ssh_retry_interval_seconds": 5,
            "mm_poll_interval_seconds": 30,
            "mm_up_timeout_seconds": 1200,
            "membership_poll_interval_seconds": 30,
            "membership_timeout_seconds": 600,
            "membership_confirmations": 2,
            "post_poll_interval_seconds": 30,
            "post_timeout_seconds": 600,
            "post_confirmations": 3,
            "report_timezone": "Asia/Seoul",
        }
    )
    assert settings.schema_version == 2
    assert settings.recovery_unlock_confirmations == 3
    assert settings.pause_after_non_success is True
