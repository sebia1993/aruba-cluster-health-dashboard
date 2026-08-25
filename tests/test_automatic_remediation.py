from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aruba_mini_dashboard.collectors.base import READ_ONLY_COMMANDS
from aruba_mini_dashboard.remediation.models import (
    ActionCommandResult,
    ActionResultCode,
    ClusterMemberObservation,
    ClusterObservation,
    MmObservation,
    RemediationCandidate,
    RemediationOutcome,
)
from aruba_mini_dashboard.remediation.report import render_report, write_report
from aruba_mini_dashboard.remediation.repository import RemediationRepository
from aruba_mini_dashboard.remediation.settings import RemediationSettings
from aruba_mini_dashboard.remediation.ssh_actions import (
    ACTION_COMMANDS,
    REBALANCE_COMMAND,
    RELOAD_FORCE_COMMAND,
    rebalance_output_confirmed,
    validate_action_command,
)
from aruba_mini_dashboard.remediation.workflow import RemediationWorkflow


IPS = ("192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14")
TARGET = IPS[1]
LEADER = IPS[0]


def _mm(states: dict[str, str]) -> MmObservation:
    return MmObservation(datetime.now(timezone.utc), states, complete=True, source_ip="192.0.2.1")


def _cluster() -> ClusterObservation:
    members = {
        ip: ClusterMemberObservation(
            ip=ip,
            status="CONNECTED (Leader)" if ip == LEADER else "CONNECTED (Member)",
            connection_type="N/A" if ip == LEADER else "L2-Connected",
            is_connected=True,
            is_leader=ip == LEADER,
            active_clients=250,
            standby_clients=250,
        )
        for ip in IPS
    }
    return ClusterObservation(
        datetime.now(timezone.utc),
        LEADER,
        members,
        complete=True,
        membership_complete=True,
        distribution_complete=True,
    )


class _ActionSession:
    def __init__(self, kind: str, calls: list[str]) -> None:
        self.kind = kind
        self.calls = calls

    def run_reload_force(self) -> ActionCommandResult:
        self.calls.append(RELOAD_FORCE_COMMAND)
        return ActionCommandResult(
            RELOAD_FORCE_COMMAND,
            ActionResultCode.EXPECTED_DISCONNECT,
            True,
            True,
            message="reload force 전송 후 SSH 연결이 종료되었습니다.",
        )

    def run_rebalance(self) -> ActionCommandResult:
        self.calls.append(REBALANCE_COMMAND)
        return ActionCommandResult(
            REBALANCE_COMMAND,
            ActionResultCode.REBALANCE_TRIGGERED,
            True,
            True,
            output_excerpt="Cluster rebalance triggered",
            message="Cluster rebalance triggered 정상 출력을 확인했습니다.",
        )

    def close(self) -> None:
        return None


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mm_calls = 0

    def collect_mm(self) -> MmObservation:
        self.mm_calls += 1
        # Precheck is Down. Recovery and post-monitoring are Up.
        state = "down" if self.mm_calls == 1 else "up"
        return _mm({ip: (state if ip == TARGET else "up") for ip in IPS})

    def collect_cluster(self, controller_ip: str | None = None) -> ClusterObservation:
        observation = _cluster()
        if controller_ip is None:
            return observation
        return ClusterObservation(
            observation.collected_at,
            controller_ip,
            observation.members,
            complete=True,
            membership_complete=True,
            distribution_complete=True,
        )

    def open_action_session(self, controller_ip: str) -> _ActionSession:
        return _ActionSession("target" if controller_ip == TARGET else "leader", self.calls)


def test_remediation_defaults_off_and_has_three_ssh_attempts() -> None:
    settings = RemediationSettings()
    settings.validate()
    assert settings.enabled is False
    assert settings.ssh_max_attempts == 3


def test_mutating_commands_are_separate_from_read_only_allowlist() -> None:
    assert RELOAD_FORCE_COMMAND not in READ_ONLY_COMMANDS
    assert REBALANCE_COMMAND not in READ_ONLY_COMMANDS
    assert ACTION_COMMANDS == frozenset({RELOAD_FORCE_COMMAND, REBALANCE_COMMAND})
    assert validate_action_command(RELOAD_FORCE_COMMAND) == RELOAD_FORCE_COMMAND
    assert validate_action_command(REBALANCE_COMMAND) == REBALANCE_COMMAND


def test_rebalance_success_requires_an_exact_independent_line() -> None:
    assert rebalance_output_confirmed("prompt\nCluster rebalance triggered\nprompt")
    assert not rebalance_output_confirmed("Cluster rebalance triggered later")
    assert not rebalance_output_confirmed("cluster rebalance triggered")


def test_successful_workflow_runs_reload_then_current_leader_rebalance(tmp_path: Path) -> None:
    backend = _Backend()
    repository = RemediationRepository(":memory:")
    settings = RemediationSettings()
    settings.mm_poll_interval_seconds = 0
    settings.mm_up_timeout_seconds = 2
    settings.membership_poll_interval_seconds = 0
    settings.membership_timeout_seconds = 2
    settings.membership_confirmations = 2
    settings.post_poll_interval_seconds = 0
    settings.post_timeout_seconds = 2
    settings.post_confirmations = 2
    detected = datetime.now(timezone.utc)
    candidate = RemediationCandidate(
        incident_key="incident-1",
        target_ip=TARGET,
        target_alias="WLC-02",
        cluster_name="Aruba 7240XM Cluster",
        expected_member_ips=IPS,
        detected_at=detected,
        detected_snapshot={"captured_at": detected, "members": {}},
    )
    result = RemediationWorkflow(
        candidate,
        backend,  # type: ignore[arg-type]
        repository,
        settings,
        reports_dir=tmp_path,
        app_version="v0.5.0",
    ).run_workflow()

    assert result.outcome is RemediationOutcome.COMPLETED
    assert result.leader_ip == LEADER
    assert backend.calls == [RELOAD_FORCE_COMMAND, REBALANCE_COMMAND]
    assert Path(result.report_path).is_file()
    assert not repository.is_target_claimed(TARGET)
    repository.close()


def test_report_escapes_dynamic_values_and_contains_timeline(tmp_path: Path) -> None:
    backend = _Backend()
    repository = RemediationRepository(":memory:")
    settings = RemediationSettings()
    for name in (
        "mm_poll_interval_seconds",
        "membership_poll_interval_seconds",
        "post_poll_interval_seconds",
    ):
        setattr(settings, name, 0)
    settings.mm_up_timeout_seconds = settings.membership_timeout_seconds = settings.post_timeout_seconds = 2
    settings.membership_confirmations = settings.post_confirmations = 1
    now = datetime.now(timezone.utc)
    candidate = RemediationCandidate(
        "incident-html",
        TARGET,
        "<WLC&02>",
        "Cluster <Core>",
        IPS,
        now,
        {"captured_at": now, "members": {}},
    )
    result = RemediationWorkflow(
        candidate,
        backend,  # type: ignore[arg-type]
        repository,
        settings,
        reports_dir=tmp_path,
        app_version="v0.5.0",
    ).run_workflow()
    run = repository.load_run(result.run_id)
    html = render_report(run)
    assert "&lt;WLC&amp;02&gt;" in html
    assert "<WLC&02>" not in html
    assert "조치 타임라인" in html
    assert "Cluster rebalance triggered" in html
    written = write_report(run, tmp_path)
    assert written.is_file()
    repository.close()
