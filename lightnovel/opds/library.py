#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书库数据层：路径工具、目录索引、epub 封面提取、epub 元数据、zip 流式打包。"""

import hashlib
import html
import io
import json
import os
import posixpath
import re
import socket
import struct
import threading
import time
import zipfile

from urllib.parse import quote, unquote

from ..paths import (
    CATEGORY_DIRS,
    COVER_CACHE_DIR,
    EPUB_EXTS,
    EXCLUDE_FILE_NAMES,
    INDEX_TTL,
    LIGHT_NOVEL_DIR,
    LOG_DIR,
    SKIP_DIRS,
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


def _shrink(blob, mime, max_w):
    """把封面等比缩到 max_w 宽、转渐进式 JPEG。

    Pillow 不可用或任何异常时**原样返回** —— 本项目运行时零第三方依赖，
    压缩只是可选增强，绝不能因为缺 Pillow 就让封面挂掉。
    """
    if not max_w or not blob:
        return blob, mime
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(blob))
        w, h = im.size
        if not w or w <= max_w:          # 已经够小，别动
            return blob, mime
        im = im.convert("RGB")
        im = im.resize((max_w, max(1, round(h * max_w / w))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
        out = buf.getvalue()
        if out and len(out) < len(blob):
            return out, "image/jpeg"
        return blob, mime
    except Exception:
        return blob, mime                # 降级：用原图


def get_cover(rel, max_w=None):
    """提取 epub 封面 -> (bytes, mime)；无封面返回 (None, None)。内存 + 磁盘缓存。

    max_w：给定时，若封面比它宽就等比压缩（移动端首屏性能的关键）。
    需要 Pillow；未安装时自动跳过压缩、照常返回原图。
    """
    path = resolve_under(LIGHT_NOVEL_DIR, rel)
    if not path or not os.path.isfile(path):
        return None, None
    try:
        st = os.stat(path)
        base = f"{rel}|{st.st_size}|{int(st.st_mtime)}"
        # 不带 max_w 时沿用旧 key 格式，让已缓存的原图继续命中
        key = hashlib.md5((base if not max_w else base + "|w%d" % max_w).encode("utf-8")).hexdigest()
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
        blob, mime = _shrink(blob, mime, max_w)
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


