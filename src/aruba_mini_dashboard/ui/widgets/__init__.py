"""Reusable Qt widgets and compatibility exports for the original UI helpers."""

from .empty_state import EmptyState
from .event_list import EventList, RecentEvent, RecentEventList
from .legacy import (
    CLICK_TO_ENABLE_WHEEL_TOOLTIP,
    ClickArmedComboBox,
    ClickArmedSpinBox,
    CollapsibleSection,
    NoWheelSlider,
    SubtleSelectionTableWidget,
    SubtleTabWidget,
    _blend_colors,
    _contrast_ratio,
    _ensure_minimum_contrast,
    available_screen_geometry,
    bounded_window_geometry,
    fit_window_to_available_screen,
)
from .metric_card import MetricCard
from .sparkline import Sparkline, SparklineWidget
from .status_badge import StatusBadge
from .status_card import StatusCard

__all__ = [
    "CLICK_TO_ENABLE_WHEEL_TOOLTIP",
    "ClickArmedComboBox",
    "ClickArmedSpinBox",
    "CollapsibleSection",
    "EmptyState",
    "EventList",
    "MetricCard",
    "NoWheelSlider",
    "RecentEvent",
    "RecentEventList",
    "Sparkline",
    "SparklineWidget",
    "StatusBadge",
    "StatusCard",
    "SubtleSelectionTableWidget",
    "SubtleTabWidget",
    "available_screen_geometry",
    "bounded_window_geometry",
    "fit_window_to_available_screen",
]
