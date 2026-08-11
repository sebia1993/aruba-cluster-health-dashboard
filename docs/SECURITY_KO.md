# 보안 설계

## 보안 목표와 범위

Aruba Mini Dashboard는 사용자가 등록한 MM/WLC에 읽기 전용 상태 명령을
실행하는 로컬 Windows 프로그램입니다. 외부 서버, 클라우드, 업데이트 조회,
분석 SDK와 텔레메트리를 포함하지 않습니다. 관리자 권한과 장비 설정 변경
권한을 요구하지 않도록 설계했지만, 조직의 Windows·네트워크 정책은 별도로
적용됩니다.

## 자격 증명

- 영구 계정은 현재 Windows 사용자 범위의 Credential Manager Generic
  Credential에 저장합니다.
- 설정 JSON과 SQLite에는 UUID 형식 credential ID만 저장합니다.
- 사용자명은 Credential Manager의 `UserName`, 비밀번호와 Enable Secret은
  Credential blob에 저장하며 JSON·SQLite에 복제하지 않습니다.
- 세션 전용 모드는 프로세스 메모리에서만 사용하고 프로그램 종료 후 복원하지
  않습니다.
- MM과 WLC는 공통 계정 또는 각각의 별도 계정을 사용할 수 있습니다.
- 앱은 자신이 참조하는 credential ID만 읽고 삭제하며, 관련 없는 Windows
  Credential을 열거하거나 정리하지 않습니다.

Credential Manager는 Windows 로그온 사용자 경계의 보호 수단입니다. 해당
Windows 계정 자체가 탈취되거나 악성 프로세스가 같은 사용자 권한으로 실행되는
상황까지 방어하지 못하므로 화면 잠금, 최소 권한의 장비 읽기 전용 계정과
Endpoint 보안 정책이 필요합니다. 조직 정책상 영구 저장이 금지되면 세션 전용
모드를 사용하십시오.

## SSH 호스트 키

- 자격 증명을 읽거나 전송하기 전에 서버 공개 키의 SHA-256 지문을 스캔합니다.
- 사용자가 네트워크 담당자가 보유한 값과 비교해 승인한 키만 앱 전용
  `%LOCALAPPDATA%\ArubaMiniDashboard\known_hosts`에 기록합니다.
- 저장된 키와 다른 키는 자동 승인·교체하거나 Fallback으로 우회하지 않습니다.
- Cluster 연결 테스트는 Primary가 응답하지 않을 때 다음 Fallback의 지문
  확인을 계속하지만, 응답한 각 Controller는 독립적으로 승인되어야 합니다.

호스트 키 변경은 장비 교체·재설치일 수도 있고 중간자 공격 징후일 수도
있습니다. 원인을 확인하지 않은 채 `known_hosts`를 삭제하거나 교체하지
마십시오.

## 장비 명령 제한

운영 출력 수집 allowlist는 다음 세 명령뿐입니다.

- `show switches`
- `show lc-cluster load distribution client`
- `show lc-cluster group-membership`

선택한 경우 privileged EXEC에 들어가기 위한 `enable`과 해당 SSH 세션의
페이징을 끄는 `no paging`이 추가로 사용됩니다. `no paging`이 지원되지 않으면
출력 크기·시간·프롬프트를 제한한 Space 페이저 처리로 전환합니다. 설정 모드
진입, 구성 저장, 삭제·재부팅·동기화 명령은 구현하지 않았습니다.

## 로컬 저장소

- `settings.json`: 비밀정보가 없는 endpoint, threshold, UI/알림 설정과
  credential ID
- `app.db`: 파싱 상태, 최근 관측·최근 정상 상태, baseline, streak,
  사건·복구·확인 상태와 failover 기록
- `known_hosts`: 승인된 호스트 공개 키
- `logs\app.log`: 명령 원문을 제외한 상태·오류 코드
- `logs\ssh_debug.log`: 사용자가 활성화한 동안 파싱 실패/부분 결과의 제한된
  excerpt

SQLite에는 사용자명, 비밀번호, Enable Secret, credential blob과 원본 명령
출력을 저장하지 않습니다. 저장 API는 비밀 필드 이름을 거부합니다. 설정은
임시 파일 작성 후 원자 교체하고, 손상된 JSON이나 SQLite는 원본을 덮어쓰지
않은 채 자동 점검을 멈춥니다.

## 로그와 화면 원문

- 두 로그는 파일당 최대 5MB, 백업 5개로 회전합니다.
- 일반 로그에는 장비 명령 원문을 쓰지 않습니다.
- SSH 디버그 로그는 기본 비활성이며, 파싱 실패/부분 결과에서 ANSI·페이징·
  흔한 credential 줄을 정리한 최대 2,048자 excerpt만 기록합니다.
- 등록된 사용자명·비밀번호·Enable Secret과 흔한 key/value 비밀 형식은 공통
  마스커를 통과합니다.
- 세부 정보 화면의 최근 원본은 메모리상 데이터이며 흔한 인증 프롬프트를
  방어적으로 마스킹합니다. 재시작용 SQLite에는 저장하지 않습니다.

마스킹은 모든 사내 운영 정보를 익명화하는 기능이 아닙니다. IP, Hostname,
장비 별칭, 토폴로지와 사용자 정의 출력은 남을 수 있으므로 로그·스크린샷·
fixture를 외부 공유하기 전에 별도로 비식별화하십시오.

## 수집 실패의 안전한 처리

SSH 인증 실패, timeout, 명령 거부, 빈 출력, 파싱 실패와 일부 데이터만 수집된
상태는 장비 Down과 분리합니다. MM Down의 복구도 다시 신뢰 가능한 Up 행을
확인했을 때만 기록합니다. 불완전한 회차는 Client/누락 streak를 임의로
증가시키지 않아 모니터링 장애가 네트워크 장애로 오인되지 않게 합니다.

## 배포 검사

패키지 verifier는 onedir 산출물에서 앱·개발용 `.py`, 모든 `.pyc`, `.db`,
`.log`, `.cred`, `.key` 파일을 거부합니다. LGPL 런타임을 교체할 수 있도록
manifest에 고정된 PySide6/shiboken6/Paramiko/scp 원본 `.py` 경로만 예외로 허용하며, 각 파일
해시와 라이선스 증거가 일치하고 같은 모듈이 EXE의 PYZ에 중복 포함되지 않았는지
확인합니다. Qt `qwindows.dll`, `qsvg.dll`, 검토된 DLL/plugin exact inventory와
필수 문서도 함께 검사합니다. 루트 MIT `LICENSE`는 배포물의 `LICENSE.txt`와
바이트 단위로 일치해야 하며 누락이나 변경 시 검증이 실패합니다. MIT 라이선스는
제3자 구성요소의 별도 저작권·라이선스를 대체하지 않고, 프로젝트 배포 조건도
LGPL 구성요소의 자체 사용 목적 수정이나 해당 수정 디버깅을 위한 리버스
엔지니어링을 제한하지 않습니다. 또한 Python 관련 환경변수와 PATH 항목을 제거한
로컬 환경에서 EXE smoke를 실행합니다. 이 검사는 clean VM 증거를 대신하지
않으므로 최종 배포 전 Python이 없는 Windows 11 일반 사용자 환경에서 별도
검수해야 합니다.
