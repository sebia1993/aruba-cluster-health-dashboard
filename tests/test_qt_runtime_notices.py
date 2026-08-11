from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import shutil
import sys

import pytest

from scripts import verify_release_package as package_verifier


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_qt_runtime_notices.py"


def _load_collector():
    spec = spec_from_file_location("collect_qt_runtime_notices", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_package(tmp_path: Path, manifest: dict) -> Path:
    package_root = tmp_path / "ArubaMiniDashboard"
    internal = package_root / "_internal"
    for index, artifact in enumerate(sorted(manifest["artifacts"])):
        path = internal / Path(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"reviewed-artifact-{index}:{artifact}".encode("ascii"))
    return package_root


def test_manifest_pins_official_qt_611_sources_and_complete_license_evidence() -> None:
    collector = _load_collector()
    manifest, raw = collector.load_manifest(ROOT)

    assert manifest["qt_version"] == "6.11.0"
    assert manifest["pyside_distribution"] == "PySide6-Essentials==6.11.0"
    assert len(manifest["components"]) == 38
    assert len(manifest["artifacts"]) == 23
    assert raw.endswith(b"\n")
    for source in manifest["source_repositories"].values():
        assert source["url"].startswith("https://code.qt.io/")
        assert source["tag"] == "v6.11.0"
        assert len(source["tag_object"]) == 40
        assert len(source["commit"]) == 40

    assert "xsvg" in manifest["artifacts"]["pyside6/qt6svg.dll"]
    assert "wintab" in manifest["artifacts"]["pyside6/plugins/platforms/qwindows.dll"]
    assert "pcre2" in manifest["artifacts"]["pyside6/qt6core.dll"]
    assert "harfbuzz-ng" in manifest["artifacts"]["pyside6/qt6gui.dll"]


def test_manifest_evidence_hash_is_independent_of_checkout_line_endings(tmp_path: Path) -> None:
    collector = _load_collector()
    projects: list[Path] = []
    source_manifest = (ROOT / "scripts" / collector.MANIFEST_NAME).read_text(encoding="utf-8")
    normalized = source_manifest.replace("\r\n", "\n").replace("\r", "\n")

    for name, newline in (("lf", "\n"), ("crlf", "\r\n")):
        project = tmp_path / name
        (project / "scripts").mkdir(parents=True)
        shutil.copytree(
            ROOT / "scripts" / "license_texts" / "qt-6.11.0",
            project / "scripts" / "license_texts" / "qt-6.11.0",
        )
        (project / "scripts" / collector.MANIFEST_NAME).write_bytes(
            normalized.replace("\n", newline).encode("utf-8")
        )
        projects.append(project)

    _lf_manifest, lf_evidence = collector.load_manifest(projects[0])
    _crlf_manifest, crlf_evidence = collector.load_manifest(projects[1])
    _lf_reviewed, lf_verifier_evidence = package_verifier._load_reviewed_manifest(
        projects[0] / "scripts" / collector.MANIFEST_NAME, label="Qt runtime test"
    )
    _crlf_reviewed, crlf_verifier_evidence = package_verifier._load_reviewed_manifest(
        projects[1] / "scripts" / collector.MANIFEST_NAME, label="Qt runtime test"
    )

    assert lf_evidence == crlf_evidence
    assert collector._sha256(lf_evidence) == collector._sha256(crlf_evidence)
    assert lf_verifier_evidence == crlf_verifier_evidence == lf_evidence


def test_generated_release_evidence_is_checked_out_with_lf() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "docs/QT_THIRD_PARTY_NOTICES.txt text eol=lf" in attributes
    assert "scripts/qt_runtime_manifest.json text eol=lf" in attributes
    assert "scripts/lgpl_runtime_manifest.json text eol=lf" in attributes


def test_pyinstaller_qt_allowlist_exactly_matches_reviewed_manifest() -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    spec_text = (ROOT / "ArubaMiniDashboard.spec").read_text(encoding="utf-8")
    tree = ast.parse(spec_text)
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "APPROVED_QT_RUNTIME_ARTIFACTS"
            for target in node.targets
        )
    )
    allowlist = ast.literal_eval(assignment.value)

    assert allowlist == set(manifest["artifacts"])
    assert '"PySide6.QtNetwork"' in spec_text
    assert '"pyside6/opengl32sw.dll"' in spec_text
    assert '"pyside6/plugins/imageformats/qsvg.dll"' in spec_text
    assert '"pyside6/plugins/platforms/qwindows.dll"' in spec_text


