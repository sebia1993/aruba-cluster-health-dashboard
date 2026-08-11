from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-windows.yml"


def test_windows_ci_is_read_only_and_uses_pinned_actions() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert yaml.compose(text) is not None
    assert "pull_request:" in text
    assert "branches:\n      - main" in text
    assert "permissions:\n  contents: read" in text
    assert "persist-credentials: false" in text
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in text
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in text
    assert 'python-version: "3.11.9"' in text
    assert "package_release.ps1 -Version" in text


def test_windows_ci_does_not_publish_or_use_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()

    assert "contents: write" not in text
    assert "gh release" not in text
    assert "secrets." not in text
    assert "workflow_dispatch" not in text
