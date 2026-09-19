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
# 封面缩略图最大宽度 —— 移动端性能的关键开关。
# 实测：目录页 52 张封面原图平均 371 KB（合计 18.8 MB，4G 下约 9 秒）；
# 压到 400px 宽的渐进式 JPEG 后约 20–30 KB/张（合计约 1.3 MB）。
# 设为 0 可关闭压缩（保持原始封面）。需要 Pillow；没装时自动跳过、不影响服务。
COVER_MAX_W = int(os.environ.get("LN_COVER_MAX_W", "400"))
META_CACHE_FILE = os.path.join(LOG_DIR, "meta_cache.json")
# 「已读完」清单：管理员标记过的作品集合（键 = 分类/书名）。运行时状态，不进仓库。
FINISHED_FILE   = os.path.join(LOG_DIR, "finished.json")
# 新增卷提示：书库快照 + 「有新卷、还没点进去看」的作品（键同样 = 分类/书名）。
# 同样落在 .autosync/，不会被同步到 GitHub / F 盘。
UPDATES_FILE    = os.path.join(LOG_DIR, "updates.json")
# 管理员会话（登录 cookie）的签名密钥：32 字节随机数，首次用到时自动生成。
# 落在这里而不是写死在源码里 —— 本包会同步进公开仓库，密钥必须留在本机。
SESSION_KEY_FILE = os.path.join(LOG_DIR, "session.key")
# 单实例锁的路径由 lightnovel.sync.monitor.lock_path() 现算（跟着 LOG_DIR 走），
# 面板也从那里取，保证「监控写哪儿、面板就读哪儿」只有一个真源。

# ==================== Moon+ Reader 阅读进度（只读桥接）====================
# .Moon+ 目录通常落在网盘挂载盘上（本项目是 CloudDrive2 挂载的 F:），
# 那里**每个小文件一次网络往返约 55 ms**（实测读 128 个 .po 要 6.7 s），
# 所以进度一律「后台线程刷 + 本地缓存」，请求路径上只读内存，绝不现读网盘。
MOON_ROOT   = os.environ.get("LN_MOON_ROOT", r"F:\Apps\Books\.Moon+")
MOON_CACHE_DIR = os.path.join(LOG_DIR, "moon_cache")       # 本地缓存（gitignore 内）
MOON_POS_FILE  = os.path.join(MOON_CACHE_DIR, "positions.json")
# 后台刷新间隔（秒）。设成 **5 秒**：进度是「读完一本书」这类需要即时反馈的状态，
# 5 分钟一轮的话，用户在阅读器里读完了，网页要过几分钟才认。敢设这么短是因为刷新是
# **增量**的 —— 每轮只做一次目录 stat（几十个文件约 7 ms），真有文件变了才读它
# （见 moon.read_positions）；只有首次启动或缓存失效才是全量重读。
# 想更保守/更激进用 LN_MOON_TTL 覆盖（单位秒）。
MOON_TTL    = int(os.environ.get("LN_MOON_TTL", "5"))
# 单卷「读完」阈值：用 >=99 而不是 ==100 —— Moon+ 的百分比是估算值，
# 实测存在 99.4% / 97.9% 这类「读到底但没到 100」的情况。
MOON_DONE_PERCENT = float(os.environ.get("LN_MOON_DONE", "99"))
# 关掉进度功能（网盘没挂载 / 不想要角标时用 LN_MOON_PROGRESS=0）：整层静默退化为无角标。
MOON_ENABLED = os.environ.get("LN_MOON_PROGRESS", "1") != "0"
# 进度角标对谁可见。默认**所有人**：这是阅读进度不是管理功能，
# 且手机端通常以访客身份浏览（设 0 则只有管理员看得到）。
MOON_PUBLIC  = os.environ.get("LN_MOON_PUBLIC", "1") != "0"

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

# 书单（README）的两个 <details> 区块由 lightnovel.sync.catalog 自动刷新
README_PATH = os.path.join(TARGET_DIR, "README.md")

MONITOR_INTERVAL    = 5     # 监控轮询间隔（秒）
SETTLE_TIME         = 3     # 判定文件「已写完、稳定」的等待时间（秒）
MAX_SETTLE_WAIT     = 180   # 单轮最多等待稳定的时间（秒），超时则强制同步

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
