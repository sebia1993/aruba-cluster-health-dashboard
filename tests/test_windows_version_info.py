from __future__ import annotations

import runpy
from pathlib import Path

import pytest


NAMESPACE = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "generate_windows_version_info.py"),
    run_name="generate_windows_version_info",
)


def test_version_resource_contains_expected_product_metadata() -> None:
    text = NAMESPACE["render_version_info"](
        version="1.2.3",
        description="Aruba MM and WLC Mini Dashboard",
        original_filename="ArubaMiniDashboard.exe",
    )

    assert "filevers=(1, 2, 3, 0)" in text
    assert "StringStruct('ProductName', 'Aruba Mini Dashboard')" in text
    assert "StringStruct('FileVersion', '1.2.3.0')" in text
    assert "StringStruct('OriginalFilename', 'ArubaMiniDashboard.exe')" in text


def test_prerelease_suffix_uses_numeric_version_prefix() -> None:
    assert NAMESPACE["version_parts"]("2.0.1-rc.2") == (2, 0, 1, 0)


def test_non_numeric_version_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="three numeric components"):
        NAMESPACE["render_version_info"](
            version="development",
            description="Dashboard",
            original_filename="ArubaMiniDashboard.exe",
        )
