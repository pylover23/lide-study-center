# -*- mode: python ; coding: utf-8 -*-
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all

packages = ["ddddocr", "onnxruntime", "numpy", "PIL", "cryptography"]
datas, binaries, hiddenimports = [], [], []
for package in packages:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

conda_bin = os.path.join(sys.prefix, "Library", "bin")
for dll in glob.glob(os.path.join(conda_bin, "mkl*.dll")):
    binaries.append((dll, "."))
openmp_dll = os.path.join(conda_bin, "libiomp5md.dll")
if os.path.exists(openmp_dll):
    binaries.append((openmp_dll, "."))


a = Analysis(
    ["gui_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "onnxruntime.tools", "onnxruntime.quantization", "onnxruntime.transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LideStudyCenterGUI",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LideStudyCenterGUI",
)
