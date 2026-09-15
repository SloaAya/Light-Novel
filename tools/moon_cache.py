#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Moon+ Reader（静读天下）云端缓存 ``.Moon+`` 的只读解析器。

用途：把 Moon+ 同步到网盘的那几个「没有文档」的二进制/文本格式解出来，
供 OPDS 集成评估与后续功能使用。**全程只读，绝不写回 Moon+ 目录。**

格式速查（已用本机真实数据反推 + 社区逆向结论交叉验证）：

    <书名>.epub.po       阅读位置。形如  ``1779019601627*3@0#10926:100%``
    <书名>.epub.an       批注/书签。**raw zlib**，需前置 gzip 头再 gunzip
    books.sync           书架全量元数据。zlib 压缩的 JSON 数组
    books.sorts          ZIP，内含 shelf.options（排序偏好）
    books.id            本设备的同步 id（13 位数字）
    covers.id            封面索引版本号（与 books.id 同值）
    Cover/<书名>.epub_2.png   渲染好的封面图

.po 字段语义（``dev*x@y#z:p%``）：

    dev  Moon+ 设备 id（不是时间戳！本机 128 个文件跨 11 个月只有 2 个取值，
         且其一等于 books.id / books.sync[].deviceId）
    x    PDF = 逻辑页码；EPUB/MOBI = 零基 spine（章节）序号
    y    次级章节序号，通常为 0
    z    该章节内的零基字符偏移（PDF 无此段）
    p    显示用百分比，仅供 UI，不能用来定位

用法：

    python tools/moon_cache.py                 # 汇总报告
    python tools/moon_cache.py --join           # 加上与本地 OPDS 书库的关联统计
    python tools/moon_cache.py --json           # 机器可读
