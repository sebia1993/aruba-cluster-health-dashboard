from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-release-on-version.yml"


def test_version_release_workflow_creates_annotated_tag_and_dispatches_verified_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.load(text, Loader=yaml.BaseLoader)
    assert parsed["on"]["push"]["branches"] == ["main"]
    assert parsed["permissions"] == {"contents": "write", "actions": "write"}
    assert "git tag -a" in text
    assert "gh workflow run release-windows.yml" in text
    assert "release_mode=publish-prerelease" in text
    assert "confirmation=$AMD_TAG" in text
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
