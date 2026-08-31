from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel

from aruba_mini_dashboard.ui.pages import OverviewPage
from aruba_mini_dashboard.ui.theme import contrast_ratio, semantic_palette
from aruba_mini_dashboard.ui.view_models import DashboardView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _view(active: object = 12) -> DashboardView:
    return DashboardView.from_source(
        {
            "severity": "critical",
            "checked_at": "2026-09-01T12:30:00+09:00",
            "reasons": ["테스트 장애"],
            "devices": [
                {
                    "ip": "192.0.2.11",
                    "alias": "WLC-01",
                    "controller_state": "up",
                    "active_clients": active,
                    "standby_clients": 4,
                    "connection_type": "CONNECTED",
                    "severity": "normal",
                },
                {
                    "ip": "192.0.2.12",
                    "alias": "WLC-02",
                    "controller_state": "down",
                    "active_clients": None,
                    "standby_clients": None,
                    "connection_type": None,
                    "severity": "critical",
                },
            ],
        }
    )


def _palette(*, dark: bool) -> QPalette:
    palette = QPalette()
    surface = QColor("#1f2329" if dark else "#ffffff")
    surface_alt = QColor("#282d34" if dark else "#f3f5f7")
    text = QColor("#f4f6f8" if dark else "#20242a")
    secondary = QColor("#b7c0ca" if dark else "#59636e")
    border = QColor("#66717f" if dark else "#707780")
    focus = QColor("#7db7ff" if dark else "#266fba")
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        palette.setColor(group, QPalette.Base, surface)
        palette.setColor(group, QPalette.Window, surface)
        palette.setColor(group, QPalette.AlternateBase, surface_alt)
        palette.setColor(group, QPalette.Text, text)
        palette.setColor(group, QPalette.WindowText, text)
        palette.setColor(group, QPalette.PlaceholderText, secondary)
        palette.setColor(group, QPalette.Mid, border)
        palette.setColor(group, QPalette.Highlight, focus)
    return palette


def test_overview_kpis_keep_acknowledged_active_incidents_active() -> None:
    _app()
    page = OverviewPage()
    view = _view()

    page.update_dashboard(
        view,
        view.devices,
        [
            {"active": True, "acknowledged": True, "ip": "192.0.2.11"},
            {"active": True, "acknowledged": False, "incident_type": "collection_failure"},
            {"active": False, "acknowledged": False, "ip": "192.0.2.12"},
        ],
    )

    assert page.overall_card.badge.status_text == "장애"
    assert page.controller_card.value == "1 / 2"
    assert page.active_client_card.value == "12"
    assert "1/2대 확인" in page.active_client_card.subtitle_label.text()
    assert page.incident_card.value == "2"
    assert len(page.controller_cards) == 2
    assert page.controller_cards[1].badge.status_text == "장애"


def test_overview_history_is_session_only_bounded_and_invalid_values_are_safe() -> None:
    _app()
    page = OverviewPage()
    for index in range(65):
        view = _view(active=index)
        page.update_dashboard(view, view.devices[:1], [])

    assert len(page.active_client_history) == 60
    assert page.active_client_history[0] == 5
    assert page.active_client_history[-1] == 64

    invalid = _view(active="invalid")
    page.update_dashboard(invalid, invalid.devices, [])
    assert page.active_client_card.value == "-"
    assert page.active_client_history[-1] is None


def test_overview_controller_cards_are_bounded_for_large_inventories() -> None:
    _app()
    page = OverviewPage()
    view = _view()
    devices = [
        type(view.devices[0]).from_source(
            {
                "ip": f"2001:db8::{index + 1:x}",
                "alias": f"TEST-WLC-{index:02d}",
                "controller_state": "up",
                "distribution_state": "normal",
                "active_clients": index,
                "standby_clients": index + 1,
                "connection_type": "MEMBER",
                "severity": "normal",
            }
        )
        for index in range(20)
    ]

    page.update_dashboard(view, devices, [])

    assert len(page.controller_cards) == page.MAX_CONTROLLER_CARDS
    assert page.controller_title.text() == "등록 Controller 20대"
    assert "그 외 12대" in page.controller_overflow_label.text()


