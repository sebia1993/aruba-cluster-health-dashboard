from __future__ import annotations

import pytest

from aruba_mini_dashboard.connection_types import (
    clean_legacy_connection_type,
    normalize_connection_type,
)


@pytest.mark.parametrize(
    ("stored", "expected"),
    (
        ("N/A CONNECTED (Leader)", "N/A"),
        ("L2-Connected CONNECTED (Member, last HBT_RSP 44ms ago)", "L2-Connected"),
        ("l3_connected DISCONNECTED", "L3-Connected"),
        ("N/A ISOLATED (Leader)", "N/A"),
        ("N/A SECURE-TUNNEL-NEGOTIATING", "N/A"),
    ),
)
def test_known_legacy_status_suffixes_are_removed(stored: str, expected: str) -> None:
    cleaned, changed = clean_legacy_connection_type(stored)

    assert cleaned == expected
    assert changed is True
    assert normalize_connection_type(cleaned) == normalize_connection_type(expected)


@pytest.mark.parametrize(
    "stored",
    (
        "N/A",
        "L2-Connected",
        "Vendor-Future CONNECTED (Member)",
        "L2-Connected VENDOR-UNKNOWN",
    ),
)
def test_unknown_or_already_clean_values_are_preserved(stored: str) -> None:
    assert clean_legacy_connection_type(stored) == (stored, False)
