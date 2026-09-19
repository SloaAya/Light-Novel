#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Moon+ Reader（静读天下）阅读进度 —— 只读桥接层。

## 它做什么

把手机阅读器同步到网盘的那份「阅读位置」读进来，换成 OPDS 书库能用的
``书库相对路径 → 进度百分比`` 映射，供封面角标 / 详情页进度环 / 「已读完」派生视图使用。

## 为什么是这个形状

``.Moon+`` 目录在**网盘挂载盘**上（CloudDrive2 挂载的 ``F:``），实测**每个小文件一次
网络往返约 55 ms** —— 读 128 个 ``.po`` 要 6.7 秒。因此：

* **请求路径上只读内存**，绝不现读网盘；刷新交给一个 daemon 线程（TTL 见
  ``paths.MOON_TTL``，默认 **5 秒**），失败就沿用旧数据（网盘偶尔掉线不该拖垮书库）。
* 解析结果同时落一份本地缓存，进程重启后第一次刷新是毫秒级；**校验戳变化时只重读
  变了的那几个文件**（增量，见 :func:`read_positions`）—— 这是刷新周期敢设到 5 秒的
  前提：一轮刷新通常只花一次目录 stat（约 7 ms），而不是几秒的全量读。
* 唯一缓存的东西是 ``rel → 百分比``；「每部作品读了几卷」这类聚合全部**现算**
  （纯字典运算，1659 卷也就亚毫秒），避免聚合结果跟着缓存一起变陈旧。

## 关联方式（唯一的键是「卷文件名字符串」）

Moon+ 的 ``.po`` 按**卷文件名**命名，没有任何 id / ISBN 可依赖，所以：

1. 先按**卷标题严格相等**匹配；
2. 不中就按 ``norm_key()`` 归一化后匹配 —— ``第X卷`` ↔ ``XX``、汉字数字 ↔ 阿拉伯、
   补零、去标点空格，另支持用户自己的「``第三之五卷`` ↔ ``3.5``」约定；
3. 还不行就只能放弃（实测常见于：书库改名成完全不规则的写法、该书不在书库里）。

实测关联率（128 条位置记录）：严格 102 → 加归一化 115（未关联 13，歧义 1）。
剩下的未关联主要是「书库改了名、写法与阅读器完全对不上」和「不在书库里」。

## 判定语义

* **单卷读完**：该卷百分比 ``>= paths.MOON_DONE_PERCENT``（默认 99，不是 100 ——
  Moon+ 的百分比是估算值，实测有 99.4% / 97.9% 这类「读到底但没到 100」）。
* **整部读完**：作品下**所有**卷都读完 → 才归入「已读完」（派生视图，**不写**
  ``finished.json``）。绝不能自动写人工清单：``updates.observe()`` 有「出现新卷 →
  自动从已读完剔除」的联动，自动判定每轮写回会与它互相拉扯（加回 → 删 → 又加回）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from ..paths import (
    MOON_DONE_PERCENT, MOON_ENABLED, MOON_POS_FILE, MOON_PUBLIC, MOON_ROOT, MOON_TTL,
)
from . import library

log = logging.getLogger("sync")

# ---------------------------------------------------------------- 格式常量

# .po 文法：dev * x [@ y # z] : p%      （PDF 没有 @y#z 段）
_POS_RE = re.compile(
    r"^(?P<dev>\d+)\*(?P<sec>\d+)"
    r"(?:@(?P<sub>\d+)#(?P<off>\d+))?:(?P<pct>\d+(?:\.\d+)?)%$")

# 小数卷号的「汉字之汉字」写法（用户自己的约定：第三之五卷 = 3.5），必须在通用式之前试
_CN_DOT_RE = re.compile(r"^(?P<base>.*?)[\s\-_]*第(?P<a>[零一二三四五六七八九十]+)"
                        r"之(?P<b>[零一二三四五六七八九十]+)卷$")
# 通用卷号：可选的「第」前缀 + 汉字/阿拉伯数字（可带小数）+ 可选的「卷」
_VOL_RE = re.compile(r"^(?P<base>.*?)[\s\-_]*第?"
                     r"(?P<num>[零一二三四五六七八九十百]+|\d+(?:\.\d+)?)\s*卷?$")

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100}
_STRIP = re.compile(r"[\s_\-·・（）()\[\]【】{}，,、。！？!?\u3000]")


