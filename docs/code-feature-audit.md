# 代码功能清单与「可恢复功能」审计

> 审计时间：2026-09-14 · 范围：`D:\Light-Novel` 下全部可执行代码（只读分析，未修改任何文件）
>
> | 文件 | 行数 | 角色 |
> | --- | --- | --- |
> | `opds_server.py` | 1964 | OPDS 书源服务（手机端订阅 / 下载） |
> | `sync_lightnovel.py` | 1555 | GitHub 同步 + F 盘镜像 + 实时监控 |
> | `setup_named_tunnel.py` | 218 | Cloudflare 固定域名隧道配置向导 |
> | `run_monitor.bat` / `run_once.bat` / `run_opds.bat` | 8 / 8 / 21 | 启动器 |
> | `run_named_tunnel.bat` / `setup_named_tunnel.bat` / `stop_opds.bat` | 23 / 22 / 18 | 启动器 / 停止器 |
> | `.github/workflows/OneDriveSync.yml` | 41 | 云端 rclone 同步（当前停用） |

---

## 一、已启用的功能

### 1. `opds_server.py` — OPDS 书源服务（33 项）

**服务与安全**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 1 | HTTP/1.1 多线程服务 | `ThreadingHTTPServer`，默认 `0.0.0.0:8080`（`LN_OPDS_PORT` / `LN_OPDS_BIND` 可覆盖），`allow_reuse_address`，端口占用给出中文提示 |
| 2 | 认证 | 凭据**只**从 `LN_OPDS_USER` / `LN_OPDS_PASS` 读取；两者皆空则「回环=管理员，其余=访客」 |

> **2026-09-14 更新（认证模型已改，上表第 2 行仅存历史）**：
> 网页侧改为 `/opds/login` 口令登录 + HMAC 签名 cookie（`lightnovel/opds/session.py`，密钥在
> `.autosync/session.key`，30 天有效），Basic 保留给阅读器与脚本。
> 关键变化：**匿名不再被 401 拦下**，而是降级成访客 —— 书库本身对所有人开放（浏览/搜索/
> 下载/OPDS 订阅），只有「已读完」这类管理功能要管理员身份。Basic 凭据不对时也降级成访客
> 而不是 401，否则浏览器会把陈旧的缓存凭据反复弹成系统认证框。
> 详见 `OPDSHandler._role` 的文档字符串与 `tests/smoke_test.py` 的 R3 / T 两节。
| 3 | 目录穿越防护 | `_safe_relpath` + `resolve_under`（realpath 白名单），`/dl` `/cover` `/zip` 全部走该校验 |
| 4 | HEAD 请求支持 | `do_HEAD` 复用 `do_GET(head_only=True)`，只发头部不写 body |
| 5 | 断连容错 | `BrokenPipeError` / `ConnectionResetError` 静默吞掉；`500` 页面兜底 |
| 6 | 懒加载日志 | `_ensure_logging()` 写 `.autosync/opds.log` + stdout（被 `sync_lightnovel.py` 复用时自动挂载） |
| 7 | 独立 / 后台双启动模式 | `serve_forever()`（阻塞）与 `start_background()`（daemon 线程，供 sync 调用） |

**书库索引**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 8 | 双层书库扫描 | `_scan_library()`：分类（已完结 / 未完结）→ 书名 → 卷（支持一级文件或嵌套子目录 `正篇/番外`），自动跳过 `.git` / `.autosync` / `desktop.ini` |
| 9 | 索引缓存 | 线程锁 + `INDEX_TTL = 30s` TTL，`get_library(force=True)` 强制刷新 |
| 10 | 书内子目录分组 | `_group_vols_by_subdir()`：按子目录分组，根目录散卷排最后归类为「正篇」 |

**OPDS（Atom）接口**

| # | 路由 | 说明 |
| --- | --- | --- |
| 11 | `/` | 根导航 feed（已完结 / 未完结 / 最近更新 / 全部作品），内链**全用相对 URL**，无论走局域网 IP、穿透域名还是 Tailscale 都自动跟随 |
| 12 | `/opds/catalog/<分类>` | 分类导航 feed；`all` 合并视图；分页 `PAGE_SIZE = 100`，带 `rel=next` / `rel=previous` / `thr:count` |
| 13 | `/opds/book/<分类>/<书名>` | 获取型（acquisition）feed，每卷 4 条 link（image / thumbnail / acquisition / open-access / alternate） |
| 14 | `/opds/recent` | 按 mtime 排序取 `RECENT_SIZE = 100` 卷 |
| 15 | `/opds/search?q=` | 书名 + 相对路径子串检索（大小写不敏感） |
| 16 | `/opds/opensearch.xml` | OpenSearch 描述，阅读器内出现搜索框 |
| 17 | 内容协商 | `_accept_wants_xml()`：Accept 含 `atom+xml` / `opds` → XML，否则 HTML；同 URL 双协议 |

