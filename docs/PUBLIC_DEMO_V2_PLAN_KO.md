# Public Demo v2 구현 계획 — Aruba Cluster Health Dashboard

## 목적

현재 `portfolio_demo`는 시나리오를 선택하면 합성 CLI를 실제 Parser와 `CorrelationEngine`에 전달하는 엔진 데모다. 분석 로직 재사용은 적절하지만 실제 Windows 프로그램의 운영 흐름과 상호작용은 충분히 재현하지 않는다.

Public Demo v2의 목표는 **원본 프로그램을 직접 사용하는 느낌**을 브라우저에서 재현하는 것이다. Aruba Wireless Policy Mapper의 Streamlit 구현은 "UI가 production workflow를 직접 호출하고, 진행상태·실패경계·결과 근거를 끝까지 보여준다"는 품질 기준만 참고한다. 화면 구조를 복제하지 않는다.

## 원본 프로그램의 실제 사용 흐름

원본 Qt 앱과 문서 기준 핵심 흐름:

1. 설정에서 MM 관리 IP, 등록 Controller/MD, Primary/Fallback 순서를 확인
2. `지금 점검`으로 1회 수집하거나 `자동 시작`으로 반복 점검
3. `show switches`, `show lc-cluster load distribution client`, `show lc-cluster group-membership` 결과를 Parser가 해석
4. IP 기준으로 관측값을 상관분석
5. 정상 / 주의 / 장애 / 확인 불가 상태와 판단 근거 표시
6. 연속 Poll로 Client 분배 이상, 누락, 복구를 판정
7. 선택 장비의 상세 근거와 Raw CLI 확인
8. Incident를 `알림 확인` 처리
9. Connection-Type 변화는 운영자가 별도로 정상 baseline으로 수용 가능
10. 자동 점검 일시정지/재개와 최근 Poll 흐름 확인

Public Demo도 이 흐름을 중심으로 구성한다.

## 핵심 원칙

- 실제 SSH는 절대 열지 않는다.
- 합성 Transport/fixture만 public 경계로 사용한다.
- Parser, 상태 모델, `CorrelationEngine`, 연속 Poll 판단 로직은 production 코드를 재사용한다.
- 결과를 미리 만들어 놓은 JSON으로 렌더링하지 않는다.
- 단순 `Normal/Warning/Critical` selectbox가 메인 UX가 되어서는 안 된다.
- 사용자가 버튼을 눌러 **시간이 진행되고 상태가 변하는 Dashboard**를 체험해야 한다.
- 수집 실패를 Controller Down으로 취급하지 않는다.
- ACK와 복구를 같은 의미로 처리하지 않는다.
- Connection-Type baseline 수용은 일반 ACK와 분리한다.

## 구현 목표

### 1. Demo Lab / 가상 토폴로지

Sidebar 또는 Settings 영역에서 다음 비식별 topology를 표시한다.

- DEMO-MM
- DEMO-MD-01 ~ DEMO-MD-04
- Primary / Fallback 순서
- Poll interval(표현용)
- Demo Mode / 실제 SSH 비활성 표시

사용자가 장비 주소를 실제로 입력할 필요는 없다. 설정 확인 경험만 재현하면 된다.

### 2. 실제 Dashboard 흐름

초기 상태는 "아직 점검하지 않음".

주요 버튼:
- 지금 점검
- 자동 점검 시작
- 일시정지
- Demo Reset

자동 점검은 Streamlit의 rerun 특성 때문에 서버 background thread를 장시간 돌리는 방식보다 **사용자가 Next Poll 또는 자동 재생 컨트롤로 Poll을 진행**할 수 있도록 안전하게 구현해도 된다. 중요한 것은 Poll마다 production engine 상태가 이어지는 것이다.

