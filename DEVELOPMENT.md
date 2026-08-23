# 개발 및 유지관리 기준

이 문서는 Aruba MM / WLC 상태 모니터링 프로젝트를 수정할 때 유지해야 할 구조·안전·검증 기준을 정리합니다.

## 1. 프로젝트 목적

이 프로젝트는 Windows 11에서 Aruba Mobility Master와 Aruba 7240XM Cluster 상태를 읽기 전용 SSH로 수집하고, 여러 명령의 결과를 IP 기준으로 상관분석해 운영자에게 종합 상태를 제공하는 도구입니다.

개발 편의보다 **운영망 안전, 오탐 방지, 재현 가능한 검증, 민감정보 비노출**을 우선합니다.

## 2. 장비 접근 안전 경계

런타임에서 허용하는 운영 명령은 다음 범위로 제한합니다.

```text
show switches
show lc-cluster load distribution client
show lc-cluster group-membership
```

환경에 따라 다음 두 명령만 세션 제어 목적으로 추가할 수 있습니다.

```text
enable
no paging
```

개발 시 다음 원칙을 유지합니다.

- 설정 모드에 진입하는 코드를 추가하지 않습니다.
- 구성 변경 명령을 allowlist에 추가하지 않습니다.
- 실제 장비 테스트는 별도 승인과 명시적인 접속 정보가 있을 때만 수행합니다.
- 접속 실패, 명령 실패, 빈 출력, Parser 실패를 WLC Down의 증거로 사용하지 않습니다.
- 장비 지문 확인 전에 자격 증명을 전송하지 않습니다.

## 3. 자격 증명과 민감정보

비밀번호와 Enable Secret은 다음 위치에 저장하지 않습니다.

- JSON 설정
- SQLite
- fixture
- 일반/진단/성능 로그
- Release asset
- GitHub Issue/PR 본문

영구 보관이 필요한 자격 증명은 Windows Credential Manager를 사용하고, 세션 전용 자격 증명은 프로세스 메모리에만 둡니다.

비식별 fixture를 추가할 때도 실제 IP, Hostname, 사용자명, 내부 네트워크 구조가 남지 않았는지 확인합니다.

## 4. 모듈 책임

가능한 한 다음 책임을 분리해 유지합니다.

| 영역 | 책임 |
|---|---|
| `collectors/` | SSH 접속과 명령 실행, Primary/Fallback |
| `parsers/` | 명령 원문을 구조화된 관측값으로 변환 |
| `services/anomaly_detector.py` | 연속 이상·복구·누락 판정 |
| `services/correlation_engine.py` | 명령별 관측값을 IP 기준으로 상관분석 |
| `services/incident_manager.py` | 장애·확인·복구 lifecycle |
| `services/poll_coordinator.py` | Worker Thread와 수동/자동 점검 조정 |
| `storage.py` | SQLite 상태·baseline·사건 저장 |
| `credentials.py` | Credential Manager / 세션 자격 증명 경계 |
| `ui/` | 표시와 운영자 조작, 도메인 판단은 수행하지 않음 |

UI에서 새로운 장애 기준을 직접 계산하지 말고 도메인 계층의 결과를 표시하도록 유지합니다.

## 5. 개발 환경

기준 런타임은 **CPython 3.13.15 x64 표준 GIL 빌드**입니다. 실험적 free-threaded 빌드는 배포 기준이 아닙니다.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r .\requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

의존성은 `requirements-lock.txt`의 해시 고정 상태를 기준으로 검증합니다.

## 6. 변경 후 검증

기본 검증:

```powershell
.\scripts\run_tests.ps1
```

이 스크립트는 전체 pytest, 1,000회 결정적 fault-injection / soak,
`compileall`, `pip check`를 실행합니다.

안정성 반복 횟수를 늘려 별도로 실행하려면 다음을 사용합니다.

```powershell
.\.venv\Scripts\python.exe .\scripts\run_reliability_soak.py --cycles 5000
```

Windows 배포물까지 영향을 주는 변경은 다음도 확인합니다.

