# Windows Prerelease 빌드·검증·게시 절차

이 문서는 Aruba Mini Dashboard의 Windows onedir 배포 후보를 로컬과 GitHub
Actions에서 재현 가능하게 빌드하고, 검증된 자산만 GitHub Prerelease로 게시하는
절차입니다. 저작권자는 프로젝트 자체 소스와 배포물의 MIT License 배포를
승인했습니다. 함께 배포되는 제3자 구성요소에는 각 구성요소의 라이선스가
적용되며, 패키지 안의 고지와 라이선스 원문을 삭제하면 안 됩니다.

## 불변 원칙

- 기존에 게시되었거나 격리된 Release, Draft, 태그 또는 자산을 덮어쓰지 않습니다.
- 버전은 `pyproject.toml`, 패키지 `__version__`, `CHANGELOG.md`가 같아야 합니다.
- annotated tag, workflow가 시작된 SHA, 현재 `origin/main` HEAD는 정확히 같은
  커밋이어야 합니다. 단순히 `main` 이력에 포함된 것만으로는 게시할 수 없습니다.
- 공개 Release 자산은 versioned onedir ZIP과 그 ZIP의 `.sha256` 파일 두 개뿐입니다.
- EXE만 따로 배포하거나 ZIP의 `_internal` 파일을 제거하지 않습니다.
- 실제 IP, Hostname, 계정, 원본 출력, DB, 로그, `known_hosts`, 설정 또는
  자격 증명을 테스트·Actions artifact·Release asset에 포함하지 않습니다.
- 게시를 시도하지 않은 실패 실행이 만든 Draft는 numeric release ID, 태그, 생성 당시 URL,
  Draft/Prerelease 상태가 모두 일치하는 경우에만 자동 정리합니다. 게시 API를 한 번이라도
  호출한 뒤에는 응답이 불명확하더라도 자동 정리하지 않습니다. 기존 Release와 태그는 자동 삭제하지 않습니다.
- 공개된 Prerelease는 후속 검증이 실패해도 자동 삭제하거나 수정하지 않습니다.
  정확한 Release ID를 확인한 뒤 사람이 원인을 조사합니다.
- Draft의 `/releases/untagged/...` URL은 게시 후 `/releases/tag/...` URL로 바뀔 수 있습니다.
  게시 후 동일성은 URL 불변이 아니라 numeric release ID, 태그, Prerelease 상태와 원격
  자산 이름·크기·SHA-256 digest로 확인합니다.

## 현재 버전 경계

- `v0.1.0`: 사용하지 않는 Qt Virtual Keyboard 구성요소가 포함된 최초 패키지입니다.
  기존 Draft와 자산을 격리 상태로 보존하고 재게시하거나 교체하지 않습니다.
- `v0.1.1`: 해당 구성요소를 제거하고 라이선스 고지·패키지 검증을 강화한 첫
  MIT Prerelease 후보입니다. 새 annotated tag와 새 Release로만 게시합니다.

## 로컬 검증

CPython 3.11.9 x64와 Windows PowerShell 5.1 환경에서 실행합니다.

```powershell
.\scripts\run_tests.ps1
.\scripts\package_release.ps1 -Version 0.1.1
```

성공하면 `dist\release`에는 다음 두 파일만 생성됩니다.

```text
ArubaMiniDashboard-v0.1.1-windows-x64.zip
ArubaMiniDashboard-v0.1.1-windows-x64.zip.sha256
```

다른 위치로 전달된 자산은 다시 빌드하지 않고 다음과 같이 검증할 수 있습니다.

```powershell
.\scripts\package_release.ps1 `
  -Version 0.1.1 `
  -OutputDirectory artifacts\release `
  -VerifyOnly
