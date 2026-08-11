from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


BANNED_SUFFIXES = {".py", ".pyc", ".db", ".log", ".cred", ".key"}


def verify(path: Path, name: str, one_file: bool) -> None:
    if not path.exists():
        raise SystemExit(f"Release path not found: {path}")
    exe = path / f"{name}.exe" if path.is_dir() else path
    if one_file:
        exe = path / f"{name}.exe" if path.is_dir() else path
    if not exe.is_file():
        raise SystemExit(f"Executable missing: {exe}")

    if path.is_dir() and not one_file:
        files = [item for item in path.rglob("*") if item.is_file()]
        banned = [item for item in files if item.suffix.casefold() in BANNED_SUFFIXES]
        if banned:
            raise SystemExit("Banned release files: " + ", ".join(str(item) for item in banned))
        names = {item.name.casefold() for item in files}
        if "qwindows.dll" not in names:
            raise SystemExit("Qt qwindows.dll platform plugin is missing")
        if "qsvg.dll" not in names:
            raise SystemExit("Qt qsvg.dll image plugin is missing")
        for required in ("config.example.json", "README.txt", "WINDOWS11_QA_CHECKLIST_KO.md"):
            if not (path / required).is_file():
                raise SystemExit(f"Required release document missing: {required}")

    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith("PYTHON") or key.upper() in {"VIRTUAL_ENV", "CONDA_PREFIX"}:
            env.pop(key, None)
    env["PATH"] = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep)
        if "python" not in entry.casefold() and ".venv" not in entry.casefold()
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="ArubaMiniDashboard-smoke-") as temp_dir:
        env["ARUBA_MINI_DASHBOARD_DATA_DIR"] = temp_dir
        sentinel = Path(temp_dir) / "smoke-ok.txt"
        completed = subprocess.run(
            [str(exe), "--smoke", "--smoke-output", str(sentinel)],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        sentinel_text = sentinel.read_text(encoding="utf-8") if sentinel.is_file() else ""
    combined = (completed.stdout or "") + (completed.stderr or "")
    required_smoke_markers = {
        "ARUBA_MINI_DASHBOARD_SMOKE_OK",
        "NETMIKO_OK",
        "PARAMIKO_OK",
        "FIXTURE_DISCOVERY_OK",
        "DEMO_CORRELATION_OK",
    }
    if os.name == "nt":
        required_smoke_markers.add("WIN32CRED_OK")
    present_markers = set(sentinel_text.splitlines())
    missing_markers = sorted(required_smoke_markers - present_markers)
    if completed.returncode != 0 or missing_markers:
        raise SystemExit(
            "Executable smoke failed "
            f"rc={completed.returncode} missing_markers={missing_markers!r} "
            f"sentinel={sentinel_text!r}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    print("ARUBA_MINI_DASHBOARD_PACKAGE_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--one-file", action="store_true")
    args = parser.parse_args()
    verify(args.path.resolve(), args.name, args.one_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