```powershell
.\scripts\build.ps1
```

버전 배포 검증:

```powershell
.\scripts\package_release.ps1 -Version <버전>
```

CI는 정확한 CPython 3.13.15 x64 환경에서 onedir 패키지와 추출 후 EXE smoke를 검증합니다.

## 7. 테스트 설계 원칙

자동 테스트는 다음을 우선합니다.

- 비식별 fixture
- 메모리 또는 임시 SQLite
- 가짜 SSH 경계
- 로컬 Paramiko SSH 서버
- Offscreen PySide6 UI

실제 장비가 없어도 Parser·상관분석·상태 전이·저장·UI 동작을 재현할 수 있어야 합니다.

자동 테스트가 통과해도 다음 사실을 자동으로 증명한다고 표현하지 않습니다.

- 특정 ArubaOS 버전의 실제 출력 호환성
- 실제 구형 장비 SSH 알고리즘 호환성
- 조직 정책이 적용된 Windows 알림/자격 증명 환경
- Python 미설치 클린 Windows PC의 현장 운용

## 8. 개발자 UI 식별 모드

F12 개발자 UI 식별 모드는 화면 요소를 안정적인 이름·식별자·소스 위치로 지목하기 위한 개발 보조 기능입니다. 일반 운영 기능과 분리하며 **모든 새 실행은 항상 개발자 모드가 꺼진 상태로 시작**합니다.

- 활성화는 애플리케이션 창이 **수정 키 없는 직접 `F12` 입력**을 받았을 때만 가능합니다.
- `--ui-inspector` 같은 명령줄 옵션, 환경 변수, 설정 파일, 일반 메뉴, 트레이 메뉴에는 활성화 경로를 두지 않습니다.
- 요소 선택 상태에서 `Esc`는 요소 선택만 취소하며 개발자 모드 자체를 켜거나 끄지 않습니다.
- 요소를 선택할 때는 원래 버튼·표·탭·메뉴 동작을 실행하지 않고 해당 요소의 식별 정보만 표시합니다.
- Windows 네이티브 트레이 메뉴는 화면 클릭 대상으로 직접 가로채지 않고 정적 카탈로그에서 식별합니다.
- 작업 요청 복사 기능은 설정 입력값, 자격 증명, 실제 IP/Hostname, 원본 명령 출력, 로그 내용, 로컬 절대 경로를 포함하지 않습니다.
- 개발자 모드 상태는 JSON, SQLite, 레지스트리 등 영구 저장소에 저장하지 않습니다.

이 기능을 변경할 때는 일반 사용자의 화면·입력 동작이 개발 모드 때문에 바뀌지 않는지와 비식별 복사 범위를 함께 회귀 검증합니다.

## 9. Release 원칙

`dist/`, `build/`, `.venv/` 등 생성 산출물은 Git에 커밋하지 않습니다.

Release는 다음 조건을 만족해야 합니다.

- 전체 자동 테스트 통과
- Windows onedir 패키지 검증 통과
- 패키지 inventory와 민감 파일 검사 통과
- SHA-256 확인
- 제3자 라이선스·Qt/LGPL 고지 포함
- 기존 태그와 자산을 덮어쓰지 않음

배포 세부사항은 `docs/RELEASE_PROCESS_KO.md`를 기준으로 합니다.

## 10. 문서 변경 원칙

README에는 운영자가 프로젝트를 이해하는 데 필요한 핵심 내용만 유지합니다.

상세 구현은 다음 문서로 분리합니다.

- 구조: `docs/ARCHITECTURE_KO.md`
- 판정 규칙: `docs/DETECTION_LOGIC_KO.md`
- 검증: `docs/VALIDATION_REPORT_KO.md`
- 보안: `docs/SECURITY_KO.md`
- 성능: `docs/PERFORMANCE_REPORT_KO.md`
- Windows QA: `docs/WINDOWS11_QA_CHECKLIST_KO.md`

문서에 실제 운영망 정보나 자동 테스트로 증명하지 못한 호환성 주장을 추가하지 않습니다.
