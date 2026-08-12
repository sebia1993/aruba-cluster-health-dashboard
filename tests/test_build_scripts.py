from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_copies_root_mit_license_to_release_package() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert '(Join-Path $repo "LICENSE")' in build
    assert '(Join-Path $releaseRoot "LICENSE.txt")' in build


def test_windows_build_copies_performance_report() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert '(Join-Path $repo "docs\\PERFORMANCE_REPORT_KO.md")' in build
    assert '(Join-Path $releaseRoot "PERFORMANCE_REPORT_KO.md")' in build


def test_spec_keeps_only_korean_qt_translations_and_excludes_cli_helpers() -> None:
    spec = (ROOT / "ArubaMiniDashboard.spec").read_text(encoding="utf-8")

    assert '"pyside6/translations/qt_ko.qm"' in spec
    assert '"pyside6/translations/qtbase_ko.qm"' in spec
    assert 'destination.startswith("pyside6/translations/")' in spec
    for module in ("netmiko.cli_tools", "rich", "ruamel", "markdown_it", "pygments"):
        assert f'"{module}"' in spec
