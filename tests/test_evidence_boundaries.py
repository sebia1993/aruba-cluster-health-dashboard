from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_FILES = (
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / ".github" / "SECURITY.md",
    ROOT / ".github" / "workflows" / "release-windows.yml",
    ROOT / "docs" / "PROJECT_STATUS_KO.md",
    ROOT / "docs" / "README.txt",
    ROOT / "docs" / "RELEASE_PROCESS_KO.md",
    ROOT / "docs" / "VALIDATION_REPORT_KO.md",
)
FORBIDDEN_UNSUPPORTED_CLAIMS = (
    "운영자가 확인했습니다",
    "운영자가 확인했으며",
    "운영자가 확인했고",
    "운영자 확인 완료",
)


@pytest.mark.parametrize("path", EVIDENCE_FILES, ids=lambda path: path.name)
def test_public_documents_do_not_claim_unverified_live_device_validation(path: Path) -> None:
    normalized = " ".join(path.read_text(encoding="utf-8").split())

    for unsupported_claim in FORBIDDEN_UNSUPPORTED_CLAIMS:
        assert unsupported_claim not in normalized


def test_validation_report_keeps_live_device_evidence_pending() -> None:
    report = (ROOT / "docs" / "VALIDATION_REPORT_KO.md").read_text(encoding="utf-8")
    normalized = " ".join(report.split())

    assert "실제 Aruba MM / 7240XM 읽기 전용 동작 | ⚠️ 별도 현장 증거" in report
    assert "현재 완료 상태를 뜻하지 않습니다" in normalized
