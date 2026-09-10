#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Light-Novel OPDS 书源服务（手机端远程同步）
==========================================
把 D:\\Light-Novel\\轻小说 下的书库以标准 OPDS 1.2 目录的形式发布出去，
手机上的阅读器（静读天下 / Lithium / KyBook / Moon+ Reader 等）订阅这个书源后，
即可在 App 内直接浏览、检索并下载 epub 到书架。

目录层级（与磁盘结构一致）：
    /                            导航：已完结 / 未完结 / 最近更新 / 全部作品 / 搜索
    /opds/catalog/<分类>         该分类下所有「书名」（导航型 entry）
    /opds/book/<分类>/<书名>     该书名下所有卷（获取型 entry，可直接下载）
    /opds/recent                 最近更新的卷
    /opds/search?q=关键词        文件名检索
    /dl/<相对路径>               下载 epub（支持断点续传 Range）
    /cover/<相对路径>            封面图（从 epub 内部提取，带缓存）
    /opds/opensearch.xml         OpenSearch 描述（阅读器里出现搜索框）

关键设计：
  1. feed 内所有链接一律使用**相对 URL**。无论手机走局域网 IP、内网穿透域名
     还是 Tailscale 地址访问，链接都会自动跟随当前 Host，无需为每种网络单独生成。
  2. 认证只从**环境变量**读取（LN_OPDS_USER / LN_OPDS_PASS），绝不写进本文件——
     本文件会被同步进 GitHub 仓库。
  3. 下载 / 封面接口对路径做 realpath 白名单校验，杜绝 ../ 目录穿越。

用法：
    python opds_server.py                        # 局域网访问
    python opds_server.py --port 8080            # 指定端口
    python opds_server.py --tunnel cloudflared   # 顺带拉起公网隧道（需先安装 cloudflared）
    python opds_server.py --help-internet        # 查看外网接入方案说明
