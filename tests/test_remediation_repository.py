from __future__ import annotations

from datetime import datetime, timezone

from aruba_mini_dashboard.remediation.models import RemediationRun
from aruba_mini_dashboard.remediation.repository import RemediationRepository


def _run(run_id: str = "run-1") -> RemediationRun:
    return RemediationRun(
        run_id=run_id,
        incident_key="incident-1",
        target_ip="192.0.2.12",
        target_alias="WLC-02",
        cluster_name="cluster",
        expected_member_ips=("192.0.2.11", "192.0.2.12"),
        started_at=datetime.now(timezone.utc),
    )


def test_target_lock_prevents_duplicate_remediation() -> None:
    repository = RemediationRepository(":memory:")
    first = _run("run-1")
    second = _run("run-2")
    repository.create_run(first)
    repository.create_run(second)
    assert repository.claim_target(first.target_ip, first.incident_key, first.run_id)
    assert not repository.claim_target(second.target_ip, second.incident_key, second.run_id)
    repository.release_target(first.target_ip)
    assert repository.claim_target(second.target_ip, second.incident_key, second.run_id)
    repository.close()


def test_interrupted_run_keeps_target_lock() -> None:
    repository = RemediationRepository(":memory:")
    run = _run()
    repository.create_run(run)
    assert repository.claim_target(run.target_ip, run.incident_key, run.run_id)
    recovered = repository.recover_interrupted_runs()
    assert recovered == [run.run_id]
    assert repository.is_target_claimed(run.target_ip)
    loaded = repository.load_run(run.run_id)
    assert loaded.ended_at is not None
    assert loaded.failure_code == "PROCESS_INTERRUPTED"
    repository.close()
