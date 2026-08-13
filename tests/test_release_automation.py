from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "scripts" / "package_release.ps1"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"
RELEASE_PROCESS = ROOT / "docs" / "RELEASE_PROCESS_KO.md"
REPOSITORY_README = ROOT / "README.md"
PACKAGED_README = ROOT / "docs" / "README.txt"
WINDOWS_QA_CHECKLIST = ROOT / "docs" / "WINDOWS11_QA_CHECKLIST_KO.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_workflow_is_valid_yaml_and_manual_only() -> None:
    text = _read(RELEASE_WORKFLOW)

    assert yaml.compose(text) is not None
    assert re.search(r"(?m)^on:\s*$", text)
    assert re.search(r"(?m)^  workflow_dispatch:\s*$", text)
    assert not re.search(r"(?m)^  (push|pull_request|schedule):\s*$", text)
    assert "refs/heads/main" in text


def test_release_workflow_uses_pinned_actions_and_minimal_permissions() -> None:
    text = _read(RELEASE_WORKFLOW)
    action_refs = re.findall(r"(?m)^\s*uses:\s*[^@\s]+@([^\s#]+)", text)

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert re.search(r"(?m)^permissions:\s*\n  contents: read\s*$", text)
    assert text.count("contents: write") == 1
    assert "id-token: write" not in text
    assert "actions: write" not in text
    assert "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" not in text
    assert text.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") == 3
    assert text.count("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97") == 3
    assert text.count("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a") == 1
    assert text.count("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c") == 2


def test_release_workflow_pins_python_and_precreates_venv_before_build() -> None:
    text = _read(RELEASE_WORKFLOW)
    setup_index = text.index('python-version: "3.13.15"')
    venv_index = text.index("python -m venv .venv")
    package_index = text.index(".\\scripts\\package_release.ps1 -Version")

    assert setup_index < venv_index < package_index
    assert text.count('python-version: "3.13.15"') == 3
    assert text.count("sys.version_info[:3] == (3, 13, 15)") == 6
    assert text.count("platform.machine() == 'AMD64'") == 6
    assert text.count("not sysconfig.get_config_var('Py_GIL_DISABLED')") == 6
    assert text.count("sys._is_gil_enabled()") == 6
    assert "requirements-lock.txt" in text


def test_release_workflow_has_approved_safe_prerelease_contract() -> None:
    text = _read(RELEASE_WORKFLOW)

    checkout_index = text.index("actions/checkout@")
    approval_index = text.index("Verify MIT distribution approval in source")
    upload_index = text.index("actions/upload-artifact@")
    assert "PUBLIC_BINARY_DISTRIBUTION_BLOCKED" not in text
    assert 'AMD_PUBLIC_BINARY_DISTRIBUTION_APPROVED: "true"' in text
    assert checkout_index < approval_index < upload_index
    assert "git ls-files --error-unmatch LICENSE" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED \"AS IS\"' in text
    for mode in ("build-only", "draft-prerelease", "publish-prerelease"):
        assert mode in text
    assert "Release tag must be an annotated tag" in text
    assert "must identify the same commit" in text
    assert "current origin/main must identify the same commit" in text
    assert "Annotated release tag, packaged source, and origin/main" in text
    assert "must match pyproject.toml" in text
    assert "Draft and publish modes require confirmation that exactly matches" in text
    assert "A release or draft already exists" in text
    assert "release(tagName: $tag)" in text
    assert "databaseId" in text
    assert "$releaseId = [long]$draftIdentity.databaseId" in text
    assert "Prerelease draft lookup by exact numeric ID failed" in text
    assert "--verify-tag" in text
    assert "--draft" in text
    assert "--prerelease" in text
    assert "digest -ne \"sha256:$hash\"" in text
    assert "-F draft=false" in text
    assert "-F prerelease=true" in text
    assert "Verified prerelease draft created" in text
    assert "Verified public prerelease published" in text
    assert "Published prerelease assets no longer match" in text
    assert "$publishAttempted = $true" in text
    assert "automatic draft cleanup is forbidden" in text
    assert "MIT License" in text
    assert "Python 미설치 클린 Windows 11" in text
    assert "실제 100%/125%/150% DPI" in text
    assert "자동 점검 간격은 설정값과 120초 중 큰 값" in text
    assert "MM과 클러스터를 최대 두 작업으로 동시에 수집" in text
    assert "전체 장비가 250대를 넘으면 전체 정렬 후 현재 250대만" in text
    assert "저사양 PC·HDD·느린 네트워크·24시간 이상 운전" in text
    assert "exact numeric release ID could not be confirmed" in text
    assert "was not deleted or modified automatically" in text
    assert "--clobber" not in text
    assert "cleanup-tag" not in text
    assert "git push" not in text


