<div align="center">

# 📚 Light Novel Collection

### 个人轻小说收藏库

📦 EPUB · 🤖 自动整理 · ☁️ 云端双备份 · 🌐 在线书源 · 🖥️ 图形控制面板

**🌐 在线访问：[https://ranqing.ccwu.cc/](https://ranqing.ccwu.cc/)**

</div>

---

## 📖 书单

> 以下书单由脚本自动维护，按名称排序，随藏书变动实时更新。

<details>
<summary>📚 未完结作品</summary>

- NO GAME NO LIFE
- Silent Witch 沉默魔女的秘密
- 三坪房间的侵略者！？
- 不过是偶像！～但是果然颜值好高～
- 刀剑神域
- 加速世界
- 和非常可爱的我交往吧！
- 在地下城寻求邂逅是否搞错了什么
- 在男性禁入的游戏世界里，我唯一该做的事情
- 孤单一人的异世界攻略
- 弱角友崎同学
- 弹珠汽水瓶里的千岁同学
- 恶女不才，请多关照！～雏宫蝶鼠换身传～
- 想要确定真命天女之前，可以先拿我试试哦。
- 我和班上第二可爱的女生成为朋友
- 我的女性朋友意外地有求必应
- 我被踢出勇者队伍而回到老家，但队员们竟然全都跟了过来
- 战斗员派遣中！
- 无职转生～蛇足篇～
- 时隔10年重逢，臭小鬼长成了清纯美少女JK
- 杖与剑的魔剑谭
- 某魔法的禁书目录
- 欢迎来到实力至上主义的教室
- 漆黑的子弹
- 物语系列
- 玩乐关系
- 盾之勇者成名录
- 谁杀了勇者
- 败北女角太多了！
- 身为阴角的我换座位後意外被S级美少女们包围
- 这里是终末停滞委员会
- 霜月同学喜欢上路人角色

</details>

<details>
<summary>✅ 已完结作品</summary>

- GAMERS电玩咖
- GJ部
- High School D×D
- 【好消息】我的不起眼未婚妻在家有够可爱
- 不正经的魔术讲师与禁忌教典
- 不起眼女主角培育法
- 与奔驰于透明之夜的你，谈一场看不见的恋爱。
- 为了女儿，我说不定连魔王都能干掉。
- 为美好的世界献上祝福
- 借给朋友500圆，他竟然拿妹妹来抵债，我到底该如何是好
- 全金属狂潮
- 关于我转生变成史莱姆这档事
- 农林
- 凉宫春日系列
- 原本阴沉的我要向青春复仇 和那个天使般的女孩一起Re life
- 古龙武侠小说丛集
- 只要长得可爱，即使是变态你也喜欢吗？
- 噬血狂袭
- 在朋友背后与你悄悄牵手,谈着无法言说的恋爱
- 夹在我女友和青梅竹马间的各种修罗场
- 如果折断她的旗
- 妹妹人生
- 学战都市Asterisk
- 小恶魔学妹缠上了被女友劈腿的我
- 就算是哥哥，有爱就没问题了，对吧
- 平凡职业成就世界最强
- 怪人的沙拉碗
- 我们的重置人生
- 我和女友的妹妹接吻了。
- 我当备胎女友也没关系。
- 我的妹妹哪有这么可爱！
- 我的妹妹是最棒的配菜
- 我的脑内恋碍选项
- 放学后，到异世界咖啡厅喝杯咖啡
- 无职转生 ～到了异世界就拿出真本事～
- 有谁规定了现实中不能有恋爱喜剧的？
- 朋友的妹妹只缠着我
- 樱花庄的宠物女孩
- 灼眼的夏娜
- 狼与香辛料
- 用催眠APP打造梦幻后宫生活
- 电波女＆青春男
- 碧阳学园学生会
- 空之境界
- 笨蛋，测验，召唤兽
- 约会大作战
- 线上游戏的老婆不可能是女生？
- 落第骑士英雄谭
- 袭来，美少女邪神！
- 重启咲良田
- 金庸武侠小说丛集
- 龙族

</details>

---

## 🚀 快速开始

### 方式一：图形控制面板（推荐）

双击 `launchers\run_panel.bat`，或先打包出 exe 再双击它：

```bat
:: 首次打包（只做一次）
python -m venv .venv
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller build\lightnovel.spec --noconfirm --distpath dist --workpath build\pyi-work
:: 产物：dist\LightNovel.exe
```

`LightNovel.exe` 双击即开控制面板：一键启停 OPDS 书源 / 目录监控、执行同步与镜像、
跑隧道向导、查看实时状态灯与输出日志。运行时状态仍写在 `D:\Light-Novel\.autosync\`，
不会塞进 exe 内部。

### 方式二：命令行

```bat
python -m lightnovel                     :: 控制面板
python -m lightnovel opds --port 8080    :: OPDS 书源服务
python -m lightnovel sync                :: 同步 + 持续监控
python -m lightnovel sync --once         :: 同步一次后退出
python -m lightnovel mirror --status     :: 只读查看 D 盘 / F 盘差异
python -m lightnovel tunnel-setup        :: Cloudflare 固定域名隧道向导
```

加 `--help` 看某个子命令的完整参数，例如 `python -m lightnovel opds --help`。

### 启动器一览（`launchers\`）

| 文件 | 作用 |
| --- | --- |
| `run_panel.bat` | 打开图形控制面板（无控制台窗口） |
| `run_opds.bat` | 启动 OPDS 书源服务 |
| `run_monitor.bat` | 后台启动目录监控（`pythonw`，无窗口） |
| `run_once.bat` | 同步一次后退出 |
| `run_named_tunnel.bat` | 启动固定域名隧道（隧道连通后自动隐藏窗口） |
| `setup_named_tunnel.bat` | Cloudflare 固定域名隧道配置向导 |
| `stop_opds.bat` | 停止 8080 端口的 OPDS 服务与 cloudflared |

---

## 🗂️ 项目结构

```
D:\Light-Novel\
├─ lightnovel\                主包（python -m lightnovel）
│  ├─ paths.py                 全局配置与路径 —— 唯一真源
│  ├─ cli.py                   统一命令行入口
│  ├─ ui.py                    Tkinter 图形控制面板
│  ├─ tunnel_setup.py          Cloudflare 固定域名隧道向导
│  ├─ opds\
│  │  ├─ library.py            路径工具 / 目录索引 / 封面提取 / epub 元数据 / zip 流式打包
│  │  ├─ feeds.py              Atom feed（导航型 + 获取型）与 HTML 视图
│  │  └─ server.py             HTTP Handler / 服务启动 / cloudflared / 二维码 / CLI
│  └─ sync\
│     ├─ gitops.py             git 封装 / 仓库初始化 / 分块推送重试 / README 维护 / 扫描
│     ├─ mirror.py             F 盘镜像：清单 / 差异比对 / 删除 / 执行 / 看板
│     └─ monitor.py            种子复制 / 快照变更检测 / 单实例锁 / 监控循环 / CLI
├─ launchers\                 批处理启动器
├─ tests\smoke_test.py        隔离式回归测试（184 项）
├─ build\
│  ├─ lightnovel.spec         PyInstaller 打包配置
│  └─ entry.py                打包入口脚本
├─ docs\                      架构与功能审计文档
├─ pyproject.toml
├─ requirements.txt
└─ 轻小说\                     书库本体（已完结 / 未完结）
```

依赖方向严格单向，不存在循环引用：

```
opds:  library → feeds → server
sync:  gitops  → mirror → monitor
```

---

## ⚙️ 自动化

| 功能 | 说明 |
| --- | --- |
| 📥 实时监控 | `轻小说/已完结` 与 `轻小说/未完结` 目录有变动即自动触发同步 |
| 🔄 GitHub 同步 | 自动提交并推送；以本地为准，远程多余文件自动清理 |
| ☁️ 网盘备份 | 自动镜像到网盘 `F:\LightNovel`（CloudDrive2），增 / 改 / 删 全量同步 |
| 🗑️ 删除传播 | F 盘多余文件/目录直接删除（多层安全护栏，只删镜像目标里 D 盘没有的内容） |
| ⚠️ 冲突检测 | F 侧被独立修改、清单外孤儿文件均会告警；24h 内新增/修改的 F 侧文件自动保护 |
| 📝 书单维护 | 本 README 的两个书单区块自动刷新，其余内容保持不变 |
| 🛡️ 安全护栏 | 大规模删除上限保护 + 系统垃圾文件（desktop.ini 等）自动排除 + 路径逃逸校验 |
| 📱 手机书源 | 内置 OPDS 1.2 书源服务，公网地址 <https://ranqing.ccwu.cc/> |
| 🖥️ 控制面板 | Tkinter 面板统一启停与状态查看，可打包为单文件 `LightNovel.exe` |

---

## 🧪 回归测试

```bat
python tests\smoke_test.py
```

184 项断言，覆盖 OPDS 全链路（feed / HTML / 封面 / 元数据 / Range 下载 / ZIP 流式打包 /
Basic 认证）、镜像差异与安全护栏、单实例锁只读探测、隧道向导解析、CLI 与启动器一致性，
以及对真实书库的只读巡检。所有写操作隔离在 `.autosync\_testtmp\` 下，不会推送 Git、
不写真实 F 盘、不动任何书籍文件。

---

<div align="center">
<sub>由 <code>lightnovel.sync</code> 自动维护 · 最后同步见提交记录</sub>
</div>