"""

from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import sys
import zlib

# ---------------------------------------------------------------- 根目录

DEFAULT_ROOT = os.environ.get("LN_MOON_ROOT", r"F:\Apps\Books\.Moon+")
# 本地缓存：网盘小文件每次读约 50ms，128 个就要 7 秒，必须缓存到本地盘
LOCAL_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".autosync", "moon_cache", "positions.json")

_POS_RE = re.compile(r"^(?P<dev>\d+)\*(?P<sec>\d+)(?:@(?P<sub>\d+)#(?P<off>\d+))?:(?P<pct>\d+(?:\.\d+)?)%$")
# category 形如 "<系列名>\n#卷号#\n书架分类\n"（卷号是浮点字符串，如 "1.0"）
_CAT_RE = re.compile(r"^<(?P<series>[^>]*)>\s*\n#(?P<vol>[^#]*)#\s*\n(?P<shelf>[^\n]*)\n?$")
_GZIP_MAGIC = b"\x1f\x8b\x08\x00\x00\x00\x00\x00"


# ---------------------------------------------------------------- 单文件解码

def parse_position(text: str):
    """解析一条 ``.po`` 内容；无法识别返回 None。"""
    m = _POS_RE.match((text or "").strip())
    if not m:
        return None
    return {
        "device_id": m.group("dev"),
        "section": int(m.group("sec")),
        "sub_section": int(m.group("sub")) if m.group("sub") is not None else None,
        "offset": int(m.group("off")) if m.group("off") is not None else None,
        "percent": float(m.group("pct")),
        "is_pdf": m.group("sub") is None,   # PDF 不带 @y#z 段
    }


def decode_an(blob: bytes):
    """``.an``（批注）是 raw zlib，且开头 2 字节被吃掉，需要把 gzip 头接回去。"""
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob)
    return gzip.decompress(_GZIP_MAGIC + blob)


def decode_books_sync(path: str):
    """``books.sync`` → 书籍记录列表（含 category 拆出的系列/卷号）。"""
    raw = open(path, "rb").read()
    data = json.loads(zlib.decompress(raw).decode("utf-8"))
    for r in data:
        m = _CAT_RE.match(r.get("category") or "")
        if m:
            r["_series"] = m.group("series").strip()
            r["_volume"] = m.group("vol").strip()
            r["_shelf"] = m.group("shelf").strip()
        else:
            # 没走 <系列>#卷# 约定的记录，第三行/首行就是书架名
            r["_series"] = ""
            r["_volume"] = ""
            r["_shelf"] = (r.get("category") or "").strip().splitlines()[-1] if r.get("category") else ""
    return data


def decode_books_sorts(path: str):
    """``books.sorts`` 是个 ZIP，里面只有 ``shelf.options``。"""
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(open(path, "rb").read()))
    out = {}
    for name in z.namelist():
        try:
            out[name] = json.loads(z.read(name).decode("utf-8"))
        except Exception:
            out[name] = z.read(name)
    return out


# ---------------------------------------------------------------- 批量装载

def load_positions(root: str, cache_file: str = None, use_cache: bool = True):
    """把 ``Cache/*.po`` 全读出来 → ``{卷标题(无扩展名): 位置信息}``。

    返回 ``(positions, meta)``；meta 里带 ``from_cache`` / ``elapsed_ms``。
    """
    import time
    cache_file = cache_file or LOCAL_CACHE
    cache_dir = os.path.join(root, "Cache")

    # 靠 (文件名, mtime, size) 三元组判断本地缓存是否还能用
    try:
        entries = sorted(os.listdir(cache_dir))
    except OSError as e:
        return {}, {"error": "读不到 Cache 目录: %s" % e, "from_cache": False}

    stamp = {f: None for f in entries}
    for f in entries:
        try:
            st = os.stat(os.path.join(cache_dir, f))
            stamp[f] = [int(st.st_mtime), st.st_size]
        except OSError:
            stamp[f] = None

    if use_cache and os.path.isfile(cache_file):
        try:
            cached = json.load(open(cache_file, encoding="utf-8"))
            if cached.get("stamp") == stamp:
                return cached["positions"], {"from_cache": True, "elapsed_ms": 0.0,
                                             "count": len(cached["positions"])}
        except Exception:
            pass

    t0 = time.perf_counter()
    positions = {}
    for f in entries:
        if not f.lower().endswith(".po"):
            continue
        try:
            text = open(os.path.join(cache_dir, f), "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        pos = parse_position(text)
        if pos is None:
            continue
        # 文件名 = <卷标题>.<ext>.po → 键用「卷标题」
        title = os.path.splitext(os.path.splitext(f)[0])[0]
        pos["ext"] = os.path.splitext(os.path.splitext(f)[0])[1].lower()
        pos["raw"] = text
        positions[title] = pos
    elapsed = (time.perf_counter() - t0) * 1000

    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stamp": stamp, "positions": positions}, fh, ensure_ascii=False)
        os.replace(tmp, cache_file)
    except OSError:
        pass

    return positions, {"from_cache": False, "elapsed_ms": elapsed, "count": len(positions)}


def load_covers(root: str):
    """``Cover/*.png`` → ``{卷标题: 文件名}``。"""
    cover_dir = os.path.join(root, "Cover")
    out = {}
    try:
        for f in os.listdir(cover_dir):
            m = re.match(r"^(?P<title>.+)_(?P<variant>\d+)\.png$", f)
            if m:
                out[m.group("title")] = f
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 关联书库

def join_with_library(positions, library_dir=None):
    """把 ``.po`` 的卷标题关联到 OPDS 书库（``分类/书名/.../卷.epub``）。

    关联键 = **卷文件名去扩展名** —— Moon+ 的 ``.po`` 就是按文件名字符串命名的，
    所以书库里一旦批量改名，关联即断（见报告「兼容性」一节）。
    """
    library_dir = library_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "轻小说")
    index = {}
    for cat in sorted(os.listdir(library_dir)):
        cdir = os.path.join(library_dir, cat)
        if not os.path.isdir(cdir) or cat.startswith("."):
            continue
        for dp, _dn, fns in os.walk(cdir):
            for f in fns:
                if not f.lower().endswith(".epub"):
                    continue
                rel = os.path.relpath(os.path.join(dp, f), library_dir).replace("\\", "/")
                book = rel.split("/")[1]
                index.setdefault(os.path.splitext(f)[0], []).append((cat, book, rel))

    matched = {t: index[t] for t in positions if t in index}
    per_book = collections.defaultdict(list)
    for title, entries in matched.items():
        for cat, book, _rel in entries:
            per_book[(cat, book)].append((title, positions[title]["percent"]))

    summary = []
    for (cat, book), vols in per_book.items():
        bdir = os.path.join(library_dir, cat, book)
        total = sum(1 for _dp, _dn, fns in os.walk(bdir)
                    for f in fns if f.lower().endswith(".epub"))
        done = sum(1 for _t, p in vols if p >= 100)
        summary.append({
            "category": cat, "book": book,
            "volumes_read": len(vols), "volumes_finished": done,
            "volumes_total": total, "max_percent": max(p for _t, p in vols),
            "all_finished": bool(total) and done >= total,
        })
    summary.sort(key=lambda r: (-r["volumes_finished"], r["book"]))
    return {
        "matched": len(matched),
        "unmatched": sorted(set(positions) - set(index)),
        "books": summary,
    }


# ---------------------------------------------------------------- CLI

def collect(root=DEFAULT_ROOT, use_cache=True):
    sync_path = os.path.join(root, "books.sync")
    out = {"root": root}
    try:
        out["books"] = decode_books_sync(sync_path)
    except Exception as e:
        out["books"] = []
        out["sync_error"] = str(e)
    try:
        out["sorts"] = decode_books_sorts(os.path.join(root, "books.sorts"))
    except Exception as e:
        out["sorts"] = {}
        out["sorts_error"] = str(e)
    for fn, key in (("books.id", "device_id"), ("covers.id", "covers_id")):
        try:
            out[key] = open(os.path.join(root, fn), "rb").read().decode("ascii").strip()
        except OSError:
            out[key] = ""
    out["positions"], out["pos_meta"] = load_positions(root, use_cache=use_cache)
    out["covers"] = load_covers(root)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Moon+ 云端缓存只读解析器")
    ap.add_argument("--root", default=DEFAULT_ROOT, help=".Moon+ 目录")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存，强制重读网盘")
    ap.add_argument("--join", action="store_true", help="关联本地 OPDS 书库并统计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)

    data = collect(a.root, use_cache=not a.no_cache)
    books = data["books"]
    pos = data["positions"]
    meta = data["pos_meta"]

    if a.json:
        payload = {k: v for k, v in data.items() if k != "positions"}
        payload["positions"] = pos
        if a.join:
            payload["join"] = join_with_library(pos)
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    def out(s=""):
        sys.stdout.write(s + "\n")

    out("Moon+ 缓存根目录: %s" % data["root"])
    out("  device_id = %s   covers_id = %s" % (data.get("device_id"), data.get("covers_id")))
    out("  shelf.options = %s" % (data.get("sorts") or {}))
    out("")
    out("books.sync : %d 条书目" % len(books))
    if books:
        shelves = collections.Counter(r.get("_shelf") or "(空)" for r in books)
        ext = collections.Counter(os.path.splitext(r.get("filename", ""))[1].lower() for r in books)
        series = len({r["_series"] for r in books if r.get("_series")})
        out("  书架分类: %s" % dict(shelves))
        out("  文件类型: %s" % dict(ext))
        out("  带 <系列>#卷号# 结构的: %d 条，共 %d 个系列"
            % (sum(1 for r in books if r.get("_series")), series))
        out("  有简介的: %d 条；favorite/rate/分组 均未使用"
            % sum(1 for r in books if r.get("description")))
        cloud = [r for r in books if r.get("downloadUrl")]
        out("  downloadUrl 非空（仅云端、本地无文件）: %d 条" % len(cloud))
    out("")
    out("Cache/*.po : %d 条位置记录%s"
        % (len(pos), "（命中本地缓存）" if meta.get("from_cache")
           else "（本次重读网盘，耗时 %.0f ms）" % meta.get("elapsed_ms", 0)))
    if pos:
        devs = collections.Counter(p["device_id"] for p in pos.values())
        out("  设备分布: %s" % dict(devs))
        out("  区间: 0%%=%d  100%%=%d  中间=%d"
            % (sum(1 for p in pos.values() if p["percent"] == 0),
               sum(1 for p in pos.values() if p["percent"] >= 100),
               sum(1 for p in pos.values() if 0 < p["percent"] < 100)))
    out("")
    out("Cover/*.png: %d 张封面" % len(data["covers"]))

    if a.join:
        j = join_with_library(pos)
        out("")
        out("== 与本地书库关联 ==")
        out("  命中 %d / %d 条位置记录" % (j["matched"], len(pos)))
        if j["unmatched"]:
            out("  未命中（书库改名 / 不在书库内）:")
            for t in j["unmatched"][:20]:
                out("    - %s" % t)
        books = j["books"]
        full = [r for r in books if r["all_finished"]]
        out("  涉及 %d 部作品；「全部卷均 100%%」的 %d 部" % (len(books), len(full)))
        for r in full:
            out("    ✅ %s/%s（%d/%d 卷）"
                % (r["category"], r["book"], r["volumes_finished"], r["volumes_total"]))
        out("  进度最高的 15 部:")
        for r in sorted(books, key=lambda x: -x["max_percent"])[:15]:
            out("    %5.1f%%  %d/%d 卷已读（%d 卷 100%%）  %s/%s"
                % (r["max_percent"], r["volumes_read"], r["volumes_total"],
                   r["volumes_finished"], r["category"], r["book"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
