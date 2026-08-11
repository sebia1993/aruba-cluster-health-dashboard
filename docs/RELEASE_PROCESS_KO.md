# Windows 배포 후보 검증 및 비공개 Draft 절차

이 문서는 Aruba Mini Dashboard의 Windows 배포 후보를 재현 가능한 방식으로
빌드·검증하고, 필요할 때 GitHub의 **비공개 Draft**로 검토하는 절차입니다.
현재 자동화에는 공개 게시 모드가 없습니다. 앱 저작권자의 배포 조건 결정과
필요한 법률·라이선스 검토가 끝나기 전에는 Prerelease를 포함한 공개 배포를
GitHub UI나 API로 수동 게시해서도 안 됩니다.

## 불변 원칙

- 이미 게시했거나 격리한 태그와 자산을 덮어쓰지 않습니다.
- 버전은 `pyproject.toml`, 패키지 `__version__`, `CHANGELOG.md`가 같아야 합니다.
- annotated tag는 반드시 `main`의 검증 대상 커밋 하나를 가리켜야 합니다.
- 공개 자산은 versioned onedir ZIP과 그 ZIP의 SHA-256 파일 두 개뿐입니다.
- EXE만 따로 배포하지 않으며 ZIP의 `_internal` 파일을 제거하지 않습니다.
- 실제 IP, Hostname, 계정, 원본 출력, DB, 로그, `known_hosts`, 키와 인증서는
  소스·Actions artifact·Release asset에 포함하지 않습니다.
- 실패한 실행이 만든 Draft는 자동 정리할 수 있지만, 게시된 Release와 기존
  태그는 자동 삭제하거나 교체하지 않습니다.

## 현재 버전 경계

- `v0.1.0`: 사용하지 않는 Qt Virtual Keyboard 구성 요소가 포함된 최초
  패키지이므로 GitHub Draft로 격리합니다. 다시 게시하지 않습니다.
- `v0.1.1`: 해당 구성 요소를 제거하고 라이선스 고지·패키지 검증을 강화한
  첫 수정 후보입니다. 현재는 로컬 검증 또는 비공개 Draft 검토까지만 허용하며,
  배포 조건 결정과 현장 검수 전에는 공개 Prerelease로 게시하지 않습니다.

## 로컬 검증

CPython 3.11.9가 설치된 Windows PowerShell 5.1 환경에서 실행합니다.

```powershell
.\scripts\run_tests.ps1
.\scripts\package_release.ps1 -Version 0.1.1
```

성공하면 `dist\release`에 다음 두 파일만 생성됩니다.

```text
ArubaMiniDashboard-v0.1.1-windows-x64.zip
ArubaMiniDashboard-v0.1.1-windows-x64.zip.sha256
```

다른 위치로 전달된 자산은 원본 소스에서 다시 빌드하지 않고 검증만 할 수
있습니다.

```powershell
.\scripts\package_release.ps1 `
  -Version 0.1.1 `
  -OutputDirectory artifacts\release `
  -VerifyOnly
```

검증은 SHA-256, 단일 최상위 폴더, 경로 순회·대소문자 충돌·민감 파일 부재,
Qt exact inventory, PySide6/shiboken6/Paramiko/scp 외부 원본 소스와 PYZ 비포함,
라이선스 증거,
필수 문서 및 압축 해제 후 동결 EXE smoke를 확인합니다. one-file과 EXE 단독
배포는 지원하지 않습니다.

## GitHub 준비

1. 변경을 Pull Request로 검토하고 필수 CI가 성공한 뒤 `main`에 반영합니다.
2. `main`의 대상 커밋에서 버전과 변경 이력을 다시 확인합니다.
3. 해당 커밋에 annotated tag를 만들고 push합니다.

```powershell
git switch main
git pull --ff-only
git tag -a v0.1.1 -m "Aruba Mini Dashboard v0.1.1"
git push origin v0.1.1
```

태그가 잘못된 커밋을 가리키면 Release workflow를 실행하지 않습니다. 게시된
태그를 재사용하지 말고 다음 patch 버전으로 수정합니다.

## GitHub Actions 배포 차단

공개 저장소의 Actions artifact도 바이너리 전달 경로가 될 수 있으므로, 현재
`[BLOCKED] Build and stage Windows prerelease` workflow는 첫 단계에서
`PUBLIC_BINARY_DISTRIBUTION_BLOCKED` 오류를 내고 종료합니다. 따라서
`build-only`와 `draft-prerelease` 어느 모드에서도 의존성 설치, 빌드, Actions
artifact 업로드 또는 GitHub Draft 생성이 일어나지 않습니다. 배포 후보 검증은
위 로컬 명령으로만 수행하고 산출물은 저장소에 커밋하거나 업로드하지 않습니다.

앱 자체 배포 조건과 함께 제공되는 LGPL/CPython 구성요소의 요구사항에 대한
저작권자 결정 및 필요한 법률 검토가 완료되기 전에는 GitHub UI나 API로 Draft나
Prerelease를 수동 생성·게시해서도 안 됩니다. 향후 승인된 배포 조건과 보호된
검토 절차를 저장소 소스에 반영하는 별도 변경에서만 workflow 차단을 해제할 수
있습니다. 기존 격리 Draft를 정리할 때에는 정확한 numeric release ID를 확인하고
기존 태그와 다른 Release를 건드리지 마십시오.

## 향후 승인된 공개 경로 추가 후 확인

아래 항목은 현재 실행할 절차가 아니라, 저작권자 승인과 필요한 검토를 거쳐
공개 경로가 별도 소스 변경으로 추가된 이후의 확인 기준입니다.

- [ ] Release가 Stable이 아닌 Prerelease로 표시됨
- [ ] 자산이 versioned ZIP과 `.sha256` 두 개뿐임
- [ ] Release의 기준 commit과 annotated tag 대상이 같음
- [ ] 로컬 ZIP SHA-256과 GitHub asset digest가 같음
- [ ] `Source code (zip/tar.gz)`가 Windows 실행 패키지가 아님을 본문에 명시함
- [ ] 실제 Aruba·클린 Windows·DPI·알림·코드 서명 미검증 경계를 명시함
- [ ] ZIP을 새 폴더에 풀어 `ArubaMiniDashboard.exe --demo`가 실행됨

현장 검수는 `WINDOWS11_QA_CHECKLIST_KO.md`를 사용하고, 검증 증거에는 사내
민감정보를 포함하지 않습니다.
