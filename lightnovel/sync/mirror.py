#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F 盘网盘镜像：清单、差异比对（查）、删除、增改删执行、状态看板。"""

import json
import logging
import os
import shutil
import time

from ..paths import (
    CATEGORY_DIRS,
    CATEGORY_DONE,
    CATEGORY_ONGOING,
    CONFLICT_MTIME_SLACK,
    F_CATEGORY_DIRS,
    F_RECENT_PROTECT_SEC,
    F_TARGET_ROOT,
    LIGHT_NOVEL_DIR,
    LOG_DIR,
    MAX_MIRROR_DELETIONS,
    MIRROR_HTML_FILE,
    MIRROR_LOG_FILE,
    MIRROR_MANIFEST,
    MIRROR_REPORT_FILE,
    MIRROR_STATE_FILE,
    MTIME_TOLERANCE,
)
from .gitops import (
    EXCLUDE_FILE_NAMES,
    IGNORE_NAMES,
    _now_iso,
    ensure_category_dirs,
    git_commit,
    push_with_retry,
    regenerate_readme,
    remove_remote_extras,
    scan_dirs,
    scan_tree,
)

log = logging.getLogger("sync")

def _append_mirror_log(records):
    """把操作明细以 JSON Lines 追加写入 .autosync/mirror.log（审计日志）。"""
    if not records:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(MIRROR_LOG_FILE, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("写入镜像审计日志失败：%s", exc)


def _save_json(path, data):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)          # 原子替换，避免写一半被读
    except OSError as exc:
        log.warning("写入 %s 失败：%s", path, exc)


