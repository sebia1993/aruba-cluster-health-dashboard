"""Escaped, snapshot-owned scenario narration; never derive new health findings."""

from html import escape


STATUS = {
    "normal": "정상",
    "warning": "주의",
    "critical": "장애",
    "unknown": "확인 불가",
}


def value(item):
    return "확인 불가" if item is None else str(item)


def observation(snap, previous=None):
    if snap.health.severity.value == "unknown":
        return "장비 상태를 판단하는 데 필요한 CLI 정보를 가져오지 못했습니다. 값이 없다는 것은 장비가 꺼졌다는 뜻이 아닙니다."
    if snap.pending:
        return " / ".join(
            f"{p.member_ip}의 연결 유형: {p.previous_value} → {p.current_value}"
            for p in snap.pending
        )
    before = {d.ip: d for d in previous.health.devices} if previous else {}
    changes = []
    for d in snap.health.devices:
        old = before.get(d.ip)
        if old and (old.active_clients, old.standby_clients) != (
            d.active_clients,
            d.standby_clients,
        ):
            changes.append(
                f"{d.display_name}: 연결 단말 {value(old.active_clients)} → {value(d.active_clients)}, "
                f"대기 단말 {value(old.standby_clients)} → {value(d.standby_clients)}"
            )
    if changes:
        return " / ".join(changes)
    if snap.anomaly_count:
        return "앞선 점검과 같은 단말 분배 이상이 다시 관측되었습니다. 장비의 응답 여부와 단말 분배 상태를 따로 확인합니다."
    if previous:
        return "다시 점검해도 정상 관측값이 유지됩니다. 한 번의 값만 보고 결론 내리지 않습니다."
    return f"관리 장비 {len(snap.health.devices)}대의 응답, 연결 단말 수, 장비 간 연결 유형을 함께 확인합니다."


def snapshot_card(snap, previous=None, *, final=False):
    state = STATUS[snap.health.severity.value]
    title = "최종 결과 · " + state if final else snap.title
    cards = []
    focused = {d["ip"] for d in snap.focus}
    for d in snap.health.devices:
        status = STATUS[d.severity.value]
        if d.ip in focused and status == "정상" and snap.anomaly_count:
            status = "이상 관측 · 확정 대기"
        cards.append(
            '<div class="story-device'
            + (" story-focus" if d.ip in focused else "")
            + '">'
            f"<b>{escape(d.display_name)}</b><span>{escape(status)}</span>"
            f"<small>연결 {value(d.active_clients)} · 대기 {value(d.standby_clients)}"
            f" · {escape(d.connection_type or '확인 불가')}</small></div>"
        )
    explanation = snap.explanation
    for d in snap.health.devices:
        explanation = explanation.replace(f"{d.display_name} ({d.ip})", d.display_name)
    explanation = (
        explanation.replace("Client 분배", "단말 분배")
        .replace("Incident를", "이상 기록을")
        .replace("Incident가", "이상 기록이")
        .replace("Incident", "이상 기록")
        .replace("장비 Down으로", "장비가 꺼졌다고")
        .replace("장비 Down", "장비가 꺼졌음")
        .replace("Collection Failure는 수집 경로의 문제 기록입니다.", "")
    )
    opened = sum(i.active for i in snap.incidents)
    resolved = sum(bool(i.recovered_at) and not i.active for i in snap.incidents)
    return (
        f"<article {'data-summary' if final else 'data-scene'} hidden>"
        f"<h4>{escape(title)}</h4>"
        f"<p><b>관측</b> · {escape(observation(snap, previous))}</p>"
        f"<p><b>판단</b> · {escape(explanation)}</p>"
        '<div class="story-metrics">'
        f"<span>응답 장비 <b>{value(snap.up)} / {len(snap.health.devices)}</b></span>"
        f"<span>연결 단말 <b>{value(snap.active_total)}</b></span>"
        f"<span>진행 중 이상 <b>{opened}</b> · 복구 완료 <b>{resolved}</b></span></div>"
        '<div class="story-devices">' + "".join(cards) + "</div></article>"
    )


def render_story(run):
    """All visible metrics belong to the selected immutable snapshot."""
    body = "".join(
        snapshot_card(snap, run.snapshots[i - 1] if i else None)
        for i, snap in enumerate(run.snapshots)
    )
    if not run.snapshots:
        return (
            body
            + "<article data-summary hidden><h4>확인 가능한 관측 없음</h4><p>완료된 관측이 없습니다. 실행 중단 원인을 확인하세요.</p></article>"
        )
    last = run.snapshots[-1]
    summary = snapshot_card(
        last, run.snapshots[-2] if len(run.snapshots) > 1 else None, final=True
    )
    milestones = []
    for snap in run.snapshots:
        text = snap.title
        if not milestones or milestones[-1] != text:
            milestones.append(text)
    summary = summary.replace(
        '<div class="story-metrics">',
        '<p class="story-recap"><b>이번에 확인한 과정</b><br>'
        + escape(" → ".join(milestones))
        + '</p><div class="story-metrics">',
        1,
    )
    if run.error:
        summary = summary.replace("최종 결과 · ", "중단 전 마지막 관측 · ", 1)
    return body + summary
