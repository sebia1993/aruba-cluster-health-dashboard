from __future__ import annotations

import ipaddress
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, Sequence, TypeVar

from aruba_mini_dashboard.connection_types import normalize_connection_type
from aruba_mini_dashboard.models import ParseIssue, ParseResult, ParseStatus


T = TypeVar("T")


PAGER_RE = re.compile(
    r"(?:--[^\S\r\n]*more[^\S\r\n]*--|"
    r"press[^\S\r\n]+(?:any[^\S\r\n]+key|space)[^\S\r\n]+to[^\S\r\n]+continue|"
    r"more:[^\S\r\n]*<space>|\(q\)uit)",
    re.IGNORECASE,
)
CREDENTIAL_LINE_RE = re.compile(
    r"(?im)^([^\S\r\n]*(?:password|passwd|enable(?:[^\S\r\n]|[_-])+secret)"
    r"[^\S\r\n]*[:=])[^\S\r\n]*.*$"
)
IPV4_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
SEPARATOR_RE = re.compile(r"^\s*[-=+_]{3,}(?:\s+[-=+_]{2,})*\s*$")
PROMPT_RE = re.compile(r"^\s*\([^)]*\)\s*[#>]\s*$|^\s*[\w.-]+\s*[#>]\s*$")
FOOTER_RE = re.compile(
    r"^\s*(?:total(?:\s+\w+)*\s*:|number\s+of\s+|entries\s*:|displayed\s*:)",
    re.IGNORECASE,
)
TRAILING_LINE_WHITESPACE_RE = re.compile(r"[^\S\n]+(?=\n|$)")
HEADER_SEPARATOR_RE = re.compile(r"[\s_-]+")
HEADER_SEGMENT_RE = re.compile(r"\S(?:.*?\S)?(?=\s{2,}|$)")
PLAIN_NONNEGATIVE_INT_RE = re.compile(r"[0-9]+")
GROUPED_NONNEGATIVE_INT_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+")
LOOSE_IPV4_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
MAX_NUMERIC_FIELD_DIGITS = 20
MAX_TABLE_LINE_CHARACTERS = 8_192
PARSE_CANCEL_CHECK_INTERVAL = 256


class ParserCancelledError(RuntimeError):
    """Cooperative cancellation raised while walking a bounded table."""


def check_parser_cancelled(
    cancel_event: threading.Event | None,
    item_index: int | None = None,
) -> None:
    if cancel_event is None:
        return
    if item_index is not None and item_index % PARSE_CANCEL_CHECK_INTERVAL:
        return
    if cancel_event.is_set():
        raise ParserCancelledError("parser cancelled")


def _apply_backspaces(value: str) -> str:
    chars: list[str] = []
    for char in value:
        if char == "\b":
            if chars:
                chars.pop()
        else:
            chars.append(char)
    return "".join(chars)


def _strip_terminal_sequences(value: str) -> str:
    """Remove CSI/OSC terminal controls in one bounded, linear pass.

    A regex that searches for an OSC terminator from every ``ESC ]`` prefix is
    quadratic on a malformed response containing many unterminated prefixes.
    Treat an unterminated OSC as decoration through end-of-output; retaining it
    could render arbitrary terminal control text in diagnostics anyway.
    """

    if "\x1b" not in value:
        return value
    result: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        if character != "\x1b":
            result.append(character)
            index += 1
            continue

        index += 1
        if index >= length:
            break
        introducer = value[index]
        if introducer == "]":
            index += 1
            while index < length:
                if value[index] == "\x07":
                    index += 1
                    break
                if (
                    value[index] == "\x1b"
                    and index + 1 < length
                    and value[index + 1] == "\\"
                ):
                    index += 2
                    break
                index += 1
            continue
        if introducer == "[":
            index += 1
            while index < length and "0" <= value[index] <= "?":
                index += 1
            while index < length and " " <= value[index] <= "/":
                index += 1
            if index < length and "@" <= value[index] <= "~":
                index += 1
            continue
        # Unknown two-byte escape: discard the ESC byte while preserving the
        # following printable character as ordinary text.
        result.append(introducer)
        index += 1
    return "".join(result)


