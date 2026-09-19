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


# README 的兜底模板（``{ONGOING}`` / ``{DONE}`` 会被替换成书单）。
#
# ⚠️ 它与仓库根目录的 ``README.md`` 是**同一份内容**，改一处必须同步另一处 ——
# 平时 ``regenerate_readme()`` 只替换 README 里两个 <details> 区块，用不到这里；
# 只有当 README 被删掉、或两个区块标题都找不到时才会拿它重建。不一致的后果是
# 「README 悄悄退回旧版本、少掉一段说明」，很难被发现。
_README_BODY = r"""<div align="center">

# 📚 Light Novel Collection

### 个人轻小说收藏库

📦 EPUB · 🗂️ 自动整理 · ☁️ 网盘镜像 · 🌐 在线书源 · 📊 阅读进度 · 🖥️ 图形面板

**🌐 在线访问：[https://ranqing.ccwu.cc/](https://ranqing.ccwu.cc/)**

</div>

---

## 📖 书单

> 以下书单由脚本自动维护，按名称排序，随藏书变动实时更新。

<details>
<summary>📚 未完结作品</summary>

{ONGOING}

</details>

<details>
<summary>✅ 已完结作品</summary>

{DONE}

</details>

---

## ✨ 功能

| 功能 | 说明 |
| --- | --- |
| 📥 实时监控 | `轻小说/已完结` 与 `轻小说/未完结` 目录有变动即自动触发同步 |
| ☁️ 网盘镜像 | 自动镜像到网盘 `F:\LightNovel`（CloudDrive2），增 / 改 / 删 全量同步 |
| 🗑️ 删除传播 | F 盘多余文件/目录直接删除（多层安全护栏，只删镜像目标里 D 盘没有的内容） |
| ⚠️ 冲突检测 | F 侧被独立修改、清单外孤儿文件均会告警；24h 内新增/修改的 F 侧文件自动保护 |
| 📝 书单维护 | 本 README 的两个书单区块自动刷新，其余内容保持不变 |
| 🛡️ 安全护栏 | 大规模删除上限保护 + 系统垃圾文件（desktop.ini 等）自动排除 + 路径逃逸校验 |
| 📱 手机书源 | 内置 OPDS 1.2 书源，公网 <https://ranqing.ccwu.cc/>（免登录即可浏览 / 搜索 / 下载 / 订阅） |
| 🔐 管理员登录 | 网页 `/opds/login` 口令登录（HMAC 签名 cookie，30 天）；阅读器可用 `https://用户:口令@域名/` 走 Basic。未登录 = 访客，只少管理入口 |
| ✅ 已读完清单 | 登录后逐部标记「已读完」；标记**原地生效不刷新页面**；书补了新卷会自动摘除并置顶提示 |
| 📊 阅读进度 | 桥接 Moon+ Reader 的阅读位置：目录页封面角标、详情页进度环、并自动判定「整部读完」 |
| 🖼️ 封面压缩 | 目录页封面自动缩到 400px 渐进式 JPEG（实测 18.8 MB → 1.3 MB）；无 Pillow 时自动跳过 |
| ⬇️ 打包下载 | 整部 / 整类打包 zip，带 `Content-Length`（下载端可显示进度）且支持 `Range` 断点续传 |
| 🖥️ 控制面板 | Tkinter 面板统一启停与状态查看，可打包为单文件 `LightNovel.exe` |
| 🚀 在线启动器 | 双击 `launchers/launch_online.bat` 一键起「书源 + 隧道 + 守护」：关窗口不掉线，服务挂了 30 秒内自动拉起 |

---

## 🚀 快速开始

运行时**零第三方依赖**（Python ≥ 3.8）。两个可选项：

- `pip install segno` —— 控制台二维码（缺失则自动跳过，不影响服务）
- `pip install Pillow` —— 封面压缩与阅读进度里的图片测量（缺失则自动降级）

```bash
python -m lightnovel ui            # 图形控制面板（不带子命令时默认就是它）
python -m lightnovel launch        # 一键上线：书源 + 隧道 + 守护进程
python -m lightnovel launch-status # 看守护 / 服务 / 隧道状态
python -m lightnovel stop          # 停止（先停守护，再停服务）
```

不想敲命令就直接双击 `launchers/` 里的 `.bat`，它们会自动探测 Python 解释器。

---

## 🖱️ 启动器（launchers/）

| 文件 | 做什么 | 什么时候用 |
| --- | --- | --- |
| `launch_online.bat` | 守护进程 + 书源 + 隧道，30 秒自愈 | 完整上线（日常就用它） |
| `restart_opds.bat` | 只热重启书源，保留守护与隧道 | 改完 Python 代码后让改动生效，公网地址不变 |
| `run_opds.bat` | 前台跑书源，日志直接打在窗口 | 调试，关窗口即停 |
| `run_panel.bat` | 图形控制面板（pythonw 无窗口） | 想用界面管理 |
| `stop_opds.bat` | 停止（先守护后服务） | 下线 |

---

## 🧩 项目结构

```text
Light-Novel/
├── lightnovel/              主包
│   ├── paths.py             路径与配置（唯一真源）
│   ├── cli.py               统一命令行入口（python -m lightnovel）
│   ├── launcher.py          守护进程：巡检 + 自愈 + 状态
│   ├── service.py           服务启停（脱离控制台方式）
│   ├── tunnel_setup.py      Cloudflare 固定域名隧道向导
│   ├── ui.py                Tkinter 控制面板
│   ├── opds/                OPDS 书源（依赖方向：library → feeds → server）
│   │   ├── library.py       数据层：目录索引 / 封面提取 / 元数据 / zip 打包
│   │   ├── feeds.py         表示层：Atom feed + 浏览器 HTML 视图
│   │   ├── server.py        服务层：HTTP Handler / 隧道 / 二维码
│   │   ├── moon.py          Moon+ Reader 阅读进度桥接（只读）
│   │   ├── finished.py      「已读完」清单（本机状态，不进仓库）
│   │   ├── updates.py       「新增卷」提示（书库快照对比）
│   │   └── session.py       管理员会话（签名 cookie）
│   └── sync/                F 盘镜像与目录监控
│       ├── catalog.py       分类目录 / 书单 / README 维护
│       ├── mirror.py        D 盘 → F 盘镜像
│       └── monitor.py       目录监控主流程
├── launchers/               双击即用的 .bat（含 _find_python.bat 探测解释器）
├── tests/smoke_test.py      端到端回归（真实 socket / 真实书库只读巡检）
├── tools/moon_cache.py      .Moon+ 目录的独立分析工具
└── docs/                    架构与审计文档
```

---

## ⚙️ 配置

全部通过**环境变量**覆盖，不写进源码（本包会同步进公开仓库，口令一类必须留在本机）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LN_OPDS_PORT` | `8080` | 监听端口 |
| `LN_OPDS_BIND` | `0.0.0.0` | 监听地址（`127.0.0.1` = 仅本机） |
| `LN_OPDS_USER` / `LN_OPDS_PASS` | 空 | 管理员口令，留空则免密（外网强烈建议设置） |
| `LN_OPDS_GUEST_USER` / `LN_OPDS_GUEST_PASS` | 空 | 访客口令（可选）：访客照常浏览，只是没有管理入口 |
| `LN_COVER_MAX_W` | `400` | 封面缩略图最大宽度，`0` = 不压缩 |
| `LN_MOON_ROOT` | `F:\Apps\Books\.Moon+` | Moon+ Reader 数据目录（网盘挂载盘） |
| `LN_MOON_TTL` | `5` | 阅读进度后台刷新间隔（秒） |
| `LN_MOON_DONE` | `99` | 单卷「读完」阈值（Moon+ 的百分比是估算值，实测有 99.4%） |
| `LN_MOON_PROGRESS` | `1` | 设 `0` 关闭整层阅读进度 |
| `LN_MOON_PUBLIC` | `1` | 设 `0` 则进度只对管理员可见 |
| `LN_TUNNEL_NAME` | `ln-opds` | Cloudflare named tunnel 名称 |
| `LN_TUNNEL_PROTOCOL` | `http2` | cloudflared 连接边缘的协议（`auto` 会在被丢包的线路上卡死在 QUIC） |

---

## 🧪 测试

```bash
python tests/smoke_test.py
```

覆盖：书库索引、Atom feed 与 HTML 视图、真实 socket 端到端（内容协商 / Range / ZIP / 认证）、
「已读完」清单与权限、新增卷提示、Moon+ 进度派生、封面提取与元数据。
测试把写操作全部隔离在临时目录，不碰真实书库与 F 盘。

---

<div align="center">
<sub>由 <code>lightnovel.sync</code> 自动维护 · 最后同步见提交记录</sub>
</div>
"""


def _default_readme(done, ongoing):
    """README 缺失、或两个书单区块都找不到时的兜底模板（见 :data:`_README_BODY`）。"""
    bullets_done = "\n".join(f"- {b}" for b in done) or "（暂无）"
    bullets_ongoing = "\n".join(f"- {b}" for b in ongoing) or "（暂无）"
    return (_README_BODY
            .replace("{ONGOING}", bullets_ongoing)
            .replace("{DONE}", bullets_done))


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
