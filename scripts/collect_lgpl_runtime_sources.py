"""Build and verify the replaceable LGPL Python-runtime boundary.

The public Windows artifact is onedir-only. PySide6, shiboken6, Paramiko, and
scp modules used by the build are collected as external Python source files,
not as bytecode in PyInstaller's PYZ archive. This script ties those files and
their exact installed or pinned-official license evidence to a reviewed
manifest and a hash-bound package inventory.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Sequence


MANIFEST_NAME = "lgpl_runtime_manifest.json"
INVENTORY_NAME = "LGPL_RUNTIME_INVENTORY.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVIEWED_DISTRIBUTIONS = {"paramiko", "pyside6-essentials", "scp", "shiboken6"}


class LgplRuntimeError(RuntimeError):
    """Raised when the reviewed replaceable-source boundary is incomplete."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LgplRuntimeError(f"Cannot read LGPL runtime manifest: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LgplRuntimeError("LGPL runtime manifest root must be an object")
    return value, raw


def _normal_release_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise LgplRuntimeError(f"{label} must be a non-empty forward-slash path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LgplRuntimeError(f"{label} is not a safe relative path: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        raise LgplRuntimeError(f"{label} must be normalized: {value!r}")
    return normalized


def load_manifest(project_root: Path) -> tuple[dict[str, Any], bytes]:
    manifest_path = project_root / "scripts" / MANIFEST_NAME
    manifest, raw = _read_json(manifest_path)
    if manifest.get("schema_version") != 1:
        raise LgplRuntimeError("Unsupported LGPL runtime manifest schema")
    components = manifest.get("components")
    if not isinstance(components, list) or len(components) != len(REVIEWED_DISTRIBUTIONS):
        raise LgplRuntimeError(
            "LGPL runtime manifest must contain exactly Paramiko, scp, PySide6-Essentials, and shiboken6"
        )

    names: set[str] = set()
    all_sources: set[str] = set()
    all_license_outputs: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise LgplRuntimeError("Invalid LGPL runtime component record")
        distribution = component.get("distribution")
        if not isinstance(distribution, str) or distribution != distribution.casefold():
            raise LgplRuntimeError("LGPL runtime distribution names must be lowercase")
        names.add(distribution)
        version = component.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise LgplRuntimeError(f"Invalid reviewed version for {distribution}")
        module_names = component.get("module_names")
        if not isinstance(module_names, list) or not module_names or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", name)
            for name in module_names
        ):
            raise LgplRuntimeError(f"Invalid module_names for {distribution}")
        sources = component.get("source_paths")
        if not isinstance(sources, list) or not sources:
            raise LgplRuntimeError(f"No replaceable source paths declared for {distribution}")
        normalized_sources = {
            _normal_release_path(value, label=f"{distribution} source path") for value in sources
        }
        if len(normalized_sources) != len(sources) or not all(
            path == path.casefold() and path.startswith("_internal/") and path.endswith(".py")
            for path in normalized_sources
        ):
            raise LgplRuntimeError(f"Invalid or duplicate replaceable source path for {distribution}")
        source_scope = component.get("source_scope")
        if source_scope not in {"complete-distribution-python", "pyinstaller-analyzed-python"}:
            raise LgplRuntimeError(f"Invalid source_scope for {distribution}")
        overlap = all_sources & normalized_sources
        if overlap:
            raise LgplRuntimeError(f"Duplicate LGPL source paths: {sorted(overlap)}")
        all_sources.update(normalized_sources)

        license_source_kind = component.get("license_source_kind")
        if license_source_kind not in {"installed-distribution", "project-supplemental"}:
            raise LgplRuntimeError(f"Invalid license_source_kind for {distribution}")
        license_source = component.get("license_source")
        if not isinstance(license_source, str) or "\\" in license_source:
            raise LgplRuntimeError(f"Invalid installed license source for {distribution}")
        license_output = _normal_release_path(
            component.get("license_output"), label=f"{distribution} license output"
        )
        folded_license_output = license_output.casefold()
        if not folded_license_output.startswith("lgpl_runtime_licenses/") or not folded_license_output.endswith(".txt"):
            raise LgplRuntimeError(f"Invalid packaged license path for {distribution}")
        if license_output in all_license_outputs:
            raise LgplRuntimeError(f"Duplicate packaged license path: {license_output}")
        all_license_outputs.add(license_output)
        if not SHA256_PATTERN.fullmatch(str(component.get("license_sha256", ""))):
            raise LgplRuntimeError(f"Invalid license hash for {distribution}")

    if names != REVIEWED_DISTRIBUTIONS:
        raise LgplRuntimeError(
            f"LGPL runtime manifest component mismatch: expected={sorted(REVIEWED_DISTRIBUTIONS)}, "
            f"actual={sorted(names)}"
        )
    return manifest, raw


