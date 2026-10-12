# PyInstaller spec for the standalone "Posture Guard.app". Build with ./build_app.sh.
# -*- mode: python -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is provided by PyInstaller)
VERSION = (ROOT / "VERSION").read_text().strip()

a = Analysis(  # noqa: F821
    [str(ROOT / "menubar.py")],
    pathex=[str(ROOT)],
    # MediaPipe loads its pose/face models (.tflite, .binarypb) from inside its package at runtime.
    datas=collect_data_files("mediapipe", includes=["**/*.tflite", "**/*.binarypb", "**/*.txt"]),
    hiddenimports=["AVFoundation"],
    excludes=["tkinter", "IPython", "jax", "jaxlib", "tensorflow", "sounddevice"],
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Posture Guard",
    console=False,
    target_arch=None,  # the architecture of the Python doing the build
)
coll = COLLECT(exe, a.binaries, a.datas, name="Posture Guard")  # noqa: F821

app = BUNDLE(  # noqa: F821
    coll,
    name="Posture Guard.app",
    icon=str(ROOT / "packaging" / "PostureGuard.icns"),
    bundle_identifier="com.postureguard.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "Posture Guard",
        "CFBundleDisplayName": "Posture Guard",
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,  # no Dock icon; the menu bar icon is the UI
        "LSMinimumSystemVersion": "11.0",
        "NSCameraUsageDescription": "Posture Guard watches your posture locally to remind you to sit up straight.",
        "NSAppleEventsUsageDescription": "Posture Guard shows notifications and stretch-break dialogs.",
        "NSHighResolutionCapable": True,
    },
)
