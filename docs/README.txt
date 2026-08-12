Aruba Mini Dashboard 0.2.0
==========================

용도
----
Aruba Mobility Master와 Aruba 7240XM Cluster의 세 show 명령 결과를 장비 IP
기준으로 종합하는 Windows 11 로컬 미니 대시보드입니다. 외부 서버, 클라우드,
텔레메트리를 사용하지 않습니다.

처음 설정
---------
1. 배포 폴더 전체를 로컬 디스크에 복사하고 ArubaMiniDashboard.exe를
   실행합니다. 첫 실행은 자동 점검 일시정지 상태입니다.
2. 메인 화면의 "설정"을 열어 MM 관리 IP와 정확히 4개의 WLC IP/별칭을
   입력합니다.
3. Cluster Primary와 Fallback 순서, SSH 포트, 연결/명령 timeout과 재시도
   횟수를 입력합니다.
4. MM/WLC 공통 계정 또는 별도 계정을 선택합니다.
5. 영구 계정은 Windows Credential Manager, 일회성 계정은 세션 전용 저장을
   선택합니다. 세션 전용 계정은 프로그램 재시작 후 다시 입력해야 합니다.
6. 연결 테스트를 실행합니다. 표시된 SSH SHA-256 지문을 네트워크 담당자가
   보유한 값과 비교한 뒤 승인합니다. 변경된 지문은 원인을 확인하기 전까지
   승인하지 마십시오.
7. "지금 점검"으로 결과를 확인한 뒤 "자동 점검 시작"을 누릅니다.

운영
----
- 기본 크기와 작은 창에서는 등록한 WLC 4대의 MM 보고 상태, Client 분배 상태와
  마지막 점검 시각만 표시됩니다.
- 창을 최대화하거나 충분히 넓히면 등록·미등록 장비의 Active/Standby,
  Connection-Type과 전체 상태가 표시됩니다. 미등록 장비는 감시 제외 정보로만
  표시되며 장애 판단과 알림에 포함되지 않습니다.
- 감시 대상은 항상 설정의 Cluster 구성원 4개 IP입니다. MM에서 발견한 다른
  장비가 감시 대상으로 자동 승격되지 않습니다.
- IP 행을 선택하면 현재·이전 값, streak, 사건 시각, 파싱 결과와 최근 원본을
  확인할 수 있습니다.
- "알림 확인"은 반복 알림을 멈추지만 감시를 중지하지 않습니다.
- 창 닫기(X)는 시스템 트레이가 사용 가능하면 창만 숨깁니다. 완전 종료는
  트레이 메뉴의 "종료"를 사용합니다.
- 트레이를 사용할 수 없는 환경에서 창 닫기는 안전한 프로그램 종료를
  요청합니다.
- 설정의 숫자·선택 항목은 직접 클릭한 뒤에만 마우스 휠로 변경됩니다. Tab 이동
  또는 커서를 올려둔 상태의 휠은 값을 변경하지 않습니다. 투명도는 휠 변경을
  항상 차단합니다.

안전
----
운영 출력은 다음 세 명령만 수집합니다.

  show switches
  show lc-cluster load distribution client
  show lc-cluster group-membership

선택한 경우 세션에서 enable과 no paging을 사용할 수 있지만 설정 모드에
진입하거나 장비 구성을 변경하지 않습니다. 비밀번호와 Enable Secret은 설정
JSON, SQLite, 일반 로그와 배포 폴더에 저장하지 않습니다.

접속·명령·파싱 실패는 WLC Down이 아닙니다. 화면에 "확인 불가" 또는 부분
수집으로 표시되면 임의로 특정 장비 장애로 해석하지 말고 오류 코드와 파싱
상태를 확인하십시오.

배포 폴더의 LICENSE.txt는 Aruba Mini Dashboard 자체에 적용되는 MIT License
원문이며 루트 LICENSE와 바이트 단위로 동일합니다. 이 라이선스는 함께 제공되는
제3자 구성요소의 저작권이나 별도 라이선스를 대체하지 않습니다.
THIRD_PARTY_NOTICES.txt에는 Python, Qt/PySide, SSH 및 암호화 런타임의 제3자
고지와 라이선스 원문이 포함되어 있습니다. ArubaMiniDashboard.exe와
`_internal` 라이브러리를 분리하거나 수정해서 다시 배포하기 전에 각각의 적용
조건을 확인하십시오.

QT_THIRD_PARTY_NOTICES.txt와 QT_RUNTIME_INVENTORY.json은 실제 Qt DLL/plugin
목록과 해시를 기록합니다. LGPL_RUNTIME_INVENTORY.json과
LGPL_RUNTIME_LICENSES 폴더는 외부 소스로 제공되는 Paramiko 4.0.0 및 scp
0.16.1의 파일·버전·라이선스 증거를 기록합니다. Aruba Mini Dashboard의 배포
조건은 사용자가 LGPL 구성요소를 자신의 용도로 수정하거나 그 수정 사항을
디버깅하기 위해 리버스 엔지니어링하는 것을 제한하지 않습니다. 교체와 복구
방법은 LGPL_RUNTIME_REPLACEMENT_KO_EN.md를 확인하십시오. one-file/EXE 단독
배포는 지원하지 않습니다.

Demo
----
실제 장비와 자격 증명 없이 동작을 확인하려면 다음과 같이 실행합니다.

  ArubaMiniDashboard.exe --demo

Demo는 운영 설정과 운영 SQLite를 사용하지 않습니다.

로그
----
기본 데이터 경로:

  %LOCALAPPDATA%\ArubaMiniDashboard

일반 오류는 logs\app.log에서 확인합니다. ssh_debug.log는 기본 비활성이며,
활성화한 경우 파싱 실패/부분 결과의 제한된 마스킹 excerpt만 기록합니다.
세부 화면의 최근 원본과 로그에는 사내 IP/Hostname 등 운영 정보가 남을 수
있으므로 외부 공유 전에 반드시 비식별화하십시오.

현장 검증 주의
--------------
기본 fixture는 비식별 예시이므로 실제 ArubaOS 버전의 출력 형식과 다를 수
있습니다. 파싱 실패를 장비 Down으로 해석하지 말고, 비식별화한 실제 출력을
tests\fixtures에 추가해 parser 테스트를 갱신하십시오.

이 배포물은 Python이 없는 PC를 목표로 패키징되지만, 현장 배포 전 반드시
WINDOWS11_QA_CHECKLIST_KO.md를 사용하여 Python 미설치 Windows 11 일반 사용자
PC/VM, 화면 배율, 알림·트레이, 실제 read-only SSH를 검수하십시오.
