from dataclasses import asdict
from pathlib import Path
import sys
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime, TOPOLOGY

st.set_page_config(
    page_title="Cluster Operations · Public Demo v2", page_icon="📡", layout="wide"
)
if "runtime" not in st.session_state:
    st.session_state.runtime = DemoRuntime()
r = st.session_state.runtime
st.title("Cluster 운영 Dashboard")
st.caption("설정 확인 → Poll 누적 → Incident → 알림 확인 → 기준 수용 → 복구")
with st.sidebar:
    st.header("Demo Lab")
    st.success("실제 SSH 비활성 · 합성 CLI")
    st.write("DEMO-MM · 192.0.2.1")
    st.dataframe(
        [
            {
                "Controller": alias,
                "IP": ip,
                "Order": "Primary" if n == 0 else f"Fallback {n}",
            }
            for n, (ip, alias) in enumerate(TOPOLOGY.items())
        ],
        hide_index=True,
    )
    st.caption(
        "Poll 간격 5초에 해당하는 시간을 다음 Poll로 진행합니다. 외부 장비에는 연결하지 않습니다."
    )
    st.caption("이상 확정 3회 · 복구 확인 2회. ACK는 장애 복구나 기준 수용이 아닙니다.")
    if st.button("Demo Reset"):
        st.session_state.runtime = DemoRuntime()
        st.rerun()
cols = st.columns(4)
step = cols[0].button("지금 점검", type="primary")
if cols[1].button("자동 점검 시작", disabled=r.running):
    r.running = True
    step = True
if cols[2].button("다음 Poll", disabled=not r.running):
    step = True
if cols[3].button("일시정지", disabled=not r.running):
    r.running = False
    st.rerun()
failure = st.checkbox("다음 Poll에 CLI Timeout 주입")
if step:
    try:
        with st.status(
            "CLI 수집 → Parser → CorrelationEngine → IncidentManager", expanded=False
        ):
            r.poll(failure)
    except ValueError as exc:
        st.warning(str(exc))
st.caption(
    "자동 점검 체험 중 · 다음 Poll을 눌러 진행"
    if r.running
    else "점검 일시정지 · 지금 점검으로 1회 실행 가능"
)
if r.health is None:
    st.info("아직 점검하지 않았습니다. 지금 점검으로 정상 기준을 관측하세요.")
else:
    h = r.health
    c = st.columns(5)
    c[0].metric("전체 상태", h.severity.value)
    c[1].metric(
        "Controller Up / Total",
        f"{sum((d.mm_status or '').lower() == 'up' for d in h.devices)} / {len(h.devices)}",
    )
    counts = [d.active_clients for d in h.devices if d.active_clients is not None]
    c[2].metric("관측 Active Client", sum(counts) if counts else "확인 불가")
    c[3].metric("활성 Incident", len(r.incidents.active_incidents()))
    c[4].metric("마지막 Poll", r.poll_count)
    st.subheader(r.stage)
    st.write(h.summary)
    table, details, evidence = st.tabs(
        ["Controller", "Incident / 상세", "Evidence / History"]
    )
    with table:
        c = st.columns(3)
        severity = c[0].selectbox(
            "상태 필터", ["전체", "normal", "warning", "critical", "unknown"]
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
    with details:
        ip = st.selectbox(
            "MD 선택", list(TOPOLOGY), format_func=lambda v: f"{TOPOLOGY[v]} · {v}"
        )
        d = h.device_by_ip(ip)
        st.write(
            f"{d.alias} · {d.severity} · Client {d.active_clients} · Connection {d.connection_type}"
        )
        with st.expander("관측 상세 / Raw state"):
            st.json(asdict(d))
        st.caption("연속 이상·복구 카운터는 production detector 상태입니다.")
        st.dataframe(
            [
                dict(Signal=key, **value)
                for key, value in r.engine.detector.dump_state().items()
                if key.endswith("|" + ip)
            ],
            hide_index=True,
        )
        c = st.columns(2)
        if c[0].button("알림 확인 · ACK"):
            r.acknowledge(ip)
            st.rerun()
        pending = [
            v for v in r.engine.pending_connection_changes() if v.member_ip == ip
        ]
        if c[1].button(
            "현재 Connection-Type을 정상 기준으로 설정", disabled=not pending
        ):
            r.accept_baseline(ip)
            st.rerun()
        st.json([asdict(v) for v in pending])
        st.dataframe(r.incident_rows(), hide_index=True, width="stretch")
    with evidence:
        st.dataframe(r.history, hide_index=True, width="stretch")
        st.json([asdict(t) for t in r.transitions])
        st.json([asdict(e) for e in h.collection_errors])
        for command, raw in r.raw.items():
            with st.expander(command):
                st.code(raw, language="text")
        st.download_button(
            "Raw TXT 다운로드",
            "\n\n".join(f"{k}\n{v}" for k, v in r.raw.items()),
            "cluster-poll.txt",
        )
with st.expander("Architecture"):
    st.write(
        "production DemoPoller → Parser → session-owned CorrelationEngine → IncidentManager"
    )
    st.caption(
        "DemoPoller의 자동 기준 수용을 노출하지 않는 어댑터를 사용합니다. 기준 수용은 사용자 버튼으로만 수행합니다. 마지막 단계는 유지하며 Reset만 엔진을 초기화합니다."
    )
st.link_button(
    "GitHub Source",
    "https://github.com/sebia1993/aruba-cluster-health-dashboard/tree/codex/public-demo-v2",
)
