from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_project_package_and_changelog_versions_match() -> None:
    project_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    package_source = (ROOT / "src" / "aruba_mini_dashboard" / "__init__.py").read_text(
        encoding="utf-8"
    )
    package_match = re.search(r'^__version__\s*=\s*"([^"]+)"$', package_source, re.MULTILINE)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_match = re.search(r"^##\s+([^\s]+)\s+-\s+\d{4}-\d{2}-\d{2}$", changelog, re.MULTILINE)

    assert package_match is not None
    assert changelog_match is not None
    assert package_match.group(1) == project_version
    assert changelog_match.group(1) == project_version
