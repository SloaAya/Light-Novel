#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全项目配置与路径 —— 唯一真源。

原先 ``opds_server.py`` 与 ``sync_lightnovel.py`` 各自重复定义了一份
「根目录 / 分类目录 / 日志目录 / OPDS 端口」等常量，一旦改一处忘一处就会
出现两套真相。本模块把配置收敛到唯一位置，其余模块一律
``from ..paths import ...`` 引用。

约定：
  * 顶层常量都可被替换（测试按模块属性打桩），故不要在本模块里做副作用。
  * 口令只从环境变量读取，绝不写死 —— 本包会同步进 GitHub 仓库。
"""

import os

# ============================ 根目录与书库 ============================
TARGET_DIR      = r"D:\Light-Novel"
LIGHT_NOVEL_DIR = os.path.join(TARGET_DIR, "轻小说")

CATEGORY_DONE    = "已完结"
CATEGORY_ONGOING = "未完结"
CATEGORY_DIRS = {
    CATEGORY_DONE:    os.path.join(LIGHT_NOVEL_DIR, CATEGORY_DONE),
    CATEGORY_ONGOING: os.path.join(LIGHT_NOVEL_DIR, CATEGORY_ONGOING),
}
# 监控只盯这两个分类目录；任一个有改动就同步到 F 盘网盘 + GitHub
WATCH_DIRS = [CATEGORY_DIRS[CATEGORY_DONE], CATEGORY_DIRS[CATEGORY_ONGOING]]

# ==================== 运行状态目录（已加入 .gitignore）====================
LOG_DIR         = os.path.join(TARGET_DIR, ".autosync")
LOG_FILE        = os.path.join(LOG_DIR, "sync.log")
OPDS_LOG_FILE   = os.path.join(LOG_DIR, "opds.log")
COVER_CACHE_DIR = os.path.join(LOG_DIR, "covers")
META_CACHE_FILE = os.path.join(LOG_DIR, "meta_cache.json")
# 「已读完」清单：管理员标记过的作品集合（键 = 分类/书名）。运行时状态，不进仓库。
FINISHED_FILE   = os.path.join(LOG_DIR, "finished.json")
# 新增卷提示：书库快照 + 「有新卷、还没点进去看」的作品（键同样 = 分类/书名）。
# 同样落在 .autosync/，不会被同步到 GitHub / F 盘。
UPDATES_FILE    = os.path.join(LOG_DIR, "updates.json")
# 单实例锁的路径由 lightnovel.sync.monitor.lock_path() 现算（跟着 LOG_DIR 走），
# 面板也从那里取，保证「监控写哪儿、面板就读哪儿」只有一个真源。

# ============================ OPDS 书源服务 ============================
# 端口与监听地址可用环境变量覆盖：LN_OPDS_PORT / LN_OPDS_BIND
PORT      = int(os.environ.get("LN_OPDS_PORT", "8080"))
OPDS_PORT = PORT
# 0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机（配合内网穿透时使用）
BIND      = os.environ.get("LN_OPDS_BIND", "0.0.0.0")
OPDS_BIND = BIND
# 口令：留空则免密。外网访问强烈建议设置。只从环境变量读取，切勿写进源码。
AUTH_USER = os.environ.get("LN_OPDS_USER", "")
AUTH_PASS = os.environ.get("LN_OPDS_PASS", "")
# 访客口令（可选）：只有管理员能看「已读完」等管理功能，访客照常浏览/下载。
# 两级判定与免密时的降级规则见 lightnovel.opds.server.OPDSHandler._role。
AUTH_GUEST_USER = os.environ.get("LN_OPDS_GUEST_USER", "")
AUTH_GUEST_PASS = os.environ.get("LN_OPDS_GUEST_PASS", "")

PAGE_SIZE   = 100   # 每个 feed 每页最多条目数
RECENT_SIZE = 100   # 「最近更新」条数
INDEX_TTL   = 30    # 目录索引缓存秒数
EPUB_EXTS   = (".epub",)
EXCLUDE_FILE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_DIRS   = {".git", ".autosync"}

SERVER_TITLE  = "Light Novel 书库"
SERVER_AUTHOR = "lightnovel"

OPDS_NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
OPDS_ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB_MIME     = "application/epub+zip"
BLANK_PNG     = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# ---- 公网隧道（named tunnel）----
NAMED_TUNNEL_NAME = os.environ.get("LN_TUNNEL_NAME", "ln-opds")
CF_CONFIG = os.path.join(os.path.expanduser("~"), ".cloudflared", "config.yml")

# ============================ GitHub 同步 ============================
# 推荐 SSH：大体积推送在部分代理/网络下 HTTPS 上传会被重置，SSH 通常能稳定通过。
# SSH 22 端口被封时的兜底：git@ssh.github.com:443/SloaAya/Light-Novel.git
REPO_URL = "git@github.com:SloaAya/Light-Novel.git"
REMOTE   = "origin"
BRANCH   = "main"
GIT_BIN  = "git"   # 不在 PATH 时可改为完整路径，如 r"C:\Program Files\Git\bin\git.exe"

README_PATH = os.path.join(TARGET_DIR, "README.md")

MONITOR_INTERVAL    = 5     # 监控轮询间隔（秒）
SETTLE_TIME         = 3     # 判定文件「已写完、稳定」的等待时间（秒）
MAX_SETTLE_WAIT     = 180   # 单轮最多等待稳定的时间（秒），超时则强制同步
MAX_DELETIONS_GUARD = 100   # 单次提交允许的最大删除数（超过判定异常，拒绝提交）

# ---- 种子复制（源目录已不存在，默认关闭）----
SOURCE_DIR   = r"D:\BaiduNetdiskDownload"
BOOK_NAME    = "线上游戏的老婆不可能是女生？"
ENABLE_SEED_COPY = False
TARGET_SUBDIR = os.path.join("轻小说", CATEGORY_ONGOING, BOOK_NAME)

# ---- 认证：留空即用系统凭据管理器 / SSH（令牌等同密码，切勿写死）----
PAT_TOKEN = ""

# ======================== F 盘网络云盘镜像（CloudDrive2）========================
F_TARGET_ROOT = r"F:\LightNovel"
F_CATEGORY_DIRS = {
    CATEGORY_DONE:    os.path.join(F_TARGET_ROOT, CATEGORY_DONE),
    CATEGORY_ONGOING: os.path.join(F_TARGET_ROOT, CATEGORY_ONGOING),
}
MIRROR_LOG_FILE    = os.path.join(LOG_DIR, "mirror.log")            # 结构化审计日志（JSON Lines）
MIRROR_STATE_FILE  = os.path.join(LOG_DIR, "mirror_state.json")     # 上次镜像状态（供「查」快速读取）
MIRROR_MANIFEST    = os.path.join(LOG_DIR, "mirror_manifest.json")  # 镜像清单：本工具曾写入 F 盘的文件
MIRROR_REPORT_FILE = os.path.join(LOG_DIR, "mirror_report.json")    # 最近一次 查/同步 完整报告
MIRROR_HTML_FILE   = os.path.join(LOG_DIR, "mirror_status.html")    # 可读的状态看板
MAX_MIRROR_DELETIONS  = 200     # 单次镜像删除上限；超过判定异常，拒绝执行并告警
MTIME_TOLERANCE       = 2       # mtime 容差（秒）：网络盘/挂载盘常有精度损失
F_RECENT_PROTECT_SEC  = 86400   # F 侧 24h 内新建/修改的文件不自动删除（防误删）
CONFLICT_MTIME_SLACK  = 5       # F 侧 mtime 比 D 侧新超过此值且大小不同 → 判定为冲突
