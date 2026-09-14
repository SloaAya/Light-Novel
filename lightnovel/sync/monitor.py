#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控与主流程：种子复制、目录快照/变更检测、单实例锁、监控循环、状态查看、命令行入口。"""

import argparse
import logging
import os
import shutil
import sys
import time

from ..paths import (
    BOOK_NAME,
    CATEGORY_DIRS,
    CATEGORY_DONE,
    CATEGORY_ONGOING,
    ENABLE_SEED_COPY,
    LIGHT_NOVEL_DIR,
    LOG_DIR,
    LOG_FILE,
    MAX_SETTLE_WAIT,
    MIRROR_HTML_FILE,
    MIRROR_LOG_FILE,
    MIRROR_REPORT_FILE,
    MONITOR_INTERVAL,
    OPDS_BIND,
    OPDS_PORT,
    SETTLE_TIME,
    SOURCE_DIR,
    TARGET_DIR,
    TARGET_SUBDIR,
    WATCH_DIRS,
)
from .gitops import (
    EXCLUDE_FILE_NAMES,
    ensure_remote,
    ensure_repo,
    git_available,
    list_books,
    run_git,
    setup_logging,
)
from .mirror import (
    mirror_query,
    perform_sync,
    sync_to_f,
    write_mirror_dashboard,
)

log = logging.getLogger("sync")

# ---------------------------- 文件复制（种子） ----------------------------
def smart_copy():
    """将源书籍文件夹增量复制到目标分类目录（仅复制缺失或变化的文件）。返回复制文件数。"""
    src = os.path.join(SOURCE_DIR, BOOK_NAME)
    dst = os.path.join(TARGET_DIR, TARGET_SUBDIR)
    if not os.path.isdir(src):
        log.error("源目录不存在：%s", src)
        return 0
    copied = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            if f.lower() in EXCLUDE_FILE_NAMES:
                continue  # 系统垃圾文件不复制进仓库
            sf = os.path.join(root, f)
            rel = os.path.relpath(sf, src)
            tf = os.path.join(dst, rel)
            need = True
            if os.path.exists(tf):
                ss = os.stat(sf)
                ts = os.stat(tf)
                if ss.st_size == ts.st_size and abs(ss.st_mtime - ts.st_mtime) < 2:
                    need = False
            if need:
                os.makedirs(os.path.dirname(tf), exist_ok=True)
                shutil.copy2(sf, tf)
                copied += 1
                log.info("已复制：%s", rel)
    log.info("复制完成，新增 / 更新文件数：%d", copied)
    return copied


# ---------------------------- 目录快照 / 变更检测 ----------------------------
def snapshot_dir(roots=None):
    """扫描 roots（默认：整个 轻小说 目录，排除 .git 与日志目录）。
    返回 {相对 LIGHT_NOVEL_DIR 的路径: (size, mtime)}，键以分类名前缀区分，避免重名碰撞。"""
    if roots is None:
        roots = [LIGHT_NOVEL_DIR]
    snap = {}
    skip = {".git", os.path.basename(LOG_DIR)}
    for root in roots:
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                if f.lower() in EXCLUDE_FILE_NAMES:
                    continue  # 系统垃圾文件不参与变更检测（避免 Explorer 生成 desktop.ini 就触发同步）
                fp = os.path.join(r, f)
                try:
                    st = os.stat(fp)
                    rel = os.path.relpath(fp, LIGHT_NOVEL_DIR).replace(os.sep, "/")
                    snap[rel] = (st.st_size, int(st.st_mtime))
                except OSError:
                    pass
    return snap


def detect_changes(prev, cur):
    added = [k for k in cur if k not in prev]
    removed = [k for k in prev if k not in cur]
    modified = [k for k in cur if k in prev and cur[k] != prev[k]]
    return added, modified, removed


