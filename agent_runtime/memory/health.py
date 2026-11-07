"""记忆健康：只读体检 + 保守维护（去重 / 失效归档 / 索引重建）。

默认全部 dry-run；只有显式 apply 才改文件。失效条目先复制到
`archive/memory/` 再移出工作区，正文不丢，可人工复查。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import context_panel, episodes, index, metrics, promote, receipts, store

DEFAULT_RETENTION_DAYS = 90
#: 回执统计窗口：与漏斗同步，面板说「近 30 天」时两处指的都是同一段时间。
RECEIPT_WINDOW_DAYS = 30

#: 学习环的七级漏斗。**这七级数的不是同一种东西**：前四级数「回合」、五六级数「条目」、
#: 第七级数「有过 shared 回注的会话」，而且第七条目的内容来自**全局共享池**，不是本项目晋升的那几条。
#: 所以「每级是上一级的子集」只在单位与作用域都相同时成立 —— 口径必须写进结构里，
#: 由 `funnel()` 逐对判 `comparable`，不能靠读的人记住，也不能让断点跨单位乱走。
#: 加它的理由很具体：此前只能报"真实项目 0 回执 / 46 条候选全来自评测"这种端点数，
#: 说不清断在哪一级，于是"量没起来"既没法归因也没法判断有没有变好。
#: 第 5 个字段是 episode 上对应的判据字段名（`None` = 不来自 episode）。
FUNNEL_STAGES = (
    ("turns", "真实回合", "turn", "project", None),
    ("with_changes", "改了文件", "turn", "project", "files"),
    ("test_run", "跑过测试", "turn", "project", "test_run"),
    ("with_receipt", "拿到回执", "turn", "project", "receipts"),
    ("adopted", "被采纳", "item", "project", None),
    ("promoted", "晋升共享", "item", "project", None),
    # 只统计 shared 共享层真的回到提示词（`memory.inject` 在 agent 的 shared 分支记）。
    # 标签刻意不写"回注提示词"：project/hook 的注入不在这条计量里，写成全部会谎报口径。
    ("injected", "共享层回注", "session", "project|global", None),
)

_UNIT_CN = {"turn": "回合", "item": "条目", "session": "会话"}
_SCOPE_CN = {"project": "本项目", "project|global": "本项目回合 × 全局共享池"}


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
    """学习环七级漏斗的当前计数 + 每级的口径 + 只在可比级之间判的断点。

    三条实测出来的口径约束（2026-11-01）：

    1. **单位不同不许比大小** —— 前四级是回合、五六是条目、七是会话。真实项目当时的形状是
       「回执 3（回合）→ 采纳 0（条目）→ 晋升 0（条目）→ 回注 63（事件，全部落在一天里）」，
       旧版把它读成「断在 晋升共享 → 共享层回注」，其实是跨单位乱走。
    2. **字段在但值为空 ≠ 缺字段** —— 21 条 episode 全都带 `files` / `test_run` 键，值为空是
       真没改文件（只跑验证也能出回执）。旧版注释把倒挂归因成「早期回合缺字段」，对本项目是
       错的断言，所以这里改成实测：按行统计「该级是否可测」（`measurable`），缺多少报多少。
    3. **窗口必须是同一个窗口** —— `metrics.report()` 的 `days` 是「最后 N 个日文件」，不是 N 天；
       和按 `ts` 过滤的 episode 放一起比就会错位。这里改用 `metrics.rows()` 的真实时间窗。
    """
    window = max(1, int(days))
    counts = {key: 0 for key, *_ in FUNNEL_STAGES}
    measurable = {key: 0 for key, *_ in FUNNEL_STAGES}

    for row in episodes.iter_project(project_id):
        age = _age_days(row.get("ts"))
        if age is not None and age > window:
            continue
        counts["turns"] += 1
        for key, _label, _unit, _scope, field in FUNNEL_STAGES:
            if not field:
                continue
            if field in row:                      # 键在 = 这一级对该回合可判
                measurable[key] += 1
                if row.get(field):
                    counts[key] += 1
    measurable["turns"] = counts["turns"]
    missing_field = {key: counts["turns"] - measurable[key]
                     for key, _label, unit, _scope, field in FUNNEL_STAGES
                     if unit == "turn" and field}
    # 一级彻底不可判（窗口内有回合，但没有一条带这个字段）就不能拿来当断点 ——
    # 否则「缺字段的旧数据」会被读成「断在这里」，那是量具的毛病不是流量的毛病。
    unmeasurable = [key for key, _label, unit, _scope, field in FUNNEL_STAGES
                    if unit == "turn" and field and counts["turns"] and not measurable[key]]

    # 条目级：窗口按 `updated` 卡（采纳/晋升本身没时间戳，updated 是唯一可用的代理）。
    items = promote.candidates(project_id)
    undated = 0
    windowed = []
    for item in items:
        age = _age_days(item.get("updated"))
        if age is None:
            undated += 1
            windowed.append(item)                 # 读不出时间就留在窗口里，别悄悄丢数据
        elif age <= window:
            windowed.append(item)
    counts["adopted"] = sum(1 for item in windowed if item.get("adopted"))
    counts["promoted"] = sum(1 for item in windowed if str(item.get("status") or "") == "promoted")
    stock = {
        "adopted": sum(1 for item in items if item.get("adopted")),
        "promoted": sum(1 for item in items if str(item.get("status") or "") == "promoted"),
    }

    # 会话级：同一张时间窗里，有多少个不同会话真的拿到过 shared 注入（事件数另报，不混用）。
    inject_rows = metrics.rows(days=window, project=project_id, event="memory.inject")
    sessions = {str(row.get("session") or "") for row in inject_rows} - {""}
    counts["injected"] = len(sessions)
    inject_events = len(inject_rows)
    inject_unattributed = sum(1 for row in inject_rows if not str(row.get("session") or ""))

    labels = {key: label for key, label, *_rest in FUNNEL_STAGES}
    meta = {key: (unit, scope) for key, _label, unit, scope, _f in FUNNEL_STAGES}
    comparable_pairs = []
    handoffs = []
    inversions = []
    for (key, _l, unit, scope, _f), (nkey, _nl, nunit, nscope, _nf) in zip(
            FUNNEL_STAGES, FUNNEL_STAGES[1:]):
        pair = {"from": key, "to": nkey,
                "from_label": labels[key], "to_label": labels[nkey]}
        if unit == nunit and scope == nscope:
            pair["comparable"] = True
            comparable_pairs.append(pair)
            if counts[nkey] > counts[key]:
                # 倒挂的成因看**两级任一侧**有缺字段：上一级不可判时，分母本身就虚。
                kind = "missing_field" if (missing_field.get(key) or missing_field.get(nkey)) else "legitimate"
                inversions.append({
                    "from": key, "to": nkey, "kind": kind,
                    "message": "「%s」%d < 「%s」%d（%s）" % (
                        labels[key], counts[key], labels[nkey], counts[nkey],
                        "有 %d 个回合缺字段，这一级判不准" % (
                            max(missing_field.get(nkey, 0), missing_field.get(key, 0)))
                        if kind == "missing_field" else
                        "字段齐备，是真发生了：同单位下一级也不必须是上一级的子集"),
                })
        else:
            pair["comparable"] = False
            pair["reason"] = "单位 %s→%s%s" % (
                _UNIT_CN[unit], _UNIT_CN[nunit],
                "" if scope == nscope else "，且作用域 %s→%s" % (_SCOPE_CN[scope], _SCOPE_CN[nscope]))
            handoffs.append(pair)
            if counts[nkey] > counts[key]:
                inversions.append({
                    "from": key, "to": nkey, "kind": "cross_unit",
                    "message": "「%s」%d（%s·%s）< 「%s」%d（%s·%s）—— 两种量纲，不是倒挂" % (
                        labels[key], counts[key], _UNIT_CN[unit], _SCOPE_CN[scope],
                        labels[nkey], counts[nkey], _UNIT_CN[nunit], _SCOPE_CN[nscope]),
                })

    stalled = ""
    skipped = []
    if not counts["turns"]:
        stalled = "还没有真实回合"
    else:
        for pair in comparable_pairs:                       # 断点只在同单位同作用域之间走
            if pair["to"] in unmeasurable:
                skipped.append({"at": "%s → %s" % (pair["from_label"], pair["to_label"]),
                                "reason": "这一级没有一条记录带判据字段，判不了"})
                continue
            if counts[pair["from"]] and not counts[pair["to"]]:
                stalled = "断在「%s → %s」（单位：%s）" % (
                    pair["from_label"], pair["to_label"], _UNIT_CN[meta[pair["to"]][0]])
                break
        if not stalled:
            # 同单位段内都通了，卡在跨单位交接处 —— 这仍然要说出来（学习环没闭合就是卡这儿），
            # 但措辞必须是「交接」而不是「子集断了」：上一级数回合、下一级数条目，没有包含关系。
            # 不因后面某级有数就免职：真实项目就是「采纳 0、晋升 0，但回注 63」—— 回注的内容
            # 来自**全局共享池**，不代表本项目这一环闭合了。
            for pair in handoffs:
                if counts[pair["from"]] and not counts[pair["to"]]:
                    stalled = "交接处停在「%s（%s）→ %s（%s）」，后面那级若有数，来源不是本级" % (
                        pair["from_label"], _UNIT_CN[meta[pair["from"]][0]],
                        pair["to_label"], _UNIT_CN[meta[pair["to"]][0]])
                    break
    cross = sum(1 for item in inversions if item["kind"] == "cross_unit")
    gaps = sum(1 for item in inversions if item["kind"] == "missing_field")
    legit = sum(1 for item in inversions if item["kind"] == "legitimate")
    caveats = []
    if inversions:
        parts = []
        if gaps:
            parts.append("%d 处判据字段缺失、这一级判不准" % gaps)
        if cross:
            parts.append("%d 处是跨单位比较（量纲不同，不是倒挂）" % cross)
        if legit:
            parts.append("%d 处属正常形状（后级多于前级，例如跑验证不必改文件）" % legit)
        caveats.append("后级多于前级：" + "；".join(parts))

    return {
        "days": window,
        "counts": counts,
        "stalled_at": stalled,
        "caveats": caveats,
        "inversions": inversions,
        "comparability": comparable_pairs + handoffs,
        "handoffs": handoffs,
        "missing_field": missing_field,
        "unmeasurable": unmeasurable,
        "skipped_breakpoints": skipped,
        "stages": [{"key": key, "label": label, "count": counts[key],
                    "unit": unit, "unit_label": _UNIT_CN[unit],
                    "scope": scope, "scope_label": _SCOPE_CN[scope],
                    "measurable": measurable[key] if unit == "turn" else None,
                    "events": inject_events if key == "injected" else None,
                    "stock": stock.get(key) if unit == "item" else None}
                   for key, label, unit, scope, _f in FUNNEL_STAGES],
        "item_window_by": "updated（采纳/晋升本身无时间戳，用更新时间作代理；%d 条读不出时间仍计入）" % undated,
        "inject_events": inject_events,
        "inject_unattributed": inject_unattributed,
    }


def _receipt_stats(project_id, days=RECEIPT_WINDOW_DAYS):
    """近 N 天的回执分布。

    采纳/晋升的上游是**通过**的回执：没有 pass 回执，候选就没有证据，采纳这一步
    只能靠猜。面板要能区分「链路断了」和「这项目压根没产出过验证证据」。
    """
    stats = {"days": int(days), "total": 0, "pass": 0, "fail": 0, "uncertain": 0}
    try:
        rows = receipts.list_receipts(project_id, limit=500)
    except Exception:  # noqa: BLE001 —— 回执目录读不到不影响其余体检
        return stats
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row.get("ts") or ""))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            continue
        stats["total"] += 1
        result = str(row.get("result") or "uncertain")
        stats[result] = stats.get(result, 0) + 1
    return stats


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
    receipt_stats = _receipt_stats(project_id)
    context = _context_usage(project_id)
    warnings = []
    if not shared_active and project_candidates:
        warnings.append({"code": "shared_empty",
                         "message": f"共享层为空：{len(project_candidates)} 条项目候选尚未采纳/晋升"})
    if not by_status.get("promoted"):
        warnings.append({"code": "no_promotion", "message": "还没有候选通过晋升门"})
    # 没有 pass 回执时，「0 采纳」的瓶颈在证据这一层，不在晋升门。分开报，
    # 否则面板只会让人去点一个本来就无从判断的「采纳」。
    if not receipt_stats["total"]:
        warnings.append({"code": "no_receipt",
                         "message": "近 %d 天没有验证回执：候选拿不到证据，采纳这一步无从判断"
                                    % receipt_stats["days"]})
    elif not receipt_stats["pass"]:
        warnings.append({"code": "no_pass_receipt",
                         "message": "近 %d 天 %d 条回执全部未通过：候选没有可采纳的证据"
                                    % (receipt_stats["days"], receipt_stats["total"])})
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
        "receipts": receipt_stats,
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
