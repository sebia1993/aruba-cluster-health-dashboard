from __future__ import annotations

import re
from pathlib import Path

from paramiko.rsakey import RSAKey
from paramiko.transport import Transport


ROOT = Path(__file__).parents[1]


def test_qt_runtime_uses_essentials_without_addons_meta_package() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
    lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").casefold()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()

    assert "pyside6-essentials==6.11.0" in requirements
    assert "pyside6-essentials==6.11.0" in lock
    assert "pyside6-essentials==6.11.0" in project
    assert "pyside6-addons" not in requirements
    assert "pyside6-addons" not in lock
    assert re.search(r"(?m)^pyside6==", lock) is None


def test_reviewed_security_dependency_versions_are_consistent() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
    lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").casefold()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()

    for pinned in ("paramiko==5.0.0", "pytest==9.1.1"):
        assert pinned in requirements
        assert pinned in lock
        assert pinned in project
    # A newly-created CPython 3.13.15 venv may start with an older bundled pip.
    # Keep the release bootstrap itself hash-pinned and security-updated.
    assert "pip==26.2.1" in requirements
    assert "pip==26.2.1" in lock


def test_source_build_backend_uses_reviewed_patched_versions() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()

    assert 'requires = ["setuptools==84.0.0", "wheel==0.46.2"]' in project
    assert "setuptools==75.8.2" not in project
    assert "wheel==0.45.1" not in project


def test_paramiko_rejects_sha1_rsa_signatures_but_keeps_rsa_key_material_support() -> None:
    assert "ssh-rsa" not in RSAKey.HASHES
    assert "ssh-rsa-cert-v01@openssh.com" not in RSAKey.HASHES
    assert "ssh-rsa" not in Transport._preferred_keys
    assert "ssh-rsa" not in Transport._preferred_pubkeys
    # The string remains a public-key material identifier, so an RSA host key
    # stored in the app-owned known_hosts file can still be parsed and matched.
    assert "ssh-rsa" in RSAKey.identifiers()


def test_pyinstaller_spec_explicitly_excludes_disallowed_qt_modules() -> None:
    spec = (ROOT / "ArubaMiniDashboard.spec").read_text(encoding="utf-8").casefold()

    for artifact in (
        "qt6virtualkeyboard.dll",
        "qtvirtualkeyboardplugin.dll",
        "qt6pdf.dll",
        "qpdf.dll",
    ):
        assert artifact in spec
