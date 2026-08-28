# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Windows portable competition build."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


ROOT = Path(SPECPATH).resolve().parents[1]
BACKEND = ROOT / "backend"

datas = []
binaries = []
hiddenimports = []

# Keep only package data for ChromaDB.  Collecting all of its optional embedding
# providers would also drag CUDA/PyTorch, TensorFlow, Qt and notebook stacks into
# this CPU-only application.
datas += collect_data_files("chromadb")
datas += collect_data_files("rfc3987_syntax")

for package in ("onnxruntime", "edge_tts"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# Chroma resolves these implementations from dotted strings in Settings, so
# PyInstaller cannot discover them through normal static import analysis.
hiddenimports += [
    "chromadb.api.rust",
    "chromadb.db.impl.sqlite",
    "chromadb.execution.executor.local",
    "chromadb.ingest.impl.simple_policy",
    "chromadb.quota.simple_quota_enforcer",
    "chromadb.rate_limit.simple_rate_limit",
    "chromadb.segment.impl.distributed.segment_directory",
    "chromadb.segment.impl.manager.local",
    "chromadb.telemetry.product.events",
    "chromadb.telemetry.product.posthog",
]

a = Analysis(
    [str(BACKEND / "desktop_launcher.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "torch",
        "torchvision",
        "torchaudio",
        "tensorflow",
        "keras",
        "PyQt5",
        "PyQt6",
        "PySide6",
        "cv2",
        "pandas",
        "scipy",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="灵山胜境AI数字人",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    upx=True,
    upx_exclude=[],
    name="灵山胜境AI数字人",
)
