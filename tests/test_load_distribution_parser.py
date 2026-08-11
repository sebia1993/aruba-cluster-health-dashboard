from __future__ import annotations

from pathlib import Path

import pytest

from aruba_mini_dashboard.models import ParseStatus
from aruba_mini_dashboard.parsers import parse_load_distribution


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_balanced_fixture_parses_all_members_and_total() -> None:
    result = parse_load_distribution(fixture("cluster_load_normal.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert len(result.rows) == 4
    assert result.metadata["total_active"] == 1015
    assert result.metadata["total_active_conflict"] is False


def test_abnormal_values_are_data_not_parser_failures() -> None:
    result = parse_load_distribution(fixture("cluster_load_abnormal.txt"))
    row = next(item for item in result.rows if item.ip == "192.0.2.12")
    assert (row.active_clients, row.standby_clients) == (0, 4)
    assert result.status is ParseStatus.COMPLETE


def test_comma_separated_numbers_are_converted() -> None:
    result = parse_load_distribution(fixture("cluster_load_comma.txt"))
    assert result.rows[0].active_clients == 1250
    assert result.rows[0].standby_clients == 1260
    assert result.metadata["total_active"] == 4995


def test_missing_member_is_a_valid_three_row_table_for_detector_to_classify() -> None:
    result = parse_load_distribution(fixture("cluster_load_missing_member.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert {row.ip for row in result.rows} == {"192.0.2.11", "192.0.2.13", "192.0.2.14"}


def test_all_low_values_parse_without_inventing_an_error() -> None:
    result = parse_load_distribution(fixture("cluster_load_all_low.txt"))
    assert result.status is ParseStatus.COMPLETE
    assert result.metadata["total_active"] == 10


@pytest.mark.parametrize("output", ["", None, fixture("malformed_output.txt")])
def test_empty_or_malformed_output_fails_closed(output: str | None) -> None:
    result = parse_load_distribution(output)
    assert result.status is ParseStatus.FAILED
    assert not result.rows


def test_invalid_numeric_row_is_skipped_but_valid_rows_survive() -> None:
    output = """IP Address       Active Clients    Standby Clients
192.0.2.11       250               260
192.0.2.12       unavailable       4
192.0.2.13       245               255
"""
    result = parse_load_distribution(output)
    assert result.status is ParseStatus.PARTIAL
    assert [row.ip for row in result.rows] == ["192.0.2.11", "192.0.2.13"]
    assert any(issue.code == "INVALID_CLIENT_COUNT" for issue in result.issues)


def test_reported_total_conflict_marks_result_partial() -> None:
    output = """IP Address       Active Clients    Standby Clients
192.0.2.11       25                30
192.0.2.12       25                30
Total: Active Clients 999
"""
    result = parse_load_distribution(output)
    assert result.status is ParseStatus.PARTIAL
    assert result.metadata["total_active_conflict"] is True
    assert any(issue.code == "TOTAL_ACTIVE_MISMATCH" for issue in result.issues)


def test_header_hyphens_ansi_and_pager_are_supported() -> None:
    output = """[2JIPv4-Address     Active-Clients    Standby-Clients
192.0.2.11       1,000             900--More--
192.0.2.12       800               700
Total-Active-Clients: 1,800
"""
    result = parse_load_distribution(output)
    assert result.status is ParseStatus.COMPLETE
    assert [(row.active_clients, row.standby_clients) for row in result.rows] == [
        (1000, 900),
        (800, 700),
    ]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("cluster_load_*.txt")), ids=lambda p: p.name)
def test_every_load_fixture_can_be_added_or_replaced_without_crashing(path: Path) -> None:
    result = parse_load_distribution(path.read_text(encoding="utf-8"))
    assert isinstance(result.status, ParseStatus)
