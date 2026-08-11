from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_packaging_uses_a_recreated_release_environment() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    package = (ROOT / "scripts" / "package_release.ps1").read_text(encoding="utf-8")

    assert "[switch]$CleanReleaseEnvironment" in build
    assert 'Join-Path $repo ".release-venv"' in build
    assert "Remove-Item -LiteralPath $venvFull -Recurse -Force" in build
    assert "& $buildScript -CleanReleaseEnvironment -PythonLauncher $bootstrapPython" in package


def test_build_rejects_wrong_architecture_and_unapproved_qt_distributions() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8").casefold()

    assert "64-bit cpython" in build
    assert "pyside6-addons" in build
    assert "unapproved qt distributions" in build


def test_build_integrates_qt_and_replaceable_lgpl_package_gates() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert "if ($OneFile)" in build
    assert "One-file builds are not a supported distribution" in build
    assert "collect_qt_runtime_notices.py" in build
    assert "--check-notice" in build
    assert "--write-package-files" in build
    assert "--check-package-files" in build
    assert "collect_lgpl_runtime_sources.py" in build
    assert "--check-manifest" in build
    assert "LGPL_RUNTIME_REPLACEMENT_KO_EN.md" in build
