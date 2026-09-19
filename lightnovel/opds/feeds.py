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
from . import moon as MOON
from . import updates as upd

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
        n_fin = len(_finished_all())          # 与网页同一口径（人工 ∪ 阅读器判定）
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


def _finished_all():
    """「已读完」全量清单：人工标记 ∪ 阅读器自动判定，一部作品只出现一次。

    为什么必须合并：``finished.json`` 可能是空的，而手机阅读器早就把整部书读完了。
    首页那张卡若只数人工清单，就会写着「0 部作品」，点进去却是满满一屏 —— 卡片与
    清单页自相矛盾。所以 **首页计数、清单页、OPDS feed 共用这一个口径**。

    返回 ``[(key, 分类, 书名, [卷…], 统计或 None)]``；``None`` = 人工标记（可取消），
    非 ``None`` 是阅读器判定（``moon.stat_of`` 的结果，带 percent 等字段）。
    """
    items = _finished_books()
    merged = [(key, cat, book, vols, None) for key, cat, book, vols in items]
    auto = MOON.auto_finished() if MOON.enabled() else {}
    if auto:
        manual = {key for key, _c, _b, _v in items}
        lib = get_library()
        for key, st in sorted(auto.items()):
            if key in manual:
                continue
            cat, book = key.split("/", 1)
            vols = lib.get(cat, {}).get(book)
            if vols:
                merged.append((key, cat, book, vols, st))
    return merged


def feed_finished(page=1):
    """「已读完」导航型 feed：每条指向一部作品的详情 feed（管理员专有）。

    清单口径与网页一致：人工标记 ∪ 阅读器自动判定。
    """
    items = _finished_all()
    chunk, extra = _paginate(items, page, "/opds/read?page=1")
    body = "".join(
        nav_entry("urn:ln:book:" + quote(key), book, "/opds/book/" + encode_path(key),
                  f"{cat} · {len(vols)} 卷 · 已读完")
        for key, cat, book, vols, _st in chunk)
    return _feed("urn:ln:read", f"{SERVER_TITLE} · 已读完", [],
                 "/opds/read?page=" + str(page), OPDS_NAV_TYPE, extra + body)


def _upd_suffix(pend, key):
    n = len(pend[key]["vols"]) if key in pend else 0
    return f" · 有更新 +{n} 卷" if n else ""


