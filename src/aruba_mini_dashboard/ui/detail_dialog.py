from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from .view_models import DeviceView, display, flatten_errors, safe_raw_output, sequence, value
from .widgets import SubtleTabWidget


def _plain(value_: Any) -> Any:
    if is_dataclass(value_):
        return {key: _plain(item) for key, item in asdict(value_).items()}
    if isinstance(value_, Enum):
        return value_.value
    if isinstance(value_, dict):
        return {str(key): _plain(item) for key, item in value_.items()}
    if isinstance(value_, (list, tuple, set)):
        return [_plain(item) for item in value_]
    if isinstance(value_, (str, int, float, bool)) or value_ is None:
        return value_
    return str(value_)


class DetailDialog(QDialog):
    acknowledge_requested = Signal(str)

    def __init__(
        self,
        device: Any,
        parent: QWidget | None = None,
        *,
        raw_outputs: Any | None = None,
        parsed_results: Any | None = None,
        previous_device: Any | None = None,
        raw_outputs_provider: Callable[[], Any] | None = None,
        parsed_results_provider: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("장비 상세 정보")
        self.resize(680, 520)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._device = device
        self._raw_outputs = raw_outputs
        self._parsed_results = parsed_results
        self._raw_outputs_provider = raw_outputs_provider
        self._parsed_results_provider = parsed_results_provider
        self._previous_device = previous_device
        self._parsed_editor: QPlainTextEdit | None = None
        self._raw_editor: QPlainTextEdit | None = None

        root = QVBoxLayout(self)
        self.tabs = SubtleTabWidget(self)
        root.addWidget(self.tabs)
        self._build_summary_tab()
        self.tabs.addTab(QWidget(self.tabs), "파싱 결과")
        self.tabs.addTab(QWidget(self.tabs), "원본 출력")
        self.tabs.currentChanged.connect(self._materialize_tab)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def update_snapshot(
        self,
        device: Any,
        *,
        raw_outputs: Any | None = None,
        parsed_results: Any | None = None,
        previous_device: Any | None = None,
    ) -> None:
        """Replace every cycle-bound value as one coherent detail snapshot.

        A dialog may remain open across polls.  Replacing the summary, parsed
        results, and raw output together prevents an old health summary from
        being displayed beside a newer command response, while retaining at
        most the application's current raw-output mapping.
        """

        selected_tab = self.tabs.currentIndex()
        self.tabs.blockSignals(True)
        try:
            if self._parsed_editor is not None:
                self._parsed_editor.clear()
            if self._raw_editor is not None:
                self._raw_editor.clear()
            self._device = device
            self._raw_outputs = raw_outputs
            self._parsed_results = parsed_results
            self._raw_outputs_provider = None
            self._parsed_results_provider = None
            self._previous_device = previous_device
            self._parsed_editor = None
            self._raw_editor = None
            self._build_summary_tab()
            self._reset_lazy_tab(1, "파싱 결과")
            self._reset_lazy_tab(2, "원본 출력")
            self.tabs.setCurrentIndex(max(0, min(selected_tab, 2)))
        finally:
            self.tabs.blockSignals(False)
        self._materialize_tab(self.tabs.currentIndex())

    def _build_summary_tab(self) -> None:
        view = DeviceView.from_source(self._device)
        page = QWidget(self)
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("IP", self._selectable(view.ip))
        form.addRow("장비명", self._selectable(view.alias or view.hostname or "-"))
        form.addRow("상태", self._selectable(view.status))
        form.addRow("MM Status", self._selectable(view.mm_status))
        form.addRow("Active / Standby", self._selectable(f"{view.active_clients} / {view.standby_clients}"))
        previous_connection = display(value(self._device, "previous_connection_type"))
        form.addRow("Connection-Type", self._selectable(f"{previous_connection} → {view.connection_type}"))
        form.addRow("연속 이상 감지", self._selectable(display(value(self._device, "load_anomaly_streak"), "0")))
        form.addRow("마지막 확인", self._selectable(view.last_seen))
        if self._previous_device is not None:
            previous = DeviceView.from_source(self._previous_device)
            previous_text = (
                f"MM {previous.mm_status} / Active {previous.active_clients} / "
                f"Standby {previous.standby_clients} / Connection-Type {previous.connection_type}"
            )
            if previous.last_seen and previous.last_seen != "-":
                previous_text += f"\n수집 시각: {previous.last_seen}"
            form.addRow("이전 수집값", self._selectable(previous_text))
        change_time = self._connection_change_time()
        if change_time:
            form.addRow("Connection-Type 최초 변화", self._selectable(change_time))
        reasons = "\n".join(f"• {item}" for item in view.issue_reasons) or "없음"
        form.addRow("판단 근거", self._selectable(reasons))
        errors = "\n".join(f"• {item}" for item in flatten_errors(self._device)) or "없음"
        form.addRow("최근 수집 오류", self._selectable(errors))
        if self.tabs.count() == 0:
            self.tabs.addTab(page, "요약")
        else:
            previous_page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            self.tabs.insertTab(0, page, "요약")
            if previous_page is not None:
                previous_page.deleteLater()

    def _build_parsed_tab(self) -> None:
        if self._parsed_editor is not None:
            return
        parsed = self._filtered_parse_results()
        if parsed is None:
            parsed = value(self._device, "parsed_results", value(self._device, "parsed_result", None))
        if parsed is None:
            parsed = {
                "ip": value(self._device, "ip"),
                "mm_status": value(self._device, "mm_status"),
                "active_clients": value(self._device, "active_clients"),
                "standby_clients": value(self._device, "standby_clients"),
                "connection_type": value(self._device, "connection_type"),
                "previous_values": value(self._device, "previous_values", {}),
                "signals": sequence(self._device, "signals"),
                "observations": sequence(self._device, "observations"),
                "previous_values": _plain(self._previous_device),
            }
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setPlainText(json.dumps(_plain(parsed), ensure_ascii=False, indent=2))
        self._parsed_editor = editor
        self._replace_placeholder(1, editor, "파싱 결과")

    def _build_raw_tab(self) -> None:
        if self._raw_editor is not None:
            return
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setPlaceholderText("현재 실행의 원본 명령 출력이 메모리에 남아 있지 않습니다.")
        raw_outputs = (
            self._raw_outputs_provider()
            if self._raw_outputs_provider is not None
            else self._raw_outputs
        )
        raw_source = self._device if raw_outputs is None else {"raw_outputs": raw_outputs}
        editor.setPlainText(safe_raw_output(raw_source))
        self._raw_editor = editor
        self._replace_placeholder(2, editor, "원본 출력")

    @Slot(int)
    def _materialize_tab(self, index: int) -> None:
        if index == 1:
            self._build_parsed_tab()
        elif index == 2:
            self._build_raw_tab()

    def _replace_placeholder(self, index: int, widget: QWidget, title: str) -> None:
        placeholder = self.tabs.widget(index)
        self.tabs.removeTab(index)
        self.tabs.insertTab(index, widget, title)
        self.tabs.setCurrentIndex(index)
        if placeholder is not None:
            placeholder.deleteLater()

    def _reset_lazy_tab(self, index: int, title: str) -> None:
        previous_page = self.tabs.widget(index)
        self.tabs.removeTab(index)
        self.tabs.insertTab(index, QWidget(self.tabs), title)
        if previous_page is not None:
            previous_page.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        # QPlainTextDocument keeps its own large character buffer. Clearing it
        # before dropping the snapshot references makes closure deterministic
        # during long-running monitoring sessions.
        if self._parsed_editor is not None:
            self._parsed_editor.clear()
        if self._raw_editor is not None:
            self._raw_editor.clear()
        self._device = None
        self._raw_outputs = None
        self._parsed_results = None
        self._raw_outputs_provider = None
        self._parsed_results_provider = None
        self._previous_device = None
        super().closeEvent(event)

    def _filtered_parse_results(self) -> dict[str, Any] | None:
        parsed_results = (
            self._parsed_results_provider()
            if self._parsed_results_provider is not None
            else self._parsed_results
        )
        if not isinstance(parsed_results, dict):
            return None
        ip = display(value(self._device, "ip", ""), "")
        results: dict[str, Any] = {}
        for command, parsed in parsed_results.items():
            if parsed is None:
                results[str(command)] = {"status": "수집 결과 없음", "rows": [], "issues": []}
                continue
            rows = [row for row in sequence(parsed, "rows") if display(value(row, "ip", ""), "") == ip]
            results[str(command)] = {
                "status": display(value(parsed, "status", "unknown")),
                "rows": _plain(rows),
                "issues": _plain(sequence(parsed, "issues")),
                "header_map": _plain(value(parsed, "header_map", {})),
                "metadata": _plain(value(parsed, "metadata", {})),
            }
        return results

    def _connection_change_time(self) -> str:
        for signal in sequence(self._device, "signals"):
            incident_type = display(value(signal, "incident_type", ""), "")
            if incident_type != "connection_type_changed":
                continue
            details = value(signal, "details", {})
            detected = value(details, "first_detected_at", "")
            if detected:
                return display(detected, "")
        return ""

    @staticmethod
    def _selectable(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        label.setWordWrap(True)
        return label
