# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
SRC = ROOT / "src"
CONSOLE = os.environ.get("ARUBA_BUILD_CONSOLE", "0") == "1"
ONEFILE = os.environ.get("ARUBA_BUILD_ONEFILE", "0") == "1"
NAME = "ArubaMiniDashboardConsole" if CONSOLE else "ArubaMiniDashboard"

netmiko_hidden = collect_submodules("netmiko")
paramiko_hidden = collect_submodules("paramiko")

datas = [
    (str(ROOT / "tests" / "fixtures"), "tests/fixtures"),
    (str(SRC / "aruba_mini_dashboard" / "ui" / "resources"), "aruba_mini_dashboard/ui/resources"),
]

a = Analysis(
    [str(ROOT / "scripts" / "pyinstaller_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(netmiko_hidden + paramiko_hidden + [
        "win32cred",
        "win32timezone",
        "cryptography.hazmat.backends.openssl",
    ])),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=NAME,
    )
