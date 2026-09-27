# Public Streamlit Demo

**[Live Demo](https://sebia1993-cluster-health-demo.streamlit.app/)** · [GitHub Source](https://github.com/sebia1993/aruba-cluster-health-dashboard)

별도 장비와 계정 없이 원 프로젝트의 Python 분석 로직을 실행합니다.
비식별 문서 주소와 합성 CLI만 사용하며, 외부 연결·내부 파일 업로드는 지원하지 않습니다.

```sh
python -m venv .venv-demo
# 가상환경 활성화 후
python -m pip install -r portfolio_demo/requirements.txt
python -m streamlit run portfolio_demo/app.py
python -m unittest discover -s portfolio_demo -p test_demo.py -v
```

Streamlit Community Cloud: repository `sebia1993/aruba-cluster-health-dashboard`, branch `main`,
entrypoint `portfolio_demo/app.py`, Python 3.13.
의존성은 entrypoint 옆 `portfolio_demo/requirements.txt`를 사용합니다.
기존 Windows 앱의 런타임 잠금 파일과 패키징 경로는 유지합니다.

v2는 Poll 상태를 누적하며 ACK와 기준 수용을 별도로 실행합니다.
브라우저 세션별 엔진을 사용하고 Reset으로 초기화합니다.
Fixture·AppTest·Windows CI는 실제 장비/운영망 검증이 아닙니다.

## Public Demo v2 (review branch)

`codex/public-demo-v2` provides a session-owned operations console: inspect Demo Lab,
run one poll or start the controlled Next Poll playback, inspect incidents, ACK,
explicitly accept a Connection-Type baseline, and observe recovery. The production
DemoPoller, parsers, CorrelationEngine and IncidentManager are reused. An adapter
prevents the desktop scripted demo from automatically accepting a baseline.
Timeout injection pauses the stage and preserves unconfirmed incidents. Playback
holds the final stage and is bounded to 100 polls; Reset clears all engine state.

The existing main Live Demo remains unchanged until review. No SSH, credentials,
background daemon, shared database or production device connections are used.
