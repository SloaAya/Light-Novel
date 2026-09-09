# 远程书库手机同步方案设计

> 不走 OPDS 协议的纯 HTTP 同步方案。
> 服务端扩展 `opds_server.py`（已具备 Basic Auth 与 HTTP Range 206 基础设施），客户端为 Android 原生 App（Kotlin + Jetpack Compose）。

---

## 0. 设计目标与边界

**做**

- 72 部 / 1225 卷个人书库 → Android 手机本地
- 主流格式：EPUB / PDF / MOBI / AZW3 / TXT
- 断点续传、增量同步、Wi-Fi/蜂窝切换、下载进度、后台任务
- 书架分类、标签管理、本地阅读器集成
- 账号认证（设备维度，不强制注册流程）
- 兼容现有 Cloudflare Named Tunnel（`https://ranqing.ccwu.cc/`）和 OPDS 服务（不冲突）

**不做（v1 范围外）**

- iOS 客户端（暂缓，先吃 Android）
- 阅读器自身（依赖 Moon+ Reader / FBReader / Librera 等已安装应用）
- 双向标注/笔记同步（v2 范围，需要阅读器侧适配）
- 服务端写回（用户不能在手机改书/上传——与现有 D 盘为唯一基准原则一致）

---

## 1. 整体架构

```
┌────────────────────┐    HTTPS / Range / JSON    ┌────────────────────────┐
│  Android Phone     │◀──────────────────────────▶│ Cloudflare Edge        │
│  ┌──────────────┐  │                            │  + Named Tunnel        │
│  │ Sync App     │  │                            └───────────┬────────────┘
│  │  Kotlin/Cor  │  │                                        │
│  │  Room DB     │  │                            ┌───────────▼────────────┐
│  │  WM/Service  │  │◀───────── 8080 ────────────│ opds_server.py +/api  │
│  │  Foreground  │  │                            │ (Python http.server)   │
│  └──────┬───────┘  │                            │ + SQLite (.autosync/  │
│         │          │                            │   devices.db)         │
│         ▼ Intent   │                            └──────────────▲─────────┘
│  ┌──────────────┐  │                                           │
│  │ Moon+ Reader │  │                            D:\Light-Novel\轻小说\
│  │ FBReader     │  │                            (opds_server.py 扫描)
│  │ Librera etc. │  │
│  └──────────────┘  │
└────────────────────┘
```

---

## 2. 鉴权机制：设备配对 + 长期 Token

### 2.1 设计要点

- **不需要注册账号**——书库是单用户私人使用，设备配对即"开账号"
- **配对码是临时的，token 是长期的**：减少攻击面
- **token 不明文存库**：服务端存哈希，丢了也无法重放

### 2.2 流程

```
[首次使用]                              [服务端]
                                       SQLite: devices(id, name, token_hash, created_at, last_seen)
                                       SQLite: pair_codes(code, device_name, expires_at, used)
1. 用户在 App 输入服务地址
   "https://ranqing.ccwu.cc"
   输入设备名 "我的手机"
                                           
2. App 调 POST /api/auth/request-code        ────▶
   { device_name: "我的手机" }                  
                                            ◀──── { pairing_code: "847293",
                                                   expires_at: "...+5min",
                                                   pairing_url: "https://ranqing.ccwu.cc/pair/847293" }
                                                  
3. App 显示 6 位配对码 + 二维码
   用户在 PC 浏览器打开 pairing_url 并点击确认（更安全，物理接触）
   或直接在 App 输入配对码（更快）

4. 浏览器或 App 调 POST /api/auth/exchange  ────▶
   { pairing_code: "847293" }
                                            ◀──── { device_token: "tok_<32bytes_b64url>",
                                                   device_id: 17,
                                                   server_name: "ranqing's 书库" }
                                                   
5. App 把 device_token 存到 EncryptedSharedPreferences
   后续每次请求带 Authorization: Bearer tok_xxx
```

### 2.3 关键设计

| 项 | 决策 | 理由 |
|---|---|---|
| Token 长度 | 32 bytes random → base64url | 256 位熵，足够 |
| Token 存储（服务端） | SHA-256 哈希 | DB 泄露也不影响 |
| Token 存储（客户端） | `EncryptedSharedPreferences` | Android KeyStore 保护 |
| 配对码 TTL | 5 分钟 | 防扫 |
| 配对码长度 | 6 位数字 | 易手输 |
| 设备重命名 | 调 `POST /api/auth/rename` | 允许多设备改名 |
| Token 吊销 | 调 `POST /api/auth/revoke` + 删除服务端记录 | 用户登出 |