def test_release_workflow_finds_drafts_by_exact_tag_and_reverifies_numeric_id() -> None:
    text = _read(RELEASE_WORKFLOW)

    assert text.count("release(tagName: $tag)") == 2
    assert "releases?per_page=100" not in text
    assert "Get-MatchingReleases" not in text
    assert "function Get-ReleaseIdentityByTag" in text
    assert "GitHub exact-tag release lookup failed" in text
    assert "GitHub exact-tag response could not prove the repository identity" in text

    preflight_start = text.index("Refuse an existing release or draft before building")
    preflight_end = text.index("Set up exact build Python", preflight_start)
    preflight = text[preflight_start:preflight_end]
    assert "gh api graphql" in preflight
    assert "databaseId" in preflight
    assert "tagName" in preflight
    assert "isDraft" in preflight
    assert "isPrerelease" in preflight
    assert "url" in preflight
    assert "$null -ne $response.errors" in preflight
    assert "$null -eq $response.data.repository" in preflight
    assert "if ($null -ne $identity)" in preflight

    create_index = text.index("$createOutput = @(gh release create")
    candidate_index = text.index("$candidate = Get-ReleaseIdentityByTag", create_index)
    numeric_id_index = text.index("$releaseId = [long]$draftIdentity.databaseId", candidate_index)
    rest_lookup_index = text.index(
        'gh api "repos/$($env:AMD_REPOSITORY)/releases/$releaseId"',
        numeric_id_index,
    )
    upload_index = text.index("gh release upload", rest_lookup_index)
    assert create_index < candidate_index < numeric_id_index < rest_lookup_index < upload_index

    identity_checks = text[candidate_index:upload_index]
    assert "$candidate.tagName -ne $env:AMD_RELEASE_TAG" in identity_checks
    assert "-not $candidate.isDraft" in identity_checks
    assert "-not $candidate.isPrerelease" in identity_checks
    assert "[string]$candidate.url -ne $releaseUrl" in identity_checks
    assert "[long]$draft.id -ne $releaseId" in identity_checks
    assert "$draft.tag_name -ne $env:AMD_RELEASE_TAG" in identity_checks
    assert "[string]$draft.html_url -ne $releaseUrl" in identity_checks
    assert "-not $draft.draft -or -not $draft.prerelease" in identity_checks


def test_release_workflow_job_order_and_asset_contract() -> None:
    text = _read(RELEASE_WORKFLOW)
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]

    assert set(jobs) == {"package", "verify-handoff", "release"}
    assert jobs["verify-handoff"]["needs"] == "package"
    assert jobs["release"]["needs"] == ["package", "verify-handoff"]
    assert jobs["release"]["if"] == "needs.package.outputs.release-mode != 'build-only'"
    assert jobs["release"]["permissions"] == {"contents": "write"}

    assert "Upload MIT-approved public repository release handoff" in text
    upload_step = next(
        step
        for step in jobs["package"]["steps"]
        if step.get("name") == "Upload MIT-approved public repository release handoff"
    )
    assert upload_step["with"]["archive"] == "true"
    download_steps = [
        step
        for job_name in ("verify-handoff", "release")
        for step in jobs[job_name]["steps"]
        if step.get("name") in {
            "Download the release handoff",
            "Download the independently verified handoff",
        }
    ]
    assert len(download_steps) == 2
    assert all(step["with"]["skip-decompress"] == "false" for step in download_steps)
    assert all(step["with"]["digest-mismatch"] == "error" for step in download_steps)
    assert "$publicNames = @($env:AMD_ZIP_NAME, $env:AMD_CHECKSUM_NAME) | Sort-Object" in text
    assert "($remoteNames -join '|') -ne ($publicNames -join '|')" in text
    assert text.index("Reverify the exact assets before GitHub mutation") < text.index("-F draft=false")
    assert text.index("Test-ExactRemoteAssets -Release $draft") < text.index("-F draft=false")
    assert text.index("-F draft=false") < text.index("Test-ExactRemoteAssets -Release $published")


