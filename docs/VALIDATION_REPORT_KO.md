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
| 저장 시 지문 일괄 승인·로그인 | ✅ 자동 검증 | 가짜 SSH / offscreen 설정 UI |
| SQLite 저장·재시작·손상 보호 | ✅ 자동 검증 | 임시/메모리 SQLite 테스트 |
| Worker Thread / 중복 점검 | ✅ 자동 검증 | PollCoordinator / UI 테스트 |
| 반복 Timeout·Disconnect·Parser·저장 실패 | ✅ 자동 검증 | 결정적 fault-injection / soak |
| PySide6 offscreen UI | ✅ 자동 검증 | GUI 테스트 |
| Windows onedir 패키지 | ✅ GitHub Actions | `ci-windows.yml` |
| 추출 후 EXE smoke | ✅ GitHub Actions | 버전 ZIP 검증 경로 |
| 실제 Aruba MM / 7240XM 읽기 전용 동작 | ✅ 운영자 확인 | 민감 원문은 공개하지 않음 |
| Python 미설치 Windows 11 운용 | ⚠️ 별도 현장 증거 | 실제 PC/VM 검증 필요 |

> 자동 테스트와 운영자 현장 확인은 서로 다른 증거입니다. 실제 대상 장비 동작은
> 운영자가 확인했으며, 자동 검증은 Parser·상태 전이·저장·UI·패키지 계약과 반복
> 장애 뒤의 프로그램 상태 복구를 재현 가능하게 검증합니다.

## 2. 자동 검증 실행

기본 검증:

```powershell
.\scripts\run_tests.ps1
```

주요 항목:

- pytest
- 1,000회 결정적 fault-injection / soak
- `compileall`
- `pip check`
- 비식별 fixture Parser
- Correlation / Anomaly / Incident 상태 전이
- SQLite와 설정 검증
- 가짜 SSH / 로컬 Paramiko SSH 경계
- Offscreen PySide6 UI

긴 안정성 반복만 별도로 실행할 수도 있습니다.

```powershell
.\.venv\Scripts\python.exe .\scripts\run_reliability_soak.py --cycles 5000
```

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
- 여러 endpoint의 원자적 일괄 승인과 자격 증명 조회 순서
- 변경된 호스트 키 자동 승인 금지
- MM 및 최소 1대 Controller 필수 로그인과 Fallback 준비 경고
- 취소 가능한 TCP/SSH 경로
- Primary 실패 시 Fallback
- 실제 수집 Controller 기록

대상 MM/7240XM의 읽기 전용 연결·수집은 운영자가 확인했습니다. 버전별 legacy
알고리즘을 추측해 자동 활성화하지 않으며, 안전한 협상이 불가능하면
`SSH_ALGORITHM_INCOMPATIBLE`로 종료하는 지원 경계를 유지합니다.

## 8. 반복 장애 주입·Soak 검증

`pytest -m reliability`는 난수에 의존하지 않는 고정 순서로 다음 실패를 반복합니다.

- Timeout, 연결 끊김, 작업 스케줄 제출 거부
- 점검 중 여러 번 누른 수동 요청의 1회 병합과 다음 회차 시작
- 연결 확인 실패·취소·지문 승인 거부·승인 후 재시도
- 설정 commit, rollback, commit marker가 남은 비정상 종료 복구
- 두 번째 자격 증명 저장 실패 시 첫 번째 임시 저장 rollback
- 여러 endpoint 호스트 키 일괄 저장의 충돌 원자성
- 제어 문자, 긴 행, 잘못된 byte와 불완전한 표가 섞인 Parser 입력

기본 pytest는 120회 경계를 빠르게 확인하고, Windows CI와 Release 빌드는 별도
1,000회 suite를 다시 실행합니다. 각 반복 뒤 worker, busy 상태, 연결 요청 map,
수동 대기 요청과 설정 transaction 임시 파일이 남지 않는지 확인합니다.

## 9. Windows 패키지 검증

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

## 10. 운영자 확인과 외부 환경 경계

실제 Aruba MM/7240XM의 접속, 읽기 전용 세 명령 수집과 화면 동작은 운영자가
확인했습니다. 이번 patch는 장비 명령과 판단 규칙을 바꾸지 않고 프로그램 내부의
반복 실패 복구만 강화하므로 장비 확인을 릴리스 전제 조건으로 다시 요구하지 않습니다.

| 항목 | 상태 |
|---|---|
| MM / 7240XM 연결과 읽기 전용 수집 | ✅ 운영자 확인 완료 |
| 실제 주소·장비명·SSH 지문·CLI 원문 | 🔒 비공개 |
| 장애 주입 뒤 프로그램 상태 복구 | ✅ 자동 반복 검증 |
| Python 미설치 클린 Windows / 조직 정책 / 실제 DPI | 외부 환경별 확인 항목 |
| 코드 서명 / EDR / 물리 모니터 | 배포 환경별 확인 항목 |

## 11. 공개 가능한 현장 검증 요약

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

## 12. 공개하지 않는 정보

- 실제 IP / Hostname / 장비 별칭
- SSH 사용자명 / 비밀번호 / Enable Secret
- SSH 호스트 키 원문
- 실제 CLI 출력
- 내부 네트워크 구조
- 사이트/건물/조직 식별 정보
- 운영 SQLite 및 로그 원본

실제 운영 정보가 필요한 문제 분석은 공개 Issue가 아닌 조직의 승인된 내부 절차에서 수행해야 합니다.
