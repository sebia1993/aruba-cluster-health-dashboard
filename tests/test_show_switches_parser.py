from __future__ import annotations

from pathlib import Path

import pytest

from aruba_mini_dashboard.models import ParseStatus
from aruba_mini_dashboard.parsers import parse_show_switches


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_all_up_is_complete_and_normalized() -> None:
    result = parse_show_switches(fixture("mm_show_switches_normal.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert len(result.rows) == 4
    assert {row.status for row in result.rows} == {"Up"}
    assert result.rows[1].hostname == "WLC-02"


def test_one_down_identifies_the_exact_ip() -> None:
    result = parse_show_switches(fixture("mm_show_switches_down.txt"))
    assert [row.ip for row in result.rows if row.status == "Down"] == ["192.0.2.12"]


def test_multiple_down_rows_are_preserved() -> None:
    result = parse_show_switches(fixture("mm_show_switches_multiple_down.txt"))
    assert [row.ip for row in result.rows if row.status == "Down"] == [
        "192.0.2.12",
        "192.0.2.13",
    ]


def test_status_case_variants_are_normalized() -> None:
    result = parse_show_switches(fixture("mm_show_switches_status_variants.txt"))
    assert [row.status for row in result.rows] == ["Up", "Up", "Up", "Down"]


def test_missing_status_header_is_a_parse_failure() -> None:
    result = parse_show_switches(fixture("mm_show_switches_missing_status.txt"))
    assert result.status is ParseStatus.FAILED
    assert not result.rows
    assert any(issue.code == "PARSE_HEADER_MISSING" for issue in result.issues)


@pytest.mark.parametrize("output", ["", None, fixture("malformed_output.txt")])
def test_empty_or_malformed_output_never_raises_and_fails_closed(output: str | None) -> None:
    result = parse_show_switches(output)
    assert result.status is ParseStatus.FAILED
    assert not result.rows


def test_ansi_pager_and_header_location_are_tolerated() -> None:
    output = """[32mArubaOS 8.x[0m
unrelated preamble
Switch-IP        Host Name         STATUS
---------        ---------         ------
192.0.2.11       WLC-01            up--More--
192.0.2.12       WLC-02            DOWN
"""
    result = parse_show_switches(output)
    assert result.status is ParseStatus.COMPLETE
    assert [(row.ip, row.status) for row in result.rows] == [
        ("192.0.2.11", "Up"),
        ("192.0.2.12", "Down"),
    ]


def test_output_excerpt_redacts_accidental_password_prompt_content() -> None:
    output = """Password: should-never-be-visible
Switch IP        Name              Status
192.0.2.11       WLC-01            Up
"""
    result = parse_show_switches(output)
    assert "should-never-be-visible" not in result.output_excerpt
    assert "[REDACTED]" in result.output_excerpt


def test_broken_row_does_not_discard_other_valid_rows() -> None:
    output = """IP Address       Name              Status
192.0.2.11       WLC-01            Up
999.999.2.12     WLC-BAD           Down
192.0.2.13       WLC-03            Up
"""
    result = parse_show_switches(output)
    assert result.status is ParseStatus.PARTIAL
    assert [row.ip for row in result.rows] == ["192.0.2.11", "192.0.2.13"]
    assert any(issue.code == "INVALID_IP" for issue in result.issues)


def test_duplicate_ip_is_partial_and_first_valid_row_wins() -> None:
    output = """IP Address       Name              Status
192.0.2.11       WLC-01            Up
192.0.2.11       DUPLICATE        Down
"""
    result = parse_show_switches(output)
    assert result.status is ParseStatus.PARTIAL
    assert len(result.rows) == 1
    assert result.rows[0].status == "Up"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("mm_show_switches_*.txt")), ids=lambda p: p.name)
def test_every_mm_fixture_can_be_added_or_replaced_without_crashing(path: Path) -> None:
    result = parse_show_switches(path.read_text(encoding="utf-8"))
    assert isinstance(result.status, ParseStatus)
