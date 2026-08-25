from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from aruba_mini_dashboard.collectors.base import SshOperationError

from .backend import (
    RemediationBackend,
    cluster_snapshot,
    combined_snapshot,
    mm_snapshot,
)
from .models import (
    ActionResultCode,
    ClusterObservation,
    MmObservation,
    RemediationCandidate,
    RemediationEvent,
    RemediationOutcome,
    RemediationRun,
    RemediationStage,
    WorkflowResult,
    utc_now,
)
from .report import write_report
from .repository import RemediationRepository
from .settings import RemediationSettings


@dataclass(slots=True)
class _WorkflowAbort(RuntimeError):
    code: str
    message: str
    outcome: RemediationOutcome = RemediationOutcome.FAILED
    stage: RemediationStage = RemediationStage.FAILED

    def __str__(self) -> str:
        return self.message


class RemediationWorkflow:
    """Fail-closed one-target state machine for reload, rejoin and rebalance."""

    def __init__(
        self,
        candidate: RemediationCandidate,
        backend: RemediationBackend,
        repository: RemediationRepository,
        settings: RemediationSettings,
        *,
        reports_dir: str | Path,
        app_version: str,
        cancel_event: threading.Event | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.candidate = candidate
        self.backend = backend
        self.repository = repository
        self.settings = settings
        self.reports_dir = Path(reports_dir)
        self.app_version = app_version
        self.cancel_event = cancel_event or threading.Event()
        self.now = now
        stamp = candidate.detected_at.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        self.run = RemediationRun(
            run_id=f"REM-{stamp}-{suffix}",
            incident_key=candidate.incident_key,
            target_ip=candidate.target_ip,
            target_alias=candidate.target_alias,
            cluster_name=candidate.cluster_name,
            expected_member_ips=candidate.expected_member_ips,
            started_at=self.now(),
            app_version=app_version,
        )
        self._sequence = 0

    def _event(
        self,
        stage: RemediationStage,
        operation: str,
        result_code: str,
        message: str,
        *,
        endpoint_ip: str = "",
        attempt: int | None = None,
        duration_ms: int = 0,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self._sequence += 1
        event = RemediationEvent(
            sequence_no=self._sequence,
            occurred_at=self.now(),
            stage=stage,
            operation=operation,
            result_code=result_code,
            message=message,
            endpoint_ip=endpoint_ip,
            attempt=attempt,
            duration_ms=max(0, int(duration_ms)),
            evidence={} if evidence is None else dict(evidence),
        )
        self.run.events.append(event)
        self.run.stage = stage
        self.repository.append_event(self.run.run_id, event)
        self.repository.update_run(self.run)

    def _snapshot(self, name: str, data: dict[str, Any]) -> None:
        self.run.snapshots[name] = data
        self.repository.save_snapshot(self.run.run_id, name, data)

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise _WorkflowAbort(
                "OPERATOR_STOPPED",
                "자동 장애조치가 사용자 요청으로 중단되었습니다.",
                RemediationOutcome.STOPPED,
                RemediationStage.STOPPED,
            )

    def _wait(self, seconds: int) -> None:
        if self.cancel_event.wait(max(0, int(seconds))):
            self._check_cancelled()

    def run_workflow(self) -> WorkflowResult:
        self.repository.create_run(self.run)
        if not self.repository.claim_target(
            self.candidate.target_ip,
            self.candidate.incident_key,
            self.run.run_id,
        ):
            return self._finalize(
                RemediationOutcome.STOPPED,
                RemediationStage.STOPPED,
                "TARGET_ALREADY_CLAIMED",
                "동일 Controller에 대한 이전 자동 장애조치 잠금이 있어 실행하지 않았습니다.",
                release_target=False,
            )
        self._snapshot("detected", dict(self.candidate.detected_snapshot))
        self._event(
            RemediationStage.DETECTED,
            "incident_detected",
            "MM_DOWN_DETECTED",
            "신뢰 가능한 MM 결과에서 등록 Controller 1대의 Down 상태를 감지했습니다.",
            endpoint_ip=self.candidate.target_ip,
        )
        try:
            mm_pre = self._precheck()
            cluster_pre = self._best_effort_cluster_snapshot()
            self._snapshot(
                "pre_action",
                combined_snapshot(mm_pre, cluster_pre, self.candidate.expected_member_ips),
            )
            action = self._connect_target()
            try:
                self.run.reload_reserved = True
                self.repository.update_run(self.run)
                self._event(
                    RemediationStage.RELOAD,
                    "reload_dispatch_reserved",
                    "RELOAD_DISPATCH_RESERVED",
                    "중복 실행 방지를 위해 reload force 전송 슬롯을 영구 예약했습니다.",
                    endpoint_ip=self.candidate.target_ip,
                )
                reload_result = action.run_reload_force()
            finally:
                action.close()
            self.run.reload_sent = reload_result.sent
            self.repository.update_run(self.run)
            self._event(
                RemediationStage.RELOAD,
                "reload_force",
                reload_result.code.value,
                reload_result.message,
                endpoint_ip=self.candidate.target_ip,
                duration_ms=reload_result.duration_ms,
                evidence={"accepted": reload_result.accepted},
            )
            if reload_result.code in {
                ActionResultCode.RELOAD_REJECTED,
                ActionResultCode.ACTION_NOT_SENT,
            }:
                raise _WorkflowAbort(
                    reload_result.code.value.upper(),
                    reload_result.message,
                )

            mm_up = self._wait_for_mm_up()
            leader_ip, rejoin = self._wait_for_rejoin()
            self.run.leader_ip = leader_ip
            self.repository.update_run(self.run)
            self._snapshot(
                "pre_rebalance",
                combined_snapshot(mm_up, rejoin, self.candidate.expected_member_ips),
            )
            leader_session = self._connect_leader(leader_ip)
            try:
                self.run.rebalance_reserved = True
                self.repository.update_run(self.run)
                self._event(
                    RemediationStage.REBALANCE,
                    "rebalance_dispatch_reserved",
                    "REBALANCE_DISPATCH_RESERVED",
                    "중복 실행 방지를 위해 클러스터 재분배 전송 슬롯을 영구 예약했습니다.",
                    endpoint_ip=leader_ip,
                )
                rebalance_result = leader_session.run_rebalance()
            finally:
                leader_session.close()
            self.run.rebalance_sent = rebalance_result.sent
            self.run.rebalance_confirmed = (
                rebalance_result.code is ActionResultCode.REBALANCE_TRIGGERED
            )
            self.repository.update_run(self.run)
            self._event(
                RemediationStage.REBALANCE,
                "cluster_rebalance",
                rebalance_result.code.value,
                rebalance_result.message,
                endpoint_ip=leader_ip,
                duration_ms=rebalance_result.duration_ms,
                evidence={"accepted": rebalance_result.accepted},
            )
            if rebalance_result.code is ActionResultCode.REBALANCE_REJECTED:
                raise _WorkflowAbort(
                    "REBALANCE_REJECTED",
                    rebalance_result.message,
                )

            post_ok, mm_post, cluster_post = self._post_monitor()
            self._snapshot(
                "post_action",
                combined_snapshot(mm_post, cluster_post, self.candidate.expected_member_ips),
            )
            if post_ok and self.run.rebalance_confirmed:
                return self._finalize(
                    RemediationOutcome.COMPLETED,
                    RemediationStage.COMPLETED,
                    "",
                    "Controller 재부팅, Cluster 재가입, Leader 재분배 요청과 사후 정상 상태를 모두 확인했습니다.",
                    release_target=True,
                )
            if post_ok:
                return self._finalize(
                    RemediationOutcome.PARTIAL,
                    RemediationStage.PARTIAL,
                    "REBALANCE_OUTPUT_UNCONFIRMED",
                    "Controller 복구와 사후 정상 상태는 확인했으나 재분배 정상 출력은 확인하지 못했습니다.",
                    release_target=True,
                )
            return self._finalize(
                RemediationOutcome.PARTIAL,
                RemediationStage.PARTIAL,
                "POST_MONITOR_TIMEOUT",
                "Controller는 복구되었으나 제한시간 내 사후 정상 상태를 연속 확인하지 못했습니다.",
                release_target=True,
            )
        except _WorkflowAbort as exc:
            return self._finalize(
                exc.outcome,
                exc.stage,
                exc.code,
                exc.message,
                release_target=(not self.run.reload_reserved),
            )
        except SshOperationError as exc:
            outcome = RemediationOutcome.STOPPED if exc.code == "CANCELLED" else RemediationOutcome.FAILED
            stage = RemediationStage.STOPPED if exc.code == "CANCELLED" else RemediationStage.FAILED
            return self._finalize(
                outcome,
                stage,
                exc.code,
                exc.user_message,
                release_target=(not self.run.reload_reserved),
            )
        except Exception:
            return self._finalize(
                RemediationOutcome.FAILED,
                RemediationStage.FAILED,
                "UNEXPECTED_REMEDIATION_ERROR",
                "예기치 않은 오류로 자동 장애조치가 종료되었습니다. 세부 로그를 확인하세요.",
                release_target=(not self.run.reload_reserved),
            )


    def _precheck(self) -> MmObservation:
        self._check_cancelled()
        self._event(
            RemediationStage.PRECHECK,
            "mm_precheck",
            "STARTED",
            "reload force 실행 직전 MM 상태를 다시 확인합니다.",
            endpoint_ip=self.candidate.target_ip,
        )
        mm = self.backend.collect_mm()
        self._snapshot("mm_precheck", mm_snapshot(mm))
        if not mm.complete:
            raise _WorkflowAbort(
                mm.error_code or "MM_PRECHECK_UNTRUSTED",
                mm.error_message or "MM 상태가 완전하게 수집되지 않아 자동조치를 중단했습니다.",
            )
        expected = set(self.candidate.expected_member_ips)
        down = tuple(sorted(ip for ip in expected if mm.state_for(ip) == "down"))
        if down != (self.candidate.target_ip,):
            raise _WorkflowAbort(
                "MM_PRECHECK_CHANGED",
                f"실행 직전 Down 대상이 최초 감지와 달라 자동조치를 중단했습니다: {', '.join(down) or '없음'}",
                RemediationOutcome.STOPPED,
                RemediationStage.STOPPED,
            )
        self._event(
            RemediationStage.PRECHECK,
            "mm_precheck",
            "CONFIRMED",
            "실행 직전에도 대상 Controller 1대만 Down임을 확인했습니다.",
            endpoint_ip=self.candidate.target_ip,
        )
        return mm

    def _best_effort_cluster_snapshot(self) -> ClusterObservation | None:
        try:
            cluster = self.backend.collect_cluster()
        except Exception as exc:
            self._event(
                RemediationStage.PRECHECK,
                "pre_action_cluster_snapshot",
                "UNAVAILABLE",
                "조치 전 Cluster 비교 스냅샷을 수집하지 못했지만 MM Down 근거에는 영향을 주지 않습니다.",
            )
            return None
        self._snapshot("cluster_precheck", cluster_snapshot(cluster))
        self._event(
            RemediationStage.PRECHECK,
            "pre_action_cluster_snapshot",
            "COLLECTED" if cluster.members else "PARTIAL",
            "조치 전 Cluster Membership 및 Client 분배 비교값을 기록했습니다.",
            endpoint_ip=cluster.source_ip,
        )
        return cluster

    def _connect_target(self):
        last_error: SshOperationError | None = None
        for attempt in range(1, self.settings.ssh_max_attempts + 1):
            self._check_cancelled()
            started = monotonic()
            try:
                adapter = self.backend.open_action_session(self.candidate.target_ip)
            except SshOperationError as exc:
                last_error = exc
                self._event(
                    RemediationStage.TARGET_SSH,
                    "target_ssh_connect",
                    exc.code,
                    exc.user_message,
                    endpoint_ip=self.candidate.target_ip,
                    attempt=attempt,
                    duration_ms=int((monotonic() - started) * 1000),
                )
                if not exc.retryable:
                    raise _WorkflowAbort(exc.code, exc.user_message)
                if attempt < self.settings.ssh_max_attempts:
                    self._wait(self.settings.ssh_retry_interval_seconds)
                continue
            except Exception:
                self._event(
                    RemediationStage.TARGET_SSH,
                    "target_ssh_connect",
                    "SSH_CONNECT_FAILED",
                    "대상 Controller SSH 접속에 실패했습니다.",
                    endpoint_ip=self.candidate.target_ip,
                    attempt=attempt,
                    duration_ms=int((monotonic() - started) * 1000),
                )
                if attempt < self.settings.ssh_max_attempts:
                    self._wait(self.settings.ssh_retry_interval_seconds)
                continue
            self._event(
                RemediationStage.TARGET_SSH,
                "target_ssh_connect",
                "CONNECTED",
                "대상 Controller SSH 접속에 성공했습니다.",
                endpoint_ip=self.candidate.target_ip,
                attempt=attempt,
                duration_ms=int((monotonic() - started) * 1000),
            )
            return adapter
        message = (
            last_error.user_message
            if last_error is not None
            else "대상 Controller SSH 접속을 최대 3회 시도했으나 모두 실패했습니다."
        )
        raise _WorkflowAbort("TARGET_SSH_ATTEMPTS_EXHAUSTED", message)

    def _wait_for_mm_up(self) -> MmObservation:
        deadline = monotonic() + self.settings.mm_up_timeout_seconds
        attempt = 0
        while monotonic() < deadline:
            self._check_cancelled()
            attempt += 1
            mm = self.backend.collect_mm()
            state = mm.state_for(self.candidate.target_ip)
            self._event(
                RemediationStage.WAITING_MM_UP,
                "mm_recovery_poll",
                "UP" if mm.complete and state == "up" else (mm.error_code or state.upper()),
                (
                    "MM에서 대상 Controller Up을 확인했습니다."
                    if mm.complete and state == "up"
                    else "MM에서 대상 Controller 복구를 대기 중입니다."
                ),
                endpoint_ip=self.candidate.target_ip,
                attempt=attempt,
                evidence={"complete": mm.complete, "state": state},
            )
            if mm.complete and state == "up":
                self._snapshot("mm_up", mm_snapshot(mm))
                return mm
            self._wait(self.settings.mm_poll_interval_seconds)
        raise _WorkflowAbort(
            "MM_UP_TIMEOUT",
            "제한시간 내 MM에서 대상 Controller Up을 확인하지 못했습니다.",
        )

    def _wait_for_rejoin(self) -> tuple[str, ClusterObservation]:
        deadline = monotonic() + self.settings.membership_timeout_seconds
        stable = 0
        stable_leader = ""
        last_observation: ClusterObservation | None = None
        attempt = 0
        while monotonic() < deadline:
            self._check_cancelled()
            attempt += 1
            discovery = self.backend.collect_cluster()
            last_observation = discovery
            leaders = discovery.leader_ips if discovery.membership_complete else ()
            if len(leaders) != 1:
                stable = 0
                stable_leader = ""
                self._event(
                    RemediationStage.FINDING_LEADER,
                    "leader_discovery",
                    "LEADER_NOT_UNIQUE",
                    f"현재 Leader가 정확히 1대로 확인되지 않았습니다: {', '.join(leaders) or '없음'}",
                    endpoint_ip=discovery.source_ip,
                    attempt=attempt,
                )
                self._wait(self.settings.membership_poll_interval_seconds)
                continue
            leader = leaders[0]
            leader_view = self.backend.collect_cluster(leader)
            last_observation = leader_view
            target = leader_view.members.get(self.candidate.target_ip)
            valid = bool(
                leader_view.membership_complete
                and leader_view.source_ip == leader
                and leader_view.leader_ips == (leader,)
                and target is not None
                and target.is_connected
                and leader_view.all_expected_connected(self.candidate.expected_member_ips)
            )
            if valid and stable_leader == leader:
                stable += 1
            elif valid:
                stable_leader = leader
                stable = 1
            else:
                stable = 0
                stable_leader = ""
            self._event(
                RemediationStage.VERIFYING_REJOIN,
                "membership_rejoin_poll",
                "CONNECTED" if valid else (leader_view.error_code or "NOT_READY"),
                (
                    f"현재 Leader {leader}에서 대상 Controller CONNECTED 상태를 확인했습니다 ({stable}/{self.settings.membership_confirmations})."
                    if valid
                    else "현재 Leader에서 전체 구성원 및 대상 Controller 재가입 상태를 대기 중입니다."
                ),
                endpoint_ip=leader,
                attempt=attempt,
                evidence={"leaders": list(leader_view.leader_ips), "stable": stable},
            )
            if valid and stable >= self.settings.membership_confirmations:
                self._snapshot("rejoin_confirmed", cluster_snapshot(leader_view))
                return leader, leader_view
            self._wait(self.settings.membership_poll_interval_seconds)
        if last_observation is not None:
            self._snapshot("rejoin_timeout", cluster_snapshot(last_observation))
        raise _WorkflowAbort(
            "MEMBERSHIP_REJOIN_TIMEOUT",
            "제한시간 내 현재 Leader에서 대상 Controller CONNECTED 상태를 연속 확인하지 못했습니다.",
        )

    def _connect_leader(self, leader_ip: str):
        last_error: SshOperationError | None = None
        for attempt in range(1, self.settings.ssh_max_attempts + 1):
            self._check_cancelled()
            started = monotonic()
            try:
                adapter = self.backend.open_action_session(leader_ip)
            except SshOperationError as exc:
                last_error = exc
                self._event(
                    RemediationStage.LEADER_SSH,
                    "leader_ssh_connect",
                    exc.code,
                    exc.user_message,
                    endpoint_ip=leader_ip,
                    attempt=attempt,
                    duration_ms=int((monotonic() - started) * 1000),
                )
                if not exc.retryable:
                    raise _WorkflowAbort(exc.code, exc.user_message)
                if attempt < self.settings.ssh_max_attempts:
                    self._wait(self.settings.ssh_retry_interval_seconds)
                continue
            except Exception:
                self._event(
                    RemediationStage.LEADER_SSH,
                    "leader_ssh_connect",
                    "SSH_CONNECT_FAILED",
                    "Leader Controller SSH 접속에 실패했습니다.",
                    endpoint_ip=leader_ip,
                    attempt=attempt,
                    duration_ms=int((monotonic() - started) * 1000),
                )
                if attempt < self.settings.ssh_max_attempts:
                    self._wait(self.settings.ssh_retry_interval_seconds)
                continue
            self._event(
                RemediationStage.LEADER_SSH,
                "leader_ssh_connect",
                "CONNECTED",
                "현재 Leader Controller SSH 접속에 성공했습니다.",
                endpoint_ip=leader_ip,
                attempt=attempt,
                duration_ms=int((monotonic() - started) * 1000),
            )
            return adapter
        message = last_error.user_message if last_error else "Leader Controller SSH 접속에 실패했습니다."
        raise _WorkflowAbort("LEADER_SSH_ATTEMPTS_EXHAUSTED", message)

    def _post_monitor(self) -> tuple[bool, MmObservation | None, ClusterObservation | None]:
        deadline = monotonic() + self.settings.post_timeout_seconds
        stable = 0
        stable_leader = ""
        mm_last: MmObservation | None = None
        cluster_last: ClusterObservation | None = None
        attempt = 0
        while monotonic() < deadline:
            self._check_cancelled()
            attempt += 1
            mm_last = self.backend.collect_mm()
            cluster_last = self.backend.collect_cluster()
            expected = self.candidate.expected_member_ips
            leaders = cluster_last.leader_ips if cluster_last.membership_complete else ()
            all_up = bool(mm_last.complete and all(mm_last.state_for(ip) == "up" for ip in expected))
            valid = bool(
                all_up
                and cluster_last.membership_complete
                and cluster_last.distribution_complete
                and len(leaders) == 1
                and cluster_last.all_expected_connected(expected)
                and cluster_last.all_expected_have_distribution(expected)
            )
            leader = leaders[0] if len(leaders) == 1 else ""
            if valid and leader == stable_leader:
                stable += 1
            elif valid:
                stable_leader = leader
                stable = 1
            else:
                stable = 0
                stable_leader = ""
            self._event(
                RemediationStage.POST_MONITORING,
                "post_state_poll",
                "NORMAL" if valid else "NOT_STABLE",
                (
                    f"사후 상태가 정상 조건을 충족했습니다 ({stable}/{self.settings.post_confirmations})."
                    if valid
                    else "전체 MM Up, Membership Connected 및 Client 분배 행 정상화를 확인 중입니다."
                ),
                endpoint_ip=cluster_last.source_ip,
                attempt=attempt,
                evidence={"all_up": all_up, "leaders": list(leaders), "stable": stable},
            )
            if valid and stable >= self.settings.post_confirmations:
                self.run.leader_ip = leader
                self.repository.update_run(self.run)
                return True, mm_last, cluster_last
            self._wait(self.settings.post_poll_interval_seconds)
        return False, mm_last, cluster_last

    def _finalize(
        self,
        outcome: RemediationOutcome,
        stage: RemediationStage,
        failure_code: str,
        summary: str,
        *,
        release_target: bool,
    ) -> WorkflowResult:
        self.run.outcome = outcome
        self.run.stage = stage
        self.run.failure_code = failure_code
        self.run.summary = summary
        self.run.ended_at = self.now()
        try:
            self._event(
                stage,
                "workflow_finished",
                failure_code or outcome.value.upper(),
                summary,
                endpoint_ip=self.run.target_ip,
            )
        except Exception:
            # Preserve final run state even if the append-only event insert is
            # unavailable; report generation will still include prior evidence.
            pass
        self.repository.update_run(self.run)
        report_path = ""
        try:
            path = write_report(
                self.run,
                self.reports_dir,
                timezone_name=self.settings.report_timezone,
            )
            report_path = str(path)
            self.run.report_path = report_path
            self.repository.update_run(self.run)
        except Exception:
            report_path = ""
        if release_target:
            self.repository.release_target(self.run.target_ip)
        return WorkflowResult(
            run_id=self.run.run_id,
            outcome=outcome,
            stage=stage,
            target_ip=self.run.target_ip,
            message=summary,
            report_path=report_path,
            leader_ip=self.run.leader_ip,
        )