**HTML 站点视图（同一套路由的浏览器版）**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 18 | 完整响应式 CSS | `SITE_CSS` 约 220 行；`prefers-color-scheme:dark` 自动深色；760px 断点手机布局（顶栏两行 + tab 横滑） |
| 19 | 顶栏 + 全局搜索 | sticky header、4 个 tab（当前页高亮）、搜索表单 |
| 20 | 首页 | 分类大卡 + 快速入口 + 书库规模统计 |
| 21 | 书封网格 | `auto-fill/minmax(120px)`，卷数徽标，`_cover_url()` 懒加载 |
| 22 | 作品详情页 | hero 头（封面 + 作者 + 分类 / 卷数 / 体积 + dc:description 三行截断）+「打包下载全部」 |
| 23 | 分组折叠列表 | 文件夹式分组，第一组默认展开，`grpAll(true/false)` 全部展开 / 折叠，组内「打包本组」按钮（`stopPropagation` 不触发折叠） |
| 24 | 分页器 | `_pager_html`，`rel=next/previous` 反解为 HTML 链接 |
| 25 | 主卷封面优选 | `_primary_vol()`：多子目录时按「正篇 → 本篇 → 主线 → main → series」→ 书名相似 → 前 2 字匹配 → 字典序 兜底，避免取到副刊低质封面 |
| 26 | 空态 / 404 页 | `empty` 卡片、面包屑导航、`footer` 署名 |
| 27 | 兼容旧名 | `index_html()` → `root_html()`（`/index.html` 仍可访问） |

**资源接口**

| # | 路由 | 说明 |
| --- | --- | --- |
| 28 | `/cover/<rel>` | epub 封面提取：① OPF `<meta name="cover">`（且必须指向真图片，排除 xhtml 包装页）→ ② 文件名含 `cover` / `封面` → ③ 体积最大图兜底；支持 jpg / png / **webp / gif**；`_looks_like_spread()` 过滤宽高比 > 1.3 的跨页图；内存 + 磁盘两级缓存（`.autosync/covers/<hash前2位>/`）；无封面返回内置 1×1 PNG |
| 29 | `/dl/<rel>` | 单卷下载，支持 `Range` 断点续传（206 / 416 / `Accept-Ranges`），1 MB 分块，`Content-Disposition` 带 UTF-8 文件名 |
| 30 | `/zip/<rel>` | 流式打包：整类 / 整书 / 整组三级；`ZIP_STORED` 不二次压缩；`_ZipSink` 伪装成不可 seek 流让 `zipfile` 写 data descriptor，**全程无临时文件、不占内存**；`Connection: close` 结尾 |
| 31 | `/opds/stats` | JSON 统计（各分类作品数 / 卷数） |
| 32 | `/opds/refresh` | 强制重建索引 |

**元数据与公网接入**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 33 | epub 元数据提取 | `get_epub_meta()` 取 `dc:title` / `dc:creator` / `dc:description`；内存 + 磁盘缓存 `.autosync/meta_cache.json`（key = 路径+大小+mtime，原子替换写入） |
| 34 | Cloudflare **固定域名**隧道 | `start_named_tunnel()`：读 `~/.cloudflared/config.yml` 的 hostname；`sync_named_port()` 自动把回源端口改成当前端口；监听 `Registered tunnel connection` 触发 `on_connected` 回调 |
| 35 | cloudflared **临时**隧道 | `start_cloudflared()`：解析 `*.trycloudflare.com` 输出，60s 超时 |
| 36 | cloudflared 定位 | `find_cloudflared()`：PATH → `CLOUDFLARED_BIN` → 4 个常见安装路径 → scoop shims |
| 37 | 订阅二维码 | `print_qr()`：segno 优先 → qrcode 降级 → 都没有则提示手动输入 |
| 38 | 控制台自动隐藏 | `_hide_console()`：named tunnel 连上后 3s 隐藏窗口（`--hide-window`，配合 `stop_opds.bat` 停止） |
| 39 | CLI | `--port` `--bind` `--tunnel {cloudflared,named}` `--public-url` `--no-qr` `--hide-window` `--help-internet` |