### 2.4 服务端存储

新 SQLite：`D:\Light-Novel\.autosync\devices.db`（不入 git，`.autosync/` 已在 .gitignore）

```sql
CREATE TABLE devices (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,   -- SHA-256 hex of token
  created_at TEXT NOT NULL,           -- ISO8601
  last_seen_at TEXT,                  -- ISO8601
  user_agent TEXT,
  scope TEXT DEFAULT 'all'            -- 预留：'all' / 'cat:已完结' 等
);

CREATE TABLE pair_codes (
  code TEXT PRIMARY KEY,              -- 6 digits
  device_name TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE device_state (           -- 客户端进度上传（可选，v2）
  device_id INTEGER,
  book_rel TEXT,
  cfi TEXT,
  percent REAL,
  updated_at TEXT,
  PRIMARY KEY (device_id, book_rel)
);
```

### 2.5 端点清单（鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/auth/request-code` | 输入设备名，返回配对码 |
| `POST` | `/api/auth/exchange` | 6 位码换长期 token |
| `POST` | `/api/auth/rename` | 改名（带 Bearer） |
| `POST` | `/api/auth/revoke` | 吊销当前 token |
| `GET`  | `/api/auth/whoami` | 返回 `{device_id, device_name, last_seen_at, server_name}` |

---

## 3. 清单同步（增量）

### 3.1 核心数据：book inventory

服务端把书库视为一个**扁平的、有序的 manifest**。每条记录 = 一个文件。

```json
{
  "server_time": "2026-09-09T22:41:01+08:00",
  "server_version": 7,
  "total": 1225,
  "truncated": false,
  "books": [
    {
      "id": "已完结/GJ部/GJ部 01.epub",
      "rel": "已完结/GJ部/GJ部 01.epub",
      "size": 1234567,
      "mtime": "2025-01-15T12:34:56+08:00",
      "sha1": "a1b2c3...",
      "format": "epub",
      "category": "已完结",
      "book_name": "GJ部",
      "subdir": "正篇",
      "title": "GJ部 01",
      "author": "...",
      "cover_url": "/api/cover/已完结/GJ部/GJ部 01.epub"
    }
  ]
}
```

**字段说明**：
- `id` 用 `rel` 自身（路径即唯一 ID，避免双向映射）
- `sha1` 关键：用于客户端校验 + 增量变更判断（不只是 mtime）
- `mtime` + `size` 快速筛（无需重算 sha1 时跳过下载）
- `book_name` / `subdir` / `category` 让客户端无须二次解析

### 3.2 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/sync/manifest` | 全量 manifest |
| `GET` | `/api/sync/manifest?since=<iso8601>` | 只返回 mtime > since 的（用 last_modified 索引） |
| `GET` | `/api/sync/manifest?after_id=<id>&limit=500` | 分页拉取（首次同步 1225 本必走） |
| `GET` | `/api/sync/manifest?include=id,size,sha1,mtime` | 轻量模式（只关心变更） |

### 3.3 增量判断逻辑（客户端）

```
for each server_book in manifest:
  local = db.get(server_book.id)
  if local is None:
    state = "new"          → 下载
  elif local.sha1 != server_book.sha1:
    state = "changed"      → 重新下载
  elif local.size != server_book.size:
    state = "size_changed" → 重新下载（保守，sha1 必变）
  else:
    state = "ok"           → 跳过

for each local_book in db:
  if local_book.id not in server_ids:
    state = "removed"      → 客户端决定删除或保留
```

### 3.4 性能

- 1225 本 × 200 字节/条 ≈ 250 KB，全量拉一次够
- `since=` 增量：日常只传几本变更，< 10 KB
- 客户端首启冷同步：分页 500 × 3 次 ≈ 750 KB
- 服务端可 gzip 压缩 manifest，再省 70%

---

## 4. 下载与断点续传

### 4.1 服务端（已有 + 增强）

