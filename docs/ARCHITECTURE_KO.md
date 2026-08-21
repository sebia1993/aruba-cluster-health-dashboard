# Aruba MM / WLC 모니터링 구조

이 문서는 장비 조회부터 Parser, 상태 판단, 사건 관리, UI 표시까지의 책임 경계를 설명합니다.

## 1. 전체 구조

```mermaid
flowchart TB
    OP["Windows 운영자"] --> UI["PySide6 Dashboard"]
    UI --> PC["PollCoordinator"]

    PC --> MM["MM Collector"]
    PC --> CL["Cluster Collector"]

    MM -->|"show switches"| MP["MM Parser"]
    CL -->|"load distribution client"| LP["Load Parser"]
    CL -->|"group-membership"| GP["Membership Parser"]

    MP --> CE["Correlation Engine"]
    LP --> CE
    GP --> CE

    CE --> AD["Anomaly Detector"]
    AD --> IM["Incident Manager"]

    IM --> DB["SQLite"]
    IM --> UI
    IM --> NS["Notification Service"]

    CFG["비밀정보 없는 설정"] --> PC
    CRED["Credential Manager / Session Memory"] --> MM
    CRED --> CL
```

핵심은 **수집과 장애 판단을 분리하는 것**입니다. SSH가 실패했다고 해서 Collector가 장비를 Down으로 만들지 않으며, Parser가 만든 관측값과 수집 신뢰도를 Correlation 계층이 함께 해석합니다.

## 2. 구성요소별 책임

### Collector

`collectors/`는 장비 접속과 허용된 명령 실행을 담당합니다.

- MM: `show switches`
- Cluster: `show lc-cluster load distribution client`
- Cluster: `show lc-cluster group-membership`
- Primary Controller 실패 시 등록된 Fallback 순서로 조회
- 실제 명령을 수행한 Controller 정보를 결과에 남김
- SSH 호스트 키 확인과 취소 가능한 연결 처리

Collector는 장애 원인을 추정하지 않습니다.

### Parser

`parsers/`는 CLI 원문을 구조화된 관측값으로 변환합니다.

- ANSI/헤더/부분 행 등 출력 변형 처리
- 유효하게 읽을 수 있는 행과 불완전한 수집 상태 분리
- 수집 실패를 임의의 정상/Down 값으로 채우지 않음

### Anomaly Detector

`services/anomaly_detector.py`는 시간 축을 포함한 이상 판단을 담당합니다.

기본 설정은 다음과 같습니다.

| 항목 | 기본값 |
|---|---:|
| 낮은 Active Client 절대 기준 | 10 |
| 이상 확정 | 3회 연속 |
| 복구 확정 | 2회 연속 |
| Peer 대비 비율 기준 | 25% |
| Cluster 전체 Active 최소 기준 | 50 |
| Peer 중앙값 최소 기준 | 30 |
| 구성원 누락 확정 | 3회 연속 |

단일 숫자 하나만으로 장애를 확정하지 않고 절대값, 상대값, Cluster 전체 사용량과 연속 관측을 조합합니다.

### Correlation Engine

`services/correlation_engine.py`는 서로 다른 명령의 결과를 **장비 IP 기준**으로 연결합니다.

예를 들어 같은 IP에서 다음 두 신호가 동시에 발생할 수 있습니다.

```text
show switches          → Controller Down
load distribution      → Active Client 이상
```

이 경우 화면에는 별도 행 두 개가 아니라 같은 장비의 복수 원인으로 누적합니다.

또한 수집 자체가 실패한 경우에는 실제 장비 상태와 구분하여 `확인 불가` 또는 부분 수집으로 유지합니다.

### Incident Manager

`services/incident_manager.py`는 상태의 lifecycle을 관리합니다.

```text
정상
 ↓
이상 관측
 ↓
사건 활성
 ↓
운영자 확인 ──→ 장애 감시는 계속
 ↓
복구 조건 충족
 ↓
복구 기록
```

운영자가 알림을 확인했다고 장애 자체를 정상으로 변경하지 않습니다. 확인 상태는 반복 알림 제어용이며 실제 복구는 별도 조건을 충족해야 합니다.

### PollCoordinator

`services/poll_coordinator.py`는 UI와 수집 작업 사이의 실행 순서를 관리합니다.

- 수동 점검
- 자동 점검
- 중복 점검 방지
- Worker Thread 실행
- 종료/취소
- 다음 점검 시각

네트워크 I/O가 Qt GUI thread를 직접 점유하지 않도록 분리합니다.

### Storage

`storage.py`의 SQLite는 다음과 같은 로컬 운영 상태를 보존합니다.

- 최근 관측/최근 정상 상태
- Connection-Type baseline
- anomaly/recovery streak
- 사건/확인/복구 상태
- 일부 UI/운영 설정 미러

원본 명령 출력과 비밀번호는 SQLite에 저장하지 않습니다.

손상되거나 지원할 수 없는 기존 상태를 읽지 못한 경우 빈 DB처럼 계속 운용하지 않고, 원본을 보존하고 자동 점검을 중지한 격리 상태로 전환합니다.

## 3. 상태 계산의 신뢰도 경계

프로그램은 다음 두 종류를 구분합니다.

### 장비 상태 증거

- 정상 수집된 `show switches`의 명시적 Down
- 정상 Parser 결과의 Client 분배 이상
- 정상 Membership 결과의 Connection-Type 변화

### 수집 상태 증거

- SSH 연결 실패
- Timeout
- 명령 실패
- 빈 출력
- Parser 실패/부분 수집

두 번째 범주는 장비 Down의 직접 증거가 아닙니다.

```text
수집 실패 ≠ 장비 장애
```

이 원칙은 False Positive를 줄이기 위한 핵심 안전 경계입니다.

## 4. Primary / Fallback

Cluster 상태 조회는 운영자가 등록한 Primary부터 시작합니다.

```text
Primary
   ↓ 실패
Fallback #1
   ↓ 실패
Fallback #2
   ↓
수집 결과
```

Fallback에서 정상 수집되더라도 어떤 Controller가 실제 수집을 수행했는지 별도로 기록합니다. 따라서 수집 경로가 바뀐 사실과 Cluster 상태 자체를 혼동하지 않습니다.

## 5. UI 계층

PySide6 UI는 도메인 상태를 다시 계산하지 않고 Correlation/Incident 결과를 표시합니다.

주요 표시 항목:

- 종합 상태
- 문제 IP
- 판단 근거
- MM 보고 상태
- Active / Standby Client
- Connection-Type
- 분배 상태
- 마지막 점검 시각

폭이 작은 창은 등록된 Controller의 핵심 상태에 집중하고, 넓은 창은 전체 장비 정보를 표시합니다.

## 6. 외부 전송 경계

프로그램은 중앙 서버나 클라우드 텔레메트리를 사용하지 않습니다.

- 상태 저장: 로컬 SQLite
- 설정: 로컬 JSON
- 자격 증명: Windows Credential Manager 또는 세션 메모리
- 알림: Windows 로컬 알림/Tray

실제 운영 데이터의 외부 전송 기능은 현재 설계 범위에 없습니다.
