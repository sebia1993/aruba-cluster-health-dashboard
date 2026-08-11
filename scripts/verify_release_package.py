"""Fail-closed verification for Aruba Mini Dashboard Windows releases.

The verifier accepts a release folder or ZIP through ``--path``/``--zip``.
The historical ``--one-file`` flag is retained only to reject that unsupported
distribution form. ZIPs are fully inspected before extraction, extracted into
an isolated temporary directory, inspected a second time, and only then
smoke-tested.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile


REQUIRED_RELEASE_DOCUMENTS = (
    "config.example.json",
    "LGPL_RUNTIME_INVENTORY.json",
    "LGPL_RUNTIME_REPLACEMENT_KO_EN.md",
    "README.txt",
    "QT_RUNTIME_INVENTORY.json",
    "QT_THIRD_PARTY_NOTICES.txt",
    "THIRD_PARTY_NOTICES.txt",
    "WINDOWS11_QA_CHECKLIST_KO.md",
)
COMMITTED_RELEASE_DOCUMENT_SOURCES = {
    "config.example.json": "config.example.json",
    "LGPL_RUNTIME_REPLACEMENT_KO_EN.md": "docs/LGPL_RUNTIME_REPLACEMENT_KO_EN.md",
    "README.txt": "docs/README.txt",
    "QT_THIRD_PARTY_NOTICES.txt": "docs/QT_THIRD_PARTY_NOTICES.txt",
    "THIRD_PARTY_NOTICES.txt": "docs/THIRD_PARTY_NOTICES.txt",
    "WINDOWS11_QA_CHECKLIST_KO.md": "docs/WINDOWS11_QA_CHECKLIST_KO.md",
}
REQUIRED_QT_PLUGIN_PATHS = (
    "_internal/PySide6/plugins/platforms/qwindows.dll",
    "_internal/PySide6/plugins/imageformats/qsvg.dll",
)
REQUIRED_QT_BINDING_PATHS = (
    "_internal/shiboken6/MSVCP140.dll",
    "_internal/shiboken6/Shiboken.pyd",
    "_internal/shiboken6/shiboken6.abi3.dll",
    "_internal/shiboken6/VCRUNTIME140.dll",
    "_internal/shiboken6/VCRUNTIME140_1.dll",
)
REQUIRED_SMOKE_MARKERS = {
    "ARUBA_MINI_DASHBOARD_SMOKE_OK",
    "NETMIKO_OK",
    "PARAMIKO_OK",
    "FIXTURE_DISCOVERY_OK",
    "DEMO_CORRELATION_OK",
}

MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_MEMBER_SIZE = 1024 * 1024 * 1024
MAX_ZIP_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
WINDOWS_REPARSE_POINT = 0x400
WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

DEVELOPMENT_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".github",
    ".gitlab",
    ".hypothesis",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "htmlcov",
    "pip-wheel-metadata",
    "venv",
}
LOG_DIRECTORY_NAMES = {"log", "logs"}
SETTINGS_DIRECTORY_NAMES = {".settings", "settings"}
PYTHON_SOURCE_SUFFIXES = {
    ".egg",
    ".ipynb",
    ".py",
    ".pyc",
    ".pyo",
    ".pyi",
    ".pyz",
    ".spec",
    ".whl",
}
PRIVATE_KEY_AND_CERTIFICATE_SUFFIXES = {
    ".cer",
    ".crt",
    ".csr",
    ".der",
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
    ".pub",
}
DUMP_SUFFIXES = {".core", ".crash", ".dmp", ".dump", ".mdmp"}
DATABASE_SUFFIXES = {".db", ".db3", ".sqlite", ".sqlite3", ".wal", ".shm"}
SECRET_SUFFIXES = {".cred", ".credential", ".credentials", ".secret", ".secrets"}
PYTHON_DEVELOPMENT_FILENAMES = {
    ".coverage",
    "coverage.xml",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "uv.lock",
}
RUNTIME_CONFIG_FILENAMES = {
    "config.ini",
    "config.json",
    "config.toml",
    "config.yaml",
    "config.yml",
}
HOST_KEY_FILENAMES = {
    "authorized_keys",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
UNAPPROVED_QT_COMPONENT_MARKERS = {
    "qt6opengl",
    "qt6pdf",
    "qt6qml",
    "qt6quick",
    "qtopengl",
    "qtpdf",
    "qtqml",
    "qtquick",
    "qtvirtualkeyboard",
}
UNAPPROVED_QT_COMPONENT_FILENAMES = {"qpdf.dll"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LGPL_MANIFEST_PATH = PROJECT_ROOT / "scripts" / "lgpl_runtime_manifest.json"
QT_MANIFEST_PATH = PROJECT_ROOT / "scripts" / "qt_runtime_manifest.json"


def _fail(message: str, *, cause: BaseException | None = None) -> None:
    error = SystemExit(message)
    if cause is None:
        raise error
    raise error from cause


@lru_cache(maxsize=None)
def _load_reviewed_manifest(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"Reviewed {label} manifest cannot be read: {path}: {exc}", cause=exc)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        _fail(f"Reviewed {label} manifest has an unsupported schema: {path}")
    normalized = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return value, normalized


def _reviewed_lgpl_manifest() -> tuple[dict[str, object], bytes]:
    manifest, raw = _load_reviewed_manifest(LGPL_MANIFEST_PATH, label="LGPL runtime")
    components = manifest.get("components")
    if not isinstance(components, list) or {
        item.get("distribution") for item in components if isinstance(item, dict)
    } != {"paramiko", "pyside6-essentials", "scp", "shiboken6"}:
        _fail(
            "Reviewed LGPL runtime manifest must contain Paramiko, scp, "
            "PySide6-Essentials, and shiboken6"
        )
    return manifest, raw


@lru_cache(maxsize=None)
def _approved_lgpl_source_paths() -> frozenset[str]:
    manifest, _raw = _reviewed_lgpl_manifest()
    paths: set[str] = set()
    for component in manifest["components"]:  # type: ignore[index]
        if not isinstance(component, dict) or not isinstance(component.get("source_paths"), list):
            _fail("Reviewed LGPL runtime manifest has invalid source paths")
        for raw_path in component["source_paths"]:
            if not isinstance(raw_path, str):
                _fail("Reviewed LGPL runtime source path is not a string")
            normalized = PurePosixPath(raw_path).as_posix().casefold()
            if normalized != raw_path or not normalized.startswith("_internal/") or not normalized.endswith(".py"):
                _fail(f"Reviewed LGPL runtime source path is invalid: {raw_path!r}")
            paths.add(normalized)
    return frozenset(paths)


def _policy_relative_path(relative_path: str, *, archive_member: bool) -> str:
    parts = PurePosixPath(relative_path).parts
    if archive_member and len(parts) > 1:
        parts = parts[1:]
    return "/".join(_portable_fold(part) for part in parts)


def _portable_fold(value: str) -> str:
    """Normalize a path component the way a Windows release should compare it."""

    return unicodedata.normalize("NFC", value).casefold()


def _validate_windows_component(component: str, *, label: str) -> None:
    folded = _portable_fold(component)
    if not component or component in {".", ".."}:
        _fail(f"{label} contains an empty or traversal path component")
    if component.endswith((" ", ".")):
        _fail(f"{label} contains a Windows-ambiguous path component: {component!r}")
    if ":" in component:
        _fail(f"{label} contains a Windows alternate-stream or drive component: {component!r}")
    reserved_base = folded.split(".", 1)[0]
    if reserved_base in WINDOWS_RESERVED_BASENAMES:
        _fail(f"{label} contains a Windows reserved path component: {component!r}")


def _forbidden_release_reason(
    relative_path: str,
    *,
    is_dir: bool,
    archive_member: bool = False,
) -> str | None:
    """Return a human-readable policy reason for a release-tree path."""

    parts = PurePosixPath(relative_path).parts
    folded_parts = tuple(_portable_fold(part) for part in parts)
    if not folded_parts:
        return "empty release path"

    for part in folded_parts:
        if part in DEVELOPMENT_DIRECTORY_NAMES or part.endswith(".egg-info"):
            return "Python/cache/version-control artifact"
        if part in LOG_DIRECTORY_NAMES:
            return "log directory"
        if part in SETTINGS_DIRECTORY_NAMES:
            return "runtime settings directory"
        if part.startswith(".git"):
            return "Git metadata"
        if "qtvirtualkeyboard" in part or "qt6virtualkeyboard" in part:
            return "unapproved Qt Virtual Keyboard component"
        if part in UNAPPROVED_QT_COMPONENT_FILENAMES or any(
            marker in part for marker in UNAPPROVED_QT_COMPONENT_MARKERS
        ):
            return "unapproved unused Qt component"

    name = folded_parts[-1]
    suffix = PurePosixPath(name).suffix

    if name == "known_hosts" or name.startswith("known_hosts."):
        return "SSH known_hosts data"
    if name in HOST_KEY_FILENAMES or name.startswith("ssh_host_"):
        return "SSH key material"
    if name == ".env" or name.startswith(".env.") or suffix == ".env":
        return "environment file"
    if name == "settings" or name.startswith(("settings.", "settings-")):
        return "runtime settings file"
    if name in RUNTIME_CONFIG_FILENAMES:
        return "runtime configuration file"
    if suffix in DATABASE_SUFFIXES or re.search(
        r"\.(?:db|db3|sqlite|sqlite3)(?:[-.](?:wal|shm|journal|bak|backup|old|tmp))(?:\.|$)",
        name,
    ):
        return "SQLite/database state"
    if re.search(r"\.log(?:$|[._-])", name):
        return "log file"
    if suffix in DUMP_SUFFIXES or name == "core" or name.startswith(("core.", "hs_err_pid")):
        return "crash or memory dump"
    if suffix in PRIVATE_KEY_AND_CERTIFICATE_SUFFIXES:
        return "key or certificate material"
    if suffix in SECRET_SUFFIXES or name in {
        "credentials.json",
        "secrets.json",
    }:
        return "credential or secret material"
    if suffix == ".py":
        if _policy_relative_path(relative_path, archive_member=archive_member) in _approved_lgpl_source_paths():
            return None
        return "Python source/build artifact"
    if suffix in PYTHON_SOURCE_SUFFIXES or name in PYTHON_DEVELOPMENT_FILENAMES:
        return "Python source/build artifact"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "Python dependency manifest"
    if suffix == ".pdb":
        return "debug symbol artifact"
    if name in {".ds_store", "desktop.ini", "thumbs.db"}:
        return "desktop metadata"

    # A directory named with a file-like sensitive suffix is suspicious too.
    if is_dir and suffix in (
        DATABASE_SUFFIXES
        | PRIVATE_KEY_AND_CERTIFICATE_SUFFIXES
        | DUMP_SUFFIXES
        | SECRET_SUFFIXES
    ):
        return "sensitive artifact directory"
    return None


def _check_release_policy(
    relative_path: str,
    *,
    is_dir: bool,
    label: str,
    archive_member: bool = False,
) -> None:
    reason = _forbidden_release_reason(
        relative_path,
        is_dir=is_dir,
        archive_member=archive_member,
    )
    if reason:
        _fail(f"{label} contains forbidden {reason}: {relative_path}")


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        _fail(f"Release path could not be inspected: {path}: {exc}", cause=exc)
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
    )


def _register_case_safe_path(
    relative_path: str,
    *,
    is_dir: bool,
    label: str,
    explicit_paths: dict[str, str],
    prefix_spellings: dict[str, str],
    path_types: dict[str, str],
) -> None:
    parts = PurePosixPath(relative_path).parts
    normalized_prefixes: list[str] = []
    for index, part in enumerate(parts):
        _validate_windows_component(part, label=f"{label} path {relative_path!r}")
        normalized_prefixes.append(part)
        prefix = "/".join(normalized_prefixes)
        folded = "/".join(_portable_fold(item) for item in normalized_prefixes)
        previous_spelling = prefix_spellings.setdefault(folded, prefix)
        if previous_spelling != prefix:
            _fail(
                f"{label} contains case-insensitive path collisions: "
                f"{previous_spelling} <> {prefix}"
            )

        current_type = "dir" if index < len(parts) - 1 or is_dir else "file"
        previous_type = path_types.setdefault(folded, current_type)
        if previous_type != current_type:
            _fail(f"{label} contains a file/directory path collision: {prefix}")

    full_folded = "/".join(_portable_fold(item) for item in parts)
    if full_folded in explicit_paths:
        _fail(f"{label} contains duplicate entries: {relative_path}")
    explicit_paths[full_folded] = relative_path


def _inspect_release_tree(root: Path, *, label: str) -> list[Path]:
    """Inspect a directory without following links and return regular files."""

    if not root.is_dir():
        _fail(f"Release directory not found: {root}")
    explicit_paths: dict[str, str] = {}
    prefix_spellings: dict[str, str] = {}
    path_types: dict[str, str] = {}
    files: list[Path] = []
    try:
        for current_root, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_root)
            directory_names.sort(key=str.casefold)
            file_names.sort(key=str.casefold)
            for directory_name in directory_names:
                path = current / directory_name
                relative = path.relative_to(root).as_posix()
                if _is_reparse_or_symlink(path):
                    _fail(f"{label} contains a link or reparse point: {relative}")
                _register_case_safe_path(
                    relative,
                    is_dir=True,
                    label=label,
                    explicit_paths=explicit_paths,
                    prefix_spellings=prefix_spellings,
                    path_types=path_types,
                )
                _check_release_policy(relative, is_dir=True, label=label)
            for file_name in file_names:
                path = current / file_name
                relative = path.relative_to(root).as_posix()
                if _is_reparse_or_symlink(path):
                    _fail(f"{label} contains a link or reparse point: {relative}")
                if not path.is_file():
                    _fail(f"{label} contains a non-regular file: {relative}")
                _register_case_safe_path(
                    relative,
                    is_dir=False,
                    label=label,
                    explicit_paths=explicit_paths,
                    prefix_spellings=prefix_spellings,
                    path_types=path_types,
                )
                _check_release_policy(relative, is_dir=False, label=label)
                files.append(path)
    except SystemExit:
        raise
    except OSError as exc:
        _fail(f"{label} could not be inspected: {exc}", cause=exc)
    return files


def _verify_release_documents(root: Path) -> None:
    for required in REQUIRED_RELEASE_DOCUMENTS:
        required_path = root / required
        if not required_path.is_file() or _is_reparse_or_symlink(required_path):
            _fail(f"Required release document missing: {required}")
        try:
            if required_path.stat().st_size == 0:
                _fail(f"Required release document is empty: {required}")
        except OSError as exc:
            _fail(f"Required release document could not be inspected: {required}: {exc}", cause=exc)
    for packaged_name, source_relative in COMMITTED_RELEASE_DOCUMENT_SOURCES.items():
        packaged_path = root / packaged_name
        source_path = PROJECT_ROOT / Path(source_relative)
        try:
            packaged_bytes = packaged_path.read_bytes()
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            _fail(
                f"Release document source binding could not be read: {packaged_name}: {exc}",
                cause=exc,
            )
        if packaged_bytes != source_bytes:
            _fail(
                "Packaged release document differs from the committed source: "
                f"{packaged_name} != {source_relative}"
            )


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} could not be read: {path}: {exc}", cause=exc)
    if not isinstance(value, dict):
        _fail(f"{label} must contain a JSON object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"Release file could not be hashed: {path}: {exc}", cause=exc)
    return digest.hexdigest()


def _verify_onedir_qt_contract(root: Path, files: list[Path]) -> None:
    relative_files = {
        _portable_fold(path.relative_to(root).as_posix())
        for path in files
    }
    for runtime_path in (*REQUIRED_QT_PLUGIN_PATHS, *REQUIRED_QT_BINDING_PATHS):
        if _portable_fold(runtime_path) not in relative_files:
            _fail(f"Qt/PySide binding is missing from its required path: {runtime_path}")
    shiboken_root = root / "_internal" / "shiboken6"
    actual_shiboken_binaries = {
        path.relative_to(root).as_posix().casefold()
        for path in files
        if path.is_relative_to(shiboken_root)
        and path.suffix.casefold() in {".dll", ".pyd"}
    }
    expected_shiboken_binaries = {path.casefold() for path in REQUIRED_QT_BINDING_PATHS}
    if actual_shiboken_binaries != expected_shiboken_binaries:
        _fail(
            "shiboken6 runtime binary boundary mismatch: "
            f"missing={sorted(expected_shiboken_binaries - actual_shiboken_binaries)}, "
            f"unreviewed={sorted(actual_shiboken_binaries - expected_shiboken_binaries)}"
        )
    _verify_qt_runtime_inventory(root)


def _verify_qt_runtime_inventory(root: Path) -> None:
    manifest, raw_manifest = _load_reviewed_manifest(QT_MANIFEST_PATH, label="Qt runtime")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        _fail("Reviewed Qt runtime manifest has no artifact mapping")
    internal_root = root / "_internal"
    runtime_roots = (internal_root / "PySide6", internal_root / "shiboken6")
    actual_artifacts: set[str] = set()
    candidates = [
        path
        for runtime_root in runtime_roots
        for path in runtime_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".dll", ".pyd"}
    ]
    for artifact_path in candidates:
        relative = artifact_path.relative_to(internal_root).as_posix().casefold()
        if relative in actual_artifacts:
            _fail(f"Qt runtime contains a case-insensitive duplicate artifact: {relative}")
        actual_artifacts.add(relative)
    reviewed_artifacts = {str(path).casefold() for path in artifacts}
    if actual_artifacts != reviewed_artifacts:
        _fail(
            "Qt runtime artifact boundary mismatch: "
            f"missing={sorted(reviewed_artifacts - actual_artifacts)}, "
            f"unreviewed={sorted(actual_artifacts - reviewed_artifacts)}"
        )
    inventory = _read_json_object(root / "QT_RUNTIME_INVENTORY.json", label="Qt runtime inventory")
    if inventory.get("schema_version") != 1:
        _fail("Qt runtime inventory has an unsupported schema")
    if inventory.get("qt_version") != manifest.get("qt_version"):
        _fail("Qt runtime inventory version does not match the reviewed manifest")
    if inventory.get("pyside_distribution") != manifest.get("pyside_distribution"):
        _fail("Qt runtime inventory PySide distribution does not match the reviewed manifest")
    if inventory.get("manifest_sha256") != hashlib.sha256(raw_manifest).hexdigest():
        _fail("Qt runtime inventory manifest hash does not match the reviewed manifest")
    inventory_entries = inventory.get("artifacts")
    if not isinstance(inventory_entries, list):
        _fail("Qt runtime inventory artifacts must be a list")
    by_path: dict[str, dict[str, object]] = {}
    for entry in inventory_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            _fail("Qt runtime inventory contains an invalid artifact entry")
        path = entry["path"].casefold()
        if path in by_path:
            _fail(f"Qt runtime inventory contains a duplicate artifact: {path}")
        by_path[path] = entry
    expected_paths = reviewed_artifacts
    if set(by_path) != expected_paths:
        _fail(
            "Qt runtime inventory artifact boundary mismatch: "
            f"missing={sorted(expected_paths - set(by_path))}, "
            f"unreviewed={sorted(set(by_path) - expected_paths)}"
        )
    for path, component_ids in artifacts.items():
        normalized = str(path).casefold()
        entry = by_path[normalized]
        artifact_path = root / "_internal" / Path(normalized)
        if not artifact_path.is_file():
            _fail(f"Reviewed Qt runtime artifact is missing: _internal/{normalized}")
        if entry.get("sha256") != _file_sha256(artifact_path):
            _fail(f"Qt runtime inventory hash mismatch: _internal/{normalized}")
        if entry.get("embedded_components") != component_ids:
            _fail(f"Qt runtime inventory component mapping mismatch: {normalized}")


def _embedded_pyz_module_names(executable: Path) -> set[str]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        _fail("PyInstaller is required to inspect the embedded PYZ archive", cause=exc)
    try:
        archive = CArchiveReader(str(executable))
        pyz = archive.open_embedded_archive("PYZ.pyz")
        return set(pyz.toc)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        _fail(f"Executable PYZ archive could not be inspected: {executable}: {exc}", cause=exc)


def _verify_lgpl_runtime_contract(root: Path, files: list[Path], executable: Path) -> None:
    manifest, raw_manifest = _reviewed_lgpl_manifest()
    components = manifest["components"]
    if not isinstance(components, list):
        _fail("Reviewed LGPL runtime manifest components are invalid")
    expected_source_paths = {
        str(path).casefold()
        for component in components
        if isinstance(component, dict)
        for path in component.get("source_paths", [])
    }
    actual_source_paths = {
        path.relative_to(root).as_posix().casefold()
        for path in files
        if path.suffix.casefold() == ".py"
    }
    if actual_source_paths != expected_source_paths:
        _fail(
            "Replaceable LGPL Python source boundary mismatch: "
            f"missing={sorted(expected_source_paths - actual_source_paths)}, "
            f"unreviewed={sorted(actual_source_paths - expected_source_paths)}"
        )

    inventory = _read_json_object(
        root / "LGPL_RUNTIME_INVENTORY.json", label="LGPL runtime inventory"
    )
    if inventory.get("schema_version") != 1:
        _fail("LGPL runtime inventory has an unsupported schema")
    if inventory.get("manifest_sha256") != hashlib.sha256(raw_manifest).hexdigest():
        _fail("LGPL runtime inventory manifest hash does not match the reviewed manifest")
    inventory_components = inventory.get("components")
    if not isinstance(inventory_components, list):
        _fail("LGPL runtime inventory components must be a list")
    by_name: dict[str, dict[str, object]] = {}
    for entry in inventory_components:
        if not isinstance(entry, dict) or not isinstance(entry.get("distribution"), str):
            _fail("LGPL runtime inventory contains an invalid component")
        name = entry["distribution"].casefold()
        if name in by_name:
            _fail(f"LGPL runtime inventory contains a duplicate component: {name}")
        by_name[name] = entry
    expected_names = {
        str(component["distribution"]).casefold()
        for component in components
        if isinstance(component, dict)
    }
    if set(by_name) != expected_names:
        _fail("LGPL runtime inventory component set does not match the reviewed manifest")

    try:
        third_party_notice = (
            (root / "THIRD_PARTY_NOTICES.txt")
            .read_text(encoding="utf-8", errors="strict")
            .casefold()
            .replace("_", "-")
        )
    except (OSError, UnicodeError) as exc:
        _fail(f"THIRD_PARTY_NOTICES could not be read: {exc}", cause=exc)
    prohibited_roots: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            _fail("Reviewed LGPL runtime manifest contains an invalid component")
        name = str(component["distribution"]).casefold()
        entry = by_name[name]
        if entry.get("version") != component.get("version"):
            _fail(f"LGPL runtime inventory version mismatch for {name}")
        if entry.get("collection_mode") != "external-python-source":
            _fail(f"LGPL runtime collection mode is not external source for {name}")
        if entry.get("source_scope") != component.get("source_scope"):
            _fail(f"LGPL runtime source scope does not match the reviewed manifest for {name}")
        if f"{name.replace('_', '-')}=={component['version']}" not in third_party_notice:
            _fail(f"THIRD_PARTY_NOTICES is missing version evidence for {name}")
        license_record = entry.get("license")
        if not isinstance(license_record, dict):
            _fail(f"LGPL runtime inventory license record is invalid for {name}")
        license_path = str(component["license_output"])
        expected_license_hash = str(component["license_sha256"])
        if (
            license_record.get("path") != license_path
            or license_record.get("sha256") != expected_license_hash
        ):
            _fail(f"LGPL runtime inventory license evidence mismatch for {name}")
        packaged_license = root / Path(license_path)
        if not packaged_license.is_file() or _file_sha256(packaged_license) != expected_license_hash:
            _fail(f"LGPL runtime license evidence is missing or changed for {name}")
        if expected_license_hash not in third_party_notice:
            _fail(f"THIRD_PARTY_NOTICES is missing license hash evidence for {name}")

        source_entries = entry.get("sources")
        if not isinstance(source_entries, list):
            _fail(f"LGPL runtime inventory sources are invalid for {name}")
        source_hashes: dict[str, str] = {}
        for source_entry in source_entries:
            if not isinstance(source_entry, dict):
                _fail(f"LGPL runtime inventory source entry is invalid for {name}")
            path = source_entry.get("path")
            digest = source_entry.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                _fail(f"LGPL runtime inventory source record is invalid for {name}")
            folded_path = path.casefold()
            if folded_path in source_hashes:
                _fail(f"LGPL runtime inventory contains a duplicate source: {path}")
            source_hashes[folded_path] = digest
        expected_component_sources = {
            str(path).casefold() for path in component.get("source_paths", [])
        }
        if set(source_hashes) != expected_component_sources:
            _fail(f"LGPL runtime source inventory mismatch for {name}")
        for relative, expected_hash in source_hashes.items():
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                _fail(f"LGPL runtime source hash is invalid for {relative}")
            if _file_sha256(root / Path(relative)) != expected_hash:
                _fail(f"LGPL runtime source hash mismatch: {relative}")
        module_names = component.get("module_names")
        if not isinstance(module_names, list):
            _fail(f"Reviewed module roots are invalid for {name}")
        prohibited_roots.update(str(module) for module in module_names)

    embedded_check = inventory.get("embedded_pyz_check")
    if not isinstance(embedded_check, dict):
        _fail("LGPL runtime inventory is missing the embedded PYZ check")
    if embedded_check.get("prohibited_module_roots") != sorted(prohibited_roots):
        _fail("LGPL runtime inventory PYZ roots do not match the reviewed manifest")
    if embedded_check.get("prohibited_modules_found") != []:
        _fail("LGPL runtime inventory reports modules frozen in PYZ")
    if embedded_check.get("archive") != f"{executable.name}::PYZ.pyz":
        _fail("LGPL runtime inventory PYZ archive name does not match the executable")

    frozen_modules = _embedded_pyz_module_names(executable)
    prohibited = sorted(
        module
        for module in frozen_modules
        if any(
            module.casefold() == root_name.casefold()
            or module.casefold().startswith(f"{root_name.casefold()}.")
            for root_name in prohibited_roots
        )
    )
    if prohibited:
        _fail("Replaceable LGPL Python modules are still frozen in PYZ: " + ", ".join(prohibited))


def _pe_metadata(path: Path) -> tuple[dict[str, str], int | None]:
    try:
        import pefile
    except ImportError as exc:
        _fail("PE metadata verification requires the pefile package", cause=exc)

    values: dict[str, str] = {}
    pe = None
    machine: int | None = None
    try:
        pe = pefile.PE(str(path), fast_load=False)
        machine = getattr(getattr(pe, "FILE_HEADER", None), "Machine", None)
        for file_info_group in getattr(pe, "FileInfo", []) or []:
            groups = file_info_group if isinstance(file_info_group, list) else [file_info_group]
            for file_info in groups:
                for table in getattr(file_info, "StringTable", []) or []:
                    for raw_key, raw_value in table.entries.items():
                        key = (
                            raw_key.decode("utf-8", errors="replace")
                            if isinstance(raw_key, bytes)
                            else str(raw_key)
                        )
                        value = (
                            raw_value.decode("utf-8", errors="replace")
                            if isinstance(raw_value, bytes)
                            else str(raw_value)
                        )
                        values[key] = value
    except pefile.PEFormatError as exc:
        _fail(f"Executable is not a valid PE file: {path.name}", cause=exc)
    except OSError as exc:
        _fail(f"Executable PE metadata could not be read: {path.name}: {exc}", cause=exc)
    finally:
        if pe is not None:
            pe.close()
    return values, machine


def _expected_pe_version(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?", version.strip())
    if not match:
        _fail(f"Expected version is not SemVer-compatible: {version!r}")
    return ".".join((*match.groups(), "0"))


def _verify_pe_metadata(executable: Path, name: str, expected_version: str | None) -> None:
    values, machine = _pe_metadata(executable)
    if machine != 0x8664:
        _fail(
            f"Executable is not AMD64 PE for the windows-x64 package: "
            f"{executable.name} machine={machine!r}"
        )
    required = {
        "ProductName": "Aruba Mini Dashboard",
        "FileDescription": "Aruba MM and WLC Mini Dashboard",
        "OriginalFilename": f"{name}.exe",
    }
    for key, expected in required.items():
        if values.get(key) != expected:
            _fail(
                f"PE metadata mismatch for {executable.name}: "
                f"{key}={values.get(key)!r}, expected {expected!r}"
            )

    file_version = values.get("FileVersion", "")
    product_version = values.get("ProductVersion", "")
    if expected_version is None:
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", file_version):
            _fail(f"PE FileVersion is missing or invalid for {executable.name}")
        if product_version != file_version:
            _fail(f"PE ProductVersion does not match FileVersion for {executable.name}")
        return

    expected_pe_version = _expected_pe_version(expected_version)
    if file_version != expected_pe_version or product_version != expected_pe_version:
        _fail(
            f"PE version mismatch for {executable.name}: "
            f"FileVersion={file_version!r} ProductVersion={product_version!r} "
            f"expected={expected_pe_version!r}"
        )


def _verify_release_directory(
    root: Path,
    name: str,
    one_file: bool,
    *,
    expected_version: str | None,
) -> Path:
    if one_file:
        _fail(
            "One-file output is not a supported release: LGPL runtime replacement "
            "requires the persistent onedir _internal tree"
        )
    files = _inspect_release_tree(root, label="Release directory")
    executable = root / f"{name}.exe"
    if not executable.is_file() or _is_reparse_or_symlink(executable):
        _fail(f"Executable missing: {executable}")
    _verify_release_documents(root)
    _verify_onedir_qt_contract(root, files)
    _verify_lgpl_runtime_contract(root, files, executable)
    _verify_pe_metadata(executable, name, expected_version)
    return executable


def _safe_zip_parts(raw_name: str) -> tuple[str, ...]:
    if not raw_name or "\x00" in raw_name:
        _fail(f"Release ZIP contains an empty or NUL path: {raw_name!r}")
    if "\\" in raw_name:
        _fail(f"Release ZIP contains a backslash path: {raw_name}")
    if raw_name.startswith(("/", "\\")):
        _fail(f"Release ZIP contains an absolute path: {raw_name}")
    stripped = raw_name.rstrip("/")
    if not stripped:
        _fail(f"Release ZIP contains an empty root entry: {raw_name!r}")
    parts = tuple(stripped.split("/"))
    for part in parts:
        _validate_windows_component(part, label=f"Release ZIP entry {raw_name!r}")
    return parts


def _verify_zip_entry_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        _fail(f"Release ZIP contains an encrypted entry: {info.filename}")
    if info.file_size < 0 or info.compress_size < 0:
        _fail(f"Release ZIP contains an invalid entry size: {info.filename}")
    if info.file_size > MAX_ZIP_MEMBER_SIZE:
        _fail(f"Release ZIP entry is unreasonably large: {info.filename}")
    if info.create_system == 3:
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            _fail(f"Release ZIP contains a link or special file: {info.filename}")


def _inspect_open_zip(archive: zipfile.ZipFile, *, expected_root: str) -> str:
    infos = archive.infolist()
    if not infos:
        _fail("Release ZIP is empty")
    if len(infos) > MAX_ZIP_ENTRIES:
        _fail(f"Release ZIP contains too many entries: {len(infos)}")

    explicit_paths: dict[str, str] = {}
    prefix_spellings: dict[str, str] = {}
    path_types: dict[str, str] = {}
    roots: set[str] = set()
    total_size = 0
    has_nested_entry = False
    for info in infos:
        _verify_zip_entry_type(info)
        parts = _safe_zip_parts(info.filename)
        relative = "/".join(parts)
        is_dir = info.is_dir() or info.filename.endswith("/")
        roots.add(parts[0])
        has_nested_entry = has_nested_entry or len(parts) > 1
        total_size += info.file_size
        if total_size > MAX_ZIP_TOTAL_SIZE:
            _fail("Release ZIP expands beyond the permitted size")
        _register_case_safe_path(
            relative,
            is_dir=is_dir,
            label="Release ZIP",
            explicit_paths=explicit_paths,
            prefix_spellings=prefix_spellings,
            path_types=path_types,
        )
        _check_release_policy(
            relative,
            is_dir=is_dir,
            label="Release ZIP",
            archive_member=True,
        )

    if len(roots) != 1 or not has_nested_entry:
        _fail(
            "Release ZIP must contain exactly one top-level directory; found: "
            + ", ".join(sorted(roots))
        )
    root = next(iter(roots))
    if root != expected_root:
        _fail(f"Release ZIP top-level directory must be {expected_root!r}, found {root!r}")

    corrupt_member = archive.testzip()
    if corrupt_member is not None:
        _fail(f"Release ZIP contains a corrupt entry: {corrupt_member}")
    return root


def _extract_open_zip_safely(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract an already-inspected archive without trusting extractall()."""

    for info in archive.infolist():
        parts = _safe_zip_parts(info.filename)
        target = destination.joinpath(*parts)
        if info.is_dir() or info.filename.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        except FileExistsError as exc:
            _fail(f"Release ZIP extraction encountered a duplicate path: {info.filename}", cause=exc)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            _fail(f"Release ZIP entry could not be extracted: {info.filename}: {exc}", cause=exc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"Release checksum could not be read: {path}: {exc}", cause=exc)
    return digest.hexdigest()


