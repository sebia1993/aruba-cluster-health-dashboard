from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.ui.theme import (
    RADIUS,
    SPACING,
    contrast_ratio,
    normalize_status_key,
    semantic_palette,
    status_colors,
)
from aruba_mini_dashboard.ui.widgets import (
    EmptyState,
    EventList,
    MetricCard,
    RecentEvent,
    StatusBadge,
    StatusCard,
    SubtleTabWidget,
    bounded_window_geometry,
)
from aruba_mini_dashboard.ui.widgets.legacy import (
    SubtleTabWidget as LegacySubtleTabWidget,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_theme_preserves_status_aliases_and_contrast_in_light_and_dark_palettes() -> None:
    _app()
    assert normalize_status_key("healthy") == "normal"
    assert normalize_status_key("degraded") == "attention"
    assert normalize_status_key("critical") == "failure"
    assert normalize_status_key("unrecognized") == "unknown"
    assert SPACING.md > SPACING.xs
    assert RADIUS.md >= RADIUS.sm

    for surface, text in (("#ffffff", "#20242a"), ("#1f2329", "#f4f6f8")):
        palette = QPalette()
        palette.setColor(QPalette.Active, QPalette.Base, QColor(surface))
        palette.setColor(QPalette.Active, QPalette.Window, QColor(surface))
        palette.setColor(QPalette.Active, QPalette.Text, QColor(text))
        palette.setColor(QPalette.Active, QPalette.WindowText, QColor(text))
        palette.setColor(QPalette.Active, QPalette.Mid, QColor("#707780"))
        palette.setColor(QPalette.Active, QPalette.Highlight, QColor("#3584d4"))
        semantic = semantic_palette(palette)
        assert contrast_ratio(semantic.text_primary, semantic.surface) >= 4.5
        assert contrast_ratio(semantic.text_secondary, semantic.surface) >= 4.5
        assert contrast_ratio(semantic.border, semantic.surface) >= 3.0
        assert contrast_ratio(semantic.focus, semantic.surface) >= 3.0
        for key in ("normal", "attention", "failure", "unknown"):
            colors = status_colors(key, palette)
            assert contrast_ratio(colors.foreground, colors.background) >= 4.5
            assert contrast_ratio(colors.accent, colors.background) >= 3.0


def test_legacy_widget_imports_remain_source_compatible_after_package_split() -> None:
    _app()
    assert SubtleTabWidget is LegacySubtleTabWidget
    assert callable(bounded_window_geometry)


def test_status_badge_and_card_never_depend_on_color_alone() -> None:
    _app()
    badge = StatusBadge("critical", accessible_name="전체 상태")
    assert badge.status_key == "failure"
    assert badge.status_text == "장애"
    assert not badge.icon_label.pixmap().isNull()
    assert "전체 상태" in badge.accessibleName()
    assert "아이콘과 텍스트" in badge.accessibleDescription()
    assert set(badge.semantic_colors) == {"foreground", "background", "accent"}

    visual_cues: set[tuple[str, int]] = set()
    for status, label in (
        ("normal", "정상"),
        ("attention", "주의"),
        ("failure", "장애"),
        ("unknown", "확인 불가"),
    ):
        badge.set_status(status)
        pixmap = badge.icon_label.pixmap()
        assert not pixmap.isNull()
        assert badge.status_text == label
        assert label in badge.accessibleName()
        visual_cues.add((badge.status_text, pixmap.cacheKey()))
    assert len(visual_cues) == 4

    card = StatusCard("전체 상태", "warning", "Controller 1대를 확인하세요.")
    assert card.status_key == "attention"
    assert card.badge.status_text == "주의"
    assert "Controller 1대" in card.accessibleDescription()
    card.set_status("healthy")
    assert card.status_key == "normal"
    badge.close()
    card.close()


def test_metric_card_uses_bounded_sparkline_and_accessible_value_text() -> None:
    _app()
    card = MetricCard(
        "전체 Active Client",
        42,
        "등록 Controller 합계",
        samples=range(75),
        show_sparkline=True,
    )
    assert card.value == "42"
    assert card.sparkline.isVisibleTo(card)
    assert len(card.sparkline.samples) == 60
    assert "현재 값 42" in card.accessibleDescription()

    card.append_sample(None)
    assert card.sparkline.samples[-1] is None
    card.set_value(None)
    assert card.value == "-"
    card.close()


def test_recent_event_list_is_bounded_and_has_a_real_empty_state() -> None:
    _app()
    empty = EventList([])
    assert empty.empty_state.isVisibleTo(empty)
    assert empty.events == ()
    assert empty.accessibleDescription() == "최근 이벤트 0개"

    events = [
        RecentEvent(f"23:{index:02d}", f"테스트 이벤트 {index}", "warning")
        for index in range(12)
    ]
    event_list = EventList(events, maximum_events=10_000)
    assert event_list.maximum_events == 10
    assert len(event_list.events) == 10
    assert len(event_list.event_rows) == 10
    assert event_list.events[0].summary == "테스트 이벤트 0"
    assert "테스트 이벤트 0" in event_list.event_rows[0].accessibleName()

    event_list.prepend_event(
        {"time": "23:59", "message": "새 이벤트", "severity": "critical"}
    )
    assert len(event_list.events) == 10
    assert event_list.events[0].summary == "새 이벤트"
    assert event_list.events[0].status == "failure"
    empty.close()
    event_list.close()


def test_empty_state_exposes_meaningful_text_to_assistive_technology() -> None:
    _app()
    state = EmptyState("검색 결과 없음", "검색어나 필터를 변경하세요.")
    assert state.accessibleName() == "검색 결과 없음"
    assert state.accessibleDescription() == "검색어나 필터를 변경하세요."
    assert not state.icon_label.pixmap().isNull()
    state.set_content("표시할 장비 없음", "등록 상태를 확인하세요.")
    assert state.accessibleName() == "표시할 장비 없음"
    state.close()
