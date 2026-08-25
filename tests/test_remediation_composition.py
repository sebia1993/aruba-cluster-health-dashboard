from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_main_composes_remediation_without_monkey_patching_mainwindow() -> None:
    main_source = (ROOT / "src" / "aruba_mini_dashboard" / "main.py").read_text(encoding="utf-8")
    launcher = (ROOT / "src" / "aruba_mini_dashboard" / "remediation" / "launcher.py").read_text(encoding="utf-8")
    entry = (ROOT / "scripts" / "pyinstaller_entry.py").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "RemediationFeatureController" in main_source
    assert "remediation_controller =" in main_source
    assert "module.MainWindow =" not in launcher
    assert "from aruba_mini_dashboard.main import main" in entry
    assert 'aruba-mini-dashboard = "aruba_mini_dashboard.main:main"' in project


def test_auto_release_can_retry_when_tag_exists_without_a_release() -> None:
    workflow = (ROOT / ".github" / "workflows" / "auto-release-on-version.yml").read_text(
        encoding="utf-8"
    )
    assert "release_missing" in workflow
    assert "gh release view" in workflow
    assert "steps.release.outputs.release_missing == 'true'" in workflow