def _cn2int(s):
    """汉字数字 → int（``十二`` → 12，``二十一`` → 21，``一百二十三`` → 123）。

    **必须带量级概念**：纯按字符累加会把「十三」算成 3（十位没进账）。
    非汉字数字直接 ``int()``；出现不认识的字符返回 ``None``。
    """
    if s.isdigit():
        return int(s)
    section, number = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            section += (number or 1) * _CN_UNIT[ch]      # 单独的「十」= 10，不是 0
            number = 0
        else:
            return None
    return section + number


def _vol_token(raw):
    """把卷号串统一成 ``'01'`` / ``'10.5'`` 这种两位补零形式。"""
    if "." in raw:
        try:
            return "%g" % float(raw)
        except ValueError:
            return raw
    try:
        return "%02d" % _cn2int(raw)
    except (TypeError, ValueError):
        return raw


def norm_key(title):
    """卷标题 → ``(归一化基底, 归一化卷号)``。

    基底去掉标点空格并转小写；卷号统一成两位补零（小数保留）。
    没有卷号可识别时第二项为 ``""``。
    """
    t = (title or "").strip()
    m = _CN_DOT_RE.match(t)
    if m:
        try:
            return (_STRIP.sub("", m.group("base")).lower(),
                    "%g" % (_cn2int(m.group("a")) + _cn2int(m.group("b")) / 10.0))
        except (TypeError, ValueError):
            pass
    m = _VOL_RE.match(t)
    if not m:
        return _STRIP.sub("", t).lower(), ""
    base = m.group("base") or t
    return _STRIP.sub("", base).lower(), _vol_token(m.group("num"))


def parse_position(text):
    """解析一条 ``.po`` 内容；无法识别返回 ``None``。

    字段语义：``dev`` = Moon+ **设备 id**（不是时间戳：同一目录跨月只出现固定几个取值，
    且其一等于 ``books.id``）；``x`` = PDF 页码 / EPUB 零基章节号；``y`` = 次级章节
    （通常 0，但不要假设恒为 0）；``z`` = 章节内字符偏移；``p`` = **仅供显示**的百分比。
    """
    m = _POS_RE.match((text or "").strip())
    if not m:
        return None
    return {
        "device_id": m.group("dev"),
        "section": int(m.group("sec")),
        "sub_section": int(m.group("sub")) if m.group("sub") is not None else None,
        "offset": int(m.group("off")) if m.group("off") is not None else None,
        "percent": float(m.group("pct")),
        "is_pdf": m.group("sub") is None,
    }


# ---------------------------------------------------------------- 读取位置

# 本地缓存的格式版本。2 起多存一份 ``by_file``（文件名 → 卷标题）—— 增量重读要靠它
# 定位「这个文件上一次产出的旧记录」；没有它的旧缓存全量重读一次即自动升级。
_CACHE_VERSION = 2


def read_positions(root=None, cache_file=None, use_cache=True):
    """读 ``<root>/Cache/*.po`` → ``({卷标题: 位置信息}, meta)``。

    带本地缓存：缓存里存 ``{文件名: [mtime, size]}`` 当校验戳，一致就直接返回
    （网络盘几十次 stat 约 7 ms，而全量重读要数秒）。

    **校验戳不一致时只重读变化的那几个文件**（增量）：网盘上每个小文件一次往返
    约 55 ms，几十个全读要数秒；而实际变化通常只有一两个（阅读器正读到的那本）。
    实测把一次刷新从 7.3 s 压到零点几秒 —— 刷新周期才敢设到 5 秒
    （``paths.MOON_TTL``），否则短周期会让后台线程几乎一直泡在网盘读里。
    """
    root = root or MOON_ROOT
    cache_file = cache_file or MOON_POS_FILE
    cache_dir = os.path.join(root, "Cache")
    try:
        names = sorted(os.listdir(cache_dir))
    except OSError as exc:
        return None, {"error": "读不到 %s：%s" % (cache_dir, exc), "from_cache": False}

    stamp = {}
    for n in names:
        try:
            st = os.stat(os.path.join(cache_dir, n))
            # mtime 取**亚秒**精度（不取 int）：阅读器更新进度时常常把同一个文件
            # 改写成等长的内容（``...:2.6%`` → ``...:60.0%`` 长度就可能一样），
            # 秒级精度 + 同 size 会让校验戳看不出变化，进度永远刷不出来。
            stamp[n] = [st.st_mtime, st.st_size]
        except OSError:
            stamp[n] = None

    cached = {}
    if use_cache and os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cached = json.load(fh)
        except (OSError, ValueError):
            pass          # 缓存坏了就当没有，重读一遍即可
    if not isinstance(cached, dict):
        cached = {}

    prev_stamp = cached.get("stamp")
    prev_by = cached.get("by_file")
    prev_pos = cached.get("positions")
    reusable = (isinstance(prev_stamp, dict) and isinstance(prev_by, dict)
                and isinstance(prev_pos, dict))

    if reusable and prev_stamp == stamp:
        return prev_pos, {"from_cache": True, "elapsed_ms": 0.0, "changed": 0}

    t0 = time.perf_counter()
    if reusable:
        positions, by_file = dict(prev_pos), dict(prev_by)
        todo = [n for n in names if stamp.get(n) != prev_stamp.get(n)]
        for n in [k for k in by_file if k not in stamp]:     # 网盘上已被删掉的
            positions.pop(by_file.pop(n), None)
    else:
        positions, by_file, todo = {}, {}, list(names)

    for n in todo:
        if not n.lower().endswith(".po"):
            continue
        # 同一文件内容变了：先摘掉它的旧记录，免得同一卷留下两条（标题也可能变）
        positions.pop(by_file.pop(n, None), None)
        try:
            with open(os.path.join(cache_dir, n), "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        pos = parse_position(text)
        if pos is None:
            continue
        # 文件名 = <卷标题>.<ext>.po → 键取「卷标题」
        title = os.path.splitext(os.path.splitext(n)[0])[0]
        pos["ext"] = os.path.splitext(os.path.splitext(n)[0])[1].lower()
        positions[title] = pos
        by_file[n] = title
    elapsed = (time.perf_counter() - t0) * 1000

    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": _CACHE_VERSION, "stamp": stamp,
                       "by_file": by_file, "positions": positions},
                      fh, ensure_ascii=False)
        os.replace(tmp, cache_file)          # 原子替换：半个文件也不会被读走
    except OSError:
        pass

    return positions, {"from_cache": False, "elapsed_ms": elapsed,
                       "changed": len(todo)}