def _distribution_source_files(
    distribution_name: str,
    distribution: metadata.Distribution,
    *,
    expected_sources: set[str],
    source_scope: str,
) -> dict[str, Path]:
    results: dict[str, Path] = {}
    import_roots = {
        "paramiko": "paramiko/",
        "pyside6-essentials": "PySide6/",
        "shiboken6": "shiboken6/",
    }
    for relative in distribution.files or ():
        value = str(relative).replace("\\", "/")
        selected = (
            value == "scp.py"
            if distribution_name == "scp"
            else value.startswith(import_roots[distribution_name]) and value.endswith(".py")
        )
        if not selected:
            continue
        package_path = f"_internal/{value}".casefold()
        if source_scope == "pyinstaller-analyzed-python" and package_path not in expected_sources:
            continue
        located = Path(distribution.locate_file(relative))
        if not located.is_file():
            raise LgplRuntimeError(f"Installed source file is missing: {located}")
        results[package_path] = located
    return results


def validate_installed_components(
    manifest: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    resolved_project_root = project_root or Path(__file__).resolve().parents[1]
    installed: dict[str, dict[str, Any]] = {}
    for component in manifest["components"]:
        name = component["distribution"]
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise LgplRuntimeError(f"Reviewed runtime distribution is not installed: {name}") from exc
        if distribution.version != component["version"]:
            raise LgplRuntimeError(
                f"Installed/reviewed version mismatch for {name}: "
                f"installed={distribution.version}, reviewed={component['version']}"
            )
        expected_sources = {str(path).casefold() for path in component["source_paths"]}
        sources = _distribution_source_files(
            name,
            distribution,
            expected_sources=expected_sources,
            source_scope=component["source_scope"],
        )
        if set(sources) != expected_sources:
            raise LgplRuntimeError(
                f"Installed source inventory mismatch for {name}: "
                f"missing={sorted(expected_sources - set(sources))}, "
                f"unreviewed={sorted(set(sources) - expected_sources)}"
            )
        license_path = (
            Path(distribution.locate_file(component["license_source"]))
            if component["license_source_kind"] == "installed-distribution"
            else resolved_project_root / Path(component["license_source"])
        )
        try:
            license_bytes = license_path.read_bytes()
        except OSError as exc:
            raise LgplRuntimeError(f"Installed license evidence is missing: {license_path}: {exc}") from exc
        if component["license_source_kind"] == "project-supplemental":
            try:
                license_bytes = (
                    license_bytes.decode("utf-8")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .encode("utf-8")
                )
            except UnicodeError as exc:
                raise LgplRuntimeError(
                    f"Supplemental license evidence is not valid UTF-8: {license_path}: {exc}"
                ) from exc
        license_hash = _sha256(license_bytes)
        if license_hash != component["license_sha256"]:
            raise LgplRuntimeError(
                f"Installed license evidence hash mismatch for {name}: "
                f"expected={component['license_sha256']}, actual={license_hash}"
            )
        installed[name] = {
            "distribution": distribution,
            "sources": sources,
            "license_path": license_path,
            "license_bytes": license_bytes,
        }
    return installed


def _pyz_module_names(executable: Path) -> set[str]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise LgplRuntimeError("PyInstaller is required to inspect the embedded PYZ archive") from exc
    try:
        archive = CArchiveReader(str(executable))
        pyz = archive.open_embedded_archive("PYZ.pyz")
        return set(pyz.toc)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise LgplRuntimeError(f"Cannot inspect embedded PYZ archive: {executable}: {exc}") from exc


def verify_not_frozen(executable: Path, manifest: dict[str, Any]) -> list[str]:
    if not executable.is_file():
        raise LgplRuntimeError(f"Package executable is missing: {executable}")
    modules = _pyz_module_names(executable)
    prohibited: set[str] = set()
    for component in manifest["components"]:
        for root in component["module_names"]:
            folded_root = root.casefold()
            prohibited.update(
                name
                for name in modules
                if name.casefold() == folded_root or name.casefold().startswith(f"{folded_root}.")
            )
    if prohibited:
        raise LgplRuntimeError(
            "Replaceable LGPL Python modules are still frozen in PYZ: " + ", ".join(sorted(prohibited))
        )
    return sorted(modules)


def _find_executable(package_root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else package_root / explicit
        return path.resolve()
    candidates = sorted(package_root.glob("*.exe"))
    if len(candidates) != 1:
        raise LgplRuntimeError(
            f"Expected exactly one package executable, found {len(candidates)} in {package_root}"
        )
    return candidates[0].resolve()


def _all_python_sources(package_root: Path) -> set[str]:
    return {
        path.relative_to(package_root).as_posix().casefold()
        for path in package_root.rglob("*.py")
        if path.is_file()
    }


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_package_inventory(
    project_root: Path,
    package_root: Path,
    *,
    executable: Path | None = None,
) -> dict[str, Any]:
    manifest, raw_manifest = load_manifest(project_root)
    installed = validate_installed_components(manifest, project_root=project_root)
    expected_sources = {
        str(path).casefold()
        for component in manifest["components"]
        for path in component["source_paths"]
    }
    actual_sources = _all_python_sources(package_root)
    if actual_sources != expected_sources:
        raise LgplRuntimeError(
            "Packaged Python source boundary mismatch: "
            f"missing={sorted(expected_sources - actual_sources)}, "
            f"unreviewed={sorted(actual_sources - expected_sources)}"
        )

    resolved_executable = _find_executable(package_root, executable)
    verify_not_frozen(resolved_executable, manifest)
    component_inventory: list[dict[str, Any]] = []
    for component in manifest["components"]:
        name = component["distribution"]
        installed_component = installed[name]
        source_inventory: list[dict[str, str]] = []
        for relative in component["source_paths"]:
            packaged_path = package_root / Path(relative)
            try:
                packaged_bytes = packaged_path.read_bytes()
                installed_bytes = installed_component["sources"][relative].read_bytes()
            except OSError as exc:
                raise LgplRuntimeError(f"Cannot read replaceable source file {relative}: {exc}") from exc
            if packaged_bytes != installed_bytes:
                raise LgplRuntimeError(
                    f"Packaged source does not match installed {name}=={component['version']}: {relative}"
                )
            source_inventory.append({"path": relative, "sha256": _sha256(packaged_bytes)})

        license_output = package_root / Path(component["license_output"])
        try:
            packaged_license = license_output.read_bytes()
        except OSError as exc:
            raise LgplRuntimeError(f"Packaged license evidence is missing: {license_output}: {exc}") from exc
        if packaged_license != installed_component["license_bytes"]:
            raise LgplRuntimeError(f"Packaged license evidence mismatch for {name}")
        component_inventory.append(
            {
                "distribution": name,
                "version": component["version"],
                "collection_mode": "external-python-source",
                "source_scope": component["source_scope"],
                "sources": source_inventory,
                "license": {
                    "path": component["license_output"],
                    "sha256": component["license_sha256"],
                },
            }
        )
    return {
        "schema_version": 1,
        "manifest_sha256": _sha256(raw_manifest),
        "components": component_inventory,
        "embedded_pyz_check": {
            "archive": f"{resolved_executable.name}::PYZ.pyz",
            "prohibited_module_roots": sorted(
                {name for component in manifest["components"] for name in component["module_names"]}
            ),
            "prohibited_modules_found": [],
        },
    }


def copy_license_files(project_root: Path, package_root: Path) -> None:
    manifest, _raw = load_manifest(project_root)
    installed = validate_installed_components(manifest, project_root=project_root)
    for component in manifest["components"]:
        destination = package_root / Path(component["license_output"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(installed[component["distribution"]]["license_bytes"])


def write_package_files(
    project_root: Path,
    package_root: Path,
    *,
    executable: Path | None = None,
) -> None:
    copy_license_files(project_root, package_root)
    inventory = build_package_inventory(project_root, package_root, executable=executable)
    (package_root / INVENTORY_NAME).write_text(
        _json_text(inventory), encoding="utf-8", newline="\n"
    )


def check_package_files(
    project_root: Path,
    package_root: Path,
    *,
    executable: Path | None = None,
) -> None:
    expected = _json_text(
        build_package_inventory(project_root, package_root, executable=executable)
    )
    inventory_path = package_root / INVENTORY_NAME
    try:
        actual = inventory_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        raise LgplRuntimeError(f"Packaged LGPL runtime inventory is missing: {inventory_path}: {exc}") from exc
    if actual != expected:
        raise LgplRuntimeError(f"Packaged LGPL runtime inventory is stale: {inventory_path}")


def main(argv: Sequence[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Verify replaceable LGPL Python sources and their package inventory."
    )
    parser.add_argument("--project-root", type=Path, default=default_root)
    parser.add_argument("--check-manifest", action="store_true")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--write-package-files", action="store_true")
    parser.add_argument("--check-package-files", action="store_true")
    arguments = parser.parse_args(argv)
    project_root = arguments.project_root.resolve()
    try:
        manifest, _raw = load_manifest(project_root)
        validate_installed_components(manifest, project_root=project_root)
        if arguments.check_manifest or not any(
            (arguments.write_package_files, arguments.check_package_files)
        ):
            print("LGPL_RUNTIME_MANIFEST_OK")
        if arguments.write_package_files or arguments.check_package_files:
            if arguments.package_root is None:
                raise LgplRuntimeError("--package-root is required for package operations")
            package_root = arguments.package_root.resolve()
            executable = arguments.executable
            if arguments.write_package_files:
                write_package_files(project_root, package_root, executable=executable)
                print(f"LGPL_RUNTIME_INVENTORY_WRITTEN: {package_root / INVENTORY_NAME}")
            if arguments.check_package_files:
                check_package_files(project_root, package_root, executable=executable)
                print(f"LGPL_RUNTIME_PACKAGE_OK: {package_root}")
    except LgplRuntimeError as exc:
        print(f"LGPL_RUNTIME_ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
