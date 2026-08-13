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
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in text
    assert text.count("actions/checkout@") == 1
    assert text.count("actions/setup-python@") == 1
    assert "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" not in text
    assert text.count('python-version: "3.13.15"') == 1
    assert text.count("sys.version_info[:3] == (3, 13, 15)") == 2
    assert text.count("platform.machine() == 'AMD64'") == 2
    assert text.count("not sysconfig.get_config_var('Py_GIL_DISABLED')") == 2
    assert text.count("sys._is_gil_enabled()") == 2
    assert "package_release.ps1 -Version" in text


def test_windows_ci_does_not_publish_or_use_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()

    assert "contents: write" not in text
    assert "gh release" not in text
    assert "secrets." not in text
    assert "workflow_dispatch" not in text