"""

import os
import re
import sys
import time
import html
import io
import json
import struct
import errno
import socket
import shutil
import base64
import zipfile
import hashlib
import logging
import argparse
import threading
import subprocess
import posixpath
from urllib.parse import quote, unquote, parse_qs, urlparse
from xml.sax.saxutils import escape, quoteattr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

# ============================ 配置区 ============================
TARGET_DIR      = r"D:\Light-Novel"
LIGHT_NOVEL_DIR = os.path.join(TARGET_DIR, "轻小说")
CATEGORY_DONE    = "已完结"
CATEGORY_ONGOING = "未完结"
CATEGORY_DIRS = {
    CATEGORY_DONE:    os.path.join(LIGHT_NOVEL_DIR, CATEGORY_DONE),
    CATEGORY_ONGOING: os.path.join(LIGHT_NOVEL_DIR, CATEGORY_ONGOING),
}

LOG_DIR         = os.path.join(TARGET_DIR, ".autosync")
COVER_CACHE_DIR = os.path.join(LOG_DIR, "covers")

# ---- 公网隧道 ----
# 固定域名隧道（named tunnel）的名字与配置文件位置，需先跑 setup_named_tunnel.py
NAMED_TUNNEL_NAME = os.environ.get("LN_TUNNEL_NAME", "ln-opds")
CF_CONFIG = os.path.join(os.path.expanduser("~"), ".cloudflared", "config.yml")

PORT = int(os.environ.get("LN_OPDS_PORT", "8080"))
# 0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机（配合内网穿透时使用）
BIND = os.environ.get("LN_OPDS_BIND", "0.0.0.0")
# 口令：留空则免密。外网访问强烈建议设置（见文件头第 2 点）。
AUTH_USER = os.environ.get("LN_OPDS_USER", "")
AUTH_PASS = os.environ.get("LN_OPDS_PASS", "")

PAGE_SIZE   = 100   # 每个 feed 每页最多条目数
RECENT_SIZE = 100   # 「最近更新」条数
INDEX_TTL   = 30    # 目录索引缓存秒数
EPUB_EXTS   = (".epub",)
EXCLUDE_FILE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_DIRS   = {".git", ".autosync"}

SERVER_TITLE  = "Light Novel 书库"
SERVER_AUTHOR = "sync_lightnovel.py"
# ================================================================

log = logging.getLogger("sync")

OPDS_NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
OPDS_ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB_MIME     = "application/epub+zip"
BLANK_PNG     = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------- 路径工具 ----------------------------
def _safe_relpath(rel):
    """还原 URL 里的相对路径；含穿越嫌疑返回 None。"""
    if not rel:
        return None
    try:
        rel = unquote(rel)
    except Exception:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or rel == ".." or rel.startswith("../") or "/../" in rel or rel.endswith("/.."):
        return None
    return rel


def resolve_under(root, rel):
    """把 rel 解析到 root 之下，越界返回 None（防目录穿越）。"""
    rel = _safe_relpath(rel)
    if rel is None:
        return None
    target = os.path.realpath(os.path.join(root, rel.replace("/", os.sep)))
    root_rp = os.path.realpath(root)
    if target != root_rp and not target.startswith(root_rp + os.sep):
        return None
    return target


def encode_path(rel):
    return quote(rel.replace("\\", "/"), safe="/")


def strip_ext(name):
    return os.path.splitext(name)[0]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def local_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        if s:
            s.close()


# ---------------------------- 书库索引 ----------------------------
_index_lock = threading.Lock()
_index_cache = {"ts": 0.0, "data": None}


def _mk_vol(abspath, book_root):
    rel = os.path.relpath(abspath, LIGHT_NOVEL_DIR).replace(os.sep, "/")
    # 相对书名的子目录（如 "正篇/番外"），用于分组
    if book_root:
        rel_in_book = os.path.relpath(abspath, book_root).replace(os.sep, "/")
    else:
        rel_in_book = os.path.basename(abspath)
    subdir = os.path.dirname(rel_in_book) if "/" in rel_in_book else ""
    # 标题用纯文件名（去掉子目录前缀），避免出现 "正篇/xxx"
    title = strip_ext(os.path.basename(rel_in_book))
    try:
        st = os.stat(abspath)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {"title": title, "subdir": subdir, "rel": rel, "size": size, "mtime": mtime}


def _scan_library():
    """返回 {分类: {书名: [卷信息...]}}，卷的 rel 相对 LIGHT_NOVEL_DIR。"""
    lib = {}
    for cat, cdir in CATEGORY_DIRS.items():
        books = {}
        if os.path.isdir(cdir):
            try:
                top = sorted(os.listdir(cdir))
            except OSError:
                top = []
            for book in top:
                if book.lower() in EXCLUDE_FILE_NAMES or book.lower() in {d.lower() for d in SKIP_DIRS}:
                    continue
                bpath = os.path.join(cdir, book)
                vols = []
                if os.path.isfile(bpath):
                    if bpath.lower().endswith(EPUB_EXTS):
                        vols.append(_mk_vol(bpath, ""))
                else:
                    for r, dirs, files in os.walk(bpath):
                        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                        for f in files:
                            if f.lower() in EXCLUDE_FILE_NAMES:
                                continue
                            if not f.lower().endswith(EPUB_EXTS):
                                continue
                            vols.append(_mk_vol(os.path.join(r, f), bpath))
                if vols:
                    vols.sort(key=lambda v: v["rel"].lower())
                    books[book] = vols
        lib[cat] = books
    return lib


def get_library(force=False):
    now = time.time()
    with _index_lock:
        if not force and _index_cache["data"] is not None and now - _index_cache["ts"] < INDEX_TTL:
            return _index_cache["data"]
        data = _scan_library()
        _index_cache["data"] = data
        _index_cache["ts"] = now
        return data


def all_vols(lib=None):
    for cat, books in (lib or get_library()).items():
        for book, vols in books.items():
            for v in vols:
                yield cat, book, v


# ---------------------------- epub 封面提取 ----------------------------
_cover_lock = threading.Lock()
_cover_mem = {}


def _opf_cover_href(zf, opf_path):
    try:
        opf = zf.read(opf_path).decode("utf-8", "replace")
    except (KeyError, OSError):
        return None
    base = posixpath.dirname(opf_path)
    # 先建一张 item-id → (href, media-type) 索引，避免把 xhtml 包装页当封面
    item_map = {}
    for im in re.finditer(r"<item\b[^>]*>", opf, re.I):
        tag = im.group(0)
        idm = re.search(r'id=["\']([^"\']+)["\']', tag, re.I)
        if not idm:
            continue
        hm = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        mm = re.search(r'media-type=["\']([^"\']+)["\']', tag, re.I)
        item_map[idm.group(1)] = (hm.group(1) if hm else "", (mm.group(1) if mm else "").lower())

    def _is_image(href, media):
        if media.startswith("image/"):
            return True
        return bool(re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", href or "", re.I))

    def _resolve(href):
        return posixpath.normpath(posixpath.join(base, href)) if base else href

    # 1) <meta name="cover" content="X"> 必须指向图片
    m = (re.search(r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)["\']', opf, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']cover["\']', opf, re.I))
    if m and m.group(1) in item_map:
        href, media = item_map[m.group(1)]
        if _is_image(href, media):
            return _resolve(href)

    # 2) 按文档顺序找：id 含 "cover" 或 properties 含 "cover-image"，且**必须是图片**
    for iid, (href, media) in item_map.items():
        if not href or not _is_image(href, media):
            continue
        # 简单按 item 自身判断：id 里有 cover，或整条 <item> 标签里有 cover-image
        if "cover" in iid.lower():
            return _resolve(href)
    for iid, (href, media) in item_map.items():
        if not href or not _is_image(href, media):
            continue
        # 整段 item 标签里找 cover-image（properties 属性）
        pat = r'<item\b[^>]*\bid=["\']' + re.escape(iid) + r'["\'][^>]*>'
        for im in re.finditer(pat, opf, re.I):
            if "cover-image" in im.group(0).lower():
                return _resolve(href)
    return None


def _jpeg_size(data):
    """解析 JPEG SOF 标记，返回 (宽, 高)；非 JPEG 或失败返回 (None, None)。"""
    if not data or data[:2] != b"\xff\xd8":
        return None, None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xff:
            break
        marker = data[i + 1]
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                       0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            return w, h
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None, None


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None, None
    w = struct.unpack(">I", data[16:20])[0]
    h = struct.unpack(">I", data[20:24])[0]
    return w, h


def _img_size(data):
    """根据文件头判断图片尺寸（仅支持 JPEG / PNG；其它返回 (None,None)）。"""
    w, h = _jpeg_size(data)
    if w:
        return w, h
    return _png_size(data)


def _looks_like_spread(data):
    """封面判定：横宽比例 > 1.3 视为跨页/双联图，跳过找下一张。"""
    w, h = _img_size(data)
    if not w or not h:
        return False
    return (w / h) > 1.3


def _sniff_mime(blob):
    """根据文件头判断 mime。"""
    if not blob:
        return "image/jpeg"
    if blob[:2] == b"\xff\xd8":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if len(blob) >= 6 and blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def get_cover(rel):
    """提取 epub 封面 -> (bytes, mime)；无封面返回 (None, None)。内存 + 磁盘缓存。"""
    path = resolve_under(LIGHT_NOVEL_DIR, rel)
    if not path or not os.path.isfile(path):
        return None, None
    try:
        st = os.stat(path)
        key = hashlib.md5(f"{rel}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()
    except OSError:
        return None, None

    with _cover_lock:
        if key in _cover_mem:
            return _cover_mem[key]

    cache_file = os.path.join(COVER_CACHE_DIR, key[:2], key + ".img")
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, "rb") as f:
                blob = f.read()
            mime = _sniff_mime(blob)
            with _cover_lock:
                _cover_mem[key] = (blob, mime)
            return blob, mime
        except OSError:
            pass

    blob = mime = None
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = None
            try:
                container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
                m = re.search(r'full-path=["\']([^"\']+)["\']', container)
                if m:
                    opf_path = m.group(1)
            except (KeyError, OSError):
                pass
            # 候选列表：1) OPF 明确指定的封面；2) 名字含 cover/封面 的图；3) 全部图片
            # 注意：epub 里的封面常用 .webp/.gif（不止 jpg/png），必须全部纳入
            all_imgs = [(n, zf.getinfo(n).file_size) for n in zf.namelist()
                        if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))]
            img_names = {n for n, _ in all_imgs}
            sizes = dict(all_imgs)
            cand = []
            trusted = False   # 来自 OPF 的「权威封面」标记
            if opf_path:
                h = _opf_cover_href(zf, opf_path)
                # OPF 返回的封面必须**真的是图片**（部分 EPUB 用 xhtml 包装页当 cover id）
                if h and h in img_names:
                    cand.append(h)
                    trusted = True
            cand += [n for n in img_names
                     if re.search(r"(cover|封面)", n, re.I) and n not in cand]
            # 兜底：取所有图中最大的那张（真封面通常远大于装饰图/注释图）
            if all_imgs:
                biggest = max(all_imgs, key=lambda x: x[1])
                if biggest[0] not in cand:
                    cand.append(biggest[0])
                # 「权威封面」若实在太小（< 5KB，基本是错文件）才让位；
                # 名义候选（仅靠文件名匹配 cover）则用 30% 大小阈值过滤装饰图
                first_size = sizes.get(cand[0], 0)
                if trusted and first_size < 5 * 1024 and sizes.get(biggest[0], 0) > first_size * 10:
                    cand.insert(0, biggest[0])
                elif not trusted and first_size < max(10 * 1024, biggest[1] * 0.3):
                    cand.insert(0, biggest[0])
            for name in cand:
                try:
                    data = zf.read(name)
                except (KeyError, OSError):
                    continue
                if len(data) < 200:  # 太小的图基本是缩略图/装饰
                    continue
                if data[:2] == b"\xff\xd8":
                    blob, mime = data, "image/jpeg"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    blob, mime = data, "image/png"
                elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    blob, mime = data, "image/webp"
                elif len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
                    blob, mime = data, "image/gif"
                else:
                    continue
                # 跨页/双联图过滤：宽高比 > 1.3 明显是横向 spread，跳过找下一张
                if _looks_like_spread(data):
                    blob = mime = None
                    continue
                break
    except (zipfile.BadZipFile, OSError):
        return None, None

    if blob:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            tmp = cache_file + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, cache_file)
        except OSError:
            pass
        with _cover_lock:
            _cover_mem[key] = (blob, mime)
    return blob, mime


# ---------------------------- epub 元数据 ----------------------------
_META_CACHE_FILE = os.path.join(LOG_DIR, "meta_cache.json")
_meta_lock = threading.Lock()
_meta_mem = {}
_meta_loaded = False


def _load_meta_disk():
    global _meta_loaded
    if _meta_loaded:
        return
    _meta_loaded = True
    try:
        with open(_META_CACHE_FILE, "r", encoding="utf-8") as f:
            _meta_mem.update(json.load(f))
    except (OSError, ValueError):
        pass


def _save_meta_disk():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = _META_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_meta_mem, f, ensure_ascii=False)
        os.replace(tmp, _META_CACHE_FILE)
    except OSError:
        pass


def _meta_key(rel, st):
    return f"{rel}|{st.st_size}|{int(st.st_mtime)}"


def get_epub_meta(rel):
    """提取 epub 的 dc:title / dc:creator / dc:description，返回 dict。
    带内存 + 磁盘缓存（按 路径+大小+mtime 做 key）。"""
    path = resolve_under(LIGHT_NOVEL_DIR, rel)
    if not path or not os.path.isfile(path):
        return {}
    try:
        st = os.stat(path)
        key = _meta_key(rel, st)
    except OSError:
        return {}
    with _meta_lock:
        _load_meta_disk()
        if key in _meta_mem:
            return _meta_mem[key]
    meta = {}
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = None
            try:
                container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
                m = re.search(r'full-path=["\']([^"\']+)["\']', container)
                if m:
                    opf_path = m.group(1)
            except (KeyError, OSError):
                pass
            if opf_path:
                opf = zf.read(opf_path).decode("utf-8", "replace")

                def tag(name):
                    m = re.search(r"<dc:" + name + r"[^>]*>(.*?)</dc:" + name + r">", opf, re.I | re.S)
                    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""

                t = tag("title")
                c = tag("creator")
                d = tag("description")
                meta = {
                    "title": t,
                    "creator": c,
                    "description": re.sub(r"\s+", " ", d)[:600],
                }
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    with _meta_lock:
        _meta_mem[key] = meta
        _save_meta_disk()
    return meta


# ---------------------------- zip 流式打包 ----------------------------
class _ZipSink:
    """把 HTTP 响应流包装成 zipfile 可写的目标（不可 seek，用 data descriptors）。
    zipfile 对 seekable()==False 的流会在每条 entry 后写 data descriptor，
    从而支持真正流式生成 zip，不需要临时文件、不占内存。"""

    def __init__(self, raw):
        self._raw = raw
        self._pos = 0

    def write(self, data):
        n = self._raw.write(data)
        self._pos += n if n is not None else len(data)
        return n if n is not None else len(data)

    def flush(self):
        try:
            self._raw.flush()
        except Exception:
            pass

    def tell(self):
        return self._pos

    def seekable(self):
        return False

    def seek(self, *_args, **_kwargs):  # 防御：zipfile 在不可 seek 流上不应调用
        raise io.UnsupportedOperation("not seekable")

    def readable(self):
        return False

    def writable(self):
        return True


# ---------------------------- Feed 生成 ----------------------------
def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _vol_updated(v):
    try:
        return datetime.fromtimestamp(v["mtime"]).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except (OSError, ValueError):
        return _now_iso()


def _feed(ident, title, entries, self_href, self_type=OPDS_NAV_TYPE, extra_links=""):
    """self_href 必须是可访问的相对 URL（OPDS 客户端会用它刷新/翻页）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:opds="http://opds-spec.org/2010/catalog"'
        ' xmlns:dc="http://purl.org/dc/terms/"'
        ' xmlns:thr="http://purl.org/syndication/thread/1.0">\n'
        f"  <id>{escape(ident)}</id>\n"
        f"  <title>{escape(title)}</title>\n"
        f"  <updated>{_now_iso()}</updated>\n"
        f"  <author><name>{escape(SERVER_AUTHOR)}</name></author>\n"
        f'  <link rel="start" href="/" type="{OPDS_NAV_TYPE}"/>\n'
        f'  <link rel="self" href={quoteattr(self_href)} type="{self_type}"/>\n'
        '  <link rel="search" type="application/opensearchdescription+xml"'
        ' href="/opds/opensearch.xml"/>\n'
        f"{extra_links}"
        f"{''.join(entries)}"
        "</feed>\n"
    )