### 2. `sync_lightnovel.py` — 同步 / 镜像 / 监控（30 项）

**Git 与推送**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 1 | Git 命令封装 | `run_git()`：`GIT_TERMINAL_PROMPT=0`（无 TTY 不卡密码提示）、`timeout`、`CREATE_NO_WINDOW`（不弹黑窗）、`errors="replace"`、失败自动 `log.warning` |
| 2 | 仓库初始化 / 复用 | `ensure_repo()`：`git init -b main`；`safe.directory` 规避 Windows 可疑所有权；`core.quotepath=false`（中文路径可读）；`http.postBuffer=500MB` + `http.version=HTTP/1.1`（抗 curl 55 / 连接重置） |
| 3 | 远程配置 | `ensure_remote()`：add / set-url 自动纠正、`ls-remote` 探测后 `--set-upstream-to` |
| 4 | **SSH 推送（当前生效）** | `REPO_URL = git@github.com:SloaAya/Light-Novel.git` |
| 5 | 推送错误四分类 | `classify_push_error()` → `auth` / `nonfastforward` / `transient` / `fatal` |
| 6 | 指数退避重试 | `_push_backoff()`：5 → 10 → 20 → 40 → 60s 封顶，最后一次不等待 |
| 7 | **逐提交分块推送** | `push_with_retry()`：先 fetch，用 `FETCH_HEAD..main` 算待推范围，每次只推**最旧一个 SHA**（快进、单次上传量最小）；被抢先推进则 `rebase FETCH_HEAD` 后重算；400 轮上限 + 停滞检测防死循环；断点可续传 |
| 8 | 提交护栏 | `git_commit()`：`add -A`；无变更跳过；删除数 ≥ `MAX_DELETIONS_GUARD = 100` 判定为异常，**撤销暂存并拒绝提交** |
| 9 | **以 D 盘为准清理 GitHub 多余文件** | `remove_remote_extras()`：fetch → `ls-tree FETCH_HEAD` vs `ls-files` 求差集 → rebase → 分批（50/批）`git rm --ignore-unmatch` → commit；任何一步失败都安全跳过（宁可不删，绝不错删） |

**README 与分类**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 10 | 分类目录自建 | `ensure_category_dirs()`（F 盘建目录失败静默忽略） |
| 11 | README 书单自动刷新 | `regenerate_readme()` + `_replace_readme_section()`：正则只重写 `<details><summary>XX作品</summary>` 到对应 `</details>`，格式归一化避免尾部空行导致每次新提交 |
| 12 | README 模板重建 | `_default_readme()`：标题 / 徽标 / 两个书单区块 / 自动化说明表格 |
| 13 | 游离条目提醒 | `_warn_stray_items()`：`轻小说` 根目录下非分类条目告警 |