def test_release_workflow_allows_published_url_change_and_protects_release_first() -> None:
    text = _read(RELEASE_WORKFLOW)
    publish_branch_start = text.index("elseif ($env:AMD_RELEASE_MODE -eq 'publish-prerelease')")
    publish_start = text.index("$publishJson = gh api --method PATCH", publish_branch_start)
    publish_end = text.index("$publishedVerified = $false", publish_start)
    publish_response = text[publish_branch_start:publish_end]

    observed_published = "if ([long]$published.id -eq $releaseId -and -not $published.draft)"
    assert publish_response.index("$publishAttempted = $true") < publish_response.index(
        "$publishJson = gh api --method PATCH"
    )
    protected_index = publish_response.index("$releasePublished = $true")
    identity_index = publish_response.index("GitHub did not return the exact published prerelease")
    assert observed_published in publish_response
    assert protected_index < identity_index
    assert "[long]$published.id -ne $releaseId" in publish_response
    assert "$published.tag_name -ne $env:AMD_RELEASE_TAG" in publish_response
    assert "$published.draft -or -not $published.prerelease" in publish_response
    assert "[string]$published.html_url -ne $releaseUrl" not in text[publish_start:]
    assert "$publishedUrl = [string]$published.html_url" in publish_response

    catch_start = text.index("catch {", publish_end)
    cleanup = text[catch_start:]
    assert cleanup.index("if ($releasePublished)") < cleanup.index("elseif ($publishAttempted)")
    assert cleanup.index("elseif ($publishAttempted)") < cleanup.index(
        "elseif ($draftCreated -and $null -ne $releaseId)"
    )
    protect_non_draft = cleanup.index("if ($cleanupIdMatches -and -not $cleanup.draft)")
    delete_draft = cleanup.index("gh api --method DELETE")
    assert protect_non_draft < delete_draft
    assert "$cleanup.draft -and" in cleanup
    assert "$cleanup.prerelease" in cleanup


@pytest.mark.windows
def test_release_workflow_powershell_blocks_parse(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    workflow = yaml.load(_read(RELEASE_WORKFLOW), Loader=yaml.BaseLoader)
    blocks: list[tuple[str, str]] = []
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job["steps"]):
            if step.get("shell") == "pwsh" and "run" in step:
                blocks.append((f"{job_name}-{index}", step["run"]))

    assert blocks
    parser_command = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:AMD_PWSH_PARSE_PATH, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }"
    )
    for name, block in blocks:
        script_path = tmp_path / f"{name}.ps1"
        expanded = re.sub(r"\$\{\{.*?\}\}", "workflow_value", block)
        script_path.write_text(expanded, encoding="utf-8")
        env = os.environ.copy()
        env["AMD_PWSH_PARSE_PATH"] = str(script_path)
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                parser_command,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, f"{name}: {completed.stdout}{completed.stderr}"


def test_release_process_documents_mit_publication_and_field_boundaries() -> None:
    text = _read(RELEASE_PROCESS)

    assert "프로젝트 자체 소스와 배포물의 MIT License 배포를" in text
    assert "AMD_PUBLIC_BINARY_DISTRIBUTION_APPROVED" not in text
    for mode in ("build-only", "draft-prerelease", "publish-prerelease"):
        assert f"`{mode}`" in text
    assert "Actions artifact로 보관" in text
    assert "Node.js 24 기반 버전" in text
    assert "runner `2.327.1` 이상" in text
    assert "구형 Node 런타임을 강제로 허용" in text
    assert "annotated tag, workflow가 시작된 SHA, 현재 `origin/main` HEAD" in text
    assert "versioned onedir ZIP과 그 ZIP의 `.sha256` 파일 두 개" in text
    assert "기존 Release와 태그는 자동 삭제하지 않습니다" in text
    assert "공개된 Prerelease는 후속 검증이 실패해도 자동 삭제하거나 수정하지 않습니다" in text
    assert "게시 API를 한 번이라도" in text
    assert "`/releases/untagged/...` URL은 게시 후 `/releases/tag/...` URL로 바뀔 수 있습니다" in text
    assert "numeric release ID, 태그, Prerelease 상태와 원격" in text
    assert "Python 미설치 클린 Windows 11" in text
    assert "실제 DPI" in text
    assert "코드 서명" in text


def test_release_notes_name_the_embedded_python_runtime_verification() -> None:
    workflow = _read(RELEASE_WORKFLOW)

    assert "내장 Python DLL 집합·AMD64·3.13.15 메타데이터" in workflow


