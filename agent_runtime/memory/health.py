"""记忆健康：只读体检 + 保守维护（去重 / 失效归档 / 索引重建）。

默认全部 dry-run；只有显式 apply 才改文件。失效条目先复制到
`archive/memory/` 再移出工作区，正文不丢，可人工复查。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import context_panel, index, metrics, promote, store

DEFAULT_RETENTION_DAYS = 90


def _age_days(value):
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)


def _inactive(project_id):
    rows = []
    for item in promote.candidates(project_id):
        if item.get("status") in ("superseded", "merged"):
            rows.append(("project", item))
    for item in promote.shared_items(include_inactive=True):
        if item.get("status") in ("superseded", "merged"):
            rows.append(("global", item))
    return rows


def _context_usage(project_id):
    try:
        view = context_panel.next_view(project_id, "")
    except Exception:  # noqa: BLE001
        return {"total_chars": 0, "layers": []}
    return {
        "total_chars": int(view.get("total_chars") or 0),
        "layers": [{"key": layer.get("key"), "title": layer.get("title"),
                    "chars": int(layer.get("chars") or 0),
                    "budget": layer.get("budget")}
                   for layer in view.get("layers") or []],
    }


def report(project_id):
    """返回可直接给面板/CLI 的健康快照。"""
    project_id = str(project_id or "default")
    all_shared = promote.shared_items(include_inactive=True)
    shared_active = [item for item in all_shared if item.get("status") == "active"]
    shared_superseded = [item for item in all_shared if item.get("status") == "superseded"]
    project_items = promote.candidates(project_id)
    by_status = {}
    for item in project_items:
        key = str(item.get("status") or "unknown")
        by_status[key] = by_status.get(key, 0) + 1
    project_candidates = [item for item in project_items if item.get("status") == "candidate"]
    adopted = sum(1 for item in project_candidates if item.get("adopted"))
    frozen = sum(1 for item in project_candidates if item.get("promotion_frozen"))
    project_dupes = promote.dedupe_candidates(project_id, apply=False)
    shared_dupes = promote.dedupe_shared(apply=False)
    index_stats = index.stats()
    inactive = _inactive(project_id)
    stale = [scope_item for scope_item in inactive
             if (_age_days(scope_item[1].get("updated")) or 0) >= DEFAULT_RETENTION_DAYS]
    consolidation = metrics.report(days=7)
    context = _context_usage(project_id)
    warnings = []
    if not shared_active and project_candidates:
        warnings.append({"code": "shared_empty",
                         "message": f"共享层为空：{len(project_candidates)} 条项目候选尚未采纳/晋升"})
    if not by_status.get("promoted"):
        warnings.append({"code": "no_promotion", "message": "还没有候选通过晋升门"})
    if project_dupes.get("duplicates", project_dupes.get("merged", 0)):
        count = project_dupes.get("duplicates", project_dupes.get("merged", 0))
        warnings.append({"code": "candidate_duplicates",
                         "message": f"项目候选有 {count} 条近似重复，可运行维护去重"})
    if shared_dupes.get("duplicates"):
        warnings.append({"code": "shared_duplicates",
                         "message": f"共享层有 {shared_dupes['duplicates']} 条近似重复"})
    if stale:
        warnings.append({"code": "stale_superseded",
                         "message": f"有 {len(stale)} 条失效条目超过 {DEFAULT_RETENTION_DAYS} 天，可归档"})
    missing_vectors = max(0, int(index_stats.get("items") or 0) - int(index_stats.get("vectors") or 0))
    try:
        from config import get_memory_semantic
        semantic_on = bool(get_memory_semantic())
    except Exception:  # noqa: BLE001
        semantic_on = False
    if semantic_on and missing_vectors:
        warnings.append({"code": "missing_vectors",
                         "message": f"索引有 {missing_vectors} 条缺向量，语义召回会退化"})
    if consolidation.get("errors"):
        warnings.append({"code": "consolidation_errors",
                         "message": f"最近固化有 {consolidation['errors']} 次错误"})
    scheduler = {"interval_minutes": 0, "last": {}}
    try:
        from config import get_memory_check_interval
        scheduler["interval_minutes"] = int(get_memory_check_interval() or 0)
        from agent_runtime import maintenance
        scheduler["last"] = maintenance.last_status()
    except Exception:  # noqa: BLE001 —— 定时器状态不可读不影响体检
        pass
    return {
        "ok": True,
        "project_id": project_id,
        "generated_at": store.now(),
        "scheduler": scheduler,
        "shared": {
            "active": len(shared_active),
            "pinned": sum(1 for item in shared_active if item.get("pinned")),
            "superseded": len(shared_superseded),
        },
        "project": {
            "total": len(project_items),
            "candidate": len(project_candidates),
            "adopted": adopted,
            "frozen": frozen,
            "promoted": by_status.get("promoted", 0),
            "superseded": by_status.get("superseded", 0),
            "merged": by_status.get("merged", 0),
            "by_status": by_status,
        },
        "duplicates": {
            "project": int(project_dupes.get("merged") or 0),
            "shared": int(shared_dupes.get("duplicates") or 0),
            "planned": (project_dupes.get("planned") or []) +
                       (shared_dupes.get("planned") or []),
        },
        "stale": {
            "retention_days": DEFAULT_RETENTION_DAYS,
            "superseded": len(stale),
        },
        "index": index_stats,
        "context": context,
        "metrics": consolidation,
        "warnings": warnings,
    }


def gc(project_id, retention_days=DEFAULT_RETENTION_DAYS, apply=False):
    """归档超过保留期的失效/已合并条目；正文复制到 archive/memory 后移出工作区。"""
    retention_days = max(0, int(retention_days))
    planned = []
    for scope, item in _inactive(project_id):
        age = _age_days(item.get("updated"))
        if age is None or age < retention_days:
            continue
        planned.append({"scope": scope, "id": item.get("id"), "kind": item.get("kind"),
                        "updated": item.get("updated"), "age_days": round(age, 1)})
        if not apply:
            continue
        target = store.safe_path("archive", "memory", scope,
                                 "facts" if item.get("kind") == "fact" else "lessons",
                                 os.path.basename(item["path"]))
        store.ensure_dir(os.path.dirname(target))
        store.atomic_write(target, store.read_text(item["path"]))
        os.remove(item["path"])
    if apply and planned:
        store.ledger("memory.gc", project=store.safe_id(project_id),
                     archived=len(planned), retention_days=retention_days)
    return {"retention_days": retention_days, "archived": len(planned),
            "apply": bool(apply), "items": planned[:30]}


def maintain(project_id, *, apply=False, retention_days=DEFAULT_RETENTION_DAYS,
             threshold=0.88):
    """一次保守维护：候选去重 + 共享去重 + 失效归档；apply 后重建索引。"""
    project_id = str(project_id or "default")
    candidates = promote.dedupe_candidates(project_id, threshold=threshold, apply=apply)
    shared = promote.dedupe_shared(threshold=max(0.9, float(threshold)), apply=apply)
    garbage = gc(project_id, retention_days=retention_days, apply=apply)
    rebuilt = None
    if apply:
        try:
            rebuilt = index.rebuild(verify=True)
        except Exception as exc:  # noqa: BLE001
            rebuilt = {"error": f"{type(exc).__name__}: {exc}"}
        store.ledger("memory.maintain", project=store.safe_id(project_id),
                     apply=True, retention_days=retention_days,
                     dedupe_candidates=candidates.get("merged"),
                     dedupe_shared=shared.get("duplicates"),
                     gc=garbage.get("archived"))
    return {
        "ok": True,
        "project": project_id,
        "apply": bool(apply),
        "candidates": candidates,
        "shared": shared,
        "gc": garbage,
        "index": rebuilt,
    }