`opds_server.py` 已有 `_serve_file` 支持 HTTP Range 与 206 Partial Content。新增：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/download/<encoded_rel>` | 下载（带 Range 支持） |
| `GET` | `/api/cover/<encoded_rel>` | 封面（独立缓存路径，避免污染 /cover/） |
| `GET` | `/api/book/<encoded_rel>` | 单本书元数据 JSON |
| `HEAD` | `/api/download/<encoded_rel>` | 客户端用 HEAD 探测服务器侧 size / ETag / Last-Modified |

**响应头**：
```
Accept-Ranges: bytes
ETag: "<sha1>"
Last-Modified: <mtime>
Content-Disposition: attachment; filename*=UTF-8''<urlencoded name>
```

### 4.2 客户端断点续传

```
状态机（每个 book 一个 DownloadTask）：
  PENDING → DOWNLOADING ⇄ PAUSED → COMPLETED
                        ↘ FAILED → RETRYING ↗
                        ↘ CANCELED
```

**算法**：
```
fun download(book):
  target = books/<rel>
  if exists(target) and sha1(target) == book.sha1: skip
  tmp = target + ".part"
  downloaded = tmp.size() if exists(tmp) else 0
  if downloaded >= book.size: 
    rename(tmp, target); complete()
    return
  if downloaded > 0:
    request.addHeader("Range", "bytes=$downloaded-")
  response = server.get("/api/download/<rel>")
  // 校验 206 + Content-Range 起点 = downloaded
  append(response.body, tmp)
  while not complete:
    // 流式写入，监听 cancel/pause/error
    notify(progress = (downloaded + bytes_received) / size)
  if sha1(tmp) != book.sha1: retry from 0
  else: rename(tmp, target)
```

**关键点**：
- `.part` 文件用 tmpfs / 应用私有目录，崩溃后保留
- 应用重启时扫描 `<books>/**/*.part` 自动入队重试
- 客户端周期写"断点位置"到 Room（`downloads` 表的 `bytes_done` 字段）
- 进度显示用 StateFlow，让 UI 实时刷新

### 4.3 网络切换处理

| 状态 | 行为 |
|---|---|
| Wi-Fi 切蜂窝 | Android 系统自动断开 socket；客户端捕获 IOException 标记 PAUSED |
| 蜂窝切 Wi-Fi | 同上 |
| 应用切后台 | WorkManager 调度任务被冻结；前台 Service 继续 |
| 完全断网 | WorkManager 任务进入 WAITING；联网后自动 retry |

**关键配置**：
- `WorkManager Constraints.NetworkType.UNMETERED`：仅 Wi-Fi 同步（用户设置）
- `WorkManager Constraints.NetworkType.CONNECTED`：任何网
- 后台同步默认 `UNMETERED`，用户可在设置改

---

## 5. 后台任务管理

### 5.1 三种任务载体

| 任务类型 | Android 载体 | 限制 | 用途 |
|---|---|---|---|
| 单次主动下载 | **Foreground Service** + Notification | 仅用户触发 | UI 显示进度、可取消 |
| 周期全量同步 | **PeriodicWorkRequest** (WorkManager) | 系统调度 | 每 6 / 12 / 24 小时 |
| 网络恢复续传 | **OneTimeWorkRequest** (WorkManager) | 系统调度 | 监听 ConnectivityManager |

### 5.2 关键代码骨架

```kotlin
// 1. 主动下载（前台 Service）
class DownloadService : Service() {
  override fun onStartCommand(intent: Intent, flags: Int, startId: Int): Int {
    startForeground(NOTIF_ID, buildNotification())
    scope.launch { runDownload(intent.getStringExtra("rel")!!) }
    return START_STICKY
  }
}

// 2. 周期同步
val syncRequest = PeriodicWorkRequestBuilder<SyncWorker>(6, TimeUnit.HOURS)
  .setConstraints(Constraints.Builder()
    .setRequiredNetworkType(NetworkType.UNMETERED)
    .setRequiresBatteryNotLow(true)
    .build())
  .build()
WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
  "sync", ExistingPeriodicWorkPolicy.KEEP, syncRequest)

// 3. 网络恢复
val networkRequest = NetworkRequest.Builder()
  .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
  .build()
