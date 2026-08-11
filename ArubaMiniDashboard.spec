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
VERSION_FILE = os.environ.get("ARUBA_BUILD_VERSION_FILE") or None
APPROVED_QT_RUNTIME_ARTIFACTS = {
    "pyside6/msvcp140.dll",
    "pyside6/msvcp140_1.dll",
    "pyside6/msvcp140_2.dll",
    "pyside6/pyside6.abi3.dll",
    "pyside6/qt6core.dll",
    "pyside6/qt6gui.dll",
    "pyside6/qt6svg.dll",
    "pyside6/qt6widgets.dll",
    "pyside6/qtcore.pyd",
    "pyside6/qtgui.pyd",
    "pyside6/qtwidgets.pyd",
    "pyside6/plugins/iconengines/qsvgicon.dll",
    "pyside6/plugins/imageformats/qsvg.dll",
    "pyside6/plugins/platforms/qoffscreen.dll",
    "pyside6/plugins/platforms/qwindows.dll",
    "pyside6/plugins/styles/qmodernwindowsstyle.dll",
    "pyside6/vcruntime140.dll",
    "pyside6/vcruntime140_1.dll",
    "shiboken6/msvcp140.dll",
    "shiboken6/shiboken.pyd",
    "shiboken6/shiboken6.abi3.dll",
    "shiboken6/vcruntime140.dll",
    "shiboken6/vcruntime140_1.dll",
}
EXCLUDED_QT_BINARIES = {
    "pyside6/opengl32sw.dll",
    "pyside6/qt6opengl.dll",
    "pyside6/qt6network.dll",
    "pyside6/qt6pdf.dll",
    "pyside6/qt6qml.dll",
    "pyside6/qt6qmlmeta.dll",
    "pyside6/qt6qmlmodels.dll",
    "pyside6/qt6qmlworkerscript.dll",
    "pyside6/qt6quick.dll",
    "pyside6/qt6virtualkeyboard.dll",
    "pyside6/qtnetwork.pyd",
    "pyside6/plugins/imageformats/qpdf.dll",
    "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
}


def is_reviewed_qt_runtime_path(destination):
    return destination.startswith(("pyside6/", "shiboken6/")) and destination.endswith(
        (".dll", ".pyd")
    )


def keep_runtime_entry(entry):
    destination = str(entry[0]).replace("\\", "/").casefold()
    if destination in EXCLUDED_QT_BINARIES:
        return False
    if is_reviewed_qt_runtime_path(destination):
        return destination in APPROVED_QT_RUNTIME_ARTIFACTS
    return True

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
        "scp",
        "win32cred",
        "win32timezone",
        "cryptography.hazmat.backends.openssl",
    ])),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pandas",
        "PySide6.QtNetwork",
        "PySide6.QtPdf",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtVirtualKeyboard",
    ],
    noarchive=False,
    optimize=0,
    module_collection_mode={
        "PySide6": "py",
        "paramiko": "py",
        "scp": "py",
        "shiboken6": "py",
    },
)
a.binaries = [entry for entry in a.binaries if keep_runtime_entry(entry)]
a.datas = [entry for entry in a.datas if keep_runtime_entry(entry)]
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
        version=VERSION_FILE,
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
        version=VERSION_FILE,
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