# ---------------------------- 单实例锁 ----------------------------
def _pid_alive(pid):
    """判断 pid 是否指向一个仍在运行的进程。

    ⚠ 绝不能用 `os.kill(pid, 0)` 做探测：Windows 上 CPython 会走
    `OpenProcess(PROCESS_ALL_ACCESS)` + `TerminateProcess(handle, 0)` —— 那是**终止**
    而不是探测（实测目标进程 exit code = 3221225794）。后果是启动第二个实例会把正在
    运行的监控实例杀掉。改用只读方式：
      - Windows：`OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(h, 0)`
                 返回 WAIT_TIMEOUT 表示仍在运行；不需要也不请求终止权限。
      - 其他平台：`os.kill(pid, 0)` 是标准且安全的「信号 0 探测」。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return False
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            return False
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False        # 打不开 = 进程不存在（或权限不足，按已死处理以便接管）
        try:
            rc = k32.WaitForSingleObject(handle, 0)
            return rc == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True             # 进程存在，只是没权限给它发信号
    except OSError:
        return False


def lock_path():
    """单实例锁文件的路径 —— 全项目唯一真源。

    特意由 ``LOG_DIR`` **现算**，而不是直接用 ``paths.LOCK_FILE``：测试会把
    ``LOG_DIR`` 换成临时目录，只有现算才跟得上；面板也复用本函数，避免出现
    「监控写 A、面板读 B」两处各写一份文件名的隐患。
    """
    return os.path.join(LOG_DIR, "monitor.lock")


def acquire_lock():
    lock = lock_path()
    pid = None
    try:
        with open(lock, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        pid = None               # 锁文件不存在或内容损坏 -> 可直接获取
    if pid is not None:
        if _pid_alive(pid):
            log.warning("已有监控进程 pid=%d 在运行，本实例退出。", pid)
            return False
        log.info("发现失效的锁文件（pid=%d 已退出），直接接管。", pid)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(lock_path())
    except OSError:
        pass


# ---------------------------- 监控主循环 ----------------------------
def monitor_loop():
    """实时监控循环。

    ⚠ 锁**不由本函数管理**：``main()`` 在初始同步之前就先占锁（那一步要跑全量
    F 盘镜像 + git 推送，实测两分钟起步，期间没锁会让面板状态灯一直是红的、
    单实例保护也是空的）。本函数只负责循环本身，锁由 main 的 try/finally 收尾。
    """
    log.info("开始后台实时监控 %s（每 %d 秒轮询一次，Ctrl+C 退出）", "、".join(WATCH_DIRS), MONITOR_INTERVAL)
    prev = snapshot_dir(WATCH_DIRS)
    try:
        while True:
            time.sleep(MONITOR_INTERVAL)
            try:
                cur = snapshot_dir(WATCH_DIRS)
                if cur == prev:
                    continue
                # 检测到变化 -> 等待文件写完（稳定）
                last = cur
                waited = 0
                while True:
                    time.sleep(SETTLE_TIME)
                    now = snapshot_dir(WATCH_DIRS)
                    if now == last:
                        break
                    last = now
                    waited += SETTLE_TIME
                    if waited >= MAX_SETTLE_WAIT:
                        log.warning("等待文件稳定超时（%d 秒），强制同步。", MAX_SETTLE_WAIT)
                        break
                cur = last
                added, modified, removed = detect_changes(prev, cur)
                log.info("检测到变更：新增 %d，修改 %d，删除 %d", len(added), len(modified), len(removed))
                perform_sync(
                    f"auto-sync: +{len(added)} ~{len(modified)} -{len(removed)}"
                )
                # 同步过程可能改动工作区（如清理远程多余文件时的 rebase + git rm），
                # 重新快照，避免下一轮把同步自身的改动误判为新的外部变更
                prev = snapshot_dir(WATCH_DIRS)
            except Exception as exc:  # 单次轮询出错不应中断监控
                log.error("监控轮询出错：%s", exc)
    except KeyboardInterrupt:
        log.info("收到中断信号，停止监控。")


# ---------------------------- OPDS 书源（手机端） ----------------------------
def start_opds_background(port=OPDS_PORT, bind=OPDS_BIND):
    """在后台线程启动 OPDS 书源服务，返回 (httpd, thread)；失败返回 (None, None)。
    服务失败只告警，绝不影响同步 / 监控主流程。"""
    try:
        from ..opds import server as opds_server
    except Exception as exc:
        log.error("无法加载 lightnovel.opds.server（OPDS 书源未启动）：%s", exc)
        return None, None
    try:
        httpd, t = opds_server.start_background(port=port, bind=bind)
        ip = opds_server.local_ip()
        lib = opds_server.get_library(force=True)
        n_books = sum(len(b) for b in lib.values())
        n_vols = sum(1 for _ in opds_server.all_vols(lib))
        log.info("OPDS 书源已随监控启动：http://%s:%d/ （%d 部 / %d 卷）", ip, port, n_books, n_vols)
        if not (opds_server.AUTH_USER or opds_server.AUTH_PASS):
            log.warning("OPDS 当前免密访问；如需外网访问请先设置 LN_OPDS_USER / LN_OPDS_PASS 环境变量。")
        return httpd, t
    except Exception as exc:
        log.warning("OPDS 书源启动失败（不影响同步）：%s", exc)
        return None, None


# ---------------------------- 状态查看 ----------------------------
def show_status():
    rc, out, _ = run_git(["status", "-s"], check=False)
    log.info("===== git status =====\n%s", out.strip() or "(干净)")
    snap = snapshot_dir()
    log.info("轻小说 当前监控快照文件数：%d", len(snap))
    log.info("已完结 %d 本 / 未完结 %d 本", len(list_books(CATEGORY_DIRS[CATEGORY_DONE])),
             len(list_books(CATEGORY_DIRS[CATEGORY_ONGOING])))
    log.info("日志文件：%s", LOG_FILE)


# ---------------------------- F 盘镜像状态（查） ----------------------------
def show_mirror_status():
    """查：打印 D 盘与 F 盘的差异报告（只读，不改动任何文件）。"""
    rep = mirror_query()
    c = rep["counts"]
    log.info("===== D 盘 → F 盘 镜像差异报告 =====")
    log.info("数据源：%s", rep["source_root"])
    log.info("备份目标：%s（%s）", rep["target_root"],
             "已挂载" if rep["f_mounted"] else "未挂载 / 不可用")
    if not rep["f_mounted"]:
        log.warning("F 盘不可用，无法比对。请检查 CloudDrive2 是否已挂载。")
        return rep
    for cat, plan in rep["categories"].items():
        pc = plan["counts"]
        log.info("  [%s] D 盘 %d 个 / F 盘 %d 个 → 增 %d、改 %d、删 %d（其中清单外 %d）、"
                 "孤儿子目录 %d、保护 %d、一致 %d、冲突 %d",
                 cat, pc["src_total"], pc["dst_total"], pc["add"], pc["update"],
                 pc["delete"], pc.get("orphan", 0), pc.get("orphan_dirs", 0),
                 pc["protected"], pc["unchanged"], pc["conflict"])
    log.info("  合计：增 %d / 改 %d / 删 %d / 一致 %d / 冲突 %d",
             c["add"], c["update"], c["delete"], c["unchanged"], c["conflict"])
    for cat, plan in rep["categories"].items():
        for cf in plan["conflicts"]:
            log.warning("  冲突·%s [%s] %s：%s", cat, cf["kind"], cf["rel"], cf["detail"])
        for item in plan["delete"][:20]:
            if not item["protected"]:
                log.info("  待删·%s：%s", cat, item["rel"])
        if plan["counts"]["delete"] > 20:
            log.info("  …… 待删清单已截断，完整列表见 %s", MIRROR_REPORT_FILE)
    log.info("完整报告：%s", MIRROR_REPORT_FILE)
    log.info("审计日志：%s", MIRROR_LOG_FILE)
    write_mirror_dashboard(rep)
    log.info("可视化看板：%s", MIRROR_HTML_FILE)
    return rep


# ---------------------------- 主流程 ----------------------------
def main():
    parser = argparse.ArgumentParser(description="Light-Novel GitHub 自动同步与监控工具（增强版）")
    parser.add_argument("--once", action="store_true", help="种子复制 + 提交推送一次后退出")
    parser.add_argument("--monitor-only", action="store_true", help="不复制种子，仅同步当前状态并持续监控")
    parser.add_argument("--init", action="store_true", help="仅初始化 / 校验仓库与远程配置")
    parser.add_argument("--status", action="store_true", help="查看仓库状态与监控快照")
    parser.add_argument("--mirror-f", action="store_true",
                        help="执行一次 D 盘 → F 盘的完整镜像（增 / 改 / 删 / 查）后退出")
    parser.add_argument("--mirror-status", action="store_true",
                        help="查看 D 盘与 F 盘的差异报告（只读，不做任何改动）")
    parser.add_argument("--mirror-dry-run", action="store_true",
                        help="配合 --mirror-f：只预演不落盘")
    parser.add_argument("--mirror-no-delete", action="store_true",
                        help="配合 --mirror-f：只做增 / 改，跳过删除")
    parser.add_argument("--opds", action="store_true",
                        help="同步的同时启动 OPDS 书源服务（手机阅读器可订阅）")
    parser.add_argument("--opds-only", action="store_true",
                        help="仅启动 OPDS 书源服务，不做同步与监控")
    parser.add_argument("--opds-port", type=int, default=OPDS_PORT,
                        help=f"OPDS 服务端口（默认 {OPDS_PORT}）")
    args = parser.parse_args()

    setup_logging()

    if not git_available():
        log.error("Git 不可用，程序无法运行。")
        sys.exit(1)

    if args.opds_only:
        try:
            from ..opds import server as opds_server
            opds_server._ensure_logging()
            opds_server.run_service(port=args.opds_port, bind=OPDS_BIND)
        except Exception as exc:
            log.error("OPDS 服务退出：%s", exc)
        return

    if args.status:
        ensure_repo()
        show_status()
        return

    if args.mirror_status:
        show_mirror_status()
        return

    if args.mirror_f:
        sync_to_f(dry_run=args.mirror_dry_run, allow_delete=not args.mirror_no_delete)
        return

    if not ensure_repo():
        sys.exit(1)
    ensure_remote()

    if args.init:
        log.info("仓库与远程配置校验完成。")
        show_status()
        return

    if args.once:
        if ENABLE_SEED_COPY and not args.monitor_only:
            smart_copy()
        perform_sync("sync: 手动/初始同步")
        log.info("--once 完成。")
        return

    # 默认模式：（可选）种子复制 + 提交推送 + 持续监控
    # ⚠ 锁必须在**初始同步之前**拿到：那一步要跑全量 F 盘镜像 + git 推送，实测
    #   两分钟起步。期间若还没写锁，控制面板会一直显示「已停止」（用户看不出它在
    #   干活），而且单实例保护形同虚设 —— 这段时间再启动一个实例，就会两个进程
    #   并发跑同步/镜像/git，互相踩（rebase 撞 git rm 会造成工作区幽灵删除）。
    if not acquire_lock():
        log.error("已有监控实例在运行（锁文件 %s），本实例退出。", lock_path())
        sys.exit(1)
    try:
        if ENABLE_SEED_COPY and not args.monitor_only:
            smart_copy()
        perform_sync("sync: 初始同步")
        if args.opds:
            start_opds_background(args.opds_port)
        monitor_loop()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