def nav_entry(ident, title, href, content=""):
    return (
        "  <entry>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <id>{escape(ident)}</id>\n"
        f"    <updated>{_now_iso()}</updated>\n"
        f'    <content type="text">{escape(content)}</content>\n'
        f'    <link rel="subsection" href={quoteattr(href)} type="{OPDS_NAV_TYPE}"/>\n'
        "  </entry>\n"
    )


def acq_entry(v, title, author):
    href = "/dl/" + encode_path(v["rel"])
    cover = "/cover/" + encode_path(v["rel"])
    ident = "urn:ln:vol:" + hashlib.md5(v["rel"].encode("utf-8")).hexdigest()
    return (
        "  <entry>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <id>{escape(ident)}</id>\n"
        f"    <updated>{_vol_updated(v)}</updated>\n"
        f"    <author><name>{escape(author)}</name></author>\n"
        f"    <dc:issued>{_vol_updated(v)[:10]}</dc:issued>\n"
        f'    <content type="text">{escape(author + " · " + human_size(v["size"]))}</content>\n'
        f'    <link rel="http://opds-spec.org/image" href={quoteattr(cover)} type="image/jpeg"/>\n'
        f'    <link rel="http://opds-spec.org/image/thumbnail" href={quoteattr(cover)} type="image/jpeg"/>\n'
        f'    <link rel="http://opds-spec.org/acquisition" href={quoteattr(href)} type="{EPUB_MIME}"/>\n'
        f'    <link rel="http://opds-spec.org/acquisition/open-access" href={quoteattr(href)} type="{EPUB_MIME}"/>\n'
        f'    <link rel="alternate" href={quoteattr(href)} type="{EPUB_MIME}"/>\n'
        "  </entry>\n"
    )


def _paginate(items, page, base_href):
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    chunk = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    extra = ""
    if page < pages:
        extra += f'  <link rel="next" href={quoteattr(base_href + "&page=" + str(page + 1))} type="{OPDS_ACQ_TYPE}"/>\n'
    if page > 1:
        extra += f'  <link rel="previous" href={quoteattr(base_href + "&page=" + str(page - 1))} type="{OPDS_ACQ_TYPE}"/>\n'
    extra += f"  <thr:count>{total}</thr:count>\n"
    return chunk, extra


def feed_root():
    lib = get_library()
    n_books = sum(len(b) for b in lib.values())
    n_vols = sum(1 for _ in all_vols(lib))
    entries = []
    for cat in (CATEGORY_ONGOING, CATEGORY_DONE):
        books = lib.get(cat, {})
        entries.append(nav_entry(
            "urn:ln:cat:" + quote(cat), cat, "/opds/catalog/" + quote(cat),
            f"{len(books)} 部作品 · {sum(len(v) for v in books.values())} 卷"))
    entries.append(nav_entry("urn:ln:recent", "最近更新", "/opds/recent", f"最近改动的 {RECENT_SIZE} 卷"))
    entries.append(nav_entry("urn:ln:all", "全部作品", "/opds/catalog/all", f"{n_books} 部作品 · {n_vols} 卷"))
    return _feed("urn:ln:root", SERVER_TITLE, entries, "/")


def feed_catalog(cat, page=1):
    lib = get_library()
    if cat == "all":
        merged = {}
        for c, bs in lib.items():
            for b, vols in bs.items():
                merged[f"{c}/{b}"] = (c, len(vols))
        keys = sorted(merged.keys())
        chunk, extra = _paginate(keys, page, "/opds/catalog/all?page=1")
        body = "".join(
            nav_entry("urn:ln:book:" + quote(k), k.split("/", 1)[1], "/opds/book/" + encode_path(k),
                      f"{merged[k][0]} · {merged[k][1]} 卷")
            for k in chunk)
        return _feed("urn:ln:cat:all", f"{SERVER_TITLE} · 全部作品", [],
                     "/opds/catalog/all?page=" + str(page), OPDS_ACQ_TYPE, extra + body)
    if cat not in CATEGORY_DIRS:
        return None
    books = lib.get(cat, {})
    keys = sorted(books.keys(), key=lambda s: s.lower())
    chunk, extra = _paginate(keys, page, "/opds/catalog/" + quote(cat) + "?page=1")
    body = "".join(
        nav_entry("urn:ln:book:" + quote(f"{cat}/{k}"), k, "/opds/book/" + encode_path(f"{cat}/{k}"),
                  f"{len(books[k])} 卷")
        for k in chunk)
    return _feed("urn:ln:cat:" + quote(cat), f"{SERVER_TITLE} · {cat}", [],
                 "/opds/catalog/" + quote(cat) + "?page=" + str(page), OPDS_NAV_TYPE, extra + body)


def feed_book(rel, page=1):
    rel = _safe_relpath(rel)
    if not rel:
        return None
    parts = rel.split("/")
    cat = parts[0]
    book = parts[1] if len(parts) > 1 else ""
    vols = get_library().get(cat, {}).get(book)
    if vols is None:
        return None
    chunk, extra = _paginate(vols, page, "/opds/book/" + encode_path(rel) + "?page=1")
    body = "".join(acq_entry(v, v["title"], book) for v in chunk)
    return _feed("urn:ln:book:" + quote(rel), f"{book} · {cat}", [],
                 "/opds/book/" + encode_path(rel) + "?page=" + str(page), OPDS_ACQ_TYPE, extra + body)


def feed_recent(page=1):
    vols = sorted(all_vols(), key=lambda t: t[2]["mtime"], reverse=True)[:RECENT_SIZE]
    chunk, extra = _paginate(vols, page, "/opds/recent?page=1")
    body = "".join(acq_entry(v, f"{b} · {v['title']}", c) for c, b, v in chunk)
    return _feed("urn:ln:recent", f"{SERVER_TITLE} · 最近更新", [],
                 "/opds/recent?page=" + str(page), OPDS_ACQ_TYPE, extra + body)


def feed_search(q, page=1):
    q = (q or "").strip().lower()
    hits = []
    if q:
        for c, b, v in all_vols():
            if q in f"{b}/{v['rel']}".lower():
                hits.append((c, b, v))
    chunk, extra = _paginate(hits, page, "/opds/search?q=" + quote(q) + "&page=1")
    body = "".join(acq_entry(v, f"{b} · {v['title']}", c) for c, b, v in chunk)
    return _feed("urn:ln:search:" + quote(q), f"{SERVER_TITLE} · 搜索「{q}」", [],
                 "/opds/search?q=" + quote(q) + "&page=" + str(page), OPDS_ACQ_TYPE, extra + body)


def opensearch_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
        f"  <ShortName>{escape(SERVER_TITLE)}</ShortName>\n"
        "  <Description>搜索轻小说书名与卷名</Description>\n"
        "  <InputEncoding>UTF-8</InputEncoding>\n"
        "  <OutputEncoding>UTF-8</OutputEncoding>\n"
        f'  <Url type="{OPDS_ACQ_TYPE}" template="/opds/search?q={{searchTerms}}"/>\n'
        "</OpenSearchDescription>\n"
    )


# ---------------------------- HTML 视图（手机/桌面浏览器） ----------------------------
def _accept_wants_xml(accept):
    """OPDS 客户端/阅读器发含 atom+xml 或 opds 的 Accept；普通浏览器不会。"""
    a = (accept or "").lower()
    return "atom+xml" in a or "opds" in a


