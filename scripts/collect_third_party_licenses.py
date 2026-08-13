from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import ssl
import sys
import tomllib
from typing import Iterable, Sequence

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


PROJECT_DISTRIBUTION = canonicalize_name("aruba-mini-dashboard")
PACKAGING_RUNTIME_DISTRIBUTIONS = ("pyinstaller", "pyinstaller-hooks-contrib")
LICENSE_NAME_PREFIXES = ("license", "licence", "copying", "notice", "copyright")
LOCK_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)")


class LicenseCollectionError(RuntimeError):
    """Raised when locked or installed license evidence is incomplete."""


@dataclass(frozen=True)
class LicenseDocument:
    title: str
    source: str
    sha256: str
    text: str


@dataclass(frozen=True)
class RuntimeComponent:
    canonical_name: str
    name: str
    version: str
    license_declaration: str
    project_url: str
    required_by: tuple[str, ...]
    direct: bool
    documents: tuple[LicenseDocument, ...]


@dataclass(frozen=True)
class SupplementalLicense:
    filename: str
    expected_sha256: str
    applies_to: tuple[str, ...]
    source: str


SUPPLEMENTAL_LICENSES = (
    SupplementalLicense(
        filename="PySide6-LGPL-3.0-only.txt",
        expected_sha256="da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
        applies_to=("pyside6-essentials", "shiboken6"),
        source=(
            "Qt for Python source tag v6.11.0, commit "
            "04cd59c10681242e387d125cfe5269902962ded1, "
            "LICENSES/LGPL-3.0-only.txt"
        ),
    ),
    SupplementalLicense(
        filename="PySide6-GPL-2.0-only.txt",
        expected_sha256="8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
        applies_to=("pyside6-essentials", "shiboken6"),
        source=(
            "Qt for Python source tag v6.11.0, commit "
            "04cd59c10681242e387d125cfe5269902962ded1, "
            "LICENSES/GPL-2.0-only.txt"
        ),
    ),
    SupplementalLicense(
        filename="PySide6-GPL-3.0-only.txt",
        expected_sha256="8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
        applies_to=("pyside6-essentials", "shiboken6"),
        source=(
            "Qt for Python source tag v6.11.0, commit "
            "04cd59c10681242e387d125cfe5269902962ded1, "
            "LICENSES/GPL-3.0-only.txt"
        ),
    ),
    SupplementalLicense(
        filename="pyserial-3.5-LICENSE.txt",
        expected_sha256="f91cb9813de6a5b142b8f7f2dede630b5134160aedaeaf55f4d6a7e2593ca3f3",
        applies_to=("pyserial",),
        source=(
            "PyPI pyserial 3.5 source distribution (sha256 "
            "3c77e014170dfffbd816e6ffc205e9842efb10be9f58ec16d3e8675b4925cddb), "
            "LICENSE.txt"
        ),
    ),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text_bytes(data: bytes, source: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
        except UnicodeDecodeError:
            continue
    raise LicenseCollectionError(f"License text is not decodable: {source}")


def _target_environment() -> dict[str, str]:
    environment = default_environment()
    environment.update(
        {
            "extra": "",
            "os_name": "nt",
            "platform_system": "Windows",
            "sys_platform": "win32",
            "python_version": "3.13",
            "python_full_version": platform.python_version(),
        }
    )
    return environment


def load_project_runtime_requirements(project_root: Path) -> tuple[Requirement, ...]:
    pyproject = project_root / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise LicenseCollectionError(f"Cannot read project runtime dependencies: {pyproject}: {exc}") from exc

    environment = _target_environment()
    requirements: list[Requirement] = []
    for raw in project.get("dependencies", ()):
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate(environment=environment):
            requirements.append(requirement)
    if not requirements:
        raise LicenseCollectionError("No Windows runtime dependencies were declared in pyproject.toml")
    return tuple(requirements)


def load_locked_versions(project_root: Path) -> dict[str, str]:
    lock_file = project_root / "requirements-lock.txt"
    try:
        lines = lock_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LicenseCollectionError(f"Cannot read dependency lock: {lock_file}: {exc}") from exc
    versions: dict[str, str] = {}
    for line in lines:
        match = LOCK_ENTRY.match(line)
        if match:
            versions[canonicalize_name(match.group(1))] = match.group(2)
    if not versions:
        raise LicenseCollectionError(f"No pinned packages found in {lock_file}")
    return versions


def _is_license_path(value: str) -> bool:
    name = PurePosixPath(value.replace("\\", "/")).name.casefold()
    if name.startswith("licenseref-"):
        return True
    return any(
        name == prefix
        or name.startswith(prefix + ".")
        or name.startswith(prefix + "-")
        or name.startswith(prefix + "_")
        for prefix in LICENSE_NAME_PREFIXES
    )


def _distribution_license_documents(distribution: metadata.Distribution) -> tuple[LicenseDocument, ...]:
    documents: list[LicenseDocument] = []
    seen_hashes: set[str] = set()
    for relative in sorted(distribution.files or (), key=lambda item: str(item).casefold()):
        source = str(relative).replace("\\", "/")
        if not _is_license_path(source):
            continue
        path = Path(distribution.locate_file(relative))
        if not path.is_file():
            continue
        data = path.read_bytes()
        digest = _sha256(data)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        documents.append(
            LicenseDocument(
                title=f"{distribution.metadata.get('Name', distribution.name)}: {PurePosixPath(source).name}",
                source=f"installed distribution file: {source}",
                sha256=digest,
                text=_read_text_bytes(data, source),
            )
        )
    return tuple(documents)


def _license_declaration(distribution: metadata.Distribution) -> str:
    value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
    if value and value.strip() and value.strip().casefold() != "unknown":
        return " ".join(value.split())
    classifiers = distribution.metadata.get_all("Classifier") or ()
    licenses = [item.partition("License ::")[2].strip() for item in classifiers if "License ::" in item]
    if licenses:
        return "; ".join(licenses) + " (metadata classifier)"
    raise LicenseCollectionError(
        f"Installed distribution has no usable license declaration: {distribution.metadata.get('Name')}"
    )


def _project_url(distribution: metadata.Distribution) -> str:
    preferred = ("homepage", "repository", "source", "documentation", "docs")
    parsed: list[tuple[str, str]] = []
    for value in distribution.metadata.get_all("Project-URL") or ():
        label, separator, url = value.partition(",")
        if separator and url.strip():
            parsed.append((label.strip().casefold(), url.strip()))
    for wanted in preferred:
        for label, url in parsed:
            if label == wanted:
                return url
    home = distribution.metadata.get("Home-page")
    if home and home.strip() and home.strip().casefold() != "unknown":
        return home.strip()
    if parsed:
        return parsed[0][1]
    return "not declared in installed metadata"


def _iter_active_dependencies(distribution: metadata.Distribution) -> Iterable[Requirement]:
    environment = _target_environment()
    for raw in distribution.requires or ():
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate(environment=environment):
            yield requirement


def resolve_runtime_components(project_root: Path) -> tuple[RuntimeComponent, ...]:
    root_requirements = load_project_runtime_requirements(project_root)
    locked = load_locked_versions(project_root)
    direct_names = {canonicalize_name(requirement.name) for requirement in root_requirements}
    pending = list(root_requirements)
    distributions: dict[str, metadata.Distribution] = {}
    parents: dict[str, set[str]] = {name: set() for name in direct_names}

    while pending:
        requirement = pending.pop()
        canonical_name = canonicalize_name(requirement.name)
        if canonical_name == PROJECT_DISTRIBUTION or canonical_name in distributions:
            continue
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError as exc:
            raise LicenseCollectionError(f"Locked runtime dependency is not installed: {requirement.name}") from exc
        installed_name = canonicalize_name(distribution.metadata.get("Name", distribution.name))
        if installed_name != canonical_name:
            raise LicenseCollectionError(
                f"Distribution name mismatch: required {canonical_name}, installed metadata reports {installed_name}"
            )
        locked_version = locked.get(canonical_name)
        if locked_version is None:
            raise LicenseCollectionError(f"Runtime dependency is not pinned in requirements-lock.txt: {canonical_name}")
        if distribution.version != locked_version:
            raise LicenseCollectionError(
                f"Installed/locked version mismatch for {canonical_name}: "
                f"installed={distribution.version}, locked={locked_version}"
            )
        if requirement.specifier and distribution.version not in requirement.specifier:
            raise LicenseCollectionError(
                f"Installed version does not satisfy runtime requirement {requirement}: {distribution.version}"
            )
        distributions[canonical_name] = distribution
        for dependency in _iter_active_dependencies(distribution):
            dependency_name = canonicalize_name(dependency.name)
            parents.setdefault(dependency_name, set()).add(canonical_name)
            pending.append(dependency)

    components: list[RuntimeComponent] = []
    for canonical_name, distribution in distributions.items():
        component = RuntimeComponent(
            canonical_name=canonical_name,
            name=distribution.metadata.get("Name", distribution.name),
            version=distribution.version,
            license_declaration=_license_declaration(distribution),
            project_url=_project_url(distribution),
            required_by=tuple(sorted(parents.get(canonical_name, ()))),
            direct=canonical_name in direct_names,
            documents=_distribution_license_documents(distribution),
        )
        components.append(component)
    return tuple(sorted(components, key=lambda item: item.canonical_name))


def load_supplemental_documents(project_root: Path) -> dict[str, tuple[LicenseDocument, ...]]:
    source_root = project_root / "scripts" / "license_texts"
    by_component: dict[str, list[LicenseDocument]] = {}
    for entry in SUPPLEMENTAL_LICENSES:
        path = source_root / entry.filename
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LicenseCollectionError(f"Supplemental license text is missing: {path}: {exc}") from exc
        text = _read_text_bytes(data, str(path))
        digest = _sha256(text.encode("utf-8"))
        if digest != entry.expected_sha256:
            raise LicenseCollectionError(
                f"Supplemental license hash mismatch for {path}: expected={entry.expected_sha256}, actual={digest}"
            )
        document = LicenseDocument(
            title=entry.filename,
            source=entry.source,
            sha256=digest,
            text=text,
        )
        for component in entry.applies_to:
            by_component.setdefault(canonicalize_name(component), []).append(document)
    return {name: tuple(documents) for name, documents in by_component.items()}


def _load_python_license() -> LicenseDocument:
    license_path = Path(sys.base_prefix) / "LICENSE.txt"
    try:
        data = license_path.read_bytes()
    except OSError as exc:
        raise LicenseCollectionError(f"CPython runtime license is missing: {license_path}: {exc}") from exc
    return LicenseDocument(
        title=f"CPython {platform.python_version()} LICENSE.txt",
        source=f"installed CPython runtime file: {license_path.name}",
        sha256=_sha256(data),
        text=_read_text_bytes(data, str(license_path)),
    )


def _load_packaging_runtime_documents(
    project_root: Path,
) -> tuple[tuple[str, str, str, str, tuple[LicenseDocument, ...]], ...]:
    locked = load_locked_versions(project_root)
    results: list[tuple[str, str, str, str, tuple[LicenseDocument, ...]]] = []
    for name in PACKAGING_RUNTIME_DISTRIBUTIONS:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise LicenseCollectionError(f"Packaging runtime distribution is not installed: {name}") from exc
        canonical_name = canonicalize_name(name)
        locked_version = locked.get(canonical_name)
        if locked_version is None:
            raise LicenseCollectionError(
                f"Packaging runtime distribution is not pinned in requirements-lock.txt: {name}"
            )
        if distribution.version != locked_version:
            raise LicenseCollectionError(
                f"Installed/locked version mismatch for packaging runtime {name}: "
                f"installed={distribution.version}, locked={locked_version}"
            )
        documents = _distribution_license_documents(distribution)
        if not documents:
            raise LicenseCollectionError(f"Packaging runtime license file is missing: {name}")
        results.append(
            (
                distribution.metadata.get("Name", distribution.name),
                distribution.version,
                _license_declaration(distribution),
                _project_url(distribution),
                documents,
            )
        )
    return tuple(results)


def _openssl_components() -> tuple[tuple[str, str], ...]:
    dynamic_version = ssl.OPENSSL_VERSION.removeprefix("OpenSSL ").split()[0]
    components: list[tuple[str, str]] = [
        ("OpenSSL used by the embedded CPython ssl runtime", dynamic_version)
    ]
    try:
        cryptography_distribution = metadata.distribution("cryptography")
    except metadata.PackageNotFoundError as exc:
        raise LicenseCollectionError("cryptography is required to identify its embedded OpenSSL") from exc
    sbom_path = None
    for relative in cryptography_distribution.files or ():
        value = str(relative).replace("\\", "/")
        if value.endswith(".dist-info/sboms/sbom.json"):
            sbom_path = Path(cryptography_distribution.locate_file(relative))
            break
    if sbom_path is None or not sbom_path.is_file():
        raise LicenseCollectionError("cryptography OpenSSL SBOM is missing from the installed distribution")
    try:
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        openssl = next(
            component for component in sbom["components"]
            if str(component.get("name", "")).casefold() == "openssl"
        )
        embedded_version = str(openssl["version"])
    except (OSError, KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LicenseCollectionError(f"Cannot parse cryptography OpenSSL SBOM: {sbom_path}: {exc}") from exc
    components.append(("OpenSSL statically embedded in cryptography", embedded_version))
    return tuple(components)


def _section(title: str) -> list[str]:
    return [title, "=" * len(title), ""]


def _append_document(lines: list[str], document: LicenseDocument) -> None:
    lines.extend(
        [
            "-" * 78,
            document.title,
            f"Source: {document.source}",
            f"SHA-256: {document.sha256}",
            "-" * 78,
            document.text.rstrip(),
            "",
        ]
    )


def build_notice(project_root: Path) -> str:
    components = resolve_runtime_components(project_root)
    supplemental = load_supplemental_documents(project_root)
    component_names = {component.canonical_name for component in components}
    stale_supplements = sorted(set(supplemental) - component_names)
    if stale_supplements:
        raise LicenseCollectionError(
            "Supplemental license mapping does not match the locked runtime closure: "
            + ", ".join(stale_supplements)
        )
    for component in components:
        if not component.documents and component.canonical_name not in supplemental:
            raise LicenseCollectionError(
                f"No installed or pinned supplemental license text found for {component.name}=={component.version}"
            )

    lines: list[str] = []
    lines.extend(_section("ARUBA MINI DASHBOARD - THIRD-PARTY NOTICES"))
    lines.extend(
        [
            "This file describes third-party software redistributed with the Windows build.",
            "It is generated from pyproject.toml runtime dependencies, requirements-lock.txt",
            "pins, installed distribution metadata, installed LICENSE/COPYING/NOTICE files,",
            "and the pinned supplemental source texts identified below.",
            "",
            "This notice does NOT grant or select a license for Aruba Mini Dashboard itself.",
            "The project's own license must be decided separately by its copyright holder.",
            "Third-party terms remain the terms of their respective copyright holders.",
            "This inventory is compliance evidence, not legal advice.",
            "",
            "Target runtime: Windows x64, CPython 3.13.15 (standard GIL build)",
            "Dependency authority: pyproject.toml + requirements-lock.txt",
            "",
        ]
    )

    lines.extend(_section("RUNTIME COMPONENT INVENTORY"))
    for component in components:
        relationship = "direct project dependency" if component.direct else (
            "required by " + ", ".join(component.required_by)
        )
        document_hashes = [document.sha256 for document in component.documents]
        document_hashes.extend(document.sha256 for document in supplemental.get(component.canonical_name, ()))
        lines.extend(
            [
                f"{component.name}=={component.version}",
                f"  Relationship: {relationship}",
                f"  Declared license: {component.license_declaration}",
                f"  Project URL: {component.project_url}",
                f"  License evidence SHA-256: {', '.join(document_hashes)}",
                "",
            ]
        )

    lines.extend(_section("PACKAGED LANGUAGE, CRYPTOGRAPHY, AND BOOTLOADER RUNTIMES"))
    python_document = _load_python_license()
    lines.extend(
        [
            f"CPython=={platform.python_version()}",
            "  Relationship: Python interpreter and standard library embedded by PyInstaller",
            "  Declared license: Python Software Foundation License Version 2 and the",
            "  additional third-party terms reproduced in the installed CPython LICENSE.txt",
            f"  License evidence SHA-256: {python_document.sha256}",
            "",
        ]
    )
    cryptography_component = next(
        (component for component in components if component.canonical_name == "cryptography"),
        None,
    )
    openssl_license = next(
        (
            document for document in (cryptography_component.documents if cryptography_component else ())
            if document.title.casefold().endswith("license.apache")
        ),
        None,
    )
    if openssl_license is None:
        raise LicenseCollectionError("cryptography LICENSE.APACHE is required for OpenSSL notice evidence")
    for title, version in _openssl_components():
        lines.extend(
            [
                f"{title}=={version}",
                "  Relationship: cryptographic runtime redistributed in the frozen build",
                "  Declared license: Apache License 2.0 (OpenSSL 3.0 and later)",
                "  License text: cryptography LICENSE.APACHE reproduced below",
                f"  License evidence SHA-256: {openssl_license.sha256}",
                "  Project URL: https://www.openssl.org/",
                "",
            ]
        )

    packaging_runtime = _load_packaging_runtime_documents(project_root)
    for name, version, license_declaration, project_url, documents in packaging_runtime:
        lines.extend(
            [
                f"{name}=={version}",
                "  Relationship: bootloader and/or runtime hooks embedded by the packager",
                f"  Declared license: {license_declaration}",
                f"  Project URL: {project_url}",
                f"  License evidence SHA-256: {', '.join(document.sha256 for document in documents)}",
                "",
            ]
        )

    lines.extend(_section("QT / PYSIDE DISTRIBUTION NOTE"))
    lines.extend(
        [
            "PySide6-Essentials and shiboken6 metadata declare the alternatives",
            "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only and include a commercial-license",
            "reference. The corresponding open-source license texts from the official Qt for",
            "Python v6.11.0 source tag are reproduced here without selecting a license for the",
            "application. The reviewed module-specific and embedded third-party terms are",
            "recorded in QT_THIRD_PARTY_NOTICES.txt. The build gate validates the actual",
            "DLL/plugin set and hashes in QT_RUNTIME_INVENTORY.json for every onedir release.",
            "Official Qt licensing: https://doc.qt.io/qt-6/licensing.html",
            "Qt third-party code: https://doc.qt.io/qt-6/licenses-used-in-qt.html",
            "",
            "Do not distribute GPL-only Qt modules (including Qt Virtual Keyboard) unless the",
            "chosen project/distribution terms satisfy that module's license or a valid Qt",
            "commercial license applies.",
            "",
        ]
    )

    lines.extend(_section("LICENSE TEXTS"))
    seen_documents: set[str] = set()
    for component in components:
        for document in (*component.documents, *supplemental.get(component.canonical_name, ())):
            if document.sha256 in seen_documents:
                continue
            seen_documents.add(document.sha256)
            _append_document(lines, document)

    if python_document.sha256 not in seen_documents:
        seen_documents.add(python_document.sha256)
        _append_document(lines, python_document)

    for _name, _version, _license, _url, documents in packaging_runtime:
        for document in documents:
            if document.sha256 in seen_documents:
                continue
            seen_documents.add(document.sha256)
            _append_document(lines, document)

    return "\n".join(lines).rstrip() + "\n"


def check_notice(output: Path, expected: str) -> None:
    try:
        actual = output.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        raise LicenseCollectionError(f"Third-party notice is missing: {output}: {exc}") from exc
    if actual != expected:
        raise LicenseCollectionError(
            f"Third-party notice is stale: {output}. "
            "Run scripts/collect_third_party_licenses.py and review the diff."
        )


def main(argv: Sequence[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Generate or verify deterministic third-party notices for the Windows runtime."
    )
    parser.add_argument("--project-root", type=Path, default=default_root)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="fail if the committed notice is stale")
    arguments = parser.parse_args(argv)
    project_root = arguments.project_root.resolve()
    output = arguments.output or project_root / "docs" / "THIRD_PARTY_NOTICES.txt"
    try:
        expected = build_notice(project_root)
        if arguments.check:
            check_notice(output, expected)
            print(f"THIRD_PARTY_NOTICES_OK: {output}")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(expected, encoding="utf-8", newline="\n")
            print(f"Wrote {output}")
    except LicenseCollectionError as exc:
        print(f"THIRD_PARTY_NOTICES_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
