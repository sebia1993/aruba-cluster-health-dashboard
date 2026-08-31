from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


STATUS_NORMAL: Final = "normal"
STATUS_ATTENTION: Final = "attention"
STATUS_FAILURE: Final = "failure"
STATUS_UNKNOWN: Final = "unknown"

STATUS_KEYS: Final = (
    STATUS_NORMAL,
    STATUS_ATTENTION,
    STATUS_FAILURE,
    STATUS_UNKNOWN,
)

# These aliases preserve the meanings already used by the domain/view-model
# boundary. Presentation code may normalize vocabulary, but it must not infer a
# healthier or more severe state than the supplied value.
STATUS_ALIASES: Final = MappingProxyType(
    {
        "normal": STATUS_NORMAL,
        "ok": STATUS_NORMAL,
        "healthy": STATUS_NORMAL,
        "정상": STATUS_NORMAL,
        "attention": STATUS_ATTENTION,
        "warning": STATUS_ATTENTION,
        "degraded": STATUS_ATTENTION,
        "주의": STATUS_ATTENTION,
        "failure": STATUS_FAILURE,
        "critical": STATUS_FAILURE,
        "down": STATUS_FAILURE,
        "장애": STATUS_FAILURE,
        "unknown": STATUS_UNKNOWN,
        "unavailable": STATUS_UNKNOWN,
        "partial": STATUS_UNKNOWN,
        "확인 불가": STATUS_UNKNOWN,
        "확인불가": STATUS_UNKNOWN,
    }
)

STATUS_LABELS: Final = MappingProxyType(
    {
        STATUS_NORMAL: "정상",
        STATUS_ATTENTION: "주의",
        STATUS_FAILURE: "장애",
        STATUS_UNKNOWN: "확인 불가",
    }
)

# Existing light-theme status colors. The palette layer contrast-corrects these
# seeds and derives dark-palette variants without changing their semantics.
STATUS_COLOR_SEEDS: Final = MappingProxyType(
    {
        STATUS_NORMAL: ("#176B42", "#E8F7EF", "#2AA56A"),
        STATUS_ATTENTION: ("#805500", "#FFF5D8", "#E7A900"),
        STATUS_FAILURE: ("#8A1C1C", "#FDECEC", "#D33A3A"),
        STATUS_UNKNOWN: ("#374151", "#F1F3F5", "#77808D"),
    }
)


@dataclass(frozen=True, slots=True)
class SpacingTokens:
    xxs: int = 2
    xs: int = 4
    sm: int = 7
    md: int = 10
    lg: int = 12
    xl: int = 16
    xxl: int = 24


@dataclass(frozen=True, slots=True)
class RadiusTokens:
    sm: int = 4
    md: int = 6
    lg: int = 8
    pill: int = 999


@dataclass(frozen=True, slots=True)
class SizeTokens:
    icon_sm: int = 14
    icon_md: int = 18
    sparkline_height: int = 36
    card_minimum_width: int = 136


SPACING: Final = SpacingTokens()
RADIUS: Final = RadiusTokens()
SIZES: Final = SizeTokens()
