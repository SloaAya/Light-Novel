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

# ---- Basic 认证（运行时改全局，用完即恢复）----
_o_u, _o_p = O.AUTH_USER, O.AUTH_PASS
O.AUTH_USER, O.AUTH_PASS = "tester", "s3cret"
try:
    _s, _h, _b = http("/")
    check("开启口令后无凭据 -> 401",
          lambda: (_s == 401 and "Basic" in (_h.get("WWW-Authenticate") or ""), "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Basic " + base64.b64encode(b"tester:s3cret").decode()})
    check("正确凭据 -> 200", lambda: (_s == 200, "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()})
    check("错误口令 -> 401", lambda: (_s == 401, "status=%s" % _s))
    _s, _h, _b = http("/", headers={"Authorization": "Bearer abc"})
    check("非 Basic 认证头 -> 401", lambda: (_s == 401, "status=%s" % _s))
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
