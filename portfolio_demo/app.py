from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime, TOPOLOGY

from portfolio_demo.execution_trace import render_trace
from portfolio_demo.guided_flow import GuidedSlot, begin
try:
    from portfolio_demo.scenario_runner import (
        SCENARIO_DESCRIPTIONS,
        SCENARIOS,
        ScenarioRunner,
    )
except ImportError:
    from portfolio_demo.scenario_runner import SCENARIOS, ScenarioRunner

    SCENARIO_DESCRIPTIONS = {
        "incident_recovery": (
            "특정 장비의 연결 단말 수가 급감한 상태를 반복 관측하고, "
            "문제 확정 후 정상 상태가 연속 확인되면 복구 처리하는 흐름입니다."
        ),
        "normal": (
            "모든 장비가 정상적으로 응답하고 연결 단말 수도 정상인 상태가 반복되는 흐름입니다."
        ),
        "collection_failure": (
            "장비 상태 정보를 가져오지 못했을 때 이를 장비 고장으로 단정하지 않고 "
            "'확인 불가'로 처리하는 흐름입니다."
        ),
        "connection_change": (
            "장비 간 연결 상태가 기존 정상 기준과 달라졌을 때 자동으로 정상 처리하지 않고 "
            "확인 대상으로 남기는 흐름입니다."
        ),
    }
from portfolio_demo.story_view import render_story