**D → F 盘镜像（增删改查）**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 14 | 目录树扫描 | `scan_tree()`（`{rel: {size, mtime}}`）；`scan_dirs()` 用 `scandir` 递归，**规避 CloudDrive2 挂载盘 `os.walk` 不 yield 空目录的坑** |
| 15 | 镜像清单 | `.autosync/mirror_manifest.json`，`ensure_manifest_seeded()` 首次运行用 F 盘现有文件播种；用于区分「我们放上去的」与「F 盘外来文件」 |
| 16 | 差异计划（查） | `plan_mirror()`：增 / 改 / 删 / 一致，`MTIME_TOLERANCE = 2s` 容差 |
| 17 | 三类冲突检测 | `F_NEWER`（F 侧更新且大小不同）、`F_FOREIGN_RECENT`（清单外 + 24h 内改动 → **保护不删**）、`F_ORPHAN`（清单外历史遗留 → 按 D 盘为准删，但明确告警） |
| 18 | 计划执行（增改删） | `apply_mirror()`：`shutil.copy2` 增量复制；直接删除；单次删除 > `MAX_MIRROR_DELETIONS = 200` 触发护栏并关闭本轮删除（`guard_triggered`） |
| 19 | 孤儿子树递归删除 | `_prune_foreign_subtrees()`：**四重安全网** ① 每个候选目录 realpath + `commonpath` 逃逸校验 ② 子树内任意文件或**目录自身** mtime 在 `F_RECENT_PROTECT_SEC = 86400` 内则跳过 ③ `src` 不存在则绝不执行任何删除 ④ 逐项 log 不阻断主流程；`.trash` 前缀的历史残留不清理 |
| 20 | 空目录清理 | `_prune_empty_dirs()`：自底向上，`scandir` 判空后 `rmdir` |
| 21 | 垃圾文件清理 | `_purge_excluded_files()`：`desktop.ini` / `thumbs.db` / `.ds_store`，删除前 `chmod 0o666` 去只读，失败计数告警 |
| 22 | 审计日志 | `.autosync/mirror.log`（JSON Lines，逐条 增/改/删/跳过/失败） |
| 23 | 状态与报告 | `mirror_state.json`（上次同步摘要）、`mirror_report.json`（完整差异报告），均为原子替换写入 |
| 24 | HTML 看板 | `write_mirror_dashboard()` → `.autosync/mirror_status.html`：一致率进度条、六张指标卡、分类明细表、冲突 / 待删清单；看板失败不影响主流程 |
| 25 | 只看不动的「查」 | `mirror_query()` + `--mirror-status`：纯只读差异报告 |

**监控主流程**

| # | 功能 | 关键实现 |
| --- | --- | --- |
| 26 | 实时监控循环 | `monitor_loop()`：`MONITOR_INTERVAL = 5s` 轮询 `已完结` / `未完结` |
| 27 | 写入稳定判定 | 连续两次快照一致才认为写完；`SETTLE_TIME = 3s`、`MAX_SETTLE_WAIT = 180s` 超时强制同步 |
| 28 | 变更检测 | `detect_changes()` → added / modified / removed，写入 commit message（`+n ~n -n`） |
| 29 | 单实例锁 | `.autosync/monitor.lock`，读 PID + `os.kill(pid, 0)` 存活探测；`finally` 释放 |
| 30 | 一次完整同步 | `perform_sync()`：建目录 → 刷 README → F 盘镜像 → `git_commit`（工作区先干净，为 rebase 铺路）→ 删远程多余 → 统一推送 |
| 31 | 监控防自激 | 同步后重新 `snapshot_dir()`，避免把同步自身的 rebase / git rm 误判为外部变更 |
| 32 | OPDS 集成 | `start_opds_background()`：导入失败或端口占用只告警，绝不影响同步主流程 |
| 33 | 状态查看 | `--status`（git status + 快照文件数 + 两个分类书目数） |
| 34 | CLI | `--once` `--monitor-only` `--init` `--status` `--mirror-f` `--mirror-status` `--mirror-dry-run` `--mirror-no-delete` `--opds` `--opds-only` `--opds-port` |

### 3. `setup_named_tunnel.py`（5 项）

| # | 功能 |
| --- | --- |
| 1 | 四步向导：`tunnel login`（浏览器授权，已有 `cert.pem` 则跳过）→ `tunnel create`（已存在则复用 id）→ 写 `~/.cloudflared/config.yml`（ingress + 404 兜底）→ `tunnel route dns`（自动加 CNAME） |
| 2 | ANSI 转义清洗 `clean()`，`cloudflared` 输出可读 |
| 3 | 隧道 UUID 双路解析：`tunnel list` 按名匹配 + 正则全局兜底 |
| 4 | 独立运行兜底：导入 `opds_server` 失败时自建常量与 `find_cloudflared()` |
| 5 | CLI：`--name` `--port` `--hostname`；域名格式校验 + 失败时打印手动添加 CNAME 的指引 |

### 4. BAT 启动器（6 个）

| 文件 | 已启用功能 |
| --- | --- |
| `run_monitor.bat` | 优先 `pythonw`（无窗口后台）启动 `sync_lightnovel.py`，`start` 脱离当前窗口 |
| `run_once.bat` | `python` / `py` 兜底探测，执行 `--once` |
| `run_opds.bat` | 启动 `opds_server.py`，`%*` 透传命令行参数，`pause` 便于看报错 |
| `run_named_tunnel.bat` | `opds_server.py --tunnel named --no-qr --hide-window`（固定域名 + 不打印二维码 + 连上后自动隐藏窗口） |
| `setup_named_tunnel.bat` | 前置条件说明 + `pause` + 调用向导 |
| `stop_opds.bat` | `netstat -ano \| findstr :8080 \| findstr LISTENING` 取 PID → `taskkill /F`；再 `taskkill /IM cloudflared.exe /F` |

