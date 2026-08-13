from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_project_and_lock_target_python_313_only() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    lock = _read("requirements-lock.txt")

    assert project["requires-python"] == ">=3.13,<3.14"
    assert "pip-compile with Python 3.13" in lock


def test_windows_build_requires_exact_standard_gil_x64_cpython() -> None:
    build = _read("scripts/build.ps1")

    assert "& $PythonLauncher -3.13 -m venv $venv" in build
    assert '$actual.Trim() -ne "3.13.15"' in build
    assert '$implementation.Trim() -ne "cpython"' in build
    assert '$architecture.Trim() -ne "64:AMD64"' in build
    assert '$gilContract.Trim() -ne "0:1"' in build
    assert build.index('$gilContract.Trim() -ne "0:1"') < build.index(
        '"scripts\\collect_third_party_licenses.py"'
    )


def test_release_bootstrap_reuses_only_an_exact_build_runtime() -> None:
    package = _read("scripts/package_release.ps1")

    exact_contract = "cpython|3.13.15|64|AMD64|0|1"
    assert "Test-ExactBuildPython -Candidate $venvPython" in package
    assert exact_contract in package
    assert 'Get-Command "py"' in package
    assert (
        'Test-ExactBuildPython -Candidate $pythonLauncher.Source '
        '-InterpreterArguments @("-3.13")'
    ) in package
    assert "CPython 3.13.15 x64 (standard GIL build)" in package


def test_release_verifier_independently_checks_the_embedded_python_runtime() -> None:
    verifier = _read("scripts/verify_release_package.py")

    assert 'EXPECTED_PYTHON_RUNTIME_VERSION = "3.13.15"' in verifier
    assert '"_internal/python313.dll"' in verifier
    assert '"_internal/python3.dll"' in verifier
    assert "Embedded Python runtime DLL boundary mismatch" in verifier
    assert "Embedded Python runtime metadata mismatch" in verifier
    assert verifier.index("_verify_embedded_python_runtime(root, files)") < verifier.index(
        "_verify_onedir_qt_contract(root, files)"
    )


def test_license_collector_and_current_docs_name_the_new_runtime() -> None:
    collector = _read("scripts/collect_third_party_licenses.py")
    current_docs = "\n".join(
        _read(path)
        for path in (
            "AGENTS.md",
            "README.md",
            "docs/README.txt",
            "docs/RELEASE_PROCESS_KO.md",
            "docs/LGPL_RUNTIME_REPLACEMENT_KO_EN.md",
        )
    )
    performance_report = _read("docs/PERFORMANCE_REPORT_KO.md")

    assert '"python_version": "3.13"' in collector
    assert "CPython 3.13.15 (standard GIL build)" in collector
    assert "3.13.15" in current_docs
    assert "free-threaded" in current_docs
    assert "CPython 3.11.9" in performance_report
    assert "역사 측정값" in performance_report
    assert "측정 불가" in performance_report
