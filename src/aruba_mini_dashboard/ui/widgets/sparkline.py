from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import math
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import SIZES, normalize_status_key, presentation_palette, status_colors
from ._palette_aware import PaletteAwareWidgetMixin


class SparklineWidget(PaletteAwareWidgetMixin, QWidget):
    """A tiny dependency-free, session-only trend widget.

    Samples are deliberately retained only in a bounded in-process deque. A
    missing or non-finite sample creates a visual gap instead of raising or
    being mistaken for a zero value.
    """

    samples_changed = Signal()
    MAX_SAMPLES = 60

    _STYLE_CHANGE_EVENTS = {
        QEvent.ApplicationPaletteChange,
        QEvent.PaletteChange,
        QEvent.StyleChange,
    }

    def __init__(
        self,
        samples: Iterable[Any] | None = None,
        parent: QWidget | None = None,
        *,
        max_samples: int = 60,
        status: Any = "normal",
        accessible_name: str = "최근 추세",
    ) -> None:
        super().__init__(parent)
        try:
            bounded_maximum = min(self.MAX_SAMPLES, max(1, int(max_samples)))
        except (TypeError, ValueError, OverflowError):
            bounded_maximum = 60
        self._samples: deque[float | None] = deque(maxlen=bounded_maximum)
        self._status_key = normalize_status_key(status)
        self._paint_color_name = ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(SIZES.sparkline_height)
        self.setAccessibleName(accessible_name.strip() or "최근 추세")
        self.setToolTip("현재 실행 중 수집한 최근 값의 추세입니다.")
        self.set_samples(samples)
        self._initialize_palette_awareness()

    @staticmethod
    def _coerce_sample(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            sample = float(value)
        except Exception:
            # Numeric conversion belongs only to supplementary presentation;
            # a hostile or broken value object must not escape into polling.
            return None
        return sample if math.isfinite(sample) else None

    @property
    def samples(self) -> tuple[float | None, ...]:
        return tuple(self._samples)

    @property
    def valid_samples(self) -> tuple[float, ...]:
        return tuple(sample for sample in self._samples if sample is not None)

    @property
    def maximum_samples(self) -> int:
        return self._samples.maxlen or 0

    @property
    def status_key(self) -> str:
        return self._status_key

    @property
    def paint_color_name(self) -> str:
        """Expose the palette-derived line color for deterministic GUI tests."""

        return self._paint_color_name

    def set_status(self, status: Any) -> None:
        key = normalize_status_key(status)
        if key == self._status_key:
            return
        self._status_key = key
        self._refresh_accessibility()
        self.update()

    def set_samples(self, samples: Iterable[Any] | None) -> None:
        replacement: deque[float | None] = deque(maxlen=self.maximum_samples)
        if samples is not None:
            if isinstance(samples, (str, bytes)):
                iterator = iter((samples,))
            else:
                try:
                    iterator = iter(samples)
                except TypeError:
                    iterator = iter((samples,))
            try:
                for sample in iterator:
                    replacement.append(self._coerce_sample(sample))
            except Exception:
                # A UI-only iterable must never break the monitoring update.
                # Valid values yielded before the failure remain useful.
                pass
        self._samples = replacement
        self._refresh_accessibility()
        self.samples_changed.emit()
        self.update()

    def append_sample(self, sample: Any) -> None:
        self._samples.append(self._coerce_sample(sample))
        self._refresh_accessibility()
        self.samples_changed.emit()
        self.update()

    def clear_samples(self) -> None:
        if not self._samples:
            return
        self._samples.clear()
        self._refresh_accessibility()
        self.samples_changed.emit()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(120, SIZES.sparkline_height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(48, SIZES.sparkline_height)

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in self._STYLE_CHANGE_EVENTS:
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            samples = tuple(self._samples)
            valid = tuple(sample for sample in samples if sample is not None)
            colors = status_colors(self._status_key, presentation_palette(self))
            self._paint_color_name = colors.accent.name()
            if not valid:
                return

            rect = self.contentsRect().adjusted(3, 3, -3, -3)
            if rect.width() <= 0 or rect.height() <= 0:
                return
            minimum = min(valid)
            maximum = max(valid)
            span = maximum - minimum
            denominator = max(1, len(samples) - 1)

            def point(index: int, value: float) -> QPointF:
                x = (
                    rect.center().x()
                    if len(samples) == 1
                    else rect.left() + (rect.width() * index / denominator)
                )
                if span == 0:
                    y = rect.center().y()
                else:
                    ratio = (value - minimum) / span
                    y = rect.bottom() - (rect.height() * ratio)
                return QPointF(float(x), float(y))

            pen = QPen(colors.accent, 1.5)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.setClipRect(self.contentsRect())

            path = QPainterPath()
            segment_points = 0
            drew_segment = False
            single_points: list[QPointF] = []
            for index, sample in enumerate(samples):
                if sample is None:
                    if segment_points == 1:
                        single_points.append(path.currentPosition())
                    elif segment_points > 1:
                        painter.drawPath(path)
                        drew_segment = True
                    path = QPainterPath()
                    segment_points = 0
                    continue
                current = point(index, sample)
                if segment_points == 0:
                    path.moveTo(current)
                else:
                    path.lineTo(current)
                segment_points += 1
            if segment_points == 1:
                single_points.append(path.currentPosition())
            elif segment_points > 1:
                painter.drawPath(path)
                drew_segment = True

            if single_points or (len(valid) == 1 and not drew_segment):
                painter.setBrush(colors.accent)
                for current in single_points:
                    painter.drawEllipse(current, 1.8, 1.8)
        finally:
            painter.end()

    def _refresh_accessibility(self) -> None:
        valid = self.valid_samples
        if not valid:
            description = "표시할 유효한 세션 추세 값이 없습니다."
        else:
            description = (
                f"세션 추세 {len(valid)}개, 최근 값 {valid[-1]:g}, "
                f"최솟값 {min(valid):g}, 최댓값 {max(valid):g}."
            )
        self.setAccessibleDescription(description)

    def _refresh_presentation(self) -> None:
        self.update()


Sparkline = SparklineWidget
