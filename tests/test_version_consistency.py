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
    repository_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    packaged_readme = (ROOT / "docs" / "README.txt").read_text(encoding="utf-8")
    release_process = (ROOT / "docs" / "RELEASE_PROCESS_KO.md").read_text(
        encoding="utf-8"
    )
    release_workflow = (ROOT / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )

    assert package_match is not None
    assert changelog_match is not None
    assert package_match.group(1) == project_version
    assert changelog_match.group(1) == project_version
    assert packaged_readme.startswith(f"Aruba Mini Dashboard {project_version}\n")
    assert f".\\scripts\\package_release.ps1 -Version {project_version}" in repository_readme
    assert f".\\scripts\\package_release.ps1 -Version {project_version}" in release_process
    assert f"ArubaMiniDashboard-v{project_version}-windows-x64.zip" in release_process
    # The workflow input description is an example rather than a source-of-truth
    # version declaration. Functional validation below resolves the requested tag
    # against pyproject.toml, package __version__, annotated tag, and current main.
    assert 'description: "Existing annotated release tag, for example v' in release_workflow
