# LGPL 런타임 교체 및 복구 안내 / LGPL Runtime Replacement and Rollback Guide

이 문서는 Aruba Mini Dashboard Windows **onedir** 패키지에 외부 파일로 함께
제공되는 LGPL 런타임의 기술적 교체 가능 경로를 설명합니다. Aruba Mini
Dashboard 자체 코드는 루트 `LICENSE` 및 배포물의 `LICENSE.txt`에 수록된 MIT
License로 배포합니다. 이 문서는 법률 자문이 아니며, 함께 제공되는 제3자
구성요소의 별도 라이선스나 권리를 대체하지 않습니다.

This document describes the technical replacement path for LGPL runtimes shipped
as external files in the Aruba Mini Dashboard Windows **onedir** package. Aruba
Mini Dashboard's own code is distributed under the MIT License in the root
`LICENSE` and packaged `LICENSE.txt`. This guide is not legal advice and does not
replace the separate licenses or rights of bundled third-party components.

## 검토된 런타임 / Reviewed runtimes

- Paramiko 5.0.0: `_internal\paramiko\*.py`
- scp 0.16.1: `_internal\scp.py`
- PySide6-Essentials / Qt 6.11.0: 외부 순수 Python 소스
  `_internal\PySide6\__init__.py`와 `_internal\PySide6\` 아래의 검토된 DLL,
  PYD 및 plugin. 정확한 바이너리 목록과 해시는 `QT_RUNTIME_INVENTORY.json` 참조.
- shiboken6 6.11.0: 외부 순수 Python 소스
  `_internal\shiboken6\__init__.py`, `_internal\shiboken6\Shiboken.pyd` 및
  `_internal\shiboken6\shiboken6.abi3.dll`

Paramiko, scp, PySide6 및 shiboken6의 외부 Python 원본 파일 목록·SHA-256·
버전·라이선스 증거는
`LGPL_RUNTIME_INVENTORY.json` 및 `LGPL_RUNTIME_LICENSES\`에 있습니다.
`ArubaMiniDashboard.exe`의 PYZ 아카이브에는 `PySide6`, `PySide6.*`,
`shiboken6`, `shiboken6.*`, `paramiko`, `paramiko.*`, `scp` 모듈이 들어가지
않도록 빌드 검증됩니다. 다른 애플리케이션 Python 소스는 배포 패키지에 허용되지
않습니다.

The exact external Python source lists, SHA-256 hashes, versions, and license evidence
for Paramiko, scp, PySide6, and shiboken6
are recorded in `LGPL_RUNTIME_INVENTORY.json` and `LGPL_RUNTIME_LICENSES\`.
The build gate verifies that `PySide6`, `shiboken6`, `paramiko`, their submodules,
and `scp` are absent from the executable's PYZ archive. No other application
Python source is permitted in the release package.

## 교체 절차 / Replacement procedure

1. 실행 중인 대시보드를 완전히 종료하고 배포 폴더 전체를 별도 위치에
   복사하여 백업합니다. / Exit the dashboard completely and back up the entire
   distribution directory.
2. 현재 패키지와 **동일한 버전**의 공식 배포본을 준비합니다: Paramiko 5.0.0,
   scp 0.16.1, PySide6-Essentials 6.11.0, shiboken6 6.11.0. Python 모듈은
   CPython 3.13.15 x64 표준 GIL 빌드와 호환되어야 하며 Qt DLL/PYD/plugin은 모두 같은 Qt 6.11.0
   세트여야 합니다. / Obtain official, same-version distributions. Python
   sources must support the CPython 3.13.15 x64 standard GIL build, and all Qt DLL/PYD/plugin files must remain
   a matched Qt 6.11.0 set.
3. Paramiko는 `_internal\paramiko\` 디렉터리 전체를 한 세트로, scp는
   `_internal\scp.py`를 교체합니다. 서로 다른 버전의 일부 파일만 혼합하지
   마십시오. / Replace the complete `_internal\paramiko\` directory as one set
   and replace `_internal\scp.py`; do not mix files from different versions.
4. PySide6와 shiboken6의 `__init__.py`도 동일 버전 공식 배포본의 파일로
   교체하고, Qt/PySide/shiboken 바이너리는 `QT_RUNTIME_INVENTORY.json`에 기록된 상대 경로를
   유지하여 동일 버전 파일로 교체합니다. PySide6와 shiboken6, Qt DLL 및
   plugin을 서로 다른 빌드에서 섞지 마십시오. / Replace the PySide6 and
   shiboken6 `__init__.py` files from the same-version official distributions,
   preserve every binary relative path in `QT_RUNTIME_INVENTORY.json`, and do
   not mix PySide6, shiboken6, Qt DLLs, or plugins from different builds.
5. 아래의 로컬 스모크 검사를 수행합니다. 이 명령은 장비에 접속하지 않습니다.
   / Run the local smoke check below; it does not connect to network devices.

   ```powershell
   .\ArubaMiniDashboard.exe --smoke --smoke-output .\smoke-result.txt
   Get-Content .\smoke-result.txt
   ```

   결과에는 최소한 `ARUBA_MINI_DASHBOARD_SMOKE_OK`, `NETMIKO_OK`,
   `PARAMIKO_OK`, `FIXTURE_DISCOVERY_OK`, `DEMO_CORRELATION_OK`가 있어야
   합니다. Windows 빌드에서는 `WIN32CRED_OK`도 확인합니다. / The result must
   contain those markers and, on Windows, `WIN32CRED_OK`.
6. `--demo`로 UI를 열어 표·설정·세부 화면을 확인한 뒤, 승인된 환경에서만
   읽기 전용 실제 장비 점검을 수행합니다. / Open `--demo` to check the UI;
   perform read-only live-device validation only in an approved environment.

`LGPL_RUNTIME_INVENTORY.json`과 `QT_RUNTIME_INVENTORY.json`은 원본 배포
검증 기록입니다. 파일을 교체한 뒤 해시가 달라지는 것은 예상되지만, 수정본을
재배포할 때에는 새 인벤토리, 대응 라이선스 증거, 완전한 대응 소스 및 검증
기록을 다시 작성해야 합니다.

The inventory files describe the original reviewed distribution. Hash changes
are expected after a replacement. If a modified build is redistributed, generate
new inventories, corresponding license evidence, complete corresponding source,
and new verification records.

## 복구 / Rollback

1. 프로그램을 종료합니다. / Exit the application.
2. 수정한 배포 폴더를 다른 위치로 이동해 조사 자료로 보존합니다. / Move the
   modified directory aside if it is needed for diagnosis.
3. 1단계에서 백업한 **전체 폴더**를 복원합니다. 일부 DLL/PYD/PY 파일만 되돌려
   혼합하지 마십시오. / Restore the complete backed-up directory; do not roll
   back only selected files.
4. 스모크 검사와 `--demo` 검사를 다시 실행합니다. / Repeat smoke and demo
   checks.

## 배포 조건과 권리 보존 / Distribution terms and preservation of rights

이 저장소는 기술적으로 외부 교체 가능한 onedir 구조와 검증 자료를 제공합니다.
Aruba Mini Dashboard의 MIT 배포 조건은 패키지에 포함된 LGPL 구성요소
(Qt/PySide6/shiboken6, Paramiko, scp)를 사용자가 자신의 용도로 수정하거나 그
수정 사항을 디버깅하기 위해 리버스 엔지니어링하는 것을 금지하거나 제한하지
않습니다. 각 제3자 구성요소에 실제로 적용되는 권리·의무와 라이선스 원문은
패키지의 고지, 인벤토리 및 라이선스 파일에서 별도로 확인해야 합니다.

The onedir package provides a technical replacement mechanism and verification
evidence. Aruba Mini Dashboard's MIT distribution terms do not prohibit or
restrict a user's modification of packaged LGPL components
(Qt/PySide6/shiboken6, Paramiko, and scp) for the user's own use, or reverse
engineering for debugging those modifications. The actual rights, obligations,
and license texts that apply to each third-party component remain separately
identified in the packaged notices, inventories, and license files.

## 출처 / Sources

- Paramiko 5.0.0: <https://github.com/paramiko/paramiko/tree/5.0.0>
- scp 0.16.1: <https://github.com/jbardin/scp.py/tree/v0.16.1>
- Qt for Python 6.11.0: <https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.11.0>
- Qt licensing: <https://doc.qt.io/qt-6/licensing.html>

실제 포함된 라이선스 원문과 해시는 `THIRD_PARTY_NOTICES.txt`,
`QT_THIRD_PARTY_NOTICES.txt`, `LGPL_RUNTIME_LICENSES\`에서 확인하십시오.
Refer to those packaged files for the actual license texts and recorded hashes.
