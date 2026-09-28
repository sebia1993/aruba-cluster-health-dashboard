# Cluster Health Dashboard — Public Demo

**[Live Demo](https://sebia1993-cluster-health-demo.streamlit.app/)** · [GitHub Source](https://github.com/sebia1993/aruba-cluster-health-dashboard)

## 무엇을 보여주는 데모인가

여러 Aruba 무선 Controller의 상태를 자동으로 종합해 **실제 장애 징후와 단순 수집 실패를 구분하는 운영 Dashboard**입니다.

실제 운영에서는 다음 정보를 사람이 각각 확인해야 합니다.

- MM이 보고하는 Controller 상태
- Active / Standby Client 분배
- Cluster Connection-Type과 구성원 상태
- 순간적인 수집 실패인지 지속적인 이상인지 여부

Public Demo는 이 값을 Controller IP 기준으로 합쳐 **정상 / 주의 / 장애 / 확인 불가**로 계산하고, 운영자가 확인할 사건만 Incident로 표시합니다.

## 가장 빠르게 보는 방법

1. Live Demo를 엽니다.
2. **대표 장애 시나리오 1-click**을 누릅니다.
3. 상단의 자연어 **결론**을 먼저 읽습니다.
4. `운영 Dashboard`에서 Controller별 상태를 확인합니다.
5. `Incident / 상세`에서 어떤 Controller가 왜 문제인지 확인합니다.
6. 필요할 때만 `Evidence / Export`에서 Raw CLI와 상태 전이를 봅니다.

실제 장비 연결은 하지 않습니다. 비식별 합성 CLI가 입력 역할만 하며, production Parser, CorrelationEngine, IncidentManager와 연속 이상·복구 판단 로직을 재사용합니다.

## 직접 실행

```sh
python -m venv .venv-demo
# 가상환경 활성화 후
python -m pip install -r portfolio_demo/requirements.txt
python -m streamlit run portfolio_demo/app.py
python -m unittest discover -s portfolio_demo -p test_demo.py -v
```

Streamlit Community Cloud는 repository `sebia1993/aruba-cluster-health-dashboard`, branch `main`, entrypoint `portfolio_demo/app.py`를 사용합니다.

Fixture·AppTest·Windows CI 결과는 실제 운영 장비 검증이나 현장 성과 수치와 구분합니다.
