#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一命令行入口。

用法：
    python -m lightnovel                        启动图形控制面板（默认）
    python -m lightnovel ui                     同上
    python -m lightnovel opds   [参数…]         OPDS 书源服务（--port/--tunnel/--no-qr/…）
    python -m lightnovel sync   [参数…]         GitHub 同步与监控（--once/--status/--opds/…）
    python -m lightnovel mirror [参数…]         D 盘 → F 盘镜像（--status/--dry-run/--no-delete）
    python -m lightnovel tunnel-setup [参数…]   Cloudflare 固定域名隧道向导

各子命令的完整参数请加 ``--help``，例如 ``python -m lightnovel opds --help``。
"""
import sys

USAGE = """Light-Novel 书库工具

用法：python -m lightnovel [子命令] [参数…]

子命令：
  ui            启动图形控制面板（不带子命令时默认执行）
  opds          OPDS 书源服务（手机阅读器订阅）
  sync          GitHub 同步 / 目录监控
  mirror        D 盘 → F 盘网盘镜像
  tunnel-setup  Cloudflare 固定域名隧道配置向导

示例：
  python -m lightnovel ui
  python -m lightnovel opds --port 8080 --tunnel named
  python -m lightnovel sync --once
  python -m lightnovel mirror --status
  python -m lightnovel tunnel-setup --name ln-opds

加 --help 查看某个子命令的完整参数，例如：
  python -m lightnovel opds --help
"""

# 子命令 -> (模块路径, 是否转发到该模块的 main)
_COMMANDS = ("ui", "opds", "sync", "sync-lightnovel", "opds-server",
             "mirror", "tunnel-setup", "tunnel", "tunnel-setup-wizard")


def _pipe_utf8():
    """stdout / stderr 是**管道**时强制 UTF-8。

    控制面板用 ``PIPE`` 抓子进程输出、并按 UTF-8 解码；而 Python 对管道默认走系统
    locale（简体中文 Windows = cp936/GBK）→ 子进程每句中文都解不出来，日志里整片
    变成 ``◆``。真实控制台（``isatty()``）**不动**：cmd 的代码页是 cp936，硬改成
    UTF-8 反而花屏。

    为什么写在进程内、而不是给子进程设 ``PYTHONIOENCODING``：实测 PyInstaller 打出来
    的 exe **不认**那个环境变量（管道里吐的仍是 GBK），只有进程内 reconfigure 对
    「源码运行」和「exe 运行」两种方式同时有效。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 不支持 reconfigure / 已关闭的流：保持原样，别影响主流程


def _dispatch(cmd, argv):
    """把 argv 转成目标模块 main() 期望的 sys.argv 后调用。"""
    if cmd == "ui":
        from .ui import main as ui_main
        return ui_main(argv)

    if cmd in ("opds", "opds-server"):
        from .opds import server as mod
        sys.argv = ["lightnovel opds"] + argv
        mod.main()
        return 0

    if cmd in ("sync", "sync-lightnovel"):
        from .sync import monitor as mod
        sys.argv = ["lightnovel sync"] + argv
        mod.main()
        return 0

    if cmd == "mirror":
        from .sync import monitor as mod
        flags = []
        if "--status" in argv:
            flags.append("--mirror-status")
        else:
            flags.append("--mirror-f")
            if "--dry-run" in argv:
                flags.append("--mirror-dry-run")
            if "--no-delete" in argv:
                flags.append("--mirror-no-delete")
        sys.argv = ["lightnovel mirror"] + flags
        mod.main()
        return 0

    if cmd in ("tunnel-setup", "tunnel", "tunnel-setup-wizard"):
        from . import tunnel_setup as mod
        sys.argv = ["lightnovel tunnel-setup"] + argv
        return mod.main()

    print("未知子命令：%s\n" % cmd, file=sys.stderr)
    print(USAGE)
    return 2


def main(argv=None):
    _pipe_utf8()          # 必须在任何输出之前：管道下钉死 UTF-8（面板按 UTF-8 解码）
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        return _dispatch("ui", [])
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        from . import __version__
        print(__version__)
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd not in _COMMANDS:
        print("未知子命令：%s\n" % cmd, file=sys.stderr)
        print(USAGE)
        return 2
    return _dispatch(cmd, rest)


if __name__ == "__main__":
    sys.exit(main())
