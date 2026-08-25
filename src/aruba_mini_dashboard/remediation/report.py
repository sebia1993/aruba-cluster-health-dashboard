from __future__ import annotations

import html
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import RemediationOutcome, RemediationRun


MAX_REPORT_BYTES = 2 * 1024 * 1024


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _local(value: datetime | None, zone_name: str) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone(zone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _clock(value: datetime | None, zone_name: str) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone(zone_name)).strftime("%H:%M:%S")


def _duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}시간 {minutes}분 {secs}초"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _status_meta(outcome: RemediationOutcome) -> tuple[str, str, str]:
    return {
        RemediationOutcome.COMPLETED: ("정상 완료", "success", "자동 복구·재가입·재분배·사후 확인 완료"),
        RemediationOutcome.PARTIAL: ("부분 완료", "warning", "Controller 복구 또는 재분배 일부 증거가 미확인"),
        RemediationOutcome.FAILED: ("실패", "danger", "자동 조치 단계에서 실패 또는 제한시간 초과"),
        RemediationOutcome.STOPPED: ("중단", "danger", "안전조건 또는 사용자 요청에 따라 조치 중단"),
        RemediationOutcome.INTERRUPTED: ("비정상 중단", "danger", "프로그램 종료로 작업 상태가 중단됨"),
        RemediationOutcome.RUNNING: ("진행 중", "warning", "자동 장애조치가 아직 종료되지 않음"),
    }[outcome]


def _escape(value: Any) -> str:
    return html.escape("-" if value is None or value == "" else str(value), quote=True)


