# Aruba Mini Dashboard

Aruba Mobility Master(MM)와 Aruba 7240XM 클러스터를 읽기 전용 SSH로
주기적으로 점검하고, 세 명령 결과를 장비 IP 기준으로 종합하는 Windows 11
미니 대시보드입니다. 한글 UI, 시스템 트레이, 로컬 상태 저장, 규칙 기반 장애
판단을 제공하며 외부 서버·클라우드·텔레메트리를 사용하지 않습니다.

## 구현 범위

- MM `show switches`: IP, Hostname, Status 파싱 및 Down 즉시 감지
- Cluster `show lc-cluster load distribution client`: IP별 Active/Standby,
  연속 이상·복구·구성원 누락 감지
- Cluster `show lc-cluster group-membership`: IP별 Connection-Type baseline,
  변경 사건·확인 상태·재시작 후 비교
- 같은 IP의 원인을 누적하는 정상·주의·장애·확인 불가 종합 판단
- Primary Controller 실패 시 설정 순서에 따른 Fallback 수집
- 점검 중 UI가 멈추지 않는 Worker Thread, 중복 점검 방지, 다음 점검 시각
- Windows 시스템 트레이 알림, 선택적 알림음, 확인·반복·복구 알림
- 항상 위, 40~100% 투명도, 창 위치·크기와 UI 설정 유지
- 실제 SSH를 사용하지 않는 비식별 fixture Demo 모드
- PyInstaller onedir 운영 빌드와 선택적 Console/one-file 빌드

## 안전 경계

- 운영 데이터 명령은 다음 세 개만 허용합니다.
  - `show switches`
  - `show lc-cluster load distribution client`
  - `show lc-cluster group-membership`
- 접속 세션에서는 선택한 경우 privileged EXEC 진입을 위한 `enable`과 페이징
  비활성화용 `no paging`만 추가로 사용합니다. 설정 모드에는 진입하지 않습니다.
- 비밀번호와 Enable Secret은 JSON, SQLite, 일반 로그와 배포물에 저장하지
  않습니다.
- SSH 호스트 키는 자격 증명을 보내기 전에 확인하며, 최초 키는 사용자가
  승인해야 하고 변경된 키는 자동 교체하지 않습니다.
- 접속 실패, 명령 실패, 빈 출력과 파싱 실패를 특정 WLC Down으로 판단하지
  않습니다.

## 운영 순서

1. 프로그램을 실행합니다. 초기 설정은 자동 점검 일시정지 상태입니다.
2. `설정`에서 MM 관리 IP와 정확히 4개의 Cluster 구성원 IP·별칭을 입력합니다.
3. Cluster Primary와 Fallback 순서, 포트, 제한시간, 재시도 횟수를 지정합니다.
4. MM/WLC 공통 계정 또는 별도 계정을 선택합니다.
5. 영구 저장은 Windows Credential Manager를, 종료 후 사라져야 하는 계정은
   세션 전용 방식을 선택합니다.
6. 연결 테스트에서 MM과 수집 Controller의 SSH SHA-256 지문을 관리자가
   보유한 값과 비교한 뒤 승인합니다.
7. `지금 점검`으로 결과를 확인한 다음 `자동 점검 시작`을 누릅니다.

세션 전용 자격 증명은 프로그램 재시작 후 다시 입력해야 합니다. 설정이나
자격 증명이 완전하지 않으면 자동 점검은 시작되지 않습니다.

## 기본 판단 규칙

### MM 상태

- 완전하거나 유효 행을 보존한 `show switches` 결과에서 `Status = Down`이면
  해당 IP를 즉시 장애로 판단합니다.
- 다시 확인된 `Status = Up`만 기존 MM Down의 복구 근거로 사용합니다.
- MM 접속·명령·파싱 실패 또는 행 누락은 Down이 아니라 확인 불가 또는 부분
  수집으로 표시합니다.

### Client 분배

기본 이상 조건은 다음과 같습니다.

- 해당 IP의 Active와 Standby가 각각 10 이하
- 전체 Active 합계가 50 이상
- `절대값과 상대 비교` 모드에서는 다른 구성원의 Active/Standby 중앙값이
  각각 30 이상이고, 대상 값이 각 중앙값의 25% 이하
- 위 조건이 3회 연속 유지

한 번의 저하는 streak만 증가시키며 알림을 만들지 않습니다. 모든 장비의
사용량이 낮은 경우에는 특정 IP 장애를 판단하지 않습니다. 활성 이상은 유효한
정상 값이 2회 연속 확인되면 복구됩니다. 완전한 출력에서 구성원 행이 3회
연속 사라지면 별도 누락 주의로 처리하고, 파싱 실패 회차는 누락 streak를
증가시키지 않습니다.

### Connection-Type

최초 값은 알림 없이 구성원 IP별 baseline으로 저장합니다. 수집 Controller가
Primary에서 Fallback으로 바뀌어도 같은 구성원 IP의 이전 값과 계속 비교하며,
실제 수집 Controller IP는 변화 사건의 진단 메타데이터로 별도 보존합니다.
표시 형식만 다른 동일 값은 변화로 보지 않습니다. 실제 값이 바뀌면 이전 값,
현재 값, 최초 감지·마지막 확인 시각과 확인 여부를 SQLite에 저장합니다. 같은
변화는 반복 생성하지 않으며, 다시 이전 값으로 돌아오거나 다른 값으로 바뀌면
새 사건으로 기록합니다. 행 누락은 Connection-Type 변화와 분리합니다.

### IP 종합 판단

