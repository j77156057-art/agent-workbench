"""记忆健康：只读体检 + 保守维护（去重 / 失效归档 / 索引重建）。

默认全部 dry-run；只有显式 apply 才改文件。失效条目先复制到
`archive/memory/` 再移出工作区，正文不丢，可人工复查。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import context_panel, episodes, index, metrics, promote, store

DEFAULT_RETENTION_DAYS = 90

#: 学习环的七级漏斗。**对新回合**每级是上一级的子集，第一个归零处即断点；
#: 历史回合可能缺后加的字段，所以 `funnel()` 另报 `inversions`，不假装单调。
#: 加它的理由很具体：此前只能报"真实项目 0 回执 / 46 条候选全来自评测"这种端点数，
#: 说不清断在哪一级，于是"量没起来"既没法归因也没法判断有没有变好。
FUNNEL_STAGES = (
    ("turns", "真实回合"),
    ("with_changes", "改了文件"),
    ("test_run", "跑过测试"),
    ("with_receipt", "拿到回执"),
    ("adopted", "被采纳"),
    ("promoted", "晋升共享"),
    # 只统计 L2 共享层真的回到提示词（`memory.inject` 在 agent 的 L2 分支记）。
    # 标签刻意不写"回注提示词"：L3/L4 的注入不在这条计量里，写成全部会谎报口径。
    ("injected", "共享层回注"),
)


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


def funnel(project_id, days=30):
    """学习环七级漏斗的当前计数 + 第一个断点。

    口径说明：episode 按项目分桶，评测流量已经隔离到 `eval-golden`，所以这里的
    `turns` 就是**这个项目的真实回合**，不需要再减什么。`injected` 来自
    `memory.inject` 计量（L2 共享层真的进了提示词才记），不是"检索命中过"。
    """
    counts = {key: 0 for key, _ in FUNNEL_STAGES}
    for row in episodes.iter_project(project_id):
        age = _age_days(row.get("ts"))
        if age is not None and age > days:
            continue
        counts["turns"] += 1
        if row.get("files"):
            counts["with_changes"] += 1
        if row.get("test_run"):
            counts["test_run"] += 1
        if row.get("receipts"):
            counts["with_receipt"] += 1
    items = promote.candidates(project_id)
    counts["adopted"] = sum(1 for item in items if item.get("adopted"))
    counts["promoted"] = sum(1 for item in items if str(item.get("status") or "") == "promoted")
    counts["injected"] = int(metrics.report(days=days, project=project_id)
                             .get("by_event", {}).get("memory.inject", 0))
    # 倒挂检测：后级比前级多。对**新**回合每级确实是上一级的子集，但历史回合可能压根
    # 没有后加的字段（`test_run` 就是 2026-11-01 才落的），实测探针项目就是
    # 「真实回合 10 → 改了文件 0 → 跑过测试 0 → 拿到回执 4」。不报出来的话，
    # `stalled_at` 会把这种缺字段的旧数据说成「断在 真实回合→改了文件」，纯属误导。
    inversions = []
    for (key, label), (nkey, nlabel) in zip(FUNNEL_STAGES, FUNNEL_STAGES[1:]):
        if counts[nkey] > counts[key]:
            inversions.append({"from": key, "to": nkey,
                               "message": f"「{label}」{counts[key]} < 「{nlabel}」{counts[nkey]}"})
    stalled = ""
    if not counts["turns"]:
        stalled = "还没有真实回合"
    else:
        for (key, label), (nkey, nlabel) in zip(FUNNEL_STAGES, FUNNEL_STAGES[1:]):
            if counts[key] and not counts[nkey]:
                stalled = f"断在「{label} → {nlabel}」"
                break
        if inversions and stalled:
            stalled += "（注意：有 " + str(len(inversions)) + " 处倒挂，早期回合缺字段，别按断点解读）"
    return {
        "days": int(days),
        "counts": counts,
        "stalled_at": stalled,
        "inversions": inversions,
        "stages": [{"key": key, "label": label, "count": counts[key]}
                   for key, label in FUNNEL_STAGES],
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
    consolidation = metrics.report(days=7, project=project_id)
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
        "funnel": funnel(project_id),
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