```

검증기는 SHA-256, ZIP 최상위 폴더, 경로 순회·대소문자 충돌·민감 파일,
Qt exact inventory, PySide6/shiboken6/Paramiko/scp 외부 소스와 PYZ 비포함,
필수 라이선스 문서, 압축 해제 후 EXE smoke를 확인합니다. one-file과 EXE 단독
배포는 지원하지 않습니다.

## GitHub 준비

1. Pull Request의 필수 CI를 통과시킨 뒤 `main`에 반영합니다.
2. `main` HEAD에서 버전, `CHANGELOG.md`, `LICENSE`, 제3자 고지를 다시 확인합니다.
3. 같은 커밋에 annotated tag를 만들고 push합니다.

```powershell
git switch main
git pull --ff-only
git tag -a v0.1.1 -m "Aruba Mini Dashboard v0.1.1"
git push origin v0.1.1
```

태그가 잘못된 커밋을 가리키면 Release workflow를 실행하지 않습니다. 게시된
태그를 강제로 이동하거나 자산을 교체하지 말고 다음 patch 버전으로 수정합니다.

## GitHub Actions 실행 모드

GitHub의 **Actions → Build and publish Windows prerelease → Run workflow**에서
반드시 `main` 브랜치를 선택합니다. 입력값은 다음과 같습니다.

- `tag`: 이미 origin에 존재하는 annotated tag. 예: `v0.1.1`
- `release_mode`:
  - `build-only`: 빌드·검증 후 Actions artifact만 생성
  - `draft-prerelease`: 검증된 두 자산을 새 Prerelease Draft에 업로드하고 정지
  - `publish-prerelease`: 검증된 Draft를 만든 뒤 원격 자산까지 재검증하여 공개 게시
- `confirmation`: Draft 또는 공개 게시 모드에서 `tag`와 대소문자까지 정확히 같은 값

MIT 배포 승인에 따라 이 공개 저장소의 실행은 검증된 ZIP과 `.sha256`을 14일간
Actions artifact로 보관합니다. `build-only`도 artifact를 만들기 때문에 민감정보가
없는 fixture와 패키지 계약을 반드시 유지해야 합니다. artifact는 Release 자산이
아니며 장기 배포 링크로 사용하지 않습니다.

## 자동 검증과 게시 순서

workflow는 다음 순서를 바꿀 수 없도록 구성되어 있습니다.

1. 수동 `workflow_dispatch` 및 `main` 실행 여부 확인
2. checkout된 소스에서 tracked `LICENSE`와 MIT 본문, 승인된 source gate 확인
3. 버전 형식과 소스 버전 일치 확인
4. origin의 annotated tag object, tag commit, workflow SHA, `origin/main` HEAD 일치 확인
5. 같은 태그의 기존 Release 또는 Draft가 없는지 전체 페이지 조회
6. CPython 3.11.9 환경에서 테스트·onedir 빌드·ZIP·checksum 생성 및 검증
7. Actions artifact로 전달한 두 파일을 다른 job에서 다시 다운로드하여 검증
8. Draft 생성 직전 같은 태그·main SHA를 다시 확인
9. 새 Draft의 numeric ID와 URL을 확인한 뒤 ZIP과 `.sha256` 두 파일만 업로드
10. 원격 asset 이름, 상태, 바이트 크기, SHA-256 digest를 로컬 파일과 비교
11. `publish-prerelease`에서만 동일 태그와 main SHA를 다시 확인하고 Draft 해제
12. 동일 numeric release ID가 공개 상태가 되는 즉시 자동 정리를 금지
13. URL 변경을 허용하면서 공개 Prerelease의 ID·태그·상태와 두 원격 자산을 마지막으로 재검증

기존 같은 태그의 Release/Draft가 하나라도 있으면 workflow는 중단합니다.
`draft-prerelease`로 만든 Draft를 나중에 자동 승격하지도 않습니다. 검토용 Draft를
사용한 뒤 공개 게시가 필요하면 정확한 Draft ID를 사람이 확인하여 안전하게 정리하고,
같은 불변 자산으로 `publish-prerelease`를 새로 실행합니다.

## Release notes 필수 내용

자동 생성되는 notes에는 다음이 포함됩니다.

- 앱 자체 MIT License와 커밋 고정 `LICENSE` 링크
- ZIP과 `.sha256` 파일명 및 ZIP SHA-256
- 제3자 고지와 Qt/LGPL 런타임 인벤토리 위치
- 빌드에 사용한 정확한 source commit과 Python 버전
- GitHub 자동 Source code 아카이브가 Windows 실행 패키지가 아니라는 설명
- 실제 Aruba 장비, 사내 SSH, Python 미설치 클린 Windows 11, 실제 DPI,
  알림 정책, 코드 서명이 별도 현장 검증이라는 제한사항

## 게시 후 확인

- [ ] Release가 Stable이 아닌 Prerelease로 표시됨
- [ ] 자산이 versioned ZIP과 `.sha256` 두 개뿐임
- [ ] Release tag가 가리키는 commit과 현재 게시 대상 `main` SHA가 일치함
- [ ] 로컬 ZIP SHA-256, checksum 본문, GitHub asset digest가 일치함
- [ ] `Source code (zip/tar.gz)`가 Windows 실행 패키지가 아님을 notes에 명시함
- [ ] MIT License 링크와 제3자 고지 위치가 notes에 명시됨
- [ ] 실제 장비·클린 Windows·DPI·알림·코드 서명 미검증 경계를 명시함
- [ ] ZIP 전체를 해제한 뒤 `ArubaMiniDashboard.exe --demo`가 실행됨

현장 검수에는 `WINDOWS11_QA_CHECKLIST_KO.md`를 사용하고, 검증 자료에는 사내
민감정보를 포함하지 않습니다.