SITE_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#f2f4f7; --card:#fff; --text:#1f2328; --muted:#6b7785; --border:#e5e7eb;
  --accent:#0a66c2; --accent2:#004182; --accent-fg:#fff; --hover:#eef4fb;
  --shadow:0 1px 2px rgba(16,22,26,.06),0 2px 8px rgba(16,22,26,.05);
  --shadow-sm:0 1px 2px rgba(16,22,26,.05);
  --radius:12px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e1116; --card:#171c23; --text:#e8edf3; --muted:#9aa4b0; --border:#2c333d;
    --accent:#4c9df0; --accent2:#2f7fd0; --accent-fg:#0e1116; --hover:#1d2530;
    --shadow:none; --shadow-sm:none;
  }
}
html{-webkit-text-size-adjust:100%;scroll-padding-top:76px}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
  line-height:1.55;font-size:14px}
a{color:inherit;text-decoration:none}
img{display:block}
.hero,.group,.sec-head{scroll-margin-top:76px}

/* ---------- 顶栏 ---------- */
header{position:sticky;top:0;z-index:50;background:var(--card);
  border-bottom:1px solid var(--border);
  box-shadow:0 1px 3px rgba(16,22,26,.04),0 4px 12px rgba(16,22,26,.04);
  isolation:isolate}
.hbar{max-width:1180px;margin:0 auto;padding:0 20px;display:flex;align-items:center;gap:14px;
  height:58px;min-width:0}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;font-size:15px;flex-shrink:0}
.brand .dot{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:var(--accent-fg);display:grid;place-items:center;font-size:14px;flex-shrink:0}
.tabs{display:flex;gap:2px;margin-left:4px;flex-shrink:0}
.tab{padding:7px 13px;border-radius:8px;font-size:13px;color:var(--muted);transition:background .12s,color .12s;
  position:relative}
.tab:hover{background:var(--hover);color:var(--text)}
.tab.on{background:var(--hover);color:var(--accent);font-weight:600}
.tab.on::after{content:"";position:absolute;left:14px;right:14px;bottom:-1px;height:2px;
  background:var(--accent);border-radius:2px 2px 0 0}
.hsearch{flex:1 1 auto;display:flex;max-width:360px;margin-left:auto;min-width:0}
.hsearch input{flex:1;min-width:0;padding:8px 12px;font-size:13px;border:1px solid var(--border);
  border-radius:8px 0 0 8px;background:var(--bg);color:var(--text);outline:none}
.hsearch input:focus{border-color:var(--accent)}
.hsearch button{padding:8px 14px;font-size:13px;border:1px solid var(--accent);
  border-left:0;border-radius:0 8px 8px 0;background:var(--accent);color:var(--accent-fg);
  cursor:pointer;white-space:nowrap;flex-shrink:0}

/* ---------- 容器 ---------- */
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
h1{font-size:20px;font-weight:600;margin:0 0 4px;letter-spacing:.2px}
h2{font-size:15px;font-weight:600;margin:28px 0 12px;display:flex;align-items:center;gap:8px;
  padding-left:10px;border-left:3px solid var(--accent);line-height:1.2}
h2 .n{font-size:12px;font-weight:400;color:var(--muted);margin-left:2px}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
.crumb{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin:0 0 16px}
.crumb a:hover{color:var(--accent)}

/* ---------- 首页 ---------- */
.hero-home{padding:30px 28px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:28px;position:relative;overflow:hidden}
.hero-home::before{content:"";position:absolute;right:-40px;top:-40px;width:180px;height:180px;
  border-radius:50%;background:linear-gradient(135deg,var(--hover),transparent 70%);opacity:.6;pointer-events:none}
.hero-home h1{font-size:24px;margin-bottom:6px;position:relative}
.hero-home .sub{margin:0;position:relative}
.hero-home .tips{margin-top:18px;display:flex;flex-wrap:wrap;gap:8px;position:relative}
.hero-home .tip{padding:6px 12px;border-radius:20px;background:var(--hover);
  border:1px solid var(--border);font-size:12px;color:var(--muted)}

/* ---------- 分类大卡 ---------- */
.cats{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.cat{padding:24px 22px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);transition:transform .14s,border-color .14s,box-shadow .14s}
.cat:hover{transform:translateY(-3px);border-color:var(--accent);box-shadow:0 4px 16px rgba(10,102,194,.08)}
.cat .ico{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;
  font-size:20px;margin-bottom:16px}
.cat .nm{font-weight:600;font-size:17px}
.cat .ds{font-size:12px;color:var(--muted);margin-top:5px}
.cat .go{margin-top:16px;font-size:12px;color:var(--accent);font-weight:500}

/* ---------- 书封网格 ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:20px 14px}
.card{display:block}
.card .ph{position:relative;width:100%;aspect-ratio:2/3;border-radius:10px;overflow:hidden;
  background:var(--border);box-shadow:var(--shadow);transition:transform .14s,box-shadow .14s}
.card:hover .ph{transform:translateY(-4px);box-shadow:0 8px 18px rgba(16,22,26,.12)}
.card .ph img{width:100%;height:100%;object-fit:cover}
.card .ph .badge{position:absolute;right:6px;top:6px;padding:2px 8px;border-radius:20px;
  background:rgba(15,20,26,.72);color:#fff;font-size:10px;font-weight:500;backdrop-filter:blur(6px)}
.card .t{margin-top:9px;font-size:13px;font-weight:500;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .s{margin-top:3px;font-size:11px;color:var(--muted)}

/* ---------- 卷列表 ---------- */
.vol{display:flex;align-items:center;gap:14px;padding:12px 14px;background:var(--card);
  border:1px solid var(--border);border-radius:var(--radius);margin-bottom:8px;
  transition:background .12s,border-color .12s,box-shadow .12s,transform .12s}
.vol:hover{background:var(--hover);border-color:var(--accent);
  box-shadow:0 2px 8px rgba(10,102,194,.06);transform:translateX(2px)}
.vol .ph{width:48px;height:72px;flex-shrink:0;border-radius:7px;overflow:hidden;background:var(--border);
  box-shadow:var(--shadow-sm)}