def test_developer_inspector_release_contract_is_documented() -> None:
    repository_readme = _read(REPOSITORY_README)
    packaged_readme = _read(PACKAGED_README)
    release_process = _read(RELEASE_PROCESS)
    workflow = _read(RELEASE_WORKFLOW)
    qa_checklist = _read(WINDOWS_QA_CHECKLIST)

    assert "모든 새 실행은 항상 개발자 모드가 꺼진 상태로 시작" in repository_readme
    assert "수정 키 없는 직접 `F12` 입력" in repository_readme
    assert "`--ui-inspector` 같은 명령줄 옵션, 환경 변수, 설정 파일" in repository_readme
    assert "`Esc`는 요소 선택만 취소" in repository_readme
    assert "원래 버튼·표·탭·메뉴 동작을 실행하지 않고" in repository_readme
    assert "Windows 네이티브 트레이 메뉴" in repository_readme
    assert "원본 명령 출력, 로그 내용, 로컬 절대 경로" in repository_readme

    assert "모든 새 실행은 항상 일반 사용자 모드로" in packaged_readme
    assert "환경 변수, 설정 파일, 일반 메뉴와 트레이" in packaged_readme
    assert "Esc는 요소 선택만 취소" in packaged_readme

    assert "`v0.3.7`" in release_process
    assert "트레이 항목의 정적 카탈로그 확인과 비식별 복사 범위" in release_process

    assert "모든 새 실행은 일반 사용자 모드로 시작" in workflow
    assert "수정 키 없는 직접" in workflow and "F12" in workflow
    assert "활성화 경로가 없습니다" in workflow
    assert "요소 선택 중 클릭은 원래 버튼·표·탭·메뉴 동작을 실행하지 않습니다" in workflow
    assert "시스템 트레이 카탈로그" in workflow
    assert "조직 클립보드 정책" in workflow

    assert "Ctrl+F12, Shift+F12, Alt+F12" in qa_checklist
    assert "Esc를 누르면 선택과 테두리만 취소" in qa_checklist
    assert "설정 입력값, 원본 명령 출력, 로그 내용" in qa_checklist
    assert "실제 물리 F12/Fn 키 매핑" in qa_checklist


def test_developer_inspector_has_no_command_line_or_persisted_activation() -> None:
    from aruba_mini_dashboard.config import AppSettings
    from aruba_mini_dashboard.main import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--ui-inspector"])
    assert exc_info.value.code == 2

    serialized = repr(AppSettings.default().to_dict()).lower()
    assert "inspector" not in serialized
    assert "developer" not in serialized


def test_package_script_builds_versioned_onedir_zip_and_reverifies_extraction() -> None:
    text = _read(PACKAGE_SCRIPT)

    assert '"build.ps1"' in text
    assert "& $buildScript -CleanReleaseEnvironment -PythonLauncher $bootstrapPython" in text
    assert "Test-ExactBuildPython -Candidate $venvPython" in text
    assert 'Get-Command "py"' in text
    assert 'Test-ExactBuildPython -Candidate $pythonLauncher.Source -InterpreterArguments @("-3.13")' in text
    assert "cpython|3.13.15|64|AMD64|0|1" in text
    assert "[AllowEmptyCollection()][string[]]$Arguments" in text
    assert "ForEach-Object { Write-Host $_ }" in text
    assert '"verify_release_package.py"' in text
    assert "$productName-v$Version-windows-x64" in text
    assert "New-OnedirZip" in text
    assert "Assert-ZipEntryContract" in text
    assert "$unsafeSegments.Count -gt 0" in text
    assert "Assert-MatchingInventories" in text
    assert "ExtractToDirectory" in text
    assert '"--zip"' in text
    assert '"--sha256-file"' in text
    assert "Release ZIP SHA-256 mismatch" in text
    assert "ARUBA_MINI_DASHBOARD_RELEASE_PACKAGE_OK" in text
    assert "Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force" in text
    assert "StartsWith($tempPrefix" in text
    assert "Release packaging requires a clean working tree" in text
    assert "Source version changed during the build" in text
    assert "Source commit changed during the build" in text
    assert "must not be published" in text


@pytest.mark.windows
@pytest.mark.parametrize(
    ("version", "expected_error"),
    [
        ("not-a-version", "Invalid release version"),
        ("9.9.9", "does not match source version"),
    ],
)
def test_package_script_fails_before_build_for_invalid_version(
    version: str,
    expected_error: str,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PACKAGE_SCRIPT),
            "-Version",
            version,
            "-VerifyOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert expected_error in combined
    assert "Windows onedir build" not in combined