### 5. `.github/workflows/OneDriveSync.yml`

- 存在（41 行）：`workflow_dispatch` 手动触发、checkout、安装 rclone、写 `RCLONE_CONFIG`、校验配置、`rclone sync` 下载、提交推送 —— **但整个 job 被 `if: false` 停用**，详见下文 B-6。

---

## 二、可恢复的功能（注释 / 开关 / 死代码）

> 判定标准：逻辑完整、依赖齐备、恢复后能跑起来。按「恢复风险」排序。

### A 类 — 纯注释掉的代码（`rem` / `#`），恢复零风险

| # | 位置 | 被注释内容 | 恢复方式 | 风险 |
| --- | --- | --- | --- | --- |
| A-1 | `run_opds.bat:10-11` | `set LN_OPDS_USER=ln`<br>`set LN_OPDS_PASS=改成你自己的口令` | 去掉 `rem` | **无**。唯一注意：`run_opds.bat` 本身在 Git 仓库里，明文口令会随同步推到 GitHub（文件内注释已警告「不要提交」）。更稳妥的做法是在系统环境变量里设，别写进 bat |
| A-2 | `run_opds.bat:14` | `set LN_OPDS_PORT=8080` | 去掉 `rem` | **无**。覆盖 OPDS 默认端口，与 `opds_server.py` 的 `LN_OPDS_PORT` 读取逻辑天然对接 |
| A-3 | `run_named_tunnel.bat:13-15` | `set LN_OPDS_PORT=8080`<br>`set LN_OPDS_USER=your_user`<br>`set LN_OPDS_PASS=your_password` | 去掉 `rem` | **无**，同上。这是固定域名（公网暴露）场景，**强烈建议**启用口令 |

### B 类 — 被开关 / 条件停用，逻辑完整但需前置条件

| # | 位置 | 被停用功能 | 完整逻辑所在 | 恢复方式 | 风险评估 |
| --- | --- | --- | --- | --- | --- |
| B-1 | `sync_lightnovel.py:104` | HTTPS 远程地址 `https://github.com/SloaAya/Light-Novel.git` | `ensure_remote()` / `get_remote_url()` / `push_with_retry()` | 取消该行注释，**并同时注释掉 105 行的 SSH 版** | ⚠️ 单独取消注释**不生效**：104 行会被 105 行的 SSH 赋值覆盖，不报错但无效。且注释里已说明 HTTPS 大体积推送在部分代理下会被重置 —— **恢复会降低推送稳定性，不建议** |
| B-2 | `sync_lightnovel.py:69` `ENABLE_SEED_COPY = False` | 百度网盘下载目录的**种子式增量复制** `smart_copy()`（1278-1305）+ 两处调用点（1539-1540、1546-1547） | `smart_copy()`：`os.walk` 增量对比 size + mtime(<2s) 复制，跳过 `desktop.ini` 等垃圾文件 | 改回 `True` | ✅ **安全**。源目录 `D:\BaiduNetdiskDownload` 已不存在 → 只 `log.error` + `return 0`，不会崩、不会误删。代价是每次运行多一条报错日志。相关常量 `SOURCE_DIR`(64) / `BOOK_NAME`(66) / `TARGET_SUBDIR`(83) 均在位，目标路径计算正确 |
| B-3 | `sync_lightnovel.py:133` `PAT_TOKEN = ""` | **HTTPS PAT 认证链路**：`auth_url()`（236-241）注入 token、`push_with_retry()` 里的 `set_remote_url(auth_url(...))` / `finally` 恢复原 URL（344-346、375-377）、`git_commit_push()` | 逻辑完整 | 填入 token | ❌ **不建议**。① `auth_url()` 只处理 `https://` 开头的 URL，当前是 SSH → 单独填 token 完全不生效，必须同时切到 HTTPS；② 本文件会同步进 GitHub，token 等同密码，泄漏即仓库被接管 |
| B-4 | 同上（B-3 的调用方） | `git_commit_push()`（406-413）—— 提交 + 推送一步封装 | 完整 | 接线即可 | ✅ 安全，见 C-2 |
| B-5 | `sync_lightnovel.py:406` | — 见 C-2 | — | — | — |
| B-6 | `.github/workflows/OneDriveSync.yml:7` `if: false` | 整个 **OneDrive 云端 rclone 同步** job（checkout → 装 rclone → 写配置 → `rclone sync` → commit & push） | workflow 全文完整 | 删除该行（注释已明说「恢复只需删除本行」） | ❌ **不推荐**。注释给出的停用理由仍然成立：会与本地 `sync_lightnovel.py` 抢同一 `main` 分支；且 `rclone sync onedrive:/LightNovel ./LightNovel` 方向是**云盘 → 仓库**，路径还是旧的 `./LightNovel`（与当前 `轻小说/` 结构不符），`sync` 的删除语义有误删书籍风险。若要恢复必须先改路径与方向 |

