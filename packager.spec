# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 构建定义 —— 这是构建参数的**唯一真相源**。
#
# 注意：spec 存在时，PyInstaller 会忽略命令行上的 --onefile / --windowed /
# --name 等选项，改由本文件决定。build_exe.bat 因此只传 --clean --noconfirm。
# 想改构建行为请改这里，不要改 bat 的命令行参数（那样不会生效且无任何提示）。

import os

# 用 SPECPATH 而非相对路径，保证从任意工作目录执行
# `pyinstaller D:\path\to\packager.spec` 都能找到入口脚本
a = Analysis(
    [os.path.join(SPECPATH, 'packager.py')],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='packager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 不使用 UPX：UPX 不在 PATH 时 PyInstaller 只打印 warning 并继续构建，
    # 产物是否被压缩取决于构建机是否装了 UPX，跨机产物不一致。
    # 为保证可复现性，这里固定关闭。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 窗口模式（无控制台）：GUI 工具双击不应弹出黑色控制台窗口。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