# ---------------------------- 镜像清单：区分「我们放的」与「F 盘外来文件」 ----------------------------
def _load_manifest():
    """读取镜像清单 {分类: set(相对路径)}。文件不存在 / 损坏时返回空 dict。"""
    if not os.path.isfile(MIRROR_MANIFEST):
        return {}
    try:
        with open(MIRROR_MANIFEST, "r", encoding="utf-8") as fh:
            return {k: set(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError) as exc:
        log.warning("镜像清单读取失败（将按空清单处理）：%s", exc)
        return {}


def _save_manifest(manifest):
    _save_json(MIRROR_MANIFEST, {k: sorted(v) for k, v in manifest.items()})


def ensure_manifest_seeded():
    """首次运行时用 F 盘现有文件初始化清单（历史文件都是本工具镜像过去的）。
    之后由每次同步增量维护。返回 True 表示本次发生了初始化。"""
    if os.path.isfile(MIRROR_MANIFEST) or not os.path.isdir(F_TARGET_ROOT):
        return False
    manifest = {}
    for cat, dst in F_CATEGORY_DIRS.items():
        manifest[cat] = set(scan_tree(dst).keys())
    _save_manifest(manifest)
    log.info("首次运行：已用 F 盘现有文件初始化镜像清单（%s）。",
             {k: len(v) for k, v in manifest.items()})
    return True


# ---------------------------- 查：差异比对（只读） ----------------------------
def plan_mirror(src, dst, known=None):
    """比对源（D）与目标（F），生成「增 / 改 / 删」计划并检测冲突。
    known = 镜像清单中该分类「本工具曾写入」的相对路径集合；用于区分
            「我们放上去、D 盘已删 → 可安全删除」与「F 盘外来文件 → 保护并告警」。
    纯只读，绝不修改任何文件。返回结构化计划 dict。"""
    s_tree = scan_tree(src)
    d_tree = scan_tree(dst)
    now = time.time()

    add, update, keep, delete, conflicts = [], [], [], [], []

    for rel, sm in s_tree.items():
        dm = d_tree.get(rel)
        if dm is None:
            add.append({"rel": rel, "size": sm["size"]})
            continue
        # 改：大小不同 或 mtime 差异超过容差
        if dm["size"] == sm["size"] and abs(dm["mtime"] - sm["mtime"]) < MTIME_TOLERANCE:
            keep.append(rel)
            continue
        update.append({
            "rel": rel,
            "src_size": sm["size"], "dst_size": dm["size"],
            "src_mtime": sm["mtime"], "dst_mtime": dm["mtime"],
        })
        # 冲突检测 1：F 侧比 D 侧更新且大小不同 → F 侧可能被独立修改过
        if dm["size"] != sm["size"] and (dm["mtime"] - sm["mtime"]) > CONFLICT_MTIME_SLACK:
            conflicts.append({
                "kind": "F_NEWER",
                "rel": rel,
                "detail": "F 盘副本比 D 盘源文件更新且大小不同，D 盘为唯一数据源，本次将以 D 盘覆盖",
                "dst_size": dm["size"], "src_size": sm["size"],
                "dst_mtime": dm["mtime"], "src_mtime": sm["mtime"],
            })

    for rel, dm in d_tree.items():
        if rel in s_tree:
            continue
        rec = {"rel": rel, "size": dm["size"], "mtime": dm["mtime"],
               "ours": bool(known is not None and rel in known)}
        if not rec["ours"] and (known is not None) and (now - dm["mtime"]) < F_RECENT_PROTECT_SEC:
            # 冲突检测 2：既不在镜像清单里、又是 24h 内新建/修改 → F 盘独有资料，先保护不删
            rec["protected"] = True
            conflicts.append({
                "kind": "F_FOREIGN_RECENT",
                "rel": rel,
                "detail": "该文件仅存在于 F 盘、不在镜像清单中且 24 小时内被新建/修改，"
                          "疑似 F 盘独有资料，已跳过删除，请人工确认",
                "dst_size": dm["size"], "dst_mtime": dm["mtime"],
            })
        elif not rec["ours"] and (known is not None):
            # 冲突检测 3：清单外的历史遗留文件，D 盘没有 → 按 D 为准删除，但明确告警
            rec["protected"] = False
            conflicts.append({
                "kind": "F_ORPHAN",
                "rel": rel,
                "detail": "该文件仅存在于 F 盘且不在镜像清单中（历史遗留/外部写入），"
                          "按 D 盘为唯一数据源将删除之",
                "dst_size": dm["size"], "dst_mtime": dm["mtime"],
            })
        else:
            rec["protected"] = False
        delete.append(rec)

    add.sort(key=lambda x: x["rel"])
    update.sort(key=lambda x: x["rel"])
    delete.sort(key=lambda x: x["rel"])
    return {
        "src": src, "dst": dst, "scanned_at": _now_iso(),
        "add": add,
        "update": update,
        "delete": delete,
        "conflicts": conflicts,
        "counts": {
            "add": len(add),
            "update": len(update),
            "delete": sum(1 for d in delete if not d["protected"]),
            "protected": sum(1 for d in delete if d["protected"]),
            "unchanged": len(keep),
            "conflict": len(conflicts),
            "src_total": len(s_tree),
            "dst_total": len(d_tree),
        },
    }


def mirror_query():
    """查：汇总 D→F 全部分类目录的差异，返回总报告 dict（只读）。"""
    report = {
        "generated_at": _now_iso(),
        "source_root": LIGHT_NOVEL_DIR,
        "target_root": F_TARGET_ROOT,
        "f_mounted": os.path.isdir(F_TARGET_ROOT),
        "categories": {},
        "counts": {"add": 0, "update": 0, "delete": 0, "protected": 0, "orphan": 0,
                   "orphan_dirs": 0, "unchanged": 0, "conflict": 0,
                   "src_total": 0, "dst_total": 0},
    }
    if not report["f_mounted"]:
        return report
    manifest = _load_manifest()
    for cat, src in CATEGORY_DIRS.items():
        plan = plan_mirror(src, F_CATEGORY_DIRS[cat], known=manifest.get(cat))
        plan["counts"]["orphan"] = sum(1 for d in plan["delete"]
                                       if not d["ours"] and not d["protected"])
        # 孤儿目录数（只读，不动手）
        src_dirs = {d for d in scan_dirs(src) if d}
        dst_dirs = {d for d in scan_dirs(F_CATEGORY_DIRS[cat]) if d}
        foreign_dirs = {d for d in (dst_dirs - src_dirs)
                        if not d.split("/", 1)[0].startswith(".trash")}
        plan["counts"]["orphan_dirs"] = len(foreign_dirs)
        report["categories"][cat] = plan
        for k in report["counts"]:
            report["counts"][k] += plan["counts"].get(k, 0)
    _save_json(MIRROR_REPORT_FILE, report)
    return report


# ---------------------------- 删除：直接删除 ----------------------------
def _delete_file(abs_path):
    """直接删除 F 侧待删文件（不可逆）。返回 True/False。"""
    try:
        os.chmod(abs_path, 0o666)    # 去掉只读属性（Windows 只读文件无法删除）
    except OSError:
        pass
    try:
        os.remove(abs_path)
        return True
    except OSError as exc:
        log.warning("删除失败 %s：%s", abs_path, exc)
        return False


def _prune_empty_dirs(dst):
    """删除 dst 下因文件被删而变空的目录（自底向上），返回删除目录数。
    用 scan_dirs 避免 os.walk 在 CloudDrive2 挂载盘上跳过空目录。"""
    removed = 0
    for rel in sorted(scan_dirs(dst), key=lambda d: d.count("/"), reverse=True):
        if not rel:                                              # 跳过根目录本身
            continue
        abs_dir = os.path.join(dst, rel.replace("/", os.sep))
        try:
            # 空 = 无子目录且无文件
            with os.scandir(abs_dir) as it:
                if not any(True for _ in it):
                    os.rmdir(abs_dir)
                    removed += 1
        except OSError:
            pass
    return removed


def _prune_foreign_subtrees(src, dst, manifest_set, allow_delete=True):
    """递归处理 F 侧「整棵孤儿子树」——源目录里没有的整个目录树（含子目录/文件）整体直接删除。
    与 _prune_empty_dirs 互补：后者只清"删完文件自然变空"的目录；本函数负责
    清理"我们压根没源对应"或"还有遗留文件没被识别为 delete"的整棵子树。
    安全网：
      1) 仅在 dst 范围内操作：每个候选目录做 abs+commonpath 校验，禁止触碰 dst 之外
      2) 24h 内修改过的 F-only 子树 → 跳过并告警（防误删刚被外部工具新建的内容）
      3) src 不存在 → 不执行任何删除（防 D 盘挂载失败引发误删）
      4) 操作失败逐项 log，绝不阻断主流程
    返回 (moved_dirs, skipped_dirs)，都是相对于 dst 的 rel 路径列表。"""
    moved, skipped = [], []
    if not os.path.isdir(src):
        return moved, skipped                                  # D 盘不可用 → 直接返回，绝不删 F
    # 0) 安全校验：把 dst 解析为绝对路径并规整化，后续所有 abs_dir 必须在其下
    try:
        dst_abs = os.path.realpath(os.path.abspath(dst))
    except OSError:
        return moved, skipped
    # 1) 收集 D / F 各自的「中间目录」rel（用 scan_dirs 而非 os.walk —— CD2 挂载盘
    #    上 os.walk 不 yield 空目录，会漏掉"整本书已删"的空目录残留）
    src_dirs = {d for d in scan_dirs(src) if d}
    dst_dirs = {d for d in scan_dirs(dst) if d}
    # 2) F 侧存在但 D 侧没有 → 整棵孤儿子树（`.trash` 历史残留不在镜像范围内，不清理）
    foreign = dst_dirs - src_dirs
    foreign = {d for d in foreign if not d.split("/", 1)[0].startswith(".trash")}
    if not foreign:
        return moved, skipped
    # 3) 自底向上：先删最深层的子树（避免父目录被先移走后子目录路径失效）
    foreign_sorted = sorted(foreign, key=lambda d: d.count("/"), reverse=True)
    cutoff = time.time() - F_RECENT_PROTECT_SEC
    for rel in foreign_sorted:
        abs_dir = os.path.normpath(os.path.join(dst_abs, rel.replace("/", os.sep)))
        # 1) 安全网：严格校验 abs_dir 在 dst_abs 之下，防止任何逃逸（symlink / 盘符/.. 攻击）
        try:
            abs_real = os.path.realpath(abs_dir)
        except OSError:
            skipped.append(rel)
            log.warning("【安全网】无法解析 F 侧孤儿子树路径：%s", rel)
            continue
        if (os.path.commonpath([abs_real, dst_abs]) != dst_abs
                or abs_real == dst_abs):
            skipped.append(rel)
            log.error("【安全网】F 侧孤儿子树路径逃逸 dst，拒绝操作：%s -> %s", rel, abs_real)
            continue
        if not os.path.isdir(abs_real):
            continue
        # 4) 24h 保护：子树内任一文件**或目录本身**的 mtime 在保护窗口内 → 跳过
        # 注意：apply_mirror 先把孤儿文件删除，子树会变空；
        # 只看子文件 mtime 会让 24h 保护失效。必须同时检查目录自身的 mtime。
        recent = False
        try:
            if os.path.getmtime(abs_real) > cutoff:
                recent = True
        except OSError:
            pass
        if not recent:
            for r, _, fs in os.walk(abs_real):
                for f in fs:
                    try:
                        if os.path.getmtime(os.path.join(r, f)) > cutoff:
                            recent = True
                            break
                    except OSError:
                        pass
                if recent:
                    break
        if recent:
            skipped.append(rel)
            log.warning("【安全网】F 侧孤儿子树 %s 含有 24h 内新增/修改的文件，跳过整棵删除，"
                        "请确认内容后再手动清理", rel)
            continue
        if not allow_delete:
            skipped.append(rel)
            continue
        # 5) 整棵直接删除（不可逆；上面已通过归属/路径/24h 多重校验）
        try:
            # 去掉只读属性（CD2 上整棵删除有时因只读失败）
            for r, _, fs in os.walk(abs_real):
                for f in fs:
                    try:
                        os.chmod(os.path.join(r, f), 0o666)
                    except OSError:
                        pass
            shutil.rmtree(abs_real)
            moved.append(rel)
            # 6) 从镜像清单移除该子树下的所有 rel
            if manifest_set is not None:
                prefix = rel + "/"
                for mrel in [m for m in manifest_set if m == rel or m.startswith(prefix)]:
                    manifest_set.discard(mrel)
        except OSError as exc:
            log.warning("无法删除 F 孤儿子树 %s：%s", abs_real, exc)
            skipped.append(rel)
    return moved, skipped


# ---------------------------- 增 / 改 / 删：执行 ----------------------------
def apply_mirror(plan, dry_run=False, allow_delete=True, manifest_set=None):
    """按计划执行镜像。dry_run=True 时只记录不落盘。返回执行结果 dict。
    manifest_set: 镜像清单中"本分类"的文件 rel 集合；孤儿子树被整棵移除时同步清理。"""
    src, dst = plan["src"], plan["dst"]
    cat = os.path.basename(dst)
    result = {"added": [], "updated": [], "deleted": [], "skipped": [],
              "failed": [], "dirs_removed": 0, "dirs_deleted": [],
              "dirs_skipped": [], "dry_run": dry_run}
    records = []

    def _rec(op, rel, status, extra=None):
        rec = {"ts": _now_iso(), "category": cat, "op": op, "rel": rel,
               "status": status, "dry_run": dry_run}
        if extra:
            rec.update(extra)
        records.append(rec)
        return rec

    # 删除保护：数量异常时拒绝执行（防止 D 盘挂载失败 / 目录被误换导致全量删除）
    del_count = plan["counts"]["delete"]
    if allow_delete and del_count > MAX_MIRROR_DELETIONS:
        log.error("【安全护栏】检测到 %s 有 %d 个待删文件，超过上限 %d，本次拒绝删除。"
                  "请确认 D 盘数据源完整后再运行；如需强制执行请调大 MAX_MIRROR_DELETIONS。",
                  cat, del_count, MAX_MIRROR_DELETIONS)
        allow_delete = False
        result["guard_triggered"] = True

    # ---- 增 / 改 ----
    for item in plan["add"] + plan["update"]:
        rel = item["rel"]
        op = "add" if item in plan["add"] else "update"
        sf = os.path.join(src, rel.replace("/", os.sep))
        tf = os.path.join(dst, rel.replace("/", os.sep))
        if dry_run:
            result["skipped"].append(rel); _rec(op, rel, "dry-run"); continue
        try:
            os.makedirs(os.path.dirname(tf), exist_ok=True)
            shutil.copy2(sf, tf)
            (result["added"] if op == "add" else result["updated"]).append(rel)
            _rec(op, rel, "ok", {"size": item.get("size") or item.get("src_size")})
        except OSError as exc:
            result["failed"].append({"rel": rel, "op": op, "error": str(exc)})
            _rec(op, rel, "failed", {"error": str(exc)})
            log.warning("镜像 %s 失败（%s）：%s", rel, op, exc)

    # ---- 删（直接删除）----
    if not allow_delete:
        for d in plan["delete"]:
            if not d["protected"]:
                result["skipped"].append(d["rel"])
                _rec("delete", d["rel"], "skipped", {"reason": "未开启删除或触发安全护栏"})
    else:
        for d in plan["delete"]:
            rel = d["rel"]
            if d.get("protected"):
                result["skipped"].append(rel)
                _rec("delete", rel, "skipped", {"reason": "F 侧近期修改，保护中（冲突）"})
                continue
            fp = os.path.join(dst, rel.replace("/", os.sep))
            if dry_run:
                result["skipped"].append(rel); _rec("delete", rel, "dry-run"); continue
            if _delete_file(fp):
                result["deleted"].append(rel)
                _rec("delete", rel, "ok")
            else:
                result["failed"].append({"rel": rel, "op": "delete", "error": "删除失败"})
                _rec("delete", rel, "failed")

    # ---- 清理孤儿子树（递归删除源中不存在的整棵子树，含子目录/文件/残留空目录）----
    # 关键：D 盘是唯一数据源，F 侧任何不存在的整棵子树都该被直接删除。
    # 失败兜底：删完文件后还要清空目录（_prune_empty_dirs 处理被并发残留的空目录）。
    if not dry_run:
        try:
            moved_dirs, skipped_dirs = _prune_foreign_subtrees(
                src, dst, manifest_set, allow_delete=allow_delete)
            result["dirs_deleted"] = moved_dirs
            result["dirs_skipped"] = skipped_dirs
            for d in moved_dirs:
                _rec("rmdir", d, "ok")
            for d in skipped_dirs:
                _rec("rmdir", d, "skipped", {"reason": "24h 保护 / allow_delete=False"})
        except Exception as exc:
            log.warning("清理 F 侧孤儿子树时异常：%s", exc)

    # ---- 清理空目录（兜底：并发残留 / 上面没覆盖到的）----
    if allow_delete and not dry_run:
        result["dirs_removed"] = _prune_empty_dirs(dst)

    _append_mirror_log(records)
    return result


def write_mirror_dashboard(report):
    """把镜像差异报告渲染成一张可读的 HTML 看板（每次 查 / 同步 后自动刷新）。"""
    try:
        c = report["counts"]
        consistent = (c["add"] == 0 and c["update"] == 0 and c["delete"] == 0)
        badge = ("✅ 两端完全一致" if consistent and report["f_mounted"]
                 else "🔄 存在差异待同步" if report["f_mounted"] else "⚠️ F 盘未挂载")
        color = "#16a34a" if consistent and report["f_mounted"] else (
            "#d97706" if report["f_mounted"] else "#dc2626")

        rows = []
        for cat, plan in report["categories"].items():
            pc = plan["counts"]
            rows.append(
                f"<tr><td><b>{cat}</b></td><td class=n>{pc['src_total']}</td>"
                f"<td class=n>{pc['dst_total']}</td>"
                f"<td class=n add>{pc['add']}</td><td class=n upd>{pc['update']}</td>"
                f"<td class=n del>{pc['delete']}</td><td class=n ok>{pc['unchanged']}</td>"
                f"<td class=n warn>{pc['conflict']}</td></tr>")

        items = []
        for cat, plan in report["categories"].items():
            for cf in plan["conflicts"]:
                items.append(f"<li class=cf><span class=tag>{cf['kind']}</span>"
                             f"<code>{cf['rel']}</code><br><span class=dt>{cf['detail']}</span></li>")
            for d in plan["delete"]:
                if not d["protected"]:
                    items.append(f"<li><span class=tag del>待删</span><code>{d['rel']}</code></li>")
        if not items:
            items.append("<li class=empty>无待处理项</li>")

        total = c["add"] + c["update"] + c["delete"]
        src_all = c["src_total"] or 1
        pct = round(c["unchanged"] / src_all * 100, 1) if src_all else 0.0

        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>D 盘 → F 盘 镜像状态</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--bd:#e3e6ea;--tx:#1f2328;--mut:#6b7280}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:960px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:12px;margin-bottom:20px}}
.badge{{display:inline-block;padding:6px 14px;border-radius:999px;color:#fff;
background:{color};font-weight:600;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:20px 0}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}}
.card .k{{color:var(--mut);font-size:12px}}
.card .v{{font-size:22px;font-weight:700;margin-top:2px}}
.add{{color:#2563eb}} .upd{{color:#d97706}} .del{{color:#dc2626}}
.ok{{color:#16a34a}} .warn{{color:#b45309}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--bd);border-radius:10px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--bd)}}
th{{background:#f0f2f5;font-size:12px;color:var(--mut)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
h2{{font-size:15px;margin:24px 0 10px}}
ul{{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:12px 12px 12px 30px;margin:0;max-height:340px;overflow:auto}}
li{{margin-bottom:8px}}
li.empty{{list-style:none;margin-left:-18px;color:var(--mut)}}
code{{background:#f0f2f5;padding:1px 6px;border-radius:4px;font-size:12.5px;
word-break:break-all}}
.tag{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:4px;
background:#eef2f7;color:#475569;margin-right:6px;vertical-align:1px}}
.tag.del{{background:#fee2e2;color:#b91c1c}}
li.cf code{{background:#fff7ed}}
.dt{{color:var(--mut);font-size:12px}}
.bar{{height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin-top:8px}}
.bar>i{{display:block;height:100%;background:#16a34a;width:{pct}%}}
.foot{{color:var(--mut);font-size:12px;margin-top:20px}}
</style></head><body><div class="wrap">
<h1>D 盘 → F 盘 网盘镜像状态</h1>
<div class="sub">数据源 <code>{report['source_root']}</code> · 备份目标 <code>{report['target_root']}</code></div>
<span class="badge">{badge}</span>
<div class="cards">
<div class="card"><div class="k">待处理合计</div><div class="v">{total}</div></div>
<div class="card"><div class="k">新增</div><div class="v add">{c['add']}</div></div>
<div class="card"><div class="k">修改</div><div class="v upd">{c['update']}</div></div>
<div class="card"><div class="k">删除</div><div class="v del">{c['delete']}</div></div>
<div class="card"><div class="k">一致</div><div class="v ok">{c['unchanged']}</div></div>
<div class="card"><div class="k">冲突</div><div class="v warn">{c['conflict']}</div></div>
</div>
<div class="bar"><i></i></div>
<div class="sub" style="margin:6px 0 20px">一致率 {pct}%（{c['unchanged']} / {c['src_total']}）</div>
<h2>分类明细</h2>
<table><thead><tr><th>分类</th><th class=n>D 盘</th><th class=n>F 盘</th>
<th class=n>增</th><th class=n>改</th><th class=n>删</th><th class=n>一致</th><th class=n>冲突</th>
</tr></thead><tbody>{''.join(rows) or '<tr><td colspan=8>无数据</td></tr>'}</tbody></table>
<h2>冲突与待处理项</h2>
<ul>{''.join(items)}</ul>
<div class="foot">生成时间 {report['generated_at']} · 完整报告 <code>mirror_report.json</code> ·
审计日志 <code>mirror.log</code>（删除为直接删除，仅作用于镜像目标中 D 盘不存在的多余内容）</div>
</div></body></html>"""
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = MIRROR_HTML_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, MIRROR_HTML_FILE)
    except Exception as exc:      # 看板失败不应影响同步主流程
        log.warning("生成镜像看板失败：%s", exc)


def _purge_excluded_files(dst):
    """删除 dst 目录树下已存在的系统垃圾文件（历史上被镜像过去的），返回删除数。
    删除失败会记录警告（不再静默吞掉），便于发现权限 / 网络盘挂载问题。"""
    removed = 0
    failed = 0
    for r, _dirs, files in os.walk(dst):
        for f in files:
            if f.lower() in EXCLUDE_FILE_NAMES:
                p = os.path.join(r, f)
                try:
                    os.chmod(p, 0o666)  # 去掉只读属性（Windows 上只读文件无法删除）
                except OSError:
                    pass
                try:
                    os.remove(p)
                    removed += 1
                except OSError as exc:
                    failed += 1
                    log.warning("无法删除垃圾文件 %s：%s", p, exc)
    if failed:
        log.warning("%s 下有 %d 个垃圾文件删除失败，请检查权限或手动清理。", dst, failed)
    return removed


def sync_to_f(dry_run=False, allow_delete=True):
    """把 轻小说 下的两个分类完整镜像到 F 盘网络云盘（CloudDrive2）。
    覆盖「增删改查」四类操作：
      增 = D 有 F 无 → 复制
      改 = 大小或 mtime 不同 → 覆盖（同时检测 F 侧更新的冲突）
      删 = F 有 D 无 → 直接删除，并清理空目录
      查 = 每轮都生成差异报告并写入审计日志
    返回 True/False（F 盘不可用时返回 False，但不影响 GitHub 推送）。"""
    if not os.path.isdir(F_TARGET_ROOT):
        log.warning("未找到网络云盘 %s（CD2 未挂载？），跳过 F 盘镜像。", F_TARGET_ROOT)
        return False
    tag = "[预演] " if dry_run else ""
    ok = True
    summary = []
    manifest = _load_manifest()
    if not manifest:
        ensure_manifest_seeded()
        manifest = _load_manifest()
    cat_plans = {}
    for cat, src in CATEGORY_DIRS.items():
        dst = F_CATEGORY_DIRS[cat]
        try:
            known = manifest.setdefault(cat, set())
            plan = plan_mirror(src, dst, known=known)
            plan["counts"]["orphan"] = sum(1 for d in plan["delete"]
                                           if not d["ours"] and not d["protected"])
            cat_plans[cat] = plan
            c = plan["counts"]
            for cf in plan["conflicts"]:
                log.warning("%s镜像冲突（%s）：%s —— %s", tag, cat, cf["rel"], cf["detail"])
            res = apply_mirror(plan, dry_run=dry_run, allow_delete=allow_delete,
                               manifest_set=known)
            purged = 0 if dry_run else _purge_excluded_files(dst)
            if dry_run:
                # 同时算出预演模式下的孤儿目录数
                src_dirs = {d for d in scan_dirs(src) if d}
                dst_dirs = {d for d in scan_dirs(dst) if d}
                foreign_dirs = {d for d in (dst_dirs - src_dirs)
                                if not d.split("/", 1)[0].startswith(".trash")}
                c["orphan_dirs"] = len(foreign_dirs)
                plan["counts"]["orphan_dirs"] = len(foreign_dirs)
                log.info("%s%s 计划：增 %d / 改 %d / 删 %d（清单外 %d）/ 孤儿子目录 %d / 保护 %d / 一致 %d / 冲突 %d",
                         tag, dst, c["add"], c["update"], c["delete"],
                         c.get("orphan", 0), c.get("orphan_dirs", 0),
                         c["protected"], c["unchanged"], c["conflict"])
            else:
                log.info("%s已镜像到 F 盘 %s（实际：增 %d / 改 %d / 删 %d / 失败 %d；"
                         "一致 %d，冲突 %d，整棵孤儿子树 %d（跳过 %d），空目录清理 %d，垃圾文件清理 %d）",
                         tag, dst, len(res["added"]), len(res["updated"]), len(res["deleted"]),
                         len(res["failed"]), c["unchanged"], c["conflict"],
                         len(res.get("dirs_deleted", [])),
                         len(res.get("dirs_skipped", [])),
                         res["dirs_removed"], purged)
            if res["failed"]:
                ok = False
            # 维护镜像清单：新写入的记入，已删除的移出
            known.update(res["added"])
            known.update(res["updated"])
            for rel in res["deleted"]:
                known.discard(rel)
            summary.append({"category": cat, "counts": c, "result": {
                "added": len(res["added"]), "updated": len(res["updated"]),
                "deleted": len(res["deleted"]), "failed": len(res["failed"])}})
        except Exception as exc:  # F 盘网络异常不应阻断主流程
            log.warning("镜像到 F 盘 %s 失败：%s", dst, exc)
            ok = False
    if not dry_run:
        _save_manifest(manifest)
        _save_json(MIRROR_STATE_FILE, {"last_sync": _now_iso(), "categories": summary})
        # 同步后再扫一遍生成看板（此时两端应已一致）
        write_mirror_dashboard({
            "generated_at": _now_iso(),
            "source_root": LIGHT_NOVEL_DIR, "target_root": F_TARGET_ROOT,
            "f_mounted": True, "categories": cat_plans,
            "counts": {k: sum(p["counts"].get(k, 0) for p in cat_plans.values())
                       for k in ("add", "update", "delete", "protected", "orphan",
                                 "unchanged", "conflict", "src_total", "dst_total")},
        })
    return ok


def _warn_stray_items():
    """提醒：轻小说 根目录下不应直接放书，应归入 已完结/未完结。"""
    try:
        entries = [e for e in os.listdir(LIGHT_NOVEL_DIR)
                   if e not in IGNORE_NAMES and e not in CATEGORY_DIRS]
        if entries:
            log.warning("注意：轻小说 根目录下存在非分类条目 %s，请将其移入「%s」或「%s」子目录。",
                        entries, CATEGORY_DONE, CATEGORY_ONGOING)
    except OSError:
        pass


def perform_sync(message):
    """一次完整的同步：确保目录 -> 刷新 README -> 镜像 F 盘 -> 提交本地改动
    -> 以本地为准删除 GitHub 多余文件 -> 统一推送。"""
    ensure_category_dirs()
    regenerate_readme()
    try:
        sync_to_f()
    except Exception as exc:
        log.warning("F 盘镜像异常：%s", exc)
    _warn_stray_items()
    if not git_commit(message):  # 先提交本地改动，保证工作区干净（后续 rebase 需要）
        return False
    try:
        remove_remote_extras()  # 以本地为准：删除 GitHub 上多出来的文件
    except Exception as exc:
        log.warning("远程多余文件清理异常：%s", exc)
    ok = push_with_retry()
    if not ok:
        log.warning("推送未完全成功，请检查网络 / 凭据后重试。")
    return ok


