"""固化与注入的计量：只落盘，不设硬预算。

先在真实长任务上量出"每小时任务成本"和 p50/p95，再决定要不要设阈值；
结构性控制（单 worker、跳过平凡轮、失败跳过）放在 consolidate.py。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import store


def record(event, **fields):
    try:
        name = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = store.safe_path("metrics", name + ".jsonl")
        row = {"ts": store.now(), "event": str(event)}
        row.update({key: value for key, value in fields.items() if value is not None})
        store.ensure_dir(os.path.dirname(target))
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:  # noqa: BLE001 —— 计量失败绝不影响主流程
        return False


def rows(days=7, project=None, event=None):
    """按**真实时间窗**取计量行（`report()` 的 `days` 是「最后 N 个日文件」，不是 N 天）。

    为什么要单独一个：`memory.inject` 这类是按回合/按事件累加的，用文件数当窗口时，
    只要中间有几天没写过计量，"最近 30 天"就会悄悄变成"最近 30 个有计量的日子"，
    和同一张面板上按 `ts` 过滤的 episode _stage 不是同一个窗口 —— 两级放在一起比大小
    就成了量具错觉。这里逐行按 `ts` 卡窗口，只做过滤不做汇总。
    """
    import glob
    limit = max(1, int(days))
    files = sorted(glob.glob(os.path.join(store.root(), "metrics", "*.jsonl")))[-max(2, limit + 7):]
    cutoff = _now_ts() - limit * 86400
    out = []
    for target in files:
        try:
            with open(target, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    if _ts_of(value.get("ts")) < cutoff:
                        continue
                    if project is not None and str(value.get("project") or "") != str(project):
                        continue
                    if event is not None and str(value.get("event") or "") != str(event):
                        continue
                    out.append(value)
        except OSError:
            continue
    return out


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def _ts_of(value):
    """把 `store.now()` 那种带时区的 ISO 串换成 epoch 秒；认不出来就当「很旧」（宁漏不误计）。"""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("-inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def report(days=7, project=None):
    """按天汇总固化开销；P0 只度量，不据此自动设阈值。

    `project` 给定时只统计该项目的行。计量文件本身是全局的（成本要能跨项目合计），
    但**健康面板是按项目看的**：不按项目过滤的话，评测流量撞一次 402 就会在真实
    项目的体检里挂一条「最近固化有 N 次错误」—— 隔离写入之后这正是残留的污染面。
    缺省 None 保持全局口径，成本视图不受影响。

    注意：这里的 `days` 是「最后 N 个**日文件**」，不是 N 天 —— 成本视图这么算没问题，
    但任何要和 episode 漏斗放在一起比大小的数，必须改用 `rows()` 的真实时间窗。
    """
    import glob
    files = sorted(glob.glob(os.path.join(store.root(), "metrics", "*.jsonl")))[-max(1, int(days)):]
    rows = []
    for target in files:
        try:
            with open(target, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError:
            continue
    if project is not None:
        rows = [row for row in rows
                if str(row.get("project") or "") == str(project)]
    calls = [row for row in rows if row.get("event") == "consolidate"]
    by_event: dict[str, int] = {}
    for row in rows:
        name = str(row.get("event") or "unknown")
        by_event[name] = by_event.get(name, 0) + 1
    latencies = sorted(int(row.get("latency_ms") or 0) for row in calls)
    def pct(percent):
        if not latencies:
            return 0
        index = min(len(latencies) - 1, max(0, int(round(percent / 100 * len(latencies))) - 1))
        return latencies[index]
    return {
        "days": len(files),
        "events": len(rows),
        "by_event": by_event,
        "consolidate_calls": len(calls),
        "skipped": sum(1 for row in rows if row.get("event") == "consolidate.skip"),
        "errors": sum(1 for row in rows if row.get("event") == "consolidate.error"),
        "tokens_in": sum(int(row.get("tokens_in") or 0) for row in calls),
        "tokens_out": sum(int(row.get("tokens_out") or 0) for row in calls),
        "promoted": sum(int(row.get("promoted") or 0) for row in calls),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "models": sorted({str(row.get("model") or "") for row in calls if row.get("model")}),
    }