def _normalize_expected_sha256(value: str) -> str:
    expected = value.strip()
    if expected.casefold().startswith("sha256:"):
        expected = expected.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", expected):
        _fail("Expected SHA-256 must contain exactly 64 hexadecimal characters")
    return expected.casefold()


def _read_checksum_file(checksum_file: Path, artifact: Path) -> str:
    try:
        text = checksum_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        _fail(f"Checksum file could not be read as UTF-8: {checksum_file}: {exc}", cause=exc)
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        _fail("Checksum file must contain exactly one non-comment checksum line")
    line = lines[0]
    raw_only = re.fullmatch(r"[0-9A-Fa-f]{64}", line)
    if raw_only:
        return _normalize_expected_sha256(line)
    match = re.fullmatch(r"([0-9A-Fa-f]{64})\s+\*?(.+?)", line)
    if match is None:
        _fail("Checksum file does not contain a valid SHA-256 line")
    listed_name = match.group(2).strip()
    if listed_name != artifact.name:
        _fail(
            f"Checksum filename mismatch: expected {artifact.name!r}, found {listed_name!r}"
        )
    return _normalize_expected_sha256(match.group(1))


def _verify_hash(
    artifact: Path,
    *,
    expected_sha256: str | None,
    checksum_file: Path | None,
) -> None:
    if expected_sha256 is not None and checksum_file is not None:
        _fail("Use only one of expected_sha256 or checksum_file")
    if expected_sha256 is None and checksum_file is None:
        return
    expected = (
        _normalize_expected_sha256(expected_sha256)
        if expected_sha256 is not None
        else _read_checksum_file(checksum_file, artifact)  # type: ignore[arg-type]
    )
    actual = _sha256(artifact)
    if actual != expected:
        _fail(f"Release SHA-256 mismatch: expected {expected}, actual {actual}")


