#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Light-Novel GitHub 自动同步与监控工具（增强版）
============================================
功能：
  1. 初始化 / 校验 Git 仓库与远程配置（已存在则直接复用）
  2. 将指定书籍（文件夹）从百度网盘下载目录种子式复制到 轻小说/未完结/ 下（增量、可重复）
  3. 自动 git add / commit / push（大仓库走 SSH + 逐提交分块推送 + 自动重试）
  4. 后台实时监控 D:/Light-Novel/轻小说/已完结 与 未完结 ：
       - 任意外部文件被复制进 已完结 / 未完结 子目录即自动提交并推送到 GitHub
       - 自动刷新 README.md 中对应分类的书名列表
       - 自动镜像到 F:/LightNovel/已完结(未完结)/ （CloudDrive2 网络云盘，失败不阻断推送）
       - 以本地为准：GitHub 上多出来的文件（本地没有的）自动删除
  5. 完整的日志、错误处理、单实例锁，保证可重复运行

目录结构约定：
  D:/Light-Novel/
    轻小说/
      已完结/    <- 已完结书籍（每本一个文件夹）
      未完结/    <- 未完结书籍（每本一个文件夹）
    README.md    <- 由本工具自动维护两个书单区块
    .autosync/   <- 日志与锁（已加入 .gitignore）

用法：
  python sync_lightnovel.py                  # 种子复制 + 提交推送 + 持续后台监控
  python sync_lightnovel.py --once           # 种子复制 + 提交推送一次，然后退出
  python sync_lightnovel.py --monitor-only   # 不复制种子，仅同步当前状态并持续监控
  python sync_lightnovel.py --init           # 仅初始化 / 校验仓库与远程配置
  python sync_lightnovel.py --status         # 查看仓库状态与监控快照
