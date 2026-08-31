from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from PySide6.QtGui import QColor, QGuiApplication, QPalette

from .tokens import (
    STATUS_ALIASES,
    STATUS_ATTENTION,
    STATUS_COLOR_SEEDS,
    STATUS_FAILURE,
    STATUS_NORMAL,
    STATUS_UNKNOWN,
)


_SEMANTIC_STYLED_OBJECT_NAMES = frozenset(
    {
        "semanticStatusBadge",
        "reusableStatusCard",
        "metricCard",
        "recentEventRow",
    }
)


def blend_colors(first: QColor, second: QColor, second_weight: float) -> QColor:
    """Blend two opaque colors while clamping an untrusted weight."""

    weight = min(1.0, max(0.0, float(second_weight)))
    first_weight = 1.0 - weight
    return QColor(
        round(first.red() * first_weight + second.red() * weight),
        round(first.green() * first_weight + second.green() * weight),
        round(first.blue() * first_weight + second.blue() * weight),
    )


def relative_luminance(color: QColor) -> float:
    """Return WCAG relative luminance for an opaque sRGB color."""

    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * linear(color.red())
        + 0.7152 * linear(color.green())
        + 0.0722 * linear(color.blue())
    )


def contrast_ratio(first: QColor, second: QColor) -> float:
    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def ensure_minimum_contrast(
    foreground: QColor,
    background: QColor,
    fallback: QColor,
    minimum: float = 3.0,
) -> QColor:
    """Move a color toward readable text until it reaches the target ratio."""

    if contrast_ratio(foreground, background) >= minimum:
        return QColor(foreground)
    resolved_fallback = QColor(fallback)
    if contrast_ratio(resolved_fallback, background) < minimum:
        black = QColor("#000000")
        white = QColor("#ffffff")
        resolved_fallback = max(
            (black, white),
            key=lambda color: contrast_ratio(color, background),
        )

    best = QColor(resolved_fallback)
    low, high = 0.0, 1.0
    for _ in range(16):
        weight = (low + high) / 2.0
        candidate = blend_colors(foreground, resolved_fallback, weight)
        if contrast_ratio(candidate, background) >= minimum:
            best = candidate
            high = weight
        else:
            low = weight
    return best


def normalize_status_key(status: Any) -> str:
    """Normalize presentation aliases and fail closed to ``unknown``."""

    if isinstance(status, Enum):
        status = status.value
    key = (
        str(status or STATUS_UNKNOWN)
        .strip()
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
    )
    return STATUS_ALIASES.get(key, STATUS_UNKNOWN)


@dataclass(frozen=True, slots=True)
class StatusColors:
    foreground: QColor
    background: QColor
    accent: QColor


@dataclass(frozen=True, slots=True)
class SemanticPalette:
    normal: StatusColors
    attention: StatusColors
    failure: StatusColors
    unknown: StatusColors
    surface: QColor
    surface_alt: QColor
    text_primary: QColor
    text_secondary: QColor
    border: QColor
    focus: QColor

    def for_status(self, status: Any) -> StatusColors:
        return getattr(self, normalize_status_key(status))


def _active_palette(palette: QPalette | None) -> QPalette:
    if palette is not None:
        return QPalette(palette)
    application = QGuiApplication.instance()
    return QPalette(application.palette()) if application is not None else QPalette()


def presentation_palette(widget: Any | None = None) -> QPalette:
    """Return the native/container palette behind semantic widget QSS.

    Qt style sheets intentionally change the styled widget's resolved palette.
    Reading that palette again during an OS theme change would treat the old
    semantic card background as the new native surface and lock the widget in
    its previous light/dark appearance. Skip our own styled containers and use
    the first ordinary ancestor, falling back to the application palette.
    """

    if widget is not None:
        current = widget.parentWidget()
        while current is not None:
            if current.objectName() not in _SEMANTIC_STYLED_OBJECT_NAMES:
                return QPalette(current.palette())
            current = current.parentWidget()
    return _active_palette(None)


def _status_colors(
    key: str,
    *,
    surface: QColor,
    text_primary: QColor,
) -> StatusColors:
    foreground_seed, background_seed, accent_seed = (
        QColor(value) for value in STATUS_COLOR_SEEDS[key]
    )
    if relative_luminance(surface) < 0.35:
        # Retain the hue but derive the tint from the active OS/Qt surface.
        background = blend_colors(surface, accent_seed, 0.20)
        foreground_seed = blend_colors(accent_seed, QColor("#ffffff"), 0.62)
    else:
        background = background_seed

    foreground = ensure_minimum_contrast(
        foreground_seed,
        background,
        text_primary,
        minimum=4.5,
    )
    accent = ensure_minimum_contrast(
        accent_seed,
        background,
        foreground,
        minimum=3.0,
    )
    return StatusColors(foreground, background, accent)


def semantic_palette(
    palette: QPalette | None = None,
    *,
    group: QPalette.ColorGroup = QPalette.Active,
) -> SemanticPalette:
    """Resolve semantic colors from the current Qt palette.

    Status seeds preserve the dashboard's established meanings. Surface, text,
    border, focus, and dark-mode status variants follow the active native
    palette and are contrast-corrected for readable UI widgets.
    """

    source = _active_palette(palette)
    surface = source.color(group, QPalette.Base)
    if not surface.isValid():
        surface = source.color(group, QPalette.Window)

    surface_alt = source.color(group, QPalette.AlternateBase)
    text_seed = source.color(group, QPalette.Text)
    window_text = source.color(group, QPalette.WindowText)
    text_primary = ensure_minimum_contrast(
        text_seed,
        surface,
        window_text,
        minimum=4.5,
    )
    if not surface_alt.isValid() or surface_alt == surface:
        surface_alt = blend_colors(surface, text_primary, 0.05)

    placeholder_role = getattr(QPalette, "PlaceholderText", QPalette.Mid)
    secondary_seed = source.color(group, placeholder_role)
    text_secondary = ensure_minimum_contrast(
        secondary_seed,
        surface,
        text_primary,
        minimum=4.5,
    )
    border_seed = source.color(group, QPalette.Mid)
    border = ensure_minimum_contrast(
        border_seed,
        surface,
        text_primary,
        minimum=3.0,
    )
    focus_seed = source.color(group, QPalette.Highlight)
    focus = ensure_minimum_contrast(
        focus_seed,
        surface,
        text_primary,
        minimum=3.0,
    )

    status = {
        key: _status_colors(key, surface=surface, text_primary=text_primary)
        for key in (
            STATUS_NORMAL,
            STATUS_ATTENTION,
            STATUS_FAILURE,
            STATUS_UNKNOWN,
        )
    }
    return SemanticPalette(
        normal=status[STATUS_NORMAL],
        attention=status[STATUS_ATTENTION],
        failure=status[STATUS_FAILURE],
        unknown=status[STATUS_UNKNOWN],
        surface=surface,
        surface_alt=surface_alt,
        text_primary=text_primary,
        text_secondary=text_secondary,
        border=border,
        focus=focus,
    )


def status_colors(
    status: Any,
    palette: QPalette | None = None,
    *,
    group: QPalette.ColorGroup = QPalette.Active,
) -> StatusColors:
    """Return palette-aware presentation colors for a supplied status."""

    return semantic_palette(palette, group=group).for_status(status)
