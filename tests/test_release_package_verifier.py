from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from scripts import verify_release_package as verifier


NAME = "ArubaMiniDashboard"


def _write(path: Path, content: bytes = b"release-test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _make_onedir(tmp_path: Path, *, root_name: str = NAME) -> Path:
    root = tmp_path / root_name
    _write(root / f"{NAME}.exe", b"fake-executable")
    for packaged_name, source_relative in verifier.COMMITTED_RELEASE_DOCUMENT_SOURCES.items():
        _write(root / packaged_name, (verifier.PROJECT_ROOT / source_relative).read_bytes())
    _write(root / "LGPL_RUNTIME_INVENTORY.json", b"{}")
    _write(root / "QT_RUNTIME_INVENTORY.json", b"{}")
    _write(root / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll")
    _write(root / "_internal" / "PySide6" / "plugins" / "imageformats" / "qsvg.dll")
    _write(root / "_internal" / "shiboken6" / "MSVCP140.dll")
    _write(root / "_internal" / "shiboken6" / "Shiboken.pyd")
    _write(root / "_internal" / "shiboken6" / "shiboken6.abi3.dll")
    _write(root / "_internal" / "shiboken6" / "VCRUNTIME140.dll")
    _write(root / "_internal" / "shiboken6" / "VCRUNTIME140_1.dll")
    return root


def _zip_tree(root: Path, zip_path: Path, *, archive_root: str | None = None) -> Path:
    top = archive_root or root.name
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, f"{top}/{path.relative_to(root).as_posix()}")
    return zip_path


def _valid_zip_entries() -> list[tuple[str, bytes]]:
    entries = [
        (f"{NAME}/{NAME}.exe", b"fake-executable"),
        (f"{NAME}/LGPL_RUNTIME_INVENTORY.json", b"{}"),
        (f"{NAME}/QT_RUNTIME_INVENTORY.json", b"{}"),
        (f"{NAME}/_internal/PySide6/plugins/platforms/qwindows.dll", b"plugin"),
        (f"{NAME}/_internal/PySide6/plugins/imageformats/qsvg.dll", b"plugin"),
        (f"{NAME}/_internal/shiboken6/MSVCP140.dll", b"binding"),
        (f"{NAME}/_internal/shiboken6/Shiboken.pyd", b"binding"),
        (f"{NAME}/_internal/shiboken6/shiboken6.abi3.dll", b"binding"),
        (f"{NAME}/_internal/shiboken6/VCRUNTIME140.dll", b"binding"),
        (f"{NAME}/_internal/shiboken6/VCRUNTIME140_1.dll", b"binding"),
    ]
    entries.extend(
        (f"{NAME}/{packaged_name}", (verifier.PROJECT_ROOT / source_relative).read_bytes())
        for packaged_name, source_relative in verifier.COMMITTED_RELEASE_DOCUMENT_SOURCES.items()
    )
    return entries


def _write_zip_entries(zip_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return zip_path


def _disable_smoke(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    called: list[Path] = []

    def record(executable: Path, *, timeout: int = 30) -> None:
        assert timeout > 0
        assert executable.read_bytes() == b"fake-executable"
        called.append(executable)

    monkeypatch.setattr(verifier, "_run_executable_smoke", record)
    monkeypatch.setattr(verifier, "_verify_pe_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verifier, "_verify_qt_runtime_inventory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verifier, "_verify_lgpl_runtime_contract", lambda *_args, **_kwargs: None)
    return called


def _make_source_bound_documents(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for packaged_name, source_relative in verifier.COMMITTED_RELEASE_DOCUMENT_SOURCES.items():
        _write(root / packaged_name, (verifier.PROJECT_ROOT / source_relative).read_bytes())
    for generated_name in {"LGPL_RUNTIME_INVENTORY.json", "QT_RUNTIME_INVENTORY.json"}:
        _write(root / generated_name, b"{}")


@pytest.mark.parametrize("packaged_name", sorted(verifier.COMMITTED_RELEASE_DOCUMENT_SOURCES))
def test_release_documents_are_byte_bound_to_committed_sources(
    tmp_path: Path, packaged_name: str
) -> None:
    root = tmp_path / NAME
    _make_source_bound_documents(root)
    verifier._verify_release_documents(root)

    (root / packaged_name).write_bytes(b"tampered but non-empty")
    with pytest.raises(SystemExit, match="differs from the committed source"):
        verifier._verify_release_documents(root)


def test_pe_metadata_matches_product_filename_and_expected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = {
        b"ProductName": b"Aruba Mini Dashboard",
        b"FileDescription": b"Aruba MM and WLC Mini Dashboard",
        b"OriginalFilename": b"ArubaMiniDashboard.exe",
        b"FileVersion": b"0.1.1.0",
        b"ProductVersion": b"0.1.1.0",
    }

    class FakePe:
        FileInfo = [[SimpleNamespace(StringTable=[SimpleNamespace(entries=entries)])]]
        FILE_HEADER = SimpleNamespace(Machine=0x8664)

        def close(self) -> None:
            return None

    fake_module = SimpleNamespace(
        PE=lambda *_args, **_kwargs: FakePe(),
        PEFormatError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "pefile", fake_module)

    verifier._verify_pe_metadata(Path(f"{NAME}.exe"), NAME, "0.1.1")

    entries[b"FileVersion"] = b"0.1.0.0"
    with pytest.raises(SystemExit, match="PE version mismatch"):
        verifier._verify_pe_metadata(Path(f"{NAME}.exe"), NAME, "0.1.1")


def test_pe_metadata_rejects_non_amd64_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePe:
        FileInfo: list[object] = []
        FILE_HEADER = SimpleNamespace(Machine=0x14C)

        def close(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "pefile",
        SimpleNamespace(PE=lambda *_args, **_kwargs: FakePe(), PEFormatError=RuntimeError),
    )

    with pytest.raises(SystemExit, match="not AMD64 PE"):
        verifier._verify_pe_metadata(Path(f"{NAME}.exe"), NAME, "0.1.1")


def test_legacy_path_name_interface_verifies_onedir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_onedir(tmp_path)
    called = _disable_smoke(monkeypatch)

    verifier.verify(root, NAME, False)

    assert called == [root / f"{NAME}.exe"]
    assert "ARUBA_MINI_DASHBOARD_PACKAGE_OK" in capsys.readouterr().out


def test_one_file_directory_is_rejected_as_an_unsupported_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "onefile"
    _write(root / f"{NAME}.exe", b"fake-executable")
    _write(root / "config.example.json", b"{}")
    _write(root / "README.txt", b"readme")
    _write(root / "LGPL_RUNTIME_INVENTORY.json", b"{}")
    _write(root / "LGPL_RUNTIME_REPLACEMENT_KO_EN.md", b"replacement guide")
    _write(root / "QT_RUNTIME_INVENTORY.json", b"{}")
    _write(root / "QT_THIRD_PARTY_NOTICES.txt", b"qt notices")
    _write(root / "THIRD_PARTY_NOTICES.txt", b"third-party notices")
    _write(root / "WINDOWS11_QA_CHECKLIST_KO.md", b"checklist")
    called = _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="One-file output is not a supported release"):
        verifier.verify(root, NAME, True)

    assert called == []


def test_standalone_one_file_executable_is_not_a_complete_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / f"{NAME}.exe"
    _write(executable, b"fake-executable")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="One-file output is not a supported release"):
        verifier.verify(executable, NAME, True)


def test_standalone_executable_without_one_file_flag_is_not_a_complete_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / f"{NAME}.exe"
    _write(executable, b"fake-executable")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="standalone executable is not a complete release"):
        verifier.verify(executable, NAME, False)


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("known_hosts", "known_hosts"),
        ("known_hosts.old", "known_hosts"),
        ("settings.json", "runtime settings"),
        ("config.json", "runtime configuration"),
        ("app.db", "database"),
        ("app.sqlite3.backup", "database"),
        ("app.db-wal", "database"),
        ("state.shm", "database"),
        (".env", "environment"),
        ("prod.env", "environment"),
        ("server.pem", "key or certificate"),
        ("device.pfx", "key or certificate"),
        ("id_rsa", "SSH key"),
        ("crash.dmp", "dump"),
        ("core.123", "dump"),
        ("logs/app.txt", "log directory"),
        ("app.log.1", "log file"),
        ("module.py", "Python source"),
        ("requirements-lock.txt", "dependency manifest"),
        ("__pycache__/module.bin", "cache"),
        (".pytest_cache/state", "cache"),
        (".git/config", "version-control"),
        ("credentials.json", "credential"),
        ("symbols.pdb", "debug symbol"),
    ],
)
def test_folder_rejects_sensitive_and_development_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    message: str,
) -> None:
    root = _make_onedir(tmp_path)
    _write(root / Path(relative), b"must-not-ship")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match=message):
        verifier.verify(root, NAME, False)


