#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LightNovel 一键打包脚本（Windows）—— 自举、缺件自动联网补齐。

用法（在项目根目录）::

    build\\build_exe.bat                  # 推荐：自动挑 Python 后转交本脚本
    python build\\build_exe.py            # 自己指定解释器
    python build\\build_exe.py --bundle   # 额外组装可分发包（目录 + zip）
    python build\\build_exe.py --clean --no-tests

它按顺序做这些事，每步都是「先检查，缺了才联网补」：

    [1] 挑一个带 tkinter 的基础 Python（图形面板必需；精简版 Python 没有）
    [2] 用它在项目根建 / 复用 .venv
    [3] 补齐 pip（ensurepip）
    [4] 补齐 PyInstaller（pip 自动从索引下载，走用户 pip.ini 的镜像源）
    [5] pyflakes 静态检查（装了就跑，没装跳过，不阻断）
    [6] 隔离式回归测试 tests\\smoke_test.py（--no-tests 跳过）
    [7] PyInstaller 打包 -> dist\\LightNovel.exe，并校验 PE 头 / 体积 / 能否自举
    [8] 找不到 cloudflared.exe 时自动补齐（先复用本机已有，再联网下载）
    [9] --bundle：组装 dist\\LightNovel-<版本>-win64\\ 分发目录并打 zip

下载一律走 HTTPS 并自动继承 HTTP_PROXY / HTTPS_PROXY。
任何一步失败都会打印「下一步怎么办」，不会静默退出。
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ============================ 路径与常量 ============================
ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
VENV_DIR = ROOT / ".venv"
SPEC = BUILD_DIR / "lightnovel.spec"
ENTRY = BUILD_DIR / "entry.py"
PYPROJECT = ROOT / "pyproject.toml"

APP_NAME = "LightNovel"
MIN_EXE_BYTES = 5 * 1024 * 1024          # 单文件 exe 正常在 10MB 上下，低于此值判为异常
MIN_CF_BYTES = 5 * 1024 * 1024           # cloudflared.exe 约 20MB

CF_ASSETS = {
    "amd64": "cloudflared-windows-amd64.exe",
    "arm64": "cloudflared-windows-arm64.exe",
    "x86": "cloudflared-windows-386.exe",
}
# 依次尝试：官方源 -> 两个常用 GitHub 加速镜像（国内网络更稳）
CF_MIRRORS = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}",
    "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}",
    "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}",
)

STEPS_TOTAL = 9


# ============================ 输出小工具 ============================
def log(msg=""):
    print(msg, flush=True)


def step(n, title):
    log()
    log("=" * 68)
    log(f"[{n}/{STEPS_TOTAL}] {title}")
    log("=" * 68)


def info(msg):
    log(f"  - {msg}")


def ok(msg):
    log(f"  [OK] {msg}")


def warn(msg):
    log(f"  [!!] {msg}")


def die(msg, hint=""):
    log()
    log(f"  [FAIL] {msg}")
    if hint:
        log()
        for line in hint.strip().splitlines():
            log(f"         {line}")
    log()
    sys.exit(1)


