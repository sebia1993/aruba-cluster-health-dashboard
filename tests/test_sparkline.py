from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from aruba_mini_dashboard.ui.widgets.sparkline import Sparkline, SparklineWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_history_is_session_only_bounded_and_invalid_values_are_gaps() -> None:
    _app()
    sparkline = SparklineWidget(max_samples=10_000)
    spy = QSignalSpy(sparkline.samples_changed)

    for value in range(65):
        sparkline.append_sample(value)
    sparkline.append_sample(None)
    sparkline.append_sample("not-a-number")
    sparkline.append_sample(float("inf"))
    sparkline.append_sample(True)

    assert sparkline.maximum_samples == 60
    assert len(sparkline.samples) == 60
    assert sparkline.samples[-4:] == (None, None, None, None)
    assert sparkline.valid_samples[0] == 9.0
    assert spy.count() == 69
    assert "세션 추세" in sparkline.accessibleDescription()
    sparkline.close()


def test_empty_single_flat_and_gapped_samples_render_safely_offscreen() -> None:
    app = _app()
    sparkline = Sparkline([], accessible_name="Active Client 추세")
    sparkline.resize(180, 40)
    sparkline.show()

    for samples in (
        [],
        [None, "bad"],
        [4],
        [4, 4, 4],
        [1, None, 3, float("nan"), 2],
    ):
        sparkline.set_samples(samples)
        app.processEvents()
        image = sparkline.grab().toImage()
        assert not image.isNull()

    assert sparkline.accessibleName() == "Active Client 추세"
    assert sparkline.samples == (1.0, None, 3.0, None, 2.0)
    sparkline.close()


def test_broken_sample_iterable_never_escapes_into_monitoring_update() -> None:
    _app()

    class BrokenNumber:
        def __float__(self) -> float:
            raise RuntimeError("broken numeric adapter")

    def broken_samples():
        yield 2
        yield None
        raise RuntimeError("supplementary UI history failed")

    sparkline = SparklineWidget([1])
    sparkline.set_samples(broken_samples())

    assert sparkline.samples == (2.0, None)
    sparkline.append_sample(object())
    assert sparkline.samples[-1] is None
    sparkline.append_sample(BrokenNumber())
    assert sparkline.samples[-1] is None
    sparkline.set_samples([BrokenNumber(), 4])
    assert sparkline.samples == (None, 4.0)
    sparkline.close()


def test_status_and_palette_changes_refresh_contrast_safe_paint_color() -> None:
    app = _app()
    sparkline = SparklineWidget([1, 3, 2], status="failure")
    sparkline.resize(160, 40)
    sparkline.show()
    app.processEvents()
    sparkline.grab()
    light_color = QColor(sparkline.paint_color_name)
    assert light_color.isValid()

    dark = QPalette(sparkline.palette())
    dark.setColor(QPalette.Active, QPalette.Base, QColor("#1f2329"))
    dark.setColor(QPalette.Active, QPalette.Window, QColor("#1f2329"))
    dark.setColor(QPalette.Active, QPalette.Text, QColor("#f4f6f8"))
    dark.setColor(QPalette.Active, QPalette.WindowText, QColor("#f4f6f8"))
    dark.setColor(QPalette.Active, QPalette.Mid, QColor("#66717f"))
    dark.setColor(QPalette.Active, QPalette.Highlight, QColor("#7db7ff"))
    sparkline.setPalette(dark)
    QApplication.sendEvent(sparkline, QEvent(QEvent.PaletteChange))
    app.processEvents()
    sparkline.grab()

    assert QColor(sparkline.paint_color_name).isValid()
    assert sparkline.status_key == "failure"
    sparkline.set_status("unexpected-value")
    assert sparkline.status_key == "unknown"
    sparkline.close()