"""

import os
import re
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

# ---- 轻小说分类目录（监控、README、F 盘镜像的核心）----
CATEGORY_DONE    = "已完结"
CATEGORY_ONGOING = "未完结"
LIGHT_NOVEL_DIR  = os.path.join(TARGET_DIR, "轻小说")
CATEGORY_DIRS = {
    CATEGORY_DONE:    os.path.join(LIGHT_NOVEL_DIR, CATEGORY_DONE),
    CATEGORY_ONGOING: os.path.join(LIGHT_NOVEL_DIR, CATEGORY_ONGOING),
}

# 监控只盯这两个分类目录；任一个有改动就同步到 F 盘网盘 + GitHub
WATCH_DIRS = [CATEGORY_DIRS[CATEGORY_DONE], CATEGORY_DIRS[CATEGORY_ONGOING]]
# 种子书籍默认放入“未完结”分类（如需默认放入已完结，改 CATEGORY_DONE 即可）
TARGET_SUBDIR = os.path.join("轻小说", CATEGORY_ONGOING, BOOK_NAME)

# ---- F 盘网络云盘（CloudDrive2 挂载）镜像目录 ----
F_TARGET_ROOT  = r"F:\LightNovel"
F_CATEGORY_DIRS = {
    CATEGORY_DONE:    os.path.join(F_TARGET_ROOT, CATEGORY_DONE),
    CATEGORY_ONGOING: os.path.join(F_TARGET_ROOT, CATEGORY_ONGOING),
}

README_PATH = os.path.join(TARGET_DIR, "README.md")

# 推荐用 SSH：大体积推送在部分代理 / 网络下，HTTPS 上传会被重置（Connection was reset /
# remote end hung up），而 SSH 通常能稳定通过。请先把本机公钥加到 GitHub（见脚本顶部说明）。
# 若坚持用 HTTPS，改回下面这行并配置 PAT_TOKEN 或 Git 凭据管理器（GCM）即可：
#   REPO_URL = "https://github.com/SloaAya/Light-Novel.git"
REPO_URL      = "git@github.com:SloaAya/Light-Novel.git"
# SSH 的 22 端口被封时的兜底地址（走 443）： git@ssh.github.com:443/SloaAya/Light-Novel.git
REMOTE        = "origin"
BRANCH        = "main"
GIT_BIN       = "git"          # 若 git 不在 PATH，可改为完整路径，如 r"C:\Program Files\Git\bin\git.exe"

MONITOR_INTERVAL = 5           # 监控轮询间隔（秒）
SETTLE_TIME      = 3           # 判定文件“已写完、稳定”的等待时间（秒）
MAX_SETTLE_WAIT  = 180         # 单轮最多等待稳定的时间（秒），超时则强制同步
MAX_DELETIONS_GUARD = 100      # 单次提交允许的最大删除文件数（超过则判定为异常，拒绝提交）

LOG_DIR       = os.path.join(TARGET_DIR, ".autosync")
LOG_FILE      = os.path.join(LOG_DIR, "sync.log")

# 可选：GitHub Personal Access Token（默认留空，使用系统凭据管理器 / SSH）。
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
def run_git(args, cwd=TARGET_DIR, check=True, timeout=None):
    """执行一条 git 命令，返回 (returncode, stdout, stderr)。
    - 设置 GIT_TERMINAL_PROMPT=0：无桌面/无 TTY 环境下不会卡在密码提示。
    - 支持 timeout：避免 push/pull 因等待凭据而无期限挂起。
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        # Windows 下避免 subprocess 启动 git.exe 时弹出黑色命令行窗口
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.run(
            [GIT_BIN, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        log.error("git %s 超时（%ds），可能被凭据提示阻塞。", " ".join(args), timeout)
        return (124, "", "timeout")
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
    # 大体积推送（多本 EPUB）调优：提高 postBuffer，并降级为 HTTP/1.1，
    # 可避免部分代理 / 网络环境下 “curl 55 Send failure / 连接被重置” 的瞬时失败。
    run_git(["config", "--local", "http.postBuffer", "524288000"], check=False)
    run_git(["config", "--local", "http.version", "HTTP/1.1"], check=False)
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


# ---------------------------- 推送（错误分类 + 分块 + 重试） ----------------------------
def classify_push_error(out, err):
    """把 push 失败归类为：'auth' | 'nonfastforward' | 'transient' | 'fatal'。"""
    c = (out + err).lower()
    auth_keys = ("authentication failed", "permission denied", "could not read username",
                 "could not read password", "terminal prompts disabled", "access denied",
                 "403", "401")
    nff_keys = ("rejected", "non-fast-forward", "fetch first", "not fast-forward",
                "tip of your current branch is behind")
    trans_keys = ("rpc failed", "send failure", "connection was reset",
                  "connection reset by peer", "unexpected disconnect", "early eof",
                  "the remote end hung up", "failed to connect", "could not resolve",
                  "connection timed out", "connection refused", "broken pipe",
                  "timed out", "reset by peer", "failed to push some refs")
    if any(k in c for k in auth_keys):
        return "auth"
    if any(k in c for k in nff_keys):
        return "nonfastforward"
    if any(k in c for k in trans_keys):
        return "transient"
    return "fatal"


def _push_backoff(attempt, max_attempts, base=5):
    """指数退避等待：5s, 10s, 20s... 上限 60s；最后一次不等待。"""
    if attempt >= max_attempts:
        return
    wait = min(base * (2 ** (attempt - 1)), 60)
    log.info("等待 %d 秒后重试...", wait)
    time.sleep(wait)


def get_unpushed_commits():
    """返回本地有而远程 main 没有的提交（从旧到新排序）。
    先 fetch 再用 FETCH_HEAD 计算，避免个别环境下 origin/main 远程跟踪引用
    未被 fetch 刷新，导致「陈旧计数 / 误判分叉」的问题。"""
    run_git(["fetch", REMOTE, BRANCH], check=False, timeout=600)
    rc, out, _ = run_git(["rev-list", "--reverse", f"FETCH_HEAD..{BRANCH}"], check=False)
    if rc != 0:
        # 退化：改用本地远程跟踪引用估算
        rc, out, _ = run_git(["rev-list", "--reverse", f"{REMOTE}/{BRANCH}..{BRANCH}"], check=False)
        if rc != 0:
            return []
    return [c.strip() for c in out.splitlines() if c.strip()]


def _retry_push(cmd, max_attempts=5, label=""):
    """对单条 push 命令按错误类型自动重试，返回 True/False。
    只处理「超时 / 网络瞬时错误」的重试；auth / 分叉(non-fast-forward) / fatal
    直接返回 False，由调用方负责 pull --rebase 后重新计算范围。（rebase 会改写
    提交 SHA，不能在本函数内用旧 SHA 反复重推，否则陷入分叉死循环。）"""
    tag = f"({label})" if label else ""
    for attempt in range(1, max_attempts + 1):
        rc, out, err = run_git(cmd, check=False, timeout=3600)
        if rc == 0:
            log.info("推送成功%s。", tag)
            return True
        if rc == 124:
            log.warning("推送超时（网络过慢或被重置），重试 %d/%d %s", attempt, max_attempts, tag)
            _push_backoff(attempt, max_attempts)
            continue
        kind = classify_push_error(out, err)
        if kind == "auth":
            log.error("Git 认证失败。请配置 Personal Access Token 或 SSH 密钥，"
                      "或在有桌面的环境中运行以使用 Git 凭据管理器（GCM）。详见脚本顶部说明。")
            return False
        if kind == "nonfastforward":
            log.warning("推送被拒绝（存在分叉）%s", tag)
            return False  # 交给外层 rebase 后重新计算范围再推
        if kind == "transient":
            log.warning("网络瞬时错误（连接被重置 / 断开），重试 %d/%d %s：%s",
                        attempt, max_attempts, tag, (out + err).strip()[:200])
            if attempt < max_attempts:
                _push_backoff(attempt, max_attempts)
                continue
            log.error("多次重试后仍因网络错误推送失败，请检查网络 / 代理后重试。")
            return False
        # fatal / 未知
        log.error("推送失败%s：%s %s", tag, out.strip(), err.strip())
        return False
    return False


def push_with_retry():
    """逐提交分块推送（旧 -> 新），每次都先 fetch 并基于真实远程(FETCH_HEAD)计算待推范围。
    关键：
    - 待推提交均为 FETCH_HEAD(真实远程 tip) 的后代，因此每次只推最旧一个提交都是干净快进、
      单次上传量最小，最不易被网络重置；
    - 若远程被其他写入者(如 OneDrive/CD2 机器人)抢先推进，推送会被拒(non-fast-forward)，
      此时重新 fetch 并把本地提交 rebase 到最新远程之上再继续——既能续传，也不会用旧 SHA 死循环。
    已成功推送部分保留在远程，失败后重跑本程序即可从断点续传。"""
    original_url = get_remote_url() if PAT_TOKEN else None
    if PAT_TOKEN:
        set_remote_url(auth_url(REPO_URL))
    try:
        for _ in range(400):  # 上限保护，防止意外死循环
            commits = get_unpushed_commits()  # 内部已 fetch 并基于 FETCH_HEAD 计算
            if not commits:
                break  # 全部推完
            sha = commits[0]  # 最旧的待推送提交(必然是 FETCH_HEAD 的后代)
            ok = _retry_push(["push", REMOTE, f"{sha}:refs/heads/{BRANCH}"],
                             label=f"块(剩 {len(commits)})")
            if ok:
                continue  # 该提交已上推，下轮重算会自动跳过它
            # 失败：说明远程又被抢先推进。重新 fetch 并把本地提交 rebase 到最新远程之上
            run_git(["fetch", REMOTE, BRANCH], check=False, timeout=600)
            rc2, o2, e2 = run_git(["rebase", "FETCH_HEAD"], check=False, timeout=3600)
            if rc2 != 0:
                log.error("rebase 到最新远程失败（可能存在文件冲突），放弃本次推送：%s %s",
                          o2.strip(), e2.strip())
                run_git(["rebase", "--abort"], check=False)
                return False
            # rebase 后 SHA 已变，检测是否真的有进展，避免停滞死循环
            new_commits = get_unpushed_commits()
            if new_commits and new_commits[0] == sha and len(new_commits) >= len(commits):
                log.error("推送停滞（分叉无法经 rebase 解决），放弃。已推送部分保留在远程，"
                          "修复冲突 / 网络后重跑可续传。")
                return False
            # 否则进入下一轮，用新的 commits 继续
        # 末次确保 tip 与上游跟踪
        _retry_push(["push", "-u", REMOTE, BRANCH], label="tip")
        return True
    finally:
        if PAT_TOKEN and original_url:
            set_remote_url(original_url)


def git_commit(message):
    """提交工作树变更（有变更才提交）。返回 True=成功（含无变更），False=提交失败。
    安全护栏：若暂存区包含 >=MAX_DELETIONS_GUARD 个删除（通常是工作区被意外清空 /
    在错误目录运行 / 磁盘异常），拒绝提交并撤销暂存，防止把大规模误删推上远程。"""
    run_git(["add", "-A"], check=False)
    rc, _, _ = run_git(["diff", "--cached", "--quiet"], check=False)
    if rc == 0:
        log.info("没有需要提交的变更，跳过提交。")
        return True
    rc, names, _ = run_git(["diff", "--cached", "--name-only", "--diff-filter=D"], check=False)
    deletions = len([x for x in names.splitlines() if x])
    if deletions >= MAX_DELETIONS_GUARD:
        log.error("检测到大规模删除（%d 个文件将被删除），疑似工作区异常（如目录被清空），"
                  "已取消本次提交并撤销暂存。请人工确认后重跑；若确属有意删除，"
                  "请分批删除或临时调大 MAX_DELETIONS_GUARD。", deletions)
        run_git(["reset", "-q"], check=False)  # 仅撤销暂存，不改动工作区文件
        return False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rc, _, err = run_git(["commit", "-m", f"{message}\n\n自动同步于 {ts}"], check=False)
    if rc != 0:
        log.error("提交失败：%s", err.strip())
        return False
    log.info("已提交：%s", message)
    return True


def git_commit_push(message):
    """提交工作树变更（有变更才提交），随后无论如何都尝试推送（含历史未推送提交）。"""
    if not git_commit(message):
        return False
    ok = push_with_retry()
    if not ok:
        log.warning("提交/推送未完全成功，请检查网络 / 凭据后重试。")
    return ok


def remove_remote_extras():
    """以本地（D:\\Light-Novel）为准：删除 GitHub 上多出来的文件（远程有、本地没有的文件）。

    要求调用前工作区已干净（先 git_commit）。
    流程：fetch -> 对比 FETCH_HEAD 与本地文件列表 -> 远程多出的文件，
    rebase 本地到最新远程之上后 git rm 删除并提交（推送由后续 push_with_retry 统一完成）。
    任何一步失败都安全跳过（宁可不删，绝不错删）。返回实际删除的文件数。"""
    rc, _, _ = run_git(["fetch", REMOTE, BRANCH], check=False, timeout=600)
    if rc != 0:
        log.warning("fetch 失败，跳过远程多余文件检查（不会误删）。")
        return 0
    rc, remote_out, _ = run_git(["ls-tree", "-r", "--name-only", "FETCH_HEAD"], check=False)
    if rc != 0:
        log.warning("读取远程文件列表失败，跳过远程多余文件检查。")
        return 0
    rc, local_out, _ = run_git(["ls-files"], check=False)
    if rc != 0:
        return 0
    remote_files = {x for x in remote_out.splitlines() if x}
    local_files = {x for x in local_out.splitlines() if x}
    extras = sorted(remote_files - local_files)
    if not extras:
        return 0
    log.info("远程比本地多 %d 个文件，将以本地为准删除（示例：%s）",
             len(extras), "、".join(extras[:5]))
    # 先把本地 rebase 到最新远程之上（多余文件随之进入工作区/索引），再删除。
    rc, o, e = run_git(["rebase", "FETCH_HEAD"], check=False, timeout=3600)
    if rc != 0:
        log.error("rebase 到最新远程失败，跳过本次多余文件清理：%s %s", o.strip(), e.strip())
        run_git(["rebase", "--abort"], check=False)
        return 0
    # 分批 git rm（--ignore-unmatch 容忍个别文件在 rebase 中已被本地提交删除），避免命令行过长。
    for i in range(0, len(extras), 50):
        run_git(["rm", "-q", "--ignore-unmatch", "--"] + extras[i:i + 50], check=False)
    rc, _, _ = run_git(["diff", "--cached", "--quiet"], check=False)
    if rc == 0:
        log.info("rebase 后远程多余文件已不存在，无需删除。")
        return 0
    rc, names, _ = run_git(["diff", "--cached", "--name-only", "--diff-filter=D"], check=False)
    removed = len([x for x in names.splitlines() if x])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rc, o, e = run_git(["commit", "-m",
                        f"sync: 以本地为准，删除远程多余文件 -{removed}\n\n自动同步于 {ts}"],
                       check=False)
    if rc != 0:
        log.error("多余文件删除提交失败：%s", e.strip())
        return 0
    log.info("已删除远程多余文件 %d 个（待推送）。", removed)
    return removed


# ---------------------------- 分类目录与 README / F 镜像 ----------------------------
IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", ".autosync"}

# 同步要排除的系统垃圾文件（Windows/macOS 自动生成）：不提交 Git、不镜像网盘、不触发监控。
# 比较时统一转小写。本地磁盘上的这些文件保留不删（Explorer 会自动重建），只是不再同步出去。
EXCLUDE_FILE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def ensure_category_dirs():
    for d in CATEGORY_DIRS.values():
        os.makedirs(d, exist_ok=True)
    for d in F_CATEGORY_DIRS.values():
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass  # F 盘（CloudDrive2）可能未挂载，忽略


def list_books(category_dir):
    """返回某分类目录下的一级条目（书名）列表，按名称排序。"""
    if not os.path.isdir(category_dir):
        return []
    items = []
    for name in os.listdir(category_dir):
        if name.lower() in IGNORE_NAMES:
            continue
        items.append(name)
    return sorted(items, key=lambda s: s.lower())


def _replace_readme_section(text, title, bullets):
    """把 README 中 <details> 区块（summary 含 title）里的书单替换为 bullets。
    保留区块外的所有内容（标题、功能说明等）。"""
    pattern = re.compile(
        r'(<details>\s*<summary>[^<]*' + re.escape(title) + r'[^<]*</summary>).*?(</details>)',
        re.DOTALL,
    )

    def repl(m):
        # 规范化为固定格式（summary 后空一行、书单、再空一行、</details>），
        # 且只重建从 summary 到对应 </details> 的部分，避免反复累积尾部空行导致每次都产生新提交。
        return m.group(1) + "\n\n" + bullets.rstrip() + "\n\n" + m.group(2)

    return pattern.subn(repl, text, count=1)


def regenerate_readme():
    """根据 轻小说/已完结 与 轻小说/未完结 的实际内容，刷新 README 的两个书单区块。"""
    done = list_books(CATEGORY_DIRS[CATEGORY_DONE])
    ongoing = list_books(CATEGORY_DIRS[CATEGORY_ONGOING])
    bullets_done = "\n".join(f"- {b}" for b in done) or "（暂无）"
    bullets_ongoing = "\n".join(f"- {b}" for b in ongoing) or "（暂无）"

    if os.path.exists(README_PATH):
        with open(README_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        # 若区块标题缺失则补建默认模板，否则仅替换内容
        if "未完结作品" not in text or "已完结作品" not in text:
            text = _default_readme(done, ongoing)
    else:
        text = _default_readme(done, ongoing)

    text, n1 = _replace_readme_section(text, "未完结作品", bullets_ongoing)
    text, n2 = _replace_readme_section(text, "已完结作品", bullets_done)

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("已更新 README.md（已完结 %d 本 / 未完结 %d 本）", len(done), len(ongoing))
    return n1 > 0 and n2 > 0


def _default_readme(done, ongoing):
    bullets_done = "\n".join(f"- {b}" for b in done) or "（暂无）"
    bullets_ongoing = "\n".join(f"- {b}" for b in ongoing) or "（暂无）"
    return (
        "# 📚 个人轻小说收集\n\n"
        "---\n\n"
        "<details>\n"
        "<summary>📚 未完结作品</summary>\n\n"
        f"{bullets_ongoing}\n"
        "</details>\n\n"
        "---\n\n"
        "<details>\n"
        "<summary>✅ 已完结作品</summary>\n\n"
        f"{bullets_done}\n"
        "</details>\n\n"
        "---\n\n"
        "## 功能\n"
        "通过 Git 自动同步至 GitHub 仓库；本文件由 sync_lightnovel.py 自动维护。\n"
    )


def mirror_dir(src, dst):
    """将 src 目录树增量镜像到 dst（仅复制缺失 / 变化的文件），返回复制文件数。"""
    if not os.path.isdir(src):
        return 0
    copied = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            if f.lower() in EXCLUDE_FILE_NAMES:
                continue  # 系统垃圾文件不同步到网盘
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
    return copied


def _purge_excluded_files(dst):
    """删除 dst 目录树下已存在的系统垃圾文件（历史上被镜像过去的），返回删除数。
    删除失败会记录警告（不再静默吞掉），便于发现权限 / 网络盘挂载问题。"""
    removed = 0
    failed = 0
    for r, _dirs, files in os.walk(dst):
        for f in files:
            if f.lower() in EXCLUDE_FILE_NAMES:
                p = os.path.join(r, f)
                try:
                    os.chmod(p, 0o666)  # 去掉只读属性（Windows 上只读文件无法删除）
                except OSError:
                    pass
                try:
                    os.remove(p)
                    removed += 1
                except OSError as exc:
                    failed += 1
                    log.warning("无法删除垃圾文件 %s：%s", p, exc)
    if failed:
        log.warning("%s 下有 %d 个垃圾文件删除失败，请检查权限或手动清理。", dst, failed)
    return removed


def sync_to_f():
    """把 轻小说 下的两个分类镜像到 F 盘网络云盘（CloudDrive2）。
    返回 True/False（F 盘不可用时返回 False，但不影响 GitHub 推送）。"""
    if not os.path.isdir(F_TARGET_ROOT):
        log.warning("未找到网络云盘 %s（CD2 未挂载？），跳过 F 盘镜像。", F_TARGET_ROOT)
        return False
    ok = True
    for cat, src in CATEGORY_DIRS.items():
        dst = F_CATEGORY_DIRS[cat]
        try:
            copied = mirror_dir(src, dst)
            purged = _purge_excluded_files(dst)
            log.info("已镜像到 F 盘 %s（新增/更新 %d 个文件，清理垃圾文件 %d 个）", dst, copied, purged)
        except Exception as exc:  # F 盘网络异常不应阻断主流程
            log.warning("镜像到 F 盘 %s 失败：%s", dst, exc)
            ok = False
    return ok


def _warn_stray_items():
    """提醒：轻小说 根目录下不应直接放书，应归入 已完结/未完结。"""
    try:
        entries = [e for e in os.listdir(LIGHT_NOVEL_DIR)
                   if e not in IGNORE_NAMES and e not in CATEGORY_DIRS]
        if entries:
            log.warning("注意：轻小说 根目录下存在非分类条目 %s，请将其移入「%s」或「%s」子目录。",
                        entries, CATEGORY_DONE, CATEGORY_ONGOING)
    except OSError:
        pass


def perform_sync(message):
    """一次完整的同步：确保目录 -> 刷新 README -> 镜像 F 盘 -> 提交本地改动
    -> 以本地为准删除 GitHub 多余文件 -> 统一推送。"""
    ensure_category_dirs()
    regenerate_readme()
    try:
        sync_to_f()
    except Exception as exc:
        log.warning("F 盘镜像异常：%s", exc)
    _warn_stray_items()
    if not git_commit(message):  # 先提交本地改动，保证工作区干净（后续 rebase 需要）
        return False
    try:
        remove_remote_extras()  # 以本地为准：删除 GitHub 上多出来的文件
    except Exception as exc:
        log.warning("远程多余文件清理异常：%s", exc)
    ok = push_with_retry()
    if not ok:
        log.warning("推送未完全成功，请检查网络 / 凭据后重试。")
    return ok


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
    finally:
        release_lock()


# ---------------------------- 状态查看 ----------------------------
def show_status():
    rc, out, _ = run_git(["status", "-s"], check=False)
    log.info("===== git status =====\n%s", out.strip() or "(干净)")
    snap = snapshot_dir()
    log.info("轻小说 当前监控快照文件数：%d", len(snap))
    log.info("已完结 %d 本 / 未完结 %d 本", len(list_books(CATEGORY_DIRS[CATEGORY_DONE])),
             len(list_books(CATEGORY_DIRS[CATEGORY_ONGOING])))
    log.info("日志文件：%s", LOG_FILE)


# ---------------------------- 主流程 ----------------------------
def main():
    parser = argparse.ArgumentParser(description="Light-Novel GitHub 自动同步与监控工具（增强版）")
    parser.add_argument("--once", action="store_true", help="种子复制 + 提交推送一次后退出")
    parser.add_argument("--monitor-only", action="store_true", help="不复制种子，仅同步当前状态并持续监控")
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
        perform_sync("sync: 手动/初始同步")
        log.info("--once 完成。")
        return

    # 默认模式：种子复制 + 提交推送 + 持续监控
    if not args.monitor_only:
        smart_copy()
    perform_sync("sync: 初始同步")
    monitor_loop()


if __name__ == "__main__":
    main()