def run(cmd, **kw):
    """执行外部命令并回显。"""
    log("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def capture(cmd):
    """执行并用 (returncode, stdout+stderr 文本) 返回。"""
    p = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip()


def probe(exe_cmd, code):
    """在给定解释器里跑一小段代码，判断能力是否存在。"""
    try:
        p = subprocess.run([str(c) for c in list(exe_cmd) + ["-c", code]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=90)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolvable(cmd):
    """cmd 的第一个 token 是否可用（绝对路径要看文件，命令名要看 PATH）。"""
    first = str(cmd[0])
    if os.path.isabs(first):
        return os.path.isfile(first)
    return shutil.which(first) is not None


# ============================ [1] 挑基础 Python ============================
def _python_candidates(forced):
    cands = []
    if forced:
        cands.append([forced])
    if sys.executable:
        cands.append([sys.executable])
    for name in ("py",):
        p = shutil.which(name)
        if p:
            cands.append([p, "-3"])
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p:
            cands.append([p])
    local = os.environ.get("LOCALAPPDATA", "")
    for ver in ("314", "313", "312", "311", "310", "39"):
        if local:
            cands.append([str(Path(local) / "Programs" / "Python" / f"Python{ver}" / "python.exe")])
        cands.append([rf"C:\Python{ver}\python.exe"])
    return cands


def find_base_python(forced=None):
    """返回 (带 tkinter 的解释器命令, 不带 tkinter 的兜底命令)。"""
    seen, fallback = set(), None
    for cmd in _python_candidates(forced):
        key = tuple(str(c) for c in cmd)
        if key in seen:
            continue
        seen.add(key)
        if not _resolvable(cmd):
            continue
        if probe(cmd, "import tkinter, tkinter.ttk"):
            return cmd, fallback
        if fallback is None and probe(cmd, "import sys"):
            fallback = cmd
    return None, fallback


def do_pick_python(args):
    step(1, "选择基础 Python 解释器")
    cmd, fallback = find_base_python(args.python)
    if cmd is None:
        if fallback is not None:
            die(
                f"找到 Python（{' '.join(str(c) for c in fallback)}）但缺少 tkinter，"
                "图形控制面板无法打包。",
                """
                处理办法（任选其一）：
                  * 官方安装包重装 Python，安装时勾选 "tcl/tk and IDLE"；
                  * 或安装系统组件后重试；
                  * 或换一个自带 tkinter 的解释器：python build\\build_exe.py --python "D:\\Python313\\python.exe"
                """,
            )
        die(
            "没找到可用的 Python 解释器。",
            """
            处理办法：
              * 安装官方 Python 3.10+（安装时勾选 "Add python.exe to PATH" 与 "tcl/tk and IDLE"）；
              * 或用参数指定：python build\\build_exe.py --python "C:\\路径\\python.exe"
            """,
        )
    rc, ver = capture(list(cmd) + ["-c", "import sys;print(sys.version.split()[0])"])
    ok(f"基础解释器：{' '.join(str(c) for c in cmd)}  (Python {ver or '?'})")
    return cmd


# ============================ [2] 虚拟环境 ============================
def do_venv(base):
    step(2, "准备虚拟环境 .venv")
    vpy = VENV_DIR / "Scripts" / "python.exe"
    if vpy.is_file() and probe([str(vpy)], "import sys"):
        if not probe([str(vpy)], "import tkinter, tkinter.ttk"):
            die(
                "检测到已有 .venv，但它来自一个没有 tkinter 的解释器，无法打包图形面板。",
                """
                处理办法：删掉项目根目录下的 .venv 文件夹后重跑（该目录纯属构建产物，
                已在 .gitignore 中，删除不影响源码与书库）：
                    rmdir /s /q .venv
                    build\\build_exe.bat
                """,
            )
        ok(f"复用已有虚拟环境：{vpy}")
        return vpy

    info("创建虚拟环境（首次约需十几秒）…")
    r = run(list(base) + ["-m", "venv", str(VENV_DIR)])
    if r.returncode != 0 or not vpy.is_file():
        r = run(list(base) + ["-m", "venv", "--without-pip", str(VENV_DIR)])
        if r.returncode != 0 or not vpy.is_file():
            die("创建虚拟环境失败。",
                "处理办法：确认基础 Python 安装完整（含 venv 模块），或手动执行\n"
                "    python -m venv .venv")
    ok(f"虚拟环境就绪：{vpy}")
    return vpy


# ============================ [3] pip / [4] PyInstaller ============================
def do_pip(vpy):
    step(3, "确认 pip 可用")
    if probe([str(vpy)], "import pip"):
        rc, ver = capture([str(vpy), "-m", "pip", "--version"])
        ok(f"pip 已可用：{ver.splitlines()[0] if ver else '?'}")
        return
    warn("虚拟环境里没有 pip，正在用 ensurepip 补齐…")
    r = run([str(vpy), "-m", "ensurepip", "--upgrade", "--default-pip"])
    if r.returncode != 0 or not probe([str(vpy)], "import pip"):
        die("ensurepip 未能补齐 pip。",
            "处理办法：联网后重试；或手动执行\n"
            f"    \"{vpy}\" -m ensurepip --upgrade --default-pip")
    ok("pip 补齐完成。")


def _pip_install(vpy, packages, upgrade=False):
    args = [str(vpy), "-m", "pip", "install",
            "--disable-pip-version-check", "--retries", "3", "--timeout", "60"]
    if upgrade:
        args.append("--upgrade")
    args += list(packages)
    return run(args)


def do_pyinstaller(vpy, upgrade=False):
    step(4, "确认 PyInstaller（缺失自动联网安装）")
    if not upgrade and probe([str(vpy)], "import PyInstaller"):
        rc, ver = capture([str(vpy), "-c", "import PyInstaller;print(PyInstaller.__version__)"])
        ok(f"PyInstaller 已安装：{ver or '?'}")
        return ver
    info("正在从 pip 索引安装 pyinstaller（自动继承 pip.ini 的镜像源）…")
    r = _pip_install(vpy, ["pyinstaller>=6"], upgrade=upgrade)
    if r.returncode != 0 or not probe([str(vpy)], "import PyInstaller"):
        die(
            "PyInstaller 安装失败（多半是网络 / 代理问题）。",
            """
            处理办法：
              * 确认能访问 pip 源；需要代理时先在当前窗口设置：
                    set HTTPS_PROXY=http://127.0.0.1:12130
              * 或手动安装后重跑：
                    .venv\\Scripts\\python -m pip install pyinstaller
              * 离线机器：在有网的机器上 pip download pyinstaller -d wheels\\，
                把 wheels 目录拷过来后 pip install --no-index --find-links wheels pyinstaller
            """,
        )
    rc, ver = capture([str(vpy), "-c", "import PyInstaller;print(PyInstaller.__version__)"])
    ok(f"PyInstaller 安装完成：{ver or '?'}")
    return ver


# ============================ [5] 静态检查 / [6] 回归测试 ============================
def do_lint(vpy):
    step(5, "pyflakes 静态检查")
    if not probe([str(vpy)], "import pyflakes"):
        warn("未安装 pyflakes，跳过静态检查（不阻断打包）。")
        info("想启用：.venv\\Scripts\\python -m pip install pyflakes")
        return
    p = subprocess.run([str(vpy), "-m", "pyflakes", "lightnovel", "build", "tests"],
                       cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    if out:
        for line in out.splitlines():
            log(f"    {line}")
    if p.returncode != 0:
        die("pyflakes 报出问题，已中止打包。",
        "处理办法：修掉上面列出的告警（未使用变量 / 未定义名称 / 重复导入）后重跑。")
    ok("静态检查通过，零告警。")


def do_tests(vpy, skip):
    step(6, "隔离式回归测试")
    if skip:
        warn("已按 --no-tests 跳过回归测试。")
        return
    script = ROOT / "tests" / "smoke_test.py"
    if not script.is_file():
        warn(f"未找到 {script}，跳过。")
        return
    info("所有写操作隔离在 .autosync\\_testtmp\\，不会推送 Git / 不动真实书库。")
    r = subprocess.run([str(vpy), str(script)], cwd=str(ROOT))
    if r.returncode != 0:
        die(f"回归测试未通过（退出码 {r.returncode}），已中止打包。",
            "处理办法：先修到全绿再打包；只想出包不验证可用 --no-tests，但不推荐。")
    ok("回归测试全绿。")


# ============================ [7] 打包 ============================
def do_build(vpy, clean):
    step(7, "PyInstaller 打包单文件 exe")
    for f in (SPEC, ENTRY):
        if not f.is_file():
            die(f"缺少打包配置 {f}。", "处理办法：确认 build\\ 目录完整（lightnovel.spec / entry.py）。")

    work = BUILD_DIR / "pyi-work"
    if clean and work.exists():
        info(f"清理上次构建缓存 {work}")
        shutil.rmtree(work, ignore_errors=True)

    args = [str(vpy), "-m", "PyInstaller", str(SPEC),
            "--noconfirm", "--distpath", str(DIST_DIR), "--workpath", str(work)]
    if clean:
        args.append("--clean")
    t0 = time.time()
    r = run(args)
    if r.returncode != 0:
        die("PyInstaller 打包失败（上面有详细报错）。",
            """
            常见原因：
              * 某个子模块漏进 spec 的 hiddenimports —— 按报错补进去；
              * 杀毒软件拦截写入 dist\\ —— 加白名单或临时关闭；
              * dist\\LightNovel.exe 正被旧进程占用 —— 关掉它再重跑。
            """)
    ok(f"打包完成，耗时 {time.time() - t0:.1f}s")
    return DIST_DIR / f"{APP_NAME}.exe"


def verify_exe(exe):
    if not exe.is_file():
        die(f"没找到产物 {exe}。")
    size = exe.stat().st_size
    if size < MIN_EXE_BYTES:
        die(f"产物体积异常（{size / 1048576:.1f} MB，低于 {MIN_EXE_BYTES / 1048576:.0f} MB），疑似打包不完整。")
    with open(exe, "rb") as fh:
        if fh.read(2) != b"MZ":
            die("产物不是合法的 Windows 可执行文件（缺 MZ 头）。")
    ok(f"产物校验通过：{exe}  ({size / 1048576:.1f} MB)")

    rc, out = capture([str(exe), "--version"])
    if rc != 0:
        die(f"产物自检失败：`{exe.name} --version` 退出码 {rc}。",
            "处理办法：删掉 dist\\ 后重跑；仍失败则检查 spec 的 hiddenimports。")
    ok(f"产物可正常自举（版本 {out.splitlines()[0] if out else '?'}）")


# ============================ [8] cloudflared ============================
def _cf_asset():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return CF_ASSETS["arm64"]
    if machine in ("x86", "i386", "i686"):
        return CF_ASSETS["x86"]
    return CF_ASSETS["amd64"]


def _valid_pe(path, min_bytes):
    try:
        if Path(path).stat().st_size < min_bytes:
            return False
        with open(path, "rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def find_local_cloudflared():
    for name in ("cloudflared", "cloudflared.exe"):
        p = shutil.which(name)
        if p and _valid_pe(p, MIN_CF_BYTES):
            return p
    home = Path.home()
    cands = [
        home / ".workbuddy" / "bin" / "cloudflared.exe",
        home / "cloudflared" / "cloudflared.exe",
        home / "scoop" / "shims" / "cloudflared.exe",
        Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
        Path("C:/Program Files/cloudflared/cloudflared.exe"),
    ]
    for c in cands:
        if _valid_pe(c, MIN_CF_BYTES):
            return str(c)
    return None


def _download(url, dest):
    """下载到 dest，带百分比进度；失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "lightnovel-build"})
    with urllib.request.urlopen(req, timeout=90) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got, mark = 0, 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total and got - mark >= total // 10:
                mark = got
                log(f"      … {got * 100 // total}%  ({got / 1048576:.1f}/{total / 1048576:.1f} MB)")
    return got


def do_cloudflared(args, dist):
    step(8, "准备 cloudflared.exe（公网隧道用，缺失自动补齐）")
    if args.no_cloudflared:
        warn("已按 --no-cloudflared 跳过；分发到新机器后需自行安装 cloudflared。")
        return None

    target = dist / "cloudflared.exe"
    if _valid_pe(target, MIN_CF_BYTES):
        ok(f"dist\\cloudflared.exe 已存在（{target.stat().st_size / 1048576:.1f} MB），复用。")
        return target

    local = find_local_cloudflared()
    if local:
        info(f"复用本机已有的 cloudflared：{local}")
        shutil.copy2(local, target)
        if _valid_pe(target, MIN_CF_BYTES):
            ok(f"已复制到 {target}")
            return target
        warn("复制结果异常，改为联网下载。")

    asset = _cf_asset()
    tmp = target.with_suffix(".exe.part")
    errors = []
    for tpl in CF_MIRRORS:
        url = tpl.format(asset=asset)
        info(f"下载 {url}")
        try:
            got = _download(url, tmp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{url} -> {exc}")
            warn(f"失败：{exc}")
            tmp.unlink(missing_ok=True)
            continue
        if not _valid_pe(tmp, MIN_CF_BYTES):
            errors.append(f"{url} -> 下载内容不是有效程序（{got} 字节）")
            warn("下载内容不像合法的 Windows 程序，换下一个源。")
            tmp.unlink(missing_ok=True)
            continue
        os.replace(tmp, target)
        ok(f"已下载 cloudflared.exe（{target.stat().st_size / 1048576:.1f} MB）")
        return target

    warn("cloudflared 全部下载源均失败，exe 仍可正常打包（仅公网隧道不可用）。")
    for e in errors:
        log(f"      - {e}")
    info("补救（任选）：")
    info("  * 手动下载 cloudflared-windows-amd64.exe 放到 dist\\ 并改名 cloudflared.exe；")
    info("  * 或 winget install Cloudflare.cloudflared（装完本脚本会自动复用）；")
    info("  * 或在当前窗口设置代理后重跑：set HTTPS_PROXY=http://127.0.0.1:12130")
    return None


# ============================ [9] 分发包 ============================
HELP_TXT = """LightNovel —— 个人轻小说库工具（Windows 单文件版）
================================================================

【这是什么】
  一个 exe 打包了全部功能：OPDS 1.2 书源、目录监控 + GitHub / F 盘同步、
  图形控制面板。目标机器无需安装 Python。

【怎么用】
  1. 双击 LightNovel.exe            —— 打开图形控制面板（推荐）
  2. 命令行（在本目录开 cmd）：
       LightNovel.exe opds --port 8080     启动 OPDS 书源
       LightNovel.exe sync                 同步 + 持续监控
       LightNovel.exe sync --once          只同步一次后退出
       LightNovel.exe mirror --status      查看 D 盘 / F 盘差异（只读）
       LightNovel.exe tunnel-setup         固定域名隧道配置向导
       LightNovel.exe --help               查看全部命令

【书库放哪】
  默认根目录 D:\\Light-Novel，书放在 D:\\Light-Novel\\轻小说\\{已完结,未完结}\\。
  若书库在别处，设一次环境变量即可（用户级，永久生效）：
       setx LN_ROOT "E:\\我的轻小说"
       setx LN_F_ROOT "G:\\LightNovel"        :: F 盘镜像目标，可选
  只临时用就在同一个 cmd 窗口里 set。运行状态写在 <根目录>\\.autosync\\。

【公网隧道（可选）】
  本目录已附带 cloudflared.exe，与 exe 放同级即会被自动找到，无需另装。
  首次使用先跑一次：LightNovel.exe tunnel-setup

【外部依赖】
  git —— 仅 GitHub 同步需要，需自行安装并加入 PATH；只用书源 / F 盘镜像可不装。
  端口 8080 —— OPDS 默认端口，可用 --port 改。
"""


def _pyproject_version():
    try:
        text = PYPROJECT.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("version"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return "0.0.0"


def do_bundle(exe, cf, enabled):
    step(9, "组装可分发包")
    if not enabled:
        info("未指定 --bundle，跳过。要生成分发包：python build\\build_exe.py --bundle")
        return None

    version = _pyproject_version()
    out = DIST_DIR / f"{APP_NAME}-{version}-win64"
    if out.exists():
        info(f"重建已有分发目录 {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    shutil.copy2(exe, out / exe.name)
    if cf is not None and Path(cf).is_file():
        shutil.copy2(cf, out / "cloudflared.exe")
    note = out / "使用说明.txt"
    note.write_text(HELP_TXT, encoding="utf-8-sig")     # 带 BOM，记事本不乱码

    bundle_files = sorted(p.name for p in out.iterdir())
    ok(f"分发目录：{out}")
    for name in bundle_files:
        log(f"      - {name}")

    zip_base = DIST_DIR / out.name
    archive = shutil.make_archive(str(zip_base), "zip", root_dir=str(out))
    ok(f"压缩包：{archive}  ({Path(archive).stat().st_size / 1048576:.1f} MB)")
    return Path(archive)


# ============================ 主流程 ============================
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="build_exe.py",
        description="LightNovel 一键打包（自举、缺件自动联网补齐）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python build\\build_exe.py --bundle\n"
               "  python build\\build_exe.py --clean --no-tests\n"
               "  python build\\build_exe.py --python \"C:\\Python313\\python.exe\"\n",
    )
    ap.add_argument("--python", metavar="PATH", help="指定基础 Python 解释器（需带 tkinter）")
    ap.add_argument("--clean", action="store_true", help="构建前清理 build/pyi-work 缓存")
    ap.add_argument("--no-tests", action="store_true", help="跳过回归测试")
    ap.add_argument("--no-cloudflared", action="store_true", help="不准备 cloudflared.exe")
    ap.add_argument("--bundle", action="store_true", help="额外组装 dist\\LightNovel-<版本>-win64\\ 与 zip")
    ap.add_argument("--upgrade", action="store_true", help="强制升级 PyInstaller 后再打包")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    log("=" * 68)
    log("  LightNovel 一键打包  ——  自举 / 缺件自动补齐")
    log(f"  项目根目录：{ROOT}")
    log(f"  平台：{platform.platform()}   架构：{platform.machine()}")
    if "WINDOWS" not in platform.platform().upper() and os.name != "nt":
        die("本脚本只面向 Windows（PyInstaller 需在目标平台上构建）。")
    log("=" * 68)

    base = do_pick_python(args)
    vpy = do_venv(base)
    do_pip(vpy)
    do_pyinstaller(vpy, upgrade=args.upgrade)
    do_lint(vpy)
    do_tests(vpy, args.no_tests)
    exe = do_build(vpy, args.clean)
    verify_exe(exe)
    cf = do_cloudflared(args, DIST_DIR)
    archive = do_bundle(exe, cf, args.bundle)

    log()
    log("=" * 68)
    log("  打包成功")
    log("=" * 68)
    log(f"  可执行文件 : {exe}")
    log(f"  cloudflared: {cf if cf else '（未准备，公网隧道需另装）'}")
    if archive:
        log(f"  分发包     : {archive}")
    log()
    log("  下一步：双击 exe 打开图形面板；或 build\\build_exe.bat --bundle 出可分发包。")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
