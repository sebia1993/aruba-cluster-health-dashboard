# 자동 Controller 장애조치

## 목적과 기본 상태

자동 장애조치는 기존 Aruba MM/WLC **읽기 전용 자동점검과 분리된 선택 기능**입니다.
기본값은 `OFF`이며 사용자가 위험 고지를 확인하고 직접 켠 경우에만 다음 두 명령을
실행할 수 있습니다.

```text
reload force
cluster-debug bucketmap rebalance
```

기존 조회 명령 allowlist에는 위 명령을 추가하지 않습니다. 변경 명령은 별도의 SSH
경계에서 정확히 두 문자열만 허용하며 설정 모드, 임의 명령, 사용자 입력 명령을
실행하지 않습니다.

## 자동조치 흐름

1. 완전하게 수집·파싱된 MM `show switches`에서 등록 Controller 한 대의 명시적인
   `Down`을 감지합니다.
2. 실행 직전에 MM 상태를 다시 확인합니다. 대상이 달라졌거나 여러 대가 Down이면
   조치를 시작하지 않습니다.
3. Down Controller에 SSH 접속을 최대 3회 시도합니다. 인증·호스트 키·권한 오류는
   즉시 중단하고, 일시적인 연결 오류만 설정된 간격으로 재시도합니다.
4. 접속에 성공하면 대상 Controller에서 `reload force`를 한 번만 전송합니다.
   전송 후 SSH 종료는 정상적인 재부팅 가능성으로 기록합니다. 결과가 불명확해도
   같은 장애에서 자동 재전송하지 않습니다.
5. MM `show switches`로 대상 Controller가 `Up`이 될 때까지 제한시간 동안 확인합니다.
6. Cluster Membership에서 현재 Leader가 정확히 한 대인지 다시 찾고, 그 Leader에
   직접 접속해 대상 Controller와 모든 등록 구성원이 `CONNECTED`인지 연속 확인합니다.
7. 현재 Leader에서 `cluster-debug bucketmap rebalance`를 한 번만 실행합니다.
8. 출력의 독립된 행이 정확히 `Cluster rebalance triggered`일 때 재분배 요청 성공으로
   기록합니다. 이 문구는 재분배 **완료**가 아니라 정상적인 **요청 시작**의 증거입니다.
9. 모든 등록 Controller의 MM `Up`, Membership `CONNECTED`, Leader 한 대, Client
   Distribution 행 복원을 연속 확인한 뒤 종료합니다.
10. 성공·부분 완료·실패·중단과 관계없이 단계별 타임라인과 전후 상태가 포함된 단일
    HTML 장애조치 보고서를 생성합니다.

## ON/OFF 동작

- `OFF`: 기존 읽기 전용 점검과 장애 알림만 수행합니다.
- `ON`: 신뢰 가능한 단일 Controller Down에 대해 자동 장애조치를 수행합니다.
- 조치 시작 전 `OFF`: 즉시 취소합니다.
- `reload force` 전송 후 `OFF`: 이미 전송한 명령은 되돌릴 수 없으므로 추가 변경
  명령을 금지하고 중단 보고서를 생성합니다.
- 조치 중 기존 자동점검: 예약 점검을 잠시 멈추고 장애조치 상태 머신이 필요한
  읽기 전용 확인을 전담합니다. 종료 후 이전 자동점검 상태를 복원합니다.

## 중복 실행 방지

별도 SQLite 감사 저장소에 Controller별 잠금과 명령 전송 예약을 먼저 저장합니다.

```text
%LOCALAPPDATA%\ArubaMiniDashboard\remediation\remediation.db
```

- 같은 장애에서 `reload force` 최대 1회
- 같은 장애에서 재분배 최대 1회
- 명령 전송 직전에 영구 예약을 먼저 저장
- 프로그램이 비정상 종료되어도 동일 Controller를 자동 재부팅하지 않음
- 이후 신뢰 가능한 MM 결과에서 대상 Controller가 `Up`이고 활성 작업이 없을 때만
  대상 잠금을 해제

## HTML 보고서

```text
%LOCALAPPDATA%\ArubaMiniDashboard\remediation\reports
```

보고서는 외부 CDN을 사용하지 않는 UTF-8 단일 HTML이며 A4 인쇄에 대응합니다.

- 상급보고용 결과와 핵심 소요시간
- 장애 감지부터 종료까지 단계별 타임라인
- 조치 전·후 MM, Membership, Active/Standby Client 비교
- 대상 Controller 및 실제 실행 Leader
- 실행 명령과 제한된 정형 결과 증거
- 자동 확인 범위, 확인할 수 없는 근본 원인, 후속 권고

비밀번호, Enable Secret, Credential ID, 전체 SSH 원문은 보고서와 SQLite에 저장하지
않습니다.

## 자동 실행 차단 조건

- MM 수집 또는 파싱이 완전하지 않음
- 등록 Controller 두 대 이상이 동시에 Down
- Down IP가 설정된 Cluster Member가 아님
- SSH Host Key 불일치 또는 미승인
- 인증·Enable·권한 오류
- 현재 Leader가 없거나 두 대 이상
- Leader 출력에서 대상 Controller가 `CONNECTED`가 아님
- 동일 Controller에 이전 자동조치 잠금이 남아 있음
- 프로그램 종료 또는 사용자가 기능을 끔

## 검증 경계

자동 테스트는 가짜 SSH, 비식별 Parser 출력, 임시 SQLite와 HTML을 사용해 명령
allowlist, 3회 접속 경계, 상태 전이, 중복 실행 방지와 보고서 escaping을 검증합니다.
실제 Aruba MM/7240XM의 재부팅 시간, 장비별 출력, 서비스 영향과 운영 권한은 승인된
현장 환경에서 별도로 확인해야 합니다.
