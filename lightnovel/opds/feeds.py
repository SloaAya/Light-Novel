#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPDS 表示层：Atom（导航型/获取型）feed 与面向浏览器的 HTML 视图。"""

import hashlib
import html
import re

from datetime import datetime
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from ..paths import (
    CATEGORY_DIRS,
    CATEGORY_DONE,
    CATEGORY_ONGOING,
    EPUB_MIME,
    OPDS_ACQ_TYPE,
    OPDS_NAV_TYPE,
    PAGE_SIZE,
    RECENT_SIZE,
    SERVER_AUTHOR,
    SERVER_TITLE,
)
from .library import (
    _safe_relpath,
    all_vols,
    encode_path,
    get_epub_meta,
    get_library,
    human_size,
)
from .finished import load_finished

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


def feed_root(is_admin=False):
    """根导航。``is_admin`` 为假时「已读完」这个 subsection **不会出现**在 feed 里 ——
    阅读器（Moon+ / 静读天下）订阅到的目录结构与访客网页一致，不多一个入口。"""
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
    if is_admin:
        n_fin = len(_finished_books())
        entries.append(nav_entry("urn:ln:read", "已读完", "/opds/read", f"已读完的 {n_fin} 部作品"))
    return _feed("urn:ln:root", SERVER_TITLE, entries, "/")


def _finished_books():
    """已读完、且**当前书库里确实存在**的作品：``[(key, 分类, 书名, [卷…])]``。

    清单里可能留着已被删除/改名的旧键，这里统一过滤掉 —— 列表页与 feed 都不会
    出现点不开的死条目（也不在读取时顺手清理文件，删书是可逆的，静默丢标记更糟）。
    """
    lib = get_library()
    out = []
    for key in sorted(load_finished(), key=lambda s: s.lower()):
        cat, _, book = key.partition("/")
        vols = lib.get(cat, {}).get(book)
        if vols:
            out.append((key, cat, book, vols))
    return out


def feed_finished(page=1):
    """「已读完」导航型 feed：每条指向一部作品的详情 feed（管理员专有）。"""
    items = _finished_books()
    chunk, extra = _paginate(items, page, "/opds/read?page=1")
    body = "".join(
        nav_entry("urn:ln:book:" + quote(key), book, "/opds/book/" + encode_path(key),
                  f"{cat} · {len(vols)} 卷 · 已读完")
        for key, cat, book, vols in chunk)
    return _feed("urn:ln:read", f"{SERVER_TITLE} · 已读完", [],
                 "/opds/read?page=" + str(page), OPDS_NAV_TYPE, extra + body)


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
.card{display:block;position:relative}
.cardlink{display:block}
.card .ph{position:relative;width:100%;aspect-ratio:2/3;border-radius:10px;overflow:hidden;
  background:var(--border);box-shadow:var(--shadow);transition:transform .14s,box-shadow .14s}
