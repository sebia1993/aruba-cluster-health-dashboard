"""Connection-Type normalization and legacy parser compatibility helpers."""

from __future__ import annotations

import re


_CONNECTION_TYPE_SEPARATOR_RE = re.compile(r"[\s_-]+")
_KNOWN_CONNECTION_TYPE_RE = re.compile(
    r"^(?P<value>n\s*/\s*a|l\s*(?P<layer>[23])[\s_-]*connected)"
    r"(?P<suffix>(?:\s+.*)?)$",
    re.IGNORECASE,
)
_LEGACY_STATUS_PREFIX_RE = re.compile(
    r"^(?:"
    r"connected(?=\s|\(|$)|"
    r"disconnected(?=\s|$)|"
    r"isolated(?=\s|\(|$)|"
    r"secure-tunnel-[a-z0-9_-]+(?=\s|$)"
    r")",
    re.IGNORECASE,
)


def normalize_connection_type(value: object) -> str:
    """Normalize display-only separators without changing semantic tokens."""

    return _CONNECTION_TYPE_SEPARATOR_RE.sub("", str(value).strip()).casefold()


def clean_legacy_connection_type(value: object) -> tuple[str, bool]:
    """Remove a STATUS suffix captured by the pre-v5 membership parser.

    The old fixed-width parser extended ``Connection-Type`` to the end of the
    row when it did not recognize the following ``STATUS`` header.  Only the
    documented cluster connection values and known STATUS prefixes are
    repaired here; unknown values are preserved rather than guessed.
    """

    collapsed = " ".join(str(value).split())
    match = _KNOWN_CONNECTION_TYPE_RE.fullmatch(collapsed)
    if match is None:
        return collapsed, False

    suffix = match.group("suffix").strip()
    if not suffix or _LEGACY_STATUS_PREFIX_RE.match(suffix) is None:
        return collapsed, False

    layer = match.group("layer")
    canonical = "N/A" if layer is None else f"L{layer}-Connected"
    return canonical, True