def sanitize_output(output: str | bytes | None) -> str:
    """Remove terminal decoration without changing table column positions.

    Newlines and ordinary spaces are retained because the concrete parsers use
    header positions.  Invalid bytes are replaced rather than propagating an
    exception into the polling worker.
    """

    if output is None:
        return ""
    if isinstance(output, bytes):
        value = output.decode("utf-8", errors="replace")
    else:
        value = str(output)

    # Most device output is already clean. Avoid allocating a second multi-MB
    # string when no newline normalization, terminal cleanup, redaction, pager
    # removal, or per-line rstrip is required. The existing regexes remain
    # authoritative, so the fast path cannot bypass secret redaction.
    if not any(marker in value for marker in ("\r", "\x00", "\x1b", "\b")):
        if (
            PAGER_RE.search(value) is None
            and CREDENTIAL_LINE_RE.search(value) is None
            and TRAILING_LINE_WHITESPACE_RE.search(value) is None
        ):
            return value

    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    value = _strip_terminal_sequences(value)
    value = _apply_backspaces(value)
    value = PAGER_RE.sub("", value)
    value = CREDENTIAL_LINE_RE.sub(r"\1 [REDACTED]", value)
    return "\n".join(line.rstrip() for line in value.split("\n"))


def output_excerpt(output: str, limit: int = 2_048) -> str:
    clean = sanitize_output(output).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


@lru_cache(maxsize=512)
def normalize_header(value: str) -> str:
    return HEADER_SEPARATOR_RE.sub("", value).casefold()


def normalize_mm_status(value: str) -> str:
    collapsed = " ".join(value.split())
    normalized = collapsed.casefold()
    if normalized == "up":
        return "Up"
    if normalized == "down":
        return "Down"
    return collapsed


def valid_ipv4(value: str) -> str | None:
    candidate = value.strip().strip("[](),;:")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return str(address) if address.version == 4 else None


def find_ipv4(value: str) -> str | None:
    for match in IPV4_CANDIDATE_RE.finditer(value):
        parsed = valid_ipv4(match.group(0))
        if parsed is not None:
            return parsed
    return None


def parse_nonnegative_int(value: str) -> int | None:
    candidate = value.strip()
    if "," in candidate:
        if not GROUPED_NONNEGATIVE_INT_RE.fullmatch(candidate):
            return None
        digits = candidate.replace(",", "")
    else:
        if not PLAIN_NONNEGATIVE_INT_RE.fullmatch(candidate):
            return None
        digits = candidate
    # Avoid Python's large-decimal conversion limit and pointless big-integer
    # work on a malformed device response. Twenty digits still accommodates
    # every unsigned 64-bit decimal representation used by counters.
    if len(digits) > MAX_NUMERIC_FIELD_DIGITS:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


@dataclass(slots=True, frozen=True)
class HeaderColumn:
    canonical: str
    label: str
    start: int
    end: int | None


