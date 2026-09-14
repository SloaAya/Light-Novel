#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「新增卷」提示：书里多了新卷 → 该书置顶 + 封面打角标，点进去看过就恢复。

要解决的问题
------------
书库是「一部作品 = 一个目录，目录里若干 epub」的结构。往某个作品目录里补了一卷之后，
浏览时很难发现 —— 52 部作品、封面右上角的卷数从 48 变 49 根本看不出来。
所以这里维护一份「有哪些作品多了新卷」的待读提示。

怎么判断「多了新卷」
-------------------
* 每次 :func:`observe` 拿当前书库（``library.get_library()``，自带缓存）与上次快照对比，
  以「每本书的 ``{卷相对路径: 字节数}``」为签名。
* **首次运行只建立基线，不产生任何提示** —— 否则装上功能那一刻全库 1500 卷全是「新增」。
* **整本新出现的作品不算**（那是「新书」不是「新卷」）：批量导入时不会被刷屏，
  监控目录被清空/换盘后重新扫回来时也不会误报。只有**已存在的作品多了卷**才提示。
* **大小相同的「少一个 / 多一个」判为改名，不算新增** —— 本项目的 epub 经常被批量改
  文件名（规整成「[系列][03卷]书名」这类），按改名处理才不会让整个书库变成「有更新」。

状态文件 ``.autosync/updates.json``
-----------------------------------
* ``seen``：上次看到的书库签名，用于下次对比。**体积不小（全库 1500 卷 ≈ 90KB）**，
  所以整体用紧凑 JSON 落盘，不做缩进美化。
* ``pending``：待读提示，``{作品键: {"at": 发现时间, "vols": [卷相对路径…]}}``。
  只存相对路径，**标题/所属子目录等展示信息渲染时再从书库索引里取** ——
  单一真源，不会留下「清单里还写着改名前的旧标题」这种坑。