.vol .ph img{width:100%;height:100%;object-fit:cover;display:block}
.vol .meta{flex:1;min-width:0}
.vol .meta .t{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vol .meta .s{font-size:12px;color:var(--muted);margin-top:3px;display:flex;align-items:center;gap:8px}
.dl{padding:7px 16px;border-radius:8px;background:var(--accent);color:var(--accent-fg);
  font-size:12px;font-weight:500;white-space:nowrap;flex-shrink:0;transition:filter .12s,transform .12s}
.vol .dl{padding:8px 18px;font-size:12.5px}
.dl:hover{filter:brightness(1.08)}
.dl:active{transform:scale(.97)}
.dl.big{padding:11px 22px;font-size:14px;border-radius:10px}
.dl.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.dl.ghost:hover{background:var(--hover)}
.dl.small{padding:5px 11px;font-size:11.5px;border-radius:7px}

/* ---------- 分组（书内子目录，可折叠） ---------- */
.sec-head{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:10px;margin:32px 0 14px}
.sec-head h2{margin:0;display:flex;align-items:center;gap:8px}
.sec-head .grp-tools{display:flex;align-items:center;gap:6px;margin:0;flex-shrink:0}
.grp-btn{padding:6px 13px;border-radius:8px;border:1px solid var(--border);
  background:var(--card);color:var(--muted);font-size:12px;cursor:pointer;transition:all .12s}
.grp-btn:hover{border-color:var(--accent);color:var(--accent);background:var(--hover)}
.group{margin-bottom:18px}
.group-head{display:flex;align-items:center;gap:12px;padding:13px 16px;cursor:pointer;
  background:var(--card);border:1px solid var(--border);border-radius:12px 12px 0 0;
  box-shadow:var(--shadow);user-select:none;-webkit-tap-highlight-color:transparent;
  transition:background .12s}
.group:not(.open) .group-head{border-radius:12px}
.group-head:hover{background:var(--hover)}
.group-head:active{background:var(--hover)}
.group-head .caret{width:20px;height:20px;flex-shrink:0;display:grid;place-items:center;
  color:var(--muted);font-size:10px;transition:transform .2s ease}
.group.open .group-head .caret{transform:rotate(90deg)}
.group-head .gico{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;
  background:#eef4fb;color:var(--accent);font-size:14px;flex-shrink:0}
.group-head .gnm{font-weight:600;font-size:14.5px}
.group-head .gmeta{font-size:12px;color:var(--muted);flex:1;display:flex;align-items:center;gap:6px}
.group-head .gmeta::before{content:"";width:3px;height:3px;border-radius:50%;background:var(--muted);
  opacity:.5;flex-shrink:0}
.group-head .gmeta:empty::before{display:none}
.group-body{display:none;padding:12px 8px 6px;border:1px solid var(--border);border-top:0;
  border-radius:0 0 12px 12px;background:var(--bg)}
.group.open .group-body{display:block}
.group-body .vol{margin-bottom:6px}
.group-body .vol:last-child{margin-bottom:0}
@media (prefers-reduced-motion:reduce){.group-head .caret{transition:none}}

/* ---------- 详情头 ---------- */
.hero{display:flex;gap:24px;padding:24px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:24px}
.hero .ph{width:140px;flex-shrink:0;aspect-ratio:2/3;border-radius:12px;overflow:hidden;
  background:var(--border);box-shadow:0 4px 14px rgba(16,22,26,.1)}
.hero .ph img{width:100%;height:100%;object-fit:cover}
.hero .info{flex:1;min-width:0;display:flex;flex-direction:column}
.hero .meta-line{font-size:13px;color:var(--muted);margin:3px 0}
.hero .meta-line b{color:var(--text);font-weight:500}
.hero .desc{font-size:13px;color:var(--muted);margin-top:12px;line-height:1.7;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.hero .actions{display:flex;gap:10px;margin-top:auto;padding-top:16px;flex-wrap:wrap}

/* ---------- 工具条 ---------- */
.bar{display:flex;align-items:center;gap:10px;margin:20px 0 14px;flex-wrap:wrap}
.bar .spacer{flex:1}

/* ---------- 分页 ---------- */
.pager{display:flex;justify-content:center;align-items:center;gap:10px;margin:30px 0 6px}
.pager a,.pager span{padding:9px 18px;border:1px solid var(--border);border-radius:10px;
  background:var(--card);font-size:13px;transition:all .12s}
.pager a:hover{border-color:var(--accent);color:var(--accent);box-shadow:0 2px 6px rgba(10,102,194,.08)}
.pager .cur{color:var(--muted);border-color:transparent;background:transparent}
.empty{padding:60px 16px;text-align:center;color:var(--muted);font-size:14px;
  background:var(--card);border:1px dashed var(--border);border-radius:var(--radius)}
footer{margin-top:48px;padding:24px 16px;text-align:center;color:var(--muted);font-size:12px;
  border-top:1px solid var(--border)}
@media (max-width:760px){
  .wrap{padding:16px 14px 36px}
  /* 顶栏变两行：①品牌+搜索 ②tab 可横滑 */
  .hbar{flex-wrap:wrap;height:auto;padding:10px 12px;gap:8px;align-items:center}
  .brand{flex:0 0 auto}
  .brand span{display:none}
  .hsearch{flex:1 1 auto;max-width:none;min-width:0;margin-left:0;order:2}
  .hsearch input{flex:1;min-width:0;font-size:14px}
  .tabs{order:3;flex:0 0 100%;overflow-x:auto;scrollbar-width:none;
    white-space:nowrap;margin:2px -4px 0;padding:0 4px}
  .tabs::-webkit-scrollbar{display:none}
  .tab{padding:7px 11px;font-size:13px;flex-shrink:0}
  .tab.on::after{display:none}
  /* 内容区 */
  .grid{grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:14px 10px}
  .card .t{font-size:12px;line-height:1.3;-webkit-line-clamp:2}
  .card .ph .badge{font-size:10px;padding:2px 7px}
  .hero{gap:14px;padding:16px;align-items:stretch;border-radius:10px}
  .hero .ph{width:108px;flex-shrink:0;border-radius:9px}
  .hero .info h1{font-size:17px;margin-bottom:2px}
  .hero .meta-line{font-size:12px;margin:1px 0}
  .hero .desc{font-size:12px;margin-top:8px;line-height:1.6;
    display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
  .hero .actions{margin-top:auto;padding-top:12px}
  .hero .actions .dl.big{padding:10px 14px;font-size:13px;width:100%;text-align:center}
  .hero-home{padding:20px 18px;border-radius:10px}.hero-home h1{font-size:20px}
  .cats{grid-template-columns:1fr;gap:12px}
  .grp-tools{gap:6px}.grp-btn{padding:5px 10px}
  .group{margin-bottom:14px}
  .group-head{padding:11px 12px;border-radius:10px 10px 0 0;gap:10px}
  .group:not(.open) .group-head{border-radius:10px}
  .group-body{border-radius:0 0 10px 10px;padding:10px 6px 4px}
  .group-head .gnm{font-size:13px}
  .group-head .gico{width:24px;height:24px;font-size:13px}
  .group-head .dl.small{padding:4px 9px;font-size:11px}
  .vol{padding:10px 11px;gap:11px}
  .vol .ph{width:44px;height:66px;border-radius:6px}
  .vol .meta .t{font-size:13px}
  .vol .dl{padding:6px 12px;font-size:11.5px}
  h2{font-size:14px;margin:20px 0 10px;padding-left:8px;border-left-width:2px}
  h1{font-size:18px}
  .sec-head{margin:24px 0 12px}
  .bar{margin:16px 0 12px}
}
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%230a66c2'/%3E"
           "%3Cpath d='M9 8h7v16H9z' fill='white' opacity='.95'/%3E"
           "%3Cpath d='M17.5 8h5.5v16h-5.5z' fill='white' opacity='.6'/%3E%3C/svg%3E")


def _html_page(title, body_inner, active="", extra_css=""):
    tabs = (
        ("done",    "/opds/catalog/" + quote(CATEGORY_DONE),    CATEGORY_DONE),
        ("ongoing", "/opds/catalog/" + quote(CATEGORY_ONGOING), CATEGORY_ONGOING),
        ("recent",  "/opds/recent",   "最近更新"),
        ("all",     "/opds/catalog/all", "全部作品"),
    )
    tab_html = "".join(
        '<a class="tab%s" href="%s">%s</a>' % (" on" if k == active else "", href, label)
        for k, href, label in tabs)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#0e1116">'
        f'<link rel="icon" href="{FAVICON}">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{SITE_CSS}{extra_css}</style>"
        "</head><body>"
        '<header><div class="hbar">'
        f'<a class="brand" href="/"><span class="dot">&#128218;</span><span>{html.escape(SERVER_TITLE)}</span></a>'
        f'<nav class="tabs">{tab_html}</nav>'
        '<form class="hsearch" action="/opds/search">'
        '<input name="q" placeholder="搜索书名或卷名…" aria-label="搜索">'
        '<button type="submit">搜索</button></form>'
        "</div></header>"
        f'<main class="wrap">{body_inner}</main>'
        f'<footer>{html.escape(SERVER_TITLE)} · OPDS 书源 · 由 opds_server.py 自动维护</footer>'
        '<script>function grpAll(open){'
        'document.querySelectorAll(".group").forEach(function(g){'
        'g.classList.toggle("open",open)});}</script>'
        "</body></html>"
    )


def _cover_url(rel):
    return "/cover/" + encode_path(rel)


def _next_link(extra):
    m = re.search(r'rel="next" href="([^"]+)"', extra or "")
    return m.group(1) if m else ""


def _prev_link(extra):
    m = re.search(r'rel="previous" href="([^"]+)"', extra or "")
    return m.group(1) if m else ""


def _pager_html(page, nxt, prv):
    if not nxt and not prv:
        return ""
    nxt_html = f'<a href="{html.escape(nxt)}">下一页 →</a>' if nxt else "<span>下一页</span>"
    prv_html = f'<a href="{html.escape(prv)}">← 上一页</a>' if prv else "<span>上一页</span>"
    return f'<div class="pager">{prv_html}<span class="cur">第 {page} 页</span>{nxt_html}</div>'


def root_html():
    lib = get_library()
    cats = "".join(
        f'<a class="cat" href="/opds/catalog/{quote(cat)}">'
        f'<div class="ico" style="background:{"#fff1e6" if cat == CATEGORY_DONE else "#e7f3ff"};'
        f'color:{"#bc4c00" if cat == CATEGORY_DONE else "#0a66c2"}">'
        f'{"&#10003;" if cat == CATEGORY_DONE else "&#128336;"}</div>'
        f'<div class="nm">{html.escape(cat)}</div>'
        f'<div class="ds">{len(books)} 部作品 · {sum(len(v) for v in books.values())} 卷</div>'
        f'<div class="go">进入浏览 →</div></a>'
        for cat, books in lib.items())
    body = (
        '<div class="hero-home">'
        f"<h1>{html.escape(SERVER_TITLE)}</h1>"
        f'<p class="sub">个人轻小说收藏 · 手机可直接浏览、下载单卷或整本打包</p>'
        f'<div class="tips">'
        f'<span class="tip">&#128214; 支持单卷下载</span>'
        f'<span class="tip">&#128230; 整本 / 整组打包 ZIP</span>'
        f'<span class="tip">&#128241; OPDS 阅读器可直接订阅</span>'
        f"</div>"
        "</div>"
        f"<h2>分类浏览</h2>"
        f'<div class="cats">{cats}</div>'
        f"<h2>快速入口 <span class=\"n\">最近更新 · 全部作品</span></h2>"
        f'<div class="cats">'
        f'<a class="cat" href="/opds/recent"><div class="ico" style="background:#e7f3ff;color:#0a66c2">&#128336;</div>'
        f'<div class="nm">最近更新</div><div class="ds">最近改动的 {RECENT_SIZE} 卷</div><div class="go">查看 →</div></a>'
        f'<a class="cat" href="/opds/catalog/all"><div class="ico" style="background:#f0e7ff;color:#6639ba">&#128218;</div>'
        f'<div class="nm">全部作品</div><div class="ds">不分分类浏览</div><div class="go">查看 →</div></a>'
        f"</div>"
    )
    return _html_page(SERVER_TITLE, body, active="")


def catalog_html(cat, page=1):
    lib = get_library()
    if cat == "all":
        merged = {}
        for c, bs in lib.items():
            for b, vols in bs.items():
                merged[f"{c}/{b}"] = (c, vols)
        keys = sorted(merged.keys())
        chunk, extra = _paginate(keys, page, "/opds/catalog/all?page=1")
        cards = "".join(
            f'<a class="card" href="/opds/book/{encode_path(k)}">'
            f'<div class="ph"><img src="{_cover_url(merged[k][1][0]["rel"])}" alt="" loading="lazy"></div>'
            f'<div class="t">{html.escape(k.split("/", 1)[1])}</div>'
            f'<div class="s">{html.escape(merged[k][0])} · {len(merged[k][1])} 卷</div></a>'
            for k in chunk)
        total = len(keys)
        label = "全部作品"
        active = "all"
    elif cat in CATEGORY_DIRS:
        books = lib.get(cat, {})
        keys = sorted(books.keys(), key=lambda s: s.lower())
        chunk, extra = _paginate(keys, page, "/opds/catalog/" + quote(cat) + "?page=1")
        cards = "".join(
            f'<a class="card" href="/opds/book/{encode_path(cat + "/" + b)}">'
            f'<div class="ph"><img src="{_cover_url(books[b][0]["rel"])}" alt="" loading="lazy">'
            f'<span class="badge">{len(books[b])} 卷</span></div>'
            f'<div class="t">{html.escape(b)}</div>'
            f'<div class="s">{len(books[b])} 卷</div></a>'
            for b in chunk)
        total = len(keys)
        label = cat
        active = "done" if cat == CATEGORY_DONE else "ongoing"
    else:
        return None
    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span>'
        f'<span>{html.escape(label)}</span></div>'
        '<div class="bar">'
        f"<h1 style=\"margin:0\">{html.escape(label)}</h1>"
        '<span class="spacer"></span>'
        f'<span class="sub" style="margin:0">{total} 部作品</span>'
        "</div>"
        f'<div class="grid">{cards}</div>'
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"{label} · {SERVER_TITLE}", body, active=active)


def book_html(rel, page=1):                                # page 参数保留以兼容旧 URL，详情页不再分页
    rel = _safe_relpath(rel)
    if not rel:
        return None
    parts = rel.split("/")
    cat = parts[0]
    book = parts[1] if len(parts) > 1 else ""
    vols = get_library().get(cat, {}).get(book)
    if vols is None:
        return None
    total_size = sum(v["size"] for v in vols)
    meta = get_epub_meta(vols[0]["rel"]) if vols else {}
    author = meta.get("creator", "")
    desc = meta.get("description", "")
    zip_url = "/zip/" + encode_path(rel)

    meta_lines = (
        f'<div class="meta-line">作者 <b>{html.escape(author)}</b></div>' if author else "")
    meta_lines += f'<div class="meta-line">分类 <b>{html.escape(cat)}</b> · 卷数 <b>{len(vols)}</b> · 体积 <b>{human_size(total_size)}</b></div>'

    desc_html = f'<div class="desc">{html.escape(desc)}</div>' if desc else ""

    # 详情页一次性渲染全部卷（不翻页）：直接传完整 vols 列表，_render_groups 会显示所有分组
    groups = _group_vols_by_subdir(vols, cat, book)
    groups_html = _render_groups(groups, cat, book, vols)

    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span>'
        f'<a href="/opds/catalog/{quote(cat)}">{html.escape(cat)}</a><span>/</span>'
        f'<span>{html.escape(book)}</span></div>'
        '<div class="hero">'
        f'<div class="ph"><img src="{_cover_url(vols[0]["rel"])}" alt=""></div>'
        '<div class="info">'
        f"<h1>{html.escape(book)}</h1>"
        + meta_lines + desc_html +
        '<div class="actions">'
        f'<a class="dl big" href="{zip_url}">⬇ 打包下载全部（{len(vols)} 卷 · {human_size(total_size)}）</a>'
        "</div>"
        "</div></div>"
        + (f'<div class="sec-head">'
         f'<h2>分卷下载 <span class="n">{len(vols)} 卷</span></h2>'
         f'<div class="grp-tools">'
         f'<button class="grp-btn" onclick="grpAll(true)">全部展开</button>'
         f'<button class="grp-btn" onclick="grpAll(false)">全部折叠</button>'
         f'</div></div>' if groups_html else
         f'<h2>分卷下载 <span class="n">{len(vols)} 卷</span></h2>')
        + (groups_html or '<div class="empty">这一页没有内容</div>')
    )
    active = "done" if cat == CATEGORY_DONE else "ongoing"
    return _html_page(f"{book} · {SERVER_TITLE}", body, active=active)


