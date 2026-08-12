from __future__ import annotations

from aruba_mini_dashboard.parsers.common import (
    _alias_pattern,
    normalize_header,
    sanitize_output,
)


def test_sanitize_output_clean_fast_path_preserves_exact_text() -> None:
    output = "Switch IP       Name       Status\n192.0.2.11      WLC-01     Up"

    assert sanitize_output(output) == output


def test_sanitize_output_fast_path_cannot_bypass_cleanup_or_redaction() -> None:
    output = (
        "Password: should-not-survive  \r\n"
        "\x1b[31m192.0.2.11\x1b[0m --More--\r\n"
        "value-overwritten\b!  "
    )

    clean = sanitize_output(output)

    assert "should-not-survive" not in clean
    assert "[REDACTED]" in clean
    assert "\x1b" not in clean
    assert "More" not in clean
    assert clean.endswith("value-overwritte!")
    assert "\r" not in clean


def test_parser_normalization_caches_are_bounded_and_behavior_preserving() -> None:
    normalize_header.cache_clear()
    assert normalize_header("Connection-Type") == "connectiontype"
    assert normalize_header("Connection-Type") == "connectiontype"
    assert normalize_header.cache_info().hits == 1
    assert normalize_header.cache_info().maxsize == 512

    first = _alias_pattern("Switch IP")
    second = _alias_pattern("Switch IP")
    assert first is second
    assert first.search("Switch-IP       Name") is not None
    assert _alias_pattern.cache_info().maxsize == 128
