# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 单文件 exe，运行时状态仍写 D:\\Light-Novel\\.autosync。

构建（在项目根目录执行）：
    .venv\\Scripts\\pyinstaller.exe build\\lightnovel.spec --noconfirm

产物：
    dist\\LightNovel.exe

为什么 console=True：
    headless 子命令（sync / mirror / opds）需要控制台才能看到输出；
    双击启动图形面板时由 lightnovel/ui.py 的 _hide_console() 自动隐藏该窗口。
"""
import os

ROOT = os.path.dirname(SPECPATH)

# 子模块全部走「函数内延迟导入」，显式列全，避免被静态分析漏掉。
HIDDEN = [
    "lightnovel",
    "lightnovel.paths",
    "lightnovel.cli",
    "lightnovel.ui",
    "lightnovel.tunnel_setup",
    "lightnovel.opds",
    "lightnovel.opds.library",
    "lightnovel.opds.feeds",
    "lightnovel.opds.server",
    # 运行时状态类模块：目前都是 server/feeds 里的静态 import，静态分析能自己找到；
    # 显式列出来是为了「哪天改成函数内延迟导入也不会漏」（包体只大几 KB）。
    "lightnovel.opds.finished",
    "lightnovel.opds.updates",
    "lightnovel.opds.session",
    "lightnovel.opds.moon",
    "lightnovel.sync",
    "lightnovel.sync.gitops",
    "lightnovel.sync.mirror",
    "lightnovel.sync.monitor",
]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "numpy", "pandas", "matplotlib", "scipy", "PIL",
        "setuptools", "pip", "wheel", "pydoc_data",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LightNovel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
