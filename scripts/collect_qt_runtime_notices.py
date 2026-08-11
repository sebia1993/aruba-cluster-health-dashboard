from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
from typing import Any, Sequence


MANIFEST_NAME = "qt_runtime_manifest.json"
NOTICE_NAME = "QT_THIRD_PARTY_NOTICES.txt"
INVENTORY_NAME = "QT_RUNTIME_INVENTORY.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class QtNoticeError(RuntimeError):
    """Raised when the reviewed Qt runtime boundary is incomplete or stale."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normal_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as exc:
        raise QtNoticeError(f"Required Qt notice input is missing: {path}: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QtNoticeError(f"Cannot read Qt runtime manifest: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QtNoticeError(f"Qt runtime manifest root must be an object: {path}")
    return value


def load_manifest(project_root: Path) -> tuple[dict[str, Any], bytes]:
    manifest_path = project_root / "scripts" / MANIFEST_NAME
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise QtNoticeError(f"Qt runtime manifest is missing: {manifest_path}: {exc}") from exc
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != 1:
        raise QtNoticeError("Unsupported Qt runtime manifest schema")
    qt_version = manifest.get("qt_version")
    if not isinstance(qt_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", qt_version):
        raise QtNoticeError("Qt runtime manifest has an invalid qt_version")

    repositories = manifest.get("source_repositories")
    if not isinstance(repositories, dict) or set(repositories) != {"qtbase", "qtsvg"}:
        raise QtNoticeError("Qt runtime manifest must pin qtbase and qtsvg sources")
    for name, source in repositories.items():
        if not isinstance(source, dict):
            raise QtNoticeError(f"Invalid source repository entry: {name}")
        if source.get("tag") != f"v{qt_version}":
            raise QtNoticeError(f"Qt source tag/version mismatch: {name}")
        for field in ("tag_object", "commit"):
            if not isinstance(source.get(field), str) or not COMMIT_PATTERN.fullmatch(source[field]):
                raise QtNoticeError(f"Invalid {field} for Qt source repository: {name}")
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith("https://code.qt.io/"):
            raise QtNoticeError(f"Qt source must use the official code.qt.io repository: {name}")

    license_files = manifest.get("license_files")
    if not isinstance(license_files, dict) or not license_files:
        raise QtNoticeError("Qt runtime manifest contains no license files")
    license_root = project_root / "scripts" / "license_texts" / f"qt-{qt_version}"
    actual_names = {path.name for path in license_root.glob("*.txt") if path.is_file()}
    expected_names = set(license_files)
    if actual_names != expected_names:
        raise QtNoticeError(
            "Qt license file set does not match the reviewed manifest: "
            f"missing={sorted(expected_names - actual_names)}, unexpected={sorted(actual_names - expected_names)}"
        )
    for filename, entry in license_files.items():
        if not isinstance(entry, dict) or not SHA256_PATTERN.fullmatch(str(entry.get("sha256", ""))):
            raise QtNoticeError(f"Invalid Qt license hash declaration: {filename}")
        path = license_root / filename
        actual_hash = _sha256(path.read_bytes())
        if actual_hash != entry["sha256"]:
            raise QtNoticeError(
                f"Qt license hash mismatch for {filename}: expected={entry['sha256']}, actual={actual_hash}"
            )
        source_path = entry.get("source_path")
        if not isinstance(source_path, str) or not source_path.startswith(("qtbase/LICENSES/", "qtsvg/LICENSES/")):
            raise QtNoticeError(f"Invalid official Qt license source path: {filename}")

    source_files = manifest.get("source_files")
    components = manifest.get("components")
    artifacts = manifest.get("artifacts")
    if not isinstance(source_files, dict) or not isinstance(components, dict) or not isinstance(artifacts, dict):
        raise QtNoticeError("Qt runtime manifest inventory sections must be objects")
    if not components or not artifacts:
        raise QtNoticeError("Qt runtime manifest must contain components and artifacts")

    referenced_components: set[str] = set()
    for artifact, component_ids in artifacts.items():
        if (
            artifact != artifact.casefold()
            or "\\" in artifact
            or not artifact.startswith(("pyside6/", "shiboken6/"))
        ):
            raise QtNoticeError(f"Qt artifact path is not normalized: {artifact}")
        if not isinstance(component_ids, list) or len(component_ids) != len(set(component_ids)):
            raise QtNoticeError(f"Invalid component mapping for Qt artifact: {artifact}")
        for component_id in component_ids:
            if component_id not in components:
                raise QtNoticeError(f"Unknown component {component_id} mapped by {artifact}")
            referenced_components.add(component_id)
    if referenced_components != set(components):
        raise QtNoticeError(
            "Qt component set is not exactly tied to the artifact inventory: "
            f"unmapped={sorted(set(components) - referenced_components)}"
        )

    referenced_licenses: set[str] = set()
    for component_id, component in components.items():
        if not isinstance(component, dict) or component.get("Id") != component_id:
            raise QtNoticeError(f"Invalid Qt component record: {component_id}")
        source_path = component.get("source_attribution")
        source_hash = component.get("source_sha256")
        if source_path not in source_files or source_files[source_path].get("sha256") != source_hash:
            raise QtNoticeError(f"Qt attribution provenance mismatch: {component_id}")
        if not SHA256_PATTERN.fullmatch(str(source_hash or "")):
            raise QtNoticeError(f"Invalid Qt attribution hash: {component_id}")
        mapped_licenses = component.get("license_files")
        if not isinstance(mapped_licenses, list) or not mapped_licenses:
            raise QtNoticeError(f"Qt component has no mapped license text: {component_id}")
        for filename in mapped_licenses:
            if filename not in license_files:
                raise QtNoticeError(f"Unknown Qt license file {filename} used by {component_id}")
            referenced_licenses.add(filename)
    if referenced_licenses != set(license_files):
        raise QtNoticeError(
            "Qt license file set contains unreviewed/stale entries: "
            f"unused={sorted(set(license_files) - referenced_licenses)}"
        )
    return manifest, raw


def _copyright_lines(value: object) -> list[str]:
    if isinstance(value, str):
        return value.splitlines() or [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        result: list[str] = []
        for item in value:
            result.extend(item.splitlines() or [item])
        return result
    return ["Not stated in the selected Qt attribution record"]


def build_notice(project_root: Path) -> str:
    manifest, raw_manifest = load_manifest(project_root)
    artifacts: dict[str, list[str]] = manifest["artifacts"]
    components: dict[str, dict[str, Any]] = manifest["components"]
    component_artifacts: dict[str, list[str]] = {component_id: [] for component_id in components}
    for artifact, component_ids in artifacts.items():
        for component_id in component_ids:
            component_artifacts[component_id].append(artifact)

    lines = [
        f"QT {manifest['qt_version']} EMBEDDED THIRD-PARTY NOTICES",
        "=" * 78,
        "",
        "This file supplements THIRD_PARTY_NOTICES.txt for the exact reviewed Qt",
        "DLL/plugin inventory of the Aruba Mini Dashboard Windows onedir package.",
        "It is a conservative module-level attribution set and may include code paths",
        "that this application does not exercise. A new or removed Qt DLL/plugin makes",
        "the post-build inventory check fail until this manifest is reviewed.",
        "",
        "This document is provided for attribution and release-audit assistance. It is",
        "not legal advice and does not select or grant a license for the application.",
        "Qt's own licensing terms are documented separately in THIRD_PARTY_NOTICES.txt.",
        "",
        f"Manifest SHA-256: {_sha256(raw_manifest)}",
        f"PySide distribution: {manifest['pyside_distribution']}",
        f"Qt version: {manifest['qt_version']}",
        "Official licensing documentation: https://doc.qt.io/qt-6/licensing.html",
        "Official third-party overview: https://doc.qt.io/qt-6/licenses-used-in-qt.html",
        "",
        "PINNED OFFICIAL QT SOURCES",
        "-" * 78,
    ]
    for repository_name, source in sorted(manifest["source_repositories"].items()):
        lines.extend(
            [
                f"{repository_name}: {source['url']}",
                f"  Tag: {source['tag']}",
                f"  Annotated tag object: {source['tag_object']}",
                f"  Peeled commit: {source['commit']}",
            ]
        )
    lines.extend(["", "REVIEWED QT ARTIFACT INVENTORY", "-" * 78])
    for artifact, component_ids in sorted(artifacts.items()):
        mapping = (
            ", ".join(component_ids)
            if component_ids
            else "No Qt embedded-component mapping; see general runtime notices"
        )
        lines.append(f"{artifact}: {mapping}")

    lines.extend(["", "EMBEDDED COMPONENT ATTRIBUTIONS", "-" * 78])
    for component_id, component in sorted(components.items()):
        lines.extend(
            [
                component.get("Name", component_id),
                f"  ID: {component_id}",
                f"  Version: {component.get('Version', 'not stated by upstream')}",
                f"  Qt usage: {' '.join(str(component.get('QtUsage', 'not stated')).split())}",
                f"  Declared license: {component.get('License', 'not stated')}",
                f"  SPDX/license ID: {component.get('LicenseId', 'not stated')}",
                f"  Mapped artifacts: {', '.join(sorted(component_artifacts[component_id]))}",
                f"  Attribution source: {component['source_attribution']}",
                f"  Attribution source SHA-256: {component['source_sha256']}",
                f"  License files: {', '.join(component['license_files'])}",
                "  Copyright:",
            ]
        )
        lines.extend(f"    {item}" for item in _copyright_lines(component.get("Copyright")))
        lines.append("")

    license_root = project_root / "scripts" / "license_texts" / f"qt-{manifest['qt_version']}"
    lines.extend(["LICENSE TEXTS", "-" * 78, ""])
    for filename, entry in sorted(manifest["license_files"].items()):
        text = _normal_text(license_root / filename).rstrip()
        lines.extend(
            [
                "=" * 78,
                filename,
                f"Official Qt source path: {entry['source_path']}",
                f"SHA-256: {entry['sha256']}",
                "=" * 78,
                text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def check_notice(path: Path, expected: str) -> None:
    actual = _normal_text(path)
    if actual != expected:
        raise QtNoticeError(
            f"Qt third-party notice is stale: {path}. "
            "Regenerate it with scripts/collect_qt_runtime_notices.py --write-notice and review the diff."
        )


def _qt_artifact_paths(package_root: Path) -> dict[str, Path]:
    internal_root = package_root / "_internal"
    runtime_roots = (internal_root / "PySide6", internal_root / "shiboken6")
    for runtime_root in runtime_roots:
        if not runtime_root.is_dir():
            raise QtNoticeError(
                f"Qt/PySide runtime directory is missing from onedir package: {runtime_root}"
            )
    results: dict[str, Path] = {}
    candidates = [
        path
        for runtime_root in runtime_roots
        for path in runtime_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".dll", ".pyd"}
    ]
    for path in candidates:
        relative = path.relative_to(internal_root).as_posix().casefold()
        if relative in results:
            raise QtNoticeError(f"Case-insensitive duplicate Qt artifact path: {relative}")
        results[relative] = path
    return results


def build_package_inventory(project_root: Path, package_root: Path) -> dict[str, Any]:
    manifest, raw_manifest = load_manifest(project_root)
    actual = _qt_artifact_paths(package_root)
    expected = set(manifest["artifacts"])
    actual_names = set(actual)
    if actual_names != expected:
        raise QtNoticeError(
            "Qt runtime artifact set is outside the reviewed boundary: "
            f"missing={sorted(expected - actual_names)}, unreviewed={sorted(actual_names - expected)}"
        )
    return {
        "schema_version": 1,
        "qt_version": manifest["qt_version"],
        "pyside_distribution": manifest["pyside_distribution"],
        "manifest_sha256": _sha256(raw_manifest),
        "source_repositories": manifest["source_repositories"],
        "artifacts": [
            {
                "path": name,
                "sha256": _sha256(actual[name].read_bytes()),
                "embedded_components": manifest["artifacts"][name],
            }
            for name in sorted(actual)
        ],
    }


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_package_files(project_root: Path, package_root: Path) -> None:
    inventory = build_package_inventory(project_root, package_root)
    notice = build_notice(project_root)
    (package_root / INVENTORY_NAME).write_text(_json_text(inventory), encoding="utf-8", newline="\n")
    (package_root / NOTICE_NAME).write_text(notice, encoding="utf-8", newline="\n")


def check_package_files(project_root: Path, package_root: Path) -> None:
    expected_inventory = _json_text(build_package_inventory(project_root, package_root))
    actual_inventory = _normal_text(package_root / INVENTORY_NAME)
    if actual_inventory != expected_inventory:
        raise QtNoticeError(f"Packaged Qt runtime inventory is missing or stale: {package_root / INVENTORY_NAME}")
    check_notice(package_root / NOTICE_NAME, build_notice(project_root))


def validate_installed_qt(manifest: dict[str, Any]) -> None:
    expected = manifest["qt_version"]
    try:
        installed = metadata.version("PySide6-Essentials")
    except metadata.PackageNotFoundError as exc:
        raise QtNoticeError("PySide6-Essentials is not installed in the audit environment") from exc
    if installed != expected:
        raise QtNoticeError(f"PySide6-Essentials/manifest mismatch: installed={installed}, expected={expected}")
    try:
        from PySide6.QtCore import qVersion
    except ImportError as exc:
        raise QtNoticeError(f"Cannot import the installed Qt runtime: {exc}") from exc
    runtime = qVersion()
    if runtime != expected:
        raise QtNoticeError(f"Qt runtime/manifest mismatch: runtime={runtime}, expected={expected}")


def main(argv: Sequence[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Generate and fail-closed verify the reviewed Qt DLL/plugin notice boundary."
    )
    parser.add_argument("--project-root", type=Path, default=default_root)
    parser.add_argument("--notice", type=Path)
    parser.add_argument("--write-notice", action="store_true")
    parser.add_argument("--check-notice", action="store_true")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--write-package-files", action="store_true")
    parser.add_argument("--check-package-files", action="store_true")
    arguments = parser.parse_args(argv)
    project_root = arguments.project_root.resolve()
    notice_path = arguments.notice or project_root / "docs" / NOTICE_NAME
    try:
        manifest, _raw = load_manifest(project_root)
        validate_installed_qt(manifest)
        expected_notice = build_notice(project_root)
        if arguments.write_notice:
            notice_path.parent.mkdir(parents=True, exist_ok=True)
            notice_path.write_text(expected_notice, encoding="utf-8", newline="\n")
            print(f"Wrote {notice_path}")
        if arguments.check_notice or not any(
            (arguments.write_notice, arguments.write_package_files, arguments.check_package_files)
        ):
            check_notice(notice_path, expected_notice)
            print(f"QT_THIRD_PARTY_NOTICES_OK: {notice_path}")
        if arguments.write_package_files or arguments.check_package_files:
            if arguments.package_root is None:
                raise QtNoticeError("--package-root is required for packaged Qt inventory operations")
            package_root = arguments.package_root.resolve()
            if arguments.write_package_files:
                write_package_files(project_root, package_root)
                print(f"QT_RUNTIME_INVENTORY_WRITTEN: {package_root / INVENTORY_NAME}")
            if arguments.check_package_files:
                check_package_files(project_root, package_root)
                print(f"QT_RUNTIME_PACKAGE_OK: {package_root}")
    except QtNoticeError as exc:
        print(f"QT_RUNTIME_NOTICE_ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
