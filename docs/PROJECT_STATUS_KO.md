# 프로젝트 상태

이 문서는 Aruba MM / WLC 상태 모니터링의 구현 완료 범위와 별도 증거가 필요한 항목을 구분합니다.

## 구현 목표

Windows 11에서 Aruba Mobility Master와 7240XM Cluster의 상태를 읽기 전용으로 수집하고, 세 명령 결과를 장비 IP 기준으로 상관분석해 `정상 / 주의 / 장애 / 확인 불가`를 명시적인 규칙으로 표시합니다.

배포물은 PyInstaller onedir 구조로 제공하며 최종 사용자가 Python을 별도로 설치하지 않아도 되는 형태를 목표로 합니다.

## 구현 완료 범위

- [x] 비밀정보 없는 설정 모델과 Windows Credential Manager 경계
- [x] SQLite 상태·baseline·사건 저장
- [x] Netmiko SSH 어댑터
- [x] MM / Cluster Collector
- [x] Primary / Fallback 조회
- [x] `show switches` Parser
- [x] Client 분배 Parser
- [x] Group Membership Parser
- [x] MM Down과 수집 실패 분리
- [x] Client 연속 이상 / 복구 판정
- [x] 저사용량 오탐 방지
- [x] 구성원 누락 debounce
- [x] Connection-Type baseline / 변화 사건
- [x] IP 기준 복수 원인 상관분석
- [x] Incident 확인 / 복구 lifecycle
- [x] PySide6 Dashboard / 설정 / 세부 정보
- [x] Worker Thread / 중복 점검 제어
- [x] 결정적 장애 주입 / 반복 lifecycle 안정성 검증
- [x] 시스템 Tray / 알림
- [x] Demo 모드
- [x] 비식별 fixture / 가짜 SSH / UI 자동 검증
- [x] PyInstaller onedir 빌드
- [x] 패키지 inventory / SHA-256 / 제3자 라이선스 검증
- [x] GitHub Windows CI

## 운영자 확인과 외부 환경 항목

실제 대상 장비의 읽기 전용 동작은 운영자가 확인했으며 민감 원문은 공개하지
않습니다. 나머지는 구현 미완료가 아니라 배포 PC와 조직 정책에 따라 달라지는
외부 환경 항목입니다.

- [x] 실제 Aruba MM / 7240XM 접속과 세 읽기 전용 명령 수집
- [x] 실제 장비 결과의 화면 표시와 기본 운영 흐름
- [ ] Python 미설치 Windows 11 x64 일반 사용자 PC/VM 실행
- [ ] 실제 100%, 125%, 150% 배율과 다중 모니터 검수
- [ ] 조직 정책이 적용된 Windows 알림센터·Tray·Credential Manager 검수

실제 주소, 장비명, 계정, SSH 지문과 CLI 원문은 운영자 확인 근거로 사용하더라도
공개 저장소에는 기록하지 않습니다. 특정 ArubaOS의 약한 legacy 알고리즘을 자동
활성화하지 않으며, 안전한 SSH 협상 실패는 명시적 호환성 오류로 처리합니다.

## 운영 안전 경계

- 설정 변경 명령을 실행하지 않습니다.
- 설정 모드 API를 구현하지 않습니다.
- 실제 장비 접근은 승인된 환경에서만 수행합니다.
- 수집 실패를 실제 WLC Down으로 추정하지 않습니다.
- 비밀정보와 운영 원문을 공개 fixture·로그·Release에 포함하지 않습니다.
- 운영자 확인 범위를 넘어 특정 ArubaOS 버전 전체를 검증 완료로 확대 표현하지 않습니다.

## 관련 문서

- [README](../README.md)
- [프로그램 구조](ARCHITECTURE_KO.md)
- [장애 판단 로직](DETECTION_LOGIC_KO.md)
- [검증 보고서](VALIDATION_REPORT_KO.md)
- [운영 보안 모델](SECURITY_KO.md)
- [Windows QA](WINDOWS11_QA_CHECKLIST_KO.md)