def _snapshot_members(snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not snapshot:
        return {}
    members = snapshot.get("members", {})
    return members if isinstance(members, Mapping) else {}


def _device_comparison_rows(run: RemediationRun) -> str:
    before = _snapshot_members(run.snapshots.get("pre_action"))
    after = _snapshot_members(run.snapshots.get("post_action"))
    rows: list[str] = []
    for ip in run.expected_member_ips:
        b = before.get(ip, {}) if isinstance(before.get(ip, {}), Mapping) else {}
        a = after.get(ip, {}) if isinstance(after.get(ip, {}), Mapping) else {}
        rows.append(
            "<tr>"
            f"<td class='mono'>{_escape(ip)}</td>"
            f"<td>{_escape(b.get('mm_status', '-'))}</td>"
            f"<td>{_escape(a.get('mm_status', '-'))}</td>"
            f"<td>{_escape(b.get('status', '-'))}</td>"
            f"<td>{_escape(a.get('status', '-'))}</td>"
            f"<td>{_escape(b.get('active_clients', '-'))} / {_escape(b.get('standby_clients', '-'))}</td>"
            f"<td>{_escape(a.get('active_clients', '-'))} / {_escape(a.get('standby_clients', '-'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<tr><td colspan='7'>비교 가능한 Controller 스냅샷이 없습니다.</td></tr>"
    return "".join(rows)


def _timeline_rows(run: RemediationRun, zone_name: str) -> str:
    rows: list[str] = []
    for event in run.events:
        elapsed = (event.occurred_at - run.started_at).total_seconds()
        attempt = "-" if event.attempt is None else str(event.attempt)
        rows.append(
            "<tr>"
            f"<td>{_escape(_clock(event.occurred_at, zone_name))}</td>"
            f"<td>{_escape(_duration(elapsed))}</td>"
            f"<td>{_escape(event.stage.value)}</td>"
            f"<td class='mono'>{_escape(event.endpoint_ip)}</td>"
            f"<td>{_escape(event.operation)}</td>"
            f"<td>{_escape(attempt)}</td>"
            f"<td><span class='event-code'>{_escape(event.result_code)}</span><br>{_escape(event.message)}</td>"
            "</tr>"
        )
    if not rows:
        return "<tr><td colspan='7'>기록된 타임라인 이벤트가 없습니다.</td></tr>"
    return "".join(rows)


def _command_evidence(run: RemediationRun) -> str:
    reload_events = [event for event in run.events if event.operation == "reload_force"]
    rebalance_events = [event for event in run.events if event.operation == "cluster_rebalance"]
    reload_result = reload_events[-1].result_code if reload_events else "미실행"
    rebalance_result = rebalance_events[-1].result_code if rebalance_events else "미실행"
    confirmed = "확인" if run.rebalance_confirmed else "미확인"
    return f"""
    <div class="command-grid">
      <article class="command-card">
        <div class="command-label">대상 Controller 재부팅</div>
        <code>reload force</code>
        <div class="command-result">결과: {_escape(reload_result)}</div>
      </article>
      <article class="command-card">
        <div class="command-label">Leader Controller 재분배</div>
        <code>cluster-debug bucketmap rebalance</code>
        <div class="command-result">결과: {_escape(rebalance_result)}</div>
      </article>
      <article class="command-card wide">
        <div class="command-label">정상 응답 증거</div>
        <code>Cluster rebalance triggered</code>
        <div class="command-result">정확한 독립 행 확인: {_escape(confirmed)}</div>
      </article>
    </div>
    """


def render_report(run: RemediationRun, *, timezone_name: str = "Asia/Seoul") -> str:
    result_label, result_class, result_description = _status_meta(run.outcome)
    ended = run.ended_at or datetime.now(timezone.utc)
    summary = run.summary or result_description
    report_id = run.run_id
    generated_at = _local(datetime.now(timezone.utc), timezone_name)
    target_display = run.target_alias or run.target_ip

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aruba WLC 자동 장애조치 보고서 - {_escape(report_id)}</title>
<style>
:root {{ --navy:#102A43; --navy2:#243B53; --blue:#2F80ED; --line:#D9E2EC; --muted:#627D98; --bg:#F4F7FA; --success:#147D64; --warning:#B7791F; --danger:#B83232; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:#243B53; font-family:"Segoe UI","Noto Sans KR","Malgun Gothic",sans-serif; line-height:1.55; }}
.report {{ width:min(1160px,calc(100% - 32px)); margin:24px auto 48px; }}
.hero {{ background:linear-gradient(135deg,var(--navy),var(--navy2)); color:white; padding:30px 34px; border-radius:16px 16px 0 0; box-shadow:0 12px 28px rgba(16,42,67,.15); }}
.eyebrow {{ font-size:12px; font-weight:700; letter-spacing:.12em; opacity:.72; }}
h1 {{ margin:8px 0 5px; font-size:30px; line-height:1.25; }}
.subtitle {{ margin:0; opacity:.82; }}
.meta-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:18px; font-size:13px; }}
.sheet {{ background:#fff; border:1px solid var(--line); border-top:0; padding:30px 34px; box-shadow:0 12px 28px rgba(16,42,67,.08); }}
.section {{ margin-top:30px; page-break-inside:avoid; }}
.section:first-child {{ margin-top:0; }}
h2 {{ margin:0 0 14px; color:var(--navy); font-size:20px; border-left:4px solid var(--blue); padding-left:10px; }}
.result-strip {{ display:grid; grid-template-columns:1.2fr 1fr 1fr 1fr; gap:12px; }}
.kpi {{ border:1px solid var(--line); border-radius:12px; padding:16px; background:#FBFCFE; min-height:105px; }}
.kpi-label {{ color:var(--muted); font-size:12px; font-weight:700; letter-spacing:.04em; }}
.kpi-value {{ margin-top:7px; font-size:20px; font-weight:800; color:var(--navy); word-break:break-word; }}
.badge {{ display:inline-flex; align-items:center; border-radius:999px; padding:7px 12px; color:#fff; font-weight:800; font-size:14px; }}
.badge.success {{ background:var(--success); }} .badge.warning {{ background:var(--warning); }} .badge.danger {{ background:var(--danger); }}
.summary {{ border:1px solid #B8D5F2; border-radius:12px; background:#EFF7FF; padding:18px 20px; }}
.summary strong {{ color:var(--navy); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:#EEF3F8; color:var(--navy); text-align:left; font-weight:800; padding:10px 9px; border:1px solid var(--line); }}
td {{ padding:9px; border:1px solid var(--line); vertical-align:top; }}
tbody tr:nth-child(even) {{ background:#FAFCFE; }}
.mono, code {{ font-family:Consolas,"SFMono-Regular",monospace; }}
.event-code {{ font-family:Consolas,monospace; color:#2C5282; font-size:12px; font-weight:700; }}
.command-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.command-card {{ border:1px solid var(--line); border-radius:12px; padding:16px; background:#FAFCFE; }}
.command-card.wide {{ grid-column:1 / -1; }}
.command-label {{ color:var(--muted); font-size:12px; font-weight:800; margin-bottom:8px; }}
.command-card code {{ display:block; border-radius:8px; background:#102A43; color:#E6F1FF; padding:12px; overflow-wrap:anywhere; }}
.command-result {{ margin-top:9px; font-size:13px; }}
.two-column {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.note {{ border:1px solid var(--line); border-radius:12px; padding:16px 18px; background:#FAFCFE; }}
.note h3 {{ margin:0 0 8px; color:var(--navy); font-size:15px; }}
.note ul {{ margin:0; padding-left:19px; }}
.footer {{ background:#EAF0F5; border:1px solid var(--line); border-top:0; border-radius:0 0 16px 16px; padding:15px 34px; color:var(--muted); font-size:12px; display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; }}
@media (max-width:800px) {{ .result-strip,.two-column,.command-grid {{ grid-template-columns:1fr; }} .command-card.wide {{ grid-column:auto; }} .hero,.sheet {{ padding:22px 20px; }} .report {{ width:min(100% - 16px,1160px); margin-top:8px; }} }}
@media print {{ @page {{ size:A4 landscape; margin:11mm; }} body {{ background:#fff; }} .report {{ width:100%; margin:0; }} .hero,.sheet,.footer {{ box-shadow:none; }} .section {{ break-inside:avoid; }} a {{ color:inherit; text-decoration:none; }} }}
</style>
</head>
<body>
<main class="report">
<header class="hero">
  <div class="eyebrow">NETWORK OPERATIONS · AUTOMATED REMEDIATION</div>
  <h1>Aruba WLC 자동 장애조치 보고서</h1>
  <p class="subtitle">MM Down Controller 재부팅, Cluster 재가입 확인 및 Leader 기반 재분배 결과</p>
  <div class="meta-row"><span>보고서 ID: {_escape(report_id)}</span><span>생성: {_escape(generated_at)}</span><span>프로그램: {_escape(run.app_version or '-')}</span></div>
</header>
<section class="sheet">
  <section class="section">
    <h2>1. 상급보고 요약</h2>
    <div class="result-strip">
      <div class="kpi"><div class="kpi-label">최종 결과</div><div class="kpi-value"><span class="badge {result_class}">{_escape(result_label)}</span></div></div>
      <div class="kpi"><div class="kpi-label">대상 Controller</div><div class="kpi-value">{_escape(target_display)}<br><span class="mono">{_escape(run.target_ip)}</span></div></div>
      <div class="kpi"><div class="kpi-label">현재 Leader</div><div class="kpi-value mono">{_escape(run.leader_ip or '미확인')}</div></div>
      <div class="kpi"><div class="kpi-label">전체 조치시간</div><div class="kpi-value">{_escape(_duration((ended-run.started_at).total_seconds()))}</div></div>
    </div>
    <div class="summary" style="margin-top:14px"><strong>상황 요약</strong><br>{_escape(summary)}</div>
  </section>

  <section class="section">
    <h2>2. 핵심 시각</h2>
    <table><tbody>
      <tr><th>장애 최초 감지</th><td>{_escape(_local(run.started_at, timezone_name))}</td><th>조치 종료</th><td>{_escape(_local(run.ended_at, timezone_name))}</td></tr>
      <tr><th>재부팅 명령 전송</th><td>{_escape('예' if run.reload_sent else '아니오/미확인')}</td><th>재분배 명령 전송</th><td>{_escape('예' if run.rebalance_sent else '아니오/미확인')}</td></tr>
      <tr><th>재분배 정상 출력</th><td>{_escape('Cluster rebalance triggered 확인' if run.rebalance_confirmed else '미확인')}</td><th>종료 코드</th><td>{_escape(run.failure_code or run.outcome.value)}</td></tr>
    </tbody></table>
  </section>

  <section class="section">
    <h2>3. 장애조치 타임라인</h2>
    <table>
      <thead><tr><th>시각</th><th>누적 경과</th><th>단계</th><th>대상</th><th>수행 내용</th><th>시도</th><th>결과 및 근거</th></tr></thead>
      <tbody>{_timeline_rows(run, timezone_name)}</tbody>
    </table>
  </section>

  <section class="section">
    <h2>4. Controller 상태 전·후 비교</h2>
    <table>
      <thead><tr><th>Controller IP</th><th>조치 전 MM</th><th>조치 후 MM</th><th>조치 전 Membership</th><th>조치 후 Membership</th><th>조치 전 Active/Standby</th><th>조치 후 Active/Standby</th></tr></thead>
      <tbody>{_device_comparison_rows(run)}</tbody>
    </table>
  </section>

  <section class="section">
    <h2>5. 실행 명령 및 정형 증거</h2>
    {_command_evidence(run)}
  </section>

  <section class="section">
    <h2>6. 확인 범위와 후속 권고</h2>
    <div class="two-column">
      <div class="note"><h3>자동으로 확인한 범위</h3><ul><li>MM의 Controller Up/Down 상태</li><li>Cluster Membership과 현재 Leader</li><li>대상 Controller의 CONNECTED 재가입 상태</li><li>Active/Standby Client 분배 행 존재</li><li>재분배 명령의 정형 정상 응답</li></ul></div>
      <div class="note"><h3>자동으로 확정하지 않은 범위</h3><ul><li>장애의 물리적·소프트웨어적 근본 원인</li><li>사용자 체감 영향 범위와 서비스 중단시간</li><li>AP/RF, ClearPass/RADIUS, 상위 회선·방화벽 상태</li><li>Crash dump 및 시스템 로그의 상세 원인 분석</li></ul></div>
    </div>
    <div class="note" style="margin-top:14px"><h3>후속 권고</h3><ul><li>대상 Controller의 시스템 로그, Crash 정보와 재부팅 원인을 별도 확인합니다.</li><li>동일 장비 및 동일 증상의 재발 여부를 추적합니다.</li><li>반복 발생 시 하드웨어 상태, ArubaOS 버전과 Cluster 관련 알려진 이슈를 점검합니다.</li></ul></div>
  </section>
</section>
<footer class="footer"><span>본 보고서는 자동 수집된 정형 상태와 명령 결과를 기반으로 생성되었습니다.</span><span>Report ID {_escape(report_id)}</span></footer>
</main>
</body>
</html>
"""


def write_report(
    run: RemediationRun,
    reports_dir: str | os.PathLike[str],
    *,
    timezone_name: str = "Asia/Seoul",
) -> Path:
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.astimezone(_zone(timezone_name)).strftime("%Y%m%d_%H%M%S")
    safe_alias = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (run.target_alias or run.target_ip))
    filename = f"WLC_장애조치보고서_{stamp}_{safe_alias}_{run.run_id}.html"
    destination = directory / filename
    encoded = render_report(run, timezone_name=timezone_name).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise RuntimeError("장애조치 보고서가 안전한 최대 크기를 초과했습니다.")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".report.", suffix=".tmp", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