与 ``finished.py`` 同一套约定：运行时状态不进仓库（``.autosync/`` 已 gitignore）、
写入原子（临时文件 + fsync + :func:`os.replace`）、读取永不抛（缺失/损坏 → 当空）、
读写全程持锁（``ThreadingHTTPServer`` 是并发的）。
"""

import json
import logging
import os
import tempfile
import threading
import time

from ..paths import CATEGORY_DIRS, UPDATES_FILE
from .finished import normalize_key        # 作品键（分类/书名）的校验规则复用同一份

log = logging.getLogger("sync")

_LOCK = threading.RLock()
_FILE_VERSION = 1


# ---------------------------- 落盘 ----------------------------
def _empty():
    return {"version": _FILE_VERSION, "updated": "", "seen": {}, "pending": {}}


def _load():
    """读状态。缺失 / 损坏 / 结构不对 → 空状态（= 下次 observe 只建基线，不误报）。"""
    try:
        with open(UPDATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        log.warning("新增卷状态读取失败（按空处理，将重建基线）：%s", exc)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    st = _empty()
    seen = data.get("seen")
    if isinstance(seen, dict):
        for k, v in seen.items():
            key = normalize_key(k)
            if key and isinstance(v, dict):
                st["seen"][key] = {str(r): int(s or 0) for r, s in v.items()}
    pend = data.get("pending")
    if isinstance(pend, dict):
        for k, v in pend.items():
            key = normalize_key(k)
            if not key or not isinstance(v, dict):
                continue
            vols = [str(r) for r in (v.get("vols") or []) if r]
            if vols:
                st["pending"][key] = {"at": str(v.get("at") or ""), "vols": vols}
    return st


def _save(state):
    """原子写：临时文件 → fsync → :func:`os.replace`。

    用紧凑 JSON：``seen`` 装着全库每卷的路径与大小，缩进美化后会变成几千行。
    """
    payload = dict(state)
    payload["version"] = _FILE_VERSION
    payload["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    directory = os.path.dirname(UPDATES_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".updates-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, UPDATES_FILE)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------- 检测 ----------------------------
def _sig(vols):
    """一本书的签名：``{卷相对路径: 字节数}``。字节数用于识别「改名」而非「新增」。"""
    out = {}
    for v in vols:
        try:
            out[str(v["rel"])] = int(v.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _real_additions(added, gone, cur, prev):
    """从「新增」里剔除其实只是改了名的卷。

    判据：一个新增的卷与一个消失的卷**字节数相同** → 判为改名。大小 0（stat 失败）
    不参与配对，免得把两个「读不到大小」的文件错配成改名。
    """
    by_size = {}
    for rel in gone:
        size = prev.get(rel) or 0
        if size > 0:
            by_size.setdefault(size, []).append(rel)
    out = []
    for rel in added:
        size = cur.get(rel) or 0
        bucket = by_size.get(size) if size > 0 else None
        if bucket:
            bucket.pop()                      # 一对一消耗，不重复配对
            continue
        out.append(rel)
    return out


def _copy_pending(pending):
    return {k: {"at": v["at"], "vols": list(v["vols"])} for k, v in pending.items()}


def observe(lib=None):
    """用当前书库对比上次快照、更新提示状态，返回 ``pending`` 的副本。

    调用方是页面渲染，成本必须低：``lib`` 默认取 :func:`library.get_library`（自带缓存），
    对比是纯内存集合运算，**只有真的变了才落盘**（否则每次刷新都写 90KB 文件）。
    """
    from .library import get_library
    lib = get_library() if lib is None else lib

    with _LOCK:
        state = _load()
        seen, pending = state["seen"], state["pending"]
        first_run = not seen
        dirty = False
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        live = {}                             # 本轮真实存在的作品键 → 签名
        for cat, books in (lib or {}).items():
            for book, vols in books.items():
                key = normalize_key(f"{cat}/{book}")
                if key and vols:
                    live[key] = _sig(vols)

        if first_run:
            # 基线：只看不提示。「装上功能」≠「全库都有更新」。
            state["seen"] = live
            dirty = True
            log.info("新增卷提示：已建立书库基线（%d 部作品），本次不提示任何更新", len(live))
        else:
            for key, cur in live.items():
                prev = seen.get(key)
                if prev is None:
                    continue                  # 新出现的作品：不提示（见模块文档）
                added = [r for r in cur if r not in prev]
                if not added:
                    continue
                gone = [r for r in prev if r not in cur]
                new_vols = _real_additions(added, gone, cur, prev)
                if new_vols:
                    old = pending.get(key) or {"vols": []}
                    pending[key] = {"at": now,
                                    "vols": list(dict.fromkeys(old["vols"] + new_vols))}
                    dirty = True
                    log.info("检测到新增卷：%s +%d 卷", key, len(new_vols))
            if live != seen:
                state["seen"] = live
                dirty = True

        # 收尾清理：作品没了 / 新卷又被删了 → 提示一并收回（不留点不开的死条目）
        for key in list(pending):
            cur = live.get(key)
            if not cur:
                del pending[key]
                dirty = True
                continue
            keep = [r for r in pending[key]["vols"] if r in cur]
            if keep != pending[key]["vols"]:
                if keep:
                    pending[key]["vols"] = keep
                else:
                    del pending[key]
                dirty = True

        if dirty:
            _save(state)
        return _copy_pending(pending)


# ---------------------------- 读取 / 清理 ----------------------------
def load_pending():
    """只看不扫：返回 ``{作品键: {"at":…, "vols":[…]}}`` 的副本。"""
    with _LOCK:
        return _copy_pending(_load()["pending"])


def pending_for(key):
    key = normalize_key(key)
    return load_pending().get(key) if key else None


def clear(key=None):
    """清除提示：``key`` 为空则全清。返回清掉的条数。

    键非法返回 ``0`` 且不落盘（调用方据此回 400）。
    """
    with _LOCK:
        state = _load()
        pending = state["pending"]
        if not key:
            n = len(pending)
            if n:
                state["pending"] = {}
                _save(state)
            return n
        key = normalize_key(key)
        if not key or key not in pending:
            return 0
        del pending[key]
        _save(state)
        return 1


def order_keys(keys, pending):
    """把「有更新」的作品排到最前（新→旧），其余保持原字母序，返回新列表。

    排序发生在分页**之前**，所以第 3 页的书冒出新卷时会真的出现在第一页。
    """
    if not pending:
        return list(keys)
    hot = sorted((k for k in keys if k in pending),
                 key=lambda k: (pending[k].get("at") or "", k.lower()), reverse=True)
    return hot + [k for k in keys if k not in pending]


def counts():
    """``{分类: 有更新的作品数}``，给首页分类卡用。"""
    per = {c: 0 for c in CATEGORY_DIRS}
    for key in load_pending():
        cat = key.split("/", 1)[0]
        if cat in per:
            per[cat] += 1
    return per