@pytest.mark.parametrize(
    "relative",
    [
        "_internal/PySide6/Qt6VirtualKeyboard.dll",
        "_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
        "_internal/PySide6.QtVirtualKeyboard/module.bin",
    ],
)
def test_qt_virtual_keyboard_artifacts_are_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = _make_onedir(tmp_path)
    _write(root / Path(relative), b"unapproved-component")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="Qt Virtual Keyboard"):
        verifier.verify(root, NAME, False)


@pytest.mark.parametrize(
    "relative",
    [
        "_internal/PySide6/Qt6Pdf.dll",
        "_internal/PySide6/plugins/imageformats/qpdf.dll",
        "_internal/PySide6/Qt6Qml.dll",
        "_internal/PySide6/Qt6QmlModels.dll",
        "_internal/PySide6/Qt6Quick.dll",
        "_internal/PySide6/Qt6OpenGL.dll",
        "_internal/PySide6/QtQml.pyd",
        "_internal/PySide6/QtQuick.pyd",
        "_internal/PySide6/QtOpenGL.pyd",
    ],
)
def test_unused_qt_runtime_artifacts_are_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = _make_onedir(tmp_path)
    _write(root / Path(relative), b"unapproved-component")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="unused Qt component"):
        verifier.verify(root, NAME, False)