cm.registerNetworkCallback(networkRequest, object : NetworkCallback() {
  override fun onAvailable(network: Network) {
    WorkManager.getInstance(ctx).enqueue(OneTimeWorkRequestBuilder<ResumeWorker>().build())
  }
})
```

### 5.3 下载队列

```kotlin
@Entity
data class DownloadTask(
  @PrimaryKey val rel: String,        // book.id
  val state: State,                    // PENDING / DOWNLOADING / ...
  val bytesDone: Long,
  val totalBytes: Long,
  val errorMsg: String?,
  val priority: Int,                   // 用户手动下载 = 10，周期同步 = 1
  val createdAt: Long,
  val updatedAt: Long,
  val attempt: Int                     // 重试次数
)
```

- 队列处理器：`Channel<DownloadTask>` + 单 worker
- 同 book 重复入队 → 去重（idempotent）
- 用户主动下载的优先级高于周期同步

---

## 6. 书架分类与标签

### 6.1 服务端

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET`  | `/api/shelves` | 列出所有书架 |
| `POST` | `/api/shelves` | 新建 `{name, book_ids: [...]}` |
| `PATCH` | `/api/shelves/{id}` | 改名 / 改书 |
| `DELETE` | `/api/shelves/{id}` | 删书架（不删书） |
| `GET`  | `/api/tags` | 所有标签云 |
| `GET`  | `/api/tags?book_id=<id>` | 单本书的标签 |

> **不存储在书库目录结构**——书库目录（已完结/未完结/正篇/番外）是物理组织，标签/书架是逻辑组织，两者正交。
> 实际：服务端读 `<root>/.labels.json`（如果存在），用户用文本编辑器或后续的 PC 端工具维护。

```json
{
  "shelves": [
    {"name": "在追", "book_ids": ["未完结/GJ部", "未完结/樱花庄的宠物女孩"]},
    {"name": "重读", "book_ids": [...]}
  ],
  "tags": {
    "未完结/GJ部": ["校园", "日常", "轻喜剧"],
    "已完结/樱花庄的宠物女孩": ["校园", "创作", "青春"]
  }
}
```

### 6.2 客户端本地

- `shelves` 表镜像服务端 + 客户端可建离线书架
- 书架/标签都是**派生数据**：选书 = `db.bookDao().byTag(...)`，不冗余存关联

---

## 7. 本地阅读器集成

### 7.1 Intent 派发

```kotlin
fun openBook(book: Book) {
  val file = File(book.localPath)
  val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", file)
  val mime = when (book.format) {
    "epub"  -> "application/epub+zip"
    "pdf"   -> "application/pdf"
    "mobi"  -> "application/x-mobipocket-ebook"
    "azw3"  -> "application/vnd.amazon.ebook"
    "txt"   -> "text/plain"
    else    -> "*/*"
  }
  val intent = Intent(Intent.ACTION_VIEW).apply {
    setDataAndType(uri, mime)
    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
  }
  if (intent.resolveActivity(pm) != null) {
    startActivity(Intent.createChooser(intent, "用 $defaultReader 打开"))
  } else {
    toast("未找到能打开 ${book.format} 的应用，请安装 Moon+ Reader")
  }
}
```

### 7.2 推荐阅读器

| App | EPUB | PDF | MOBI | AZW3 | TXT | 备注 |
|---|---|---|---|---|---|---|
| **Moon+ Reader** | ★★★ | ★★★ | ★★ | ★ | ★★★ | 推荐默认，UI 友好 |
| **FBReader** | ★★★ | ★★ | ★ | × | ★★★ | 开源，纯净 |
| **Librera** | ★★★ | ★★★ | ★★ | ★ | ★★★ | 全格式 |
| **ReadEra** | ★★★ | ★★★ | ★★ | × | ★★★ | 国产，UI 现代 |
| **Calibre Companion** | ★★★ | × | × | × | × | 仅 EPUB |

App 在设置里可指定"默认阅读器"，未指定时弹 chooser。

---

## 8. 数据模型（Room）

