# Aruba Mini Dashboard 구현 계획 및 상태

## 목표

Windows 11에서 Aruba Mobility Master의 `show switches`와 Aruba 7240XM
클러스터의 Client 분배 및 Group Membership 결과를 읽기 전용으로 수집하고,
IP 기준으로 상관분석하여 정상·주의·장애·확인 불가를 명시적인 규칙으로
표시한다. 배포물은 Python이 없는 일반 사용자 PC에서 인터넷 연결 없이 실행할
수 있도록 PyInstaller onedir로 구성한다.

## 운영 흐름

1. 운영자가 MM 및 WLC 4개와 Primary/Fallback을 설정한다.
2. 공통/별도, Credential Manager/세션 전용 자격 증명을 선택한다.
3. 인증정보 전송 전에 SSH SHA-256 지문을 확인하고 승인한다.
4. `지금 점검`으로 초기 결과를 검토한 뒤 자동 점검을 시작한다.
5. 상단 종합 상태와 IP별 표를 확인하고, 필요 시 세부 정보·파싱 결과·최근
   원본을 조회한다.
6. 사건을 확인해 반복 알림을 멈추되 감시는 계속하며, 복구는 별도 기록한다.

설정은 비밀정보가 없는 JSON과 SQLite 설정 미러에 저장하고, 상태 baseline,
streak, 사건은 SQLite에 저장한다. 비밀번호와 Enable Secret은 Windows
Credential Manager 또는 프로세스 메모리에만 둔다. 외부 전송·내보내기는
구현하지 않는다.

## 단계별 구현 상태

1. [x] 기존 다중 프로젝트 작업공간 확인 및 독립 프로젝트 구조 확정
2. [x] 설정 모델, 공통 데이터 모델, SQLite v4 저장소, 회전 로그
3. [x] Netmiko SSH 어댑터, MM/Cluster 수집기, Primary/Fallback
4. [x] 세 명령 파서, ANSI·페이징·헤더 변형·부분 행 처리, 비식별 fixture
5. [x] Client 연속 이상·복구, 구성원 누락, Connection-Type baseline·사건
6. [x] IP 상관분석, 주요/복수 문제 IP, 장애와 수집 실패 분리
7. [x] PySide6 대시보드, 설정·세부 정보, Worker Thread, 시스템 트레이
8. [x] 항상 위, 투명도, 창 위치, 알림·알림음·확인·복구
9. [x] 독립 Demo, 단위·통합·UI 자동화, 오류 처리 보강
10. [x] PyInstaller spec, build/test/package 검증 스크립트, 배포 문서
11. [x] Essentials 전용 Qt 런타임, 제3자 고지, Windows PE 버전 리소스,
    versioned ZIP/SHA-256, GitHub Windows CI와 공개 배포 차단 workflow
12. [x] onedir 전용 배포, Qt exact inventory,
    PySide6/shiboken6/Paramiko/scp 외부 원본 소스와 PYZ 비포함 검증,
    LGPL 런타임 교체·복구 안내

## 자동화 검증 범위

- 모든 fixture의 fail-closed 파싱과 일부 깨진 행 보존
- MM Down 즉시 감지, 수집 실패·부분 수집 분리
- Client 3회 활성, 2회 복구, 전체 저사용량 오탐 방지, 누락 debounce
- 구성원 IP별 Connection-Type baseline, 수집 Controller 전환을 넘는 비교,
  변경·확인·재시작·중복 방지
- SQLite WAL, lock 재시도, 손상 원본 보존, 비밀 필드 거부
- 동일 원인 사건·알림 중복 방지, 확인과 복구 lifecycle
- Strict known_hosts, 사전 지문 확인, 명령 allowlist, Fallback 수집
- 로컬 Paramiko SSH 서버와 실제 Netmiko 어댑터 통합 경로
- Poll Worker Thread와 중복 점검 skip/coalesce
- offscreen UI, 투명도·항상 위·트레이 대체 경로, 1.0/1.25/1.5 scale 계산
- PyInstaller 산출물 구성과 Python 환경을 제거한 로컬 smoke 경로
- ZIP 안전 경로·대소문자 충돌·민감 파일·미사용 Qt 모듈 차단과 격리 추출
- 런타임 잠금 파일과 제3자 고지 정합성, Windows PE 제품/버전 메타데이터
- GitHub Actions의 정확한 CPython 3.13.15 x64 표준 GIL runtime, immutable annotated tag와 자산 digest
  재검증 경로

## 외부 검증 대기 항목

아래 항목은 구현 미완료가 아니라 현재 사용할 수 없는 현장 증거의 경계이다.
체크리스트에서 실제 확인하기 전에는 검증 완료로 표현하지 않는다.

- [ ] 실제 ArubaMM-HW-10K `show switches` 비식별 출력 fixture
- [ ] 실제 Aruba 7240XM 두 Cluster 명령 비식별 출력 fixture
- [ ] 실제 ArubaOS 버전·프롬프트·페이징·Enable 동작 확인
- [ ] 실제 장비 read-only 수집과 Primary/Fallback 장애 전환 확인
- [ ] Python 미설치 클린 Windows 11 x64 일반 사용자 PC/VM 실행
- [ ] 실제 100%, 125%, 150% 배율 시각 검수
- [ ] 조직 정책이 적용된 Windows 알림 센터·트레이·Credential Manager 검수

## 안전 경계

- 장비 설정 변경 명령과 설정 모드 API는 구현하지 않는다.
- 실제 장비 접근은 별도 승인과 접속 정보가 있을 때만 수행한다.
- fixture와 로컬 SSH 테스트 결과를 실장비 호환성 증거로 표현하지 않는다.
- 비밀정보와 운영 원문은 저장·로그·배포 검사에서 fail-closed로 보호한다.
- Git push, tag와 배포 게시 작업은 별도 승인 없이 수행하지 않는다.
- 저작권자 배포 조건 결정과 필요한 검토 전에는 Actions artifact, Draft 또는
  Prerelease를 포함한 GitHub 바이너리 전달을 차단한다.
- 그 경계를 해제한 뒤에도 실제 장비와 클린 Windows 증거 전에는 GitHub
  배포를 Stable로 표시하지 않는다.
- 격리된 `v0.1.0` 태그/Release 자산을 덮어쓰거나 다시 게시하지 않는다.
