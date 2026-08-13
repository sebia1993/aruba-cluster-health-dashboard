from __future__ import annotations

from aruba_mini_dashboard.parsers.common import (
    _alias_pattern,
    _strip_terminal_sequences,
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


def test_terminal_sequence_stripping_is_linear_and_fail_closed_on_unterminated_osc() -> None:
    assert _strip_terminal_sequences("before\x1b[31mred\x1b[0mafter") == "beforeredafter"
    assert _strip_terminal_sequences("before\x1b]title\x07after") == "beforeafter"
    assert _strip_terminal_sequences("before\x1b]title\x1b\\after") == "beforeafter"

    # A repeated unterminated OSC prefix made the previous regex rescan the
    # remaining suffix for every prefix. The single-pass implementation drops
    # the first unterminated control and its remainder without pathological CPU.
    assert _strip_terminal_sequences("\x1b]" * 50_000) == ""


def test_sanitizer_does_not_backtrack_across_many_whitespace_only_lines() -> None:
    value = " \n" * 50_000

    clean = sanitize_output(value)

    assert " " not in clean
    assert clean.count("\n") == 50_000
