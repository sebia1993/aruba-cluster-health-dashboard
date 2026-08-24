from __future__ import annotations

from pathlib import Path

import pytest

from aruba_mini_dashboard.models import ParseStatus
from aruba_mini_dashboard.parsers import parse_group_membership


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_initial_membership_parses_all_rows() -> None:
    result = parse_group_membership(fixture("group_membership_initial.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert len(result.rows) == 4
    assert {row.connection_type for row in result.rows} == {"Type-A"}


def test_changed_membership_preserves_display_value() -> None:
    result = parse_group_membership(fixture("group_membership_changed.txt"))
    row = next(item for item in result.rows if item.ip == "192.0.2.12")
    assert row.connection_type == "Type-B"


def test_missing_row_remains_distinguishable_from_a_change() -> None:
    result = parse_group_membership(fixture("group_membership_missing_member.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert "192.0.2.12" not in {row.ip for row in result.rows}


@pytest.mark.parametrize("output", ["", None, fixture("malformed_output.txt")])
def test_empty_or_malformed_output_fails_closed(output: str | None) -> None:
    result = parse_group_membership(output)
    assert result.status is ParseStatus.FAILED


def test_header_spacing_hyphen_ansi_and_pager_are_supported() -> None:
    output = """[33mSwitch-IP        Connection Type[0m
192.0.2.11       L2-Connected--More--
192.0.2.12       N/A
"""
    result = parse_group_membership(output)
    assert result.status is ParseStatus.COMPLETE
    assert [row.connection_type for row in result.rows] == ["L2-Connected", "N/A"]


def test_7240xm_full_table_keeps_dynamic_status_out_of_connection_type() -> None:
    result = parse_group_membership(fixture("group_membership_7240xm.txt"))

    assert result.status is ParseStatus.COMPLETE
    assert [row.connection_type for row in result.rows] == [
        "N/A",
        "L2-Connected",
        "L2-Connected",
        "L2-Connected",
    ]
    member = next(row for row in result.rows if row.ip == "192.0.2.12")
    assert member.raw_fields["status"] == (
        "CONNECTED (Member, last HBT_RSP 44ms ago, RTD = 0.000 ms)"
    )
    assert member.raw_fields["priority"] == "128"


def test_broken_row_does_not_discard_valid_members() -> None:
    output = """IP Address       Connection-Type
192.0.2.11       Type-A
999.0.2.12       Type-B
192.0.2.13       Type-C
"""
    result = parse_group_membership(output)
    assert result.status is ParseStatus.PARTIAL
    assert [row.ip for row in result.rows] == ["192.0.2.11", "192.0.2.13"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("group_membership_*.txt")), ids=lambda p: p.name)
def test_every_membership_fixture_can_be_added_or_replaced_without_crashing(path: Path) -> None:
    result = parse_group_membership(path.read_text(encoding="utf-8"))
    assert isinstance(result.status, ParseStatus)
