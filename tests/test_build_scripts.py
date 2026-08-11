from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_copies_root_mit_license_to_release_package() -> None:
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert '(Join-Path $repo "LICENSE")' in build
    assert '(Join-Path $releaseRoot "LICENSE.txt")' in build
