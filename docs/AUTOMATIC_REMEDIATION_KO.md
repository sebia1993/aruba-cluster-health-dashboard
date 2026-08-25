# 자동 Controller 장애조치

## 목적과 기본 경계

자동 장애조치는 기존 MM/WLC 읽기 전용 점검과 분리된 선택 기능입니다. 기본값은
`OFF`이며 사용자가 명시적으로 켠 경우에만 아래 두 변경 명령을 실행합니다.

```text
reload force
cluster-debug bucketmap rebalance
```

기존 `READ_ONLY_COMMANDS`에는 변경 명령을 추가하지 않습니다. 자동 장애조치 패키지는
별도 SSH allowlist, 별도 SQLite 감사 저장소, 별도 HTML 보고서 경계를 사용합니다.

## v0.6.0 실행 흐름

1. 신뢰 가능한 MM `show switches` 결과에서 등록 Controller 한 대의 명시적 `Down`을 감지합니다.
2. 로컬 SQLite 무결성·원자적 쓰기, 보고서 폴더 쓰기와 실행 설정 지문을 확인합니다.
3. 실행 직전 MM 결과를 다시 수집해 최초 Down 대상과 일치하는지 확인합니다.
4. 대상 Controller SSH를 최대 3회 시도합니다. 인증·Host Key 오류는 즉시 중단합니다.
5. `reload force` 쓰기 슬롯을 SQLite에 먼저 예약하고 명령을 한 번만 시도합니다.
6. MM에서 대상이 `Up`이 될 때까지 제한시간 동안 대기합니다.
7. 현재 Leader를 탐색하고 Leader 출력에서 대상과 전체 구성원의 `CONNECTED`를 연속 확인합니다.
8. 현재 Leader에 재분배용 SSH 세션을 연결합니다.
9. **동일 SSH 세션**에서 `show lc-cluster group-membership`을 다시 실행하고, MM 전체
   Controller가 여전히 `Up`인지 재확인합니다.
10. 최종 Gate를 통과하면 재분배 쓰기 슬롯을 SQLite에 예약하고
    `cluster-debug bucketmap rebalance`를 한 번만 실행합니다.
11. `Cluster rebalance triggered`가 독립 행으로 정확히 나타나는지 확인합니다.
12. 전체 MM Up, Membership Connected, Leader 1대, Client Distribution 행 존재를 연속 확인합니다.
13. 모든 단계와 결과를 한국 표준시(KST, UTC+09:00) 타임라인과 HTML 보고서로 생성합니다.

## 쓰기 단계와 중복 방지

각 변경 명령은 다음 단계로 기록합니다.

```text
not_attempted
reserved
write_attempted
write_returned
response_observed
```

`reserved` 이후 프로그램이 종료되거나 SSH 결과가 불명확해도 동일 장애에서 명령을
자동 재전송하지 않습니다. 실행 상태, 이벤트, 대상 잠금과 스냅샷은 하나의 SQLite
트랜잭션으로 저장합니다.

## Circuit Breaker

- Cluster 전체 자동조치 기본 냉각시간: 30분
- 동일 Controller 기본 24시간 한도: 2회
- 비정상 종료 또는 결과 불명확 시 대상 잠금 유지
- 잠금은 신뢰 가능한 MM/Controller 정상 상태가 기본 3회 연속 확인된 뒤에만 해제
- 실패·부분 완료·운영자 중단 시 자동 장애조치 기본 일시정지
- 다중 Controller Down에서는 변경 명령 실행 금지

## 보고서

보고서의 모든 시간은 고정 KST로 표시하며 Windows IANA 시간대 데이터 유무에 따라
UTC로 조용히 변경되지 않습니다. 파일은 임시 파일, `fsync`, 원자적 교체로 생성합니다.

보고서 생성 실패 시 조치 결과는 SQLite에 보존하고 `report_pending`으로 표시합니다.
다음 프로그램 시작 시 미완료 보고서를 다시 생성합니다.

저장 위치:

```text
%LOCALAPPDATA%\ArubaMiniDashboard\remediation\reports
```

보고서와 SQLite에는 비밀번호, Enable Secret, Credential ID와 전체 SSH 원문을
저장하지 않습니다.

## 구조

```text
remediation/
├─ backend.py              읽기 전용 증거 수집과 최종 Gate
├─ controller.py           애플리케이션 서비스 및 Worker 수명주기
├─ models.py               상태·증거·쓰기 단계 모델
├─ operation_registry.py   모든 remediation SSH 연결의 취소 권한
├─ report.py               KST 단일 HTML 보고서
├─ repository.py           원자적 감사 저장소·잠금·Circuit Breaker
├─ settings.py             버전형 설정과 v1→v2 마이그레이션
├─ ssh_actions.py          정확한 두 변경 명령 실행 경계
├─ timebase.py             고정 KST 시간 기준
├─ ui_panel.py             UI 표시·입력 전용 컴포넌트
└─ workflow.py             Fail-closed 상태 머신
```

`MainWindow` 런타임 교체 방식은 제거했습니다. `main.py`가 기존 `MainWindow`를 생성한
뒤 `RemediationFeatureController`를 명시적으로 합성하고, 종료 시에도 명시적으로
정리합니다.

## 현장 검증

자동 테스트는 Fake SSH, 합성 MM/Cluster 출력, 임시 SQLite와 offscreen Qt를 사용합니다.
실제 Aruba 장비에서는 다음을 확인해야 합니다.

- 장비별 재부팅 소요시간
- `reload force` 실행 직후 SSH 종료 형태
- 현재 ArubaOS의 Membership 출력 형식
- 실제 Leader 변경 시점
- 재분배 응답과 Client 이동 시간
- 운영 계정 권한과 사내 변경 절차