def test_committed_qt_notice_is_deterministic_and_has_release_boundaries() -> None:
    collector = _load_collector()
    expected = collector.build_notice(ROOT)
    collector.check_notice(ROOT / "docs" / collector.NOTICE_NAME, expected)

    for required in (
        "QT 6.11.0 EMBEDDED THIRD-PARTY NOTICES",
        "not legal advice",
        "does not select or grant a license for the application",
        "Annotated tag object:",
        "Peeled commit:",
        "pyside6/plugins/platforms/qwindows.dll: wintab",
        "pyside6/plugins/imageformats/qsvg.dll: xsvg",
        "PCRE2",
        "HarfBuzz-NG",
        "Unicode Character Database",
        "HPND-sell-variant.txt",
        "LICENSE TEXTS",
    ):
        assert required in expected


def test_exact_reviewed_qt_artifacts_generate_hash_bound_inventory(tmp_path: Path) -> None:
    collector = _load_collector()
    manifest, raw_manifest = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)

    inventory = collector.build_package_inventory(ROOT, package_root)

    assert inventory["manifest_sha256"] == collector._sha256(raw_manifest)
    assert {entry["path"] for entry in inventory["artifacts"]} == set(manifest["artifacts"])
    assert all(len(entry["sha256"]) == 64 for entry in inventory["artifacts"])
    collector.write_package_files(ROOT, package_root)
    collector.check_package_files(ROOT, package_root)

    persisted = json.loads((package_root / collector.INVENTORY_NAME).read_text(encoding="utf-8"))
    qwindows = next(
        entry
        for entry in persisted["artifacts"]
        if entry["path"] == "pyside6/plugins/platforms/qwindows.dll"
    )
    assert qwindows["embedded_components"] == ["wintab"]


def test_unreviewed_qt_plugin_fails_closed(tmp_path: Path) -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)
    unexpected = package_root / "_internal" / "PySide6" / "plugins" / "imageformats" / "qjpeg.dll"
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_bytes(b"unreviewed")

    with pytest.raises(collector.QtNoticeError, match="unreviewed=.*qjpeg"):
        collector.build_package_inventory(ROOT, package_root)


def test_release_verifier_independently_rejects_unreviewed_qt_plugin(tmp_path: Path) -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)
    collector.write_package_files(ROOT, package_root)

    package_verifier._verify_qt_runtime_inventory(package_root)
    unexpected = package_root / "_internal" / "PySide6" / "plugins" / "imageformats" / "qjpeg.dll"
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_bytes(b"unreviewed")

    with pytest.raises(SystemExit, match="Qt runtime artifact boundary mismatch"):
        package_verifier._verify_qt_runtime_inventory(package_root)


def test_release_verifier_rejects_unreviewed_pyside_binary(tmp_path: Path) -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)
    collector.write_package_files(ROOT, package_root)
    unexpected = package_root / "_internal" / "PySide6" / "unreviewed-helper.dll"
    unexpected.write_bytes(b"unreviewed")

    with pytest.raises(SystemExit, match="Qt runtime artifact boundary mismatch"):
        package_verifier._verify_qt_runtime_inventory(package_root)


def test_release_verifier_rejects_unreviewed_shiboken_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)
    shiboken_root = package_root / "_internal" / "shiboken6"
    shiboken_root.mkdir(parents=True, exist_ok=True)
    (shiboken_root / "Shiboken.pyd").write_bytes(b"reviewed")
    (shiboken_root / "shiboken6.abi3.dll").write_bytes(b"reviewed")
    (shiboken_root / "unreviewed-helper.dll").write_bytes(b"unreviewed")
    monkeypatch.setattr(package_verifier, "_verify_qt_runtime_inventory", lambda _root: None)

    with pytest.raises(SystemExit, match="shiboken6 runtime binary boundary mismatch"):
        package_verifier._verify_onedir_qt_contract(
            package_root, [path for path in package_root.rglob("*") if path.is_file()]
        )


def test_missing_required_qwindows_or_qsvg_fails_closed(tmp_path: Path) -> None:
    collector = _load_collector()
    manifest, _raw = collector.load_manifest(ROOT)
    package_root = _fake_package(tmp_path, manifest)
    (package_root / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll").unlink()

    with pytest.raises(collector.QtNoticeError, match="missing=.*qwindows"):
        collector.build_package_inventory(ROOT, package_root)


def test_tampered_offline_qt_license_text_fails_hash_check(tmp_path: Path) -> None:
    collector = _load_collector()
    project = tmp_path / "project"
    (project / "scripts" / "license_texts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / collector.MANIFEST_NAME, project / "scripts" / collector.MANIFEST_NAME)
    shutil.copytree(
        ROOT / "scripts" / "license_texts" / "qt-6.11.0",
        project / "scripts" / "license_texts" / "qt-6.11.0",
    )
    target = project / "scripts" / "license_texts" / "qt-6.11.0" / "MIT.txt"
    target.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(collector.QtNoticeError, match="license hash mismatch"):
        collector.load_manifest(project)