def _group_vols_by_subdir(vols, cat, book):
    """按卷相对书名的子目录分组。
    返回 [(subdir_name, [vol, ...])]，无子目录（根目录 epub）放最后，名为「根目录」。"""
    inner = f"{cat}/{book}/"
    groups = {}
    for v in vols:
        if v["rel"].startswith(inner):
            rem = v["rel"][len(inner):]  # "正篇/01.epub" 或 "01.epub"
        else:
            rem = v["rel"].split("/")[-1]
        subdir = rem.rsplit("/", 1)[0] if "/" in rem else ""
        groups.setdefault(subdir, []).append(v)
    keys = sorted(k for k in groups if k)  # 按名称排（去掉空键）
    ordered = [(k, groups.pop(k)) for k in keys]
    if "" in groups:  # 根目录（无子目录）放最后
        ordered.append(("", groups[""]))
    return ordered


def _render_groups(groups, cat, book, page_chunk):
    """把分组渲染成「文件夹 + 卷列表」，并标记哪些卷在当前分页里。"""
    rels_in_page = {v["rel"] for v in page_chunk}
    out = []
    for subdir, vols in groups:
        in_page = [v for v in vols if v["rel"] in rels_in_page]
        if not in_page:  # 该组没卷在当前页，跳过
            continue
        group_size = sum(v["size"] for v in vols)
        group_name = subdir if subdir else "正篇"
        if subdir:
            zip_url = "/zip/" + encode_path(f"{cat}/{book}/{subdir}")
        else:
            zip_url = "/zip/" + encode_path(f"{cat}/{book}")
        rows = "".join(
            f'<a class="vol" href="/dl/{encode_path(v["rel"])}">'
            f'<div class="ph"><img src="{_cover_url(v["rel"])}" alt="" loading="lazy"></div>'
            f'<div class="meta"><div class="t">{html.escape(v["title"])}</div>'
            f'<div class="s">{human_size(v["size"])}</div></div>'
            f'<span class="dl">下载</span></a>'
            for v in in_page)
        opened = " open" if not out else ""   # 第一组默认展开，其余折叠
        out.append(
            f'<section class="group{opened}">'
            f'<header class="group-head" onclick="this.parentNode.classList.toggle(\'open\')">'
            f'<span class="caret">&#9654;</span>'
            f'<span class="gico">&#128194;</span>'
            f'<span class="gnm">{html.escape(group_name)}</span>'
            f'<span class="gmeta">{len(vols)} 卷</span>'
            f'<a class="dl ghost small" href="{zip_url}" '
            f'onclick="event.stopPropagation()">打包本组</a>'
            f'</header>'
            f'<div class="group-body">{rows}</div>'
            f'</section>')
    return "".join(out)


def _vol_rows(items):
    return "".join(
        f'<a class="vol" href="/dl/{encode_path(v["rel"])}">'
        f'<div class="ph"><img src="{_cover_url(v["rel"])}" alt="" loading="lazy"></div>'
        f'<div class="meta"><div class="t">{html.escape(title)}</div>'
        f'<div class="s">{html.escape(c)} · {human_size(v["size"])}</div></div>'
        f'<span class="dl">下载</span></a>'
        for c, b, v, title in items)


def recent_html(page=1):
    vols = sorted(all_vols(), key=lambda t: t[2]["mtime"], reverse=True)[:RECENT_SIZE]
    chunk, extra = _paginate(vols, page, "/opds/recent?page=1")
    rows = _vol_rows([(c, b, v, f"{b} · {v['title']}") for c, b, v in chunk])
    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span><span>最近更新</span></div>'
        "<h1>最近更新</h1>"
        f'<p class="sub">按文件修改时间排序 · {len(vols)} 卷</p>'
        + (rows or '<div class="empty">暂无内容</div>')
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"最近更新 · {SERVER_TITLE}", body, active="recent")


