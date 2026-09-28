from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime, TOPOLOGY

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
            '<div class="window-title">Aruba 네트워크 상태 미니보드 — 데모</div>'
            '<div class="window-meta">Desktop App의 운영 화면을 그대로 옮긴 Public Web Edition</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<div style="padding-top:.65rem;text-align:right">'
            '<span class="demo-pill">PUBLIC DEMO</span>'
            '<span class="demo-pill">SYNTHETIC CLI</span>'
            '<span class="demo-pill">READ ONLY</span>'
            '</div>',
            unsafe_allow_html=True,
        )


def render_status_card() -> None:
    if r.health is None:
        status = "확인 불가"
        problem = "문제 IP: 확인 전"
        reason = "점검을 실행하면 판단 근거가 표시됩니다."
    else:
        status = ko_status(r.health.severity.value)
        problem_devices = [
            d for d in r.health.devices if d.severity.value != "normal"
        ]
        if problem_devices:
            first = problem_devices[0]
            problem = f"주요 문제 IP: {first.ip} · {first.display_name}"
        else:
            problem = "문제 IP: 없음"
        reason = r.health.summary or "현재 관측에서 별도 이상 신호가 없습니다."

    st.markdown(
        '<div class="status-hero">'
        f'<div class="status-value">{status}</div>'
        f'<div class="status-line">{problem}</div>'
        f'<div class="status-line">판단 근거: {reason}</div>'
        '</div>',
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
        controller_up = (
            f"{sum((d.mm_status or '').lower() == 'up' for d in r.health.devices)}"
            f" / {len(r.health.devices)}"
        )
        known = [
            d.active_clients
            for d in r.health.devices
            if d.active_clients is not None
        ]
        active_total = sum(known) if known else "확인 불가"
        incident_count = len(r.incidents.active_incidents())

    cols = st.columns(4)
    cols[0].metric("전체 상태", overall)
    cols[1].metric("Controller Up", controller_up)
    cols[2].metric("전체 Active Client", active_total)
    cols[3].metric("활성 Incident", incident_count)

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### 등록 Controller")
        devices = r.health.devices if r.health else []
        if devices:
            cards = st.columns(2)
            for index, device in enumerate(devices[:8]):
                with cards[index % 2]:
                    st.markdown(
                        '<div class="controller-card">'
                        f'<div class="controller-name">{device.display_name}</div>'
                        f'<div class="controller-meta">{device.ip}<br>'
                        f'{ko_status(device.severity.value)} · Active {device.active_clients} · '
                        f'Standby {device.standby_clients}<br>'
                        f'Connection {device.connection_type or "확인 불가"}</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
        else:
            st.caption("점검 전입니다. 등록된 Demo Controller 4대를 대상으로 상태를 수집합니다.")

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
    c[1].caption(
        "다음 점검: Demo 재생 대기"
        if r.running
        else "다음 점검: 일시정지"
    )


def selected_ip() -> str | None:
    if not r.health:
        return None
    current = st.session_state.get("cluster_selected_ip")
    valid = [d.ip for d in r.health.devices]
    return current if current in valid else valid[0]


def run_one_poll(*, failure: bool = False) -> None:
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
        c[0].metric("이상 확정", "3회")
        c[1].metric("복구 확정", "2회")
        c[2].metric("실제 SSH", "비활성")
        

def render_device_table() -> str | None:
    st.markdown("#### 장비 검색 및 필터")
    f = st.columns([2.2, 1, 1, 1])
    search = f[0].text_input(
        "장비 검색",
        placeholder="IP, alias, hostname 검색",
        label_visibility="collapsed",
    )
    status_filter = f[1].selectbox(
        "상태 필터",
        ["전체 상태", "정상", "주의", "장애", "확인 불가"],
        label_visibility="collapsed",
    )
    problem_only = f[2].checkbox("문제만 보기")
    monitoring_only = f[3].checkbox("감시 대상만", value=True)

    rows = []
    if r.health:
        for device in r.health.devices:
            row = {
                "IP": device.ip,
                "장비명": device.display_name,
                "MM 보고 상태": device.mm_status or "확인 불가",
                "Active": device.active_clients,
                "Standby": device.standby_clients,
                "Connection-Type": device.connection_type or "확인 불가",
                "종합 상태": ko_status(device.severity.value),
                "마지막 확인": f"Poll {r.poll_count}",
                "감시 범위": "감시 중",
                "분배 상태": distribution_status(device),
            }
            if search and search.casefold() not in (
                row["IP"] + row["장비명"]
            ).casefold():
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
                "MM 보고 상태": "점검 전",
                "Active": "-",
                "Standby": "-",
                "Connection-Type": "-",
                "종합 상태": "확인 불가",
                "마지막 확인": "-",
                "감시 범위": "감시 중",
                "분배 상태": "점검 전",
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
        format_func=lambda ip: (
            f"{r.health.device_by_ip(ip).display_name} · {ip}"
        ),
    )
    st.session_state.cluster_selected_ip = choice
    return choice


def render_detail(ip: str | None) -> None:
    if not ip or not r.health:
        st.caption("장비를 선택하면 상세 정보를 표시합니다.")
        return

    device = r.health.device_by_ip(ip)
    st.markdown("#### 장비 상세 정보")
    summary, parsed, raw = st.tabs(["요약", "파싱 결과", "원본 출력"])

    with summary:
        c = st.columns(2)
        c[0].write(f"**장비명**  {device.display_name}")
        c[0].write(f"**IP**  {device.ip}")
        c[0].write(f"**MM 보고 상태**  {device.mm_status or '확인 불가'}")
        c[0].write(f"**종합 상태**  {ko_status(device.severity.value)}")
        c[1].write(f"**Active Client**  {device.active_clients}")
        c[1].write(f"**Standby Client**  {device.standby_clients}")
        c[1].write(
            f"**Connection-Type**  {device.connection_type or '확인 불가'}"
        )
        reasons = " / ".join(device.issue_reasons) or "별도 이상 근거 없음"
        st.info(f"판단 근거: {reasons}")

        pending = [
            item
            for item in r.engine.pending_connection_changes()
            if item.member_ip == ip
        ]
        if pending:
            st.warning("Connection-Type 변화가 기준 수용 대기 중입니다.")
            if st.button("현재 Connection-Type을 정상 기준으로 설정"):
                r.accept_baseline(ip)
                st.rerun()

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
            demo = DemoRuntime()
            for _ in range(6):
                demo.poll()
            st.session_state.runtime = demo
            st.rerun()
        if c[1].button("Demo Reset", use_container_width=True):
            st.session_state.runtime = DemoRuntime()
            st.session_state.cluster_selected_ip = None
            st.rerun()


render_window_header()
render_status_card()
render_overview()
render_time_row()
render_controls()
render_settings()

selected = render_device_table()
render_detail(selected)
render_demo_shortcut()

st.caption(
    "Public Web Edition · production DemoPoller / Parser / CorrelationEngine / "
    "IncidentManager 재사용 · 실제 장비 연결 없음"
)
st.link_button(
    "GitHub Source",
    "https://github.com/sebia1993/aruba-cluster-health-dashboard",
)