def join_library(positions, lib=None):
    """``{卷标题: 位置}`` × 书库索引 → ``({书库相对路径: 百分比}, meta)``。

    关联阶梯：卷标题严格相等 → ``norm_key`` 归一化。同名卷命中多个候选时取路径最短的
    那个，并把歧义数记进 meta（不静默吞掉）。
    """
    lib = lib if lib is not None else library.get_library()
    exact, norm = {}, {}
    for _cat, books in lib.items():
        for _book, vols in books.items():
            for v in vols:
                exact.setdefault(v["title"], []).append(v["rel"])
                norm.setdefault(norm_key(v["title"]), []).append(v["rel"])

    rel_pct, ambiguous, unmatched, matched = {}, 0, [], 0
    for title, pos in positions.items():
        cands = exact.get(title)
        if not cands:
            cands = norm.get(norm_key(title))
        if not cands:
            unmatched.append(title)
            continue
        matched += 1
        if len(cands) > 1:
            ambiguous += 1
            cands = sorted(cands, key=lambda r: (len(r), r))
        rel_pct[cands[0]] = pos["percent"]

    # matched = 关联上的位置记录条数；rels = 最终覆盖的卷数（两条记录落到同一卷时会少）
    return rel_pct, {"matched": matched, "unmatched": len(unmatched),
                     "ambiguous": ambiguous}


# ---------------------------------------------------------------- 状态与刷新

_state = {
    "rel_pct": {},
    "meta": {"ok": False, "at": 0.0, "error": "", "po_files": 0,
             "matched": 0, "unmatched": 0, "ambiguous": 0, "elapsed_ms": 0.0},
}
_lock = threading.RLock()
_worker = {"started": False, "wake": threading.Event(), "busy": False}


def enabled():
    return bool(MOON_ENABLED)


def visible(is_admin=False):
    """进度信息对谁可见：默认所有人（``MOON_PUBLIC``）；设 0 则仅管理员。"""
    return enabled() and (MOON_PUBLIC or bool(is_admin))


def _swap(rel_pct, **meta):
    """原子替换进度表（刷新线程与测试都走这里）。"""
    with _lock:
        _state["rel_pct"] = rel_pct
        _state["meta"].update(meta)


def snapshot():
    """给 /opds/stats 用的只读状态快照。"""
    with _lock:
        meta = dict(_state["meta"])
        n = len(_state["rel_pct"])
    meta.update({"enabled": enabled(), "root": MOON_ROOT, "ttl": MOON_TTL,
                 "done_percent": MOON_DONE_PERCENT, "rel_entries": n,
                 "public": MOON_PUBLIC})
    return meta


