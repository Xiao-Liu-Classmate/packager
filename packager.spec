# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 构建定义 —— 这是构建参数的**唯一真相源**。
#
# 注意：spec 存在时，PyInstaller 会忽略命令行上的 --onefile / --windowed /
# --name 等选项，改由本文件决定。build_exe.bat 因此只传 --clean --noconfirm。
# 想改构建行为请改这里，不要改 bat 的命令行参数（那样不会生效且无任何提示）。

import os

# 用 SPECPATH 而非相对路径，保证从任意工作目录执行
# `pyinstaller D:\path\to\packager.spec` 都能找到入口脚本
# 界面层已迁移到 PySide6/Qt 6（packager_ui.py）。
# PyInstaller 自带 PySide6 hook（pyinstaller-hooks-contrib），会自动收集
# QtCore/QtGui/QtWidgets 及所需的 platform plugin，无需手工列 hiddenimports。
#
# 关于 excludes：这里只排掉**确实用不到**且体积大的模块。
# 不要排 QtQuick/QtQml —— PySide6-Essentials 里的 QtWidgets 会间接引用它们，
# 排掉会在运行时报 "DLL load failed"。真正的减体积手段是装
# PySide6-Essentials 而不是完整的 PySide6（后者多带 QtWebEngine、
# QtMultimedia、Qt3D 等，约 +60MB，见 requirements-build.txt）。
EXCLUDES = [
    # 未使用的可选功能（本项目不写文件预览/媒体/网络）
    'PySide6.QtNetwork',
    'PySide6.QtMultimedia',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.Qt3DCore',
    'PySide6.QtCharts',
    'PySide6.QtDataVisualization',
    'PySide6.QtOpenGL',
    'PySide6.QtSql',
    'PySide6.QtTest',
    'PySide6.QtBluetooth',
    'PySide6.QtPositioning',
    'PySide6.QtSerialPort',
    'PySide6.QtSensors',
    'PySide6.QtSerialBus',
    # 与 tkinter 同理：GUI 已不依赖它，排掉可少带一个模块
    'tkinter',
]

a = Analysis(
    [os.path.join(SPECPATH, 'packager.py')],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
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
