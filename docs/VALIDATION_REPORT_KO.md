# 검증 보고서

이 문서는 Aruba MM / WLC 상태 모니터링의 **자동화 검증과 실제 현장 검증을 서로 다른 증거 수준으로 관리**하기 위한 기준입니다.

실제 장비 IP, Hostname, 사용자명, SSH 지문, 원본 명령 출력은 공개 저장소에 기록하지 않습니다.

## 1. 현재 공개 검증 상태

| 영역 | 상태 | 공개 근거 |
|---|---|---|
| `show switches` Parser | ✅ 자동 검증 | 비식별 fixture / pytest |
| Client 분배 Parser | ✅ 자동 검증 | 비식별 fixture / pytest |
| Group Membership Parser | ✅ 자동 검증 | 비식별 fixture / pytest |
| MM Down / 수집 실패 분리 | ✅ 자동 검증 | 도메인 회귀 테스트 |
| 3회 이상 / 2회 복구 streak | ✅ 자동 검증 | anomaly detector 테스트 |
| 낮은 전체 사용량 오탐 방지 | ✅ 자동 검증 | anomaly detector 테스트 |
| 구성원 누락 debounce | ✅ 자동 검증 | anomaly detector 테스트 |
| Connection-Type baseline / 변화 | ✅ 자동 검증 | correlation / incident 테스트 |
| 복수 문제 IP 상관분석 | ✅ 자동 검증 | correlation engine 테스트 |
| Primary / Fallback | ✅ 자동 검증 | Collector / 가짜 SSH 통합 테스트 |
| strict host-key 경계 | ✅ 자동 검증 | 로컬 SSH 통합 경로 |
| SQLite 저장·재시작·손상 보호 | ✅ 자동 검증 | 임시/메모리 SQLite 테스트 |
| Worker Thread / 중복 점검 | ✅ 자동 검증 | PollCoordinator / UI 테스트 |
| PySide6 offscreen UI | ✅ 자동 검증 | GUI 테스트 |
| Windows onedir 패키지 | ✅ GitHub Actions | `ci-windows.yml` |
| 추출 후 EXE smoke | ✅ GitHub Actions | 버전 ZIP 검증 경로 |
| 실제 Aruba MM / 7240XM 출력 | ⚠️ 별도 현장 증거 | 자동 fixture로 대체하지 않음 |
| Python 미설치 Windows 11 운용 | ⚠️ 별도 현장 증거 | 실제 PC/VM 검증 필요 |

> 자동 테스트가 실제 Aruba 장비 호환성을 증명한다고 표현하지 않습니다. 자동 검증은 구현된 Parser·상태 전이·저장·UI·패키지 계약이 재현 가능하게 동작하는지를 검증합니다.

## 2. 자동 검증 실행

기본 검증:

```powershell
.\scripts\run_tests.ps1
```

주요 항목:

- pytest
- `compileall`
- `pip check`
- 비식별 fixture Parser
- Correlation / Anomaly / Incident 상태 전이
- SQLite와 설정 검증
- 가짜 SSH / 로컬 Paramiko SSH 경계
- Offscreen PySide6 UI

Windows 패키지 검증:

```powershell
.\scripts\build.ps1
```

버전 ZIP과 배포 검증:

```powershell
.\scripts\package_release.ps1 -Version <버전>
```

GitHub Actions는 정확한 CPython 3.13.15 x64 표준 GIL 환경에서 테스트와 패키지 검증을 반복합니다.

## 3. Parser 검증

각 명령은 서로 독립적으로 실패할 수 있으므로 Parser 성공/부분/실패 경계를 확인합니다.

### MM

```text
show switches
```

확인 항목:

- 정상 행 파싱
- Controller Up/Down
- 부분 행 또는 변형 헤더 처리
- 수집 실패와 실제 Down 분리

### Client 분배

```text
show lc-cluster load distribution client
```

확인 항목:

- IP별 Active / Standby
- 비식별 정상/이상 fixture
- 누락 행
- 부분 수집

### Membership

```text
show lc-cluster group-membership
```

확인 항목:

- 구성원 IP
- Connection-Type
- 최초 baseline
- 이후 변화

## 4. 상태 판단 검증

### 이상 streak

기본 정책:

```text
이상 1회 → 관찰
이상 2회 → 관찰
이상 3회 → 이상 확정
```

### 복구 streak

```text
정상 1회 → 복구 확인 중
정상 2회 → 복구 확정
```

### 저사용량 오탐 방지

특정 장비의 Active Client가 낮아도 Cluster 전체 사용량과 Peer 기준이 충분하지 않으면 장애로 확대하지 않는지 확인합니다.

### 구성원 누락

