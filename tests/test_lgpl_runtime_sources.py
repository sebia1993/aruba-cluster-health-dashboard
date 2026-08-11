from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil

import pytest

from scripts import collect_lgpl_runtime_sources as collector
from scripts import verify_release_package as verifier


ROOT = Path(__file__).resolve().parents[1]


def _fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest, _raw = collector.load_manifest(ROOT)
    installed = collector.validate_installed_components(manifest)
    package_root = tmp_path / "ArubaMiniDashboard"
    for component in manifest["components"]:
        name = component["distribution"]
        for relative, source in installed[name]["sources"].items():
            destination = package_root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    executable = package_root / "ArubaMiniDashboard.exe"
    executable.write_bytes(b"fake-executable")
    monkeypatch.setattr(collector, "_pyz_module_names", lambda _path: {"netmiko", "os"})
    collector.write_package_files(ROOT, package_root, executable=executable)
    return package_root


def test_manifest_matches_complete_and_analyzed_installed_lgpl_sources() -> None:
    manifest, raw = collector.load_manifest(ROOT)
    installed = collector.validate_installed_components(manifest)

    assert raw.endswith(b"\n")
    assert {item["distribution"] for item in manifest["components"]} == {
        "paramiko",
        "pyside6-essentials",
        "scp",
        "shiboken6",
    }
    assert set(installed["paramiko"]["sources"]) == {
        path
        for item in manifest["components"]
        if item["distribution"] == "paramiko"
        for path in item["source_paths"]
    }
    assert set(installed["scp"]["sources"]) == {"_internal/scp.py"}
    assert set(installed["pyside6-essentials"]["sources"]) == {
        "_internal/pyside6/__init__.py"
    }
    assert set(installed["shiboken6"]["sources"]) == {
        "_internal/shiboken6/__init__.py"
    }


def test_pyinstaller_collects_lgpl_python_modules_as_external_source_only() -> None:
    tree = ast.parse((ROOT / "ArubaMiniDashboard.spec").read_text(encoding="utf-8"))
    analysis = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Analysis"
    )
    mode_keyword = next(keyword for keyword in analysis.keywords if keyword.arg == "module_collection_mode")

    assert ast.literal_eval(mode_keyword.value) == {
        "PySide6": "py",
        "paramiko": "py",
        "scp": "py",
        "shiboken6": "py",
    }


def test_package_inventory_binds_sources_licenses_and_pyz_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = _fake_package(tmp_path, monkeypatch)

    collector.check_package_files(
        ROOT, package_root, executable=package_root / "ArubaMiniDashboard.exe"
    )
    inventory = json.loads(
        (package_root / collector.INVENTORY_NAME).read_text(encoding="utf-8")
    )

    assert {item["distribution"] for item in inventory["components"]} == {
        "paramiko",
        "pyside6-essentials",
        "scp",
        "shiboken6",
    }
    assert inventory["embedded_pyz_check"]["prohibited_module_roots"] == [
        "PySide6",
        "paramiko",
        "scp",
        "shiboken6",
    ]
    assert inventory["embedded_pyz_check"]["prohibited_modules_found"] == []
    assert all(item["collection_mode"] == "external-python-source" for item in inventory["components"])
    assert {
        item["distribution"]: item["source_scope"] for item in inventory["components"]
    } == {
        "paramiko": "complete-distribution-python",
        "pyside6-essentials": "pyinstaller-analyzed-python",
        "scp": "complete-distribution-python",
        "shiboken6": "pyinstaller-analyzed-python",
    }

    (package_root / "_internal" / "scp.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(collector.LgplRuntimeError, match="does not match installed"):
        collector.check_package_files(
            ROOT, package_root, executable=package_root / "ArubaMiniDashboard.exe"
        )


def test_any_reviewed_lgpl_module_frozen_in_pyz_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ArubaMiniDashboard.exe"
    executable.write_bytes(b"fake")
    manifest, _raw = collector.load_manifest(ROOT)
    monkeypatch.setattr(
        collector,
        "_pyz_module_names",
        lambda _path: {"PySide6", "paramiko", "paramiko.client", "scp", "netmiko"},
    )

    with pytest.raises(collector.LgplRuntimeError, match="still frozen in PYZ"):
        collector.verify_not_frozen(executable, manifest)


def test_release_policy_allows_only_exact_reviewed_lgpl_python_paths() -> None:
    assert (
        verifier._forbidden_release_reason(
            "_internal/paramiko/client.py", is_dir=False
        )
        is None
    )
    assert (
        verifier._forbidden_release_reason(
            "ArubaMiniDashboard/_internal/scp.py",
            is_dir=False,
            archive_member=True,
        )
        is None
    )
    assert (
        verifier._forbidden_release_reason(
            "_internal/PySide6/__init__.py", is_dir=False
        )
        is None
    )
    assert "Python source" in str(
        verifier._forbidden_release_reason(
            "_internal/paramiko/unreviewed.py", is_dir=False
        )
    )
    assert "Python source" in str(
        verifier._forbidden_release_reason("src/application.py", is_dir=False)
    )


def test_release_verifier_rechecks_inventory_hashes_and_embedded_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = _fake_package(tmp_path, monkeypatch)
    manifest, _raw = collector.load_manifest(ROOT)
    notice_lines = []
    for component in manifest["components"]:
        notice_lines.extend(
            [
                f"{component['distribution']}=={component['version']}",
                component["license_sha256"],
            ]
        )
    (package_root / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n".join(notice_lines), encoding="utf-8"
    )
    files = [path for path in package_root.rglob("*") if path.is_file()]
    monkeypatch.setattr(verifier, "_embedded_pyz_module_names", lambda _path: {"netmiko"})

    verifier._verify_lgpl_runtime_contract(
        package_root, files, package_root / "ArubaMiniDashboard.exe"
    )

    inventory_path = package_root / collector.INVENTORY_NAME
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["components"][0]["version"] = "0.0.0"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(SystemExit, match="version mismatch"):
        verifier._verify_lgpl_runtime_contract(
            package_root, files, package_root / "ArubaMiniDashboard.exe"
        )
