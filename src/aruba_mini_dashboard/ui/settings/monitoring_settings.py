from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aruba_mini_dashboard.config import AppSettings

from ..widgets import ClickArmedComboBox, CollapsibleSection


class MonitoringSettingsPresentationMixin:
    """Construct polling, performance, and detection presentation."""

    def _build_detection_section(self, parent: QWidget) -> CollapsibleSection:
        content = QWidget(parent)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 2, 0, 4)
        form_widget = QWidget(content)
        form = QFormLayout(form_widget)
        d = self.settings.detection
        self.low_threshold = self._spin(0, 1_000_000, d.low_client_threshold)
        self.anomaly_cycles = self._spin(1, 100, d.anomaly_cycles, "회")
        self.recovery_cycles = self._spin(1, 100, d.recovery_cycles, "회")
        self.comparison_mode = ClickArmedComboBox()
        self.comparison_mode.addItem("절대값과 상대 비교 함께 사용", "absolute_and_relative")
        self.comparison_mode.addItem("절대값만 사용", "absolute_only")
        self.comparison_mode.setCurrentIndex(max(0, self.comparison_mode.findData(d.comparison_mode)))
        self.relative_ratio = self._spin(1, 100, d.relative_ratio_percent, "%")
        self.minimum_total = self._spin(0, 1_000_000, d.minimum_cluster_active_clients)
        self.minimum_peer = self._spin(0, 1_000_000, d.minimum_peer_median)
        self.missing_cycles = self._spin(1, 100, d.missing_cycles, "회")
        rows = (
            ("Low Client Threshold", self.low_threshold, "Active와 Standby가 모두 이 값 이하인지 확인합니다. 기본값 10입니다."),
            ("연속 이상 감지", self.anomaly_cycles, "Client 저하가 이 횟수만큼 연속될 때 장애를 활성화합니다. 기본값 3회입니다."),
            ("복구 확인", self.recovery_cycles, "정상 값이 이 횟수만큼 연속된 뒤 복구로 판단합니다. 기본값 2회입니다."),
            ("감지 모드", self.comparison_mode, "절대 기준만 또는 다른 장비 중앙값과의 상대 비교를 함께 사용합니다."),
            ("상대 비교 기준", self.relative_ratio, "정상 Peer 중앙값 대비 이 비율 이하인지 확인합니다. 기본값 25%입니다."),
            ("클러스터 최소 전체 Active", self.minimum_total, "전체 사용량이 낮을 때 특정 장비 장애로 오판하지 않는 하한입니다. 기본값 50입니다."),
            ("Peer 중앙값 최소", self.minimum_peer, "다른 장비가 충분한 Client를 보유했는지 확인하는 하한입니다. 기본값 30입니다."),
            ("행 누락 활성화", self.missing_cycles, "구성원 행이 이 횟수만큼 연속 누락될 때 경고합니다. 기본값 3회입니다."),
        )
        for label, widget, description in rows:
            self._add_row(form, label, widget, description)
        layout.addWidget(form_widget)
        self.detection_reset_button = QPushButton("감지 기본값 복원", content)
        self._describe(
            self.detection_reset_button,
            "감지 기본값 복원",
            "고급 감지 기준만 안전한 기본값으로 돌립니다.",
        )
        self.detection_reset_button.clicked.connect(self._reset_detection_defaults)
        layout.addWidget(self.detection_reset_button)
        return CollapsibleSection("고급 감지 기준", content, parent)

    def _reset_detection_defaults(self) -> None:
        defaults = AppSettings.default().detection
        self.low_threshold.setValue(defaults.low_client_threshold)
        self.anomaly_cycles.setValue(defaults.anomaly_cycles)
        self.recovery_cycles.setValue(defaults.recovery_cycles)
        self.comparison_mode.setCurrentIndex(
            max(0, self.comparison_mode.findData(defaults.comparison_mode))
        )
        self.relative_ratio.setValue(defaults.relative_ratio_percent)
        self.minimum_total.setValue(defaults.minimum_cluster_active_clients)
        self.minimum_peer.setValue(defaults.minimum_peer_median)
        self.missing_cycles.setValue(defaults.missing_cycles)

    def _build_polling_tab(self) -> None:
        self.polling_page = QWidget(self)
        layout = QVBoxLayout(self.polling_page)
        form_widget = QWidget(self.polling_page)
        form = QFormLayout(form_widget)
        self.poll_interval = self._spin(10, 3600, self.settings.polling.interval_seconds, "초")
        self._add_row(
            form,
            "점검 주기",
            self.poll_interval,
            "자동 점검 간격입니다. 10~3600초이며 기본값은 60초입니다.",
        )
        performance = getattr(self.settings, "performance", None)
        self.low_spec_mode = QCheckBox("저사양 모드")
        self.low_spec_mode.setChecked(bool(getattr(performance, "low_spec_mode", False)))
        self._describe(
            self.low_spec_mode,
            "저사양 모드",
            "자동 점검은 최소 120초 간격으로 실행하고 ‘지금 점검’은 즉시 실행됩니다. "
            "MM과 클러스터 수집은 최대 2개까지 병렬 실행합니다. 대용량 원본 출력은 내용에 "
            "따라 압축 여부를 판단하고, 전체 화면 장비표는 250대씩 표시하며, 운영 로그는 "
            "파일당 최대 2MB와 백업 2개로 제한합니다. 같은 명령과 감지 기준을 사용하므로 "
            "결과 정확성은 바뀌지 않습니다.",
        )
        form.addRow(self.low_spec_mode)
        self.performance_logging = QCheckBox("선택적 성능 로그")
        self.performance_logging.setChecked(
            bool(getattr(performance, "performance_logging", False))
        )
        self._describe(
            self.performance_logging,
            "선택적 성능 로그",
            "시작·수집·저장·화면 처리 시간과 개수만 별도 회전 로그에 기록합니다. "
            "IP, 사용자 ID, 자격 증명과 원본 명령 출력은 기록하지 않습니다. 기본값은 꺼짐입니다.",
        )
        form.addRow(self.performance_logging)
        self.polling_note = QLabel(
            "점검 중 예약 시각이 도래하면 해당 회차는 건너뛰며, 중복 SSH 작업을 실행하지 않습니다."
        )
        self.polling_note.setWordWrap(True)
        form.addRow(self.polling_note)
        layout.addWidget(form_widget)
        self.detection_section = self._build_detection_section(self.polling_page)
        layout.addWidget(self.detection_section)
        layout.addStretch(1)
        self.tabs.addTab(self.polling_page, "운영")