한 번 누락된 행을 즉시 장애로 만들지 않고 기본 3회 연속 누락에서 상태가 전환되는지 확인합니다.

### MM Down

정상 수집된 MM 결과의 명시적 Down은 Client streak와 별개로 즉시 장애 증거가 되는지 확인합니다.

## 5. 상관분석 검증

같은 IP에 여러 신호가 동시에 존재할 수 있습니다.

예:

```text
WLC-02
├─ MM: Up
├─ Client: 연속 이상
└─ Connection-Type: baseline 변화
```

검증에서는 다음을 확인합니다.

- 같은 IP에 원인이 병합되는지
- 문제 IP가 중복되지 않는지
- 정상 장비가 문제 IP에 섞이지 않는지
- 복수 문제 장비가 함께 표시되는지
- 수집 실패가 장애 원인으로 잘못 병합되지 않는지

## 6. 저장·재시작 검증

SQLite 관련 주요 경계:

- baseline 유지
- anomaly/recovery streak 복원
- Incident 확인/복구 상태
- 활성 장애 보존
- DB lock 제한 재시도
- 손상 상태에서 원본 보존
- 비밀 필드 저장 거부
- 저장 실패 시 UI thread 장시간 block 방지

기존 durable state를 신뢰할 수 없으면 빈 정상 상태로 계속하지 않고 자동 점검을 중지하는 fail-closed 경계를 확인합니다.

## 7. SSH 검증

자동 검증은 실제 운영 장비 대신 가짜 SSH 경계와 로컬 Paramiko SSH 서버를 사용합니다.

확인 항목:

- 허용 명령만 실행
- 호스트 키 사전 확인
- 변경된 호스트 키 자동 승인 금지
- 취소 가능한 TCP/SSH 경로
- Primary 실패 시 Fallback
- 실제 수집 Controller 기록

실제 구형 ArubaOS와 Paramiko의 KEX/서명 알고리즘 호환성은 별도 현장 검증 대상으로 남깁니다.

## 8. Windows 패키지 검증

CI와 패키지 검증기는 다음을 확인합니다.

- PyInstaller onedir 생성
- Python 설치와 무관한 EXE smoke 경로
- 필수 런타임 파일 inventory
- 금지되거나 불필요한 Qt 모듈 차단
- 외부 Python 소스 포함 경계
- 제3자 라이선스와 LGPL 자료
- SHA-256
- 버전 메타데이터

이 검증은 실제 조직 정책, EDR, Windows 알림센터 정책과 물리 모니터 배율 검수를 대체하지 않습니다.

## 9. 현장 검증 체크리스트

실제 허가된 환경에서 검증할 경우 다음을 확인합니다.

| 항목 | 확인 내용 |
|---|---|
| MM 연결 | SSH와 지문 승인 절차 정상 |
| MM 출력 | 실제 `show switches`가 Parser와 일치 |
| Cluster 연결 | Primary 조회 정상 |
| Fallback | Primary 불가 시 Fallback 수집 정상 |
| Client 분배 | 실제 Active/Standby와 화면 값 대조 |
| Membership | Connection-Type과 화면 값 대조 |
| 설정 변경 | 점검 전후 구성 변경 없음 |
| 자동 점검 | 지정 간격과 중복 실행 방지 확인 |
| 장애/복구 | 실제 또는 승인된 시험 시나리오에서 상태 전이 확인 |
| Credential Manager | 실제 Windows 사용자 범위에서 저장/조회 확인 |
| 알림/Tray | 조직 정책이 적용된 Windows에서 확인 |
| 화면 배율 | 100/125/150%와 다중 모니터 확인 |
| 클린 PC | Python 미설치 Windows 11에서 배포 폴더 실행 |

## 10. 공개 가능한 현장 검증 요약

실제 검증을 완료했더라도 공개 저장소에는 아래 정도만 기록합니다.

```text
현장 검증
- 대상 계열: Aruba Mobility Master / 7240XM
- 읽기 전용 3개 명령 수집: 확인
- MM / Client / Membership 상관분석: 확인
- Primary / Fallback: 확인
- 장비 설정 변경: 없음 확인
- Windows 배포 실행: 확인
- 실제 주소·장비명·출력 원문: 비공개
```

## 11. 공개하지 않는 정보

- 실제 IP / Hostname / 장비 별칭
- SSH 사용자명 / 비밀번호 / Enable Secret
- SSH 호스트 키 원문
- 실제 CLI 출력
- 내부 네트워크 구조
- 사이트/건물/조직 식별 정보
- 운영 SQLite 및 로그 원본

실제 운영 정보가 필요한 문제 분석은 공개 Issue가 아닌 조직의 승인된 내부 절차에서 수행해야 합니다.