@pytest.mark.parametrize(
    "relative",
    [
        "settings.json",
        "known_hosts",
        ".env.production",
        "state.sqlite-wal",
        "logs/app.log",
        "server.key",
        "crash.dump",
        "module.pyc",
        ".git/HEAD",
        "_internal/PySide6/Qt6VirtualKeyboard.dll",
    ],
)
def test_zip_rejects_sensitive_artifacts_before_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    archive_path = _write_zip_entries(
        tmp_path / "release.zip",
        [*_valid_zip_entries(), (f"{NAME}/{relative}", b"must-not-ship")],
    )
    smoke = _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="forbidden"):
        verifier.verify(archive_path, NAME, False)

    assert smoke == []


def test_zip_is_hashed_extracted_reinspected_and_smoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_onedir(tmp_path / "source")
    archive_path = _zip_tree(root, tmp_path / "release.zip")
    expected = sha256(archive_path.read_bytes()).hexdigest().upper()
    smoke = _disable_smoke(monkeypatch)

    verifier.verify(archive_path, NAME, False, expected_sha256=expected)

    assert len(smoke) == 1
    assert smoke[0].name == f"{NAME}.exe"
    assert not smoke[0].exists(), "ZIP smoke must run from a cleaned isolated extraction"


def test_standard_checksum_sidecar_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(tmp_path / "release.zip", _valid_zip_entries())
    digest = sha256(archive_path.read_bytes()).hexdigest()
    sidecar = tmp_path / "release.zip.sha256.txt"
    sidecar.write_text(f"{digest.upper()}  {archive_path.name}\n", encoding="ascii")
    _disable_smoke(monkeypatch)

    verifier.verify(archive_path, NAME, False, checksum_file=sidecar)


def test_hash_mismatch_fails_before_zip_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(tmp_path / "release.zip", _valid_zip_entries())
    smoke = _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        verifier.verify(archive_path, NAME, False, expected_sha256="0" * 64)

    assert smoke == []