- 장애: MM Down 또는 같은 IP에서 두 종류 이상의 이상 신호 활성
- 주의: 확정된 Client 분배 이상, Connection-Type 변화, 지속 구성원 누락
- 확인 불가: 접속·명령·파싱 실패로 필요한 데이터를 신뢰할 수 없음
- 정상: 수집이 성공했고 활성 이상 신호가 없음

주요 문제 IP는 MM Down, 동일 IP 복합 신호, Client 이상, Connection-Type
변화, 지속 누락 순으로 선택합니다. 같은 우선순위의 IP가 여러 개면 모두
표시하며, 수집 데이터가 부족하면 임의의 문제 IP를 만들지 않습니다.

## 로컬 데이터와 로그

기본 경로는 `%LOCALAPPDATA%\ArubaMiniDashboard`입니다.

- `app.db`: 최근 관측·최근 정상 상태, baseline, streak, 사건·복구·확인 상태,
  UI/알림 설정의 로컬 미러
- `config\settings.json`: 비밀정보가 없는 장비·점검 설정과 불투명 credential ID
- `known_hosts`: 사용자가 승인한 앱 전용 SSH 호스트 키
- `logs\app.log`: 원문 명령 출력을 제외한 회전형 일반 로그
- `logs\ssh_debug.log`: 사용자가 켠 경우 파싱 실패/부분 결과의 최대 2,048자
  비밀정보 마스킹 excerpt를 기록하는 진단 로그

두 로그는 파일당 최대 5MB, 백업 5개로 제한됩니다. 최근 원본 명령 출력은
세부 정보 화면에 메모리상 표시되며 일반적인 인증 프롬프트를 마스킹합니다.
SQLite에는 원본 명령 출력을 저장하지 않습니다. 원문에는 사내 IP, Hostname
등 운영 정보가 남을 수 있으므로 외부 공유 전 반드시 비식별화하십시오.

## 개발 실행

CPython 3.11.9와 repository-local 가상환경을 사용합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r .\requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m aruba_mini_dashboard.main
```

## Demo 모드

Demo는 운영 설정·자격 증명·운영 SQLite를 사용하지 않고, `tests\fixtures`를
실제 파서와 상관분석 엔진에 순서대로 입력합니다. 전체 정상, Client 일시 저하,
3회 연속 이상, Connection-Type 변화, MM Down, 2회 복구를 재현합니다.

개발 환경:

```powershell
.\scripts\run_demo.ps1
```

배포 폴더:

```powershell
.\ArubaMiniDashboard.exe --demo
```

## 테스트

```powershell
.\scripts\run_tests.ps1
```

이 스크립트는 pytest, `compileall`, `pip check`를 순서대로 실행합니다. 테스트는
비식별 fixture, 메모리/임시 SQLite, 가짜 SSH 경계, 로컬 Paramiko SSH 서버,
offscreen PySide6 UI를 사용합니다. 실제 장비 출력 파일은 IP, Hostname,
사용자명 등 민감정보를 제거한 뒤 `tests\fixtures`에 추가할 수 있습니다.

자동화 테스트의 통과는 실제 ArubaOS 버전 호환성, Windows 알림 센터 정책,
물리 화면 배율 또는 Python 미설치 클린 PC 동작의 증거를 대신하지 않습니다.

## Windows 빌드

기본 onedir/windowed 빌드:

```powershell
.\scripts\build.ps1
```

진단용 Console 또는 선택적 one-file:

```powershell
.\scripts\build.ps1 -Console
.\scripts\build.ps1 -OneFile
```

빌드 스크립트는 다음 순서로 동작합니다.

1. CPython 3.11.9 가상환경 확인 또는 생성
2. 해시 고정 의존성 설치
3. 전체 자동화 테스트 실행, 실패 시 중단
4. PyInstaller 실행
5. 문서와 설정 예제 복사
6. 금지 확장자 및 Qt 플러그인 확인
7. Python 관련 환경변수와 PATH 항목을 제거한 로컬 EXE smoke 실행
   (Netmiko·Paramiko·Windows Credential Manager 로드, 동결 fixture 탐색,
   정상 데모 1회 파싱·IP 종합 판단 포함)
8. SHA-256 파일 생성

기본 결과는 `dist\ArubaMiniDashboard\ArubaMiniDashboard.exe`입니다. onedir
폴더 전체가 배포 단위이며 내부 파일을 임의로 제거하면 안 됩니다. 패키지에는
실행에 필요한 Python runtime과 라이브러리가 포함되므로 최종 사용자 PC에
Python 설치가 필요하지 않도록 구성되어 있습니다.

## 검증 상태와 남은 현장 확인

로컬 자동화는 파서, 감지·상관분석, 저장·재시작, 알림 중복 방지, SSH
allowlist/호스트 키, Primary/Fallback, Worker Thread, UI 설정과 PyInstaller
smoke 경로를 검증합니다. Windows Credential Manager 실제 API 왕복과 로컬
Windows GUI 실행도 개발 PC 범위에서 확인할 수 있습니다.

다음 항목은 별도 현장 증거가 필요하며 이 저장소의 자동화 결과만으로 완료로
간주하지 않습니다.

- 실제 ArubaMM-HW-10K 및 7240XM ArubaOS 버전별 세 명령 원본 형식
- 실제 프롬프트, `enable`, `no paging` 지원 여부와 페이징 동작
- Primary 장애 시 실제 Fallback 수집 및 지문 승인 운영 절차
- Python이 설치되지 않은 깨끗한 Windows 11 일반 사용자 PC/VM
- 실제 100%, 125%, 150% 화면 배율과 Windows 알림 센터·트레이 정책
- 사내 보안 정책에 따른 Credential Manager 사용 허용 여부

현장 검수에는 [Windows 11 배포 검수 체크리스트](docs/WINDOWS11_QA_CHECKLIST_KO.md)를
사용하십시오.