```kotlin
// 镜像服务端 manifest
@Entity(tableName = "books")
data class Book(
  @PrimaryKey val rel: String,           // server id
  val size: Long,
  val mtime: String,                     // ISO8601
  val sha1: String,
  val format: String,                    // epub/pdf/mobi/azw3/txt
  val category: String,                  // 已完结 / 未完结
  val bookName: String,                  // GJ部
  val subdir: String?,                   // 正篇 / 番外 / null
  val title: String,                     // GJ部 01
  val author: String?,
  val localPath: String?,                // null = 未下载
  val downloadedAt: Long?,               // epoch millis
  val downloadState: String = "NOT_DOWNLOADED"  // NOT/DOWNLOADING/COMPLETED/FAILED
)

@Entity(tableName = "tags")
data class Tag(
  @PrimaryKey(autoGenerate = true) val id: Long = 0,
  val bookRel: String,                   // FK → books.rel
  val name: String                       // "校园" / "重读"
)

@Entity(tableName = "shelves")
data class Shelf(
  @PrimaryKey val name: String,          // 用户起的名
  val source: String = "local"           // "local" / "server"
)

@Entity(tableName = "shelf_books", primaryKeys = ["shelfName", "bookRel"])
data class ShelfBook(
  val shelfName: String,
  val bookRel: String
)

@Entity(tableName = "downloads")
data class Download(
  @PrimaryKey val rel: String,
  val state: String,                     // PENDING/DOWNLOADING/PAUSED/...
  val bytesDone: Long,
  val totalBytes: Long,
  val errorMsg: String?,
  val priority: Int = 1,
  val createdAt: Long,
  val updatedAt: Long,
  val attempt: Int = 0
)

@Entity(tableName = "progress")
data class ReadingProgress(
  @PrimaryKey val rel: String,           // FK → books.rel
  val cfi: String,                       // EPUB CFI / PDF page / TXT line
  val percent: Float,
  val updatedAt: Long
)
```

---

## 9. 客户端技术栈

| 组件 | 选型 | 理由 |
|---|---|---|
| 语言 | Kotlin 2.0 | 一等公民 |
| UI | Jetpack Compose | 现代、声明式 |
| 架构 | MVVM + UseCase | 标准 |
| DI | Hilt | Google 官方 |
| 异步 | Coroutines + Flow | 替代 RxJava |
| 网络 | Retrofit + OkHttp | 事实标准 |
| JSON | kotlinx.serialization | 编译期生成 |
| 本地 DB | Room | 关系查询 |
| 加密存储 | EncryptedSharedPreferences | KeyStore 保护 token |
| 图片 | Coil | Compose 友好 |
| 后台 | WorkManager + Foreground Service | 系统级支持 |
| 协程 | Structured concurrency | 父子任务清晰 |
| 最低 SDK | API 26 (Android 8.0) | 覆盖 95%+ 用户 |
| 目标 SDK | API 34 (Android 14) | 适配 scoped storage |

**包结构**：
```
com.ranqing.lightnovel
├── data/
│   ├── api/         Retrofit service 接口
│   ├── db/          Room DAO + Database
│   ├── model/       DTO + Entity 转换
│   └── repo/        Repository
├── domain/
│   ├── model/       业务模型
│   └── usecase/     DownloadBook, SyncManifest, ...
├── ui/
│   ├── library/     书架列表
│   ├── downloads/   下载管理
│   ├── settings/    设置
│   └── theme/       Compose 主题
├── service/
│   ├── DownloadService.kt
│   └── NetworkObserver.kt
└── util/
    ├── Crypto.kt
    └── MimeTypes.kt
```

---

## 10. 安全考虑

| 风险 | 缓解 |
|---|---|
| 配对码暴力破解 | 6 位数字 + 5min TTL + 单次使用；可加 IP 限制（仅同子网） |
| Token 泄露 | 仅存哈希、明文只回写客户端一次；EncryptedSharedPreferences |
| 中间人 | HTTPS（Cloudflare 已提供）；App 启用 Network Security Config 严格模式（仅信任 Cloudflare 证书链） |
| 设备丢/换 | 旧设备上 App 登出 = `POST /api/auth/revoke`；新设备走完整配对 |
| 增量同步泄露书单 | 整条 API 都在 Bearer 后面；未配对的设备 401 |
| 恶意下载 | 服务端 `_safe_relpath` 防御目录穿越；限制下载文件后缀白名单 |
| 限速/防滥用 | 每设备并发 2，单 IP 限速（v2 范围） |

---

## 11. 渐进交付路线图

### v1（MVP，约 2-3 周）
- 设备配对 + token
- 一次性全量 manifest
- 单本同步下载（前台 Service + 进度通知）
- 库列表 UI（按 category 分类）
- 点击用 Moon+ 打开
- 公共 Cloudflare Tunnel 已就绪

### v2（约 2 周）
- 增量同步（`?since=`）
- 断点续传（`.part` + Range）
- 周期同步（WorkManager + Wi-Fi only 约束）
- 后台 Service 在切后台后继续
- 通知中心（多任务进度）

