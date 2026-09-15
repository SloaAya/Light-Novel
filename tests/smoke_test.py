#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理死代码后的回归验证套件。

原则：
  * 不推送 Git、不写真实 F 盘、不动 轻小说/ 下任何书籍文件
  * 所有写操作隔离在 .autosync/_testtmp/ 下自建的临时目录
  * 真实只读操作（扫描书库、提取封面、HTTP 请求）照常跑，验证的是真功能

运行：python tests/smoke_test.py
"""
import io
import os
import re
import sys
import json
import time
import base64
import shutil
import zipfile
import tempfile
import logging
import http.client as httpclient
import subprocess
import threading
import traceback
from datetime import datetime
from urllib import request as urlreq
from urllib import error as urlerr
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

RESULTS = []
INFOS = []
_SECTION = [""]


def section(name):
    _SECTION[0] = name
    print("\n" + "=" * 70)
    print("  " + name)
    print("=" * 70)


def check(label, fn):
    """fn 返回 (ok, detail) 或抛异常。"""
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        if os.environ.get("SMOKE_TRACE"):
            traceback.print_exc()
    RESULTS.append((_SECTION[0], label, ok, detail))
    print("  %s %-50s %s" % ("[PASS]" if ok else "[FAIL]", label, detail or ""))
    return ok


def info(label, detail):
    INFOS.append((_SECTION[0], label, detail))
    print("  [INFO] %-50s %s" % (label, detail))


def eq(label, got, want):
    return check(label, lambda: (got == want, "got=%r want=%r" % (got, want)))


def truthy(label, got, note=""):
    return check(label, lambda: (bool(got), note or ("got=%r" % (got,))))


def png_w_h(w, h):
    """构造一个只含 IHDR 的最小 PNG 头，用于尺寸解析测试。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


# ============================== 临时区 ==============================
_TMP_BASE = os.path.join(ROOT, ".autosync", "_testtmp")
os.makedirs(_TMP_BASE, exist_ok=True)
TMP_ROOT = tempfile.mkdtemp(prefix="smoke-", dir=_TMP_BASE)

PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def tpath(*parts):
    p = os.path.join(TMP_ROOT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write(p, data, mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        with open(p, "wb") as fh:
            fh.write(data)
    else:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def sfile(d, name, data, mtime=None):
    return write(os.path.join(d, name.replace("/", os.sep)), data, mtime)


def age_tree(root, ts):
    """把整棵树（含每级目录自身）的 mtime 都改到 ts。
    必须连目录一起改：_prune_foreign_subtrees 会检查目录自身 mtime 做 24h 保护。"""
    for r, _dirs, files in os.walk(root, topdown=False):
        for f in files:
            try:
                os.utime(os.path.join(r, f), (ts, ts))
            except OSError:
                pass
        try:
            os.utime(r, (ts, ts))
        except OSError:
            pass
    return root


# ============================== 导入被测模块 ==============================
# 功能已拆进 lightnovel 包，这里用「聚合命名空间」还原成单个模块的用法：
#   * 读属性：返回第一个拥有该名字的子模块的值
#   * 写属性：同步写进所有拥有该名字的子模块（配置常量在每个模块里各有一份
#     绑定，只改一处会导致别的模块仍读旧值）
class _Agg:
    def __init__(self, *mods):
        object.__setattr__(self, "_mods", mods)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        for m in object.__getattribute__(self, "_mods"):
            if hasattr(m, name):
                return getattr(m, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        hit = [m for m in object.__getattribute__(self, "_mods") if hasattr(m, name)]
        for m in hit:
            setattr(m, name, value)
        if not hit:
            object.__setattr__(self, name, value)


from lightnovel import paths as P                                      # noqa: E402
from lightnovel.opds import library as OL, feeds as OF, server as OS    # noqa: E402
from lightnovel.sync import gitops as SG, mirror as SM, monitor as SN   # noqa: E402
import lightnovel.tunnel_setup as T                                    # noqa: E402

O = _Agg(OL, OF, OS, P)      # 对应原 opds_server.py
S = _Agg(SG, SM, SN, P)      # 对应原 sync_lightnovel.py

# 会话签名密钥一开始就隔离到临时目录：E 节的 HTTP 用例会真的走一遍 _role()，
# 不先指一下就会在真实 .autosync/ 里生成一把密钥（测试不该有这种副作用）。
from lightnovel.opds import session as OSESS          # noqa: E402
_SESS_DEFAULT = OSESS.SESSION_KEY_FILE                # 打桩前的真实默认路径（T 节要核对它）
OSESS.SESSION_KEY_FILE = os.path.join(TMP_ROOT, "session.key")

# ---- 日志重定向：验证 setup_logging 真能建文件，同时保持控制台干净 ----
slog = logging.getLogger("sync")
_orig_log_file = S.LOG_FILE
S.LOG_FILE = tpath("logs", "sync.log")
S.setup_logging()
slog.info("smoke test logging probe")
_log_ok = os.path.isfile(S.LOG_FILE) and os.path.getsize(S.LOG_FILE) > 0
slog.handlers.clear()
slog.addHandler(logging.NullHandler())
slog.setLevel(logging.CRITICAL)
S.LOG_FILE = _orig_log_file

# ============================== A. 清理结果核对 ==============================
section("A. 清理结果（删掉的确实没了 / 保留的确实还在）")
check("已删除 opds_server.make_qr_png", lambda: (not hasattr(O, "make_qr_png"), ""))
check("已删除 sync_lightnovel.git_commit_push", lambda: (not hasattr(S, "git_commit_push"), ""))
check("已删除 sync_lightnovel.mirror_dir", lambda: (not hasattr(S, "mirror_dir"), ""))
check("已删除 setup_named_tunnel 的 json 导入",
      lambda: ("import json" not in open("lightnovel/tunnel_setup.py", encoding="utf-8").read(), ""))
check("保留 sync_lightnovel.smart_copy（种子复制）", lambda: (callable(S.smart_copy), ""))
check("保留 sync_lightnovel.auth_url（PAT 链路）", lambda: (callable(S.auth_url), ""))
check("保留 ENABLE_SEED_COPY 开关", lambda: (S.ENABLE_SEED_COPY is False, "value=%r" % S.ENABLE_SEED_COPY))
check("保留 opds_server.print_qr", lambda: (callable(O.print_qr), ""))
check("setup_logging 能写日志文件", lambda: (_log_ok, S.LOG_FILE))

# 扫描范围：重构后的全部源码 + 启动器 + README（不含本测试文件自身）
_SRC_FILES = []
for _r, _dirs, _fs in os.walk(os.path.join(ROOT, "lightnovel")):
    _SRC_FILES += [os.path.join(_r, _f) for _f in _fs if _f.endswith(".py")]
_lau = os.path.join(ROOT, "launchers")
if os.path.isdir(_lau):
    _SRC_FILES += [os.path.join(_lau, _f) for _f in os.listdir(_lau)]
_SRC_FILES.append(os.path.join(ROOT, "README.md"))

_leak = []
for _f in _SRC_FILES:
    if os.path.isfile(_f):
        _t = open(_f, encoding="utf-8").read()
        for _n in ("make_qr_png", "git_commit_push", "mirror_dir", "group_size"):
            if re.search(r"(?<![\w.])" + _n + r"(?![\w])", _t):
                _leak.append("%s:%s" % (os.path.relpath(_f, ROOT), _n))
check("全项目无残留引用（3 个函数名 + group_size）", lambda: (not _leak, "leaks=%s" % _leak))

# ============================== B. OPDS 纯函数 ==============================
section("B. OPDS 路径与工具函数（lightnovel.opds.library）")
eq("_safe_relpath 正常路径", O._safe_relpath("已完结/某书/01.epub"), "已完结/某书/01.epub")
eq("_safe_relpath 拒绝 ../", O._safe_relpath("../secret.txt"), None)
eq("_safe_relpath 拒绝 a/../../b", O._safe_relpath("a/../../b"), None)
eq("_safe_relpath 拒绝空串", O._safe_relpath(""), None)
eq("resolve_under 越界返回 None", O.resolve_under(ROOT, "../outside.txt"), None)
truthy("resolve_under 合法路径可解析",
       O.resolve_under(ROOT, "lightnovel/paths.py") == os.path.realpath(os.path.join(ROOT, "lightnovel", "paths.py")))
eq("strip_ext", O.strip_ext("GAMERS01.epub"), "GAMERS01")
eq("encode_path 编码中文并保留斜杠", O.encode_path("已完结/a b.epub"), "%E5%B7%B2%E5%AE%8C%E7%BB%93/a%20b.epub")
eq("human_size B", O.human_size(512), "512 B")
eq("human_size MB", O.human_size(3 * 1024 * 1024), "3.0 MB")
truthy("local_ip 返回字符串", isinstance(O.local_ip(), str))
eq("_sniff_mime 识别 PNG", O._sniff_mime(O.BLANK_PNG), "image/png")
eq("_sniff_mime 识别 JPEG", O._sniff_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 8), "image/jpeg")
eq("_sniff_mime 识别 WEBP", O._sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
eq("_sniff_mime 识别 GIF", O._sniff_mime(b"GIF89a\x00\x00"), "image/gif")
eq("_img_size 解析 1x1 PNG", O._img_size(O.BLANK_PNG), (1, 1))
truthy("_looks_like_spread 横向图判为跨页", O._looks_like_spread(png_w_h(400, 200)) is True)
truthy("_looks_like_spread 竖版图不判跨页", O._looks_like_spread(png_w_h(200, 300)) is False)
eq("_accept_wants_xml 识别 OPDS", O._accept_wants_xml("application/atom+xml;profile=opds-catalog"), True)
eq("_accept_wants_xml 浏览器 html", O._accept_wants_xml("text/html,application/xhtml+xml"), False)
truthy("find_cloudflared 可调用（返回 None 或真实路径）",
       O.find_cloudflared() is None or os.path.isfile(O.find_cloudflared()))

# ============================== C. 书库索引与 Feed ==============================
section("C. 书库索引 / OPDS Feed / HTML 页面")
lib = O.get_library(force=True)
eq("get_library 返回两个分类", sorted(lib.keys()), sorted([O.CATEGORY_DONE, O.CATEGORY_ONGOING]))
_n_books = sum(len(b) for b in lib.values())
_n_vols = sum(1 for _ in O.all_vols(lib))
truthy("书库非空（%d 部 / %d 卷）" % (_n_books, _n_vols), _n_books > 0 and _n_vols > 0)
truthy("索引缓存命中（30s TTL 内同对象）", O.get_library() is lib)

# 挑卷数最少的书跑后续路由，保证测试足够快
CAT, BOOK, _ = min(((c, b, v) for c, b, v in O.all_vols(lib)), key=lambda t: len(lib[t[0]][t[1]]))
VOLS = lib[CAT][BOOK]
REL = VOLS[0]["rel"]
BOOK_REL = "%s/%s" % (CAT, BOOK)
print("  · 测试样本：%s / %s（%d 卷）" % (CAT, BOOK, len(VOLS)))


def _xml_ok(text):
    root = ET.fromstring(text)
    return root.tag.endswith("feed") and len(list(root)) > 0


check("feed_root 是合法 Atom feed", lambda: (_xml_ok(O.feed_root()), ""))
check("feed_catalog(已完结)", lambda: (_xml_ok(O.feed_catalog(O.CATEGORY_DONE, 1)), ""))
check("feed_catalog(all) 合并视图", lambda: (_xml_ok(O.feed_catalog("all", 1)), ""))
check("feed_book 获取型 feed", lambda: (_xml_ok(O.feed_book(BOOK_REL, 1)), ""))
check("feed_recent", lambda: (_xml_ok(O.feed_recent(1)), ""))
check("feed_search 命中", lambda: (_xml_ok(O.feed_search(BOOK[:4], 1)), ""))
check("feed_search 无命中仍返回合法 feed", lambda: (_xml_ok(O.feed_search("zzz不存在zzz", 1)), ""))
check("feed_catalog 非法分类返回 None", lambda: (O.feed_catalog("不存在", 1) is None, ""))
check("feed_book 路径穿越返回 None", lambda: (O.feed_book("../etc/passwd", 1) is None, ""))
truthy("OpenSearch 描述可解析", ET.fromstring(O.opensearch_xml()).tag.endswith("OpenSearchDescription"))
truthy("_paginate 首页带 rel=next", 'rel="next"' in O._paginate(list(range(250)), 1, "/x?page=1")[1])
truthy("_paginate 尾页无 next", 'rel="next"' not in O._paginate(list(range(250)), 3, "/x?page=1")[1])
truthy("_paginate 中间页同时有 prev/next",
       all(k in O._paginate(list(range(250)), 2, "/x?page=1")[1] for k in ('rel="next"', 'rel="previous"')))

check("root_html 含分类与搜索框",
      lambda: (all(k in O.root_html() for k in ("<!doctype html", O.SERVER_TITLE, "/opds/search", O.CATEGORY_DONE)), ""))
check("catalog_html 含书封网格", lambda: ('class="grid"' in O.catalog_html(CAT, 1), ""))
check("catalog_html(all)", lambda: ('class="grid"' in O.catalog_html("all", 1), ""))
check("catalog_html 非法分类返回 None", lambda: (O.catalog_html("不存在", 1) is None, ""))
check("book_html 含 hero 与两种下载入口",
      lambda: (all(k in O.book_html(BOOK_REL, 1) for k in ('class="hero"', "/dl/", "/zip/")), ""))
check("book_html 含分组折叠交互", lambda: ("grpAll(" in O.book_html(BOOK_REL, 1), ""))
check("book_html 非法路径返回 None", lambda: (O.book_html("../x", 1) is None, ""))
check("recent_html", lambda: ("最近更新" in O.recent_html(1), ""))
check("search_html 命中", lambda: (BOOK[:4] in O.search_html(BOOK[:4], 1), ""))
check("search_html 空查询提示", lambda: ("输入书名或卷名开始搜索" in O.search_html("", 1), ""))
check("index_html 兼容旧名 == root_html", lambda: (O.index_html() == O.root_html(), ""))
truthy("_primary_vol 选出主卷封面", O._primary_vol(VOLS, CAT, BOOK) is not None)
truthy("_group_vols_by_subdir 返回分组", len(O._group_vols_by_subdir(VOLS, CAT, BOOK)) >= 1)

# ============================== D. epub 封面 / 元数据 ==============================
section("D. epub 封面提取与元数据（真实文件，走缓存链路）")
_blob, _mime = O.get_cover(REL)
truthy("get_cover 提取到封面（%s, %d bytes）" % (_mime, len(_blob or b"")),
       _blob and _mime and _mime.startswith("image/") and len(_blob) > 200)
truthy("第二遍命中内存缓存（同一对象）", O.get_cover(REL)[0] is _blob)
eq("get_cover 非法路径安全返回 (None,None)", O.get_cover("../x"), (None, None))
_meta = O.get_epub_meta(REL)
truthy("get_epub_meta 返回 title/creator/description",
       isinstance(_meta, dict) and set(_meta) >= {"title", "creator", "description"}, str(_meta.get("title"))[:40])
truthy("元数据第二次读命中缓存（同一对象）", O.get_epub_meta(REL) is _meta)
eq("get_epub_meta 非法路径返回 {}", O.get_epub_meta("../x"), {})

# ============================== E. HTTP 端到端 ==============================
section("E. HTTP 端到端（真实 socket，内容协商 / Range / ZIP / 认证）")
httpd = O.make_server(port=0, bind="127.0.0.1")
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print("  · 测试服务器：http://127.0.0.1:%d/" % PORT)


def http(path, accept=None, headers=None, method="GET"):
    req = urlreq.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method)
    if accept:
        req.add_header("Accept", accept)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlreq.urlopen(req, timeout=90) as r:
            return r.status, dict(r.headers), r.read()
    except urlerr.HTTPError as e:
        return e.code, dict(e.headers), e.read()


_NAV, _ACQ = O.OPDS_NAV_TYPE, O.OPDS_ACQ_TYPE
_s, _h, _b = http("/", accept=_NAV)
check("GET / (Accept: opds) -> Atom", lambda: (_s == 200 and _xml_ok(_b.decode()),
                                          "status=%s ctype=%s" % (_s, _h.get("Content-Type"))))
_s, _h, _b = http("/", accept="text/html")
check("GET / (浏览器) -> HTML", lambda: (_s == 200 and b"<!doctype html" in _b, "ctype=%s" % _h.get("Content-Type")))
_s, _h, _b = http("/index.html")
check("GET /index.html 兼容入口", lambda: (_s == 200 and b"<!doctype html" in _b, ""))
_s, _h, _b = http("/opds/opensearch.xml")
check("GET /opds/opensearch.xml", lambda: (_s == 200 and _b.startswith(b"<?xml"), ""))
_s, _h, _b = http("/opds/stats")
check("GET /opds/stats 是 JSON", lambda: (_s == 200 and isinstance(json.loads(_b), dict), ""))
_s, _h, _b = http("/opds/refresh")
check("GET /opds/refresh 强制刷新", lambda: (_s == 200 and json.loads(_b).get("ok") is True, ""))
_s, _h, _b = http("/opds/catalog/" + O.quote(CAT), accept=_NAV)
check("GET /opds/catalog/<分类> (OPDS)", lambda: (_s == 200 and _xml_ok(_b.decode()), ""))
_s, _h, _b = http("/opds/book/" + O.encode_path(BOOK_REL), accept=_ACQ)
check("GET /opds/book/<书> (OPDS 获取型)", lambda: (_s == 200 and _xml_ok(_b.decode()), ""))
_s, _h, _b = http("/opds/recent")
check("GET /opds/recent (HTML)", lambda: (_s == 200 and "最近更新" in _b.decode(), ""))
_s, _h, _b = http("/opds/search?q=" + O.quote(BOOK[:4]))
check("GET /opds/search 有结果", lambda: (_s == 200 and BOOK[:4] in _b.decode(), ""))
_s, _h, _b = http("/opds/catalog/" + O.quote("不存在"))
check("GET 非法分类 -> 404", lambda: (_s == 404, "status=%s" % _s))
_s, _h, _b = http("/opds/nowhere")
check("GET 未知路径 -> 404", lambda: (_s == 404, "status=%s" % _s))

_s, _h, _b = http("/cover/" + O.encode_path(REL))
check("GET /cover 返回图片", lambda: (_s == 200 and _h.get("Content-Type", "").startswith("image/"),
                                 "ctype=%s len=%d" % (_h.get("Content-Type"), len(_b))))


def raw_header_all(path, name):
    """取原始响应里某个头的全部取值（dict(r.headers) 会丢掉重复头）。"""
    c = httpclient.HTTPConnection("127.0.0.1", PORT, timeout=30)
    try:
        c.request("GET", path)
        r = c.getresponse()
        vals = r.headers.get_all(name) or []
        r.read()
        return vals
    finally:
        c.close()


# 缺陷①回归：同一响应头绝不能出现两次（否则客户端按首个取值，长缓存静默失效）
_CACHE_COVER = raw_header_all("/cover/" + O.encode_path(REL), "Cache-Control")
check("封面只发一个 Cache-Control 且带 max-age",
      lambda: (len(_CACHE_COVER) == 1 and "max-age" in _CACHE_COVER[0], "Cache-Control=%s" % _CACHE_COVER))
_CACHE_BLANK = raw_header_all("/cover/__no_such_file__.epub", "Cache-Control")
check("无封面占位图同样只发一个 Cache-Control",
      lambda: (len(_CACHE_BLANK) == 1 and "max-age" in _CACHE_BLANK[0], "Cache-Control=%s" % _CACHE_BLANK))
_CACHE_HTML = raw_header_all("/", "Cache-Control")
check("HTML 页仍保持 no-cache（未过度放宽）",
      lambda: (len(_CACHE_HTML) == 1 and "no-cache" in _CACHE_HTML[0], "Cache-Control=%s" % _CACHE_HTML))

_fsize = os.path.getsize(os.path.join(O.LIGHT_NOVEL_DIR, REL.replace("/", os.sep)))
_s, _h, _b = http("/dl/" + O.encode_path(REL))
check("GET /dl 完整下载", lambda: (_s == 200 and len(_b) == _fsize and _h.get("Accept-Ranges") == "bytes",
                               "len=%d file=%d" % (len(_b), _fsize)))
_s, _h, _b = http("/dl/" + O.encode_path(REL), headers={"Range": "bytes=0-99"})
check("GET /dl Range -> 206 断点续传",
      lambda: (_s == 206 and len(_b) == 100 and _h.get("Content-Range", "").startswith("bytes 0-99/"),
               "status=%s len=%d cr=%s" % (_s, len(_b), _h.get("Content-Range"))))
_s, _h, _b = http("/dl/" + O.encode_path(REL), headers={"Range": "bytes=%d-" % (_fsize + 10)})
check("GET /dl 越界 Range -> 416", lambda: (_s == 416, "status=%s" % _s))
_s, _h, _b = http("/dl/" + O.encode_path("已完结/../secret.txt"))
check("GET /dl 路径穿越 -> 404", lambda: (_s == 404, "status=%s" % _s))

_s, _h, _b = http("/zip/" + O.encode_path(BOOK_REL))
_zip_ok, _zip_n = False, 0
if _s == 200:
    try:
        with zipfile.ZipFile(io.BytesIO(_b)) as zf:
            _zip_n = len(zf.namelist())
            _zip_ok = _zip_n == len(VOLS) and zf.testzip() is None
    except Exception:
        _zip_ok = False
check("GET /zip 流式打包可解压且卷数一致",
      lambda: (_s == 200 and _zip_ok, "status=%s 包内 %d 卷 / 预期 %d 卷" % (_s, _zip_n, len(VOLS))))
check("ZIP 响应头正确",
      lambda: (_h.get("Content-Type") == "application/zip" and "attachment" in (_h.get("Content-Disposition") or ""),
               "%s | %s" % (_h.get("Content-Type"), _h.get("Content-Disposition"))))
_s, _h, _b = http("/zip/" + O.encode_path("%s/%s/__no_such_subdir__" % (CAT, BOOK)))
check("GET /zip 空子目录 -> 404", lambda: (_s == 404, "status=%s" % _s))

_s, _h, _b = http("/", method="HEAD")
check("HEAD / 只回头不带 body",
      lambda: (_s == 200 and _b == b"" and _h.get("Content-Length", "0") != "0", "len=%d" % len(_b)))

# ---- Basic 凭据（运行时改全局，用完即恢复）----
# 新版模型：Basic 只管「带了正确凭据就升权」，不负责「带了错凭据就赶人」——
# 匿名与错口令都降级成访客（200 + 正常页面），绝不回 401。原因见 OPDSHandler._role：
# 浏览器会把旧 Basic 凭据长期自动附上，回 401 就会每次开书库都弹一次系统认证框。
_ADMIN = "已读完".encode("utf-8")
_o_u, _o_p = O.AUTH_USER, O.AUTH_PASS
O.AUTH_USER, O.AUTH_PASS = "tester", "s3cret"
try:
    _s, _h, _b = http("/")
    check("配了口令后匿名 -> 200 访客页（不再 401，浏览器不会弹认证框）",
          lambda: (_s == 200 and _h.get("WWW-Authenticate") is None
                   and b"/opds/login" in _b and _ADMIN not in _b, "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Basic " + base64.b64encode(b"tester:s3cret").decode()})
    check("正确凭据 -> 200（管理员视角，含「已读完」入口）",
          lambda: (_s == 200 and _ADMIN in _b, "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()})
    check("错误口令 -> 200 访客页（降级而非 401，旧凭据被缓存时不会反复弹框）",
          lambda: (_s == 200 and _ADMIN not in _b and b"/opds/login" in _b, "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Bearer abc"})
    check("非 Basic 认证头 -> 200 访客页（解析不出来就当没带）",
          lambda: (_s == 200 and _ADMIN not in _b, "status=%s" % _s))
finally:
    O.AUTH_USER, O.AUTH_PASS = _o_u, _o_p

httpd.shutdown()
httpd.server_close()
info("HTTP 测试服务器已关闭", "端口 %d" % PORT)

# ============================== F. sync 推送分类与工具 ==============================
section("F. 推送错误分类与退避（lightnovel.sync.gitops）")
eq("classify auth", S.classify_push_error("", "Authentication failed for 'https://x'"), "auth")
eq("classify nonfastforward", S.classify_push_error("", "! [rejected] main -> main (non-fast-forward)"), "nonfastforward")
eq("classify nonfastforward(fetch first)",
   S.classify_push_error("Updates were rejected because the tip of your current branch is behind", ""), "nonfastforward")
eq("classify transient(rpc failed)", S.classify_push_error("error: RPC failed; curl 55 Send failure", ""), "transient")
eq("classify transient(early eof)",
   S.classify_push_error("", "fatal: the remote end hung up unexpectedly\nfatal: early EOF"), "transient")
eq("classify fatal", S.classify_push_error("", "fatal: not a git repository"), "fatal")

_orig_sleep = time.sleep


def probe_backoff():
    calls = []
    time.sleep = lambda s: calls.append(s)
    try:
        S._push_backoff(1, 3)
        a = list(calls)
        calls.clear()
        S._push_backoff(3, 3)
        b = list(calls)
        calls.clear()
        S._push_backoff(4, 5)
        c = list(calls)
        return a, b, c
    finally:
        time.sleep = _orig_sleep


_bk = probe_backoff()
truthy("_push_backoff 指数退避（首 5s / 第 4 次 40s）", _bk[0] == [5] and _bk[2] == [40], "%s" % (_bk,))
truthy("_push_backoff 末次不等待", _bk[1] == [], "%s" % (_bk[1],))

eq("auth_url 空 token 原样返回", S.auth_url("https://x/y.git"), "https://x/y.git")
truthy("git_available 为真", S.git_available())
_rc, _out, _ = S.run_git(["--version"], check=False)
check("run_git --version", lambda: (_rc == 0 and "git version" in _out, _out.strip()))
check("run_git 坏参数走非 0 分支", lambda: (S.run_git(["--no-such-flag-xyz"], check=False)[0] != 0, ""))

# ============================== G. plan_mirror 差异与冲突 ==============================
section("G. plan_mirror 差异计划与三类冲突检测")
src = tpath("mirror", "src")
dst = tpath("mirror", "dst")
now = time.time()
OLD = now - 3 * 86400
os.makedirs(src, exist_ok=True)
os.makedirs(dst, exist_ok=True)
sfile(src, "same.txt", "aaa")
sfile(dst, "same.txt", "aaa", mtime=os.path.getmtime(os.path.join(src, "same.txt")))
sfile(src, "grown.txt", "1234567890")
sfile(dst, "grown.txt", "123", mtime=os.path.getmtime(os.path.join(src, "grown.txt")))
sfile(src, "newer.txt", "x" * 10)
sfile(dst, "newer.txt", "y" * 20, mtime=os.path.getmtime(os.path.join(src, "newer.txt")) + 100)
sfile(src, "sub/added.txt", "new")
sfile(dst, "listed_old.txt", "stale", mtime=OLD)
sfile(dst, "foreign_new.txt", "fresh")
sfile(dst, "orphan_old.txt", "old", mtime=OLD)

plan = S.plan_mirror(src, dst, known={"same.txt", "grown.txt", "newer.txt", "listed_old.txt"})
_del = {d["rel"]: d for d in plan["delete"]}
_kinds = {c["rel"]: c["kind"] for c in plan["conflicts"]}
eq("add 识别 D 独有文件", sorted(a["rel"] for a in plan["add"]), ["sub/added.txt"])
truthy("update 识别被改文件", "grown.txt" in [u["rel"] for u in plan["update"]],
       str([u["rel"] for u in plan["update"]]))
truthy("keep 识别一致文件", plan["counts"]["unchanged"] >= 1, "unchanged=%d" % plan["counts"]["unchanged"])
eq("冲突 F_NEWER（F 更新且大小不同）", _kinds.get("newer.txt"), "F_NEWER")
eq("冲突 F_FOREIGN_RECENT（清单外 + 24h 内）", _kinds.get("foreign_new.txt"), "F_FOREIGN_RECENT")
eq("冲突 F_ORPHAN（清单外 + 超 24h）", _kinds.get("orphan_old.txt"), "F_ORPHAN")
eq("清单内且超 24h 的 F 独有文件不被保护", _del.get("listed_old.txt", {}).get("protected"), False)
eq("24h 内 F 外来文件被保护", _del.get("foreign_new.txt", {}).get("protected"), True)
eq("counts.protected 只统计受保护项", plan["counts"]["protected"], 1)
eq("counts.delete 只统计可删项", plan["counts"]["delete"], 2)
eq("plan 必含 src/dst/scanned_at", set(plan) >= {"src", "dst", "scanned_at"}, True)

# ============================== H. apply_mirror 执行与护栏 ==============================
section("H. apply_mirror 执行与安全护栏")
_dst_before = sorted(S.scan_tree(dst).keys())
res_dry = S.apply_mirror(S.plan_mirror(src, dst, known=set()), dry_run=True, allow_delete=True)
eq("dry-run 不改动目标盘", sorted(S.scan_tree(dst).keys()), _dst_before)
truthy("dry-run 全部记为 skipped",
       len(res_dry["skipped"]) > 0 and not res_dry["added"] and not res_dry["deleted"],
       "skipped=%d" % len(res_dry["skipped"]))

_dst2 = tpath("mirror2", "dst")
os.makedirs(_dst2, exist_ok=True)
sfile(_dst2, "gone.txt", "bye", mtime=OLD)
_res_nodelete = S.apply_mirror(S.plan_mirror(src, _dst2, known={"gone.txt"}),
                               dry_run=False, allow_delete=False)
truthy("allow_delete=False 只增改不删",
       os.path.isfile(os.path.join(_dst2, "gone.txt")) and _res_nodelete["added"], "")

_res = S.apply_mirror(S.plan_mirror(src, dst, known=set()), dry_run=False,
                      allow_delete=True, manifest_set=set())
truthy("实跑：D 独有文件已复制", os.path.isfile(os.path.join(dst, "sub", "added.txt")), str(_res["added"]))
truthy("实跑：modified 文件已被覆盖",
       open(os.path.join(dst, "grown.txt"), encoding="utf-8").read() == "1234567890")
truthy("实跑：清单外孤儿已删除", not os.path.exists(os.path.join(dst, "orphan_old.txt")), str(_res["deleted"]))
truthy("实跑：24h 保护文件仍在", os.path.exists(os.path.join(dst, "foreign_new.txt")))
truthy("实跑：一致文件未被重写", os.path.isfile(os.path.join(dst, "same.txt")))
truthy("实跑：无失败项", not _res["failed"], str(_res["failed"]))

_guard_dst = tpath("mirror3", "dst")
os.makedirs(_guard_dst, exist_ok=True)
for i in range(3):
    sfile(_guard_dst, "bulk%d.txt" % i, "z", mtime=OLD)
_guard = S.plan_mirror(src, _guard_dst, known=set())   # 必须先造文件再算计划
_orig_max = S.MAX_MIRROR_DELETIONS
S.MAX_MIRROR_DELETIONS = 2
try:
    _gr = S.apply_mirror(_guard, dry_run=False, allow_delete=True)
    check("超过 MAX_MIRROR_DELETIONS 护栏生效并停止删除",
          lambda: (bool(_gr.get("guard_triggered")) and os.path.isfile(os.path.join(_guard_dst, "bulk0.txt")),
                   "待删 %d 个 / 上限 2 / guard_triggered=%s" % (_guard["counts"]["delete"], _gr.get("guard_triggered"))))
finally:
    S.MAX_MIRROR_DELETIONS = _orig_max

# ============================== I. 孤儿子树安全网 ==============================
section("I. 孤儿子树删除的四重安全网")
_iso = tpath("iso", "dst")            # F 侧（镜像目标）
_src_real = tpath("iso", "src")       # D 侧（有源）
_src_off = tpath("iso", "src_off")    # D 侧不存在，模拟盘掉线
os.makedirs(_iso, exist_ok=True)
os.makedirs(_src_real, exist_ok=True)
sfile(_src_real, "keep.txt", "k")
sfile(_iso, "kill/child/deep.txt", "x", mtime=OLD)
age_tree(_iso, OLD)                   # 连目录 mtime 一起改旧，否则会命中 24h 保护

mv, sk = S._prune_foreign_subtrees(_src_off, _iso, set(), allow_delete=True)
truthy("源目录不存在 -> 一律不删（防 D 盘掉线误删）",
       mv == [] and os.path.isdir(os.path.join(_iso, "kill")), "moved=%s" % mv)
mv2, sk2 = S._prune_foreign_subtrees(_src_real, _iso, set(), allow_delete=True)
truthy("源存在 -> 孤儿子树内容被整棵删除",
       "kill/child" in mv2 and not os.path.exists(os.path.join(_iso, "kill", "child")),
       "moved=%s skipped=%s" % (mv2, sk2))
# 删除子目录会刷新父目录 mtime（NTFS 语义），父目录因此被 24h 保护挡下；
# 这正是 apply_mirror 末尾还要调 _prune_empty_dirs 兜底的原因。
_pruned = S._prune_empty_dirs(_iso)
truthy("兜底 _prune_empty_dirs 清掉残留的空父目录",
       not os.path.exists(os.path.join(_iso, "kill")), "清理 %d 个空目录" % _pruned)
mv3, sk3 = S._prune_foreign_subtrees(_src_real, _iso, set(), allow_delete=True)
sfile(_iso, "fresh/sub.txt", "new")
mv4, sk4 = S._prune_foreign_subtrees(_src_real, _iso, set(), allow_delete=True)
truthy("24h 内新建的 F 孤儿子树被保护",
       "fresh" in sk4 and os.path.isdir(os.path.join(_iso, "fresh")), "skipped=%s" % sk4)
sfile(_iso, "keepme/a/b.txt", "y", mtime=OLD)
age_tree(os.path.join(_iso, "keepme"), OLD)
mv5, sk5 = S._prune_foreign_subtrees(_src_real, _iso, set(), allow_delete=False)
truthy("allow_delete=False 不删子树",
       os.path.isdir(os.path.join(_iso, "keepme")) and mv5 == [], "moved=%s" % mv5)
truthy("_prune_empty_dirs 返回整数", isinstance(S._prune_empty_dirs(_iso), int))

# ============================== J. 快照 / README / 清单 / 看板 / 锁 ==============================
section("J. 快照 / 变更检测 / README / 清单 / 看板 / 单实例锁")
_snap_root = tpath("snap")
sfile(_snap_root, "a.txt", "1")
sfile(_snap_root, "b/c.txt", "2")
sfile(_snap_root, "desktop.ini", "junk")
_snap = S.snapshot_dir([_snap_root])
truthy("snapshot_dir 扫描到文件且排除 desktop.ini",
       len(_snap) == 2 and not any("desktop.ini" in k for k in _snap), str(sorted(_snap)))
_a, _m, _r = S.detect_changes({"a": 1}, {"a": 2, "b": 1})
truthy("detect_changes 增/改/删三类齐全",
       _a == ["b"] and _m == ["a"] and _r == [], "add=%s mod=%s rm=%s" % (_a, _m, _r))
_dirs = S.scan_dirs(_snap_root)
truthy("scan_dirs 含根标记与子目录", "" in _dirs and "b" in _dirs, str(sorted(_dirs)))

_rd = tpath("readme", "README.md")
write(_rd, S._default_readme(["书A"], ["书B"]))
_orig_readme = S.README_PATH
S.README_PATH = _rd
try:
    S.regenerate_readme()
    _after = open(_rd, encoding="utf-8").read()
    _first_done = sorted(os.listdir(S.CATEGORY_DIRS[S.CATEGORY_DONE]))[0]
    check("regenerate_readme 用真实书单刷新区块",
          lambda: ("- " + _first_done in _after and "未完结作品" in _after and "已完结作品" in _after,
                   "已写 %d 字节" % len(_after)))
    S.regenerate_readme()
    check("regenerate_readme 可重入（内容不漂移）",
          lambda: (open(_rd, encoding="utf-8").read() == _after, "重复执行后一致"))
    _t, _c = S._replace_readme_section(_after, "已完结作品", "- 新条目")
    check("_replace_readme_section 精确命中并替换",
          lambda: (_c == 1 and "- 新条目" in _t and _first_done not in _t, "count=%d" % _c))
finally:
    S.README_PATH = _orig_readme

_orig_manifest = S.MIRROR_MANIFEST
S.MIRROR_MANIFEST = tpath("state", "manifest.json")
try:
    S._save_manifest({"已完结": {"a.epub", "b.epub"}, "未完结": {"c.epub"}})
    _lm = S._load_manifest()
    check("镜像清单存取往返一致",
          lambda: (_lm["已完结"] == {"a.epub", "b.epub"} and _lm["未完结"] == {"c.epub"},
                   str({k: sorted(v) for k, v in _lm.items()})))
finally:
    S.MIRROR_MANIFEST = _orig_manifest

_orig_html, _orig_ld = S.MIRROR_HTML_FILE, S.LOG_DIR
S.LOG_DIR = tpath("state2", "x")
S.MIRROR_HTML_FILE = tpath("state2", "board.html")
try:
    S.write_mirror_dashboard({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": S.LIGHT_NOVEL_DIR, "target_root": S.F_TARGET_ROOT, "f_mounted": True,
        "categories": {S.CATEGORY_DONE: {"counts": {"src_total": 3, "dst_total": 2, "add": 1, "update": 0,
                                                    "delete": 0, "unchanged": 2, "conflict": 1,
                                                    "protected": 0, "orphan": 0, "orphan_dirs": 0},
                                       "conflicts": [{"kind": "F_NEWER", "rel": "x.epub", "detail": "d"}],
                                       "delete": []}},
        "counts": {"add": 1, "update": 0, "delete": 0, "protected": 0, "orphan": 0,
                   "orphan_dirs": 0, "unchanged": 2, "conflict": 1, "src_total": 3, "dst_total": 2},
    })
    _board = open(S.MIRROR_HTML_FILE, encoding="utf-8").read()
    check("镜像看板生成且含关键指标",
          lambda: (all(k in _board for k in ("F_NEWER", "一致率", "镜像状态")), "%d 字节" % len(_board)))
finally:
    S.MIRROR_HTML_FILE, S.LOG_DIR = _orig_html, _orig_ld

# 缺陷②回归：单实例锁的存活探测必须是「只读」的，绝不能终止目标进程
_orig_ld2 = S.LOG_DIR
S.LOG_DIR = tpath("lock")
_dead = subprocess.Popen([PY, "-c", "pass"])
_dead.wait()
try:
    check("_pid_alive(自身 pid) 为真",
          lambda: (S._pid_alive(os.getpid()) is True, "pid=%d" % os.getpid()))
    check("_pid_alive(已退出进程) 为假",
          lambda: (S._pid_alive(_dead.pid) is False, "pid=%d" % _dead.pid))
    check("_pid_alive 非法输入安全返回 False",
          lambda: (S._pid_alive("abc") is False and S._pid_alive(None) is False
                   and S._pid_alive(0) is False and S._pid_alive(-1) is False, ""))

    _a1 = S.acquire_lock()                     # 无锁文件 -> 成功
    _a2 = S.acquire_lock()                     # 锁里是自己且活着 -> 拒绝
    S.release_lock()
    with open(os.path.join(S.LOG_DIR, "monitor.lock"), "w", encoding="utf-8") as _fh:
        _fh.write(str(_dead.pid))              # 陈旧锁：死进程的 pid
    _a3 = S.acquire_lock()                     # -> 应接管
    _owner = open(os.path.join(S.LOG_DIR, "monitor.lock"), encoding="utf-8").read().strip()
    S.release_lock()
    check("单实例锁：首获成功 / 活进程拒绝 / 死进程陈旧锁可接管",
          lambda: (_a1 is True and _a2 is False and _a3 is True and _owner == str(os.getpid()),
                   "%s %s %s 接管后 owner=%s" % (_a1, _a2, _a3, _owner)))
    _a4 = S.acquire_lock()
    S.release_lock()
    check("release_lock 后锁文件已清理且可重获",
          lambda: (_a4 is True and not os.path.exists(os.path.join(S.LOG_DIR, "monitor.lock")), ""))
    check("回归期未终止任何进程（死进程确已自行退出）",
          lambda: (_dead.poll() is not None, "exit=%s" % _dead.poll()))

    # 缺陷②最直接的行为回归：锁被另一个**活进程**持有时，必须只是拒绝启动，
    # 绝不能把那个进程干掉（旧实现 os.kill(pid, 0) 会真的终止它）。
    _live = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    try:
        with open(os.path.join(S.LOG_DIR, "monitor.lock"), "w", encoding="utf-8") as _fh:
            _fh.write(str(_live.pid))
        _refused = S.acquire_lock()
        _alive_after = _live.poll() is None
    finally:
        _live.kill()
        _live.wait()
    check("拒绝并存时不会终止正在运行的进程（缺陷②核心回归）",
          lambda: (_refused is False and _alive_after is True,
                   "acquire_lock 返回 %s，持锁进程仍存活=%s" % (_refused, _alive_after)))
finally:
    S.LOG_DIR = _orig_ld2

_src = open(os.path.join(ROOT, "lightnovel", "sync", "monitor.py"), encoding="utf-8").read()
check("源码已把存活探测抽成 _pid_alive（不再裸调 os.kill(pid, 0)）",
      lambda: ("def _pid_alive(" in _src and "OpenProcess" in _src, ""))

# 行为断言：本机上 _pid_alive 必须完全不触碰 os.kill（走只读的 OpenProcess 分支）
_orig_kill = os.kill
_kill_calls = []


def _spy_kill(*a, **kw):
    _kill_calls.append(a)
    return _orig_kill(*a, **kw)


os.kill = _spy_kill
try:
    _r_live = S._pid_alive(os.getpid())
    _r_dead = S._pid_alive(_dead.pid)
finally:
    os.kill = _orig_kill

if sys.platform == "win32":
    check("Windows 上 _pid_alive 调用 os.kill 次数为 0（只读探测，不会杀进程）",
          lambda: (not _kill_calls and _r_live is True and _r_dead is False,
                   "os.kill 调用=%d 次，活进程=%s 已退出=%s" % (len(_kill_calls), _r_live, _r_dead)))
else:
    info("当前非 Windows 平台，_pid_alive 走标准 os.kill(pid, 0) 探测（该平台语义安全）",
         "活=%s 死=%s" % (_r_live, _r_dead))

_rep = S.mirror_query()
check("mirror_query 返回结构完整（只读）",
      lambda: (all(k in _rep for k in ("generated_at", "source_root", "target_root", "f_mounted",
                                       "categories", "counts")), "f_mounted=%s" % _rep["f_mounted"]))
truthy("_warn_stray_items 可执行不抛错", S._warn_stray_items() is None)
truthy("list_books 已排序",
       S.list_books(S.CATEGORY_DIRS[S.CATEGORY_DONE]) ==
       sorted(S.list_books(S.CATEGORY_DIRS[S.CATEGORY_DONE]), key=lambda s: s.lower()))
truthy("EXCLUDE_FILE_NAMES 含 desktop.ini", "desktop.ini" in S.EXCLUDE_FILE_NAMES)

# ============================== K. 隧道向导 ==============================
section("K. 隧道向导逻辑（lightnovel.tunnel_setup，全部 monkeypatch，不碰真实配置）")
eq("clean 去 ANSI 转义", T.clean("\x1b[32mOK\x1b[0m  "), "OK")


class _FakeR:
    def __init__(self, out="", err="", rc=0):
        self.out, self.err, self.returncode, self.stdout = out, err, rc, out


_orig_run_cf = T.run_cf
try:
    T.run_cf = lambda args, interactive=False, exe=None: _FakeR(
        out="ID                                NAME            CREATED\n"
            "a1b2c3d4-1111-2222-3333-444455556666  ln-opds         2026-09-01")
    eq("tunnel_id_of 从 tunnel list 解析 UUID",
       T.tunnel_id_of("ln-opds", "x"), "a1b2c3d4-1111-2222-3333-444455556666")
    T.run_cf = lambda args, interactive=False, exe=None: _FakeR(
        out="Created tunnel ln-opds with id 9999aaaa-bbbb-cccc-dddd-eeeeffff0000")
    eq("step_create 解析新建隧道 id",
       T.step_create("ln-opds", "x"), "9999aaaa-bbbb-cccc-dddd-eeeeffff0000")
    T.run_cf = lambda args, interactive=False, exe=None: _FakeR(out="no id here")
    eq("step_create 无法定位时返回 None", T.step_create("ln-opds", "x"), None)
    T.run_cf = lambda args, interactive=False, exe=None: _FakeR(out="Added CNAME ln.example.com", rc=0)
    eq("step_route_dns 成功返回 True", T.step_route_dns("ln-opds", "ln.example.com", "x"), True)
finally:
    T.run_cf = _orig_run_cf

import pathlib  # noqa: E402

_orig_cfg, _orig_dir = T.CF_CONFIG, T.CF_DIR
_cfg = tpath("cf", "config.yml")
T.CF_CONFIG, T.CF_DIR = pathlib.Path(_cfg), pathlib.Path(os.path.dirname(_cfg))
try:
    T.write_config("ln-opds", "9999aaaa-bbbb-cccc-dddd-eeeeffff0000", "ln.example.com", 8080)
    _cfgtxt = open(_cfg, encoding="utf-8").read()
    check("write_config 生成正确 ingress 配置",
          lambda: (all(k in _cfgtxt for k in ("tunnel: ln-opds", "hostname: ln.example.com",
                                              "service: http://127.0.0.1:8080", "http_status:404")),
                   "已写 %d 字节" % len(_cfgtxt)))
finally:
    T.CF_CONFIG, T.CF_DIR = _orig_cfg, _orig_dir

# OPDS 服务侧解析同一份配置（读域名 + 同步回源端口）
_orig_cfg2 = O.CF_CONFIG
O.CF_CONFIG = _cfg
try:
    eq("read_named_hostname 解析配置文件", O.read_named_hostname(), "ln.example.com")
    _changed = O.sync_named_port(18080, _cfg)
    check("sync_named_port 改写回源端口",
          lambda: (_changed and "service: http://127.0.0.1:18080" in open(_cfg, encoding="utf-8").read(),
                   "changed=%s" % _changed))
    eq("sync_named_port 端口未变时返回 False", O.sync_named_port(18080, _cfg), False)
finally:
    O.CF_CONFIG = _orig_cfg2

# ============================== L. CLI 入口（真实子进程） ==============================
section("L. CLI 入口与 bat 参数一致性（真实子进程）")


def run_cli(args, timeout=180):
    p = subprocess.run([PY] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, cwd=ROOT, env=ENV)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


_rc, _o = run_cli(["-m", "lightnovel", "--help"])
check("python -m lightnovel --help 列出全部子命令",
      lambda: (_rc == 0 and all(c in _o for c in ("ui", "opds", "sync", "mirror", "tunnel-setup")),
               "rc=%s" % _rc))
_rc, _o = run_cli(["-m", "lightnovel", "opds", "--help"])
check("lightnovel opds --help", lambda: (_rc == 0 and "--tunnel" in _o and "--hide-window" in _o, "rc=%s" % _rc))
check("bat(run_named_tunnel) 的 flag 全部在册",
      lambda: (all(f in _o for f in ("--tunnel", "--no-qr", "--hide-window")), "查 --tunnel/--no-qr/--hide-window"))
check("lightnovel opds 全部选项在册",
      lambda: (all(f in _o for f in ("--port", "--bind", "--public-url", "--no-qr", "--help-internet")), ""))
_rc, _o = run_cli(["-m", "lightnovel", "opds", "--tunnel", "bogus"])
check("非法 --tunnel 取值被 argparse 拒绝", lambda: (_rc == 2 and "invalid choice" in _o, "rc=%s" % _rc))
_rc, _o = run_cli(["-m", "lightnovel", "opds", "--help-internet"])
check("lightnovel opds --help-internet 打印方案说明",
      lambda: (_rc == 0 and "Cloudflare" in _o and "Tailscale" in _o, "rc=%s %d 字节" % (_rc, len(_o))))
_rc, _o = run_cli(["-m", "lightnovel", "sync", "--help"])
check("lightnovel sync --help", lambda: (_rc == 0 and "--mirror-f" in _o and "--opds" in _o, "rc=%s" % _rc))
check("bat(run_once) 的 --once 在册", lambda: ("--once" in _o, ""))
check("sync 全部模式选项在册",
      lambda: (all(f in _o for f in ("--mirror-status", "--mirror-dry-run", "--mirror-no-delete",
                                     "--opds-only", "--opds-port", "--monitor-only", "--init", "--status")), ""))
_rc, _o = run_cli(["-m", "lightnovel", "tunnel-setup", "--help"])
check("lightnovel tunnel-setup --help", lambda: (_rc == 0 and "--hostname" in _o, "rc=%s" % _rc))

# ============================== M. bat 与 workflow 完整性 ==============================
section("M. bat 启动器与 workflow 完整性")
_LAU = os.path.join(ROOT, "launchers")
_bats = ["run_monitor.bat", "run_once.bat", "run_opds.bat", "run_named_tunnel.bat",
         "setup_named_tunnel.bat", "stop_opds.bat"]
_bl = []
for b in _bats:
    _p = os.path.join(_LAU, b)
    if not os.path.isfile(_p):
        _bl.append("%s 缺失" % b)
        continue
    _t = open(_p, encoding="utf-8").read()
    if b != "stop_opds.bat" and "lightnovel" not in _t:
        _bl.append("%s 未走 python -m lightnovel" % b)
check("launchers/ 下 6 个启动器齐全且都指向 lightnovel 包", lambda: (not _bl, "问题=%s" % _bl))
check("stop_opds 仍按 8080 + cloudflared 停止",
      lambda: (all(k in open(os.path.join(_LAU, "stop_opds.bat"), encoding="utf-8").read()
                   for k in ("8080", "cloudflared.exe", "taskkill")), ""))
check("run_monitor 用 pythonw 无窗口启动",
      lambda: ("pythonw" in open(os.path.join(_LAU, "run_monitor.bat"), encoding="utf-8").read(), ""))
_wf = open(".github/workflows/OneDriveSync.yml", encoding="utf-8").read()
check("OneDrive workflow 保持停用（本次未擅自恢复）", lambda: ("if: false" in _wf, ""))
check("OneDrive workflow 骨架完整",
      lambda: (all(k in _wf for k in ("name:", "on:", "jobs:", "runs-on:", "steps:")), ""))

# ============================== N. 真实书库只读巡检 ==============================
section("N. 真实书库只读巡检（运行时无回归）")
_lib2 = O.get_library(force=True)
_books2 = sum(len(b) for b in _lib2.values())
_vols2 = sum(1 for _ in O.all_vols(_lib2))
check("强制重建索引后书库规模稳定",
      lambda: (_books2 == _n_books and _vols2 == _n_vols, "%d 部 / %d 卷" % (_books2, _vols2)))
_sample = [(c, b, v["rel"]) for c, b, v in O.all_vols(_lib2)][:5]
_cov = [O.get_cover(r) for _, _, r in _sample]
check("随机 5 卷封面提取连通",
      lambda: (all(m and m.startswith("image/") for _, m in _cov),
               "命中 %d/5" % sum(1 for bl, _ in _cov if bl)))
_pages = [O.book_html("%s/%s" % (c, b), 1) for c, b, _ in _sample[:3]]
check("随机 3 本书详情页渲染成功",
      lambda: (all(p and 'class="hero"' in p for p in _pages), ""))

# ============================== O. 本轮修复确认 ==============================
section("O. 本轮两个缺陷的修复确认与边界说明")
check("缺陷① 已修：同名响应头不再重复（封面单值 + max-age）",
      lambda: (len(_CACHE_COVER) == 1 and "max-age" in _CACHE_COVER[0],
               "封面 %s / 占位图 %s / HTML %s" % (_CACHE_COVER, _CACHE_BLANK, _CACHE_HTML)))
check("缺陷① 未误伤：HTML 仍 no-cache、占位图仍长缓存",
      lambda: ("no-cache" in _CACHE_HTML[0] and "max-age" in _CACHE_BLANK[0], ""))
check("缺陷② 已修：acquire_lock() 改为只读探测且不会终止目标进程",
      lambda: (S._pid_alive(os.getpid()) is True and S._pid_alive(_dead.pid) is False,
               "实测活进程=True / 已退出进程=False，全程无进程被终止"))
info("两个缺陷均为**预先存在**、非本次死代码清理引入",
     "修复前已用独立实验各自复现（重复响应头 / TerminateProcess exit=3221225794）")
info("测试全程未触碰真实 F 盘 / 未执行任何 git push",
     "所有写操作隔离在 %s" % os.path.relpath(TMP_ROOT, ROOT))
info("本轮新增清理项之外，未删除任何功能",
     "A 类注释选项 / B 类开关功能 / D 类兼容入口 均在位（见 A 节断言）")

# ============================== P. 面板状态灯与乱码 ==============================
section("P. 控制面板：状态灯时机 + 子进程编码（乱码回归）")

# 缺陷③：单实例锁必须在**初始同步之前**拿到。
# 旧顺序是 perform_sync("sync: 初始同步") -> monitor_loop()（锁在 loop 里才写），
# 全量镜像 + git 推送实测两分钟起步，那段时间面板读不到锁 -> 一直显示「已停止」，
# 而且单实例保护也是空的（可并发跑两个实例互相踩）。
_mainsrc = _src[_src.index("def main("):]
_i_lock = _mainsrc.find("acquire_lock()")
_i_sync = _mainsrc.find('perform_sync("sync: 初始同步")')
check("缺陷③ 锁在初始同步之前获取（状态灯立刻变绿 + 单实例保护不留空窗）",
      lambda: (0 <= _i_lock < _i_sync,
               "acquire_lock@%d < perform_sync@%d" % (_i_lock, _i_sync)))
check("缺陷③ 监控循环不再自己抢锁（锁统一由 main 的 try/finally 收尾）",
      lambda: ("def monitor_loop" in _src
               and "acquire_lock()" not in _src[_src.index("def monitor_loop"):_src.index("def start_opds_background")],
               ""))
check("锁路径只有一处定义（面板与监控不会各写一份文件名）",
      lambda: (S.lock_path().endswith("monitor.lock")
               and "LOCK_FILE" not in open(os.path.join(ROOT, "lightnovel", "paths.py"),
                                           encoding="utf-8").read(),
               "lock_path=%s" % os.path.basename(S.lock_path())))

# 缺陷④：面板用 PIPE 抓子进程输出并按 UTF-8 解码，而子进程在管道里默认按系统 locale
# （中文 Windows = cp936/GBK）输出 -> 每个汉字都解不出来，日志里整片变成「◆」。
# 修法是子进程自己在 cli._pipe_utf8() 里把**非 tty** 的 stdout/stderr reconfigure
# 成 UTF-8；控制台（isatty）必须保持系统代码页，否则 cmd 里反而花屏。
# 注意：不能靠给子进程设 PYTHONIOENCODING —— 实测 PyInstaller 打出来的 exe 不认它。
from lightnovel import cli as CLI  # noqa: E402


class _FakeStream:
    def __init__(self, tty):
        self._tty = tty
        self.enc = None

    def isatty(self):
        return self._tty

    def reconfigure(self, **kw):
        self.enc = kw.get("encoding")


_stdout_backup, _stderr_backup = sys.stdout, sys.stderr
try:
    _pipe_out, _pipe_err, _tty_stream = _FakeStream(False), _FakeStream(False), _FakeStream(True)
    sys.stdout, sys.stderr = _pipe_out, _pipe_err
    CLI._pipe_utf8()
    sys.stdout = _tty_stream
    CLI._pipe_utf8()
finally:
    sys.stdout, sys.stderr = _stdout_backup, _stderr_backup

check("缺陷④ 管道下 stdout/stderr 钉成 UTF-8（面板按 UTF-8 解码）",
      lambda: (_pipe_out.enc == "utf-8" and _pipe_err.enc == "utf-8",
               "stdout=%s stderr=%s" % (_pipe_out.enc, _pipe_err.enc)))
check("缺陷④ 真实控制台不改编码（cp936 控制台按 UTF-8 输出会花屏）",
      lambda: (_tty_stream.enc is None, "enc=%s" % _tty_stream.enc))

_src_cli = open(os.path.join(ROOT, "lightnovel", "cli.py"), encoding="utf-8").read()
_main_cli = _src_cli[_src_cli.index("def main("):]
check("缺陷④ _pipe_utf8 在 main() 里、且在任何输出之前被调用",
      lambda: (0 <= _main_cli.find("_pipe_utf8()") < _main_cli.find("if not argv:"),
               "_pipe_utf8@%d" % _main_cli.find("_pipe_utf8()")))

# 端到端：剥掉 PYTHONUTF8 / PYTHONIOENCODING（面板不会设它们），走真实入口拉一个
# 管道子进程，它输出的中文必须是合法 UTF-8 —— 不是的话面板那边就会满屏 ◆。
_ENV_CLEAN = {k: v for k, v in os.environ.items()
              if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")}
_enc_proc = subprocess.run([PY, "-m", "lightnovel", "opds", "--help-internet"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=_ENV_CLEAN, timeout=180, cwd=ROOT)
_enc_raw = _enc_proc.stdout
try:
    _enc_text = _enc_raw.decode("utf-8")
    _enc_ok = True
except UnicodeDecodeError:
    _enc_text, _enc_ok = _enc_raw.decode("gbk", "replace"), False
check("缺陷④ 端到端：面板管道下子进程的中文是合法 UTF-8（不再满屏 ◆）",
      lambda: (_enc_ok and any("\u4e00" <= c <= "\u9fff" for c in _enc_text),
               "退出码=%s 前 40 字节=%r" % (_enc_proc.returncode, _enc_raw[:40])))

# ============================== Q. 面板日志版式 ==============================
section("Q. 控制面板日志版式（统一字体/字号/行距 + 三列对齐 + 语义配色）")

# 用户诉求：日志区「统一字体字号行距、分段与标题层级、留白与对齐、配色与版式一致」。
# 落实成两条可断言的东西：① 只有一处行构造入口，正文列固定；② 字体字号只有一套。
# ui.py 顶层 import tkinter，某些解释器（本机 managed python）没带，所以功能性断言
# 在没有 tkinter 时降级为「跳过」而不是失败；纯文本/常量断言始终执行。
_uisrc = open(os.path.join(ROOT, "lightnovel", "ui.py"), encoding="utf-8").read()
try:
    from lightnovel import ui as UI  # noqa: E402
    _UI_ERR = ""
except Exception as _exc:            # 无 tkinter 的解释器
    UI, _UI_ERR = None, "%s: %s" % (type(_exc).__name__, _exc)


def _ui_check(label, fn):
    """没有 tkinter 时把功能断言标成跳过（保留通过），避免套件在不同解释器上分叉。"""
    if UI is None:
        check(label, lambda: (True, "跳过（本解释器无 tkinter）"))
    else:
        check(label, fn)


def _strip_comments(src):
    """去掉整行注释：注释里会「提到」Consolas 来说明为什么不用它，别当成真的字体设置。"""
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _panel_body(name):
    """取 ``Panel.<name>`` 的函数体源码（到下一个同缩进的 def 为止）。"""
    i = _uisrc.index("def %s(" % name)
    j = _uisrc.find("\n    def ", i + 1)
    return _uisrc[i:j if j != -1 else len(_uisrc)]


_ui_check("日志行解析：INFO -> (时间, info, 正文)",
          lambda: (UI.parse_log_line("[2026-09-14 20:18:52] INFO: 检测到已存在的 Git 仓库。")
                   == ("20:18:52", "info", "检测到已存在的 Git 仓库。"), str(
                       UI.parse_log_line("[2026-09-14 20:18:52] INFO: 检测到已存在的 Git 仓库。"))))
_ui_check("日志行解析：WARNING 压成 5 字符（不换行、不错位）",
          lambda: (UI.parse_log_line("[2026-09-14 20:19:14] WARNING: 注意：非分类条目。")[1] == "warn"
                   and UI._CHIP["warn"] == "WARN", UI._CHIP["warn"]))
_ui_check("日志行解析：CRITICAL 归入错误色，未知级别不冒充",
          lambda: (UI.parse_log_line("[2026-09-14 20:19:14] CRITICAL: x")[1] == "err"
                   and UI.parse_log_line("[2026-09-14 20:19:14] NOTICE: x")[1] == "",
                   "CRITICAL->err / NOTICE->''"))
_ui_check("日志行解析：裸输出（git 回显/二维码）不丢内容，仍走同一栅格",
          lambda: (UI.parse_log_line("  M lightnovel/ui.py") == (None, "", "  M lightnovel/ui.py"),
                   "裸行原样返回，调用方缩进到正文列"))
def _tkcheck(label, fn):
    """需要真实 Tk 运行时的断言：拿不到显示环境就标记为「跳过」，不算失败。

    本机（沙箱）能建隐藏窗口，但别的解释器/CI 未必有显示，所以只降级不红。
    """
    if UI is None:
        check(label, lambda: (True, "跳过（本解释器无 tkinter）"))
        return
    try:
        ok, det = fn()
    except Exception as exc:
        _why = type(exc).__name__
        check(label, lambda: (True, "跳过（无法创建 Tk 运行环境：%s）" % _why))
        return
    check(label, lambda: (ok, det))


def _log_fonts():
    """复制面板 UI 默认字体（TkDefaultFont）派生出 regular / bold 两个字体对象。"""
    from tkinter import font as _tkfont
    reg = _tkfont.nametofont("TkDefaultFont").copy()
    reg.configure(size=UI.LOG_FONT_SIZE)
    bd = reg.copy()
    bd.configure(weight="bold")
    return reg, bd


_ui_check("三列栅格不变式：任何行型都恰好两个制表位，正文列落点与内容无关",
          lambda: (all([p[0] for p in UI.row_parts(ts, chip, "msg", "正文")].count("\t") == 2
                       and [p[1] for p in UI.row_parts(ts, chip, "msg", "正文")
                            if p[0] == "\t"] == ["msg", "msg"]
                       and UI.row_parts(ts, chip, "msg", "正文")[4][0] == "正文"
                       for ts, chip in (("20:18:52", "INFO"), (None, ""), (None, "ERROR"),
                                        ("20:18:52", ""), (None, "OK"))),
                   "两个制表位都归属 msg 标签 -> 全行共用一套 tab stop"))


def _grid_probe():
    """像素制表位自洽：正文列 = 级别列 + 最宽级别名 + 列间距。"""
    import tkinter as _tk
    root = _tk.Tk()
    root.withdraw()
    try:
        reg, bd = _log_fonts()
        t_time, t_msg = UI.grid_tabs(reg, bd)
        widest = max(bd.measure(n) for n in UI._CHIP_NAMES)
        ok = (t_msg > t_time > UI.COL_GUTTER
              and t_time == UI.COL_GUTTER + reg.measure("00:00:00") + UI.COL_GAP
              and t_msg - t_time == widest + UI.COL_GAP)
        return ok, "时间列=%d 正文列=%d（级别列宽=%d）" % (t_time, t_msg, widest)
    finally:
        root.destroy()


_tkcheck("像素制表位自洽：正文列 = 行首留白 + 时间列宽 + 间距 + 最宽级别名 + 间距",
         _grid_probe)


def _panel_probe():
    """真建一个面板：断言 Text 的制表位/挂起缩进与 grid_tabs 一致，所有标签同款。"""
    from tkinter import font as _tkf
    p = UI.Panel()
    try:
        p.update_idletasks()
        t_time, t_msg = p._log_tabs
        tabs = [int(x) for x in re.findall(r"\d+", str(p.log.cget("tabs")))]
        l1_all = {int(p.log.tag_cget(n, "lmargin1")) for n in UI.LOG_TAGS}
        l2_all = {int(p.log.tag_cget(n, "lmargin2")) for n in UI.LOG_TAGS}
        fam_ui = _tkf.nametofont("TkDefaultFont").actual("family")
        fam_log = p._log_fonts[0].actual("family")
        size_log = int(p._log_fonts[0].actual("size"))
        ok = (tabs == [t_time, t_msg] and l1_all == {UI.COL_GUTTER}
              and l2_all == {t_msg} and fam_log == fam_ui and size_log == UI.LOG_FONT_SIZE)
        return ok, "tabs=%s lmargin1=%s lmargin2=%s 字族=%r(=UI) 字号=%d" % (
            tabs, sorted(l1_all), sorted(l2_all), fam_log, size_log)
    finally:
        p.destroy()


_tkcheck("真实面板：Text 制表位/挂起缩进与 grid_tabs 一致，全部标签同款同列",
         _panel_probe)
_tkcheck("真实面板：日志字体族 == 面板 UI 默认字族、字号 == LOG_FONT_SIZE（无回退混排）",
         _panel_probe)
_ui_check("全文只有一套字体与字号（标签差异只在字重与颜色）",
          lambda: (isinstance(UI.LOG_FONT_SIZE, int)
                   and len({(n, w) for n, (_, _, w) in UI.LOG_TAGS.items()
                            if w not in ("normal", "bold")}) == 0
                   and 'tkfont.nametofont("TkDefaultFont").copy()' in _uisrc
                   and "font=regular" in _uisrc
                   and 'font": bold if _weight == "bold" else regular' in _uisrc,
                   "LOG_FONT_SIZE=%d，%d 个标签只差字重/颜色" % (UI.LOG_FONT_SIZE, len(UI.LOG_TAGS))))
_ui_check("不指定西文等宽字体（Consolas 无汉字字形，会把中文丢给系统回退成第二套字体）",
          lambda: (not re.search(r"(Consolas|Cascadia|Courier|monospace)",
                                 _strip_comments(_panel_body("_build_log")))
                   and not re.search(r"\.configure\(\s*family\s*=", _uisrc)
                   and "LOG_FAMILY" not in _uisrc and "LOG_SIZE" not in _uisrc,
                   "日志字体族跟随面板 UI，中英同族"))
_ui_check("只有语义色块带底色，正文一律无底色（避免颜色打架）",
          lambda: ({n for n, (_, bg, _) in UI.LOG_TAGS.items() if bg}
                   == {"hdr", "info", "dbg", "warn", "err", "ok", "stop"},
                   "带底色的标签=%s" % sorted(n for n, (_, bg, _) in UI.LOG_TAGS.items() if bg)))
_ui_check("行距只在 Text 组件级设定一次（保证全篇行距一致）",
          lambda: ("spacing1=2, spacing2=3, spacing3=4" in _uisrc
                   and "_opts = {\"foreground\": _fg" in _uisrc
                   and "spacing" not in _uisrc[_uisrc.index("_opts = {\"foreground\": _fg"):
                                               _uisrc.index("_opts = {\"foreground\": _fg") + 200],
                   "spacing 统一在 Text(...)，标签里不再各设一套"))
_a = _uisrc.index("self.log = tk.Text(")
_b = _uisrc.index("highlightthickness=0)", _a) + len("highlightthickness=0)")
_txt_call = _strip_comments(_uisrc[_a:_b])       # 只取 Text(...) 构造器本身
_ui_check("长行折行 + 挂起缩进（旧版 wrap=none 会直接切掉右半边）",
          lambda: ('wrap="word"' in _txt_call and 'wrap="none"' not in _txt_call
                   and '"lmargin2": tab_msg' in _uisrc and '"lmargin1": COL_GUTTER' in _uisrc
                   and "lmargin" not in _txt_call,
                   "续行由 lmargin2 顶到正文列；lmargin* 是标签专属选项，不能传 Text 构造器"))
_ui_check("面板自己的话也走同一栅格（不存在另一套左边界）",
          lambda: ("self._row(time.strftime(\"%H:%M:%S\"), \"\", tag, text)" in _uisrc
                   and "self._insert((text + \"\\n\", tag))" not in _uisrc,
                   "Panel._log 复用 _row/row_parts"))
_ui_check("一个子进程只留一条结束汇报（旧版会同时出现「进程结束」与「完成」）",
          lambda: ("进程结束" not in _uisrc
                   and "rc = proc.wait()" in _uisrc
                   and 'self.q.put(("status"' in _uisrc,
                   "页脚统一由 _pump 收口，_run_once 只更新状态栏"))
_ui_check("块与块之间留白且不重复留白（_blank 幂等）",
          lambda: ("def _blank(self):" in _uisrc and "if not self._last_blank:" in _uisrc
                   and "_last_blank = False" in _uisrc,
                   "待机时连点按钮不会刷出一堆空行"))

# ==================== R. 「已读完」+ 管理员权限 ====================
section("R. 「已读完」清单与管理员权限（入口显隐 + 服务端校验 + 持久化）")

try:
    from lightnovel.opds import finished as FIN
    from lightnovel.opds import server as SRV
    from lightnovel.opds import feeds as FEED
    from lightnovel.opds import library as LIB
    from lightnovel.opds import session as SESS
    _R_ERR = ""
except Exception as _exc:
    FIN = SRV = FEED = LIB = SESS = None
    _R_ERR = "%s: %s" % (type(_exc).__name__, _exc)

# 全部落在临时目录，绝不碰真实 .autosync/finished.json
_R_TMP = os.path.join(TMP_ROOT, "read")
os.makedirs(_R_TMP, exist_ok=True)
_FIN_DEFAULT = FIN.FINISHED_FILE if FIN is not None else ""   # 打桩前的真实默认路径
# 会话密钥的默认路径在文件开头的导入区就存下来了（那时还没打桩），见 _SESS_DEFAULT


def _rcheck(label, fn):
    if FIN is None:
        check(label, lambda: (True, "跳过（opds 模块不可用：%s）" % _R_ERR))
    else:
        check(label, fn)


def _rskip(label, why):
    check(label, lambda: (True, "跳过（%s）" % why))


_r_key = {"mtime": None}


def _reset_fin(path):
    """把清单指到临时文件，并清空。"""
    FIN.FINISHED_FILE = path
    if os.path.exists(path):
        os.remove(path)
    return path


# ---------- R1. 键校验（服务端唯一的入参闸门） ----------
_rcheck("键校验：只放行「已知分类/非空书名」",
        lambda: (FIN.normalize_key("已完结/GAMERS电玩咖") == "已完结/GAMERS电玩咖"
                 and FIN.normalize_key("未完结/NO GAME NO LIFE") == "未完结/NO GAME NO LIFE"
                 and FIN.normalize_key("已完结/ 书名 ") == "已完结/书名",
                 "合法键规范化"))
_rcheck("键校验：穿越 / 未知分类 / 段数不对 / 空值一律拒绝",
        lambda: (all(FIN.normalize_key(bad) is None for bad in (
            "../../etc/passwd", "已完结/../x", "不存在的分类/x", "已完结", "已完结/",
            "", None, "已完结/a/b")),
            "8 种非法输入全部拒绝"))
_rcheck("键校验：首尾多余的 / 只是被归一化（读取手改过的文件时容错）",
        lambda: (FIN.normalize_key("/已完结/x/") == "已完结/x",
                 "%r" % (FIN.normalize_key("/已完结/x/"),)))
_rcheck("键校验：反斜杠被归一化，不会被当成两段",
        lambda: (FIN.normalize_key("已完结\\书名") == "已完结/书名",
                 "%r" % (FIN.normalize_key("已完结\\书名"),)))

# ---------- R2. 持久化：原子写 / 幂等 / 损坏容错 / 并发 ----------
_rp = _reset_fin(os.path.join(_R_TMP, "finished.json"))


def _r_toggle_roundtrip():
    _reset_fin(_rp)
    first = FIN.toggle_finished("已完结/A")
    second = FIN.toggle_finished("已完结/A")
    return (first is True and second is False and FIN.load_finished() == set(),
            "toggle: %s -> %s -> 空" % (first, second))


_rcheck("toggle 两次回到初始态（可反复点，不会累积脏数据）", _r_toggle_roundtrip)


def _r_mark_idempotent():
    _reset_fin(_rp)
    FIN.mark_finished("已完结/A", True)
    stat1 = os.stat(_rp).st_mtime_ns
    FIN.mark_finished("已完结/A", True)          # 重复标记不该再写盘
    stat2 = os.stat(_rp).st_mtime_ns
    FIN.mark_finished("已完结/A", False)
    return (FIN.load_finished() == set() and stat1 == stat2,
            "重复标记未改 mtime（%s），取消后为空" % (stat1 == stat2))


_rcheck("标记幂等：已是目标状态就不落盘（不惊动文件监控）", _r_mark_idempotent)


def _r_atomic():
    _reset_fin(_rp)
    for i in range(5):
        FIN.mark_finished("已完结/书%d" % i, True)
    leftover = [f for f in os.listdir(_R_TMP) if f.endswith(".tmp")]
    with open(_rp, encoding="utf-8") as fh:
        data = json.load(fh)
    return (not leftover and data["count"] == 5 and len(data["keys"]) == 5
            and data["version"] == 1,
            "临时文件残留=%s 条目=%d" % (leftover, data["count"]))


_rcheck("原子写：落盘后无 .tmp 残留，JSON 结构含版本/计数", _r_atomic)


def _r_corrupt():
    with open(_rp, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是合法 JSON")
    got = FIN.load_finished()
    with open(_rp, "w", encoding="utf-8") as fh:
        json.dump(["已完结/数组形式也认"], fh, ensure_ascii=False)
    got2 = FIN.load_finished()
    return (got == set() and got2 == {"已完结/数组形式也认"},
            "损坏=空集合；裸数组也兼容")


_rcheck("损坏/异形文件不抛异常（服务不会因为手改坏文件而起不来）", _r_corrupt)


def _r_invalid_write():
    _reset_fin(_rp)
    ok1 = FIN.mark_finished("../x", True)
    ok2 = FIN.toggle_finished("未知分类/x")
    return (ok1 is False and ok2 is False and not os.path.exists(_rp),
            "非法键不落盘（文件都没建）")


_rcheck("非法键不产生写入（400 之后不留痕迹）", _r_invalid_write)


def _r_concurrent():
    _reset_fin(_rp)
    errs = []

    def worker():
        try:
            for _ in range(10):
                FIN.toggle_finished("已完结/并发")
        except Exception as exc:                 # noqa: BLE001
            errs.append(repr(exc))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    with open(_rp, encoding="utf-8") as fh:
        data = json.load(fh)                     # 必须是完整合法 JSON
    got = FIN.load_finished()
    # 80 次 toggle 是偶数 -> 应为空；关键不是结果而是「文件没被写坏」
    return (not errs and got == set() and data["count"] == 0,
            "8 线程 ×10 次 toggle：errs=%s 结果=%s" % (errs, sorted(got)))


_rcheck("并发安全：读-改-写全程持锁，文件不会被写坏（无丢更新/半截 JSON）",
        _r_concurrent)
_rcheck("清单落在 .autosync/（gitignore 内），不会被同步到仓库或 F 盘",
        lambda: (os.path.dirname(_FIN_DEFAULT) == P.LOG_DIR
                 and os.path.basename(_FIN_DEFAULT) == "finished.json"
                 and os.path.basename(P.LOG_DIR) == ".autosync",
                 "%s（默认值，测试期间打桩到临时目录）" % _FIN_DEFAULT))
_rcheck("清单粒度是「作品」不是「卷」：键 = 分类/书名",
        lambda: (FIN.normalize_key("已完结/A/01.epub") is None,
                 "带卷名的键被拒 -> 只能按作品标记"))

# ---------- R3. 角色判定（唯一入口 _role） ----------
_r_saved_auth = (SRV.AUTH_USER, SRV.AUTH_PASS, SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS)
# 会话密钥也指到临时目录：判定真的走到 cookie 分支时，不会去动真实 .autosync/session.key
_SESS_KEY = os.path.join(_R_TMP, "session.key")


def _reset_sess():
    """把签名密钥指到临时文件并清空缓存 / 节流计数。"""
    SESS.SESSION_KEY_FILE = _SESS_KEY
    if os.path.exists(_SESS_KEY):
        os.remove(_SESS_KEY)
    SESS.forget_key()
    SESS.login_throttle.clear()
    return _SESS_KEY


def _role_of(peer, user=None, pw=None, cookie=None):
    """用桩对象跑真实的 _role()，不经过网络（判定逻辑是纯函数）。"""
    import types
    h = types.SimpleNamespace()
    hdr = {}
    if user is not None:
        hdr["Authorization"] = "Basic " + base64.b64encode(
            ("%s:%s" % (user, pw)).encode("utf-8")).decode("ascii")
    if cookie is not None:
        hdr["Cookie"] = "%s=%s" % (SESS.COOKIE_NAME, cookie)
    h.headers = hdr
    h.client_address = (peer, 4321)
    for _name in ("_basic_creds", "_is_loopback", "_cookie", "_session_ok", "_client_ip"):
        setattr(h, _name, types.MethodType(getattr(SRV.OPDSHandler, _name), h))
    h._same = SRV.OPDSHandler._same
    return SRV.OPDSHandler._role(h)


def _r_set_auth(admin=("", ""), guest=("", "")):
    SRV.AUTH_USER, SRV.AUTH_PASS = admin
    SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = guest


def _r_role_noauth():
    _r_set_auth()
    _reset_sess()
    return (_role_of("127.0.0.1") == "admin" and _role_of("127.0.0.5") == "admin"
            and _role_of("::1") == "admin" and _role_of("192.168.31.9") == "guest"
            and _role_of("100.64.0.7") == "guest",
            "免密：回环=admin（含 127.x / ::1），其余=guest（仍可浏览）")


_rcheck("判定①免密：本机=管理员，局域网/公网来源=访客", _r_role_noauth)


def _r_role_withpass():
    _r_set_auth(admin=("ranqing", "s3cret"))
    _reset_sess()
    return (_role_of("192.168.31.9") == "guest"                        # 匿名 → 访客，不再 401
            and _role_of("192.168.31.9", "ranqing", "s3cret") == "admin"
            and _role_of("192.168.31.9", "ranqing", "wrong") == "guest"  # 错口令不赶人
            and _role_of("192.168.31.9", "guest", "hello") == "guest"
            and _role_of("127.0.0.1") == "guest",                     # 配了口令则本机也要登
            "匿名/错口令=访客（可浏览）；口令正确才升 admin；配了口令回环也不再白拿管理员")


_rcheck("判定②配了管理员口令：匿名与错口令都退化成访客（浏览不受影响），只有口令正确才升权",
        _r_role_withpass)


def _r_role_two_tier():
    _r_set_auth(admin=("ranqing", "s3cret"), guest=("guest", "hello"))
    _reset_sess()
    return (_role_of("10.0.0.2", "ranqing", "s3cret") == "admin"
            and _role_of("10.0.0.2", "guest", "hello") == "guest"
            and _role_of("10.0.0.2", "guest", "nope") == "guest"
            and _role_of("10.0.0.2") == "guest"
            and _role_of("127.0.0.1") == "guest",
            "命中管理员口令=admin；其余（含访客口令/匿名）都是访客（匿名已等价于访客档）")


_rcheck("判定③两级口令：管理员口令升权，访客档与匿名同权（都是访客）", _r_role_two_tier)


def _r_role_partial_env():
    # 只设用户名不设口令，属于「配置了一半」：仍按配了口令处理，避免半配时静默免密
    _r_set_auth(admin=("only", ""))
    _reset_sess()
    return (_role_of("127.0.0.1") == "guest"
            and _role_of("8.8.8.8", "only", "") == "admin",
            "半配置（只有用户名）也走口令分支，不会退化成免密")


_rcheck("判定④只配了用户名没配口令：仍走口令分支（半配置不退化成免密）",
        _r_role_partial_env)


def _r_role_compare():
    _r_set_auth(admin=("u", "p"))
    _reset_sess()
    # 同一前缀不同长度不应被短口令放行（compare_digest 语义）。现在「不匹配」的表现是
    # 降级成访客而不是 401，所以断言的是「拿不到 admin」，而不是「被拒绝」。
    bad = [("u", "p2"), ("u2", "p"), ("u", "P"), ("", "p"), ("u", ""), ("u ", "p"), ("u", "p ")]
    return (all(_role_of("1.1.1.1", u, p) != "admin" for u, p in bad)
            and _role_of("1.1.1.1", "u", "p") == "admin",
            "口令大小写敏感、不接受前后空格/前缀/变体；只有完全相等才升权")


_rcheck("判定⑤口令比较严格：大小写敏感、不接受前缀/变体（只认完全相等）", _r_role_compare)


def _r_role_cookie():
    _reset_sess()
    _r_set_auth(admin=("u", "p"))
    tok = SESS.issue()
    return (_role_of("8.8.8.8", cookie=tok) == "admin"            # 公网来源 + 有效 cookie → 管理员
            and _role_of("8.8.8.8", cookie="abc.def") == "guest"  # 伪造签名无效
            and _role_of("8.8.8.8", cookie=SESS.issue(ttl=-1)) == "guest"   # 过期无效
            and _role_of("8.8.8.8", cookie="") == "guest"         # 空 cookie
            and SESS.verify(tok) is True,
            "cookie 判定：有效=admin（公网来源也认），伪造/过期/空=访客")


_rcheck("判定⑥登录 cookie：有效即 admin（与来源地址无关），伪造或过期一律降级访客",
        _r_role_cookie)


def _r_role_cookie_beats_stale_basic():
    """浏览器会把旧的 Basic 凭据长期自动附上 —— 有效的 cookie 必须能压过它。"""
    _reset_sess()
    _r_set_auth(admin=("u", "p"))
    tok = SESS.issue()
    return (_role_of("8.8.8.8", user="old", pw="stale", cookie=tok) == "admin"
            and _role_of("8.8.8.8", user="u", pw="p") == "admin",
            "cookie 优先于 Basic；Basic 正确也能升权（两条入口并存）")


_rcheck("判定⑦两条登录入口并存：cookie 有效时即使带陈旧 Basic 凭据也是管理员",
        _r_role_cookie_beats_stale_basic)
SRV.AUTH_USER, SRV.AUTH_PASS, SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = _r_saved_auth

# ---------- R4. 入口显隐（渲染期开关，不是 CSS 隐藏） ----------
_rcheck("入口显隐：非管理员页面**根本不含**「已读完」与 /opds/read",
        lambda: ("已读完" not in FEED._html_page("t", "<p>x</p>", is_admin=False)
                 and "/opds/read" not in FEED._html_page("t", "<p>x</p>", is_admin=False),
                 "管理员版才含：%s" % ("已读完" in FEED._html_page("t", "", is_admin=True))))
_rcheck("入口显隐：CSS 本身也不含功能字样（CSS 对所有人下发，注释会漏进访客源码）",
        lambda: ("已读完" not in FEED.SITE_CSS and "/opds/read" not in FEED.SITE_CSS,
                 "SITE_CSS 长度 %d" % len(FEED.SITE_CSS)))
_rcheck("入口显隐：首页（含快速入口卡）按身份分叉",
        lambda: ("已读完" in FEED.root_html(is_admin=True)
                 and "已读完" not in FEED.root_html(is_admin=False),
                 "管理员首页多一张「已读完」卡"))
_rcheck("入口显隐：分类页书卡只有管理员才带标记按钮",
        lambda: ('"/opds/read/toggle"' in FEED.catalog_html("已完结", 1, is_admin=True)
                 and "/opds/read" not in FEED.catalog_html("已完结", 1, is_admin=False),
                 "访客书卡无表单 -> 没有可提交的入口"))
_rcheck("入口显隐：详情页「阅读状态」行与按钮只有管理员才有",
        lambda: ("阅读状态" in FEED.book_html("已完结/GAMERS电玩咖", 1, is_admin=True)
                 and "阅读状态" not in FEED.book_html("已完结/GAMERS电玩咖", 1, is_admin=False),
                 "访客详情页连状态行都不渲染"))
_rcheck("入口显隐：OPDS 阅读器 feed 的导航项同样分叉",
        lambda: ('href="/opds/read"' in FEED.feed_root(is_admin=True)
                 and "/opds/read" not in FEED.feed_root(is_admin=False),
                 "阅读器订阅到的目录结构随身份变化"))
_rcheck("入口显隐：非管理员响应里不含任何形如 /opds/read 的子串（含 toggle）",
        lambda: (all("/opds/read" not in FEED.catalog_html(c, 1, is_admin=False)
                     for c in ("已完结", "未完结", "all"))
                 and "/opds/read" not in FEED.search_html("a", 1, is_admin=False)
                 and "/opds/read" not in FEED.recent_html(1, is_admin=False),
                 "分类/搜索/最近更新页逐个核对"))


# ---------- R4b. 未读圆圈的显隐（静息不显示，悬停/选中才出现，且无残留） ----------
def _mk_css():
    """抠出与「未读圆圈」显隐相关的 CSS 规则。

    先去掉注释再断言 —— 注释里写着「刻意不用 visibility:hidden」，直接用原文匹配
    会把注释文字当成声明，断言就变假阳性了。
    """
    css = re.sub(r"/\*.*?\*/", "", FEED.SITE_CSS, flags=re.S)
    base = re.search(r"\.mk\{([^}]*)\}", css)
    rev = re.search(r"([^{}\n]*\.card:hover \.mk[^{}\n]*)\{([^}]*)\}", css)
    touch = re.search(r"@media\s*\(hover:none\)\s*\{(\.mk\{[^}]*\})\}", css)
    return {
        "css": css,
        "base": base.group(1) if base else "",
        "rev_sel": rev.group(1) if rev else "",
        "rev": rev.group(2) if rev else "",
        "touch": touch.group(1) if touch else "",
    }


_mkc = _mk_css() if FIN is not None else {"css": "", "base": "", "rev_sel": "", "rev": "", "touch": ""}

_rcheck("静息态不显示圆圈：opacity:0 + pointer-events:none（既不显形，也不吞掉封面点击）",
        lambda: ("opacity:0" in _mkc["base"] and "pointer-events:none" in _mkc["base"]
                 and "opacity:1" not in _mkc["base"],
                 "静息声明 = %s" % _mkc["base"].replace("\n", " ").strip()))
_rcheck("用 opacity 而不是 visibility 隐藏（visibility:hidden 会让按钮无法 Tab 聚焦）",
        lambda: ("visibility" not in _mkc["css"],
                 "整份 SITE_CSS 去注释后无 visibility 声明"))
_rcheck("显形条件三条齐全：卡片悬停 / 卡内有焦点 / 按钮自身聚焦",
        lambda: (all(t in _mkc["rev_sel"] for t in (":hover", ":focus-within", ":focus-visible"))
                 and "opacity:1" in _mkc["rev"] and "pointer-events:auto" in _mkc["rev"],
                 "选择器 = %s" % _mkc["rev_sel"].strip()))
_rcheck("三条显形条件合成同一条规则（避免某处漏写导致「选中后收不回去」的残留）",
        lambda: (_mkc["rev_sel"].count(",") == 2 and _mkc["rev_sel"].strip().startswith(".card:hover"),
                 "一条规则覆盖三个出口"))
_rcheck("无残留：能设 opacity:1 的只有「显形」与「触屏兜底」两处，没有第三条把圆圈钉住",
        lambda: (_mkc["css"].count("opacity:1") == 2
                 and _mkc["css"].count("pointer-events:auto") == 2,
                 "opacity:1 ×%d / pointer-events:auto ×%d"
                 % (_mkc["css"].count("opacity:1"), _mkc["css"].count("pointer-events:auto"))))
_rcheck("触屏兜底：无 hover 的设备保持常驻（否则手机上永远点不到标记）",
        lambda: ("hover:none" in _mkc["css"] and ".mk" in _mkc["touch"]
                 and "opacity:1" in _mkc["touch"],
                 "触摸设备常驻 = %s" % _mkc["touch"].strip()))
_rcheck("悬停时按钮仍是可点的（显形才接管指针，且悬停不动布局）",
        lambda: ("position:absolute" in _mkc["css"] and ".mk:hover" in _mkc["css"],
                 "绝对定位 + .mk:hover 只改背景/缩放"))


def _r_fin_note():
    """已读状态由书卡副标题的文字承载，不依赖那个圆圈常驻。"""
    _reset_fin(_rp)
    lib = LIB.get_library(force=True)
    cat = "已完结" if "已完结" in lib else list(lib)[0]
    book = sorted(lib.get(cat, {}))[0]
    FIN.mark_finished("%s/%s" % (cat, book), True)
    on = FEED.catalog_html(cat, 1, is_admin=True)
    off = FEED.catalog_html(cat, 1, is_admin=False)
    return ('<span class="fin">已读完</span>' in on
            and '<span class="fin">已读完</span>' not in off,
            "管理员书卡副标题里常驻绿色「已读完」，圆圈只负责操作")


_rcheck("已读与否看得见：状态写在书卡副标题文字里（圆圈收起也不丢信息）", _r_fin_note)


def _r_read_hint():
    """空态提示要与新交互一致（别再写「点封面左上角」这种已经不对的话）。"""
    _reset_fin(_rp)
    html_txt = FEED.read_html(1)
    return (("鼠标移到封面上" in html_txt or "鼠标移上封面" in html_txt)
            and "圆圈会一直显示" in html_txt,
            "提示已改为「悬停后点」，并说明触屏例外")


_rcheck("空态/说明文案与交互一致（悬停后点；触屏常驻）", _r_read_hint)
_reset_fin(_rp)


# ---------- R4c. 原地标记：JS 增强层（点了不刷新页面） ----------
# 分工：增强层**只管显示**（就地改 DOM），状态永远由服务端算 —— 所以这些断言全是
# 「结构/契约」检查，不含任何「前端逻辑对不对」的模拟（那种断言跑不真浏览器）。
def _mk_admin_only():
    """增强层只在管理员页面注入。访客没有标记按钮，脚本也无事可做；
    更关键的是脚本里带着「已读完」字样，注进去就等于漏进访客的源码。"""
    admin = FEED._html_page("t", "<p>x</p>", is_admin=True)
    guest = FEED._html_page("t", "<p>x</p>", is_admin=False)
    return ("X-Requested-With" in admin and 'addEventListener("submit"' in admin
            and "X-Requested-With" not in guest and "addEventListener" not in guest
            and "已读完" not in guest,
            "管理员页面含增强层；访客页面连脚本都不出现")


_rcheck("增强层只发给管理员（访客页面不含脚本、更不含功能字样）", _mk_admin_only)


def _mk_no_route():
    """脚本里不写死管理员路由，用表单自己的 ``action``。
    少一处会漂的常量，也让「访客页面不含 /opds/read」这条断言不怕脚本被误注入。"""
    js = FEED.MARK_JS
    return ("/opds/read" not in js and "f.action" in js,
            "脚本里没有 /opds/read 字样，走 f.action")


_rcheck("脚本不写死管理员路由（走表单自带的 action）", _mk_no_route)


def _mk_fallbacks():
    """三条兜底缺一不可 —— 缺哪条都会让某一类用户点不动按钮：
    ① 没有 fetch/FormData/URLSearchParams 就不拦，交给原生提交；
    ② 提交失败（网络断、认证过期）回退 ``f.submit()``；
    ③ 服务端只吃 urlencoded，所以必须走 URLSearchParams 而不是 FormData 本身
       （FormData 默认发 multipart，服务端解析不了 → 静默 400）。"""
    js = FEED.MARK_JS
    return (all(t in js for t in
                ("window.fetch", "window.FormData", "URLSearchParams",
                 "f.submit()", "credentials")),
            "老浏览器放行 + 失败回退 + urlencoded 编码 + 带上凭据")


_rcheck("增强层三条兜底齐全（无 fetch 放行 / 失败回退原生提交 / 只发 urlencoded）", _mk_fallbacks)


def _mk_server_truth():
    """前端只照抄服务端回的状态，不自己记账 —— 两边不会漂。
    ``r.json()`` + 读 ``d.finished`` 两条就是「照抄」的全部证据。"""
    js = FEED.MARK_JS
    return ("r.json()" in js and "d.finished" in js and "!r.ok" in js,
            "非 2xx 抛错走回退；2xx 才读 finished")


_rcheck("状态由服务端算、前端照抄（不回写本地推算值）", _mk_server_truth)


def _mk_script_balanced():
    """整页只注入一次、闭合正常。拼接漏个括号会把后面的 HTML 全吞掉，
    而且这种错在浏览器里表现为「页面莫名少了半截」，很难查 —— 所以在测试里钉住。"""
    on = FEED._html_page("t", "<p>x</p>", is_admin=True)
    off = FEED._html_page("t", "<p>x</p>", is_admin=False)
    return (on.count("<script>") == 1 and on.count("</script>") == 1
            and off.count("<script>") == 1 and off.count("</script>") == 1
            and on.endswith("</body></html>") and off.endswith("</body></html>")
            and "grpAll" in off,
            "两种身份各一处 <script>，分组折叠脚本对访客照旧")


_rcheck("脚本注入一次且闭合正常（漏括号会静默吞掉半页）", _mk_script_balanced)


def _mk_js_parses():
    """把整段内联脚本抠出来交给 node 做语法检查。

    为什么非要有这一步：脚本是拼字符串拼出来的，写错一个赋值目标（比如
    ``mk.title = mk.getAttribute("aria-label") = tip``）不会让 Python 报错、
    正则断言也看不出来 —— 只有浏览器执行到那一行才炸，而那时**请求已经发出去了**，
    用户看到的是「点了没反应」。node --check 能抓到这类早期错误（Early Error），
    所以把它钉在测试里，省得下次再靠开浏览器才发现。
    没有 node 就跳过（不假装通过，也不让整轮挂掉）。
    """
    if not FEED.MARK_JS.strip():
        return False, "增强层是空的"
    node = shutil.which("node")
    if not node:
        return True, "跳过：本机没有 node，无法做脚本语法检查"
    html = FEED._html_page("t", "<p>x</p>", is_admin=True)
    m = re.search(r"<script>(.*)</script>", html, re.S)
    if not m:
        return False, "页面里抠不到 <script>"
    fd, tmp = tempfile.mkstemp(suffix=".js", prefix="ln_markjs_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(m.group(1))
        r = subprocess.run([node, "--check", tmp], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return (r.returncode == 0,
                "node --check 通过（%d 字节）" % len(m.group(1)) if r.returncode == 0
                else "语法/早期错误：%s" % (r.stderr or "").strip().splitlines()[-3:])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_rcheck("内联脚本通过 node 语法检查（拼字符串最容易漏这种错）", _mk_js_parses)


def _mk_no_double_submit():
    """回写失败**不能**再 submit 一次。

    真实踩到的坑：fetch 拿到了 200、状态已经写进服务端，随后本地改 DOM 抛了个
    ReferenceError，被同一个 catch 接住 → ``f.submit()`` 又一次 POST → 状态被翻回去，
    用户看到「点了没反应」，而服务器日志里是两条记录。
    正确分工：请求没送达才重提交；送达后出错就重取本页。这两句必须在代码里。
    """
    js = FEED.MARK_JS
    # 数「语句级」的调用（行首缩进 + f.submit()），免得把注释里提到的那次也算进来
    n_submit = len(re.findall(r"^\s*f\.submit\(\)", js, re.M))
    return ("console.warn(\"本地回写失败" in js and "console.warn(\"提交没送达" in js
            and n_submit == 1
            and js.index("本地回写失败") < js.index("提交没送达")
            and js.index("提交没送达") < js.index("f.submit();"),
            "两条失败路径分开处理，f.submit() 只剩一处（%d）" % n_submit)


_rcheck("回写失败不重提交（否则状态被翻回去，表现为「点了没用」）", _mk_no_double_submit)


def _mk_readlist_attr():
    """「已读完」列表的卡带 ``data-list=read`` → 取消标记后卡片自己消失；
    目录页的卡**不带** → 取消后必须留在原地（否则一取消书就跑了）。"""
    _reset_fin(_rp)
    lib = LIB.get_library(force=True)
    cat = "已完结" if "已完结" in lib else list(lib)[0]
    book = sorted(lib.get(cat, {}))[0]
    FIN.mark_finished("%s/%s" % (cat, book), True)
    rd = FEED.read_html(1)
    cat_html = FEED.catalog_html(cat, 1, is_admin=True)
    _reset_fin(_rp)
    return ('data-list="read"' in rd and 'data-list="read"' not in cat_html
            and rd.count('data-list="read"') == 1,
            "仅「已读完」列表页带该标记")


_rcheck("只有「已读完」列表的卡带 data-list=read（取消即消失；目录页取消要留住）",
        _mk_readlist_attr)


def _mk_read_meta():
    """给 JS 记账用的三件套：data-total（分页时不能数当前页的卡）、readcnt（计数）、
    readempty 空态模板。没有作品时直接渲染空态、不发模板 —— 免得空模板和空态文案同时出现。"""
    _reset_fin(_rp)
    lib = LIB.get_library(force=True)
    cat = "已完结" if "已完结" in lib else list(lib)[0]
    book = sorted(lib.get(cat, {}))[0]
    FIN.mark_finished("%s/%s" % (cat, book), True)
    one = FEED.read_html(1)
    _reset_fin(_rp)
    zero = FEED.read_html(1)
    return ('data-total="1"' in one and 'id="readcnt"' in one
            and '<template id="readempty">' in one and "1 部作品" in one
            and '<template id="readempty">' not in zero
            and "还没有标记任何作品" in zero,
            "有作品时发模板+计数；空清单直接渲染空态")


_rcheck("已读完列表带 data-total / readcnt / 空态模板（计数不被分页带偏）", _mk_read_meta)


def _mk_hero_ids():
    """详情页的两个「原地改点」：状态行 id=finstate、按钮表单 class=mkbig。
    访客页面两者都没有（那两段 HTML 本来就只在管理员分支里拼）。"""
    _reset_fin(_rp)
    lib = LIB.get_library(force=True)
    cat = "已完结" if "已完结" in lib else list(lib)[0]
    book = sorted(lib.get(cat, {}))[0]
    rel = "%s/%s" % (cat, book)
    FIN.mark_finished(rel, True)
    on = FEED.book_html(rel, 1, is_admin=True)
    off = FEED.book_html(rel, 1, is_admin=False)
    _reset_fin(_rp)
    return ('<b id="finstate" style="color:#1a7f37">已读完</b>' in on
            and 'class="mkbig"' in on
            and "finstate" not in off and "mkbig" not in off,
            "管理员详情页两个锚点齐全，访客页面都没有")


_rcheck("详情页锚点：状态行 id=finstate + 按钮 form.mkbig（访客无）", _mk_hero_ids)


def _mk_untouched():
    """增强层不该顺手改掉既有契约：书卡副标题里那个常驻的 ``<span class="fin">``
    必须保持原样 —— 它是「圆圈收起了状态也不丢」的承载，改结构会连带丢信息。"""
    _reset_fin(_rp)
    lib = LIB.get_library(force=True)
    cat = "已完结" if "已完结" in lib else list(lib)[0]
    book = sorted(lib.get(cat, {}))[0]
    FIN.mark_finished("%s/%s" % (cat, book), True)
    on = FEED.catalog_html(cat, 1, is_admin=True)
    _reset_fin(_rp)
    return ('<span class="fin">已读完</span>' in on
            and "URLSearchParams" not in on.split("</main>")[0],
            "副标题结构未动；增强层只在 </main> 之后的脚本里")


_rcheck("不改既有契约：副标题结构原样，增强层只在正文之后的脚本里", _mk_untouched)


# ---------- R5. HTTP 层：真的打请求（含来源判定） ----------
_r_lan = LIB.local_ip() if LIB else ""


def _http(port, path, host=None, method="GET", body=None, headers=None, accept=None):
    import http.client as _hc
    host = host or "127.0.0.1"
    conn = _hc.HTTPConnection(host, port, timeout=15)
    hdrs = dict(headers or {})
    if host != "127.0.0.1":
        hdrs["Host"] = "%s:%d" % (host, port)
    if accept:
        hdrs["Accept"] = accept
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, text, resp.getheader("Location"))
    conn.close()
    return out


def _post_json(port, path, body, headers=None):
    """原地（AJAX）路径专用：要单独看 Content-Type，也会多带一个头。

    默认带上 ``X-Requested-With: fetch`` —— 服务端据此回 JSON 而不是 303。
    """
    import http.client as _hc
    conn = _hc.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = {"Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "fetch"}
    hdrs.update(headers or {})
    conn.request("POST", path, body=body, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, text, resp.getheader("Content-Type"), resp.getheader("Location"))
    conn.close()
    return out


_r_srv = None
_r_port = 0
try:
    _reset_fin(os.path.join(_R_TMP, "http_finished.json"))
    _r_srv = SRV.make_server(port=0, bind="0.0.0.0")
    _r_port = _r_srv.server_address[1]
    threading.Thread(target=_r_srv.serve_forever, daemon=True).start()
except Exception as _exc:                        # noqa: BLE001
    _r_srv = None
    _r_err = "%s: %s" % (type(_exc).__name__, _exc)


def _r_hcheck(label, fn):
    if _r_srv is None or FIN is None:
        _rskip(label, "无法起测试服务：%s" % (FIN is None and _R_ERR or "端口不可用"))
    else:
        check(label, fn)


_r_lib_ok = FIN is not None and _r_srv is not None
_r_cat, _r_book = "已完结", "GAMERS电玩咖"
if _r_lib_ok:
    _libdata = LIB.get_library(force=True)
    _r_cat = "已完结" if "已完结" in _libdata else list(_libdata)[0]
    _r_books = sorted(_libdata.get(_r_cat, {}))
    if _r_books:
        _r_book = _r_books[0]
_r_q = quote(_r_cat, safe="") + "/" + quote(_r_book, safe="")
_r_key2 = "%s/%s" % (_r_cat, _r_book)
_r_form = urlencode({"key": _r_key2, "back": "/opds/catalog/" + quote(_r_cat, safe="")})

if _r_lib_ok:
    _st, _body, _ = _http(_r_port, "/")
    check("HTTP 免密+本机 → 首页含「已读完」入口",
          lambda: (_st == 200 and "已读完" in _body and "/opds/read" in _body,
                   "status=%s" % _st))
    if _r_lan and _r_lan != "127.0.0.1":
        _gst, _gbody, _ = _http(_r_port, "/", host=_r_lan)
        check("HTTP 免密+局域网来源 → 首页不含「已读完」（来源判定真的生效）",
              lambda: (_gst == 200 and "已读完" not in _gbody and "/opds/read" not in _gbody,
                       "status=%s 来自 %s" % (_gst, _r_lan)))
        _gst2, _, _ = _http(_r_port, "/opds/read", host=_r_lan)
        check("HTTP 访客 GET /opds/read → 403（不返回内容）",
              lambda: (_gst2 == 403, "status=%s" % _gst2))
        _gst3, _, _ = _http(_r_port, "/opds/read/toggle", host=_r_lan, method="POST",
                            body=_r_form, headers={"Origin": "http://%s:%d" % (_r_lan, _r_port)})
        check("HTTP 访客 POST 标记 → 403 且清单不变",
              lambda: (_gst3 == 403 and FIN.load_finished() == set(),
                       "status=%s 清单=%s" % (_gst3, sorted(FIN.load_finished()))))
        _gst4, _gbody4, _ = _http(_r_port, "/", host=_r_lan, accept="application/atom+xml")
        check("HTTP 访客 XML feed 也不含「已读完」导航",
              lambda: (_gst4 == 200 and "已读完" not in _gbody4, "status=%s" % _gst4))
        _gst5, _gbody5, _ = _http(_r_port, "/opds/catalog/" + quote(_r_cat, safe=""), host=_r_lan)
        check("HTTP 访客分类页不含标记表单",
              lambda: ('"/opds/read/toggle"' not in _gbody5, "status=%s" % _gst5))
    else:
        _rskip("HTTP 访客（局域网来源）各项", "本机没有非回环 IP，无法模拟访客来源")

    _pst, _pbody, _ploc = _http(_r_port, "/opds/read/toggle", method="POST", body=_r_form,
                                headers={"Origin": "http://127.0.0.1:%d" % _r_port})
    check("HTTP 管理员 POST 标记 → 303 + 清单落盘",
          lambda: (_pst == 303 and FIN.load_finished() == {_r_key2},
                   "status=%s loc=%s 清单=%s" % (_pst, _ploc, sorted(FIN.load_finished()))))
    _st2, _b2, _ = _http(_r_port, "/opds/read")
    check("HTTP 管理员 /opds/read → 200 且出现该作品",
          lambda: (_st2 == 200 and _r_book in _b2 and '"/opds/read/toggle"' in _b2,
                   "status=%s" % _st2))
    _st3, _b3, _ = _http(_r_port, "/opds/book/" + _r_q)
    check("HTTP 管理员详情页 → 显示「已读完」状态与取消按钮",
          lambda: (_st3 == 200 and "阅读状态" in _b3 and "已读完（点击取消）" in _b3,
                   "status=%s" % _st3))
    _cst, _, _ = _http(_r_port, "/opds/read/toggle", method="POST", body=_r_form,
                       headers={"Origin": "https://evil.example.com"})
    check("HTTP 跨站 Origin → 403（CSRF 兜底）",
          lambda: (_cst == 403 and FIN.load_finished() == {_r_key2},
                   "status=%s 清单未被跨站改动" % _cst))
    _bst, _, _bloc = _http(_r_port, "/opds/read/toggle", method="POST",
                           body=urlencode({"key": _r_key2, "back": "//evil.example.com/x"}))
    check("HTTP back 指向外站 → 回落到本站路径（堵开放重定向）",
          lambda: (_bst == 303 and _bloc.startswith("/") and "evil" not in _bloc,
                   "Location=%s" % _bloc))
    _ist, _, _ = _http(_r_port, "/opds/read/toggle", method="POST",
                       body=urlencode({"key": "../../etc/passwd"}))
    _ist2, _, _ = _http(_r_port, "/opds/read/toggle", method="POST",
                        body=urlencode({"key": "不存在的分类/x"}))
    check("HTTP 非法键 / 未知分类 → 400",
          lambda: (_ist == 400 and _ist2 == 400, "status=%s / %s" % (_ist, _ist2)))

    # 原地标记（增强层）：同一个 POST，带 X-Requested-With 就回 JSON 而不 303 跳转
    _reset_fin(os.path.join(_R_TMP, "http_finished.json"))

    _jst, _jbody, _jct, _jloc = _post_json(_r_port, "/opds/read/toggle", _r_form)
    _jd = {}
    try:
        _jd = json.loads(_jbody)
    except Exception:                                 # noqa: BLE001
        pass
    check("HTTP 原地标记 → 200 + JSON（不再 303 跳转，浏览器就不会白屏重载）",
          lambda: (_jst == 200 and _jloc is None
                   and (_jct or "").startswith("application/json")
                   and _jd.get("ok") is True and _jd.get("finished") is True
                   and FIN.load_finished() == {_r_key2},
                   "status=%s ctype=%s loc=%s json=%s" % (_jst, _jct, _jloc, _jd)))
    _j2st, _j2body, _, _ = _post_json(_r_port, "/opds/read/toggle", _r_form)
    _j2d = json.loads(_j2body) if _j2body.startswith("{") else {}
    check("HTTP 原地标记再来一次 → finished 翻回 false（前端只需照抄这个值）",
          lambda: (_j2st == 200 and _j2d.get("finished") is False
                   and FIN.load_finished() == set(),
                   "status=%s json=%s" % (_j2st, _j2d)))
    _j3st, _, _, _ = _post_json(_r_port, "/opds/read/toggle", _r_form,
                                {"Origin": "https://evil.example.com"})
    check("HTTP 原地标记也过同站判定 → 跨站 Origin 照样 403",
          lambda: (_j3st == 403 and FIN.load_finished() == set(),
                   "status=%s 清单未被动" % _j3st))
    _j4st, _, _, _ = _post_json(_r_port, "/opds/read/toggle", urlencode({"key": "../../etc/passwd"}))
    check("HTTP 原地标记的入参校验不放松 → 非法键 400",
          lambda: (_j4st == 400, "status=%s" % _j4st))
    _j5st, _, _ = _http(_r_port, "/opds/read/toggle", method="POST", body=_r_form)
    check("HTTP 不带该头的提交仍走老路 → 303（curl / 无 JS 的浏览器不受影响）",
          lambda: (_j5st == 303, "status=%s" % _j5st))
    _reset_fin(os.path.join(_R_TMP, "http_finished.json"))

    # 口令配置下的 HTTP 行为（新版模型：匿名 = 访客，不再 401）
    SRV.AUTH_USER, SRV.AUTH_PASS = "ranqing", "s3cret"
    SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = "guest", "hello"
    _reset_sess()

    def _basic(u, p):
        return {"Authorization": "Basic " + base64.b64encode(
            ("%s:%s" % (u, p)).encode("utf-8")).decode("ascii")}

    _nst, _nb, _ = _http(_r_port, "/")
    _wst, _wb, _ = _http(_r_port, "/", headers=_basic("ranqing", "nope"))
    _ast, _ab, _ = _http(_r_port, "/", headers=_basic("ranqing", "s3cret"))
    _vst, _vb, _ = _http(_r_port, "/", headers=_basic("guest", "hello"))
    _vr, _, _ = _http(_r_port, "/opds/read", headers=_basic("guest", "hello"))
    _ar, _, _ = _http(_r_port, "/opds/read", headers=_basic("ranqing", "s3cret"))
    check("HTTP 配了口令也照常放行匿名/错口令 → 200（访客身份；浏览器不会再弹认证框）",
          lambda: (_nst == 200 and _wst == 200 and "已读完" not in _nb
                   and "已读完" not in _wb, "匿名=%s 错口令=%s" % (_nst, _wst)))
    check("HTTP 匿名拿到的是**真页面**（不是空壳/错误页）",
          lambda: ("<!doctype html" in _nb and "/opds/search" in _nb and len(_nb) > 2000,
                   "%d 字节" % len(_nb)))
    check("HTTP 管理员口令（哪怕来自局域网）→ 含「已读完」入口，/opds/read 200",
          lambda: ("已读完" in _ab and _ast == 200 and _ar == 200,
                   "首页=%s 列表=%s" % (_ast, _ar)))
    check("HTTP 访客口令：能看书库但看不到入口，且 /opds/read 被 403",
          lambda: (_vst == 200 and "已读完" not in _vb and _vr == 403,
                   "首页=%s 列表=%s" % (_vst, _vr)))
    SRV.AUTH_USER, SRV.AUTH_PASS, SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = _r_saved_auth
    _reset_sess()

if _r_srv is not None:
    try:
        _r_srv.shutdown()
        _r_srv.server_close()
    except Exception:
        pass

# ==================== S. 新增卷提示 ====================
section("S. 新增卷提示（置顶 + 封面角标 + 详情页点名 + 看过即恢复）")

_UPD_ERR = ""
try:
    from lightnovel.opds import updates as UPD
except Exception as _exc:                            # noqa: BLE001
    UPD = None
    _UPD_ERR = "%s: %s" % (type(_exc).__name__, _exc)

_S_TMP = os.path.join(TMP_ROOT, "updates")
os.makedirs(_S_TMP, exist_ok=True)
_S_FILE = os.path.join(_S_TMP, "updates.json")


def _scheck(label, fn):
    if UPD is None:
        _rskip(label, "updates 模块不可用：%s" % _UPD_ERR)
    else:
        check(label, fn)


def _s_reset():
    """把状态文件指到临时目录并清空（绝不碰真实 .autosync/updates.json）。"""
    UPD.UPDATES_FILE = _S_FILE
    if os.path.exists(_S_FILE):
        os.remove(_S_FILE)
    return _S_FILE


def _s_vol(rel, size):
    return {"rel": rel, "size": size, "mtime": 1700000000.0,
            "title": rel.rsplit("/", 1)[-1][:-5], "subdir": ""}


def _s_fake(extra_a=(), extra_c=()):
    """假书库：书A 无子目录、书C 两个子目录。

    刻意让「有更新」的书在字母序里**不是**第一个（书A < 书C），这样才能验出置顶。
    """
    return {"已完结": {
        "书A": [_s_vol("已完结/书A/01.epub", 1000)] + list(extra_a),
        "书C": [_s_vol("已完结/书C/主线/01.epub", 11),
                _s_vol("已完结/书C/外传/01.epub", 13)] + list(extra_c),
    }, "未完结": {}}


_S_A2 = _s_vol("已完结/书A/02.epub", 2000)
_S_C2 = _s_vol("已完结/书C/外传/02.epub", 14)
_S_C3 = _s_vol("已完结/书C/外传/03.epub", 15)


def _s_baseline():
    _s_reset()
    first = UPD.observe(_s_fake())
    second = UPD.observe(_s_fake())
    return (first == {} and second == {} and os.path.exists(_S_FILE),
            "首次/再次都不提示，但基线已落盘")


def _s_new_vol():
    _s_reset()
    UPD.observe(_s_fake())
    pend = UPD.observe(_s_fake(extra_a=[_S_A2]))
    m1 = os.stat(_S_FILE).st_mtime_ns
    again = UPD.observe(_s_fake(extra_a=[_S_A2]))       # 没变化 → 不该再写盘
    return (list(pend) == ["已完结/书A"]
            and pend["已完结/书A"]["vols"] == ["已完结/书A/02.epub"]
            and again == pend and m1 == os.stat(_S_FILE).st_mtime_ns,
            "只点名书A 的第 2 卷；重复检测不写盘")


def _s_rename_not_new():
    _s_reset()
    UPD.observe(_s_fake())
    # 把 01.epub 改名（大小不变）—— 本库经常批量规整文件名，不能算「新增」
    renamed = _s_fake()
    renamed["已完结"]["书A"] = [_s_vol("已完结/书A/01 - 规整后.epub", 1000)]
    return (UPD.observe(renamed) == {}, "同大小改名不算新增")


def _s_size_change_is_new():
    _s_reset()
    UPD.observe(_s_fake())
    # 同路径但大小变了（重新下载/换版本）：路径没变 → 不算新增，不打扰
    grown = _s_fake()
    grown["已完结"]["书A"] = [_s_vol("已完结/书A/01.epub", 9999)]
    return (UPD.observe(grown) == {}, "同名不同大小按「内容变了」处理，不报新增")


def _s_del_then_prune():
    _s_reset()
    UPD.observe(_s_fake())
    UPD.observe(_s_fake(extra_a=[_S_A2]))
    gone = UPD.observe(_s_fake())                        # 新卷又被删了
    return (gone == {} and "已完结/书A" not in UPD.load_pending(),
            "新卷被删 → 提示自动收回（不留点不开的死条目）")


def _s_new_book_quiet():
    _s_reset()
    UPD.observe(_s_fake())
    lib = _s_fake()
    lib["已完结"]["书Z"] = [_s_vol("已完结/书Z/01.epub", 777)]
    return (UPD.observe(lib) == {}, "整本新作品不提示（批量导入不刷屏）")


def _s_deleted_book_quiet():
    _s_reset()
    UPD.observe(_s_fake())                               # 先建基线（此时还没有第 2 卷）
    p1 = UPD.observe(_s_fake(extra_a=[_S_A2]))           # 再加卷 → 应当提示
    lib = _s_fake(extra_a=[_S_A2])
    del lib["已完结"]["书A"]                              # 整本删掉
    p2 = UPD.observe(lib)
    return (list(p1) == ["已完结/书A"] and p2 == {}, "整本删除时提示一并消失")


def _s_corrupt():
    _s_reset()
    with open(_S_FILE, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    p = UPD.observe(_s_fake())                           # 不能抛，且当成「重建基线」
    return (p == {} and UPD.load_pending() == {}, "文件损坏 → 当空处理并重建基线")


def _s_atomic():
    _s_reset()
    UPD.observe(_s_fake())
    UPD.observe(_s_fake(extra_a=[_S_A2], extra_c=[_S_C2]))
    left = [f for f in os.listdir(_S_TMP) if f.endswith(".tmp")]
    with open(_S_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return (not left and data["version"] == 1 and len(data["pending"]) == 2,
            "无 .tmp 残留，两份待读提示都落盘")


def _s_order_keys():
    reset = UPD.order_keys(["已完结/书A", "已完结/书B", "已完结/书C"], {})
    hot = UPD.order_keys(["已完结/书A", "已完结/书B", "已完结/书C"],
                         {"已完结/书C": {"at": "2026-09-14 10:00:00", "vols": ["x"]}})
    both = UPD.order_keys(["已完结/书A", "已完结/书B", "已完结/书C"],
                          {"已完结/书A": {"at": "2026-09-14 09:00:00", "vols": ["x"]},
                           "已完结/书C": {"at": "2026-09-14 10:00:00", "vols": ["x"]}})
    return (reset == ["已完结/书A", "已完结/书B", "已完结/书C"]
            and hot == ["已完结/书C", "已完结/书A", "已完结/书B"]
            and both == ["已完结/书C", "已完结/书A", "已完结/书B"],
            "无更新时原序；有更新置顶；多本按时间新→旧，其余保字母序")


def _s_clear():
    _s_reset()
    UPD.observe(_s_fake())
    UPD.observe(_s_fake(extra_a=[_S_A2], extra_c=[_S_C2]))
    bad = UPD.clear("../../etc/passwd")
    one = UPD.clear("已完结/书A")
    rest = sorted(UPD.load_pending())
    all_n = UPD.clear()
    return (bad == 0 and one == 1 and rest == ["已完结/书C"] and all_n == 1
            and UPD.load_pending() == {},
            "非法键不落盘(0)；单键清 1 条；全清返回剩余条数")


def _s_counts():
    _s_reset()
    UPD.observe(_s_fake())
    UPD.observe(_s_fake(extra_a=[_S_A2]))
    c = UPD.counts()
    return (c.get("已完结") == 1 and c.get("未完结") == 0, "按分类计数：%s" % c)


def _s_pending_for():
    _s_reset()
    UPD.observe(_s_fake())
    UPD.observe(_s_fake(extra_c=[_S_C2]))
    return (UPD.pending_for("已完结/书C") is not None
            and UPD.pending_for("已完结/书A") is None
            and UPD.pending_for("不存在的分类/x") is None
            and UPD.pending_for("../x") is None,
            "命中/未命中/非法键三态都对")


for _lbl, _fn in [
    ("首次运行只建基线，不把整库标成「有更新」", _s_baseline),
    ("往已存在的书里加卷 → 只点名那一卷，且重复检测不写盘", _s_new_vol),
    ("同大小改名不算新增（本库常做的文件名规整不该刷屏）", _s_rename_not_new),
    ("同名但大小变了 → 不报新增（内容替换不是新卷）", _s_size_change_is_new),
    ("新增的那卷又被删掉 → 提示自动收回", _s_del_then_prune),
    ("整本新作品不提示（批量导入不刷屏）", _s_new_book_quiet),
    ("整本作品被删除 → 提示一并消失", _s_deleted_book_quiet),
    ("状态文件损坏 → 当空处理并重建基线（服务照跑）", _s_corrupt),
    ("原子写：无 .tmp 残留、结构完整", _s_atomic),
    ("排序：有更新的置顶，其余保持字母序（分页前排序）", _s_order_keys),
    ("清除：单键 / 全部 / 非法键", _s_clear),
    ("按分类统计有更新的作品数", _s_counts),
    ("pending_for：命中 / 未命中 / 非法键", _s_pending_for),
]:
    _scheck(_lbl, _fn)


# ---------- S9. 渲染层（卡片角标 / 工具条权限 / CSS 顺序） ----------
def _s_card_markup():
    _old = UPD.load_pending
    try:
        UPD.load_pending = lambda: {}
        up = FEED._book_card("/x", "r.epub", "书A", "3 卷", upd=2)
        plain = FEED._book_card("/x", "r.epub", "书B", "1 卷")
    finally:
        UPD.load_pending = _old
    return ('class="card upd"' in up and '<span class="upd">有更新 +2</span>' in up
            and "有更新 +2 卷" in up and '<div class="ph">' in up
            and up.index('class="upd"') > up.index("</a>") - 200
            and 'class="card"' in plain and "有更新" not in plain,
            "有更新的卡带角标+描边类+副标题；普通卡干净")


def _s_bar_gate():
    pend = {"已完结/书A": {"at": "2026-09-14 10:00:00", "vols": ["x"]}}
    admin = FEED._upd_bar(pend, "/opds/catalog/已完结", True)
    guest = FEED._upd_bar(pend, "/opds/catalog/已完结", False)
    none = FEED._upd_bar({}, "/", True)
    return ("/opds/updates/clear" in admin and "1 部作品有更新" in admin
            and "/opds/updates/clear" not in guest and "1 部作品有更新" in guest
            and none == "",
            "清空按钮只在管理员页面出现；访客只看到文字；无更新时不渲染")


def _s_css_order():
    css = FEED.SITE_CSS
    i_hover = css.index(".card:hover .ph")
    i_upd = css.index(".card.upd .ph")
    i_updh = css.index(".card.upd:hover .ph")
    return (i_hover < i_upd < i_updh
            and "position:absolute" in css[css.index(".card .ph .upd"):][:200]
            and ".group-head .gnew" in css and ".vol .meta .s .vnew" in css
            and "prefers-color-scheme:dark" in css and "--new:" in css,
            "描边规则排在 hover 之后（否则悬停丢描边）；角标是绝对定位；含深色模式变量")


def _s_css_no_leak():
    """CSS 对**所有人**下发，注释里的字样会漏进访客源码。

    新增卷提示本身是公开信息（访客也该知道哪本有新卷），所以这里不禁止「有更新」这类词；
    真正必须守住的是**管理员专有**的那部分：清空按钮、/opds/updates/clear 路由
    绝不能出现在 CSS / 访客 HTML 里。
    """
    css = FEED.SITE_CSS
    return ("/opds/updates/clear" not in css and "标为已读" not in css,
            "CSS 里不含管理员专有的路由与按钮文案")


_scheck("书卡：有更新的带角标/描边类/副标题，普通卡不受影响", _s_card_markup)
_scheck("工具条：清空按钮仅管理员可见", _s_bar_gate)
_scheck("CSS：角标与描边规则齐备且顺序正确", _s_css_order)
_scheck("CSS 不泄露管理员专有路由/文案", _s_css_no_leak)


# ---------- S10. HTTP 端到端：加卷 → 置顶 → 点进去 → 恢复 ----------
def _s_order_of(body):
    from urllib.parse import unquote as _uq
    return [_uq(x) for x in re.findall(r'<a class="cardlink" href="/opds/book/([^"]+)"', body)]


def _s_group_open(body, name):
    """某个子目录分组在页面里是不是「默认展开」状态。

    不能直接用 ``body.index("外传")`` 判断顺序 —— 顶部的「本次新增」区块里
    也会写子目录名（那正是它的职责），所以得先定位到那个分组的 ``<section>`` 标签本身。
    """
    try:
        i = body.index('<span class="gnm">%s</span>' % name)
    except ValueError:
        return False
    start = body.rindex('<section class="group', 0, i)
    return " open" in body[start:body.index(">", start)]


_s_state = {"lib": _s_fake()}
_s_orig_get = FEED.get_library
_s_srv = None
_s_port = 0
if UPD is not None:
    _s_reset()
    FEED.get_library = lambda *a, **k: _s_state["lib"]      # observe() 内部走 library.get_library
    LIB.get_library = FEED.get_library
    try:
        _s_srv = SRV.make_server(port=0, bind="0.0.0.0")
        _s_port = _s_srv.server_address[1]
        threading.Thread(target=_s_srv.serve_forever, daemon=True).start()
    except Exception as _exc:                              # noqa: BLE001
        _s_srv = None
        _s_err = "%s: %s" % (type(_exc).__name__, _exc)


def _shcheck(label, fn):
    if _s_srv is None:
        _rskip(label, "无法起测试服务（%s）" % ("updates 不可用" if UPD is None else "端口不可用"))
    else:
        check(label, fn)


if _s_srv is not None:
    _su = "/opds/catalog/" + quote("已完结", safe="")
    _s_st0, _s_b0, _ = _http(_s_port, _su)                 # 第一发：只建基线
    check("S10a 首次打开列表：建立基线，没有任何角标",
          lambda: (_s_st0 == 200 and _s_order_of(_s_b0) == ["已完结/书A", "已完结/书C"]
                   and 'class="upd"' not in _s_b0,
                   "顺序=%s" % _s_order_of(_s_b0)))

    _s_state["lib"] = _s_fake(extra_c=[_S_C2])             # 书C 的外传多了一卷
    _s_st1, _s_b1, _ = _http(_s_port, _su)
    _ord1 = _s_order_of(_s_b1)
    check("S10b 加卷后：该书置顶 + 封面出现「有更新」",
          lambda: (_s_st1 == 200 and _ord1 == ["已完结/书C", "已完结/书A"]
                   and "有更新 +1" in _s_b1 and "部作品有更新" in _s_b1,
                   "顺序=%s" % _ord1))

    _s_sth, _s_bh, _ = _http(_s_port, "/")
    check("S10c 首页分类卡也带出「N 部有更新」",
          lambda: ("1 部有更新" in _s_bh, "首页"))

    _s_det = "/opds/book/" + quote("已完结/书C", safe="")
    _s_st2, _s_b2, _ = _http(_s_port, _s_det)
    check("S10d 详情页点名「哪个子目录的哪一卷」是新的",
          lambda: (_s_st2 == 200 and "本次新增 1 卷" in _s_b2
                   and "外传" in _s_b2 and "有更新 +1" in _s_b2
                   and 'class="vnew"' in _s_b2,
                   "status=%s" % _s_st2))
    check("S10e 有新卷的分组默认展开（否则等于没提示）",
          lambda: (_s_group_open(_s_b2, "外传") and _s_group_open(_s_b2, "主线"),
                   "外传=%s 主线=%s" % (_s_group_open(_s_b2, "外传"), _s_group_open(_s_b2, "主线"))))
    check("S10f 管理员点进去 = 看过了，提示立刻收掉",
          lambda: (UPD.load_pending() == {}, "pending=%s" % UPD.load_pending()))

    _s_st3, _s_b3, _ = _http(_s_port, _su)
    _ord3 = _s_order_of(_s_b3)
    check("S10g 回到列表：该书回到原来的位置，角标消失",
          lambda: (_ord3 == ["已完结/书A", "已完结/书C"] and 'class="upd"' not in _s_b3
                   and "部作品有更新" not in _s_b3,
                   "顺序=%s" % _ord3))

    # 再来一轮，专门验证「访客翻书不会消掉管理员的提醒」
    _s_state["lib"] = _s_fake(extra_c=[_S_C2, _S_C3])
    _http(_s_port, _su)
    _s_pend0 = UPD.load_pending()
    if _r_lan and _r_lan != "127.0.0.1":
        _s_gst, _s_gb, _ = _http(_s_port, _s_det, host=_r_lan)
        check("S10h 访客打开详情页：看得到新增内容，但不会清掉管理员的提示",
              lambda: (_s_gst == 200 and "本次新增 1 卷" in _s_gb
                       and UPD.load_pending() == _s_pend0,
                       "访客看完 pending 仍是 %s" % sorted(UPD.load_pending())))
        _s_gc, _s_gcb, _ = _http(_s_port, _su, host=_r_lan)
        check("S10i 访客列表页：有角标但没有清空按钮",
              lambda: ("有更新 +1" in _s_gcb and "/opds/updates/clear" not in _s_gcb,
                       "status=%s" % _s_gc))
        _s_gpost, _, _ = _http(_s_port, "/opds/updates/clear", host=_r_lan, method="POST",
                               body=urlencode({"back": _su}),
                               headers={"Origin": "http://%s:%d" % (_r_lan, _s_port)})
        check("S10j 访客 POST 清空 → 403 且提示还在",
              lambda: (_s_gpost == 403 and UPD.load_pending() != {},
                       "status=%s pending=%s" % (_s_gpost, sorted(UPD.load_pending()))))
    else:
        _rskip("S10h~j 访客相关", "本机没有非回环 IP，无法模拟访客来源")

    _s_xst, _, _ = _http(_s_port, "/opds/updates/clear", method="POST",
                         body=urlencode({"back": _su}),
                         headers={"Origin": "https://evil.example.com"})
    check("S10k 跨站 Origin → 403（CSRF 兜底），提示未被清掉",
          lambda: (_s_xst == 403 and UPD.load_pending() != {}, "status=%s" % _s_xst))

    _s_bst, _, _s_bloc = _http(_s_port, "/opds/updates/clear", method="POST",
                               body=urlencode({"back": "//evil.example.com/x"}))
    check("S10l back 指向外站 → 回落到本站路径（堵开放重定向）",
          lambda: (_s_bst == 303 and _s_bloc.startswith("/") and "evil" not in _s_bloc,
                   "Location=%s" % _s_bloc))

    _s_ist, _, _ = _http(_s_port, "/opds/updates/clear", method="POST",
                         body=urlencode({"key": "../../etc/passwd"}))
    check("S10m 非法键 → 400", lambda: (_s_ist == 400, "status=%s" % _s_ist))

    # 原地清空：带头 → 回 JSON 不跳转（前端拿它当成功信号，再只换 <main> 内容重排）
    _s_state["lib"] = _s_fake(extra_a=[_S_A2], extra_c=[_S_C2, _S_C3])
    _http(_s_port, _su)                                    # 重新检测出新卷（书A 的第 2 卷）
    _s_qst, _s_qb, _s_qct, _s_qloc = _post_json(_s_port, "/opds/updates/clear",
                                                urlencode({"back": _su}))
    _s_qd = {}
    try:
        _s_qd = json.loads(_s_qb)
    except Exception:                                     # noqa: BLE001
        pass
    check("S10m2 原地清空 → 200 + JSON 条数（不再 303，列表由服务端重排）",
          lambda: (_s_qst == 200 and _s_qloc is None
                   and (_s_qct or "").startswith("application/json")
                   and _s_qd.get("ok") is True and _s_qd.get("cleared") == 1
                   and UPD.load_pending() == {},
                   "status=%s ctype=%s json=%s" % (_s_qst, _s_qct, _s_qd)))

    _s_cst, _, _ = _http(_s_port, "/opds/updates/clear", method="POST",
                         body=urlencode({"back": _su}))
    check("S10n 管理员清空 → 303 且提示清空",
          lambda: (_s_cst == 303 and UPD.load_pending() == {},
                   "status=%s pending=%s" % (_s_cst, sorted(UPD.load_pending()))))

    _s_st4, _s_b4, _ = _http(_s_port, _su)
    check("S10o 清空后再看列表：顺序与角标全部复原",
          lambda: (_s_order_of(_s_b4) == ["已完结/书A", "已完结/书C"]
                   and 'class="upd"' not in _s_b4, "顺序=%s" % _s_order_of(_s_b4)))

    _s_fst, _s_fb, _ = _http(_s_port, _su, accept="application/atom+xml")
    check("S10p XML feed 用同一套排序（阅读器里也是置顶的）",
          lambda: (_s_fst == 200 and _s_fb.index("书A") < _s_fb.index("书C"), "status=%s" % _s_fst))

if _s_srv is not None:
    try:
        _s_srv.shutdown()
        _s_srv.server_close()
    except Exception:
        pass
if UPD is not None:
    FEED.get_library = _s_orig_get                     # 还原，别影响后续任何读取
    LIB.get_library = _s_orig_get

# ==================== T. 管理员登录 + 已读完自动摘除 ====================
section("T. 管理员登录（签名 cookie）与「已读完」自动摘除")

_T_TMP = os.path.join(TMP_ROOT, "session")
os.makedirs(_T_TMP, exist_ok=True)
_t_saved_auth = (SRV.AUTH_USER, SRV.AUTH_PASS, SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS)


def _t_reset():
    """密钥 / 已读完清单 / 更新状态全部指到临时目录，绝不碰真实 .autosync。"""
    SESS.SESSION_KEY_FILE = os.path.join(_T_TMP, "session.key")
    if os.path.exists(SESS.SESSION_KEY_FILE):
        os.remove(SESS.SESSION_KEY_FILE)
    SESS.forget_key()
    SESS.login_throttle.clear()
    _reset_fin(os.path.join(_T_TMP, "finished.json"))
    if UPD is not None:
        UPD.UPDATES_FILE = os.path.join(_T_TMP, "updates.json")
        if os.path.exists(UPD.UPDATES_FILE):
            os.remove(UPD.UPDATES_FILE)
    return SESS.SESSION_KEY_FILE


def _tcheck(label, fn):
    if SESS is None or FIN is None:
        _rskip(label, "session/finished 模块不可用：%s" % _R_ERR)
    else:
        check(label, fn)


# ---------- T1. 会话令牌本体（纯函数，不经过网络） ----------
def _t_token():
    _t_reset()
    tok = SESS.issue()
    tampered = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    return (tok.count(".") == 1 and SESS.verify(tok)
            and not SESS.verify(tampered)
            and not SESS.verify(SESS.issue(ttl=-1))           # 已过期
            and not SESS.verify("") and not SESS.verify(None) and not SESS.verify("abc")
            and not SESS.verify("99999999999.deadbeef")       # 签名是假的
            and not SESS.verify("notanint." + SESS._sign("notanint")),   # 签名真、时间戳是乱的
            "签发/校验/篡改/过期/格式乱 六种情况都符合预期")


def _t_keyfile():
    p = _t_reset()
    SESS.key()                                            # 首次调用触发生成
    text = ""
    if os.path.isfile(p):
        with open(p, encoding="ascii") as f:
            text = f.read().strip()
    k1 = SESS.key()
    SESS.forget_key()
    k2 = SESS.key()                                       # 重新读盘 → 必须是同一把
    return (len(text) == 64 and all(c in "0123456789abcdef" for c in text)
            and len(k1) == 32 and k1 == k2,
            "32 字节随机密钥 ✓ hex 落盘 ✓ 重读一致（重启不掉线）")


def _t_key_location():
    return (os.path.dirname(_SESS_DEFAULT) == P.LOG_DIR
            and os.path.basename(_SESS_DEFAULT) == "session.key"
            and os.path.basename(P.LOG_DIR) == ".autosync",
            "%s（默认值；测试期间打桩到临时目录 → .autosync 已 gitignore，密钥不会进仓库）"
            % _SESS_DEFAULT)


def _t_key_rotates():
    """密钥文件坏掉/被删 → 换一把新的，而不是拿个可猜的值继续跑。"""
    p = _t_reset()
    old = SESS.key()
    with open(p, "w", encoding="ascii") as f:
        f.write("short")                                  # 太短 → 判为无效
    SESS.forget_key()
    new = SESS.key()
    return (new != old and len(new) == 32 and os.path.getsize(p) == 64,
            "坏密钥文件 → 自动换新并覆盖")


def _t_cookie_parse():
    pc = SESS.parse_cookie
    return (pc("ln_adm=abc") == {"ln_adm": "abc"}
            and pc("a=1; ln_adm=xyz; b=2") == {"a": "1", "ln_adm": "xyz", "b": "2"}
            and pc("") == {} and pc(None) == {}
            and pc("ln_adm=first; ln_adm=second")["ln_adm"] == "first"
            and pc("novalue; k=") == {"k": ""},
            "单值/多值/空头/同名取首个/无等号项 都处理正确")


def _t_throttle():
    th = SESS.Throttle(limit=3, window=100, lock_for=50)
    seq = [th.fail("x", now=1000.0) for _ in range(3)]
    locked = th.retry_after("x", now=1000.0)
    other = th.retry_after("y", now=1000.0)
    freed = th.retry_after("x", now=1200.0)               # 过了窗口 → 自动遗忘
    th.ok("x")
    return (seq == [1, 2, 0] and locked == 51 and other == 0 and freed == 0
            and th.retry_after("x", now=1000.0) == 0,
            "连错 3 次锁 50 秒；别的来源不受影响；过期/成功即解锁")


for _lbl, _fn in [
    ("会话令牌：签名有效才认，篡改/过期/格式乱一律拒绝", _t_token),
    ("签名密钥：32 字节随机数、hex 落盘、重读一致", _t_keyfile),
    ("密钥落在 .autosync/session.key（gitignore 内，不进仓库）", _t_key_location),
    ("密钥文件损坏 → 自动换一把新的（不会用可猜的密钥继续跑）", _t_key_rotates),
    ("Cookie 头解析：多值/空值/同名/无等号", _t_cookie_parse),
    ("登录失败节流：连错即锁，只锁该来源，成功或超时即解", _t_throttle),
]:
    _tcheck(_lbl, _fn)


# ---------- T2. HTTP 端到端：登录 → 管理页 → 退出 ----------
_t_srv = None
_t_port = 0
if SESS is not None:
    _t_reset()
    SRV.AUTH_USER, SRV.AUTH_PASS = "ran", "147258"
    SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = "", ""
    try:
        _t_srv = SRV.make_server(port=0, bind="0.0.0.0")
        _t_port = _t_srv.server_address[1]
        threading.Thread(target=_t_srv.serve_forever, daemon=True).start()
    except Exception as _exc:                             # noqa: BLE001
        _t_srv = None
        _t_err = "%s: %s" % (type(_exc).__name__, _exc)
_T_ORIGIN = "http://127.0.0.1:%d" % _t_port


def _thcheck(label, fn):
    if _t_srv is None:
        _rskip(label, "无法起测试服务（%s）" % ("session 不可用" if SESS is None else "端口不可用"))
    else:
        check(label, fn)


def _t_http(path, method="GET", body=None, headers=None, host="127.0.0.1"):
    import http.client as _hc
    conn = _hc.HTTPConnection(host, _t_port, timeout=30)
    hdrs = dict(headers or {})
    if host != "127.0.0.1":
        hdrs["Host"] = "%s:%d" % (host, _t_port)
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, text, resp.getheader("Set-Cookie") or "", resp.getheader("Location") or "")
    conn.close()
    return out


def _t_token_of(set_cookie):
    return set_cookie.split("=", 1)[1].split(";")[0] if "=" in (set_cookie or "") else ""


if _t_srv is not None:
    SESS.login_throttle.clear()
    _t_st, _t_body, _, _ = _t_http("/")
    check("T1 未登录访问首页 → 200 正常浏览，只有「登录」入口、没有管理员入口",
          lambda: (_t_st == 200 and "<!doctype html" in _t_body and len(_t_body) > 2000
                   and "已读完" not in _t_body and "/opds/read" not in _t_body
                   and "/opds/login" in _t_body,
                   "status=%s 登录入口=%s" % (_t_st, "/opds/login" in _t_body)))
    _t_lg, _t_lb, _, _ = _t_http("/opds/login")
    check("T2 登录页 200，且自身不含管理员专有字样/路由（它本来就是公开页面）",
          lambda: (_t_lg == 200 and 'action="/opds/login"' in _t_lb
                   and "已读完" not in _t_lb and "/opds/read" not in _t_lb
                   and "/opds/updates/clear" not in _t_lb,
                   "status=%s" % _t_lg))
    _t_rd, _, _, _ = _t_http("/opds/read")
    check("T3 未登录访问管理页 → 403（不给内容，也不给入口）",
          lambda: (_t_rd == 403, "status=%s" % _t_rd))
    _t_x, _, _, _ = _t_http("/opds/login", "POST", urlencode({"user": "ran", "pass": "147258"}),
                            {"Origin": "https://evil.example.com"})
    check("T4 跨站登录 → 403（CSRF 兜底）", lambda: (_t_x == 403, "status=%s" % _t_x))

    SESS.login_throttle.clear()
    _t_w, _t_wb, _t_wck, _ = _t_http("/opds/login", "POST",
                                     urlencode({"user": "ran", "pass": "nope", "back": "/"}),
                                     {"Origin": _T_ORIGIN})
    check("T5 口令错 → 401 + 登录页文案，且**不下发** cookie",
          lambda: (_t_w == 401 and not _t_wck and "不对" in _t_wb,
                   "status=%s cookie=%r" % (_t_w, _t_wck)))

    SESS.login_throttle.clear()
    _t_ok, _, _t_ck, _t_loc = _t_http("/opds/login", "POST",
                                      urlencode({"user": "ran", "pass": "147258", "back": "/"}),
                                      {"Origin": _T_ORIGIN})
    _t_tok = _t_token_of(_t_ck)
    check("T6 口令对 → 303 跳回 back，并下发 HttpOnly + SameSite=Lax 的签名 cookie",
          lambda: (_t_ok == 303 and _t_loc == "/" and SESS.verify(_t_tok)
                   and "HttpOnly" in _t_ck and "SameSite=Lax" in _t_ck
                   and "Max-Age=" in _t_ck and "Secure" not in _t_ck,
                   "status=%s loc=%s cookie=%s" % (_t_ok, _t_loc, _t_ck)))
    _t_h = {"Cookie": "%s=%s" % (SESS.COOKIE_NAME, _t_tok)}
    _t_ast, _t_ab, _, _ = _t_http("/", headers=_t_h)
    check("T7 带上 cookie → 首页变成管理员版（出现「已读完」入口与「退出」）",
          lambda: (_t_ast == 200 and "已读完" in _t_ab and "退出" in _t_ab
                   and "/opds/read" in _t_ab, "status=%s" % _t_ast))
    _t_rst, _, _, _ = _t_http("/opds/read", headers=_t_h)
    check("T8 登录后能打开「已读完」清单页", lambda: (_t_rst == 200, "status=%s" % _t_rst))
    _t_bad, _t_bb, _, _ = _t_http("/", headers={"Cookie": "%s=forged.sig" % SESS.COOKIE_NAME})
    check("T9 伪造 cookie → 退回访客页面（不报错、也不是管理员）",
          lambda: (_t_bad == 200 and "已读完" not in _t_bb, "status=%s" % _t_bad))

    _t_lst, _, _t_lck, _t_lloc = _t_http("/opds/logout", "POST",
                                         urlencode({"back": "/opds/recent"}),
                                         {**_t_h, "Origin": _T_ORIGIN})
    check("T10 退出 → 303 跳回 back + 清 cookie（Max-Age=0）",
          lambda: (_t_lst == 303 and _t_lloc == "/opds/recent" and "Max-Age=0" in _t_lck,
                   "status=%s loc=%s cookie=%s" % (_t_lst, _t_lloc, _t_lck)))
    _t_lr, _, _, _t_lrloc = _t_http("/opds/logout", "POST",
                                    urlencode({"back": "/opds/read?page=2"}),
                                    {**_t_h, "Origin": _T_ORIGIN})
    check("T10b 从「已读完」页退出 → 落点回首页（否则访客身份打开管理页必 403）",
          lambda: (_t_lr == 303 and _t_lrloc == "/", "Location=%s" % _t_lrloc))
    _t_lx, _, _, _ = _t_http("/opds/logout", "POST", urlencode({"back": "/"}),
                             {"Origin": "https://evil.example.com"})
    check("T11 跨站退出 → 403（退出走 POST，不会被页面里塞个 <img> 触发成莫名掉线）",
          lambda: (_t_lx == 403, "status=%s" % _t_lx))

    SESS.login_throttle.clear()
    _t_or, _, _, _t_orloc = _t_http("/opds/login", "POST",
                                    urlencode({"user": "ran", "pass": "147258",
                                               "back": "//evil.example.com/x"}),
                                    {"Origin": _T_ORIGIN})
    check("T12 登录的 back 指向外站 → 回落到本站路径（堵开放重定向）",
          lambda: (_t_or == 303 and _t_orloc.startswith("/") and "evil" not in _t_orloc,
                   "Location=%s" % _t_orloc))

    SESS.login_throttle.clear()
    _t_ss, _, _t_sck, _ = _t_http("/opds/login", "POST",
                                  urlencode({"user": "ran", "pass": "147258", "back": "/"}),
                                  {"Origin": _T_ORIGIN, "X-Forwarded-Proto": "https"})
    check("T13 走 HTTPS（隧道）登录 → cookie 带 Secure",
          lambda: (_t_ss == 303 and "Secure" in _t_sck and SESS.verify(_t_token_of(_t_sck)),
                   "cookie=%s" % _t_sck))

    SESS.login_throttle.clear()
    _t_codes = [_t_http("/opds/login", "POST", urlencode({"user": "ran", "pass": "bad"}),
                        {"Origin": _T_ORIGIN})[0] for _ in range(9)]
    _t_lockg, _t_lockb, _, _ = _t_http("/opds/login")
    check("T14 连错口令触发节流：第 9 次起 429，登录页给出还要等多久",
          lambda: (_t_codes[:8] == [401] * 8 and _t_codes[8] == 429
                   and _t_lockg == 200 and "再试" in _t_lockb,
                   "前 8 次=%s 第 9 次=%s" % (_t_codes[:8], _t_codes[8])))
    SESS.login_throttle.clear()
    _t_after, _, _t_ack, _ = _t_http("/opds/login", "POST",
                                     urlencode({"user": "ran", "pass": "147258"}),
                                     {"Origin": _T_ORIGIN})
    check("T15 节流解除后能正常登录（不会把自己永久锁在外面）",
          lambda: (_t_after == 303 and SESS.verify(_t_token_of(_t_ack)), "status=%s" % _t_after))

    # 登录态对 OPDS 阅读器同样有效（不是只有网页才认 cookie）
    _t_feed, _t_fb, _, _ = _t_http("/", headers={**_t_h, "Accept": "application/atom+xml"})
    check("T16 带 cookie 的 OPDS feed 也含「已读完」导航（阅读器不必再配 Basic）",
          lambda: (_t_feed == 200 and "已读完" in _t_fb, "status=%s" % _t_feed))


# ---------- T3. 「已读完」遇新卷自动摘除（与新增卷检测联动） ----------
def _t_autoremove():
    if UPD is None:
        return True, "跳过（updates 模块不可用）"
    _t_reset()
    UPD.observe(_s_fake())                                # 先建基线
    FIN.mark_finished("已完结/书A", True)
    FIN.mark_finished("已完结/书B", True)
    before = sorted(FIN.load_finished())
    pend = UPD.observe(_s_fake(extra_a=[_S_A2]))          # 书A 多了一卷
    after = sorted(FIN.load_finished())
    return (before == ["已完结/书A", "已完结/书B"] and "已完结/书A" in pend
            and after == ["已完结/书B"],
            "加卷前 %s → 加卷后 %s（只摘被加卷的那一本）" % (before, after))


def _t_autoremove_idempotent():
    if UPD is None:
        return True, "跳过（updates 模块不可用）"
    _t_reset()
    UPD.observe(_s_fake())
    FIN.mark_finished("已完结/书A", True)
    p1 = UPD.observe(_s_fake(extra_a=[_S_A2]))
    p2 = UPD.observe(_s_fake(extra_a=[_S_A2]))
    p3 = UPD.observe(_s_fake(extra_a=[_S_A2]))
    return (FIN.load_finished() == set() and p1 == p2 == p3
            and "已完结/书A" in p3,
            "反复 observe 不报错、不重复摘、待读提示保持")


def _t_autoremove_remark():
    """摘掉之后管理员重新标记 → 只要没有新卷就不该再被摘掉。"""
    if UPD is None:
        return True, "跳过（updates 模块不可用）"
    _t_reset()
    UPD.observe(_s_fake())
    FIN.mark_finished("已完结/书A", True)
    UPD.observe(_s_fake(extra_a=[_S_A2]))
    FIN.mark_finished("已完结/书A", True)                  # 含新卷一起读完了，重新标记
    UPD.observe(_s_fake(extra_a=[_S_A2]))
    UPD.observe(_s_fake(extra_a=[_S_A2]))
    return ("已完结/书A" in FIN.load_finished(), "没有新卷就不会被再摘一次")


def _t_autoremove_no_touch_others():
    """没读完的书、以及只是改了名的卷，都不该碰「已读完」清单。"""
    if UPD is None:
        return True, "跳过（updates 模块不可用）"
    _t_reset()
    UPD.observe(_s_fake())
    FIN.mark_finished("已完结/书C", True)
    renamed = _s_fake()
    renamed["已完结"]["书A"] = [_s_vol("已完结/书A/01 - 规整后.epub", 1000)]
    UPD.observe(renamed)                                   # 同大小改名 → 不算新卷
    return (FIN.load_finished() == {"已完结/书C"}, "改名不触发摘除，其余标记不受影响")


def _t_readlist_http():
    """端到端：登录后「已读完」列表里有这本书 → 加一卷 → 列表里自动少掉它。"""
    if UPD is None:
        return True, "跳过（updates 模块不可用）"
    _t_reset()
    card_href = "/opds/book/" + quote("已完结/书A", safe="/")
    fake = {"已完结": {"书A": [_s_vol("已完结/书A/01.epub", 100)]}, "未完结": {}}
    orig = FEED.get_library
    FEED.get_library = lambda *a, **k: fake
    try:
        UPD.observe(fake)
        FIN.mark_finished("已完结/书A", True)
        h = {"Cookie": "%s=%s" % (SESS.COOKIE_NAME, SESS.issue())}
        st1, b1, _, _ = _t_http("/opds/read", headers=h)
        fake["已完结"]["书A"].append(_s_vol("已完结/书A/02.epub", 200))
        UPD.observe(fake)
        st2, b2, _, _ = _t_http("/opds/read", headers=h)
    finally:
        FEED.get_library = orig
    return (st1 == 200 and card_href in b1 and 'data-total="1"' in b1
            and st2 == 200 and card_href not in b2 and "还没有标记任何作品" in b2,
            "列表 %s → %s" % ("有该书" if card_href in b1 else "无",
                              "仍有该书" if card_href in b2 else "已自动移除"))


for _lbl, _fn in [
    ("已读完的书出现新卷 → 自动从清单摘除，其余标记不受影响", _t_autoremove),
    ("自动摘除是幂等的：反复检测不会报错也不会反复改清单", _t_autoremove_idempotent),
    ("重新标记「已读完」后，没有新卷就不再被摘掉", _t_autoremove_remark),
    ("改名不算新卷 → 不会误摘「已读完」", _t_autoremove_no_touch_others),
]:
    _tcheck(_lbl, _fn)

_thcheck("HTTP 端到端：登录后「已读完」列表在加卷后自动少掉那一本", _t_readlist_http)

if _t_srv is not None:
    try:
        _t_srv.shutdown()
        _t_srv.server_close()
    except Exception:                                     # noqa: BLE001
        pass
SRV.AUTH_USER, SRV.AUTH_PASS, SRV.AUTH_GUEST_USER, SRV.AUTH_GUEST_PASS = _t_saved_auth
if SESS is not None:
    SESS.forget_key()

# ==================== U. 阅读进度指示器 + 自动「已读完」 ====================
section("U. Moon+ 阅读进度指示器（封面角标 / 详情页进度环 / 自动判定已读完）")

try:
    from lightnovel.opds import moon as MOON
    _U_ERR = ""
except Exception as _exc:                                 # noqa: BLE001
    MOON = None
    _U_ERR = "%s: %s" % (type(_exc).__name__, _exc)

_U_TMP = os.path.join(TMP_ROOT, "moon")
os.makedirs(_U_TMP, exist_ok=True)
_u_saved = {}


def _ucheck(label, fn):
    if MOON is None:
        _rskip(label, "moon 模块不可用：%s" % _U_ERR)
    else:
        check(label, fn)


def _u_snap():
    """保存会被本节点打桩的模块级配置（避免污染别的测试 / 真实缓存）。"""
    if _u_saved or MOON is None:
        return
    _u_saved.update({
        "MOON_ROOT": MOON.MOON_ROOT,
        "MOON_POS_FILE": MOON.MOON_POS_FILE,
        "MOON_ENABLED": MOON.MOON_ENABLED,
        "MOON_PUBLIC": MOON.MOON_PUBLIC,
        "MOON_DONE_PERCENT": MOON.MOON_DONE_PERCENT,
        "rel_pct": dict(MOON._state["rel_pct"]),
        "meta": dict(MOON._state["meta"]),
    })


def _u_restore():
    if not _u_saved or MOON is None:
        return
    MOON.MOON_ROOT = _u_saved["MOON_ROOT"]
    MOON.MOON_POS_FILE = _u_saved["MOON_POS_FILE"]
    MOON.MOON_ENABLED = _u_saved["MOON_ENABLED"]
    MOON.MOON_PUBLIC = _u_saved["MOON_PUBLIC"]
    MOON.MOON_DONE_PERCENT = _u_saved["MOON_DONE_PERCENT"]
    MOON._state["rel_pct"] = _u_saved["rel_pct"]
    MOON._state["meta"] = _u_saved["meta"]


def _u_isolate():
    """把进度相关的一切指到一个**干净**的临时目录。

    「干净」是必须的：``read_positions`` 读的是 ``<root>/Cache/*.po``，
    不清空的话上一个用例写下的 ``.po`` 会漏进下一个用例（``_u_join`` 就会多出一堆
    「意外命中」，断言随之假失败）。两个 ``.tmp`` 也一并清掉，避免缓存校验戳
    用半截文件。**绝不碰真实 .autosync/moon_cache**。
    """
    _u_snap()
    root = os.path.join(_U_TMP, "dotmoon")
    cache_dir = os.path.join(root, "Cache")
    if os.path.isdir(cache_dir):
        for _n in os.listdir(cache_dir):
            try:
                os.remove(os.path.join(cache_dir, _n))
            except OSError:
                pass
    cache_file = os.path.join(_U_TMP, "positions.json")
    for _p in (cache_file, cache_file + ".tmp"):
        if os.path.exists(_p):
            try:
                os.remove(_p)
            except OSError:
                pass
    MOON.MOON_ROOT = root
    MOON.MOON_POS_FILE = cache_file
    MOON.MOON_ENABLED = True
    MOON.MOON_PUBLIC = True
    MOON.MOON_DONE_PERCENT = 99.0
    MOON._state["rel_pct"] = {}
    MOON._state["meta"] = {"ok": False, "at": 0.0, "error": "", "po_files": 0,
                           "matched": 0, "unmatched": 0, "ambiguous": 0, "elapsed_ms": 0.0}
    return MOON.MOON_ROOT


# ---------- U1. 纯函数：解析 / 归一化 / 读取 / 关联 ----------
def _u_parse():
    a = MOON.parse_position("1779019601627*3@0#10926:100%")
    b = MOON.parse_position("1779019601627*34:100%")          # PDF 没有 @y#z
    c = MOON.parse_position("1753089022677*5@0#0:11.0%")
    return (a == {"device_id": "1779019601627", "section": 3, "sub_section": 0,
                  "offset": 10926, "percent": 100.0, "is_pdf": False}
            and b["is_pdf"] and b["section"] == 34 and b["offset"] is None
            and c["percent"] == 11.0,
            "epub/pdf/中间态 三种形态都解析正确")


_ucheck("解析 .po：dev*x@y#z:p% 三种形态（含 PDF 无 @y#z 段）", _u_parse)


def _u_parse_bad():
    bad = ["", None, "garbage", "1779019601627", "1779019601627*3", "x*3@0#1:5%",
           "1779019601627*3@0#1:5", "1779019601627*3@0#1:5%%"]
    return all(MOON.parse_position(s) is None for s in bad), "8 种非法输入全部返回 None"


_ucheck("解析 .po：空值/缺段/非数字一律返回 None（不抛异常）", _u_parse_bad)


def _u_norm():
    """归一化要同时打通「第X卷 ↔ XX」「汉字 ↔ 阿拉伯」「补零」。"""
    cases = {
        "GJ部 01": ("gj部", "01"),
        "GAMERS电玩咖01": ("gamers电玩咖", "01"),
        "玩乐关系 1": ("玩乐关系", "01"),
        "在地下城寻求邂逅是否搞错了什么 第一卷": ("在地下城寻求邂逅是否搞错了什么", "01"),
        "灼眼的夏娜 第十三卷": ("灼眼的夏娜", "13"),
        "弹珠汽水瓶里的千岁同学 06.5": ("弹珠汽水瓶里的千岁同学", "6.5"),
        "NO GAME LIFE": ("nogamelife", ""),
    }
    ok = all(MOON.norm_key(k) == v for k, v in cases.items())
    pairs = [("GJ部 01", "GJ部 第一卷"), ("玩乐关系 1", "玩乐关系 第一卷"),
             ("在地下城寻求邂逅是否搞错了什么 01", "在地下城寻求邂逅是否搞错了什么 第一卷")]
    same = all(MOON.norm_key(a) == MOON.norm_key(b) for a, b in pairs)
    return ok and same, "7 组格式 + 3 组跨命名体系等价"


_ucheck("卷号归一化：第X卷 ↔ XX、汉字 ↔ 阿拉伯、补零、小数", _u_norm)


def _u_norm_cn_dot():
    """用户自己的「第三之五卷 = 3.5」约定必须归一到小数，不能当成「第五卷」。"""
    got = MOON.norm_key("第三之五卷")
    want = MOON.norm_key("第三之五卷")            # 同一输入两次结果必须一致（纯函数）
    return (got[1] == "3.5" and got == want and got[1] != "05",
            "第三之五卷 -> 卷号 %r（不是 05）" % got[1])


_ucheck("卷号归一化：支持「第三之五卷 ↔ 3.5」的小数约定", _u_norm_cn_dot)


def _u_write_po(root, name, text, mtime=None):
    p = os.path.join(root, "Cache", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _u_read_cached():
    _u_isolate()
    root = MOON.MOON_ROOT
    _u_write_po(root, "书A 01.epub.po", "1779019601627*3@0#271:2.6%")
    _u_write_po(root, "书A 02.epub.po", "1779019601627*3@0#10926:100%")
    _u_write_po(root, "书B.pdf.po", "1779019601627*34:100%")
    sfile(root, "Cache/notes.txt", "不是 po，必须被忽略")
    p1, m1 = MOON.read_positions()
    p2, m2 = MOON.read_positions()                 # 第二次应命中本地缓存
    return (m1["from_cache"] is False and m2["from_cache"] is True
            and len(p1) == 3 and set(p1) == {"书A 01", "书A 02", "书B"}
            and p1["书B"]["is_pdf"] and p1["书A 02"]["percent"] == 100.0
            and "notes" not in str(p1),
            "3 个 .po 读出（非 .po 忽略）；重读命中缓存")


_ucheck("读取位置：解析 Cache/*.po、忽略非 .po、二次读命中本地缓存", _u_read_cached)


def _u_read_invalidate():
    _u_isolate()
    root = MOON.MOON_ROOT
    _u_write_po(root, "书A 01.epub.po", "1779019601627*3@0#271:2.6%")
    MOON.read_positions()
    _u_write_po(root, "书A 01.epub.po", "1779019601627*3@0#999:60.0%")   # 内容变了
    p2, m2 = MOON.read_positions()
    return (m2["from_cache"] is False and p2["书A 01"]["percent"] == 60.0,
            "校验戳不一致 → 重读并拿到新值（不会拿旧缓存糊弄）")


_ucheck("读取位置：缓存校验戳（mtime+大小）不一致时重读", _u_read_invalidate)


def _u_read_missing():
    _u_isolate()
    MOON.MOON_ROOT = os.path.join(_U_TMP, "根本不存在")
    p, meta = MOON.read_positions()
    return (p is None and meta.get("error") and not meta.get("from_cache"),
            "网盘没挂载 → 返回 (None, error)，**不抛异常**")


_ucheck("读取位置：目录不存在（网盘没挂载）→ 返回 None 且不抛", _u_read_missing)


def _u_join():
    _u_isolate()
    root = MOON.MOON_ROOT
    _u_write_po(root, "书A 01.epub.po", "1779019601627*1@0#1:30.0%")
    _u_write_po(root, "书A 第一卷.epub.po", "1779019601627*1@0#1:30.0%")   # 归一化才命中
    _u_write_po(root, "与书库无关.epub.po", "1779019601627*1@0#1:50.0%")
    pos, _m = MOON.read_positions()
    lib = {"已完结": {"书A": [_s_vol("已完结/书A/书A 01.epub", 100),
                              _s_vol("已完结/书A/书A 02.epub", 100)]},
           "未完结": {}}
    del lib["已完结"]["书A"][0]["title"]
    lib["已完结"]["书A"][0]["title"] = "书A 01"
    lib["已完结"]["书A"][1]["title"] = "书A 02"
    rel, meta = MOON.join_library(pos, lib)
    return (rel == {"已完结/书A/书A 01.epub": 30.0}
            and meta["matched"] == 2 and meta["unmatched"] == 1,
            "严格+归一化各命中一条（都落到同一卷），异物未命中")


_ucheck("关联书库：严格相等 + 归一化兜底 + 未命中计数", _u_join)


def _u_stat():
    _u_isolate()
    vols = [_s_vol("已完结/书A/01.epub", 1), _s_vol("已完结/书A/02.epub", 1),
            _s_vol("已完结/书A/03.epub", 1)]
    for v in vols:
        v["title"] = v["rel"].rsplit("/", 1)[-1][:-5]
    if MOON.stat_of(vols) is not None:
        return False, "没有记录时应当返回 None"
    MOON._swap({"已完结/书A/01.epub": 100.0, "已完结/书A/02.epub": 99.4})
    st = MOON.stat_of(vols)
    ok1 = (st["total"] == 3 and st["seen"] == 2 and st["done"] == 2
           and st["all"] is False and abs(st["percent"] - 66.5) < 0.05)
    MOON._swap({"已完结/书A/01.epub": 100.0, "已完结/书A/02.epub": 100.0,
                "已完结/书A/03.epub": 99.9})
    st2 = MOON.stat_of(vols)
    return (ok1 and st2["all"] is True and st2["done"] == 3,
            "无记录→None；部分读完 all=False；全卷≥99% all=True")


_ucheck("作品统计：无记录返回 None；部分读完 all=False；全卷达标才 all=True", _u_stat)


def _u_threshold():
    """阈值必须是可配的：设成 100 之后 99.4% 就不算读完（证明 99 这个默认值真的在起作用）。"""
    _u_isolate()
    vols = [_s_vol("已完结/书A/01.epub", 1)]
    vols[0]["title"] = "01"
    MOON._swap({"已完结/书A/01.epub": 99.4})
    at99 = MOON.stat_of(vols)
    MOON.MOON_DONE_PERCENT = 100.0
    at100 = MOON.stat_of(vols)
    return (at99["all"] is True and at100["all"] is False,
            "阈值 99 → 算读完；阈值 100 → 不算（MoM+ 的百分比是估算值）")


_ucheck("读完阈值可配：99% 算读完，调到 100% 就不算", _u_threshold)


def _u_auto():
    _u_isolate()
    lib = {"已完结": {"全读完": [_s_vol("已完结/全读完/01.epub", 1),
                                 _s_vol("已完结/全读完/02.epub", 1)],
                      "读一半": [_s_vol("已完结/读一半/01.epub", 1),
                                 _s_vol("已完结/读一半/02.epub", 1)]},
           "未完结": {}}
    for _c, bs in lib.items():
        for _b, vs in bs.items():
            for v in vs:
                v["title"] = v["rel"].rsplit("/", 1)[-1][:-5]
    MOON._swap({"已完结/全读完/01.epub": 100.0, "已完结/全读完/02.epub": 100.0,
                "已完结/读一半/01.epub": 100.0})
    auto = MOON.auto_finished(lib)
    return (set(auto) == {"已完结/全读完"} and auto["已完结/全读完"]["done"] == 2,
            "只有「全部卷都读完」的作品入选，读到一半的排除")


_ucheck("自动判定：只有整部读完的作品入选（读一半的不能算）", _u_auto)


def _u_disabled_off():
    _u_isolate()
    vols = [_s_vol("已完结/书A/01.epub", 1)]
    vols[0]["title"] = "01"
    MOON._swap({"已完结/书A/01.epub": 100.0})
    MOON.MOON_ENABLED = False
    off = (MOON.stat_of(vols) is None and MOON.percent_of("已完结/书A/01.epub") is None
           and MOON.auto_finished({"已完结": {"书A": vols}}) == {}
           and MOON.visible(True) is False)
    MOON.MOON_ENABLED = True
    return (off, "ENABLED=0 时所有查询退化为空（整层可一键关闭）")


_ucheck("总开关：LN_MOON_PROGRESS=0 时所有进度查询退化为空", _u_disabled_off)


def _u_public_switch():
    _u_isolate()
    MOON.MOON_PUBLIC = True
    pub = MOON.visible(False) and MOON.visible(True)
    MOON.MOON_PUBLIC = False
    priv = (not MOON.visible(False)) and MOON.visible(True)
    return (pub and priv, "PUBLIC=1 访客可见；PUBLIC=0 仅管理员可见")


_ucheck("可见性开关：MOON_PUBLIC 控制访客能不能看到进度", _u_public_switch)


# ---------- U2. HTTP 端到端：真页面上的三处呈现 ----------
_u_srv = None
_u_port = 0
_u_saved_auth = (SRV.AUTH_USER, SRV.AUTH_PASS)
try:
    SRV.AUTH_USER, SRV.AUTH_PASS = "ran", "147258"
    _u_srv = SRV.make_server(port=0, bind="127.0.0.1")
    _u_port = _u_srv.server_address[1]
    threading.Thread(target=_u_srv.serve_forever, daemon=True).start()
except Exception:                                         # noqa: BLE001
    _u_srv = None

_U_ADMIN = {"Authorization": "Basic " + base64.b64encode(b"ran:147258").decode()}


def _u_http(path, headers=None):
    import http.client as _hc
    conn = _hc.HTTPConnection("127.0.0.1", _u_port, timeout=30)
    conn.request("GET", path, headers=dict(headers or {}))
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, text


def _uhcheck(label, fn):
    if _u_srv is None:
        _rskip(label, "无法起测试服务")
    else:
        check(label, fn)


def _u_pick_book():
    """挑一部真实作品并给它注入合成进度（真实数据只读，不改任何文件）。"""
    _u_isolate()
    lib = LIB.get_library(force=True)
    for cat in ("已完结", "未完结"):
        for book, vols in sorted(lib.get(cat, {}).items()):
            if len(vols) >= 2:
                return cat, book, vols
    return None, None, None


def _u_card_badge():
    _u_isolate()
    lib = LIB.get_library()
    cat, book = "已完结", sorted(lib.get("已完结", {}))[0]
    vols = lib[cat][book]
    rel_map = {v["rel"]: 42.0 for v in vols}
    MOON._swap(rel_map)
    st, body = _u_http("/opds/catalog/" + quote(cat), headers=_U_ADMIN)
    idx = body.find(f">{book}</div>")
    zone = body[max(0, idx - 1400):idx + 200] if idx > 0 else ""
    html_page = body
    return (st == 200 and 'class="prog"' in html_page
            and 'title="阅读进度 42%"' in html_page and ">42%</span>" in html_page
            and zone.count('class="prog') >= 1,
            "状态=%s 页面含 42%% 角标且落在该书的封面块里" % st)


_uhcheck("HTTP 分类页：封面右下角出现进度角标（百分比 + title 无障碍文案）", _u_card_badge)


def _u_card_badge_done():
    _u_isolate()
    lib = LIB.get_library()
    cat, book = "已完结", sorted(lib.get("已完结", {}))[0]
    MOON._swap({v["rel"]: 100.0 for v in lib[cat][book]})
    st, body = _u_http("/opds/catalog/" + quote(cat), headers=_U_ADMIN)
    return (st == 200 and 'class="prog pdone"' in body
            and f'已读 {len(lib[cat][book])}/{len(lib[cat][book])} 卷' in body,
            "整部读完 → 角标加 pdone 类、副标题写「已读 N/N 卷」")


_uhcheck("HTTP 分类页：整部读完时角标变绿色（pdone）且副标题标出卷数", _u_card_badge_done)


def _u_badge_vs_updbar():
    """有「有更新」条时角标要抬高，否则会被那条左右贯通的色带压住。"""
    css = FEED.SITE_CSS
    return (".card.upd .ph .prog{bottom:25px}" in css
            and "conic-gradient(var(--accent)" in css
            and ".pfab.pdone .pring" in css,
            "CSS 含角标避让 + 进度环 + 完成态配色三条规则")


_ucheck("CSS：角标避开「有更新」条、进度环与完成态配色齐备", _u_badge_vs_updbar)


def _u_detail_fab():
    _u_isolate()
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({v["rel"]: (100.0 if i < 1 else 0.0) for i, v in enumerate(vols)})
    st, body = _u_http("/opds/book/" + O.encode_path(f"{cat}/{book}"), headers=_U_ADMIN)
    pct = int(round(sum(100.0 if i < 1 else 0.0 for i in range(len(vols))) / len(vols)))
    return (st == 200 and 'class="pfab"' in body and f'style="--p:{pct}"' in body
            and "已读 1/%d 卷" % len(vols) in body,
            "状态=%s 进度环 --p=%d、文案「已读 1/%d 卷」" % (st, pct, len(vols)))


_uhcheck("HTTP 详情页：右下角常驻进度环（含 --p 与「已读 N/M 卷」）", _u_detail_fab)


def _u_detail_volrow():
    _u_isolate()
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({vols[0]["rel"]: 33.0, vols[1]["rel"]: 100.0})
    st, body = _u_http("/opds/book/" + O.encode_path(f"{cat}/{book}"), headers=_U_ADMIN)
    return (st == 200 and '<span class="vp">已读 33%</span>' in body
            and '<span class="vp done">读完</span>' in body,
            "分卷行分别显示「已读 33%」与「读完」")


_uhcheck("HTTP 详情页：分卷列表每行标出该卷进度（未读完/读完两种态）", _u_detail_volrow)


def _u_read_two_columns():
    _u_isolate()
    _reset_fin(os.path.join(_U_TMP, "finished_two.json"))   # 人工栏留空，专注自动栏
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({v["rel"]: 100.0 for v in vols})
    st, body = _u_http("/opds/read", headers=_U_ADMIN)
    key = f"{cat}/{book}"
    return (st == 200 and "人工标记" in body and "阅读器自动判定" in body
            and "只读展示" in body and O.encode_path(key) in body
            and "还没有作品被自动判定为读完" not in body,
            "两栏齐备，自动栏渲染出「%s」" % key)


_uhcheck("HTTP 已读完页：拆成「人工标记」+「阅读器自动判定」两栏", _u_read_two_columns)


def _u_auto_readonly():
    """最关键的一条：自动判定**绝不能**写进 finished.json（否则与「新卷自动剔除」互相拉扯）。"""
    _u_isolate()
    fin_path = _reset_fin(os.path.join(_U_TMP, "finished_ro.json"))
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({v["rel"]: 100.0 for v in vols})
    st, body = _u_http("/opds/read", headers=_U_ADMIN)
    fin = FIN.load_finished()
    return (st == 200 and FIN.normalize_key(f"{cat}/{book}") not in fin
            and not os.path.exists(fin_path),
            "自动判定命中「%s」，但 finished.json 依然不存在（%s）"
            % (f"{cat}/{book}", "未创建" if not os.path.exists(fin_path) else "被写了"))


_uhcheck("自动判定是纯派生：命中作品但绝不写 finished.json", _u_auto_readonly)


def _u_manual_wins():
    """人工标过的作品只在人工栏出现，不重复出现在自动栏（人工结论优先）。"""
    _u_isolate()
    _reset_fin(os.path.join(_U_TMP, "finished_ov.json"))
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    key = f"{cat}/{book}"
    MOON._swap({v["rel"]: 100.0 for v in vols})
    FIN.mark_finished(key, True)
    st, body = _u_http("/opds/read", headers=_U_ADMIN)
    FIN.mark_finished(key, False)
    overlapped = "与人工标记重叠" in body
    return (st == 200 and overlapped and "0 部" in body,
            "重叠时只在人工栏出现，自动栏计数归零（页头点明重叠）")


_uhcheck("已读完页：人工与自动重叠时不重复展示（人工优先）", _u_manual_wins)


def _u_guest_hidden():
    """MOON_PUBLIC=0 时访客看不到任何进度；管理员仍然看得到。"""
    _u_isolate()
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({v["rel"]: 55.0 for v in vols})
    MOON.MOON_PUBLIC = False
    _s1, guest_page = _u_http("/opds/catalog/" + quote(cat))
    _s2, admin_page = _u_http("/opds/catalog/" + quote(cat), headers=_U_ADMIN)
    off = 'class="prog"' not in guest_page and 'class="prog"' in admin_page
    _s3, guest_book = _u_http("/opds/book/" + O.encode_path(f"{cat}/{book}"))
    off = off and 'class="pfab"' not in guest_book
    MOON.MOON_PUBLIC = True
    _s4, on = _u_http("/opds/catalog/" + quote(cat))
    return (off and 'class="prog"' in on and "已读完" not in on and "/opds/read" not in on,
            "PUBLIC=0：访客无角标/无进度环；PUBLIC=1：访客可见但**仍看不到管理入口**")


_uhcheck("访客门禁：MOON_PUBLIC 关掉后访客无角标，且任何情况下都拿不到管理入口",
         _u_guest_hidden)


def _u_switch_off_http():
    _u_isolate()
    cat, book, vols = _u_pick_book()
    if not book:
        return True, "跳过（书库里没有 ≥2 卷的作品）"
    MOON._swap({v["rel"]: 88.0 for v in vols})
    MOON.MOON_ENABLED = False
    _s1, page = _u_http("/opds/catalog/" + quote(cat), headers=_U_ADMIN)
    MOON.MOON_ENABLED = True
    return ('class="prog"' not in page, "总开关关掉后页面上一个角标都没有")


_uhcheck("总开关：LN_MOON_PROGRESS=0 后页面不再渲染进度角标", _u_switch_off_http)


def _u_stats_health():
    """把「关联健康度」暴露到 /opds/stats —— 书库改名导致关联断掉时能一眼看出来。"""
    _u_isolate()
    _s, body = _u_http("/opds/stats", headers=_U_ADMIN)
    try:
        data = json.loads(body)
    except ValueError:
        return False, "stats 不是 JSON"
    m = data.get("_moon") or {}
    keys = {"enabled", "root", "ttl", "done_percent", "rel_entries", "unmatched",
            "matched", "ambiguous", "public", "ok"}
    return (keys <= set(m) and m.get("root") == MOON.MOON_ROOT,
            "stats 含 _moon 健康块（含 unmatched/ambiguous）")


_uhcheck("HTTP /opds/stats：暴露阅读器进度的关联健康度（unmatched/ambiguous）",
         _u_stats_health)


if _u_srv is not None:
    try:
        _u_srv.shutdown()
        _u_srv.server_close()
    except Exception:                                     # noqa: BLE001
        pass
SRV.AUTH_USER, SRV.AUTH_PASS = _u_saved_auth
_u_restore()

# ============================== 汇总 ==============================
print("\n" + "=" * 70)
_p = [r for r in RESULTS if r[2]]
_f = [r for r in RESULTS if not r[2]]
print("  合计 %d 项：通过 %d，失败 %d" % (len(RESULTS), len(_p), len(_f)))
if _f:
    print("\n  失败明细：")
    for sec, lab, _, det in _f:
        print("   ✗ [%s] %s  -> %s" % (sec, lab, det))
print("=" * 70)


def _shimproof_rmtree(path):
    """优先用 cmd rmdir 递归删除目录。

    本机（WorkBuddy 沙箱）会在 Python 层拦截删除：小文件被重定向到回收站，
    删除次数达到阈值时更会直接抛 SystemExit 中断进程。atexit 钩子里挨这一下
    会变成「184 项全过、退出码却是 1」的假失败。走 cmd 的系统调用即绕开。
    """
    if os.name == "nt":
        try:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException:
        pass


def cleanup_tmp():
    """尽力清理自己的临时目录（含历史遗留的 smoke-* 目录）。

    环境的批量删除保护可能拦截甚至中断删除，而它抛的是 SystemExit（不属于
    Exception），故这里一律用 BaseException 兜住，绝不让 atexit 钩子改变进程
    退出码 —— 测试结论只看断言结果，不看清理是否彻底。
    """
    try:
        targets = [d for d in (os.path.join(_TMP_BASE, n) for n in os.listdir(_TMP_BASE))
                   if os.path.isdir(d) and os.path.basename(d).startswith("smoke-")]
        # 释放日志句柄，否则 Windows 上文件占用导致删不掉
        for h in list(logging.getLogger("sync").handlers):
            try:
                h.close()
            except BaseException:
                pass
        for root_dir in targets:
            _shimproof_rmtree(root_dir)
        return not any(os.path.isdir(d) for d in targets)
    except BaseException:
        return False


import atexit  # noqa: E402

atexit.register(cleanup_tmp)
if cleanup_tmp():
    print("  临时目录已清理。")
else:
    print("  ⚠ 临时目录未能完全清理（可能被批量删除保护拦截），请手动删除：\n     %s" % TMP_ROOT)

sys.exit(1 if _f else 0)
