from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Callable

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from .developer_inspector import DeveloperInspectorController, UiElementMetadata
from .view_models import (
    DeviceView,
    display,
    flatten_errors,
    iter_safe_raw_output_chunks,
    sequence,
    value,
)
from .widgets import SubtleTabWidget, fit_window_to_available_screen


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

    _SUMMARY_DEVELOPER_FIELDS = {
        "IP": (
            "장비 IP 행",
            "DETAIL-SUMMARY-IP",
            "선택한 장비의 관리 주소 표시 영역입니다.",
        ),
        "ALIAS": (
            "장비명 행",
            "DETAIL-SUMMARY-ALIAS",
            "선택한 장비의 별칭 또는 호스트 이름 표시 영역입니다.",
        ),
        "STATUS": (
            "종합 상태 행",
            "DETAIL-SUMMARY-STATUS",
            "선택한 장비의 종합 상태 표시 영역입니다.",
        ),
        "MM-STATUS": (
            "MM Status 행",
            "DETAIL-SUMMARY-MM-STATUS",
            "Mobility Master가 보고한 장비 상태 표시 영역입니다.",
        ),
        "CLIENTS": (
            "Active 및 Standby 행",
            "DETAIL-SUMMARY-CLIENTS",
            "Active와 Standby Client 수 표시 영역입니다.",
        ),
        "CONNECTION-TYPE": (
            "Connection-Type 행",
            "DETAIL-SUMMARY-CONNECTION-TYPE",
            "현재와 이전 Connection-Type 표시 영역입니다.",
        ),
        "ANOMALY-STREAK": (
            "연속 이상 감지 행",
            "DETAIL-SUMMARY-ANOMALY-STREAK",
            "연속으로 관찰된 이상 횟수 표시 영역입니다.",
        ),
        "LAST-CHECK": (
            "마지막 확인 행",
            "DETAIL-SUMMARY-LAST-CHECK",
            "장비 상태를 마지막으로 확인한 시각 표시 영역입니다.",
        ),
        "PREVIOUS": (
            "이전 수집값 행",
            "DETAIL-SUMMARY-PREVIOUS",
            "직전 점검에서 수집한 비교값 표시 영역입니다.",
        ),
        "CONNECTION-CHANGE-TIME": (
            "Connection-Type 최초 변화 행",
            "DETAIL-SUMMARY-CONNECTION-CHANGE-TIME",
            "Connection-Type 변화가 최초 감지된 시각 표시 영역입니다.",
        ),
        "REASONS": (
            "판단 근거 행",
            "DETAIL-SUMMARY-REASONS",
            "장비 상태 판단 근거 표시 영역입니다.",
        ),
        "ERRORS": (
            "최근 수집 오류 행",
            "DETAIL-SUMMARY-ERRORS",
            "최근 수집 단계의 오류 표시 영역입니다.",
        ),
    }

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
        developer_inspector: DeveloperInspectorController | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("장비 상세 정보")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._device = device
        self._raw_outputs = raw_outputs
        self._parsed_results = parsed_results
        self._raw_outputs_provider = raw_outputs_provider
        self._parsed_results_provider = parsed_results_provider
        self._previous_device = previous_device
        self.developer_inspector = developer_inspector
        self._developer_catalog_actions: list[QAction] = []
        self._summary_fields: dict[str, QLabel] = {}
        self._parsed_editor: QPlainTextEdit | None = None
        self._raw_editor: QPlainTextEdit | None = None

        self.root_layout = QVBoxLayout(self)
        self.tabs = SubtleTabWidget(self)
        self.root_layout.addWidget(self.tabs)
        self._build_summary_tab()
        self._parsed_placeholder = QWidget(self.tabs)
        self._raw_placeholder = QWidget(self.tabs)
        self.tabs.addTab(self._parsed_placeholder, "파싱 결과")
        self.tabs.addTab(self._raw_placeholder, "원본 출력")
        self.tabs.currentChanged.connect(self._materialize_tab)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        self.close_button = self.buttons.button(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        self.root_layout.addWidget(self.buttons)
        self._register_developer_inspector()
        fit_window_to_available_screen(
            self,
            QSize(680, 520),
            minimum_size=QSize(350, 290),
            center_on_parent=True,
        )

    @staticmethod
    def _developer_metadata(
        name: str,
        stable_id: str,
        screen_path: str,
        purpose: str,
    ) -> UiElementMetadata:
        return UiElementMetadata(
            name,
            stable_id,
            screen_path,
            "src/aruba_mini_dashboard/ui/detail_dialog.py",
            purpose,
        )

    @classmethod
    def _summary_metadata(cls, field_id: str) -> UiElementMetadata:
        name, stable_id, purpose = cls._SUMMARY_DEVELOPER_FIELDS[field_id]
        return cls._developer_metadata(
            name,
            stable_id,
            "상세 정보 > 요약",
            purpose,
        )

    @classmethod
    def _tab_metadata(cls, index: int) -> UiElementMetadata:
        definitions = {
            0: (
                "요약 탭",
                "DETAIL-TAB-SUMMARY",
                "상세 정보 > 요약",
                "장비의 현재 상태와 판단 근거를 표시합니다.",
            ),
            1: (
                "파싱 결과 탭",
                "DETAIL-TAB-PARSED",
                "상세 정보 > 파싱 결과",
                "수집 결과를 구조화한 진단 정보를 표시합니다.",
            ),
            2: (
                "원본 출력 탭",
                "DETAIL-TAB-RAW",
                "상세 정보 > 원본 출력",
                "현재 실행에 남아 있는 읽기 전용 명령 출력을 표시합니다.",
            ),
        }
        return cls._developer_metadata(*definitions[index])

    @classmethod
    def _output_metadata(cls, index: int) -> UiElementMetadata:
        definitions = {
            1: (
                "파싱 결과 내용",
                "DETAIL-PARSED-OUTPUT",
                "상세 정보 > 파싱 결과",
                "선택한 장비와 관련된 구조화된 수집 결과 표시 영역입니다.",
            ),
            2: (
                "원본 출력 내용",
                "DETAIL-RAW-OUTPUT",
                "상세 정보 > 원본 출력",
                "현재 실행의 읽기 전용 명령 출력 표시 영역입니다.",
            ),
        }
        return cls._developer_metadata(*definitions[index])

    def _register_developer_inspector(self) -> None:
        inspector = self.developer_inspector
        if inspector is None:
            return

        def register_virtual(metadata: UiElementMetadata) -> None:
            action = QAction(metadata.name_ko, self)
            self._developer_catalog_actions.append(action)
            inspector.register_action(action, metadata)

        inspector.attach_host_layout(self, self.root_layout)
        inspector.register_widget(
            self,
            self._developer_metadata(
                "장비 상세 정보 창",
                "DETAIL-DIALOG",
                "상세 정보",
                "선택한 장비의 요약, 파싱 결과와 원본 출력을 제공합니다.",
            ),
        )
        inspector.register_widget(
            self.tabs.tabBar(),
            self._developer_metadata(
                "상세 정보 탭",
                "DETAIL-TABS",
                "상세 정보",
                "요약, 파싱 결과와 원본 출력 화면을 전환합니다.",
            ),
        )
        for index in range(3):
            register_virtual(self._tab_metadata(index))
        inspector.register_widget(self._parsed_placeholder, self._tab_metadata(1))
        inspector.register_widget(self._raw_placeholder, self._tab_metadata(2))
        register_virtual(
            self._developer_metadata(
                "장비 요약 정보 영역",
                "DETAIL-SUMMARY",
                "상세 정보 > 요약",
                "장비의 현재 상태, 이전 값, 판단 근거와 오류를 묶어 표시합니다.",
            )
        )
        for field_id in self._SUMMARY_DEVELOPER_FIELDS:
            register_virtual(self._summary_metadata(field_id))
        register_virtual(self._output_metadata(1))
        register_virtual(self._output_metadata(2))
        inspector.register_widget(
            self.close_button,
            self._developer_metadata(
                "상세 정보 닫기 버튼",
                "DETAIL-CLOSE",
                "상세 정보 > 하단 작업",
                "장비 상세 정보 창을 닫습니다.",
            ),
        )

    def _register_summary_widgets(self) -> None:
        inspector = self.developer_inspector
        if inspector is None:
            return
        for field_id, widget in self._summary_fields.items():
            inspector.register_widget(widget, self._summary_metadata(field_id))

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
        self._summary_fields = {}

        def add_row(label: str, field_id: str, text: str) -> None:
            field = self._selectable(text)
            self._summary_fields[field_id] = field
            form.addRow(label, field)

        add_row("IP", "IP", view.ip)
        add_row("장비명", "ALIAS", view.alias or view.hostname or "-")
        add_row("상태", "STATUS", view.status)
        add_row("MM Status", "MM-STATUS", view.mm_status)
        add_row(
            "Active / Standby",
            "CLIENTS",
            f"{view.active_clients} / {view.standby_clients}",
        )
        previous_connection = display(value(self._device, "previous_connection_type"))
        add_row(
            "Connection-Type",
            "CONNECTION-TYPE",
            f"{previous_connection} → {view.connection_type}",
        )
        add_row(
            "연속 이상 감지",
            "ANOMALY-STREAK",
            display(value(self._device, "load_anomaly_streak"), "0"),
        )
        add_row("마지막 확인", "LAST-CHECK", view.last_seen)
        if self._previous_device is not None:
            previous = DeviceView.from_source(self._previous_device)
            previous_text = (
                f"MM {previous.mm_status} / Active {previous.active_clients} / "
                f"Standby {previous.standby_clients} / Connection-Type {previous.connection_type}"
            )
            if previous.last_seen and previous.last_seen != "-":
                previous_text += f"\n수집 시각: {previous.last_seen}"
            add_row("이전 수집값", "PREVIOUS", previous_text)
        change_time = self._connection_change_time()
        if change_time:
            add_row("Connection-Type 최초 변화", "CONNECTION-CHANGE-TIME", change_time)
        reasons = "\n".join(f"• {item}" for item in view.issue_reasons) or "없음"
        add_row("판단 근거", "REASONS", reasons)
        errors = "\n".join(f"• {item}" for item in flatten_errors(self._device)) or "없음"
        add_row("최근 수집 오류", "ERRORS", errors)
        self._summary_page = page
        if self.tabs.count() == 0:
            self.tabs.addTab(page, "요약")
        else:
            previous_page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            self.tabs.insertTab(0, page, "요약")
            if previous_page is not None:
                previous_page.deleteLater()
        if self.developer_inspector is not None:
            self.developer_inspector.register_widget(page, self._tab_metadata(0))
        self._register_summary_widgets()

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
                "previous_device": _plain(self._previous_device),
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
        editor.setUndoRedoEnabled(False)
        editor.setPlaceholderText("현재 실행의 원본 명령 출력이 메모리에 남아 있지 않습니다.")
        raw_outputs = (
            self._raw_outputs_provider()
            if self._raw_outputs_provider is not None
            else self._raw_outputs
        )
        raw_source = self._device if raw_outputs is None else {"raw_outputs": raw_outputs}
        cursor = editor.textCursor()
        cursor.beginEditBlock()
        try:
            for chunk in iter_safe_raw_output_chunks(raw_source):
                cursor.insertText(chunk)
        finally:
            cursor.endEditBlock()
        cursor.movePosition(QTextCursor.Start)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()
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
        if index == 1:
            self._parsed_placeholder = None
        elif index == 2:
            self._raw_placeholder = None
        if self.developer_inspector is not None:
            self.developer_inspector.register_widget(
                widget,
                self._output_metadata(index),
            )
        if placeholder is not None:
            placeholder.deleteLater()

    def _reset_lazy_tab(self, index: int, title: str) -> None:
        previous_page = self.tabs.widget(index)
        self.tabs.removeTab(index)
        placeholder = QWidget(self.tabs)
        self.tabs.insertTab(index, placeholder, title)
        if index == 1:
            self._parsed_placeholder = placeholder
        elif index == 2:
            self._raw_placeholder = placeholder
        if self.developer_inspector is not None:
            self.developer_inspector.register_widget(
                placeholder,
                self._tab_metadata(index),
            )
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
