#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Light-Novel GitHub 自动同步与监控工具
====================================
功能：
  1. 初始化 / 校验 Git 仓库与远程配置（已存在则直接复用）
  2. 将指定书籍（文件夹）从百度网盘下载目录复制到 Light-Novel 目录（增量、可重复）
  3. 自动 git add / commit / push（非快进时自动 rebase 后重试）
  4. 后台实时监控 Light-Novel 目录，发现新增 / 修改 / 删除文件即自动同步
  5. 完整的日志、错误处理、单实例锁，保证可重复运行

用法：
  python sync_lightnovel.py                  # 复制书籍 + 提交推送 + 持续后台监控
  python sync_lightnovel.py --once           # 复制书籍 + 提交推送一次，然后退出
  python sync_lightnovel.py --monitor-only   # 不复制书籍，仅提交当前状态并持续监控
  python sync_lightnovel.py --init           # 仅初始化 / 校验仓库与远程配置
  python sync_lightnovel.py --status         # 查看仓库状态与监控快照
"""

import os
import sys
import time
import shutil
import subprocess
import logging
import argparse
from datetime import datetime

# ============================ 配置区 ============================
SOURCE_DIR    = r"D:\BaiduNetdiskDownload"
TARGET_DIR    = r"D:\Light-Novel"
BOOK_NAME     = "线上游戏的老婆不可能是女生？"

# 目标子目录（相对于 TARGET_DIR）。
# 默认按需求直接放到 Light-Novel 根目录下；
# 若想归入“轻小说”分类，改为：  TARGET_SUBDIR = os.path.join("轻小说", BOOK_NAME)
TARGET_SUBDIR = BOOK_NAME

REPO_URL      = "https://github.com/SloaAya/Light-Novel.git"
REMOTE        = "origin"
BRANCH        = "main"
GIT_BIN       = "git"          # 若 git 不在 PATH，可改为完整路径，如 r"C:\Program Files\Git\bin\git.exe"

MONITOR_INTERVAL = 5           # 监控轮询间隔（秒）
SETTLE_TIME      = 3           # 判定文件“已写完、稳定”的等待时间（秒）
MAX_SETTLE_WAIT  = 180         # 单轮最多等待稳定的时间（秒），超时则强制同步

LOG_DIR       = os.path.join(TARGET_DIR, ".autosync")
LOG_FILE      = os.path.join(LOG_DIR, "sync.log")

# 可选：GitHub Personal Access Token（默认留空，使用系统凭据管理器 / SSH）。
# 若设置，推送与拉取时会临时将其注入远程 URL，避免交互式输入。
# ⚠ 令牌等同密码：本文件本身会被同步进仓库，请务必保持为空，
#    改用 Git Credential Manager（Windows 默认）或 SSH 密钥做认证。
PAT_TOKEN     = ""
# ===============================================================


# ---------------------------- 日志 ----------------------------
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = logging.getLogger("sync")


# ---------------------------- Git 封装 ----------------------------
def run_git(args, cwd=TARGET_DIR, check=True):
    """执行一条 git 命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            [GIT_BIN, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.error("未找到 git 可执行文件，请确认已安装 Git 并在 PATH 中（或修改 GIT_BIN）。")
        return (127, "", "git not found")
    out, err = proc.stdout or "", proc.stderr or ""
    if check and proc.returncode != 0:
        log.warning("git %s 返回 %d: %s %s", " ".join(args), proc.returncode, out.strip(), err.strip())
    return (proc.returncode, out, err)


def git_available():
    rc, _, _ = run_git(["--version"], check=False)
    return rc == 0


# ---------------------------- 仓库初始化 / 复用 ----------------------------
def ensure_repo():
    git_dir = os.path.join(TARGET_DIR, ".git")
    if not os.path.isdir(git_dir):
        rc, _, _ = run_git(["init", "-b", BRANCH], check=False)
        if rc != 0:
            log.error("git init 失败，无法继续。")
            return False
        log.info("已在 %s 初始化 Git 仓库（分支 %s）", TARGET_DIR, BRANCH)
    else:
        log.info("检测到已存在的 Git 仓库，直接复用。")
    # 避免 Windows 上“可疑所有权”报错
    run_git(["config", "--global", "--add", "safe.directory", TARGET_DIR], check=False, cwd=None)
    # 让中文路径在日志 / 状态中可读
    run_git(["config", "--local", "core.quotepath", "false"], check=False)
    return True


def ensure_remote():
    rc, out, _ = run_git(["remote", "get-url", REMOTE], check=False)
    if rc != 0:
        run_git(["remote", "add", REMOTE, REPO_URL], check=False)
        log.info("已添加远程 %s -> %s", REMOTE, REPO_URL)
    else:
        cur = out.strip()
        if cur != REPO_URL:
            run_git(["remote", "set-url", REMOTE, REPO_URL], check=False)
            log.warning("远程 URL 不一致，已更新为 %s", REPO_URL)
        else:
            log.info("远程 %s 已配置：%s", REMOTE, cur)
    # 若远程已存在该分支，设置上游跟踪
    rc, out, _ = run_git(["ls-remote", "--heads", REPO_URL, BRANCH], check=False)
    if rc == 0 and BRANCH in out:
        run_git(["branch", "--set-upstream-to", f"{REMOTE}/{BRANCH}", BRANCH], check=False)
        log.info("已设置上游跟踪 %s/%s", REMOTE, BRANCH)


def auth_url(url):
    if not PAT_TOKEN:
        return url
    if url.startswith("https://"):
        return url.replace("https://", f"https://{PAT_TOKEN}@", 1)
    return url


def get_remote_url():
    rc, out, _ = run_git(["remote", "get-url", REMOTE], check=False)
    return out.strip() if rc == 0 else REPO_URL


def set_remote_url(url):
    run_git(["remote", "set-url", REMOTE, url], check=False)


def push_with_retry():
    """推送，遇到非快进冲突时自动 pull --rebase 后重试，最多 3 次。"""
    original_url = get_remote_url() if PAT_TOKEN else None
    if PAT_TOKEN:
        set_remote_url(auth_url(REPO_URL))
    try:
        for attempt in range(1, 4):
            cmd = ["push"] + (["-u", REMOTE, BRANCH] if attempt == 1 else [REMOTE, BRANCH])
            rc, out, err = run_git(cmd, check=False)
            if rc == 0:
                log.info("推送成功。")
                return True
            combined = out + err
            if any(k in combined for k in ("rejected", "non-fast-forward", "fetch first", "not fast-forward")):
                log.warning("推送被拒绝（存在分叉），尝试 pull --rebase 后重试（第 %d 次）", attempt)
                rc2, o2, e2 = run_git(["pull", "--rebase", "--autostash", REMOTE, BRANCH], check=False)
                if rc2 != 0:
                    log.error("rebase 失败，放弃本次推送：%s %s", o2.strip(), e2.strip())
                    return False
                continue
            else:
                log.error("推送失败：%s %s", out.strip(), err.strip())
                if attempt < 3:
                    time.sleep(3)
                    continue
                return False
        return False
    finally:
        if PAT_TOKEN and original_url:
            set_remote_url(original_url)


def git_commit_push(message):
    run_git(["add", "-A"], check=False)
    rc, _, _ = run_git(["diff", "--cached", "--quiet"], check=False)
    if rc == 0:
        log.info("没有需要提交的变更，跳过提交。")
        return False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rc, _, err = run_git(["commit", "-m", f"{message}\n\n自动同步于 {ts}"], check=False)
    if rc != 0:
        log.error("提交失败：%s", err.strip())
        return False
    log.info("已提交：%s", message)
    ok = push_with_retry()
    if not ok:
        log.warning("提交已完成，但推送未成功，请检查网络 / 凭据后重试。")
    return ok


# ---------------------------- 文件复制 ----------------------------
def smart_copy():
    """将源书籍文件夹增量复制到目标目录（仅复制缺失或变化的文件）。返回复制文件数。"""
    src = os.path.join(SOURCE_DIR, BOOK_NAME)
    dst = os.path.join(TARGET_DIR, TARGET_SUBDIR)
    if not os.path.isdir(src):
        log.error("源目录不存在：%s", src)
        return 0
    copied = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
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
def snapshot_dir():
    """扫描 TARGET_DIR（排除 .git 与日志目录），返回 {相对路径: (size, mtime)}。"""
    snap = {}
    skip = {".git", os.path.basename(LOG_DIR)}
    for root, dirs, files in os.walk(TARGET_DIR):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                rel = os.path.relpath(fp, TARGET_DIR).replace(os.sep, "/")
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
def acquire_lock():
    lock = os.path.join(LOG_DIR, "monitor.lock")
    try:
        with open(lock, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)  # 进程仍在运行则抛异常
        log.warning("已有监控进程 pid=%d 在运行，本实例退出。", pid)
        return False
    except (OSError, ValueError, FileNotFoundError):
        pass  # 锁文件不存在或进程已死 -> 可获取
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock():
    lock = os.path.join(LOG_DIR, "monitor.lock")
    try:
        os.remove(lock)
    except OSError:
        pass


# ---------------------------- 监控主循环 ----------------------------
def monitor_loop():
    if not acquire_lock():
        return
    log.info("开始后台实时监控 %s（每 %d 秒轮询一次，Ctrl+C 退出）", TARGET_DIR, MONITOR_INTERVAL)
    prev = snapshot_dir()
    try:
        while True:
            time.sleep(MONITOR_INTERVAL)
            try:
                cur = snapshot_dir()
                if cur == prev:
                    continue
                # 检测到变化 -> 等待文件写完（稳定）
                last = cur
                waited = 0
                while True:
                    time.sleep(SETTLE_TIME)
                    now = snapshot_dir()
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
                git_commit_push(
                    f"auto-sync: +{len(added)} ~{len(modified)} -{len(removed)}"
                )
                prev = cur
            except Exception as exc:  # 单次轮询出错不应中断监控
                log.error("监控轮询出错：%s", exc)
    except KeyboardInterrupt:
        log.info("收到中断信号，停止监控。")
    finally:
        release_lock()


# ---------------------------- 状态查看 ----------------------------
def show_status():
    rc, out, _ = run_git(["status", "-s"], check=False)
    log.info("===== git status =====\n%s", out.strip() or "(干净)")
    snap = snapshot_dir()
    log.info("当前监控快照文件数：%d", len(snap))
    log.info("日志文件：%s", LOG_FILE)


# ---------------------------- 主流程 ----------------------------
def main():
    parser = argparse.ArgumentParser(description="Light-Novel GitHub 自动同步与监控工具")
    parser.add_argument("--once", action="store_true", help="复制书籍 + 提交推送一次后退出")
    parser.add_argument("--monitor-only", action="store_true", help="不复制书籍，仅提交当前状态并持续监控")
    parser.add_argument("--init", action="store_true", help="仅初始化 / 校验仓库与远程配置")
    parser.add_argument("--status", action="store_true", help="查看仓库状态与监控快照")
    args = parser.parse_args()

    setup_logging()

    if not git_available():
        log.error("Git 不可用，程序无法运行。")
        sys.exit(1)

    if args.status:
        ensure_repo()
        show_status()
        return

    if not ensure_repo():
        sys.exit(1)
    ensure_remote()

    if args.init:
        log.info("仓库与远程配置校验完成。")
        show_status()
        return

    if args.once:
        if not args.monitor_only:
            smart_copy()
        git_commit_push("sync: 初始/手动同步")
        log.info("--once 完成。")
        return

    # 默认模式：复制 + 提交推送 + 持续监控
    if not args.monitor_only:
        smart_copy()
    git_commit_push("sync: 初始同步")
    monitor_loop()


if __name__ == "__main__":
    main()
