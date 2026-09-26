from pathlib import Path
import sys
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.logic import SCENARIOS, run_demo

st.set_page_config(
    page_title="Cluster Health · Public Demo", page_icon="📡", layout="wide"
)
st.title("Aruba Cluster Health Dashboard")
st.caption("MM · Client Distribution · Cluster Connection-Type의 IP 기준 상관분석")
st.info(
    "공개 Demo Mode · 비식별 합성 CLI를 원 프로젝트 Parser → CorrelationEngine에 전달합니다. 실제 장비 연결과 파일 업로드는 없습니다."
)
with st.sidebar:
    st.subheader("Demo Mode")
    st.success("실제 장비 연결: 비활성")
    st.write("분석: 원 프로젝트 Python 코드")
    st.caption(
        "관측된 CLI와 엔진 판단을 분리합니다. 수집 실패는 장비 장애의 증거가 아닙니다."
    )
scenario = st.selectbox("체험 시나리오", list(SCENARIOS), format_func=SCENARIOS.get)
st.caption(
    "정상 기준 Poll 이후 3회의 합성 Poll을 실행합니다. 실제 앱의 연속 관측 기준을 유지합니다."
)
if st.button("분석 실행", type="primary", width="stretch"):
    st.session_state.result = (scenario, run_demo(scenario))
if "result" in st.session_state:
    selected, (health, raw, timeline) = st.session_state.result
    st.subheader(f"결과 · {SCENARIOS[selected]}")
    if selected != scenario:
        st.info("입력이 변경되었습니다. 분석 실행을 눌러 새 결과를 확인하세요.")
    labels = {
        "normal": "정상",
        "warning": "주의",
        "critical": "장애",
        "unknown": "확인 불가",
    }
    cols = st.columns(5)
    cols[0].metric("Total MD", len(health.devices))
    for col, (key, label) in zip(cols[1:], labels.items()):
        col.metric(label, sum(d.severity.value == key for d in health.devices))
    st.write(health.summary)
    rows = [
        {
            "MD IP": d.ip,
            "MM Status": d.mm_status or "확인 불가",
            "Active Clients": d.active_clients,
            "Client Distribution": d.distribution_state.value,
            "Cluster Connection": d.connection_type or "확인 불가",
            "Final Status": labels[d.severity.value],
            "Reason": " / ".join(d.issue_reasons),
        }
        for d in health.devices
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    ip = st.selectbox("MD 근거 선택", [d.ip for d in health.devices])
    device = health.device_by_ip(ip)
    st.write(
        {
            "관측": device.observations,
            "판단 근거": device.issue_reasons,
            "수집 오류": [e.user_message for e in device.collection_errors],
        }
    )
    with st.expander("Poll History"):
        st.dataframe(timeline, hide_index=True)
    with st.expander("Raw CLI / Evidence"):
        for command, body in raw.items():
            st.markdown(f"**{command}**")
            st.code(body or "[수집 실패 · 출력 없음]", language="text")
    st.download_button(
        "Raw TXT 다운로드",
        "\n\n".join(f"{k}\n{v}" for k, v in raw.items()),
        "cluster-demo.txt",
    )
with st.expander("Architecture / How It Works"):
    st.write(
        "Fixture CLI → 기존 Parser → PollCycleResult → CorrelationEngine → MD별 상태와 근거"
    )
    st.caption("Fixture 검증은 실제 Aruba 장비·운영망·RF 상태 검증이 아닙니다.")
st.link_button(
    "GitHub Source", "https://github.com/sebia1993/aruba-cluster-health-dashboard"
)