def test_checksum_sidecar_filename_must_match_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(tmp_path / "release.zip", _valid_zip_entries())
    sidecar = tmp_path / "release.sha256"
    sidecar.write_text(f"{'0' * 64}  other.zip\n", encoding="ascii")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="Checksum filename mismatch"):
        verifier.verify(archive_path, NAME, False, checksum_file=sidecar)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        f"{NAME}/../escape.txt",
        f"{NAME}/./ambiguous.txt",
        "/absolute.txt",
        r"C:/drive.txt",
        rf"{NAME}\..\backslash-escape.txt",
        f"{NAME}/file.txt:secret",
        f"{NAME}/trailing./file.txt",
        f"{NAME}/CON/file.txt",
    ],
)
def test_zip_slip_and_windows_ambiguous_paths_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_name: str,
) -> None:
    # ZipInfo normalizes backslashes to forward slashes on Windows.  Patch the
    # same-length filename bytes in both ZIP headers to model a hostile archive
    # produced by a tool that does not perform that normalization.
    stored_name = unsafe_name.replace("\\", "|")
    archive_path = _write_zip_entries(
        tmp_path / "unsafe.zip",
        [*_valid_zip_entries(), (stored_name, b"unsafe")],
    )
    if stored_name != unsafe_name:
        archive_bytes = archive_path.read_bytes()
        placeholder = stored_name.encode("ascii")
        hostile = unsafe_name.encode("ascii")
        assert len(placeholder) == len(hostile)
        assert archive_bytes.count(placeholder) == 2
        archive_path.write_bytes(archive_bytes.replace(placeholder, hostile))
    smoke = _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit):
        verifier.verify(archive_path, NAME, False)

    assert smoke == []


def test_zip_requires_exactly_one_expected_top_level_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    multiple = _write_zip_entries(
        tmp_path / "multiple.zip",
        [*_valid_zip_entries(), ("Other/file.txt", b"extra-root")],
    )
    wrong = _write_zip_entries(
        tmp_path / "wrong.zip",
        [(name.replace(f"{NAME}/", "Wrong/", 1), body) for name, body in _valid_zip_entries()],
    )
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="exactly one top-level"):
        verifier.verify(multiple, NAME, False)
    with pytest.raises(SystemExit, match="top-level directory"):
        verifier.verify(wrong, NAME, False)


def test_zip_rejects_case_insensitive_component_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(
        tmp_path / "collision.zip",
        [
            *_valid_zip_entries(),
            (f"{NAME}/_internal/Case/A.dll", b"a"),
            (f"{NAME}/_internal/case/B.dll", b"b"),
        ],
    )
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="case-insensitive path collisions"):
        verifier.verify(archive_path, NAME, False)


def test_zip_rejects_duplicate_exact_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _valid_zip_entries()
    entries.append((entries[0][0], b"duplicate"))
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive_path = _write_zip_entries(tmp_path / "duplicate.zip", entries)
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="duplicate entries"):
        verifier.verify(archive_path, NAME, False)


def test_zip_rejects_file_directory_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(
        tmp_path / "file-dir.zip",
        [
            *_valid_zip_entries(),
            (f"{NAME}/collision", b"file"),
            (f"{NAME}/collision/child.bin", b"child"),
        ],
    )
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="file/directory path collision"):
        verifier.verify(archive_path, NAME, False)


def test_zip_rejects_symbolic_link_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(tmp_path / "symlink.zip", _valid_zip_entries())
    link = ZipInfo(f"{NAME}/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive_path, "a") as archive:
        archive.writestr(link, "target")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="link or special file"):
        verifier.verify(archive_path, NAME, False)


def test_corrupt_zip_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "corrupt.zip"
    archive_path.write_bytes(b"not-a-zip")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="not a valid ZIP"):
        verifier.verify(archive_path, NAME, False)


