from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)


class RemediationPanel(QFrame):
    enabled_changed = Signal(bool)
    report_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("automaticRemediationPanel")
        self.setStyleSheet(
            "QFrame#automaticRemediationPanel { background:#F6F8FB; border-bottom:1px solid #D9E2EC; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        self.toggle = QCheckBox("자동 장애조치", self)
        self.toggle.setAccessibleName("자동 장애조치 켜기 또는 끄기")
        self.toggle.setToolTip(
            "MM Down Controller에 reload force를 실행하고 복구 후 현재 Leader에서 Cluster 재분배를 수행합니다."
        )
        layout.addWidget(self.toggle)
        self.status = QLabel("", self)
        self.status.setStyleSheet("color:#52606D;")
        self.status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.status, 1)
        self.report_button = QPushButton("최근 조치 보고서", self)
        self.report_button.setToolTip("가장 최근에 생성된 HTML 장애조치 보고서를 엽니다.")
        layout.addWidget(self.report_button)
        self.toggle.toggled.connect(self.enabled_changed)
        self.report_button.clicked.connect(self.report_requested)

    def set_checked(self, checked: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(bool(checked))
        self.toggle.blockSignals(False)

    def set_status(self, message: str) -> None:
        self.status.setText(str(message))
        self.status.setToolTip(str(message))

    def set_feature_enabled(self, enabled: bool) -> None:
        self.toggle.setEnabled(bool(enabled))

    def set_report_enabled(self, enabled: bool) -> None:
        self.report_button.setEnabled(bool(enabled))