권장 흐름:
- Poll 0: 정상
- Poll 1: Client 저하 1/3 — 관찰 중
- Poll 2: Client 저하 2/3 — 관찰 중
- Poll 3: Client 분배 이상 3/3 — Warning
- Poll 4: Connection-Type 변화
- Poll 5: MM Down — Critical
- Poll 6: 복구 1/2
- Poll 7: 복구 2/2

이미 `src/aruba_mini_dashboard/demo.py`의 `DEMO_STAGES`, `DemoPoller`가 이 흐름을 제공하므로 최대한 재사용한다.

### 3. Dashboard 표현

상단 요약:
- 전체 상태
- Controller Up / Total
- 확인 가능한 Active Client
- 활성 Incident
- 마지막 Poll
- 현재 Demo Stage

장비 테이블:
- IP
- Alias/Name
- MM Status
- Active / Standby Client
- Connection-Type
- Severity
- Incident/ACK
- Reason

필터:
- 전체 / 정상 / 주의 / 장애 / 확인 불가
- 문제만 보기
- IP/별칭 검색

### 4. 장비 상세

선택한 MD에 대해:
- 현재 관측값
- 판단 근거
- 연속 이상/복구 진행 상태
- 수집 오류
- 해당 장비 관련 Raw CLI
- Incident 상태

### 5. Incident 조작

실제 앱과 동일한 의미를 체험하게 한다.

- `알림 확인`: 선택 Incident를 ACK
- ACK 후에도 장애 자체는 유지
- 복구 조건 충족 시 별도 복구 상태
- Connection-Type 변화가 있으면 별도 `현재 Connection-Type을 정상 기준으로 설정` 버튼 제공
- 일반 ACK가 baseline을 변경하면 안 됨

production IncidentManager/engine API를 재사용할 수 있으면 사용한다. Public Demo만의 상태는 `st.session_state`에 저장하되 판단 로직을 재구현하지 않는다.

### 6. Evidence

별도 탭 또는 expander:
- Poll History
- Raw CLI / Evidence
- Collection Errors
- Architecture

Raw CLI는 fixture를 그대로 보여주되 실제 회사정보는 절대 포함하지 않는다.

## 코드 구조 권장

현재:
- `portfolio_demo/app.py`
- `portfolio_demo/logic.py`
- `portfolio_demo/demo_data/`

개선:
- `portfolio_demo/app.py`: Streamlit UI만
- `portfolio_demo/runtime.py`: Demo session/runtime adapter
- `portfolio_demo/logic.py`: production engine adapter만
- `portfolio_demo/demo_data/`: 필요한 경우 기존 fixture 복제 대신 production demo fixture 경로 재사용 검토

가능하면 `src/aruba_mini_dashboard/demo.py::DemoPoller`를 직접 호출한다. 동일한 stage/판정 로직을 portfolio_demo 안에 다시 구현하지 않는다.

## 테스트

반드시 테스트:
1. Reset 후 첫 Poll = 정상
2. Client 저하 1/3, 2/3에서 즉시 Warning 확정하지 않음
3. 3/3에서 Warning
4. MM Down = 즉시 Critical
5. ACK 후에도 Severity가 정상으로 바뀌지 않음
6. Connection-Type ACK와 baseline 수용 분리
7. 복구 1/2에서 Incident 종료하지 않음
8. 복구 2/2에서 복구
9. Collection Timeout = Unknown/확인 불가이며 Down 아님
10. Demo Reset이 engine state까지 초기화

## 완료 기준

- 방문자가 시나리오 selectbox만 고르는 구조가 아님
- 실제 Dashboard를 운영하는 느낌으로 Poll을 진행할 수 있음
- production DemoPoller/Parser/CorrelationEngine을 사용
- 연속 Poll 상태가 브라우저 세션에서 유지
- ACK, baseline 수용, 복구의 의미가 분리
- 필터/선택/상세 Evidence 제공
- Streamlit Cloud에서 정상 실행
- 기존 README Live Demo 링크 유지
- 기존 production 코드의 안전 경계/테스트를 깨지 않음
