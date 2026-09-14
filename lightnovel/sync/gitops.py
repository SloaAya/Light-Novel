#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git 与仓库层：日志、git 封装、仓库初始化、分块推送重试、分类目录与 README 维护、扫描工具。"""

import logging
import os
import re
import subprocess
import sys
import time

from datetime import datetime

from ..paths import (
    BRANCH,
    CATEGORY_DIRS,
    CATEGORY_DONE,
    CATEGORY_ONGOING,
    F_CATEGORY_DIRS,
    GIT_BIN,
    LOG_DIR,
    LOG_FILE,
    MAX_DELETIONS_GUARD,
    PAT_TOKEN,
    README_PATH,
    REMOTE,
    REPO_URL,
    TARGET_DIR,
)

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
        '<div align="center">\n\n'
        "# 📚 Light Novel Collection\n\n"
        "### 个人轻小说收藏库\n\n"
        "📦 EPUB · 🤖 自动整理 · ☁️ 云端双备份\n\n"
        "</div>\n\n"
        "---\n\n"
        "## 📖 书单\n\n"
        "> 以下书单由脚本自动维护，按名称排序，随藏书变动实时更新。\n\n"
        "<details>\n"
        "<summary>📚 未完结作品</summary>\n\n"
        f"{bullets_ongoing}\n\n"
        "</details>\n\n"
        "<details>\n"
        "<summary>✅ 已完结作品</summary>\n\n"
        f"{bullets_done}\n\n"
        "</details>\n\n"
        "---\n\n"
        "## ⚙️ 自动化\n\n"
        "| 功能 | 说明 |\n"
        "| --- | --- |\n"
        "| 📥 实时监控 | `轻小说/已完结` 与 `轻小说/未完结` 目录有变动即自动触发同步 |\n"
        "| 🔄 GitHub 同步 | 自动提交并推送；以本地为准，远程多余文件自动清理 |\n"
        "| ☁️ 网盘备份 | 自动镜像到网盘 `F:\\LightNovel`（CloudDrive2），支持增 / 改 / 删 全量同步 |\n"
        "| 🗑️ 删除传播 | F 盘多余文件/目录直接删除（多安全护栏，只删镜像目标里 D 盘没有的内容） |\n"
        "| ⚠️ 冲突检测 | F 侧被独立修改、清单外孤儿文件均会告警；近期外来文件自动保护 |\n"
        "| 📝 书单维护 | 本 README 的两个书单区块自动刷新，其余内容保持不变 |\n"
        "| 🛡️ 安全护栏 | 大规模删除保护 + 系统垃圾文件（desktop.ini 等）自动排除 |\n\n"
        "---\n\n"
        '<div align="center">\n'
        "<sub>由 <code>lightnovel.sync</code> 自动维护 · 最后同步见提交记录</sub>\n"
        "</div>\n"
    )


# ---------------------------- F 盘镜像：工具函数 ----------------------------
def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def scan_tree(root, exclude=EXCLUDE_FILE_NAMES):
    """扫描目录树，返回 {相对路径(以 / 分隔): {"size": int, "mtime": float}}。
    目录不存在或不可读时返回空字典（不抛异常）。只读操作，不会修改任何文件。"""
    tree = {}
    if not root or not os.path.isdir(root):
        return tree
    for r, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() in exclude:
                continue
            fp = os.path.join(r, f)
            try:
                st = os.stat(fp)
            except OSError as exc:
                log.warning("无法读取文件信息 %s：%s", fp, exc)
                continue
            rel = os.path.relpath(fp, root).replace("\\", "/")
            tree[rel] = {"size": st.st_size, "mtime": st.st_mtime}
    return tree


def scan_dirs(root):
    """递归列出 root 下**所有子目录**的相对路径（含空目录），用 scandir 避免 os.walk 在
    某些挂载盘（CloudDrive2）上跳过空目录的坑。返回 set，根目录本身用空串 "" 表示。"""
    dirs = set()
    if not root or not os.path.isdir(root):
        return dirs
    dirs.add("")                                                 # 根目录
    stack = [""]
    while stack:
        rel = stack.pop()
        abs_dir = os.path.join(root, rel.replace("/", os.sep)) if rel else root
        try:
            with os.scandir(abs_dir) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_rel = (rel + "/" + entry.name) if rel else entry.name
                            dirs.add(child_rel)
                            stack.append(child_rel)
                    except OSError:
                        pass
        except (OSError, PermissionError) as exc:
            log.warning("无法扫描目录 %s：%s", abs_dir, exc)
    return dirs


