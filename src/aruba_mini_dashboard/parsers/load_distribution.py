from __future__ import annotations

import re
import threading

from aruba_mini_dashboard.models import ClientDistributionRow, ParseIssue, ParseResult
from aruba_mini_dashboard.parsers.common import (
    MAX_TABLE_LINE_CHARACTERS,
    check_parser_cancelled,
    finalize_result,
    find_header_layout,
    find_ipv4,
    is_ignorable_table_line,
    parse_nonnegative_int,
    probable_data_line,
    sanitize_output,
)


HEADER_ALIASES = {
    "ip": ("Switch IP", "IP Address", "IPv4 Address", "IP"),
    "active_clients": ("Active Clients", "Active-Clients", "ActiveClients"),
    "standby_clients": ("Standby Clients", "Standby-Clients", "StandbyClients"),
}

TOTAL_ACTIVE_PATTERNS = (
    re.compile(r"\bTotal\s*:\s*Active[\s-]*Clients\s*(?:[:=]\s*)?(\S+)", re.IGNORECASE),
    re.compile(r"\bTotal[\s-]*Active[\s-]*Clients\s*[:=]\s*(\S+)", re.IGNORECASE),
)


def _reported_total_active(
    lines: list[str],
    cancel_event: threading.Event | None = None,
) -> tuple[int | None, bool]:
    for line_index, line in enumerate(lines):
        check_parser_cancelled(cancel_event, line_index)
        for pattern in TOTAL_ACTIVE_PATTERNS:
            match = pattern.search(line)
            if match:
                value = parse_nonnegative_int(match.group(1))
                return value, value is None
    return None, False


def parse_load_distribution(
    output: str | bytes | None,
    *,
    cancel_event: threading.Event | None = None,
) -> ParseResult[ClientDistributionRow]:
    check_parser_cancelled(cancel_event)
    clean = sanitize_output(output)
    lines = clean.splitlines()
    issues: list[ParseIssue] = []
    if not clean.strip():
        issues.append(ParseIssue("EMPTY_OUTPUT", "장비가 빈 명령 결과를 반환했습니다."))

    layout = find_header_layout(
        lines,
        HEADER_ALIASES,
        required=("ip", "active_clients", "standby_clients"),
        cancel_event=cancel_event,
    )
    rows: list[ClientDistributionRow] = []
    seen: set[str] = set()
    if layout is not None:
        for line_index, line in enumerate(lines[layout.line_index + 1 :], layout.line_index + 2):
            check_parser_cancelled(cancel_event, line_index)
            if len(line) > MAX_TABLE_LINE_CHARACTERS:
                issues.append(
                    ParseIssue(
                        "PARSE_ROW_TOO_LONG",
                        "Client 분배 표의 행이 안전한 최대 길이를 초과했습니다.",
                        line_index,
                        line[:240],
                    )
                )
                continue
            if is_ignorable_table_line(line):
                continue
            fields = layout.extract(line)
            ip = find_ipv4(fields.get("ip", ""))
            if ip is None and probable_data_line(line):
                ip = find_ipv4(line)
            if ip is None:
                if probable_data_line(line):
                    issues.append(
                        ParseIssue(
                            "INVALID_IP",
                            "Client 분배 행에 유효하지 않은 IPv4 주소가 있습니다.",
                            line_index,
                            line[:240],
                        )
                    )
                continue

            active = parse_nonnegative_int(fields.get("active_clients", ""))
            standby = parse_nonnegative_int(fields.get("standby_clients", ""))
            if active is None or standby is None:
                issues.append(
                    ParseIssue(
                        "INVALID_CLIENT_COUNT",
                        f"{ip}의 Client 수가 0 이상의 정수가 아닙니다.",
                        line_index,
                        line[:240],
                    )
                )
                continue
            if ip in seen:
                issues.append(
                    ParseIssue(
                        "DUPLICATE_IP",
                        f"Client 분배 표에 IPv4 주소 {ip}가 중복되어 있습니다.",
                        line_index,
                        line[:240],
                    )
                )
                continue
            seen.add(ip)
            rows.append(
                ClientDistributionRow(
                    ip=ip,
                    active_clients=active,
                    standby_clients=standby,
                    raw_fields=dict(fields),
                )
            )

    computed_total = sum(row.active_clients for row in rows)
    reported_total, invalid_reported_total = _reported_total_active(lines, cancel_event)
    if invalid_reported_total:
        issues.append(
            ParseIssue(
                "INVALID_TOTAL_ACTIVE",
                "출력에 표시된 전체 Active Client 수가 올바른 정수가 아닙니다.",
            )
        )
    total_conflict = reported_total is not None and reported_total != computed_total
    if rows and total_conflict:
        issues.append(
            ParseIssue(
                "TOTAL_ACTIVE_MISMATCH",
                "출력에 표시된 전체 Active Client 수와 파싱한 행의 합계가 다릅니다.",
            )
        )
    metadata: dict[str, object] = {
        "computed_total_active": computed_total,
        "reported_total_active": reported_total,
        "total_active": reported_total if reported_total is not None else computed_total,
        "total_active_conflict": total_conflict,
    }
    check_parser_cancelled(cancel_event)
    return finalize_result(
        rows=rows,
        issues=issues,
        layout=layout,
        output=clean,
        metadata=metadata,
    )


class LoadDistributionParser:
    def parse(self, output: str | bytes | None) -> ParseResult[ClientDistributionRow]:
        return parse_load_distribution(output)
