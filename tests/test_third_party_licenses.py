from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import platform
import ssl
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_third_party_licenses.py"


def _load_collector():
    spec = spec_from_file_location("collect_third_party_licenses", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_dependency_closure_is_locked_and_excludes_stale_or_dev_packages() -> None:
    collector = _load_collector()
    roots = {
        collector.canonicalize_name(requirement.name)
        for requirement in collector.load_project_runtime_requirements(ROOT)
    }
    assert roots == {"netmiko", "paramiko", "pyside6-essentials", "pywin32"}

    components = collector.resolve_runtime_components(ROOT)
    names = {component.canonical_name for component in components}
    assert {
        "bcrypt",
        "cffi",
        "cryptography",
        "netmiko",
        "paramiko",
        "pynacl",
        "pyside6-essentials",
        "pywin32",
        "shiboken6",
    } <= names
    assert {"pyside6", "pyside6-addons", "pytest", "pyinstaller"}.isdisjoint(names)

    locked = collector.load_locked_versions(ROOT)
    assert all(locked[component.canonical_name] == component.version for component in components)


def test_every_runtime_component_has_verbatim_license_evidence() -> None:
    collector = _load_collector()
    components = collector.resolve_runtime_components(ROOT)
    supplemental = collector.load_supplemental_documents(ROOT)
    for component in components:
        assert component.license_declaration
        assert component.documents or supplemental.get(component.canonical_name), component.name

    assert "pyserial" in supplemental
    assert "pyside6-essentials" in supplemental
    assert "shiboken6" in supplemental


def test_notice_contains_required_frozen_runtime_evidence_and_no_project_license_choice() -> None:
    collector = _load_collector()
    notice = collector.build_notice(ROOT)
    openssl_versions = dict(collector._openssl_components())

    for required in (
        "This notice does NOT grant or select a license for Aruba Mini Dashboard itself.",
        f"CPython=={platform.python_version()}",
        (
            "OpenSSL used by the embedded CPython ssl runtime=="
            + ssl.OPENSSL_VERSION.removeprefix("OpenSSL ").split()[0]
        ),
        (
            "OpenSSL statically embedded in cryptography=="
            + openssl_versions["OpenSSL statically embedded in cryptography"]
        ),
        "pyinstaller==6.21.0",
        "pyinstaller-hooks-contrib==2026.6",
        "PySide6_Essentials==6.11.0",
        "shiboken6==6.11.0",
        "netmiko==4.6.0",
        "paramiko==4.0.0",
        "pywin32==311",
        "LICENSE TEXTS",
    ):
        assert required in notice

    inventory = notice.partition("PACKAGED LANGUAGE, CRYPTOGRAPHY, AND BOOTLOADER RUNTIMES")[0]
    assert "PySide6_Addons==" not in inventory
    assert "pytest==" not in inventory


def test_committed_notice_matches_deterministic_generator() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(ROOT), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "THIRD_PARTY_NOTICES_OK" in completed.stdout


def test_notice_check_fails_closed_for_stale_content(tmp_path: Path) -> None:
    collector = _load_collector()
    stale = tmp_path / "THIRD_PARTY_NOTICES.txt"
    stale.write_text("stale\n", encoding="utf-8")

    with pytest.raises(collector.LicenseCollectionError, match="notice is stale"):
        collector.check_notice(stale, "expected\n")


def test_supplemental_license_hash_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    collector = _load_collector()
    first = collector.SUPPLEMENTAL_LICENSES[0]
    monkeypatch.setattr(
        collector,
        "SUPPLEMENTAL_LICENSES",
        (
            collector.SupplementalLicense(
                filename=first.filename,
                expected_sha256="0" * 64,
                applies_to=first.applies_to,
                source=first.source,
            ),
        ),
    )

    with pytest.raises(collector.LicenseCollectionError, match="hash mismatch"):
        collector.load_supplemental_documents(ROOT)
