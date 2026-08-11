from __future__ import annotations

from aruba_mini_dashboard.models import MmSwitchRow, ParseIssue, ParseResult
from aruba_mini_dashboard.parsers.common import (
    finalize_result,
    find_header_layout,
    find_ipv4,
    is_ignorable_table_line,
    normalize_mm_status,
    probable_data_line,
    sanitize_output,
)


HEADER_ALIASES = {
    "ip": ("Switch IP", "IP Address", "IPv4 Address", "IP"),
    "hostname": ("Switch Name", "Host Name", "Hostname", "Name"),
    "status": ("Status",),
}


def parse_show_switches(output: str | bytes | None) -> ParseResult[MmSwitchRow]:
    clean = sanitize_output(output)
    lines = clean.splitlines()
    issues: list[ParseIssue] = []
    if not clean.strip():
        issues.append(ParseIssue("EMPTY_OUTPUT", "장비가 빈 명령 결과를 반환했습니다."))

    layout = find_header_layout(lines, HEADER_ALIASES, required=("ip", "status"))
    rows: list[MmSwitchRow] = []
    seen: set[str] = set()
    if layout is not None:
        for line_index, line in enumerate(lines[layout.line_index + 1 :], layout.line_index + 2):
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
                            "스위치 행에 유효하지 않은 IPv4 주소가 있습니다.",
                            line_index,
                            line[:240],
                        )
                    )
                continue

            status = normalize_mm_status(fields.get("status", ""))
            if not status:
                issues.append(
                    ParseIssue(
                        "INVALID_STATUS",
                        "스위치 행에서 Status 값을 찾지 못했습니다.",
                        line_index,
                        line[:240],
                    )
                )
                continue
            if ip in seen:
                issues.append(
                    ParseIssue(
                        "DUPLICATE_IP",
                        f"스위치 표에 IPv4 주소 {ip}가 중복되어 있습니다.",
                        line_index,
                        line[:240],
                    )
                )
                continue
            seen.add(ip)
            hostname = " ".join(fields.get("hostname", "").split()) or None
            rows.append(
                MmSwitchRow(
                    ip=ip,
                    hostname=hostname,
                    status=status,
                    raw_fields=dict(fields),
                )
            )

    return finalize_result(rows=rows, issues=issues, layout=layout, output=clean)


class ShowSwitchesParser:
    def parse(self, output: str | bytes | None) -> ParseResult[MmSwitchRow]:
        return parse_show_switches(output)