### v3（约 2 周）
- 书架 / 标签 / 搜索
- 离线优先（数据库冲突合并）
- 客户端加密（可选，应用内文件加密）
- 阅读进度上报（v2 设备进度接口）

### v4（持续）
- iOS 客户端（如果用户需要）
- PC 客户端（Electron 或 Tauri）
- 元数据编辑反馈（PC 端发起，手机端下载新元数据）

---

## 12. 与现有系统的兼容性

| 现有 | 新系统 | 处理 |
|---|---|---|
| OPDS 根 `/` | `/api/manifest` | 各自独立路径，不冲突 |
| `/opds/catalog/<cat>` | `/api/category/<cat>/books` | 各自独立 |
| `/cover/<rel>` | `/api/cover/<rel>` | 各自独立（也可考虑合并） |
| `/dl/<rel>` | `/api/download/<rel>` | 各自独立 |
| Named Tunnel `ranqing.ccwu.cc` | 同域名 | 复用，无需变更 |
| `LN_OPDS_USER` / `LN_OPDS_PASS` 环境变量 | 新增 `LN_SYNC_*` | 不冲突 |

**结论**：两套系统可以共存，OPDS 给通用阅读器订阅，新 `/api/sync/*` 给专用 App 用。

---

## 13. 关键技术风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 1225 本首启冷同步慢 | 用户首次体验差 | 分页 + 进度条 + 后台化 |
| 手机存储不够 | 6 GB+ 库装不下 | UI 强调"按需下载"，不自动全量 |
| Cloudflare Tunnel 带宽限制 | 大文件下载慢 | 城市内高峰可能限速；用户能感知 |
| Android 14 scoped storage | 文件管理复杂 | 用 `getExternalFilesDir` 应用私有目录 |
| 阅读器 Intent 失败 | 用户无法打开 | 多选 chooser + 推荐安装列表 |
| 多设备同步冲突 | 进度乱 | 设备独立 progress 表，最新 `updated_at` 优先 |

---

## 14. 决策记录

| 决策 | 选项 | 选定 | 原因 |
|---|---|---|---|
| 客户端平台 | Android / iOS / PWA / Web | **Android** | 用户已用 CC 推断 |
| 客户端语言 | Kotlin Native / Flutter / RN | **Kotlin** | 单平台、生态最强 |
| UI 框架 | Compose / View | **Compose** | 新项目首选 |
| 服务端语言 | 现有 Python / Node / Go | **Python** | 复用 opds_server.py |
| 数据存储 | SQLite / 文件 / Postgres | **SQLite** | 单用户、无并发 |
| Token 存哪 | DB 哈希 / DB 明文 / JWT | **DB 哈希** | 最简、可吊销 |
| 协议 | REST / gRPC / GraphQL | **REST + JSON** | 调试容易、阅读器集成容易 |
| 文件路径 | URL-encoded / ID-based | **URL-encoded rel** | 与 OPDS 一致 |
| 配对码长度 | 4 / 6 / 8 位 | **6 位** | 易手输 + 5min TTL |
| 进度上报 | 实时 / 批量 | **批量**（v2） | 省电省流量 |
| WebDAV fallback | 是 / 否 | **否** | 与设计目标冲突；如需用 Calibre Web 的 WebDAV 客户端 |

---

## 15. 落地第一步

如果你认可这个设计，建议这样推进：

1. **今天**（在 21:07 代码基础上）
   - 在 `opds_server.py` 加 `/api/auth/*` 5 个端点（~150 行）
   - 加 `devices.db` schema + 配对码逻辑（~100 行）
   - curl 自测配对 → 拿 token → 调 `/api/auth/whoami`

2. **明天**
   - 加 `/api/sync/manifest` 全量 + `?since=` + `?after_id=`（~200 行）
   - 加 `/api/book/<id>` + `/api/download/<id>`（已基本现成）
   - 服务端联调

3. **下周**
   - 起 Android 项目骨架（Kotlin + Compose + Room + Hilt）
   - 实现配对流程 + 库列表 UI
   - 提交到 GitHub

4. **第 3-4 周**
   - 断点续传 + 后台 Service
   - 阅读器 Intent 集成
   - 端到端联调

是否要我开始第 1 步？或者你想先讨论哪部分的取舍？