def search_html(q, page=1):
    q = (q or "").strip()
    if q:
        hits = []
        for c, b, v in all_vols():
            if q.lower() in f"{b}/{v['rel']}".lower():
                hits.append((c, b, v))
        chunk, extra = _paginate(hits, page, "/opds/search?q=" + quote(q) + "&page=1")
        rows = _vol_rows([(c, b, v, f"{b} · {v['title']}") for c, b, v in chunk])
        result_html = (
            f'<p class="sub">「{html.escape(q)}」命中 {len(hits)} 卷</p>'
            + (rows or f'<div class="empty">没有找到与「{html.escape(q)}」相关的内容</div>')
        )
        pager = _pager_html(page, _next_link(extra), _prev_link(extra))
    else:
        result_html = '<div class="empty">输入书名或卷名开始搜索</div>'
        pager = ""
    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span><span>搜索</span></div>'
        "<h1>搜索</h1>"
        + result_html + pager
    )
    return _html_page(f"搜索 · {SERVER_TITLE}", body, active="")


# 兼容旧名
def index_html():
    return root_html()




# ---------------------------- HTTP Handler ----------------------------
class OPDSHandler(BaseHTTPRequestHandler):
    server_version = "LN-OPDS/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            log.info("OPDS %s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def _auth_ok(self):
        if not AUTH_USER and not AUTH_PASS:
            return True
        hdr = self.headers.get("Authorization", "") or ""
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return user == AUTH_USER and pw == AUTH_PASS

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Light-Novel OPDS", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, body, ctype, extra=None, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _notfound(self, msg="Not Found"):
        self._send(404, f"<h1>404</h1><p>{html.escape(msg)}</p>", "text/html; charset=utf-8")

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        if not self._auth_ok():
            self._deny()
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query or "")
        try:
            if path in ("/", "/index.html"):
                accept = self.headers.get("Accept", "") or ""
                if "atom+xml" in accept or "opds" in accept.lower():
                    self._send(200, feed_root(), f"{OPDS_NAV_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, index_html(), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/opensearch.xml":
                self._send(200, opensearch_xml(), "application/opensearchdescription+xml; charset=utf-8",
                           head_only=head_only)
                return

            m = re.match(r"^/opds/catalog/(.+)$", path)
            if m:
                cat = unquote(m.group(1))
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    body = feed_catalog(cat, page)
                    if body is None:
                        self._notfound("分类不存在")
                    else:
                        self._send(200, body, f"{OPDS_NAV_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    body = catalog_html(cat, page)
                    if body is None:
                        self._notfound("分类不存在")
                    else:
                        self._send(200, body, "text/html; charset=utf-8", head_only=head_only)
                return

            m = re.match(r"^/opds/book/(.+)$", path)
            if m:
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    body = feed_book(m.group(1), page)
                    if body is None:
                        self._notfound("作品不存在")
                    else:
                        self._send(200, body, f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    body = book_html(m.group(1), page)
                    if body is None:
                        self._notfound("作品不存在")
                    else:
                        self._send(200, body, "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/recent":
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_recent(page), f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, recent_html(page), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/search":
                q = qs.get("q", [""])[0]
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_search(q, page), f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, search_html(q, page), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/refresh":
                get_library(force=True)
                self._send(200, '{"ok":true}', "application/json; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/stats":
                lib = get_library(force=True)
                data = {c: {"books": len(b), "vols": sum(len(v) for v in b.values())} for c, b in lib.items()}
                self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8",
                           head_only=head_only)
                return

            m = re.match(r"^/cover/(.+)$", path)
            if m:
                self._serve_cover(m.group(1), head_only)
                return

            m = re.match(r"^/zip/(.+)$", path)
            if m:
                self._serve_zip(m.group(1), head_only)
                return

            m = re.match(r"^/dl/(.+)$", path)
            if m:
                self._serve_file(m.group(1), head_only)
                return

            self._notfound(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("处理请求出错 %s：%s", path, exc)
            try:
                self._send(500, f"<h1>500</h1><p>{html.escape(str(exc))}</p>", "text/html; charset=utf-8")
            except Exception:
                pass

    def _serve_cover(self, enc_rel, head_only=False):
        blob, mime = get_cover(enc_rel)
        if not blob:
            self._send(200, BLANK_PNG, "image/png",
                       {"Cache-Control": "public, max-age=86400"}, head_only=head_only)
            return
        self._send(200, blob, mime, {"Cache-Control": "public, max-age=604800"}, head_only=head_only)

    def _serve_zip(self, enc_rel, head_only=False):
        """把一部作品（或一个子目录组）的所有卷流式打包成 zip 下载。
        epub 本身已是压缩格式，用 ZIP_STORED 不二次压缩，速度 = 纯读盘。

        路径格式：
          /zip/分类                 -> 整类打包（每部作品一个子目录）
          /zip/分类/书名             -> 整书打包（保留「正篇/」「番外/」子目录）
          /zip/分类/书名/子目录      -> 整组打包（zip 根目录就是 epub）"""
        rel = _safe_relpath(enc_rel)
        if not rel:
            self._notfound("路径非法")
            return
        parts = rel.split("/")
        cat = parts[0]
        book = parts[1] if len(parts) > 1 else ""
        subdir = "/".join(parts[2:]) if len(parts) > 2 else ""
        lib = get_library()
        if book:
            vols = list(lib.get(cat, {}).get(book) or [])
            if subdir:
                inner = f"{cat}/{book}/"
                vols = [v for v in vols
                        if v["rel"].startswith(inner)
                        and (v["rel"][len(inner):] == subdir
                             or v["rel"][len(inner):].startswith(subdir + "/"))]
            label = f"{book}-{subdir.split('/')[-1]}" if subdir else book
        else:
            books = lib.get(cat, {})
            vols = [v for vols_ in books.values() for v in vols_]
            label = cat
        if not vols:
            self._notfound("没有可打包的卷")
            return

        # 流式发送：无法预知总长度，用 Connection: close 结尾。
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(label + '.zip')}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head_only:
            return
        try:
            sink = _ZipSink(self.wfile)
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for v in vols:
                    disk = resolve_under(LIGHT_NOVEL_DIR, v["rel"])
                    if not disk or not os.path.isfile(disk):
                        continue
                    # 动态剥前缀：cat/ ( + book/ ) ( + subdir/ )
                    arc = v["rel"]
                    if arc.startswith(cat + "/"):
                        arc = arc[len(cat) + 1:]
                    if book and arc.startswith(book + "/"):
                        arc = arc[len(book) + 1:]
                    if subdir and arc.startswith(subdir + "/"):
                        arc = arc[len(subdir) + 1:]
                    zf.write(disk, arcname=arc)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("zip 打包中断 %s：%s", rel, exc)

    def _serve_file(self, enc_rel, head_only=False):
        path = resolve_under(LIGHT_NOVEL_DIR, enc_rel)
        if not path or not os.path.isfile(path):
            self._notfound("文件不存在")
            return
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", (rng or "").strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", EPUB_MIME)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(os.path.basename(path))}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("下载中断 %s：%s", path, exc)


# ---------------------------- 服务启动 ----------------------------
def make_server(port=PORT, bind=BIND):
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        return ThreadingHTTPServer((bind, port), OPDSHandler)
    except OSError as exc:
        win_in_use = getattr(errno, "WSAEADDRINUSE", None)
        if win_in_use is not None and exc.errno == win_in_use:
            raise RuntimeError(f"端口 {port} 已被占用，请换端口（--port）或先关闭占用它的程序。") from exc
        raise


def serve_forever(port=PORT, bind=BIND):
    httpd = make_server(port, bind)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def start_background(port=PORT, bind=BIND):
    """后台线程启动，返回 (httpd, thread)；供 sync_lightnovel.py 调用。"""
    httpd = make_server(port, bind)
    t = threading.Thread(target=httpd.serve_forever, name="opds-server", daemon=True)
    t.start()
    return httpd, t


# ---------------------------- 公网隧道 ----------------------------
def find_cloudflared():
    """按 PATH -> 环境变量 CLOUDFLARED_BIN -> 常见安装位置 -> 用户工具目录 的顺序查找。"""
    for name in ("cloudflared", "cloudflared.exe"):
        found = shutil.which(name)
        if found:
            return found
    env_bin = os.environ.get("CLOUDFLARED_BIN", "")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, ".workbuddy", "bin", "cloudflared.exe"),
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.join(home, "cloudflared", "cloudflared.exe"),
        os.path.join(home, "scoop", "shims", "cloudflared.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def start_named_tunnel(port, name=NAMED_TUNNEL_NAME, timeout=60):
    """拉起**固定域名**的 named tunnel -> (proc, public_url)。
    前提：已跑过 setup_named_tunnel.py 完成登录、建隧道、加 DNS 记录。"""
    exe = find_cloudflared()
    if not exe:
        log.error("未找到 cloudflared，无法启动 named tunnel。")
        return None, None
    host = read_named_hostname()
    if not host:
        log.error("未在 %s 中找到 hostname，请先运行 setup_named_tunnel.py 完成配置。", CF_CONFIG)
        return None, None
    sync_named_port(port)
    try:
        proc = subprocess.Popen(
            [exe, "--config", CF_CONFIG, "tunnel", "run", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        log.error("启动 named tunnel 失败：%s", exc)
        return None, None

    def reader():
        try:
            for line in proc.stdout:
                if re.search(r"(Registered tunnel connection|Connection .* registered)", line, re.I):
                    log.info("named tunnel 已连接：https://%s/", host)
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True, name="cf-named-reader").start()
    time.sleep(2)  # 给隧道一点建立时间
    return proc, "https://" + host + "/"


def read_named_hostname(config_path=None):
    """从 cloudflared config.yml 读出 ingress 的 hostname（固定公网域名）。"""
    config_path = config_path or CF_CONFIG
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"hostname:\s*([A-Za-z0-9._-]+)", txt)
    return m.group(1) if m else None


def sync_named_port(port, config_path=None):
    """确保 config.yml 里 ingress 的 service 端口与本次服务端口一致；返回是否改动过。"""
    config_path = config_path or CF_CONFIG
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False
    new = re.sub(r"service:\s*http://127\.0\.0\.1:\d+", f"service: http://127.0.0.1:{port}", txt)
    if new == txt:
        return False
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new)
        log.info("已将 cloudflared 配置里的回源端口同步为 %d", port)
        return True
    except OSError:
        return False


def start_cloudflared(port, timeout=60):
    """拉起 cloudflared 快速隧道 -> (proc, public_url)；未安装返回 (None, None)。"""
    exe = find_cloudflared()
    if not exe:
        return None, None
    try:
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        log.error("启动 cloudflared 失败：%s", exc)
        return None, None
    holder = {"url": None}

    def reader():
        try:
            for line in proc.stdout:
                m = re.search(r"https://[A-Za-z0-9-]+\.trycloudflare\.com", line)
                if m and not holder["url"]:
                    holder["url"] = m.group(0)
                    log.info("Cloudflare 隧道就绪：%s", holder["url"])
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True, name="cf-reader").start()
    deadline = time.time() + timeout
    while time.time() < deadline and not holder["url"]:
        time.sleep(0.5)
    return proc, holder["url"]