def feed_catalog(cat, page=1):
    lib = get_library()
    pend = upd.observe()
    if cat == "all":
        merged = {}
        for c, bs in lib.items():
            for b, vols in bs.items():
                merged[f"{c}/{b}"] = (c, len(vols))
        keys = upd.order_keys(sorted(merged.keys()), pend)      # 与网页版同一套排序
        chunk, extra = _paginate(keys, page, "/opds/catalog/all?page=1")
        body = "".join(
            nav_entry("urn:ln:book:" + quote(k), k.split("/", 1)[1], "/opds/book/" + encode_path(k),
                      f"{merged[k][0]} · {merged[k][1]} 卷" + _upd_suffix(pend, k))
            for k in chunk)
        return _feed("urn:ln:cat:all", f"{SERVER_TITLE} · 全部作品", [],
                     "/opds/catalog/all?page=" + str(page), OPDS_ACQ_TYPE, extra + body)
    if cat not in CATEGORY_DIRS:
        return None
    books = lib.get(cat, {})
    keys = upd.order_keys([f"{cat}/{k}" for k in sorted(books.keys(), key=lambda s: s.lower())], pend)
    chunk, extra = _paginate(keys, page, "/opds/catalog/" + quote(cat) + "?page=1")
    body = "".join(
        nav_entry("urn:ln:book:" + quote(k), k.split("/", 1)[1], "/opds/book/" + encode_path(k),
                  f"{len(books[k.split('/', 1)[1]])} 卷" + _upd_suffix(pend, k))
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
  --new:#bc4c00; --new-strip:rgba(188,76,0,.92);
  --shadow:0 1px 2px rgba(16,22,26,.06),0 2px 8px rgba(16,22,26,.05);
  --shadow-sm:0 1px 2px rgba(16,22,26,.05);
  --radius:12px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e1116; --card:#171c23; --text:#e8edf3; --muted:#9aa4b0; --border:#2c333d;
    --accent:#4c9df0; --accent2:#2f7fd0; --accent-fg:#0e1116; --hover:#1d2530;
    --new:#ff9d5c; --new-strip:rgba(150,58,0,.94);
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
/* 顶栏右上角的身份入口（登录 / 退出）。访客版只有「登录」两个字 —— 不写说明文案，
   管理员专有的字样不能出现在访客拿到的源码里（同 MARK_JS 的约定）。 */
.hacc{flex-shrink:0;margin:0;display:inline-flex;align-items:center;line-height:1.2;
  padding:7px 13px;border-radius:8px;border:1px solid var(--border);background:var(--card);
  color:var(--muted);font-size:13px;font-family:inherit;white-space:nowrap;cursor:pointer;
  transition:border-color .12s,color .12s,background .12s}
.hacc:hover{border-color:var(--accent);color:var(--accent);background:var(--hover)}
form.hacc{padding:0}form.hacc .hacbtn{padding:7px 13px;border:0;background:transparent;
  color:inherit;font:inherit;cursor:pointer;border-radius:8px}

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
/* 3 行而不是 2 行：本库最长书名 28 字，2 行（约 23 字）会把「想要确定真命天女之前，
   可以先拿我试试哦。」这类书名截成半截。 */
.card .t{margin-top:9px;font-size:13px;font-weight:500;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card .s{margin-top:3px;font-size:11px;color:var(--muted)}
.card .s .fin{color:#1a7f37;font-weight:500}
.card .s .newt{color:var(--new);font-weight:500}
/* 封面左下角的「有更新」条 —— 位置刻意避开左上角的操作按钮与右上角的卷数角标 */
.card .ph .upd{position:absolute;left:0;right:0;bottom:0;padding:4px 7px 3px;
  font-size:10px;font-weight:600;color:#fff;letter-spacing:.2px;
  background:linear-gradient(180deg,transparent,var(--new-strip) 62%);
  text-shadow:0 1px 2px rgba(0,0,0,.35)}
/* 有更新的卡再描一道暖色边。放在上面 .card:hover .ph 之后，否则悬停时描边会被覆盖掉。 */
.card.upd .ph{box-shadow:0 0 0 2px var(--new),var(--shadow)}
.card.upd:hover .ph{box-shadow:0 0 0 2px var(--new),0 8px 18px rgba(16,22,26,.12)}
/* 封面**右下角**的阅读进度角标（照手机阅读器的做法）。左上有标记按钮、右上是卷数、
   左下是「有更新」条，右下是唯一还空着的角，四者互不打架。 */
.card .ph .prog{position:absolute;right:6px;bottom:6px;min-width:34px;padding:3px 8px;
  border-radius:20px;text-align:center;background:rgba(15,20,26,.78);color:#fff;
  font-size:11px;font-weight:600;letter-spacing:.2px;backdrop-filter:blur(6px);
  font-variant-numeric:tabular-nums}
.card .ph .prog.pdone{background:rgba(26,127,55,.94)}
/* 有「有更新」条时角标抬到条上面（那条是左右贯通的，压着会看不清数字） */
.card.upd .ph .prog{bottom:25px}
.card .s .pnt{color:var(--muted)}

/* 标记控件 .mkform/.mk：只在管理员页面渲染（见 _mark_form）。这里刻意不写任何
   带功能名的注释 —— CSS 对所有人下发，注释里的字样会漏进访客的页面源码里。 */
.mkform{position:absolute;left:6px;top:6px;margin:0;z-index:2;line-height:0}
/* 静息态隐藏：opacity 归零 + 关掉指针事件 —— 不吞点击、不留残影。
   刻意**不用** visibility:hidden：那会让按钮无法被 Tab 聚焦，键盘用户就永远见不到它；
   只靠 opacity 才能做到「聚焦即显形」。 */
.mk{width:26px;height:26px;padding:0;border-radius:50%;cursor:pointer;
  display:grid;place-items:center;font-size:13px;line-height:1;font-family:inherit;
  border:1px solid rgba(255,255,255,.55);background:rgba(15,20,26,.55);color:#fff;
  backdrop-filter:blur(6px);opacity:0;pointer-events:none;
  transition:opacity .14s ease,background .12s,transform .12s,border-color .12s}
/* 只在**鼠标悬停整张卡**、或**按钮自身被键盘聚焦**时显形。
   刻意**不用** `.card:focus-within`：点卡片链接进详情页后按返回，浏览器会恢复那个链接
   的焦点，`:focus-within` 会一直成立 → 圆圈在没悬停时常驻不散。 */
.card:hover .mk,.mk:focus-visible{opacity:1;pointer-events:auto}
.mk:hover{transform:scale(1.08);background:rgba(15,20,26,.75)}
.mk.on{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.mk.on:hover{background:var(--accent2)}
/* 触屏设备没有 hover：若也藏起来，手机上就永远点不到标记 —— 这类设备保持常驻。 */
@media (hover:none){.mk{opacity:1;pointer-events:auto}}
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
/* 分组行上的「有更新 +N」药丸 + 新增卷行上的「新」标 */
.group-head .gnew{font-size:11px;font-weight:600;color:#fff;background:var(--new);
  padding:2px 8px;border-radius:20px;flex-shrink:0}
.vol .meta .s .vnew{font-size:10.5px;font-weight:600;color:#fff;background:var(--new);
  padding:1px 6px;border-radius:20px;flex-shrink:0}
/* 分卷行上的阅读进度药丸（来自手机阅读器的位置数据；属于「已读过」而非管理功能） */
.vol .meta .s .vp{font-size:10.5px;font-weight:600;color:var(--accent);
  background:var(--hover);padding:1px 7px;border-radius:20px;flex-shrink:0;
  font-variant-numeric:tabular-nums}
.vol .meta .s .vp.done{color:#1a7f37;background:rgba(26,127,55,.13)}
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
.bar .barupd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0}
.bar .barupd .cnt{font-size:12.5px;color:var(--new);font-weight:500}

/* ---------- 详情页「本次新增」区块 ---------- */
.updbox{margin:0 0 22px;padding:15px 17px;border:1px solid var(--new);
  border-radius:var(--radius);background:var(--card);box-shadow:var(--shadow)}
.updbox .hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-size:14px;font-weight:600;margin-bottom:11px}
.updbox .hd .dot{width:8px;height:8px;border-radius:50%;background:var(--new);flex-shrink:0}
.updbox .hd .n{font-weight:400;font-size:12px;color:var(--muted)}
.updbox .vol{margin-bottom:6px}
.updbox .vol:last-child{margin-bottom:0}
.updbox .vol .meta .s .sub2{color:var(--new)}

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
  /* 窄屏第一行：品牌 + 身份入口（搜索另起一行，见上面的 order） */
  .hacc{order:1;margin-left:auto;padding:6px 11px;font-size:12.5px}
  form.hacc .hacbtn{padding:6px 11px;font-size:12.5px}
  .tabs{order:3;flex:0 0 100%;overflow-x:auto;scrollbar-width:none;
    white-space:nowrap;margin:2px -4px 0;padding:0 4px}
  .tabs::-webkit-scrollbar{display:none}
  .tab{padding:7px 11px;font-size:13px;flex-shrink:0}
  .tab.on::after{display:none}
  /* 内容区 */
  .grid{grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:14px 10px}
  .card .t{font-size:12px;line-height:1.3;-webkit-line-clamp:3}
  .card .ph .badge{font-size:10px;padding:2px 7px}
  .card .ph .upd{font-size:9px;padding:3px 6px 2px}
  .updbox{padding:12px 13px;margin-bottom:16px}
  .updbox .hd{font-size:13px;margin-bottom:9px}
  .group-head .gnew{font-size:10px;padding:1px 7px}
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

/* ---------- 详情页右下角的常驻阅读进度指示器 ---------- */
/* 圆环用 conic-gradient 画（--p 是 0-100 的百分比），不需要 JS 也不需要 SVG。
   注意 position:fixed 的元素不能放进 overflow 容器里，所以它挂在 main 之外。 */
.pfab{position:fixed;right:18px;bottom:18px;z-index:40;display:flex;align-items:center;gap:10px;
  padding:9px 15px 9px 10px;border-radius:30px;background:var(--card);
  border:1px solid var(--border);box-shadow:0 4px 16px rgba(16,22,26,.14);
  font-size:12px;line-height:1.3;pointer-events:none}
.pfab .pring{width:34px;height:34px;border-radius:50%;flex-shrink:0;display:grid;place-items:center;
  background:conic-gradient(var(--accent) calc(var(--p) * 1%), var(--border) 0)}
.pfab .pring i{width:26px;height:26px;border-radius:50%;background:var(--card);display:grid;
  place-items:center;font-style:normal;font-size:10px;font-weight:600;color:var(--text);
  font-variant-numeric:tabular-nums}
.pfab.pdone .pring{background:conic-gradient(#1a7f37 calc(var(--p) * 1%), var(--border) 0)}
.pfab .ptxt{display:flex;flex-direction:column;min-width:0}
.pfab .ptxt b{font-weight:500;color:var(--text);white-space:nowrap}
.pfab .ptxt span{color:var(--muted);font-size:11px;white-space:nowrap}
@media (prefers-color-scheme:dark){
  .pfab.pdone .pring{background:conic-gradient(#4ac26b calc(var(--p) * 1%), var(--border) 0)}
  .vol .meta .s .vp.done{color:#4ac26b;background:rgba(74,194,107,.14)}
}
@media (max-width:640px){
  .pfab{right:10px;bottom:10px;padding:7px 12px 7px 8px;gap:8px}
  .pfab .pring{width:30px;height:30px}
  .pfab .pring i{width:23px;height:23px;font-size:9.5px}
  .pfab .ptxt span{display:none}
}
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%230a66c2'/%3E"
           "%3Cpath d='M9 8h7v16H9z' fill='white' opacity='.95'/%3E"
           "%3Cpath d='M17.5 8h5.5v16h-5.5z' fill='white' opacity='.6'/%3E%3C/svg%3E")


# 「已读完」原地生效的增强层 —— **只发给管理员**（见 _html_page）。
# 访客页面连这段脚本都没有：他没有标记按钮，脚本无事可做，而脚本里的「已读完」字样
# 会漏进访客的页面源码里 —— 项目里有专门的断言在盯这件事（CSS 同理）。
#
# 设计要点：
#   ① 拦 submit 而不是 click —— 键盘回车、辅助设备触发的提交走的是同一条路；
#   ② 服务端回的是**它自己算出来的**最终状态，前端只负责照抄，两边不会漂；
#   ③ 两条失败路径要分开处理，别混成一个 catch：
#      「请求没送达」→ 退回原生 submit（此时状态还没变，重提交是安全的）；
#      「送达了但本地回写出错」→ 重取本页重画，**绝不能**再 submit 一次
#      （那会把刚写下的状态翻回去，用户看到的就是「点了没用」）；
#   ④ 旧浏览器没有 fetch / FormData → 压根不拦，直接原生提交；
#   ⑤ 「清空有更新」会改变排序（有新卷的书要回到原位），本地补不动 →
#      重新取当前页 HTML，只换 <main> 的内容：服务端算顺序，滚动位置不动、不白屏。
MARK_JS = """
(function(){
  var GLYPH_ON = "\\u2713", GLYPH_OFF = "\\u25CB";
  var TIP_ON = "已读完，点击取消标记", TIP_OFF = "标记为已读完";
  var FIN = "已读完";
  var FIN_TOP = '<span class="fin">' + FIN + '</span> &middot; ';
  var FIN_TAIL = ' &middot; <span class="fin">' + FIN + '</span>';

  /* 去掉元素连带它前面的 " · " 分隔符，别在副标题里留下孤零零的点 */
  function sepRemove(el){
    var p = el.previousSibling;
    if(p && p.nodeType === 3){ p.nodeValue = p.nodeValue.replace(/\\s*\\u00B7\\s*$/, ""); }
    el.remove();
  }

  /* 副标题里的「已读完」跟着变：圆圈只负责操作，文字才是常驻的状态 */
  function setFinNote(card, on){
    var s = card.querySelector(".s");
    if(!s){ return; }
    var fin = s.querySelector(".fin"), newt = s.querySelector(".newt");
    if(on){
      if(fin){ return; }
      if(newt){ newt.insertAdjacentHTML("beforebegin", FIN_TOP); }   /* 排在「有更新」前面 */
      else { s.insertAdjacentHTML("beforeend", FIN_TAIL); }
    } else if(fin){
      sepRemove(fin);
    }
  }

  /* 「已读完」列表里取消标记 → 这张卡自己就不该留着，顺手把计数减 1。
     计数取自 data-total 而不是数当前页的卡：分页时两者不相等。 */
  function dropFromRead(card){
    var grid = card.parentNode;
    card.style.transition = "opacity .16s ease";
    card.style.opacity = "0";
    setTimeout(function(){
      card.remove();
      var total = Math.max(0, (parseInt(grid.getAttribute("data-total"), 10) || 0) - 1);
      grid.setAttribute("data-total", String(total));
      var cnt = document.getElementById("readcnt");
      if(cnt){ cnt.textContent = total + " 部作品"; }
      if(grid.querySelector(".card")){ return; }
      if(total > 0){
        /* 本页空了但别处还有 → 回「已读完」第 1 页。地址取自顶栏那个当前选中的标签页，
           脚本里不留写死的路由常量（换路由时不用改 JS，也不会把路由字样漏进别处）。 */
        var tab = document.querySelector(".tabs a.on");
        location.href = tab ? tab.getAttribute("href") : location.href;
        return;
      }
      var tpl = document.getElementById("readempty");
      if(tpl){ grid.outerHTML = tpl.innerHTML; }
    }, 170);
  }

  function patchCard(f, on){
    var card = f.closest(".card");
    if(!card){ return; }
    var mk = f.querySelector(".mk");
    if(mk){
      mk.classList.toggle("on", on);
      mk.textContent = on ? GLYPH_ON : GLYPH_OFF;
      var tip = on ? TIP_ON : TIP_OFF;
      mk.title = tip;
      mk.setAttribute("aria-label", tip);   /* 别写成 mk.title = mk.getAttribute(...) = tip：
                                               赋值目标不能是函数调用，那是 ReferenceError */
    }
    setFinNote(card, on);
    if(!on && f.getAttribute("data-list") === "read"){ dropFromRead(card); }
  }

  /* 详情页：按钮换成实心绿 + 阅读状态那一行同步（不重绘整页，分组的展开状态就不会被重置） */
  function patchHero(f, on){
    var b = f.querySelector("button");
    if(b){
      b.classList.toggle("ok", on);
      b.classList.toggle("ghost", !on);
      b.textContent = on ? GLYPH_ON + " 已读完（点击取消）" : "标记为已读完";
    }
    var st = document.getElementById("finstate");
    if(st){
      st.textContent = on ? FIN : "未读";
      st.style.color = on ? "#1a7f37" : "";
    }
  }

  /* 只换 <main> 内容：不刷新页面（滚动位置、历史记录、分组展开都还在），
     顺序由服务端重新算 —— 前端不必知道「哪本书该排到第几位」。 */
  function softReload(){
    if(!window.fetch){ location.reload(); return; }
    fetch(location.href, {credentials: "same-origin", cache: "no-store",
                          headers: {"Accept": "text/html"}})
      .then(function(r){ return r.text(); })
      .then(function(h){
        var src = new DOMParser().parseFromString(h, "text/html").querySelector("main");
        var cur = document.querySelector("main");
        if(src && cur){ cur.innerHTML = src.innerHTML; }
      })
      .catch(function(){ location.reload(); });
  }

  document.addEventListener("submit", function(e){
    var f = e.target;
    if(!f || !f.classList || f.getAttribute("data-busy") === "1"){ return; }
    var kind = f.classList.contains("mkform") ? "card"
             : f.classList.contains("mkbig")  ? "hero"
             : f.classList.contains("barupd") ? "bar" : "";
    if(!kind || !window.fetch || !window.FormData){ return; }   /* 不拦 → 原生提交兜底 */
    e.preventDefault();
    f.setAttribute("data-busy", "1");      /* 传完之前再点不重复提交（防连点来回翻） */
    fetch(f.action, {
      method: "POST",
      body: new URLSearchParams(new FormData(f)),   /* 服务端只吃 urlencoded，别用 multipart */
      credentials: "same-origin",
      headers: {"X-Requested-With": "fetch"}        /* 跨站带这个头会先被 CORS 预检挡下 */
    }).then(function(r){
      if(!r.ok){ throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function(d){
      f.removeAttribute("data-busy");
      try{
        if(kind === "bar"){ softReload(); return; }
        if(kind === "card"){ patchCard(f, !!d.finished); }
        else { patchHero(f, !!d.finished); }
      }catch(err){
        /* 提交**已经成功**了，只是本地回写出错（某个锚点被改名之类）。
           这里绝不能走下面的 f.submit() —— 那会把刚写下的状态再翻回去（双提交）。
           退回「重取本页」：以服务端为准重画，状态一定对。 */
        console.warn("本地回写失败，改取服务端最新页面：", err);
        softReload();
      }
    }).catch(function(err){
      f.removeAttribute("data-busy");
      console.warn("提交没送达，改用整页提交：", err);
      f.submit();                          /* 只有「请求没到服务端」时重提交才是安全的 */
    });
  });

  /* 首页那张卡的计数是服务端渲染的，两种情况下会停在旧值：
     ① 从详情页标记完按「后退」回来 —— 浏览器直接用 bfcache 里的页面，不发新请求；
     ② 页面一直开着，手机阅读器那边又读完一卷（服务端每 5 分钟才重算一次）。
     所以在这三个时机各拉一次当前页，只把计数本身换掉：不重画 DOM，轮播动画、
     滚动位置、分组展开状态都不受影响。页面上没有 #fincnt（列表页 / 详情页 / 访客
     页面）时直接跳过，一个请求都不发。 */
  function refreshFinCount(){
    var cur = document.getElementById("fincnt");
    if(!cur || !window.fetch){ return; }
    /* cache:"no-store" 不能省：浏览器对「后退」导航会直接复用缓存里的 HTML
       （实测 headed Edge：后退回来 #fincnt 还是旧值，而服务端已经是新值），
       fetch 不给 no-store 也可能命中同一份缓存。 */
    fetch(location.href, {credentials: "same-origin", cache: "no-store",
                          headers: {"Accept": "text/html"}})
      .then(function(r){ return r.text(); })
      .then(function(h){
        var src = new DOMParser().parseFromString(h, "text/html").getElementById("fincnt");
        if(src){ cur.textContent = src.textContent; }
      })
      .catch(function(){});
  }
  /* 两种「看到的可能是旧页面」的进入方式都要刷新：
     bfcache 恢复（persisted=true）、以及后退/前进导航（此时文档是新的，但内容来自
     缓存，persisted 是 false）—— 后者正是「从详情页标记完按后退回首页」的场景。 */
  window.addEventListener("pageshow", function(e){
    var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
    if(e.persisted || (nav && nav.type === "back_forward")){ refreshFinCount(); }
  });
  document.addEventListener("visibilitychange", function(){
    if(document.visibilityState === "visible"){ refreshFinCount(); }
  });
  setInterval(function(){
    if(document.visibilityState === "visible"){ refreshFinCount(); }
  }, 60000);
})();
"""


def _html_page(title, body_inner, active="", extra_css="", is_admin=False, back="/"):
    """整页外壳。``is_admin`` 是**渲染期开关**：非管理员时「已读完」入口这段 HTML
    根本不会被拼出来 —— 前端拿到的是「没有这个入口」的页面，而不是「用 CSS 藏起来」
    的页面（CSS 隐藏可以用查看源码/开发者工具还原，等于没有权限控制）。

    真正的权限闸门在服务端路由（``OPDSHandler`` 的 ``_role()`` 与 403 分支）；
    这里只负责「不给入口」，属于体验层，两道一起才叫完整。

    ``back`` 是当前页面地址：登录后跳回原处、退出后也留在原地。
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
    # 右上角的身份入口：管理员给「退出」，访客给「登录」。**访客页面只有「登录」两字**，
    # 不写「登录后可看已读完」这类说明 —— 管理员专有字样不能出现在访客源码里（同 MARK_JS）。
    back_attr = html.escape(back, quote=True)
    if is_admin:
        acc_html = ('<form class="hacc" method="post" action="/opds/logout">'
                    f'<input type="hidden" name="back" value="{back_attr}">'
                    '<button class="hacbtn" type="submit" title="退出管理员登录">退出</button>'
                    "</form>")
    else:
        acc_html = (f'<a class="hacc" href="/opds/login?back={quote(back, safe="")}"'
                    ' title="管理员登录">登录</a>')
    # grpAll：分组展开/折叠，访客也用得上，人人下发。
    # MARK_JS：标记按钮的原地生效层 —— 只有管理员页面才有那个按钮，脚本也只在管理员
    # 页面注入：既省流量，也不让「已读完」这类管理员专有字样出现在访客拿到的源码里。
    script = ('<script>function grpAll(open){'
              'document.querySelectorAll(".group").forEach(function(g){'
              'g.classList.toggle("open",open)});}'
              + (MARK_JS if is_admin else "")
              + "</script>")
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
        f"{acc_html}"
        "</div></header>"
        f'<main class="wrap">{body_inner}</main>'
        f'<footer>{html.escape(SERVER_TITLE)} · OPDS 书源 · 由 opds_server.py 自动维护</footer>'
        + script +
        "</body></html>"
    )


# 登录页专用样式。刻意**不并进 SITE_CSS**：一是省流量（每个页面都要下发一份 CSS），
# 二是它只在登录页用得上，混进去以后没人敢删。
LOGIN_CSS = """
.lgate{max-width:360px;margin:10vh auto 0}
.lgcard{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:26px 24px}
.lgcard h1{font-size:19px;margin:0 0 6px}
.lgcard .sub{margin:0 0 20px;font-size:12.5px;line-height:1.6}
.lgcard label{display:block;font-size:12.5px;color:var(--muted);margin:0 0 6px}
.lgcard input[type=text],.lgcard input[type=password]{width:100%;padding:10px 12px;font-size:14px;
  font-family:inherit;border:1px solid var(--border);border-radius:9px;background:var(--bg);
  color:var(--text);outline:none;margin:0 0 15px}
.lgcard input:focus{border-color:var(--accent)}
.lgcard button{width:100%;padding:11px;font-size:14px;font-family:inherit;border:0;
  border-radius:9px;background:var(--accent);color:var(--accent-fg);cursor:pointer}
.lgcard button:hover{filter:brightness(1.08)}
.lgcard button[disabled]{opacity:.55;cursor:not-allowed;filter:none}
.lgerr{padding:10px 12px;border-radius:9px;background:#fff1f0;border:1px solid #ffcdd2;
  color:#a40e26;font-size:12.5px;margin-bottom:16px;line-height:1.55}
@media (prefers-color-scheme:dark){
  .lgerr{background:#3a1518;border-color:#6b2b30;color:#ffb3b8}
}
.lgback{text-align:center;margin-top:16px;font-size:12.5px}
"""


def login_html(err="", back="/", wait=0):
    """管理员登录页（公开可达 —— 它本身就是入口）。

    * 文案里**不出现「已读完」**：那是管理员专有的字样，而这个页面谁都拿得到
      （项目里有专门的断言在盯「访客源码不含管理员字样」这条约定）。
    * ``wait`` > 0 时按钮直接禁用：连着试错之后，与其让用户点了没反应，不如把
      还要等多久写清楚。
    """
    esc = lambda s: html.escape(str(s), quote=True)          # noqa: E731
    err_html = f'<div class="lgerr">{esc(err)}</div>' if err else ""
    btn = ('<button type="submit" disabled>请稍候…</button>' if wait
           else '<button type="submit">登录</button>')
    body = (
        '<div class="lgate"><div class="lgcard">'
        "<h1>管理员登录</h1>"
        '<p class="sub">登录后可进行标记、清单等管理操作。<br>'
        "不登录也可以正常浏览、搜索、下载与订阅。</p>"
        + err_html +
        f'<form method="post" action="/opds/login">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        '<label for="lg-u">用户名</label>'
        '<input id="lg-u" name="user" type="text" autocomplete="username" autofocus>'
        '<label for="lg-p">口令</label>'
        '<input id="lg-p" name="pass" type="password" autocomplete="current-password">'
        + btn +
        "</form>"
        '<div class="lgback"><a href="/">← 返回书库</a></div>'
        "</div></div>")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#0e1116">'
        f'<link rel="icon" href="{FAVICON}">'
        f"<title>{html.escape('管理员登录 · ' + SERVER_TITLE)}</title>"
        f"<style>{SITE_CSS}{LOGIN_CSS}</style>"
        "</head><body>"
        '<header><div class="hbar">'
        f'<a class="brand" href="/"><span class="dot">&#128218;</span>'
        f'<span>{html.escape(SERVER_TITLE)}</span></a>'
        "</div></header>"
        f'<main class="wrap">{body}</main>'
        "</body></html>")


def _cover_url(rel):
    return "/cover/" + encode_path(rel)


def _mark_form(key, back, finished, readlist=False):
    """「标记 / 取消已读完」表单 —— 纯 HTML 表单 POST。

    刻意**不依赖 JS**：手机上 JS 被禁、阅读器内嵌浏览器兼容性参差时，原生提交照样能用，
    提交后服务端 303 跳回 ``back``，页面重绘时状态必然正确。
    ``MARK_JS`` 只是叠加在这上面的增强层：能跑 fetch 就地改 DOM（不跳转、不丢滚动位置），
    跑不动就原样 submit —— 两条路径共用同一套服务端逻辑，不存在「只有 JS 才对」的状态。
    只有管理员渲染到这里（``is_admin`` 为真时才调用）。

    ``readlist=True``：这张卡出现在「已读完」列表里 —— JS 取消标记后可以直接把卡拿掉
    （那份清单里出现「未读完」的书本身就是自相矛盾的）。目录页没有这个标记，
    因为那里的卡取消后必须留在原地。
    """
    on = " on" if finished else ""
    tip = "已读完，点击取消标记" if finished else "标记为已读完"
    glyph = "&#10003;" if finished else "&#9675;"
    esc = lambda s: html.escape(s, quote=True)          # noqa: E731
    list_attr = ' data-list="read"' if readlist else ""
    return (
        f'<form class="mkform" method="post" action="/opds/read/toggle"{list_attr}>'
        f'<input type="hidden" name="key" value="{esc(key)}">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button class="mk{on}" type="submit" title="{tip}" aria-label="{tip}">{glyph}</button>'
        "</form>")


def _prog_pill(st):
    """作品级进度 → ``(角标元组, 副标题片段)``；没有进度数据时两者都为空。"""
    if not st:
        return None, ""
    pct = int(round(st["percent"]))
    return (pct, st["all"]), f'已读 {st["done"]}/{st["total"]} 卷'


def _work_prog(vols, vis):
    """取某部作品的进度角标数据（``vis`` 为假时直接跳过，连算都不算）。"""
    return _prog_pill(MOON.stat_of(vols)) if vis else (None, "")


def _vol_prog(rel, vis):
    """分卷进度片段：``已读 33%`` / ``读完``；没有记录返回空串。

    文案刻意用「读完」而不是「已读完」—— 「已读完」是管理员功能的专有词，
    访客页面里一个字符都不该出现（有回归断言盯着这个子串）。
    """
    if not vis:
        return ""
    got = MOON.vol_progress(rel)
    if got is None:
        return ""
    done, pct = got
    if done:
        return '<span class="vp done">读完</span>'
    return f'<span class="vp">已读 {int(round(pct))}%</span>'


def _prog_fab(st):
    """作品详情页右下角的常驻进度指示器：一个百分比圆环 + 一行文字。

    ``conic-gradient`` 画环，不需要 JS / SVG；不支持时退化成纯文字（不影响可读性）。
    整部读完时环变绿、文案换成「已全部读完」（刻意避开「已读完」这个管理员专有词）。
    """
    pct = int(round(st["percent"]))
    done = bool(st["all"])
    if done:
        head, sub = "已全部读完", f"{st['total']} 卷 · 整体 {pct}%"
    else:
        head, sub = f"已读 {st['done']}/{st['total']} 卷", f"整体进度 {pct}%"
    return (
        f'<div class="pfab{" pdone" if done else ""}" role="status"'
        f' aria-label="阅读进度 {pct}%">'
        f'<span class="pring" style="--p:{pct}"><i>{pct}</i></span>'
        f'<span class="ptxt"><b>{head}</b><span>{sub}</span></span>'
        "</div>")


def _book_card(href, cover_rel, title, sub, badge=None, key=None, back="",
               finished=False, fin_note="", upd=0, readlist=False, prog=None,
               pnote=""):
    """一张书卡。

    * ``key`` 非空（= 管理员视角）时封面左上角挂「标记已读完」按钮；
    * ``upd`` > 0 表示这本书有新卷：封面左下角压一条「有更新」、整卡描暖色边、
      副标题里点明新增几卷（角标只写数字，副标题给完整说法）；
    * ``prog`` = ``(百分比, 是否整部读完)`` → 封面**右下角**的进度角标（照 Moon+ 的做法）；
      ``pnote`` 是副标题里的文字版（``已读 3/9 卷``）。
    """
    badge_html = f'<span class="badge">{html.escape(badge)}</span>' if badge else ""
    mark = _mark_form(key, back, finished, readlist=readlist) if key else ""
    prog_html = ""
    if prog:
        pct, done = prog
        prog_html = (f'<span class="prog{" pdone" if done else ""}"'
                     f' title="阅读进度 {pct}%">{pct}%</span>')
    sub_html = html.escape(sub) + (f' · <span class="fin">{html.escape(fin_note)}</span>'
                                  if fin_note else "")
    if pnote:
        sub_html += f' · <span class="pnt">{html.escape(pnote)}</span>'
    if upd:
        sub_html += f' · <span class="newt">有更新 +{upd} 卷</span>'
    upd_html = (f'<span class="upd">有更新 +{upd}</span>' if upd else "")
    return (
        '<div class="card%s">' % (" upd" if upd else "")
        + f'<a class="cardlink" href="{href}">'
        f'<div class="ph"><img src="{_cover_url(cover_rel)}" alt="" loading="lazy" decoding="async">'
        f"{badge_html}{upd_html}{prog_html}</div>"
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


# ================= 二次元首页专用样式 =================
# 只给首页下发（root_html 经 extra_css 注入），不影响其它页面。
ANIME_HOME_CSS = """
.an-hero{position:relative;overflow:hidden;border-radius:22px;padding:32px 30px 24px;
  background:linear-gradient(135deg,#ffd3e6 0%,#e3c8ff 42%,#bfe0ff 100%);
  border:1px solid rgba(255,255,255,.65);box-shadow:0 14px 40px rgba(190,140,255,.35);margin-bottom:30px}
.an-hero::before,.an-hero::after{content:"";position:absolute;border-radius:50%;pointer-events:none;filter:blur(42px)}
.an-hero::before{width:260px;height:260px;right:-60px;top:-70px;background:rgba(255,182,222,.55)}
.an-hero::after{width:220px;height:220px;left:36%;bottom:-95px;background:rgba(150,200,255,.5)}
.an-spark{position:absolute;color:#fff;pointer-events:none;text-shadow:0 0 8px rgba(255,255,255,.9);
  animation:anTwinkle 3.2s ease-in-out infinite}
@keyframes anTwinkle{0%,100%{opacity:.3;transform:scale(.8) rotate(0)}50%{opacity:1;transform:scale(1.15) rotate(18deg)}}
.an-badge{display:inline-block;padding:6px 16px;border-radius:999px;background:rgba(255,255,255,.55);
  border:1px solid rgba(255,255,255,.85);color:#c04a94;font-size:12.5px;font-weight:600;backdrop-filter:blur(6px);position:relative}
.an-hero h1{font-size:33px;font-weight:700;margin:14px 0 8px;letter-spacing:.5px;position:relative;
  background:linear-gradient(90deg,#ff6fae,#a06bff,#4d9ef0);-webkit-background-clip:text;background-clip:text;color:transparent}
.an-sub{color:#6a4a75;font-size:14px;margin:0;position:relative;max-width:62ch}
.an-tags{margin-top:18px;display:flex;flex-wrap:wrap;gap:10px;position:relative}
.an-tag{padding:7px 15px;border-radius:999px;background:rgba(255,255,255,.6);border:1px solid rgba(255,255,255,.9);
  font-size:12.5px;color:#a05fb0;font-weight:600;backdrop-filter:blur(4px);transition:transform .15s,background .15s}
.an-tag:hover{transform:translateY(-2px) scale(1.04);background:rgba(255,255,255,.9)}

.an-marquee{margin-top:24px;position:relative;overflow:hidden;border-radius:16px;
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);
  mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent)}
.an-track{display:flex;gap:14px;width:max-content;padding:4px 0;animation:anScroll 48s linear infinite}
.an-marquee:hover .an-track{animation-play-state:paused}
@keyframes anScroll{to{transform:translateX(-50%)}}
.an-slide{position:relative;width:130px;flex-shrink:0;border-radius:14px;overflow:hidden;
  background:rgba(255,255,255,.5);box-shadow:0 6px 16px rgba(160,110,220,.3);
  transition:transform .16s,box-shadow .16s}
.an-slide:hover{transform:translateY(-5px) rotate(-1deg);box-shadow:0 12px 26px rgba(160,110,220,.45)}
.an-slide img{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
/* 书名覆盖条必须放得下**完整书名**（本库最长 28 字）：130px 宽减去左右各 9px 内边距，
   10.5px 字号约 10.6 字/行 → 3 行约 32 字。原先 2 行会把长书名截成半截。 */
.an-slide .cap{position:absolute;left:0;right:0;bottom:0;padding:18px 9px 8px;font-size:10.5px;color:#fff;
  background:linear-gradient(180deg,transparent,rgba(90,40,120,.86));line-height:1.25;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
  text-shadow:0 1px 3px rgba(40,10,60,.5)}

/* 列数由**卡片数量**决定（服务端写 --ncats），不再让 auto-fit 自己算：
   自动算出的列数随窗口宽度浮动，多出来的那张卡就会被挤到第二行。
   （这段注释会随 <style> 下发给访客，别在这里写管理功能的名字。）
   视口装不下时按文件末尾的断点降级成 2 列。 */
.an-cats{display:grid;gap:18px;grid-template-columns:repeat(var(--ncats,4),minmax(0,1fr))}
.an-cat{position:relative;overflow:hidden;min-width:0;padding:24px clamp(14px,1.4vw,24px);border-radius:20px;
  border:1px solid rgba(255,255,255,.75);box-shadow:0 8px 26px rgba(160,120,230,.2);
  transition:transform .16s,box-shadow .16s}
.an-cat:hover{transform:translateY(-4px);box-shadow:0 14px 34px rgba(160,120,230,.34)}
.an-cat.a{background:linear-gradient(135deg,#ffe3f0,#f3d8ff)}
.an-cat.b{background:linear-gradient(135deg,#d6ecff,#d6f6ff)}
.an-cat.c{background:linear-gradient(135deg,#d9fbe4,#d2f4ee)}
.an-cat.d{background:linear-gradient(135deg,#efe2ff,#e0d8ff)}
.an-cat.e{background:linear-gradient(135deg,#fff0dd,#ffe3d0)}
.an-cat .ico{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;font-size:24px;
  background:rgba(255,255,255,.65);box-shadow:0 4px 10px rgba(150,100,200,.18);margin-bottom:14px}
.an-cat .nm{font-size:clamp(15.5px,1.08vw,19px);font-weight:700;color:#5a3a6b}
.an-cat .ds{font-size:clamp(11.5px,.84vw,12.5px);color:#8a6b95;margin-top:6px;line-height:1.5}
.an-cat .go{margin-top:14px;font-size:13px;font-weight:600;color:#c04a94}

.an-latest{display:flex;gap:14px;overflow-x:auto;padding:6px 2px 18px;scroll-snap-type:x mandatory;
  -webkit-overflow-scrolling:touch}
.an-latest::-webkit-scrollbar{height:8px}
.an-latest::-webkit-scrollbar-thumb{background:#d8b8f0;border-radius:8px}
.an-item{scroll-snap-align:start;flex:0 0 146px;position:relative;border-radius:16px;overflow:hidden;
  background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow);
  transition:transform .16s,box-shadow .16s}
.an-item:hover{transform:translateY(-4px);box-shadow:0 10px 24px rgba(150,110,210,.32)}
.an-item img{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
/* 同上：3 行才装得下完整书名（146px 宽 - 左右各 10px 内边距，12px 字号约 10.5 字/行） */
.an-item .t{padding:8px 10px 4px;font-size:12px;font-weight:600;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.an-item .s{padding:0 10px 10px;font-size:10.5px;color:var(--muted)}
.an-item .up{position:absolute;left:8px;top:8px;padding:2px 8px;border-radius:12px;font-size:10px;
  background:linear-gradient(135deg,#ff8fb8,#c77fff);color:#fff;font-weight:600;
  box-shadow:0 2px 8px rgba(200,110,190,.5)}

footer{margin-top:36px;padding:24px 20px;text-align:center;font-size:12px;
  background:linear-gradient(135deg,#ffd9ec,#d8ccff,#cfe6ff);color:#7a5a8b;
  border-top:1px solid rgba(255,255,255,.7)}
/* 分类导航的降级断点：880px 是「5 张卡一行」的最小可行宽度（每张 ≈160px，内容仍放得下），
   再窄就整行平分 2 列 —— 绝不会出现「前 4 张一行、第 5 张孤零零一行」。 */
@media (max-width:880px){.an-cats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){
  .an-hero{padding:26px 20px 20px}
  .an-hero h1{font-size:26px}
  /* 卡片宽度按「3 行放得下 28 字」反推：覆盖条有效宽度 = 卡片宽 - 左右内边距 */
  .an-slide{width:126px}
  .an-item{flex:0 0 148px}
  .an-cats{gap:14px}
}
"""


def _home_cover_rel(key, cat, book, lib):
    vols = lib.get(cat, {}).get(book)
    if not vols:
        return None
    pv = _primary_vol(vols, cat, book)
    return pv["rel"] if pv else None


def _home_covers(pend, lib, n):
    """轮播封面集合：优先「最近有更新」的，不够就按字母序采样补齐，保证有内容可滚。"""
    keys = sorted(pend, key=lambda k: pend[k].get("at") or "", reverse=True)
    if len(keys) < n:
        for cat, books in lib.items():
            for book in books:
                k = f"{cat}/{book}"
                if k not in keys:
                    keys.append(k)
    out = []
    for key in keys[:n]:
        cat, book = key.split("/", 1)
        rel = _home_cover_rel(key, cat, book, lib)
        if rel:
            out.append((key, cat, book, rel))
    return out


def root_html(is_admin=False):
    lib = get_library()
    pend = upd.observe()                    # 「有更新」状态（含时间戳，排序用）
    ups = upd.counts()
    # 有更新的分类在副标题后追加一段；先在循环外拼好，避免把 f-string 的隐式拼接切开
    note = {c: (f' · <b style="color:var(--new)">{n} 部有更新</b>' if n else "")
            for c, n in ups.items()}

    # ---- 轮播图区域：最近更新的封面无缝滚动（悬停暂停）----
    feat = _home_covers(pend, lib, 12)
    one = "".join(
        f'<a class="an-slide" href="/opds/book/{encode_path(key)}">'
        f'<img src="{_cover_url(rel)}" alt="" loading="lazy" decoding="async">'
        f'<div class="cap">{html.escape(book)}</div></a>'
        for key, cat, book, rel in feat)
    marquee = (f'<div class="an-marquee"><div class="an-track">{one}{one}</div></div>'
               if one else "")

    spark = ('<span class="an-spark" style="left:7%;top:12%;font-size:18px">✦</span>'
             '<span class="an-spark" style="left:20%;top:72%;font-size:14px;animation-delay:.7s">✧</span>'
             '<span class="an-spark" style="right:15%;top:24%;font-size:16px;animation-delay:1.2s">♡</span>'
             '<span class="an-spark" style="right:34%;bottom:10%;font-size:20px;animation-delay:.4s">✦</span>')
    hero = (
        '<div class="an-hero">' + spark
        + '<span class="an-badge">✿ 个人轻小说收藏 ✿</span>'
        + f"<h1>{html.escape(SERVER_TITLE)}</h1>"
        + '<p class="an-sub">手机可直接浏览、下载单卷或整本打包 · OPDS 阅读器可直接订阅</p>'
        + '<div class="an-tags">'
          '<span class="an-tag">📚 支持单卷下载</span>'
          '<span class="an-tag">📦 整本 / 整组打包 ZIP</span>'
          '<span class="an-tag">📱 OPDS 阅读器可直接订阅</span>'
          "</div>"
        + marquee
        + "</div>")

    # ---- 分类导航区：两部类 + 快捷入口，统一糖果色卡 ----
    def _cat_card(href, cls, ico, nm, ds):
        return (f'<a class="an-cat {cls}" href="{href}">'
                f'<div class="ico">{ico}</div>'
                f'<div class="nm">{html.escape(nm)}</div>'
                f'<div class="ds">{ds}</div>'
                f'<div class="go">进入浏览 →</div></a>')

    cat_cards = "".join(
        _cat_card("/opds/catalog/" + quote(cat),
                  "a" if cat == CATEGORY_DONE else "b",
                  "📖" if cat == CATEGORY_DONE else "🌙",
                  cat,
                  f'{len(books)} 部作品 · {sum(len(v) for v in books.values())} 卷{note.get(cat, "")}')
        for cat, books in lib.items())
    # 卡片顺序固定：全部作品 → 已完结 → 未完结 → 已读完 → 最近更新。
    # 色卡跟着卡走（.a 粉 / .b 蓝 / .c 绿 / .d 紫 / .e 橙），不随位置换颜色。
    cards = [_cat_card("/opds/catalog/all", "d", "📚", "全部作品", "不分分类浏览"),
             cat_cards]
    if is_admin:                       # 管理员才多这张卡（与顶栏 tab 同一开关）
        # 计数口径与清单页完全一致（人工标记 ∪ 阅读器判定）：只数人工清单的话，
        # 明明读完了一堆书却显示「0 部作品」，点进去反倒有内容。
        # id 供 MARK_JS 在「从缓存恢复 / 切回前台」时刷新这个数字。
        n_fin = len(_finished_all())
        cards.append(_cat_card("/opds/read", "e", "✅", "已读完",
                               f'已读完 <span id="fincnt">{n_fin}</span> 部作品'))
    cards.append(_cat_card("/opds/recent", "c", "⏰", "最近更新",
                           f"最近改动的 {RECENT_SIZE} 卷"))
    cards = "".join(cards)
    # 列数写进 CSS 变量：访客 4 张、管理员 5 张。必须用真实张数，
    # 否则 auto-fit 会把多出来的那张挤到第二行。
    n_cats = len(lib) + 2 + (1 if is_admin else 0)

    # ---- 最新更新列表：「有更新」的作品横向条；没有就退回轮播那批，保证不空 ----
    latest_keys = sorted(pend, key=lambda k: pend[k].get("at") or "", reverse=True)
    latest = []
    for key in latest_keys[:10]:
        cat, book = key.split("/", 1)
        rel = _home_cover_rel(key, cat, book, lib)
        if rel:
            latest.append((key, cat, book, rel))
    if not latest:
        latest = feat[:10]
    latest_html = "".join(
        f'<a class="an-item" href="/opds/book/{encode_path(key)}">'
        f'<span class="up">NEW</span>'
        f'<img src="{_cover_url(rel)}" alt="" loading="lazy" decoding="async">'
        f'<div class="t">{html.escape(book)}</div>'
        f'<div class="s">{html.escape(cat)}</div></a>'
        for key, cat, book, rel in latest)

    body = (hero
            + "<h2>✿ 分类导航</h2>"
            + f'<div class="an-cats" style="--ncats:{n_cats}">{cards}</div>'
            + "<h2>✦ 最新更新</h2>"
            + (f'<div class="an-latest">{latest_html}</div>' if latest_html
               else '<div class="empty">最近没有新卷入库。</div>'))
    return _html_page(SERVER_TITLE, body, active="", extra_css=ANIME_HOME_CSS, is_admin=is_admin)


def _upd_bar(pend, back, is_admin):
    """列表页工具条右侧的「有更新」提示：访客只看到文字，管理员多一个「全部标为已读」。

    清除动作只有管理员能点（服务端也会再判一次权限）—— 否则访客随手一刷就把
    管理员的提醒消掉了。
    """
    if not pend:
        return ""
    cnt = f'<span class="cnt">&#128293; {len(pend)} 部作品有更新</span>'
    if not is_admin:
        return cnt
    esc = lambda s: html.escape(s, quote=True)          # noqa: E731
    return (
        '<form class="barupd" method="post" action="/opds/updates/clear">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f"{cnt}"
        '<button class="dl ghost small" type="submit">全部标为已读</button>'
        "</form>")


def catalog_html(cat, page=1, is_admin=False):
    lib = get_library()
    fin = load_finished() if is_admin else set()
    pend = upd.observe()                       # 顺带做一次「新增卷」检测
    upd_n = lambda k: len(pend[k]["vols"]) if k in pend else 0      # noqa: E731
    vis = MOON.visible(is_admin)               # 进度角标对谁可见（默认所有人）

    def card_for(key, cat_, book_, vols_, back_, badge=False):
        prog, pnote = _work_prog(vols_, vis)
        return _book_card("/opds/book/" + encode_path(key),
                          _primary_vol(vols_, cat_, book_)["rel"], book_,
                          f"{cat_} · {len(vols_)} 卷",
                          badge=(f"{len(vols_)} 卷" if badge else None),
                          key=key if is_admin else None, back=back_,
                          finished=key in fin,
                          fin_note="已读完" if key in fin else "",
                          upd=upd_n(key), prog=prog, pnote=pnote)

    if cat == "all":
        merged = {}
        for c, bs in lib.items():
            for b, vols in bs.items():
                merged[f"{c}/{b}"] = (c, vols)
        keys = upd.order_keys(sorted(merged.keys()), pend)          # 有更新的排最前
        chunk, extra = _paginate(keys, page, "/opds/catalog/all?page=1")
        back = "/opds/catalog/all?page=" + str(page)
        cards = "".join(
            card_for(k, *k.split("/", 1), merged[k][1], back) for k in chunk)
        total = len(keys)
        label = "全部作品"
        active = "all"
    elif cat in CATEGORY_DIRS:
        books = lib.get(cat, {})
        # 排序用的是「分类/书名」全键（pending 的键就是它），排完再拆回书名
        keys = [k.split("/", 1)[1] for k in
                upd.order_keys([f"{cat}/{b}" for b in sorted(books, key=lambda s: s.lower())], pend)]
        chunk, extra = _paginate(keys, page, "/opds/catalog/" + quote(cat) + "?page=1")
        back = "/opds/catalog/" + quote(cat) + "?page=" + str(page)
        cards = "".join(
            card_for(f"{cat}/{b}", cat, b, books[b], back, badge=True)
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
        + _upd_bar(pend, back, is_admin)
        + f'<span class="sub" style="margin:0">{total} 部作品</span>'
        "</div>"
        f'<div class="grid">{cards}</div>'
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"{label} · {SERVER_TITLE}", body, active=active,
                      is_admin=is_admin, back=back)


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

    # 新增卷：先检测（observe）再取这本书的待读提示，展示信息（子目录/标题）从书库索引现取
    entry = upd.observe().get(key)
    new_rels = set(entry["vols"]) if entry else set()
    new_vols = [v for v in vols if v["rel"] in new_rels]

    meta_lines = (
        f'<div class="meta-line">作者 <b>{html.escape(author)}</b></div>' if author else "")
    meta_lines += f'<div class="meta-line">分类 <b>{html.escape(cat)}</b> · 卷数 <b>{len(vols)}</b> · 体积 <b>{human_size(total_size)}</b></div>'
    if is_admin:                       # 访客拿到的详情页里连「阅读状态」这一行都没有
        # id="finstate"：给 MARK_JS 就地改文字用（点一下不必整页重绘）
        meta_lines += ('<div class="meta-line">阅读状态 <b id="finstate" style="color:#1a7f37">已读完</b></div>'
                       if finished else
                       '<div class="meta-line">阅读状态 <b id="finstate">未读</b></div>')

    desc_html = f'<div class="desc">{html.escape(desc)}</div>' if desc else ""

    # 详情页一次性渲染全部卷（不翻页）：直接传完整 vols 列表，_render_groups 会显示所有分组
    vis = MOON.visible(is_admin)
    groups = _group_vols_by_subdir(vols, cat, book)
    groups_html = _render_groups(groups, cat, book, vols, new_rels, vis=vis)

    # 右下角常驻进度环：作品级「读了几卷 / 整体百分比」，由 Moon+ 的位置数据派生（只读）
    st = MOON.stat_of(vols) if vis else None
    pill = _prog_fab(st) if st else ""

    # 标记按钮：管理员才有；已读时时样式换成实心绿并提示可取消
    mark_btn = ""
    if is_admin:
        mark_btn = (
            '<form class="mkbig" method="post" action="/opds/read/toggle">'
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
        + _upd_box(new_vols, cat, book, entry)
        + (f'<div class="sec-head">'
         f'<h2>分卷下载 <span class="n">{len(vols)} 卷</span></h2>'
         f'<div class="grp-tools">'
         f'<button class="grp-btn" onclick="grpAll(true)">全部展开</button>'
         f'<button class="grp-btn" onclick="grpAll(false)">全部折叠</button>'
         f'</div></div>' if groups_html else
         f'<h2>分卷下载 <span class="n">{len(vols)} 卷</span></h2>')
        + (groups_html or '<div class="empty">这一页没有内容</div>')
        + pill
    )
    active = "done" if cat == CATEGORY_DONE else "ongoing"
    page_html = _html_page(f"{book} · {SERVER_TITLE}", body, active=active, is_admin=is_admin,
                           back="/opds/book/" + encode_path(rel))

    # 「看过了」= 点进来就把这本书的更新提示收掉（回到列表就回到原位）。
    # 只有管理员算「看过」—— 访客浏览不该静默消掉管理员的提醒。
    if is_admin and entry:
        upd.clear(key)
    return page_html


def _upd_box(new_vols, cat, book, entry):
    """详情页顶部的「本次新增」区块：直接点名**哪个子目录、哪几卷**是新的。

    只靠分组行上的小药丸还不够 —— 新卷常落在默认折叠的组里，用户得先知道去哪儿展开。
    """
    if not new_vols:
        return ""
    inner = f"{cat}/{book}/"
    rows = []
    for v in new_vols:
        rem = v["rel"][len(inner):] if v["rel"].startswith(inner) else v["rel"].split("/")[-1]
        sub = rem.rsplit("/", 1)[0] if "/" in rem else ""
        where = f"{sub} · " if sub else ""
        rows.append(
            f'<a class="vol" href="/dl/{encode_path(v["rel"])}">'
            f'<div class="ph"><img src="{_cover_url(v["rel"])}" alt="" loading="lazy" decoding="async"></div>'
            f'<div class="meta"><div class="t">{html.escape(v["title"])}</div>'
            f'<div class="s"><span class="vnew">新</span>'
            f'<span class="sub2">{html.escape(where)}</span>{human_size(v["size"])}'
            f"</div></div>"
            f'<span class="dl">下载</span></a>')
    when = (entry or {}).get("at", "")
    return (
        '<div class="updbox">'
        '<div class="hd"><span class="dot"></span>本次新增 '
        f"{len(new_vols)} 卷"
        + (f'<span class="n">{html.escape(when)}</span>' if when else "")
        + "</div>"
        + "".join(rows)
        + "</div>")


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


def _render_groups(groups, cat, book, page_chunk, new_rels=frozenset(), vis=False):
    """把分组渲染成「文件夹 + 卷列表」，并标记哪些卷在当前分页里。

    ``new_rels`` 里的卷会给行加「新」标，其所属分组额外挂「有更新 +N」并**默认展开** ——
    新卷常常落在后面那些默认折叠的组里，不展开就等于没提示。
    ``vis`` 为真时每行再带一个该卷的阅读进度（来自 Moon+ 位置数据）。
    """
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
        n_new = sum(1 for v in in_page if v["rel"] in new_rels)
        rows = "".join(
            f'<a class="vol" href="/dl/{encode_path(v["rel"])}">'
            f'<div class="ph"><img src="{_cover_url(v["rel"])}" alt="" loading="lazy" decoding="async"></div>'
            f'<div class="meta"><div class="t">{html.escape(v["title"])}</div>'
            f'<div class="s">'
            + ('<span class="vnew">新</span>' if v["rel"] in new_rels else "")
            + _vol_prog(v["rel"], vis)
            + f'{human_size(v["size"])}</div></div>'
            f'<span class="dl">下载</span></a>'
            for v in in_page)
        opened = " open" if (n_new or not out) else ""   # 第一组默认展开；有更新的也展开
        out.append(
            f'<section class="group{opened}">'
            f'<header class="group-head" onclick="this.parentNode.classList.toggle(\'open\')">'
            f'<span class="caret">&#9654;</span>'
            f'<span class="gico">&#128194;</span>'
            f'<span class="gnm">{html.escape(group_name)}</span>'
            f'<span class="gmeta">{len(vols)} 卷</span>'
            + (f'<span class="gnew">有更新 +{n_new}</span>' if n_new else "")
            + f'<a class="dl ghost small" href="{zip_url}" '
            f'onclick="event.stopPropagation()">打包本组</a>'
            f'</header>'
            f'<div class="group-body">{rows}</div>'
            f'</section>')
    return "".join(out)


def _vol_rows(items, vis=False):
    return "".join(
        f'<a class="vol" href="/dl/{encode_path(v["rel"])}">'
        f'<div class="ph"><img src="{_cover_url(v["rel"])}" alt="" loading="lazy" decoding="async"></div>'
        f'<div class="meta"><div class="t">{html.escape(title)}</div>'
        f'<div class="s">{_vol_prog(v["rel"], vis)}{html.escape(c)} · {human_size(v["size"])}</div></div>'
        f'<span class="dl">下载</span></a>'
        for c, b, v, title in items)


def recent_html(page=1, is_admin=False):
    vols = sorted(all_vols(), key=lambda t: t[2]["mtime"], reverse=True)[:RECENT_SIZE]
    chunk, extra = _paginate(vols, page, "/opds/recent?page=1")
    rows = _vol_rows([(c, b, v, f"{b} · {v['title']}") for c, b, v in chunk],
                     vis=MOON.visible(is_admin))
    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span><span>最近更新</span></div>'
        "<h1>最近更新</h1>"
        f'<p class="sub">按文件修改时间排序 · {len(vols)} 卷</p>'
        + (rows or '<div class="empty">暂无内容</div>')
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"最近更新 · {SERVER_TITLE}", body, active="recent", is_admin=is_admin,
                      back="/opds/recent?page=" + str(page))


def search_html(q, page=1, is_admin=False):
    q = (q or "").strip()
    if q:
        hits = []
        for c, b, v in all_vols():
            if q.lower() in f"{b}/{v['rel']}".lower():
                hits.append((c, b, v))
        chunk, extra = _paginate(hits, page, "/opds/search?q=" + quote(q) + "&page=1")
        rows = _vol_rows([(c, b, v, f"{b} · {v['title']}") for c, b, v in chunk],
                         vis=MOON.visible(is_admin))
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
    url = "/opds/search?q=" + quote(q) + "&page=" + str(page) if q else "/opds/search"
    return _html_page(f"搜索 · {SERVER_TITLE}", body, active="", is_admin=is_admin, back=url)


def read_html(page=1):
    """「已读完」列表页（**仅管理员**：服务端在路由层就会把访客挡在外面，
    非管理员拿不到这个页面，也拿不到顶栏入口）。

    统一成**一条清单**，不再按来源分栏：

    * **人工标记** —— ``.autosync/finished.json`` 里的作品，卡片带 ✓ 可取消标记；
    * **阅读器自动判定** —— 由手机阅读器的位置数据派生（作品下所有卷都读完）。
      **只读、不写清单**：``updates.observe()`` 有「出现新卷 → 自动从已读完剔除」的联动，
      自动判定若每轮写回 ``finished.json``，两边会互相拉扯（加回 → 删 → 又加回）。

    同一部作品两边都命中时只出现一次（人工优先，仍带 ✓）。判定栏不再单列。
    """
    merged = _finished_all()      # 人工在前、仅阅读器判定的在后；顺序稳定便于翻页与定位
    total = len(merged)
    chunk, extra = _paginate(merged, page, "/opds/read?page=1")
    back = "/opds/read?page=" + str(page)
    rows = []
    for key, cat, book, vols, st in chunk:
        card_kw = dict(
            href="/opds/book/" + encode_path(key),
            cover_rel=_primary_vol(vols, cat, book)["rel"],
            title=book,
            sub=f"{cat} · {len(vols)} 卷",
            badge=f"{len(vols)} 卷",
        )
        if st is None:                      # 人工标记：带 ✓，可取消
            rows.append(_book_card(**card_kw, key=key, back=back,
                                   finished=True, fin_note="已读完", readlist=True))
        else:                               # 仅阅读器判定：显示进度，无 ✓
            rows.append(_book_card(**card_kw,
                                   prog=(int(round(st["percent"])), True),
                                   pnote=f'{len(vols)}/{len(vols)} 卷'))
    cards = "".join(rows)

    empty_html = ('<div class="empty">还没有已读完的作品。<br>'
                  '去「已完结 / 未完结」把鼠标移到封面上，点左上角出现的 &#9675; 即可标记为已读完。'
                  '<br><span style="font-size:12px">（触屏设备没有悬停，圆圈会一直显示，直接点即可）</span></div>')
    if total:
        # data-total 给 MARK_JS 记账：取消标记后卡片消失，计数要减 1 而不是重数当前页的卡
        # （分页时当前页的卡数 ≠ 总数）。空态挂在 <template> 里，等最后一张卡也没了才用得上。
        listing = (f'<div class="grid" data-total="{total}">{cards}</div>'
                   f'<template id="readempty">{empty_html}</template>')
    else:
        listing = empty_html

    body = (
        '<div class="crumb"><a href="/">首页</a><span>/</span><span>已读完</span></div>'
        '<div class="bar">'
        '<h1 style="margin:0">已读完</h1>'
        '<span class="spacer"></span>'
        f'<span class="sub" id="readcnt" style="margin:0">{total} 部作品</span>'
        "</div>"
        f'<p class="sub">鼠标移上封面后，点左上角的 &#10003; 可取消标记。'
        f'（本机状态，不同步到书库/仓库）</p>'
        + listing
        + _pager_html(page, _next_link(extra), _prev_link(extra))
    )
    return _html_page(f"已读完 · {SERVER_TITLE}", body, active="read", is_admin=True, back=back)


# 兼容旧名
def index_html(is_admin=False):
    return root_html(is_admin=is_admin)




