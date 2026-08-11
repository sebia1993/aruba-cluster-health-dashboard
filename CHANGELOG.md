# 변경 이력

## 0.1.1 - 2026-08-11

### 변경

- 런타임 Qt 의존성을 `PySide6-Essentials`로 축소하고 사용하지 않는 Qt
  Virtual Keyboard, PDF, QML/Quick 및 OpenGL 구성 요소를 배포 대상에서 제외
- Windows 실행 파일에 제품명, 파일 설명, 원본 파일명과 버전 리소스 추가
- 버전이 포함된 Windows x64 ZIP, SHA-256 파일 및 안전한 압축 해제 검증 경로 추가
- GitHub 수동 Windows 배포 workflow와 일반 변경 검증 workflow 추가. 배포
  workflow는 저작권자 조건 결정 전 Actions artifact와 Draft 생성을 포함한 모든
  바이너리 전달을 첫 단계에서 차단
- 배포물의 제3자 고지와 런타임 라이선스 문서 구성 추가
- PySide6/shiboken6/Paramiko/scp를 PYZ 밖의 교체 가능한 원본 소스로 제공하고 exact source,
  라이선스 해시, Qt runtime inventory 및 교체·복구 안내를 검증하는 onedir 전용
  배포 경계 추가
- 설정·자격 증명·DB·로그·known_hosts·키·인증서·덤프 파일 및 안전하지 않은
  ZIP 경로를 거부하도록 패키지 verifier 강화
- 저장소 보안 제보 절차와 릴리스 운영 문서 추가

### 수정

- 최초 `v0.1.0` 배포물에 실제 UI에서 사용하지 않는 Qt Virtual Keyboard
  바이너리가 포함되던 문제 수정. 해당 GitHub Release는 Draft로 격리하고
  수정된 배포물은 새로운 `v0.1.1` 태그로만 생성
- Python/SSH/Credential 의존성, 동결 fixture와 Demo 종합 판단을 포함하는
  EXE smoke 검증을 ZIP 검증 단계에서도 수행

### 검증 경계

- 이 버전도 실제 Aruba 장비, Python 미설치 클린 Windows 11 일반 사용자
  PC/VM, 실제 100%/125%/150% 화면 배율과 조직 정책이 적용된 Windows 알림은
  별도 현장 검수가 필요하다. 저작권자 배포 조건 결정 전에는 Prerelease도
  공개하지 않으며, 이후 승인되더라도 현장 검수 전에는 Stable로 표시하지 않는다.

## 0.1.0 - 2026-08-11

### 추가

- Aruba MM `show switches`와 Aruba 7240XM Cluster 두 명령의 분리형 수집기와
  견고한 표 파서
- IP별 `DeviceHealth`, 명시적 심각도 규칙, 주요/복수 문제 IP와 근거 표시
- Client 절대/상대 비교, 연속 활성·복구, 전체 저사용량·구성원 누락 처리
- 구성원 IP별 Connection-Type baseline, Primary/Fallback 전환을 넘는 비교,
  변경 사건, 확인과 재시작 유지
- 사건 활성·갱신·복구 journal과 동일 원인 알림 중복 방지
- Primary 실패 시 Fallback 순차 시도 및 실제 수집 Controller 기록
- Windows Credential Manager 영구 자격 증명과 세션 전용 자격 증명
- 사전 SSH SHA-256 지문 승인, 앱 전용 known_hosts와 변경 키 거부
- PySide6 한글 미니 대시보드, 설정·세부 정보, Worker Thread, 시스템 트레이
- 항상 위, 40~100% 투명도, 창 위치·크기와 알림 설정 저장
- 신규/반복/복구 Windows 트레이 알림, 선택적 알림음과 사용자 확인
- 운영 저장소와 분리된 8단계 fixture Demo 모드
- PyInstaller onedir 기본 빌드, 선택적 Console, 패키지 verifier와
  SHA-256 생성
- Windows 11 현장 검수 체크리스트와 보안·운영 문서

### 신뢰성 및 보안

- 허용된 세 show 명령 외 운영 명령 거부, 설정 모드 미사용
- 접속·명령·파싱 실패를 장비 Down으로 오판하지 않는 fail-closed 처리
- 설정 JSON 원자 저장, SQLite WAL·bounded lock 재시도와 손상 원본 보존
- baseline·변경·사건·journal의 단일 트랜잭션 저장 경로
- JSON·SQLite 비밀 필드 거부와 로그 중앙 마스킹
- 일반 로그의 명령 원문 제외, opt-in 파싱 오류 excerpt만 제한 기록
- PyInstaller 패키지에서 소스·DB·로그·자격 증명 파일과 필수 Qt 플러그인 검사

### 검증 경계

- 자동화는 비식별 fixture, 가짜/로컬 SSH, 임시 저장소와 offscreen UI를
  사용한다.
- 실제 Aruba 장비·ArubaOS 출력 호환성과 Python 미설치 클린 Windows 11
  PC/VM 검수는 아직 현장 증거가 필요하다.