def test_recent_events_are_bounded_and_storage_staleness_is_supplementary() -> None:
    _app()
    page = OverviewPage()
    page.set_recent_events(
        [
            {
                "occurred_at": f"2026-09-01T12:{index:02d}:00+09:00",
                "summary": f"이벤트 {index}",
                "status": "attention",
            }
            for index in range(8)
        ],
        stale=True,
    )

    assert len(page.recent_events.events) == 5
    assert page.recent_events.maximum_events <= 10
    assert "불러오기 지연" in page.recent_events.title_label.text()
    assert (
        "모니터링 상태에는 영향이 없습니다"
        in page.recent_events.accessibleDescription()
    )


def test_overview_statuses_have_text_icons_and_accessible_operational_context() -> None:
    _app()
    page = OverviewPage()
    view = _view()
    page.update_dashboard(view, view.devices, [{"active": True}])
    page.set_recent_events(
        [
            {
                "occurred_at": "2026-09-01T12:30:00+09:00",
                "summary": "테스트 장애 이벤트",
                "status": "failure",
            }
        ]
    )

    assert page.accessibleName() == "운영 상태 개요"
    assert "Incident" in page.accessibleDescription()
    for card in (
        page.overall_card,
        page.controller_card,
        page.active_client_card,
        page.incident_card,
        *page.controller_cards,
    ):
        assert card.accessibleName().strip()
        assert card.accessibleDescription().strip()

    status_cards = (page.overall_card, *page.controller_cards)
    for card in status_cards:
        assert card.badge.status_text.strip()
        assert not card.badge.icon_label.pixmap().isNull()
        assert card.badge.status_text in card.badge.accessibleName()
        assert "아이콘과 텍스트" in card.badge.accessibleDescription()

    assert len(page.recent_events.event_rows) == 1
    event_row = page.recent_events.event_rows[0]
    assert "테스트 장애 이벤트" in event_row.accessibleName()
    icon_labels = [
        label
        for label in event_row.findChildren(QLabel)
        if not label.pixmap().isNull()
    ]
    assert icon_labels
    assert all(label.accessibleName().strip() for label in icon_labels)
    page.close()


def test_overview_widgets_recompute_contrast_when_page_palette_changes() -> None:
    app = _app()
    original_palette = QPalette(app.palette())
    page: OverviewPage | None = None
    try:
        app.setPalette(_palette(dark=False))
        page = OverviewPage()
        view = _view()
        page.update_dashboard(view, view.devices, [])
        page.resize(1000, 600)
        page.show()

        backgrounds: list[str] = []
        for dark in (False, True):
            palette = _palette(dark=dark)
            app.setPalette(palette)
            app.processEvents()
            semantic = semantic_palette(palette)
            assert contrast_ratio(semantic.text_primary, semantic.surface) >= 4.5
            assert contrast_ratio(semantic.text_secondary, semantic.surface) >= 4.5

            for card in (page.overall_card, *page.controller_cards):
                colors = {
                    name: QColor(value)
                    for name, value in card.badge.semantic_colors.items()
                }
                assert contrast_ratio(colors["foreground"], colors["background"]) >= 4.5
                assert contrast_ratio(colors["accent"], colors["background"]) >= 3.0
                assert not card.badge.icon_label.pixmap().isNull()
                assert card.badge.status_text.strip()

            assert (
                semantic.text_primary.name()
                in page.active_client_card.value_label.styleSheet()
            )
            assert (
                semantic.text_secondary.name()
                in page.active_client_card.title_label.styleSheet()
            )
            page.active_client_card.sparkline.grab()
            assert QColor(page.active_client_card.sparkline.paint_color_name).isValid()
            backgrounds.append(page.overall_card.badge.semantic_colors["background"])

        assert backgrounds[0] != backgrounds[1]
    finally:
        if page is not None:
            page.close()
        app.setPalette(original_palette)
        app.processEvents()