st.set_page_config(
    page_title="Aruba 네트워크 상태 미니보드 · Public Web Edition",
    page_icon="📡",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {
        max-width: 1540px;
        padding-top: 1.15rem;
        padding-bottom: 3rem;
    }
    .desktop-shell {
        border: 1px solid rgba(125, 145, 170, .24);
        border-radius: 14px;
        background: rgba(15, 23, 35, .56);
        padding: 14px 16px;
        margin-bottom: .75rem;
    }
    .window-title {
        font-size: 1.45rem;
        font-weight: 800;
        margin: 0;
    }
    .window-meta {
        color: #8797aa;
        font-size: .82rem;
        margin-top: .2rem;
    }
    .status-hero {
        border: 1px solid rgba(125, 145, 170, .24);
        border-radius: 12px;
        padding: 14px 16px;
        background: rgba(19, 29, 44, .66);
        margin: .55rem 0 .75rem;
    }
    .status-value {
        font-size: 1.75rem;
        line-height: 1.05;
        font-weight: 850;
    }
    .status-line {
        color: #9aabbe;
        font-size: .88rem;
        margin-top: .35rem;
    }
    .controller-card {
        border: 1px solid rgba(125, 145, 170, .22);
        border-radius: 10px;
        padding: 10px 12px;
        min-height: 92px;
        background: rgba(17, 26, 39, .55);
    }
    .controller-name {
        font-weight: 800;
        font-size: .92rem;
    }
    .controller-meta {
        color: #8fa0b4;
        font-size: .78rem;
        line-height: 1.5;
        margin-top: .25rem;
    }
    .demo-pill {
        display: inline-block;
        border: 1px solid #38506d;
        border-radius: 999px;
        padding: .18rem .55rem;
        margin-right: .3rem;
        color: #afc8ee;
        font-size: .68rem;
        font-weight: 800;
    }
    .small-help {
        color: #8392a4;
        font-size: .78rem;
    }
    .scenario-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem;}
    .scenario-card {border:1px solid #8885;border-radius:12px;padding:1rem;overflow-wrap:anywhere;}
    .scenario-current {border:2px solid #62b9ff;background:#62b9ff12;}
    .scenario-card h4 {margin:0 0 .6rem;font-size:1.05rem;}
    .scenario-card p {margin:.45rem 0;font-size:.9rem;}
    @media(max-width:700px) {.scenario-grid {grid-template-columns:1fr;}}
    [data-testid="stMetricValue"] {white-space: normal; overflow-wrap: anywhere; font-size: clamp(1rem, 2.2vw, 2rem);}
    </style>
    """,
    unsafe_allow_html=True,
)

if "runtime" not in st.session_state:
    st.session_state.runtime = DemoRuntime()
if "cluster_show_settings" not in st.session_state:
    st.session_state.cluster_show_settings = False
if "cluster_compact" not in st.session_state:
    st.session_state.cluster_compact = False

r = st.session_state.runtime


def ko_status(value: str) -> str:
    return {
        "normal": "정상",
        "warning": "주의",
        "critical": "장애",
        "unknown": "확인 불가",
    }.get(value, value)


def client_value(value) -> str:
    return "확인 불가" if value is None else str(value)


def distribution_status(device) -> str:
    if device.active_clients is None:
        return "확인 불가"
    if device.severity.value == "normal":
        return "정상"
    if device.severity.value == "unknown":
        return "확인 불가"
    return "확인 필요"


def render_window_header() -> None:
    left, right = st.columns([4, 2])
    with left:
        st.markdown(
            '<div class="desktop-shell">'
            '<div class="window-title">Aruba Cluster Health Dashboard</div>'
            '<div class="window-meta">여러 무선 장비의 상태를 반복 확인해, '
            '순간적인 변화와 실제로 지속되는 문제를 구분하고 복구까지 추적합니다.</div>'
            "</div>",
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<div style="padding-top:.65rem;text-align:right">'
            '<span class="demo-pill">PUBLIC DEMO</span>'
            '<span class="demo-pill">비식별 샘플 데이터</span>'
            '<span class="demo-pill">읽기 전용</span>'
            "</div>",
            unsafe_allow_html=True,
        )


def render_reviewer_summary() -> None:
    st.markdown("### 이 프로젝트는 무엇을 해결하나요?")
    st.write(
        "기업 Wi-Fi 환경에서는 장비 하나의 숫자만 보고 문제 여부를 판단하기 어렵습니다. "
        "장비 응답 상태, 연결된 단말 수, 장비 간 연결 상태를 사람이 따로 확인해야 합니다. "
        "이 프로젝트는 여러 정보를 반복 관측해 **순간적인 변화인지, 실제 문제가 지속되는지, "
        "단순히 정보를 가져오지 못한 것인지** 자동으로 구분합니다."
    )
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("**프로젝트 목적**")
        st.write(
            "운영자가 여러 장비 화면과 명령 결과를 하나씩 대조하지 않아도 "
            "현재 확인이 필요한 문제와 그 판단 근거를 한 화면에 정리합니다."
        )
        st.caption(
            "쉽게 말해: 여러 장비를 계속 지켜보다가 '지금 정말 확인이 필요한 문제가 생겼는지'를 "
            "대신 판단합니다."
        )
    with right, st.container(border=True):
        st.markdown("**이 데모에서 보여주는 것**")
        st.write(
            "**정상 → 이상 징후 발견 → 같은 이상 반복 → 문제 확정 → 정상 상태 반복 확인 → 복구** "
            "흐름을 사용자 추가 조작 없이 자동으로 보여줍니다."
        )
        st.caption(
            "정보 수집에 실패한 경우에는 장비가 고장났다고 추정하지 않고 '확인 불가'로 남깁니다."
        )


def render_status_card() -> None:
    if r.health is None:
        status = "확인 불가"
        problem = "주요 확인 대상: 점검 전"
        reason = "점검을 실행하면 판단 근거가 표시됩니다."
    else:
        status = ko_status(r.health.severity.value)
        problem_devices = [d for d in r.health.devices if d.severity.value != "normal"]
        if problem_devices:
            first = problem_devices[0]
            problem = f"주요 확인 대상: {first.display_name} · {first.ip}"
        else:
            problem = "주요 확인 대상: 없음"
        reason = r.health.summary or "현재 관측에서 별도 이상 신호가 없습니다."

    st.markdown(
        '<div class="status-hero">'
        f'<div class="status-value">{status}</div>'
        f'<div class="status-line">{problem}</div>'
        f'<div class="status-line">판단 근거: {reason}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def render_overview() -> None:
    if st.session_state.cluster_compact:
        return

    if r.health is None:
        overall = "확인 불가"
        controller_up = f"- / {len(TOPOLOGY)}"
        active_total = "-"
        incident_count = 0
    else:
        overall = ko_status(r.health.severity.value)
        unknown = sum(d.mm_status is None for d in r.health.devices)
        up = sum((d.mm_status or "").lower() == "up" for d in r.health.devices)
        controller_up = (
            f"확인 불가 {unknown} / {len(r.health.devices)}"
            if unknown
            else f"{up} / {len(r.health.devices)}"
        )
        known = [
            d.active_clients for d in r.health.devices if d.active_clients is not None
        ]
        active_total = (
            sum(known) if len(known) == len(r.health.devices) else "확인 불가"
        )
        incident_count = len(r.incidents.active_incidents())

    cols = st.columns(4)
    cols[0].metric("전체 상태", overall)
    cols[1].metric("정상 응답 장비", controller_up)
    cols[2].metric("현재 연결 단말 수", active_total)
    cols[3].metric("현재 확인이 필요한 이상", incident_count)

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### 등록 무선 장비")
        devices = r.health.devices if r.health else []
        if devices:
            cards = st.columns(2)
            for index, device in enumerate(devices[:8]):
                with cards[index % 2]:
                    st.markdown(
                        '<div class="controller-card">'
                        f'<div class="controller-name">{device.display_name}</div>'
                        f'<div class="controller-meta">{device.ip}<br>'
                        f"{ko_status(device.severity.value)} · 연결 단말 {client_value(device.active_clients)} · "
                        f"대기 단말 {client_value(device.standby_clients)}<br>"
                        f"장비 간 연결 상태 {device.connection_type or '확인 불가'}</div>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
        else:
            st.caption(
                "점검 전입니다. 등록된 Demo Controller 4대를 대상으로 상태를 수집합니다."
            )

    with right:
        st.markdown("#### 최근 이벤트")
        if r.history:
            st.dataframe(
                list(reversed(r.history[-5:])),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("아직 이벤트가 없습니다.")


def render_time_row() -> None:
    c = st.columns(2)
    if r.poll_count:
        c[0].caption(f"마지막 점검: Poll {r.poll_count} · {r.stage}")
    else:
        c[0].caption("마지막 점검: -")
    c[1].caption("다음 점검: Demo 재생 대기" if r.running else "다음 점검: 일시정지")


def selected_ip() -> str | None:
    if not r.health:
        return None
    current = st.session_state.get("cluster_selected_ip")
    valid = [d.ip for d in r.health.devices]
    return current if current in valid else valid[0]


def run_one_poll(*, failure: bool = False) -> None:
    st.session_state.pop("scenario_runner", None)
    try:
        r.poll(failure)
    except ValueError as exc:
        st.warning(str(exc))


def render_controls() -> None:
    c = st.columns(6)
    if c[0].button("지금 점검", type="primary", use_container_width=True):
        run_one_poll()
        st.rerun()
    if c[1].button("자동 시작", disabled=r.running, use_container_width=True):
        r.running = True
        run_one_poll()
        st.rerun()
    if c[2].button("일시정지", disabled=not r.running, use_container_width=True):
        r.running = False
        st.rerun()

    ip = selected_ip()
    if c[3].button(
        "알림 확인",
        disabled=ip is None,
        use_container_width=True,
    ):
        st.session_state.pop("scenario_runner", None)
        r.acknowledge(ip)
        st.rerun()

    if c[4].button("설정", use_container_width=True):
        st.session_state.cluster_show_settings = (
            not st.session_state.cluster_show_settings
        )
        st.rerun()
    if c[5].button("화면", use_container_width=True):
        st.session_state.cluster_compact = not st.session_state.cluster_compact
        st.rerun()

    if r.running:
        with st.expander("Public Demo 자동 점검 재생", expanded=True):
            cc = st.columns([1, 1, 4])
            if cc[0].button("다음 Poll", use_container_width=True):
                run_one_poll()
                st.rerun()
            inject = cc[1].checkbox("CLI Timeout")
            cc[2].caption(
                "Desktop App에서는 timer가 자동 Poll을 수행합니다. "
                "공개 웹 데모에서는 외부 연결 없이 다음 Poll을 수동 재생합니다."
            )
            if inject and cc[1].button("Timeout Poll 실행"):
                run_one_poll(failure=True)
                st.rerun()


def render_settings() -> None:
    if not st.session_state.cluster_show_settings:
        return
    with st.container(border=True):
        st.subheader("설정")
        st.caption(
            "Desktop App의 설정 창을 Web Edition에서 읽기 전용 Demo 설정으로 표시합니다."
        )
        a, b = st.columns(2)
        a.text_input("MM", value="DEMO-MM · 192.0.2.1", disabled=True)
        b.text_input("점검 주기", value="5초 상당 Demo Poll", disabled=True)
        st.dataframe(
            [
                {
                    "사용": True,
                    "장비명": alias,
                    "IP": ip,
                    "역할": "Primary" if n == 0 else f"Fallback {n}",
                }
                for n, (ip, alias) in enumerate(TOPOLOGY.items())
            ],
            hide_index=True,
            width="stretch",
        )
        c = st.columns(3)
        c[0].metric(
            "이상 확정", f"{r.engine.detector.settings.anomaly_confirmations}회"
        )
        c[1].metric(
            "복구 확정", f"{r.engine.detector.settings.recovery_confirmations}회"
        )
        c[2].metric("실제 SSH", "비활성")


def render_device_table() -> str | None:
    st.markdown("#### 장비 검색 및 필터")
    f = st.columns([2.2, 1, 1, 1])
    search = f[0].text_input(
        "장비 검색",
        key="장비 검색",
        placeholder="IP, alias, hostname 검색",
        label_visibility="collapsed",
    )
    status_filter = f[1].selectbox(
        "상태 필터",
        ["전체 상태", "정상", "주의", "장애", "확인 불가"],
        key="상태 필터",
        label_visibility="collapsed",
    )
    problem_only = f[2].checkbox("문제만 보기", key="문제만 보기")
    monitoring_only = f[3].checkbox("감시 대상만", value=True)

    rows = []
    if r.health:
        for device in r.health.devices:
            row = {
                "IP": device.ip,
                "장비명": device.display_name,
                "관리 시스템 보고 상태": device.mm_status or "확인 불가",
                "연결 단말": client_value(device.active_clients),
                "대기 단말": client_value(device.standby_clients),
                "장비 간 연결 상태": device.connection_type or "확인 불가",
                "종합 상태": ko_status(device.severity.value),
                "마지막 확인": f"Poll {r.poll_count}",
                "감시 범위": "감시 중",
                "단말 분배 상태": distribution_status(device),
            }
            if (
                search
                and search.casefold() not in (row["IP"] + row["장비명"]).casefold()
            ):
                continue
            if status_filter != "전체 상태" and row["종합 상태"] != status_filter:
                continue
            if problem_only and device.severity.value == "normal":
                continue
            if monitoring_only and device.ip not in TOPOLOGY:
                continue
            rows.append(row)
    else:
        rows = [
            {
                "IP": ip,
                "장비명": alias,
                "관리 시스템 보고 상태": "점검 전",
                "연결 단말": "-",
                "대기 단말": "-",
                "장비 간 연결 상태": "-",
                "종합 상태": "확인 불가",
                "마지막 확인": "-",
                "감시 범위": "감시 중",
                "단말 분배 상태": "점검 전",
            }
            for ip, alias in TOPOLOGY.items()
        ]

    st.dataframe(rows, hide_index=True, width="stretch")

    if not r.health:
        return None

    options = [d.ip for d in r.health.devices]
    current = selected_ip()
    index = options.index(current) if current in options else 0
    choice = st.selectbox(
        "선택 Controller",
        options,
        index=index,
        format_func=lambda ip: f"{r.health.device_by_ip(ip).display_name} · {ip}",
    )
    st.session_state.cluster_selected_ip = choice
    return choice


def render_detail(ip: str | None) -> None:
    if not ip or not r.health:
        st.caption("장비를 선택하면 상세 정보를 표시합니다.")
        return

    device = r.health.device_by_ip(ip)
    st.markdown("#### 장비 상세 정보")
    c = st.columns(2)
    c[0].write(f"**장비명**  {device.display_name}")
    c[0].write(f"**IP**  {device.ip}")
    c[0].write(f"**관리 시스템 보고 상태**  {device.mm_status or '확인 불가'}")
    c[0].write(f"**종합 상태**  {ko_status(device.severity.value)}")
    c[1].write(f"**연결 단말 수**  {client_value(device.active_clients)}")
    c[1].write(f"**대기 단말 수**  {client_value(device.standby_clients)}")
    c[1].write(f"**장비 간 연결 상태 (Connection-Type)**  {device.connection_type or '확인 불가'}")
    reasons = " / ".join(device.issue_reasons) or "별도 이상 근거 없음"
    st.info(f"판단 근거: {reasons}")

    pending = [
        item for item in r.engine.pending_connection_changes() if item.member_ip == ip
    ]
    if pending:
        st.warning("장비 간 연결 상태 변화가 확인되어 정상 기준 수용을 기다리고 있습니다.")


def render_technical_detail(ip: str | None) -> None:
    if not ip or not r.health:
        return
    device = r.health.device_by_ip(ip)
    parsed, raw = st.tabs(["파싱 결과", "원본 출력"])
    with parsed:
        st.json(asdict(device))

    with raw:
        if not r.raw:
            st.caption("현재 실행에 원본 출력이 없습니다.")
        for command, output in r.raw.items():
            with st.expander(command, expanded=False):
                st.code(output, language="text")


def render_demo_shortcut() -> None:
    with st.expander("Public Demo 안내", expanded=False):
        st.write(
            "실제 Desktop App과 같은 화면/판단 흐름을 사용하되 실제 SSH 대신 "
            "비식별 합성 CLI만 공급합니다."
        )
        c = st.columns(2)
        if c[0].button("대표 장애 상태까지 자동 재생", use_container_width=True):
            st.session_state.pop("scenario_runner", None)
            demo = DemoRuntime()
            demo.execution.on_change = lambda: render_trace(demo.execution, trace_slot)
            for _ in range(6):
                demo.poll()
            st.session_state.runtime = demo
            st.rerun()
        if c[1].button("Demo Reset", use_container_width=True):
            st.session_state.pop("scenario_runner", None)
            st.session_state.runtime = DemoRuntime()
            st.session_state.cluster_selected_ip = None
            st.rerun()


def render_scenario_timeline(runner, slot):
    if runner is None or runner.run is None:
        slot.empty()
        return
    slot.markdown(render_story(runner.run), unsafe_allow_html=True)


def start_scenario(key):
    global r
    begin()
    runner = ScenarioRunner()
    st.session_state.scenario_runner = runner
    st.session_state.cluster_selected_ip = None
    st.session_state.cluster_compact = False
    # Reset prior manual filters so a new story and its dashboard agree.
    for widget_key in ("장비 검색", "상태 필터", "문제만 보기"):
        st.session_state.pop(widget_key, None)

    def update(current):
        # Do not replay partial records while the analysis is still running.
        if not current.run.snapshots or current.run.completed or current.run.error:
            render_scenario_timeline(current, timeline_slot)

    try:
        runner.play(key, update)
    except Exception as exc:
        st.error(f"시나리오를 완료하지 못했습니다: {exc}")
    # play resets the runtime; retain the exact instance that produced snapshots.
    r = runner.runtime
    st.session_state.runtime = r
    st.session_state.trace_poll = (
        len(runner.run.snapshots) - 1 if runner.run and runner.run.snapshots else 0
    )


def render_incident_history():
    st.subheader("이상 기록 / History")
    incidents, history, evidence = st.tabs(
        ["이상 기록 (Incident)", "점검 이력", "기술 근거"]
    )
    with incidents:
        rows = [
            {
                "종류": i.incident_type.value,
                "Controller": i.alias or i.ip or "수집 경로",
                "상태": "확인됨"
                if i.active and i.acknowledged
                else "확인 필요"
                if i.active
                else "복구 완료"
                if i.recovered_at
                else "종료",
                "최초 관측": str(i.first_detected_at),
                "복구 시각": str(i.recovered_at or "-"),
                "근거": i.reason,
            }
            for i in r.incidents.events()
        ]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("현재 생성된 이상 기록이 없습니다.")
    with history:
        st.dataframe(r.history, hide_index=True, width="stretch")
    with evidence:
        st.json(
            {
                "detector_settings": asdict(r.engine.detector.settings),
                "detector_state": r.engine.detector.dump_state(),
                "transitions": [asdict(t) for t in r.transitions],
            }
        )


render_window_header()
render_reviewer_summary()
scenario_controls = st.container()
timeline_slot = GuidedSlot(
    st.empty(), lambda: getattr(st.session_state.get("scenario_runner"), "run", None)
)
with st.expander("실제 처리 기록 / Execution Trace", expanded=False):
    trace_selector = st.container()
    trace_slot = st.empty()
with scenario_controls:
    st.markdown("### 대표 흐름 체험")
    st.caption(
        "예시 상황을 하나 고르면 필요한 점검을 모두 자동 실행합니다. "
        "세부 버튼을 직접 조작할 필요가 없습니다."
    )
    chosen = st.selectbox("예시 상황", list(SCENARIOS), format_func=SCENARIOS.get)
    st.info(SCENARIO_DESCRIPTIONS[chosen])
    if st.button("시나리오 자동 실행", type="primary", use_container_width=True):
        start_scenario(chosen)

runner = st.session_state.get("scenario_runner")
render_scenario_timeline(runner, timeline_slot)
r.execution.on_change = lambda: render_trace(r.execution, trace_slot)
if runner and runner.run and runner.run.snapshots:
    with trace_selector:
        selected_poll = st.selectbox(
            "점검 단계별 Execution Trace",
            range(len(runner.run.snapshots)),
            format_func=lambda i: (
                f"Poll #{runner.run.snapshots[i].poll} · {runner.run.snapshots[i].title}"
            ),
            key="trace_poll",
        )
        st.caption(
            "위 흐름은 사람이 이해하기 쉬운 상황 변화이고, 이 영역은 선택한 점검 단계에서 "
            "프로그램이 실제로 어떤 수집·분석을 했는지 보여주는 기술 근거입니다."
        )
    render_trace(runner.run.snapshots[selected_poll].trace, trace_slot)
else:
    render_trace(r.execution, trace_slot)

with st.expander("상세 결과 / 마지막 점검의 대시보드", expanded=False):
    st.caption(
        "마지막 점검 시점의 상세 결과입니다. 해설 중의 장면과 시점이 다를 수 있습니다."
    )
    render_status_card()
    render_overview()
    render_time_row()
    selected = render_device_table()
    render_detail(selected)
    render_incident_history()

with st.expander("고급 운영 / 수동 점검", expanded=False):
    st.caption(
        "수동 점검을 실행하면 시나리오 기록을 닫고 현재 Runtime의 관측을 이어갑니다."
    )
    render_controls()
    if selected and any(
        p.member_ip == selected for p in r.engine.pending_connection_changes()
    ):
        if st.button("현재 Connection-Type을 정상 기준으로 설정"):
            st.session_state.pop("scenario_runner", None)
            r.accept_baseline(selected)
            st.rerun()
    render_settings()
    render_technical_detail(selected)
    render_demo_shortcut()

st.caption(
    "Public Web Edition · production DemoPoller / Parser / CorrelationEngine / Detector / IncidentManager 재사용 · 실제 장비 연결 없음"
)
st.link_button(
    "GitHub Source", "https://github.com/sebia1993/aruba-cluster-health-dashboard"
)
r.execution.on_change = None