def _smoke_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith("PYTHON") or key.upper() in {"VIRTUAL_ENV", "CONDA_PREFIX"}:
            env.pop(key, None)
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if "python" not in entry.casefold() and ".venv" not in entry.casefold()
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _run_executable_smoke(executable: Path, *, timeout: int = 30) -> None:
    required_markers = set(REQUIRED_SMOKE_MARKERS)
    if os.name == "nt":
        required_markers.add("WIN32CRED_OK")
    env = _smoke_environment()
    try:
        with tempfile.TemporaryDirectory(prefix="ArubaMiniDashboard-smoke-") as temp_dir:
            env["ARUBA_MINI_DASHBOARD_DATA_DIR"] = temp_dir
            sentinel = Path(temp_dir) / "smoke-ok.txt"
            completed = subprocess.run(
                [str(executable), "--smoke", "--smoke-output", str(sentinel)],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            sentinel_text = sentinel.read_text(encoding="utf-8") if sentinel.is_file() else ""
    except subprocess.TimeoutExpired as exc:
        _fail(f"Executable smoke timed out after {timeout} seconds", cause=exc)
    except (OSError, UnicodeError) as exc:
        _fail(f"Executable smoke could not complete: {exc}", cause=exc)

    present_markers = {line.strip() for line in sentinel_text.splitlines() if line.strip()}
    missing_markers = sorted(required_markers - present_markers)
    if completed.returncode != 0 or missing_markers:
        _fail(
            "Executable smoke failed "
            f"rc={completed.returncode} missing_markers={missing_markers!r} "
            f"sentinel={sentinel_text!r}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    if os.name == "nt":
        _run_windows_ui_smoke(executable, timeout=timeout)


def _run_windows_ui_smoke(executable: Path, *, timeout: int = 30) -> None:
    env = _smoke_environment()
    for key in list(env):
        if key.upper().startswith("QT_"):
            env.pop(key, None)
    try:
        with tempfile.TemporaryDirectory(prefix="ArubaMiniDashboard-ui-smoke-") as temp_dir:
            env["ARUBA_MINI_DASHBOARD_DATA_DIR"] = temp_dir
            sentinel = Path(temp_dir) / "ui-smoke-ok.txt"
            completed = subprocess.run(
                [str(executable), "--ui-smoke", "--smoke-output", str(sentinel)],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            sentinel_text = sentinel.read_text(encoding="utf-8") if sentinel.is_file() else ""
    except subprocess.TimeoutExpired as exc:
        _fail(f"Windows Qt UI smoke timed out after {timeout} seconds", cause=exc)
    except (OSError, UnicodeError) as exc:
        _fail(f"Windows Qt UI smoke could not complete: {exc}", cause=exc)

    if completed.returncode != 0 or "WINDOWS_QT_UI_OK" not in sentinel_text.splitlines():
        _fail(
            "Windows Qt UI smoke failed "
            f"rc={completed.returncode} sentinel={sentinel_text!r}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )


def _verify_zip(
    zip_path: Path,
    name: str,
    one_file: bool,
    *,
    expected_sha256: str | None,
    checksum_file: Path | None,
    smoke_timeout: int,
    expected_version: str | None,
) -> None:
    _verify_hash(
        zip_path,
        expected_sha256=expected_sha256,
        checksum_file=checksum_file,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="ArubaMiniDashboard-package-") as temp_dir:
            destination = Path(temp_dir)
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    root_name = _inspect_open_zip(archive, expected_root=name)
                    _extract_open_zip_safely(archive, destination)
            except zipfile.BadZipFile as exc:
                _fail(f"Release ZIP is not a valid ZIP file: {zip_path}", cause=exc)
            except OSError as exc:
                _fail(f"Release ZIP could not be read: {zip_path}: {exc}", cause=exc)

            extracted_root = destination / root_name
            executable = _verify_release_directory(
                extracted_root,
                name,
                one_file,
                expected_version=expected_version,
            )
            _run_executable_smoke(executable, timeout=smoke_timeout)
    except SystemExit:
        raise
    except OSError as exc:
        _fail(f"Release ZIP verification workspace failed: {exc}", cause=exc)


def verify(
    path: Path,
    name: str,
    one_file: bool,
    *,
    expected_sha256: str | None = None,
    checksum_file: Path | None = None,
    smoke_timeout: int = 30,
    force_zip: bool = False,
    expected_version: str | None = None,
) -> None:
    """Verify a folder, a standalone executable, or a release ZIP."""

    if one_file:
        _fail(
            "One-file output is not a supported release: LGPL runtime replacement "
            "requires the persistent onedir _internal tree"
        )
    if smoke_timeout <= 0:
        _fail("Smoke timeout must be greater than zero")
    try:
        exists = path.exists()
    except OSError as exc:
        _fail(f"Release path is not accessible: {path}: {exc}", cause=exc)
    if not exists:
        _fail(f"Release path not found: {path}")

    is_zip = force_zip or (path.is_file() and path.suffix.casefold() == ".zip")
    if is_zip:
        if not path.is_file():
            _fail(f"Release ZIP is not a file: {path}")
        _verify_zip(
            path,
            name,
            one_file,
            expected_sha256=expected_sha256,
            checksum_file=checksum_file,
            smoke_timeout=smoke_timeout,
            expected_version=expected_version,
        )
    elif path.is_dir():
        executable = _verify_release_directory(
            path,
            name,
            one_file,
            expected_version=expected_version,
        )
        _verify_hash(
            executable,
            expected_sha256=expected_sha256,
            checksum_file=checksum_file,
        )
        _run_executable_smoke(executable, timeout=smoke_timeout)
    else:
        if not path.is_file():
            _fail(f"Release executable is not a regular file: {path}")
        _fail(
            "A standalone executable is not a complete release; verify the onedir "
            "folder or its single-root ZIP with required sources, inventories, and notices"
        )
    print("ARUBA_MINI_DASHBOARD_PACKAGE_OK")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an Aruba Mini Dashboard Windows onedir folder or single-root ZIP."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--path",
        type=Path,
        help="onedir release directory or release ZIP",
    )
    source.add_argument("--zip", dest="zip_path", type=Path, help="release ZIP path")
    parser.add_argument("--name", required=True, help="expected executable and ZIP root name")
    parser.add_argument(
        "--one-file",
        action="store_true",
        help="legacy flag; one-file releases are rejected",
    )
    checksum = parser.add_mutually_exclusive_group()
    checksum.add_argument("--expected-sha256", help="expected SHA-256 for the ZIP or executable")
    checksum.add_argument("--sha256-file", type=Path, help="standard SHA-256 sidecar file")
    parser.add_argument("--smoke-timeout", type=_positive_int, default=30, help="smoke timeout in seconds")
    parser.add_argument(
        "--expected-version",
        help="expected semantic version encoded in the Windows PE metadata",
    )
    args = parser.parse_args(argv)

    release_path = args.zip_path or args.path
    verify(
        release_path.resolve(),
        args.name,
        args.one_file,
        expected_sha256=args.expected_sha256,
        checksum_file=args.sha256_file.resolve() if args.sha256_file else None,
        smoke_timeout=args.smoke_timeout,
        force_zip=args.zip_path is not None,
        expected_version=args.expected_version,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