### C 类 — 定义了但从未调用的死代码 ✅ **已于 2026-09-14 删除**

| # | 位置（删除前） | 函数 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| C-1 | `sync_lightnovel.py:1131-1135` | `mirror_dir(src, dst, dry_run, allow_delete)` | 注释自称「兼容旧接口」，全项目 **0 次调用**（`plan_mirror` + `apply_mirror` 已被 `sync_to_f` 直接用）。另有一个隐患：它调 `plan_mirror` 时**没传 `known`**，会丢失镜像清单归属判定（所有 F-only 文件都会被当作「非我们放的」） | **已删除** |
| C-2 | `sync_lightnovel.py:406-413` | `git_commit_push(message)` | 提交 + `push_with_retry()` 一步封装，**0 次调用**（`perform_sync` 自己拆开做，因为中间要插 `remove_remote_extras()`） | **已删除** |
| C-3 | `opds_server.py:1793-1807` | `make_qr_png(url, out_path, scale=8)` | 生成二维码 **PNG 文件**，**0 次调用**（CLI 只用终端二维码 `print_qr()`） | **已删除** |
| C-4 | `setup_named_tunnel.py:30` | `import json` | 未使用的导入 | **已删除** |
| C-5 | `setup_named_tunnel.py:91` | `r = run_cf(...)` | 局部变量赋值后未使用（**保留调用本身**） | **已删除赋值** |
| C-6 | `opds_server.py:1245` | `group_size = sum(...)` | `_render_groups()` 内赋值后未使用 | **已删除** |

> 恢复方式：任何一条都可用 `git revert` / `git checkout HEAD~1 -- <file>` 找回；本次清理前另有快照备份在
> `.autosync/backup-20260914-132743/`。

### D 类 — 结构上仍可访问、不算死代码的「保留兼容入口」

| 位置 | 说明 |
| --- | --- |
| `opds_server.py:1165` `book_html(rel, page=1)` | `page` 参数保留以兼容旧 URL，详情页已不再分页 —— 仍被 `do_GET` 调用，**正常生效**，只是参数冗余 |
| `opds_server.py:1324-1325` `index_html()` | 「兼容旧名」→ `root_html()`；`/index.html` 路由确实在用它，**正常生效** |
| `opds_server.py` `read_named_hostname()` 等 | 均被 `start_named_tunnel()` 调用，正常生效 |

---

## 三、附带发现（非用户提问范围内，但值得知道）

1. **注释掉的只有 1 行 Python 代码**（`sync_lightnovel.py:104`）。整个项目里真正「被注释但完整」的代码极少，更多的停用是**开关式**（`ENABLE_SEED_COPY`、`PAT_TOKEN`、`if: false`）—— 这也是为什么把三类分开列。
2. **真正「取消注释后可能影响稳定性」的只有 B-1 与 B-6**：B-1 会退回 HTTPS 大体积推送（历史上被重置过），B-6 会引入抢分支 + 误删风险。其余 A / C 类恢复均为零风险。
3. `run_opds.bat` / `run_named_tunnel.bat` 里的口令选项**建议改用系统环境变量**，避免随仓库外泄。
4. `stop_opds.bat` 按端口 8080 强杀进程，注释已说明「恰好占用 8080 的其他程序也会被杀」—— 若改过 `LN_OPDS_PORT`，此脚本会失效。
5. `.github/workflows/OneDriveSync.yml` 虽已停用，但文件仍在仓库中，`secrets.RCLONE_CONFIG` 的引用依然存在（未使用，无泄露，但值得清理或归档）。

