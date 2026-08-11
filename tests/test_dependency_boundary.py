from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_qt_runtime_uses_essentials_without_addons_meta_package() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
    lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").casefold()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()

    assert "pyside6-essentials==6.11.0" in requirements
    assert "pyside6-essentials==6.11.0" in lock
    assert "pyside6-essentials==6.11.0" in project
    assert "pyside6-addons" not in requirements
    assert "pyside6-addons" not in lock
    assert re.search(r"(?m)^pyside6==", lock) is None


def test_pyinstaller_spec_explicitly_excludes_disallowed_qt_modules() -> None:
    spec = (ROOT / "ArubaMiniDashboard.spec").read_text(encoding="utf-8").casefold()

    for artifact in (
        "qt6virtualkeyboard.dll",
        "qtvirtualkeyboardplugin.dll",
        "qt6pdf.dll",
        "qpdf.dll",
    ):
        assert artifact in spec