.card:hover .ph{transform:translateY(-4px);box-shadow:0 8px 18px rgba(16,22,26,.12)}
.card .ph img{width:100%;height:100%;object-fit:cover}
.card .ph .badge{position:absolute;right:6px;top:6px;padding:2px 8px;border-radius:20px;
  background:rgba(15,20,26,.72);color:#fff;font-size:10px;font-weight:500;backdrop-filter:blur(6px)}
.card .t{margin-top:9px;font-size:13px;font-weight:500;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .s{margin-top:3px;font-size:11px;color:var(--muted)}
.card .s .fin{color:#1a7f37;font-weight:500}

/* 标记控件 .mkform/.mk：只在管理员页面渲染（见 _mark_form）。这里刻意不写任何
   带功能名的注释 —— CSS 对所有人下发，注释里的字样会漏进访客的页面源码里。 */
.mkform{position:absolute;left:6px;top:6px;margin:0;z-index:2;line-height:0}
.mk{width:26px;height:26px;padding:0;border-radius:50%;cursor:pointer;
  display:grid;place-items:center;font-size:13px;line-height:1;font-family:inherit;
  border:1px solid rgba(255,255,255,.55);background:rgba(15,20,26,.55);color:#fff;
  backdrop-filter:blur(6px);transition:background .12s,transform .12s,border-color .12s}
.mk:hover{transform:scale(1.08);background:rgba(15,20,26,.75)}
.mk.on{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.mk.on:hover{background:var(--accent2)}
.actions form{display:inline;margin:0}
.actions button.dl{font-family:inherit;cursor:pointer;border:0}
.dl.ok{background:#1f883d;color:#fff;border:1px solid #1f883d}
.dl.ok:hover{filter:brightness(1.06)}

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


def _html_page(title, body_inner, active="", extra_css="", is_admin=False):
    """整页外壳。``is_admin`` 是**渲染期开关**：非管理员时「已读完」入口这段 HTML
    根本不会被拼出来 —— 前端拿到的是「没有这个入口」的页面，而不是「用 CSS 藏起来」
    的页面（CSS 隐藏可以用查看源码/开发者工具还原，等于没有权限控制）。

    真正的权限闸门在服务端路由（``OPDSHandler`` 的 ``_role()`` 与 403 分支）；
    这里只负责「不给入口」，属于体验层，两道一起才叫完整。
    """
    tabs = [
        ("done",    "/opds/catalog/" + quote(CATEGORY_DONE),    CATEGORY_DONE),
        ("ongoing", "/opds/catalog/" + quote(CATEGORY_ONGOING), CATEGORY_ONGOING),
        ("recent",  "/opds/recent",   "最近更新"),
        ("all",     "/opds/catalog/all", "全部作品"),
    ]
    if is_admin:
        tabs.append(("read", "/opds/read", "已读完"))
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


def _mark_form(key, back, finished):
    """「标记 / 取消已读完」表单 —— 纯 HTML 表单 POST，不依赖 JS。

    用表单而不是 ``fetch()``：手机上 JS 被禁、阅读器内嵌浏览器兼容性参差都能用；
    提交后服务端 303 跳回 ``back``，页面重绘时状态必然正确（不用前端自己记账）。
    只有管理员渲染到这里（``is_admin`` 为真时才调用）。
    """
    on = " on" if finished else ""
    tip = "已读完，点击取消标记" if finished else "标记为已读完"
    glyph = "&#10003;" if finished else "&#9675;"
    esc = lambda s: html.escape(s, quote=True)          # noqa: E731
    return (
        '<form class="mkform" method="post" action="/opds/read/toggle">'
        f'<input type="hidden" name="key" value="{esc(key)}">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button class="mk{on}" type="submit" title="{tip}" aria-label="{tip}">{glyph}</button>'
        "</form>")


def _book_card(href, cover_rel, title, sub, badge=None, key=None, back="",
               finished=False, fin_note=""):
    """一张书卡。``key`` 非空（= 管理员视角）时封面左上角挂「标记已读完」按钮。"""
    badge_html = f'<span class="badge">{html.escape(badge)}</span>' if badge else ""
    mark = _mark_form(key, back, finished) if key else ""
    sub_html = html.escape(sub) + (f' · <span class="fin">{html.escape(fin_note)}</span>'
                                  if fin_note else "")
    return (
        '<div class="card">'
        f'<a class="cardlink" href="{href}">'
        f'<div class="ph"><img src="{_cover_url(cover_rel)}" alt="" loading="lazy">{badge_html}</div>'
        f'<div class="t">{html.escape(title)}</div>'
        f'<div class="s">{sub_html}</div></a>'
        f"{mark}</div>")


# 书的「主卷」优选名：多子目录时（如 High School D×D 同时有 正篇/DX/短篇/SLASHDOG），
# 按此顺序挑子目录的第一卷作为书封/hero 封面，避免取到副刊/外传的低质量封面
_PRIMARY_SUBDIR_PREFERENCE = ["正篇", "本篇", "主线", "main", "series"]


def _primary_vol(vols, cat, book):
    """从书的卷列表里挑一卷作为「书级封面」：多子目录时按 _PRIMARY_SUBDIR_PREFERENCE 优先，
    再退到「与书名最相似的子目录」，最后按字母序挑一个。单子目录/无子目录直接返回第一卷。
    注：根目录的散文件（空子目录）不参与"多子目录"判定，被视为附属内容。"""
    if not vols:
        return None
    inner = f"{cat}/{book}/"
    # 把卷按子目录分组（保留 book 内目录相对路径）
    groups = {}
    for v in vols:
        if v["rel"].startswith(inner):
            rem = v["rel"][len(inner):]
        else:
            rem = v["rel"].split("/")[-1]
        subdir = rem.rsplit("/", 1)[0] if "/" in rem else ""
        groups.setdefault(subdir, []).append(v)
    # 只看「真子目录」，根目录的散文件不参与多子目录判定
    real_subs = {k: v for k, v in groups.items() if k}
    if len(real_subs) <= 1:
        return vols[0]
    # 1) 优选名命中
    for name in _PRIMARY_SUBDIR_PREFERENCE:
        if name in real_subs:
            return real_subs[name][0]
    # 2) 子目录名里包含书名（或书名包含子目录名）→ 通常正篇沿用书名
    for sub in real_subs:
        if sub in book or book in sub:
            return real_subs[sub][0]
    # 3) 子目录名是书名的前缀（≥2 字）→ 容错处理「为美好的世界献上祝福」+ 「为美好的世界献上祝福！」
    for sub in real_subs:
        if len(book) >= 2 and (book[:2] in sub or sub[:2] in book):
            return real_subs[sub][0]
    # 4) 都没命中：按中英文排序挑第一个（保证确定，不依赖文件系统顺序）
    first = sorted(real_subs.keys(), key=lambda s: s.lower())[0]
    return real_subs[first][0]


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


def root_html(is_admin=False):
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
    quick = (
        f'<a class="cat" href="/opds/recent"><div class="ico" style="background:#e7f3ff;color:#0a66c2">&#128336;</div>'
        f'<div class="nm">最近更新</div><div class="ds">最近改动的 {RECENT_SIZE} 卷</div><div class="go">查看 →</div></a>'
        f'<a class="cat" href="/opds/catalog/all"><div class="ico" style="background:#f0e7ff;color:#6639ba">&#128218;</div>'
        f'<div class="nm">全部作品</div><div class="ds">不分分类浏览</div><div class="go">查看 →</div></a>'
    )
    quick_label = "最近更新 · 全部作品"
    if is_admin:                       # 管理员才多这张卡（与顶栏 tab 同一开关）
        n_fin = len(_finished_books())
        quick += (
            f'<a class="cat" href="/opds/read"><div class="ico" style="background:#dafbe1;color:#1a7f37">&#10003;</div>'
            f'<div class="nm">已读完</div><div class="ds">已标记 {n_fin} 部作品</div><div class="go">查看 →</div></a>')
        quick_label = "最近更新 · 全部作品 · 已读完"
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
        f"<h2>快速入口 <span class=\"n\">{quick_label}</span></h2>"
        f'<div class="cats">{quick}</div>'
    )
    return _html_page(SERVER_TITLE, body, active="", is_admin=is_admin)


def catalog_html(cat, page=1, is_admin=False):
    lib = get_library()
    fin = load_finished() if is_admin else set()
    if cat == "all":
        merged = {}
        for c, bs in lib.items():
            for b, vols in bs.items():
                merged[f"{c}/{b}"] = (c, vols)
        keys = sorted(merged.keys())
        chunk, extra = _paginate(keys, page, "/opds/catalog/all?page=1")
        back = "/opds/catalog/all?page=" + str(page)
        cards = "".join(
            _book_card("/opds/book/" + encode_path(k),
                       _primary_vol(merged[k][1], *k.split("/", 1))["rel"],
                       k.split("/", 1)[1],
                       f"{merged[k][0]} · {len(merged[k][1])} 卷",
                       key=k if is_admin else None, back=back, finished=k in fin,
                       fin_note="已读完" if k in fin else "")
            for k in chunk)
        total = len(keys)
        label = "全部作品"
        active = "all"
    elif cat in CATEGORY_DIRS:
        books = lib.get(cat, {})
        keys = sorted(books.keys(), key=lambda s: s.lower())
        chunk, extra = _paginate(keys, page, "/opds/catalog/" + quote(cat) + "?page=1")
        back = "/opds/catalog/" + quote(cat) + "?page=" + str(page)
        cards = "".join(
            _book_card("/opds/book/" + encode_path(cat + "/" + b),
                       _primary_vol(books[b], cat, b)["rel"],
                       b, f"{len(books[b])} 卷", badge=f"{len(books[b])} 卷",
                       key=f"{cat}/{b}" if is_admin else None, back=back,
                       finished=f"{cat}/{b}" in fin,
                       fin_note="已读完" if f"{cat}/{b}" in fin else "")
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
    return _html_page(f"{label} · {SERVER_TITLE}", body, active=active, is_admin=is_admin)


def book_html(rel, page=1, is_admin=False):               # page 参数保留以兼容旧 URL，详情页不再分页
    rel = _safe_relpath(rel)
    if not rel:
        return None
    parts = rel.split("/")
    cat = parts[0]
    book = parts[1] if len(parts) > 1 else ""
    vols = get_library().get(cat, {}).get(book)
    if vols is None:
        return None
    key = f"{cat}/{book}"
    finished = bool(is_admin) and key in load_finished()
    total_size = sum(v["size"] for v in vols)
    primary = _primary_vol(vols, cat, book)               # hero 封面也用「主卷」
    meta = get_epub_meta(primary["rel"]) if primary else {}
    author = meta.get("creator", "")
    desc = meta.get("description", "")
    zip_url = "/zip/" + encode_path(rel)

    meta_lines = (
        f'<div class="meta-line">作者 <b>{html.escape(author)}</b></div>' if author else "")
    meta_lines += f'<div class="meta-line">分类 <b>{html.escape(cat)}</b> · 卷数 <b>{len(vols)}</b> · 体积 <b>{human_size(total_size)}</b></div>'
    if is_admin:                       # 访客拿到的详情页里连「阅读状态」这一行都没有
        meta_lines += ('<div class="meta-line">阅读状态 <b style="color:#1a7f37">已读完</b></div>'
                       if finished else
                       '<div class="meta-line">阅读状态 <b>未读</b></div>')

    desc_html = f'<div class="desc">{html.escape(desc)}</div>' if desc else ""

    # 详情页一次性渲染全部卷（不翻页）：直接传完整 vols 列表，_render_groups 会显示所有分组
    groups = _group_vols_by_subdir(vols, cat, book)
    groups_html = _render_groups(groups, cat, book, vols)

    # 标记按钮：管理员才有；已读时时样式换成实心绿并提示可取消
    mark_btn = ""
    if is_admin:
        mark_btn = (
            '<form method="post" action="/opds/read/toggle">'
            f'<input type="hidden" name="key" value="{html.escape(key, quote=True)}">'
            f'<input type="hidden" name="back" value="/opds/book/{html.escape(encode_path(rel), quote=True)}">'
            f'<button class="dl big{" ok" if finished else " ghost"}" type="submit">'
            + ("&#10003; 已读完（点击取消）" if finished else "标记为已读完")
            + "</button></form>")

    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span>'
        f'<a href="/opds/catalog/{quote(cat)}">{html.escape(cat)}</a><span>/</span>'
        f'<span>{html.escape(book)}</span></div>'
        '<div class="hero">'
        f'<div class="ph"><img src="{_cover_url(primary["rel"])}" alt=""></div>'
        '<div class="info">'
        f"<h1>{html.escape(book)}</h1>"
        + meta_lines + desc_html +
        '<div class="actions">'
        f'<a class="dl big" href="{zip_url}">⬇ 打包下载全部（{len(vols)} 卷 · {human_size(total_size)}）</a>'
        + mark_btn +
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
    return _html_page(f"{book} · {SERVER_TITLE}", body, active=active, is_admin=is_admin)


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


def recent_html(page=1, is_admin=False):
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
    return _html_page(f"最近更新 · {SERVER_TITLE}", body, active="recent", is_admin=is_admin)


def search_html(q, page=1, is_admin=False):
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
    return _html_page(f"搜索 · {SERVER_TITLE}", body, active="", is_admin=is_admin)


def read_html(page=1):
    """「已读完」列表页（**仅管理员**：服务端在路由层就会把访客挡在外面，
    非管理员拿不到这个页面，也拿不到顶栏入口）。

    管理动作只有「取消标记」一个 —— 清单是人工维护的小状态，不做批量清空：
    误点一下只是少一条记录，不需要「不可逆」的操作来增加风险。
    """
    items = _finished_books()
    chunk, extra = _paginate(items, page, "/opds/read?page=1")
    back = "/opds/read?page=" + str(page)
    cards = "".join(
        _book_card("/opds/book/" + encode_path(key),
                   _primary_vol(vols, cat, book)["rel"], book,
                   f"{cat} · {len(vols)} 卷", badge=f"{len(vols)} 卷",
                   key=key, back=back, finished=True, fin_note="已读完")
        for key, cat, book, vols in chunk)
    total = len(items)
    if total:
        listing = f'<div class="grid">{cards}</div>'
    else:
        listing = ('<div class="empty">还没有标记任何作品。<br>'
                   '去「已完结 / 未完结」里点封面左上角的 &#9675; 即可标记为已读完。</div>')
    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span><span>已读完</span></div>'
        '<div class="bar">'
        '<h1 style="margin:0">已读完</h1>'
        '<span class="spacer"></span>'
        f'<span class="sub" style="margin:0">{total} 部作品</span>'
        "</div>"
        f'<p class="sub">点封面左上角的 &#10003; 可取消标记。'
        f'（这份清单只保存在本机 <code>.autosync/finished.json</code>，不同步到书库/仓库）</p>'
        + listing
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"已读完 · {SERVER_TITLE}", body, active="read", is_admin=True)


# 兼容旧名
def index_html(is_admin=False):
    return root_html(is_admin=is_admin)




