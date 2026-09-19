#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书库目录与书单层：日志、分类目录维护、书单（README）刷新、目录树扫描工具。

这个模块原名 ``gitops``，当时还兼管 Git 提交与推送。2026-09-19 起同步只保留
「镜像到 F 盘」一条路（书库本体也从仓库里移除了），Git 那半边整体删除，
文件名改为 ``catalog`` 以符其实。

保留的部分是 F 盘镜像与目录监控真正依赖的东西：
``scan_tree`` / ``scan_dirs``（差异比对与孤儿目录）、``ensure_category_dirs`` /
``list_books`` / ``regenerate_readme``（分类目录与书单维护）、``setup_logging``
（全包共用一个 sync logger）。
"""

import logging
import os
import re
import sys

from datetime import datetime

from ..paths import (
    CATEGORY_DIRS,
    CATEGORY_DONE,
    CATEGORY_ONGOING,
    F_CATEGORY_DIRS,
    LOG_DIR,
    LOG_FILE,
    README_PATH,
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


# ---------------------------- 分类目录与 README ----------------------------
IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", ".autosync"}

# 同步要排除的系统垃圾文件（Windows/macOS 自动生成）：不镜像网盘、不触发监控。
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
        # 且只重建从 summary 到对应 </details> 的部分，避免反复累积尾部空行。
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
        "📦 EPUB · 🤖 自动整理 · ☁️ 网盘备份\n\n"
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