@pytest.mark.parametrize(
    ("omitted", "message"),
    [
        ("README.txt", "Required release document missing"),
        ("THIRD_PARTY_NOTICES.txt", "Required release document missing"),
        ("qwindows.dll", "qwindows.dll"),
        ("qsvg.dll", "qsvg.dll"),
    ],
)
def test_required_documents_and_qt_plugins_are_enforced_after_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted: str,
    message: str,
) -> None:
    entries = [(name, body) for name, body in _valid_zip_entries() if not name.endswith(omitted)]
    archive_path = _write_zip_entries(tmp_path / "missing.zip", entries)
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match=message):
        verifier.verify(archive_path, NAME, False)


def test_qt_plugin_basename_in_wrong_directory_does_not_satisfy_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_onedir(tmp_path)
    required = root / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll"
    required.unlink()
    _write(root / "_internal" / "decoy" / "qwindows.dll", b"decoy")
    _disable_smoke(monkeypatch)

    with pytest.raises(SystemExit, match="required path.*qwindows.dll"):
        verifier.verify(root, NAME, False)


def test_extracted_tree_is_reinspected_before_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_zip_entries(tmp_path / "release.zip", _valid_zip_entries())
    real_extract = verifier._extract_open_zip_safely
    smoke = _disable_smoke(monkeypatch)

    def inject_runtime_state(archive: ZipFile, destination: Path) -> None:
        real_extract(archive, destination)
        _write(destination / NAME / "settings.json", b"injected")

    monkeypatch.setattr(verifier, "_extract_open_zip_safely", inject_runtime_state)

    with pytest.raises(SystemExit, match="runtime settings"):
        verifier.verify(archive_path, NAME, False)
    assert smoke == []


def test_smoke_sanitizes_python_environment_and_requires_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / f"{NAME}.exe"
    executable.write_bytes(b"fake")
    monkeypatch.setenv("PYTHONPATH", "sensitive-source-path")
    monkeypatch.setenv("VIRTUAL_ENV", "sensitive-venv")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        sentinel = Path(command[command.index("--smoke-output") + 1])
        if "--ui-smoke" in command:
            captured["ui_env"] = kwargs["env"]
            sentinel.write_text("WINDOWS_QT_UI_OK\n", encoding="utf-8")
        else:
            captured["command"] = command
            captured["env"] = kwargs["env"]
            required = set(verifier.REQUIRED_SMOKE_MARKERS)
            if verifier.os.name == "nt":
                required.add("WIN32CRED_OK")
            sentinel.write_text("\n".join(sorted(required)), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    verifier._run_executable_smoke(executable)

    env = captured["env"]
    assert isinstance(env, dict)
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    if verifier.os.name == "nt":
        ui_env = captured["ui_env"]
        assert isinstance(ui_env, dict)
        assert "QT_QPA_PLATFORM" not in ui_env


def test_smoke_timeout_and_missing_markers_fail_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / f"{NAME}.exe"
    executable.write_bytes(b"fake")

    def missing_markers(command: list[str], **kwargs: object) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", missing_markers)
    with pytest.raises(SystemExit, match="missing_markers"):
        verifier._run_executable_smoke(executable)

    def timeout(command: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(verifier.subprocess, "run", timeout)
    with pytest.raises(SystemExit, match="timed out"):
        verifier._run_executable_smoke(executable)


def test_cli_supports_legacy_path_and_explicit_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release.zip"
    release.write_bytes(b"placeholder")
    calls: list[tuple[Path, str, bool, dict[str, object]]] = []

    def fake_verify(path: Path, name: str, one_file: bool, **kwargs: object) -> None:
        calls.append((path, name, one_file, kwargs))

    monkeypatch.setattr(verifier, "verify", fake_verify)

    assert verifier.main(["--path", str(release), "--name", NAME]) == 0
    assert verifier.main(
        [
            "--zip",
            str(release),
            "--name",
            NAME,
            "--one-file",
            "--expected-sha256",
            "0" * 64,
            "--smoke-timeout",
            "45",
        ]
    ) == 0

    assert calls[0][0] == release.resolve()
    assert calls[0][3]["force_zip"] is False
    assert calls[1][2] is True
    assert calls[1][3]["force_zip"] is True
    assert calls[1][3]["smoke_timeout"] == 45