@dataclass(slots=True, frozen=True)
class HeaderLayout:
    line_index: int
    columns: Mapping[str, HeaderColumn]

    @property
    def header_map(self) -> dict[str, str]:
        return {name: column.label for name, column in self.columns.items()}

    def extract(self, line: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for name, column in self.columns.items():
            fields[name] = line[column.start : column.end].strip()
        return fields


def _header_segments(line: str) -> list[tuple[str, int, int | None]]:
    matches = list(HEADER_SEGMENT_RE.finditer(line))
    segments: list[tuple[str, int, int | None]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else None
        segments.append((match.group(0), match.start(), next_start))
    return segments


def _map_segment_headers(
    line: str, aliases: Mapping[str, Sequence[str]]
) -> dict[str, HeaderColumn]:
    columns: dict[str, HeaderColumn] = {}
    alias_lookup: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            alias_lookup[normalize_header(name)] = canonical
    for label, start, end in _header_segments(line):
        canonical = alias_lookup.get(normalize_header(label))
        if canonical is not None and canonical not in columns:
            columns[canonical] = HeaderColumn(canonical, label.strip(), start, end)
    return columns


@lru_cache(maxsize=128)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    words = [part for part in re.split(r"[\s_-]+", alias.strip()) if part]
    body = r"[\s_-]+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _map_position_headers(
    line: str, aliases: Mapping[str, Sequence[str]]
) -> dict[str, HeaderColumn]:
    hits: list[tuple[int, int, str, str]] = []
    for canonical, names in aliases.items():
        candidates: list[tuple[int, int, str, str]] = []
        for alias in sorted(names, key=len, reverse=True):
            match = _alias_pattern(alias).search(line)
            if match:
                candidates.append((match.start(), match.end(), canonical, match.group(0)))
        if candidates:
            # Prefer the longest match.  This keeps "Switch IP" anchored at the
            # beginning of its field rather than matching only its "IP" suffix.
            hits.append(max(candidates, key=lambda item: (item[1] - item[0], -item[0])))
    hits.sort(key=lambda item: item[0])
    columns: dict[str, HeaderColumn] = {}
    for index, (start, _match_end, canonical, label) in enumerate(hits):
        end = hits[index + 1][0] if index + 1 < len(hits) else None
        columns[canonical] = HeaderColumn(canonical, label.strip(), start, end)
    return columns


def find_header_layout(
    lines: Sequence[str],
    aliases: Mapping[str, Sequence[str]],
    required: Iterable[str],
    *,
    cancel_event: threading.Event | None = None,
) -> HeaderLayout | None:
    required_set = set(required)
    for line_index, line in enumerate(lines):
        check_parser_cancelled(cancel_event, line_index)
        if not line.strip():
            continue
        # The three supported Aruba tables use short fixed-width rows. Do not
        # run the header regexes across a multi-megabyte unbroken line from a
        # malformed or hostile peer; valid headers later in the output remain
        # discoverable.
        if len(line) > MAX_TABLE_LINE_CHARACTERS:
            continue
        columns = _map_segment_headers(line, aliases)
        if not required_set.issubset(columns):
            columns = _map_position_headers(line, aliases)
        if required_set.issubset(columns):
            return HeaderLayout(line_index=line_index, columns=columns)
    return None


def is_ignorable_table_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or bool(SEPARATOR_RE.match(stripped))
        or bool(PROMPT_RE.match(stripped))
        or bool(FOOTER_RE.match(stripped))
    )


def probable_data_line(line: str) -> bool:
    if IPV4_CANDIDATE_RE.search(line):
        return True
    return bool(LOOSE_IPV4_RE.search(line))


def finalize_result(
    *,
    rows: list[T],
    issues: list[ParseIssue],
    layout: HeaderLayout | None,
    output: str,
    metadata: dict[str, object] | None = None,
) -> ParseResult[T]:
    if layout is None:
        issues.insert(
            0,
            ParseIssue(
                code="PARSE_HEADER_MISSING",
                message="명령 출력에서 필요한 표 머리글을 찾지 못했습니다.",
            ),
        )
        status = ParseStatus.FAILED
    elif not rows:
        issues.append(
            ParseIssue(
                code="PARSE_NO_VALID_ROWS",
                message="표 머리글 아래에서 유효한 장비 행을 찾지 못했습니다.",
            )
        )
        status = ParseStatus.FAILED
    elif issues:
        status = ParseStatus.PARTIAL
    else:
        status = ParseStatus.COMPLETE
    return ParseResult(
        status=status,
        rows=rows,
        issues=issues,
        header_map={} if layout is None else layout.header_map,
        metadata={} if metadata is None else metadata,
        output_excerpt=output_excerpt(output),
    )