def _reload():
    """读一次网盘 + 关联书库，成功才替换状态（失败保留旧数据）。"""
    if not enabled():
        return
    positions, rmeta = read_positions()
    if positions is None:                        # 网盘没挂载 / 目录不存在
        _swap(_state["rel_pct"], ok=False, error=rmeta.get("error", ""),
              at=time.time())
        return
    rel_pct, jmeta = join_library(positions)
    _swap(rel_pct, ok=True, error="", at=time.time(),
          po_files=len(positions), elapsed_ms=rmeta.get("elapsed_ms", 0.0), **jmeta)


def _loop():
    last = None
    while True:
        try:
            _reload()
            snap = snapshot()
            # 刷新周期只有几秒，每次都打日志会把日志刷满 —— 只在**结果真的变了**
            # （或从正常掉进读不到）时记一条。elapsed_ms 不参与比较，它是耗时不是状态。
            sig = (snap["ok"], snap["po_files"], snap["rel_entries"],
                   snap["unmatched"], snap["ambiguous"])
            if sig != last:
                last = sig
                if snap["ok"]:
                    log.info("Moon+ 进度已刷新：%d 条位置记录，关联书库 %d 卷"
                             "（未关联 %d，歧义 %d，耗时 %.0f ms）",
                             snap["po_files"], snap["rel_entries"],
                             snap["unmatched"], snap["ambiguous"], snap["elapsed_ms"])
                else:
                    log.warning("Moon+ 进度读取失败：%s（沿用上一次的数据）",
                                snap["error"] or "未知原因")
        except Exception:                        # 后台线程绝不能把主服务带下去
            log.exception("Moon+ 进度刷新失败（沿用上一次的数据）")
        _worker["wake"].wait(MOON_TTL)
        _worker["wake"].clear()


def ensure_worker():
    """启动后台刷新线程（幂等）。由 ``run_service()`` 调用；测试不走这里。"""
    if not enabled():
        return False
    with _lock:
        if _worker["started"]:
            return False
        _worker["started"] = True
    threading.Thread(target=_loop, name="moon-progress", daemon=True).start()
    return True


def wake():
    """让后台线程立刻刷新一次（不等 TTL）。"""
    if enabled() and _worker["started"]:
        _worker["wake"].set()


# ---------------------------------------------------------------- 查询 API

def percent_of(rel):
    """某卷的进度百分比；没有记录返回 ``None``（≠ 0，别把「没读过」和「没数据」混为一谈）。"""
    if not enabled():
        return None
    with _lock:
        return _state["rel_pct"].get(rel)


def vol_progress(rel):
    """某卷 → ``None``（无记录）或 ``(是否已读完, 百分比)``。

    阈值判定留在本模块内，渲染层不必知道 ``MOON_DONE_PERCENT`` 是多少。
    """
    p = percent_of(rel)
    if p is None:
        return None
    return (p >= MOON_DONE_PERCENT, p)


def stat_of(vols):
    """一部作品的进度统计；**一卷都没有记录时返回 ``None``**（调用方据此不渲染角标）。

    返回 ``{"total","done","seen","percent","all"}``：
    ``done`` = 百分比 >= 阈值的卷数，``seen`` = 有记录的卷数，
    ``percent`` = 全部卷的平均（缺记录的卷按 0 计），``all`` = 整部读完。

    ``all`` 的口径是**作品下每一卷都读完**，番外与不同版本同样计入（例如「书名
    01..12」+「书名 Ver.β 01..03」要 15 卷全读完）。这是刻意的：它决定「已读完」
    的入库资格，而清单页那张卡的副标题写的就是这本书的总卷数 —— 放宽成「正篇读完
    就入库」会让卡片一边写着 15 卷、一边显示 12/12 卷，自己跟自己矛盾。真读完了
    别版自然会入库，没读就别占着「已读完」这个名分。
    """
    if not enabled() or not vols:
        return None
    with _lock:
        table = _state["rel_pct"]
    pcts = [table[v["rel"]] for v in vols if v["rel"] in table]
    if not pcts:
        return None
    total = len(vols)
    done = sum(1 for p in pcts if p >= MOON_DONE_PERCENT)
    return {
        "total": total,
        "seen": len(pcts),
        "done": done,
        "percent": round(sum(pcts) / total, 1),
        "all": done >= total,
    }


def auto_finished(lib=None):
    """「Moon+ 自动判定已读完」的作品 → ``{键: 统计}``（键 = ``分类/书名``，与人工清单同格式）。

    **只读**：结果永远不写进 ``finished.json``。人工清单只存人工结论。
    """
    if not enabled():
        return {}
    lib = lib if lib is not None else library.get_library()
    out = {}
    for cat, books in lib.items():
        for book, vols in books.items():
            st = stat_of(vols)
            if st and st["all"]:
                out["%s/%s" % (cat, book)] = st
    return out
