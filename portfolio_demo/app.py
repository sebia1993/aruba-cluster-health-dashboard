from dataclasses import asdict
from pathlib import Path
import sys

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime, TOPOLOGY

st.set_page_config(
    page_title="Cluster Operations Console · Public Demo",
    page_icon="📡",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1500px;}
    .product-kicker {font-size:.78rem; letter-spacing:.08em; font-weight:800; color:#7da7ff; margin-bottom:.25rem;}
    .product-title {font-size:2.15rem; line-height:1.1; font-weight:800; margin:0;}
    .product-sub {color:#8a98aa; margin-top:.45rem; margin-bottom:1rem;}
    .demo-badge {display:inline-block; border:1px solid #31445f; border-radius:999px; padding:.22rem .62rem;
                 font-size:.72rem; font-weight:750; color:#afc8ee; background:#101927; margin-right:.35rem;}
    .flow-card {border:1px solid rgba(120,145,175,.25); border-radius:12px; padding:.8rem .95rem;
                background:rgba(18,27,41,.55); min-height:82px;}
    .flow-title {font-size:.76rem; color:#8da2bb; font-weight:700; margin-bottom:.2rem;}
    .flow-value {font-size:1rem; font-weight:760;}
    .hint {font-size:.82rem; color:#8391a3;}
    </style>
    """,
    unsafe_allow_html=True,
)

if "runtime" not in st.session_state:
    st.session_state.runtime = DemoRuntime()
r = st.session_state.runtime


def severity_label(value: str) -> str:
    return {
        "normal": "정상",
        "warning": "주의",
        "critical": "장애",
        "unknown": "확인 불가",
    }.get(value, value)


def render_header() -> None:
    st.markdown('<div class="product-kicker">ARUBA CLUSTER OPERATIONS</div>', unsafe_allow_html=True)
    st.markdown('<div class="product-title">Cluster Health Dashboard</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="product-sub">MM / Controller 상태, Client 분배, Connection-Type 변화와 Incident를 한 화면에서 확인합니다.</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<span class="demo-badge">PUBLIC DEMO</span>'
        '<span class="demo-badge">SYNTHETIC NETWORK</span>'
        '<span class="demo-badge">READ ONLY</span>',
        unsafe_allow_html=True,
    )


def render_sidebar() -> None:
    with st.sidebar:
        st.subheader("운영 범위")
        st.caption("외부 장비 연결과 자격 증명 입력은 비활성화되어 있습니다.")
        st.write("**MM**  DEMO-MM · 192.0.2.1")
        st.write("**MD**  4 Controllers")
        st.write("**판정 기준**  이상 3회 · 복구 2회")
        st.divider()
        st.caption("현재 데모는 production Parser / CorrelationEngine / IncidentManager를 사용합니다.")
        if st.button("Demo Reset", use_container_width=True):
            st.session_state.runtime = DemoRuntime()
            st.rerun()


def render_operator_controls() -> None:
    c = st.columns([1.1, 1.1, 1, 1, 3])
    step = c[0].button("지금 점검", type="primary", use_container_width=True)
    if c[1].button("자동 점검 시작", disabled=r.running, use_container_width=True):
        r.running = True
        step = True
    if c[2].button("다음 Poll", disabled=not r.running, use_container_width=True):
        step = True
    if c[3].button("일시정지", disabled=not r.running, use_container_width=True):
        r.running = False
        st.rerun()
    c[4].caption(
        "자동 점검은 백그라운드 데몬을 만들지 않습니다. "
        "‘다음 Poll’이 실제 5초 주기에 해당하는 다음 관측을 재생합니다."
    )

    failure = False
    with st.expander("Demo controls · 장애/수집 실패 재현", expanded=False):
        st.caption(
            "실제 운영 화면에서는 보조 진단 기능에 해당합니다. "
            "기본 사용 흐름과 분리해 두었습니다."
        )
        failure = st.checkbox("다음 Poll에 CLI Timeout 주입")
        st.write("ACK는 알림 확인이며, 장애 복구나 Connection-Type 기준 수용과는 별개입니다.")

    if step:
        try:
            with st.status(
                "CLI 수집 → Parser → CorrelationEngine → IncidentManager",
                expanded=False,
            ):
                r.poll(failure)
        except ValueError as exc:
            st.warning(str(exc))


def render_metrics() -> None:
    if r.health is None:
        c = st.columns(5)
        c[0].metric("전체 상태", "대기")
        c[1].metric("Controller Up / Total", f"- / {len(TOPOLOGY)}")
        c[2].metric("관측 Active Client", "-")
        c[3].metric("활성 Incident", "0")
        c[4].metric("마지막 Poll", "0")
        return

    h = r.health
    c = st.columns(5)
    c[0].metric("전체 상태", severity_label(h.severity.value))
    c[1].metric(
        "Controller Up / Total",
        f"{sum((d.mm_status or '').lower() == 'up' for d in h.devices)} / {len(h.devices)}",
    )
    counts = [d.active_clients for d in h.devices if d.active_clients is not None]
    c[2].metric("관측 Active Client", sum(counts) if counts else "확인 불가")
    c[3].metric("활성 Incident", len(r.incidents.active_incidents()))
    c[4].metric("마지막 Poll", r.poll_count)


def render_dashboard() -> None:
    st.subheader("Controller 상태")
    if r.health is None:
        st.info("아직 점검하지 않았습니다. ‘지금 점검’을 누르면 현재 Cluster 상태를 수집합니다.")
        st.dataframe(
            [
                {
                    "Controller": alias,
                    "IP": ip,
                    "MM": "대기",
                    "Active": "-",
                    "Standby": "-",
                    "Connection-Type": "-",
                    "Severity": "대기",
                    "Incident / ACK": "",
                    "Reason": "",
                }
                for ip, alias in TOPOLOGY.items()
            ],
            hide_index=True,
            width="stretch",
        )
        return

    h = r.health
    st.caption(f"현재 관측 단계: {r.stage}")
    st.write(h.summary)

    c = st.columns(3)
    severity = c[0].selectbox(
        "상태 필터",
        ["전체", "normal", "warning", "critical", "unknown"],
        format_func=lambda v: "전체" if v == "전체" else severity_label(v),
    )
    problems = c[1].checkbox("문제만 보기")
    search = c[2].text_input("IP / 별칭 검색")
    rows = [
        row
        for row in r.rows()
        if (severity == "전체" or row["Severity"] == severity)
        and (not problems or row["Severity"] != "normal")
        and search.casefold() in (row["IP"] + row["Alias"]).casefold()
    ]
    st.dataframe(rows, hide_index=True, width="stretch")


def render_incidents() -> None:
    st.subheader("Incident 조사")
    if r.health is None:
        st.info("점검 후 Controller별 Incident와 상태 변화가 표시됩니다.")
        # Keep the MD selector present so UI order remains stable for automated tests.
        st.selectbox(
            "MD 선택",
            list(TOPOLOGY),
            format_func=lambda v: f"{TOPOLOGY[v]} · {v}",
            disabled=True,
        )
        return

    h = r.health
    ip = st.selectbox(
        "MD 선택",
        list(TOPOLOGY),
        format_func=lambda v: f"{TOPOLOGY[v]} · {v}",
    )
    d = h.device_by_ip(ip)
    a, b, c = st.columns(3)
    a.markdown(
        f'<div class="flow-card"><div class="flow-title">Controller</div>'
        f'<div class="flow-value">{d.alias}<br>{d.ip}</div></div>',
        unsafe_allow_html=True,
    )
    b.markdown(
        f'<div class="flow-card"><div class="flow-title">Observed State</div>'
        f'<div class="flow-value">{severity_label(d.severity.value)} · Client {d.active_clients}</div></div>',
        unsafe_allow_html=True,
    )
    c.markdown(
        f'<div class="flow-card"><div class="flow-title">Connection-Type</div>'
        f'<div class="flow-value">{d.connection_type or "확인 불가"}</div></div>',
        unsafe_allow_html=True,
    )

    st.caption("연속 이상·복구 카운터는 production detector 상태를 사용합니다.")
    st.dataframe(
        [
            dict(Signal=key, **value)
            for key, value in r.engine.detector.dump_state().items()
            if key.endswith("|" + ip)
        ],
        hide_index=True,
        width="stretch",
    )

    x = st.columns(2)
    if x[0].button("알림 확인 · ACK", use_container_width=True):
        r.acknowledge(ip)
        st.rerun()
    pending = [v for v in r.engine.pending_connection_changes() if v.member_ip == ip]
    if x[1].button(
        "현재 Connection-Type을 정상 기준으로 설정",
        disabled=not pending,
        use_container_width=True,
    ):
        r.accept_baseline(ip)
        st.rerun()

    if pending:
        st.warning("Connection-Type 변화가 관측되었습니다. 기준 수용 전까지 별도 Incident로 유지합니다.")
        st.json([asdict(v) for v in pending])

    st.subheader("Incident History")
    rows = r.incident_rows()
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.success("현재 기록된 Incident가 없습니다.")

    with st.expander("선택 Controller 원시 상태", expanded=False):
        st.json(asdict(d))


def render_evidence() -> None:
    st.subheader("Evidence / History")
    if r.health is None:
        st.info("점검 후 관측 History와 Raw CLI가 이 영역에 누적됩니다.")
        return
    st.dataframe(r.history, hide_index=True, width="stretch")
    with st.expander("상태 전이 이벤트", expanded=False):
        st.json([asdict(t) for t in r.transitions])
    with st.expander("수집 오류", expanded=False):
        st.json([asdict(e) for e in r.health.collection_errors])
    with st.expander("Raw CLI", expanded=False):
        for command, raw in r.raw.items():
            st.markdown(f"**{command}**")
            st.code(raw, language="text")
    st.download_button(
        "Raw TXT 다운로드",
        "\n\n".join(f"{k}\n{v}" for k, v in r.raw.items()),
        "cluster-poll.txt",
    )


render_header()
render_sidebar()
render_operator_controls()
render_metrics()

dashboard, incidents, evidence = st.tabs(
    ["운영 Dashboard", "Incident / 상세", "Evidence / Export"]
)
with dashboard:
    render_dashboard()
with incidents:
    render_incidents()
with evidence:
    render_evidence()

with st.expander("Architecture", expanded=False):
    st.write(
        "production DemoPoller → Parser → session-owned CorrelationEngine → IncidentManager"
    )
    st.caption(
        "Public Demo는 실제 SSH 대신 합성 CLI만 공급합니다. "
        "상태 판정, 연속 이상/복구, Incident와 기준 수용은 production 로직을 그대로 사용합니다."
    )
st.link_button(
    "GitHub Source",
    "https://github.com/sebia1993/aruba-cluster-health-dashboard",
)
