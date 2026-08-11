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


def test_release_workflow_pins_python_and_precreates_venv_before_build() -> None:
    text = _read(RELEASE_WORKFLOW)
    setup_index = text.index('python-version: "3.11.9"')
    venv_index = text.index("python -m venv .venv")
    package_index = text.index(".\\scripts\\package_release.ps1 -Version")

    assert setup_index < venv_index < package_index
    assert "sys.version_info[:3] == (3, 11, 9)" in text
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
    assert "--verify-tag" in text
    assert "--draft" in text
    assert "--prerelease" in text
    assert "digest -ne \"sha256:$hash\"" in text
    assert "-F draft=false" in text
    assert "-F prerelease=true" in text
    assert "Verified prerelease draft created" in text
    assert "Verified public prerelease published" in text
    assert "Published prerelease assets no longer match" in text
    assert "MIT License" in text
    assert "Python 미설치 클린 Windows 11" in text
    assert "실제 100%/125%/150% DPI" in text
    assert "exact numeric release ID could not be confirmed" in text
    assert "was not deleted or modified automatically" in text
    assert "--clobber" not in text
    assert "cleanup-tag" not in text
    assert "git push" not in text


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
    assert "$publicNames = @($env:AMD_ZIP_NAME, $env:AMD_CHECKSUM_NAME) | Sort-Object" in text
    assert "($remoteNames -join '|') -ne ($publicNames -join '|')" in text
    assert text.index("Reverify the exact assets before GitHub mutation") < text.index("-F draft=false")
    assert text.index("Test-ExactRemoteAssets -Release $draft") < text.index("-F draft=false")
    assert text.index("-F draft=false") < text.index("Test-ExactRemoteAssets -Release $published")


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
    assert "annotated tag, workflow가 시작된 SHA, 현재 `origin/main` HEAD" in text
    assert "versioned onedir ZIP과 그 ZIP의 `.sha256` 파일 두 개" in text
    assert "기존 Release와 태그는 자동 삭제하지 않습니다" in text
    assert "공개된 Prerelease는 후속 검증이 실패해도 자동 삭제하거나 수정하지 않습니다" in text
    assert "Python 미설치 클린 Windows 11" in text
    assert "실제 DPI" in text
    assert "코드 서명" in text


def test_package_script_builds_versioned_onedir_zip_and_reverifies_extraction() -> None:
    text = _read(PACKAGE_SCRIPT)

    assert '"build.ps1"' in text
    assert "& $buildScript -CleanReleaseEnvironment -PythonLauncher $bootstrapPython" in text
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