---

## 四、清理执行记录（2026-09-14）

| 项 | 内容 |
| --- | --- |
| 清理范围 | C 类 6 项（3 个零调用函数 + 1 个未用导入 + 2 处未用局部变量） |
| 一并修复 | 上文两个预先存在的缺陷（重复响应头 / `os.kill` 误杀进程），另加 `_pid_alive()` 只读探测 |
| 明确保留 | A 类（注释掉的配置选项）、B 类（开关式功能）、D 类（兼容入口）、`smart_copy()` / `auth_url()` |
| 备份 | `.autosync/backup-20260914-132743/`（三个 .py 原件，为**清理前**状态） |
| 改动量 | 清理：1 增 37 删；修复：`_send()` 重写 + `_pid_alive()` 新增约 55 行 |
| 静态验证 | `pyflakes` 对三个文件 **0 告警**；`py_compile` 通过；AST 零引用扫描仅剩 `start_background`（跨模块调用，误报） |
| 回归验证 | `.autosync/smoke_test.py` → **183 项全部通过**（退出码 0） |
| 真实启动验证 | 以 `opds_server.py --port 18099 --no-qr` 真实启动，7 条路由（`/`、`/index.html`、`/opds/stats`、`/opds/opensearch.xml`、`/opds/catalog/已完结`、`/opds/recent`、`/opds/search`）**全部 200** |

回归套件覆盖：路径安全 / Feed 与 HTML 渲染 / 真实 epub 封面与元数据缓存 / **真实 HTTP 端到端**（内容协商、Range 206+416、流式 ZIP 可解压、Basic 认证 401/200、**响应头单值断言**）/
推送错误分类与指数退避 / `plan_mirror` 三类冲突 / `apply_mirror` 增改删与删除护栏 / 孤儿子树四重安全网 / README 刷新幂等 / 清单与看板 /
**单实例锁（含「拒绝并存时不杀活进程」核心回归）** / 隧道向导与配置解析 / CLI 子进程与 bat 参数一致性 / 真实书库只读巡检。

### 清理过程中发现的两个**预先存在**的缺陷 ✅ **已于 2026-09-14 修复**

| # | 位置 | 现象（修复前已实测复现） | 修复方式 |
| --- | --- | --- | --- |
| 1 | `opds_server.py` `OPDSHandler._send()` | `/cover` 响应发出**两个** `Cache-Control` 头：`no-cache` 与 `public, max-age=604800`（`_send` 无条件写 `no-cache`，`extra` 又追加一个）。客户端按首个取值会看到 `no-cache` → **封面 7 天缓存静默失效**，每次重新下载 | `_send()` 先收集 `extra` 的键（转小写），**仅当 `extra` 未提供 `cache-control` 时**才写默认的 `no-cache`。结果：封面/占位图各只发一个带 `max-age` 的头，HTML 页仍是 `no-cache` |
| 2 | `sync_lightnovel.py` `acquire_lock()` | `os.kill(pid, 0)` 在 Windows 上**不是探测**：CPython 走 `OpenProcess(PROCESS_ALL_ACCESS)` + `TerminateProcess(handle, 0)`，实测目标进程 exit = `3221225794`。后果：启动第二个监控实例会**先把正在运行的那个杀掉**、自己再 log「已有进程在运行」退出 → **监控静默停止** | 抽出 `_pid_alive(pid)` 做只读探测：Win32 走 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(h, 0)`（`WAIT_TIMEOUT` = 仍在运行，**不请求终止权限**）；非 Win32 用标准 `os.kill(pid, 0)`。`acquire_lock()` 改为「锁文件解析失败/进程已死 → 接管并记日志」，不再有裸探测 |

> 两者均已加入回归套件，且各自有一条**直接**回归：
> ① `Cache-Control` 用 `headers.get_all()` 断言单值（`dict(r.headers)` 会吞掉重复头，看不出问题）；
> ② 锁被另一个**真·活进程**持有时，断言 `acquire_lock()` 返回 `False` 且**那个进程仍然存活**（旧实现会在这条断言上失败）。
>
> 附带说明：NTFS 上删除子目录会刷新父目录 mtime，会让「24h 保护」圈住父目录 —— 这正是 `apply_mirror()` 末尾还要 `_prune_empty_dirs(dst)` 兜底的原因，属设计行为，已在套件 `I.` 节按此断言。
