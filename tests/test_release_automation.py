from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "scripts" / "package_release.ps1"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"


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


def test_release_workflow_has_fail_closed_prerelease_contract() -> None:
    text = _read(RELEASE_WORKFLOW)

    block_index = text.index("PUBLIC_BINARY_DISTRIBUTION_BLOCKED")
    checkout_index = text.index("actions/checkout@")
    upload_index = text.index("actions/upload-artifact@")
    assert 'AMD_PUBLIC_BINARY_DISTRIBUTION_APPROVED: "false"' in text
    assert block_index < checkout_index < upload_index
    for mode in ("build-only", "draft-prerelease"):
        assert mode in text
    assert "publish-prerelease" not in text
    assert "Release tag must be an annotated tag" in text
    assert "must identify the same commit" in text
    assert "must match pyproject.toml" in text
    assert "confirmation that exactly matches" in text
    assert "A release or draft already exists" in text
    assert "--verify-tag" in text
    assert "--draft" in text
    assert "--prerelease" in text
    assert "digest -ne \"sha256:$hash\"" in text
    assert "-F draft=false" not in text
    assert "Verified private prerelease draft created" in text
    assert "GitHub UI 또는 API로 공개 게시하지 마십시오" in text
    assert "exact numeric release ID could not be confirmed" in text
    assert "cleanup-tag" not in text
    assert "git push" not in text


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
