# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the frozen Spice Maker application.

Portable on purpose: every path is derived from ``SPECPATH``, the directory holding this
file, so any checkout builds — no absolute machine path is committed. Run it through the
packaging pipeline, which is what also feeds Velopack:

    uv run --extra packaging pyinstaller installer/SpiceMaker.spec --noconfirm --clean

``installer/build.ps1`` does exactly that and then hands ``dist/SpiceMaker`` to ``vpk``
(``--distpath``/``--workpath`` stay at their defaults, i.e. next to the repo root).

The frozen executable is ``SpiceMaker.exe``; ``console=False`` (windowed) because the
product is a GUI. ``installer/entry.py`` gives that windowed binary a CLI path as well.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller
INSTALLER = Path(SPECPATH)  # noqa: F821
NAME = "SpiceMaker"
ICON = INSTALLER / "assets" / "pepper.ico"

# providers/registry.py imports the provider modules by name at run time, which static
# analysis cannot see. Without this a frozen build answers the configured provider with
# "not available in this build" instead of running it.
hiddenimports = collect_submodules("boardmodeler")

# A frozen build collects what is *installed*, not what is imported. This venv carries the
# optional "sim" extra (spicelib -> scipy + matplotlib) and the "dev" extra (reportlab ->
# PIL), none of which the product imports: without these excludes the installer carries
# ~95 MB of them. spicelib is excluded by name too, so the optional-reader path reports
# its documented "not installed (optional 'sim' extra), using the native reader" instead
# of failing on a missing scipy. The native reader is authoritative in any case (D-002).
excludes = [
    "spicelib",
    "scipy",
    "matplotlib",
    "mpl_toolkits",
    "PIL",
    "reportlab",
    "pytest",
    "_pytest",
    "IPython",
    "pandas",
    "tkinter",
    # Qt modules a QtCore/QtGui/QtWidgets app never loads. QtNetwork, QtSvg and
    # QtPrintSupport stay in: they are part of the supported desktop surface.
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDBus",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtXml",
]

a = Analysis(
    [os.fspath(INSTALLER / "entry.py")],
    pathex=[os.fspath(REPO / "src")],
    binaries=[],
    # The window looks for its icon next to the executable and inside the bundle;
    # "installer/assets/pepper.ico" is the mark the splash and the .ico are drawn from.
    datas=[
        (os.fspath(ICON), "."),
        (
            os.fspath(REPO / "src" / "boardmodeler" / "models" / "peak_current_buck.json"),
            "boardmodeler/models",
        ),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
# Qt's software-OpenGL fallback: 20 MB, and nothing here asks for OpenGL (the UI is
# QWidget + QPainter, and Qt's windows platform plugin paints in software).
a.binaries = [b for b in a.binaries if not b[0].endswith("opengl32sw.dll")]
# Qt's own translations: the application's strings are English-only.
a.datas = [
    d for d in a.datas if not (d[0].endswith(".qm") and "PySide6" in d[0].replace("\\", "/"))
]
# The excluded distributions' metadata rides along even though their modules are gone,
# which would make ``importlib.metadata.version("spicelib")`` report an installed reader
# that cannot be imported. Drop it so the optional-reader path reports its documented
# "not installed (optional 'sim' extra); using the native reader".
_EXCLUDED_METADATA = ("spicelib", "scipy", "matplotlib", "pillow", "reportlab", "pytest")


def _keeps(entry):
    head = entry[0].replace("\\", "/").split("/", 1)[0].lower()
    return not any(head.startswith(f"{name}-") for name in _EXCLUDED_METADATA)


a.datas = [d for d in a.datas if _keeps(d)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.fspath(ICON)],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=NAME,
)