# ---------------------------- 二维码 ----------------------------
def print_qr(url):
    try:
        import segno
        segno.make(url).terminal(compact=True)
        return True
    except Exception:
        pass
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def make_qr_png(url, out_path, scale=8):
    """生成二维码 PNG（需 segno 或 qrcode）；成功返回路径，否则 None。"""
    try:
        import segno
        segno.make(url).save(out_path, scale=scale)
        return out_path
    except Exception:
        pass
    try:
        import qrcode
        img = qrcode.make(url)
        img.save(out_path)
        return out_path
    except Exception:
        return None


EXTERNET_NOTE = """
本机网络现状（2026-09 实测）
----------------------------
  外网出口 IPv4：223.67.255.x（运营商大内网），本机实际是 192.168.31.x
  IPv6：只有 fe80:: 链路本地地址，没有公网 IPv6
  => 没有公网 IP，端口映射 / DDNS 直连这条路走不通，只能用穿透或组网。

外网访问方案（任选其一，手机用流量也能访问本书源）
------------------------------------------------
1) Cloudflare 固定域名隧道（named tunnel）—— 地址永久不变，需自备域名
   前提：有一个 DNS 托管在 Cloudflare 的域名（免费套餐即可）
   配置：双击 setup_named_tunnel.bat（会开浏览器让你登录 Cloudflare 授权，
        然后自动建隧道、写配置、加 CNAME 记录）
   启动：python opds_server.py --tunnel named   或双击 run_named_tunnel.bat
   结果：https://你填的域名/  永久固定，重启不变
   没有域名？换方案 2，或去注册一个（.top/.xyz 一年十几块）。

2) Tailscale / ZeroTier 组网 —— 无需公网 IP、无需域名，推荐
   电脑和手机都装上并加入同一私有网络，手机访问电脑的虚拟 IP：
       http://100.x.y.z:8080/
   优点：加密点对点、速度快、地址固定、免费。
   缺点：手机要装 App，且 App 需保持后台连接。

3) Cloudflare 临时隧道 —— 免域名，但每次重启地址都变
       python opds_server.py --tunnel cloudflared
   会拿到 https://xxxx.trycloudflare.com，重跑就换一个，仅适合临时用。

4) frp / 花生壳 / 樱花穿透 等国内穿透
   需要一台有公网 IP 的服务器（或买现成服务），把 127.0.0.1:8080 映射出去。

安全建议：暴露到公网时务必设置口令：
   set LN_OPDS_USER=你的用户名
   set LN_OPDS_PASS=你的口令
阅读器里把地址写成 https://用户名:口令@域名/ 即可通过 Basic 认证。
"""


# ---------------------------- CLI ----------------------------
def _ensure_logging():
    if not log.handlers:
        os.makedirs(LOG_DIR, exist_ok=True)
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(os.path.join(LOG_DIR, "opds.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def run_service(port=PORT, bind=BIND, public_url="", tunnel=None, no_qr=False):
    """打印订阅信息并以**阻塞**方式运行服务（供 CLI 和 sync_lightnovel.py 复用）。"""
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    lib = get_library(force=True)
    n_books = sum(len(b) for b in lib.values())
    n_vols = sum(1 for _ in all_vols(lib))
    log.info("书库扫描完成：%d 部作品 / %d 卷", n_books, n_vols)

    lan_url = f"http://{local_ip()}:{port}/"
    log.info("OPDS 服务启动：%s（bind %s）", lan_url, bind)
    if AUTH_USER or AUTH_PASS:
        log.info("已启用 Basic 认证（用户 %s）", AUTH_USER or "(空)")
    else:
        log.warning("当前为免密访问；若已暴露公网，请设置 LN_OPDS_USER / LN_OPDS_PASS。")

    print("\n" + "=" * 62)
    print(f"  OPDS 书源已启动   端口 {port}")
    print(f"  局域网订阅地址： {lan_url}")

    cf_proc = None
    pub = public_url.rstrip("/") + "/" if public_url else ""
    if tunnel == "named":
        cf_proc, url = start_named_tunnel(port)
        if url:
            pub = url
            print(f"  固定公网地址：   {pub}")
        else:
            print("  named tunnel 未就绪，请先运行 setup_named_tunnel.py 完成登录与配置。")
    elif tunnel == "cloudflared":
        cf_proc, url = start_cloudflared(port)
        if url:
            pub = url.rstrip("/") + "/"
            print(f"  公网订阅地址：   {pub}（临时域名，重启会变）")
        else:
            print("  cloudflared 未就绪（未安装或超时）。安装：winget install Cloudflare.cloudflared")
    elif pub:
        print(f"  公网订阅地址：   {pub}")

    subscribe = pub or lan_url
    if not no_qr:
        print("\n  手机扫码订阅（或手动把下方地址填进阅读器）：\n")
        if not print_qr(subscribe):
            print("  （未安装 segno/qrcode，跳过二维码，手动输入地址即可）")
    print(f"\n  书源地址： {subscribe}")
    print(f"  书库规模： {n_books} 部作品 / {n_vols} 卷")
    print("  停止服务： Ctrl+C")
    print("=" * 62 + "\n")

    try:
        serve_forever(port, bind)
    except KeyboardInterrupt:
        log.info("收到中断信号，停止 OPDS 服务。")
    finally:
        if cf_proc:
            cf_proc.terminate()


def main():
    ap = argparse.ArgumentParser(description="Light-Novel OPDS 书源服务（手机端远程同步）")
    ap.add_argument("--port", type=int, default=PORT, help=f"监听端口（默认 {PORT}）")
    ap.add_argument("--bind", default=BIND, help="监听地址，默认 0.0.0.0；配隧道时可用 127.0.0.1")
    ap.add_argument("--tunnel", choices=["cloudflared", "named"],
                    help="公网隧道：cloudflared=临时域名；named=固定域名（需先跑 setup_named_tunnel.py）")
    ap.add_argument("--public-url", default="", help="已知的公网地址，仅用于打印订阅地址/二维码")
    ap.add_argument("--no-qr", action="store_true", help="不打印二维码")
    ap.add_argument("--help-internet", action="store_true", help="打印外网接入方案说明后退出")
    args = ap.parse_args()

    _ensure_logging()

    if args.help_internet:
        print(EXTERNET_NOTE)
        return

    run_service(args.port, args.bind, args.public_url, args.tunnel, args.no_qr)


if __name__ == "__main__":
    main()
