#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「已读完」清单：把作品标记成已读，持久化到 ``.autosync/finished.json``。

设计要点
--------
* **粒度是「作品」不是「卷」** —— 键用 ``分类/书名``（与 :func:`library.get_library`
  的层级一致，也是 OPDS 详情页 URL 里的那段），因为「读完了」是作品级状态。
* **运行时状态，不进仓库**：文件落在 ``.autosync/``（已在 .gitignore），与
  ``mirror_state.json`` / ``meta_cache.json`` 同类，不会被同步到 GitHub / F 盘。
* **写入原子**：临时文件 + :func:`os.replace`，且 ``fsync`` 过。半途断电也只会留下
  旧文件，不会出现半个 JSON（旧实现踩过「写一半」的坑）。
* **读取永远不抛**：文件缺失 / 损坏 / 手改坏 → 当成空清单，服务照跑。
* **线程安全**：``ThreadingHTTPServer`` 下多个请求并发，「读-改-写」全程持锁，
  否则两个并发的标记会互相覆盖（丢更新）。
"""

import json
import logging
import os
import tempfile
import threading
import time

from ..paths import CATEGORY_DIRS, FINISHED_FILE

log = logging.getLogger("sync")

_LOCK = threading.RLock()
# 清单很小（几十到几百个键），每次读盘的开销可忽略；不做缓存是为了让用户手改
# finished.json 后刷页面即刻生效，少一个「改了没反应」的排查点。

_FILE_VERSION = 1


def normalize_key(key):
    """校验并规范化 ``分类/书名``；不合法返回 ``None``。

    只放行「``分类`` 是已知分类、书名非空、且不含任何穿越片段」的键 ——
    这是 POST 参数进服务端前的唯一入口校验，防止把 ``../`` 或任意字符串写进清单。
    """
    if not key or not isinstance(key, str):
        return None
    key = key.replace("\\", "/").strip().strip("/")
    if ".." in key.split("/"):
        return None
    parts = key.split("/")
    if len(parts) != 2:
        return None
    cat, book = parts[0].strip(), parts[1].strip()
    if cat not in CATEGORY_DIRS or not book:
        return None
    return f"{cat}/{book}"


def _read_keys():
    """读盘得到键集合。文件不存在 / 损坏 / 结构不对都返回空集合（不抛）。"""
    try:
        with open(FINISHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        log.warning("已读完清单读取失败（按空处理）：%s", exc)
        return set()
    raw = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return set()
    return {k for k in (normalize_key(str(x)) for x in raw) if k}


def _write_keys(keys):
    """原子写入：临时文件 → fsync → :func:`os.replace` 覆盖。"""
    payload = {
        "version": _FILE_VERSION,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(keys),
        "keys": sorted(keys),
    }
    directory = os.path.dirname(FINISHED_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".finished-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FINISHED_FILE)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_finished():
    """返回已读完键集合（副本，调用方可随意改）。"""
    with _LOCK:
        return set(_read_keys())


def is_finished(key):
    key = normalize_key(key)
    if not key:
        return False
    with _LOCK:
        return key in _read_keys()


def mark_finished(key, on=True):
    """把作品设为「已读完 / 未读完」，返回**操作后的状态**。

    键非法时返回 ``False`` 且不落盘（调用方据此回 400）。
    已经是目标状态时不写盘（避免无意义地改动 mtime / 触发文件监控）。
    """
    key = normalize_key(key)
    if not key:
        return False
    with _LOCK:
        keys = _read_keys()
        changed = (key not in keys) if on else (key in keys)
        if changed:
            keys.add(key) if on else keys.discard(key)
            _write_keys(keys)
        return on


def toggle_finished(key):
    """翻转状态，返回**操作后的状态**（True = 现在是已读完）。"""
    key = normalize_key(key)
    if not key:
        return False
    with _LOCK:
        keys = _read_keys()
        new_state = key not in keys
        keys.add(key) if new_state else keys.discard(key)
        _write_keys(keys)
        return new_state
