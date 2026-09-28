# Aruba 네트워크 상태 미니보드 — Public Web Edition

**[고정 Live URL](https://sebia1993-cluster-health-demo.streamlit.app/)** · [GitHub Source](https://github.com/sebia1993/aruba-cluster-health-dashboard)

이 앱은 별도의 데모 UI가 아니라 **Desktop `Aruba 네트워크 상태 미니보드`의 Web Edition**입니다.

## Desktop → Web 매핑

| Desktop UI | Public Web Edition |
|---|---|
| 전체 상태 카드 | 동일한 전체 상태 / 문제 IP / 판단 근거 |
| Overview KPI | 전체 상태 / Controller Up / Active Client / Incident |
| 등록 Controller 카드 | 동일한 Controller별 상태 카드 |
| 지금 점검 / 자동 시작 / 일시정지 / 알림 확인 | 동일 작업 버튼 |
| 장비 검색·상태 필터·문제만 보기 | 동일 필터 바 |
| 전체 Controller 표 | 동일 10개 핵심 컬럼 |
| 장비 상세 정보 | 요약 / 파싱 결과 / 원본 출력 탭 |
| 설정 창 | Demo 설정을 읽기 전용으로 표시 |

Public Web Edition에서 바뀌는 유일한 운영 경계는 **장비 Transport**입니다. 실제 SSH 대신 비식별 합성 CLI를 사용합니다. 상태 판정·연속 이상/복구·Incident·Connection-Type baseline 로직은 production 코드를 재사용합니다.

Streamlit Community Cloud는 기존 repository `sebia1993/aruba-cluster-health-dashboard`, branch `main`, entrypoint `portfolio_demo/app.py`를 계속 사용합니다. **Live URL은 변경하지 않습니다.**

자동 fixture/CI 결과는 실제 운영 장비 검증과 구분합니다.
