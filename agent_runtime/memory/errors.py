"""L4 错误账本：错误签名 → 原因 → 次数；同一错误只留一条，防止重复踩坑。

签名按"工具 + 归一化后的错误文本"哈希，避免同一异常因空白/换行反复新增。
账本只是提醒，不阻断任何操作；验证通过后由调用方 mark_verified。
"""
from __future__ import annotations

import re
import threading

from . import store

MAX_ITEMS = 200
_LOCK = threading.RLock()


def path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id), "session",
                           store.safe_id(session_id), "errors.md")


def project_path(project_id):
    return store.safe_path("projects", store.safe_id(project_id), "errors.md")


def signature(tool, cause):
    normalized = re.sub(r"\s+", " ", str(cause or "")).strip().lower()[:200]
    return "err-" + store.sha256_text(str(tool or "") + "|" + normalized)[:12]


def _top(rows, limit):
    ordered = sorted(rows, key=lambda row: (int(row.get("occurrences") or 0),
                                            str(row.get("last_seen") or "")), reverse=True)
    return ordered[:max(1, int(limit))]


def _upsert(target, sig, *, tool="", cause="", label=""):
    rows = store.load_items(target)
    now = store.now()
    for row in rows:
        if row.get("sig") == sig:
            row["occurrences"] = int(row.get("occurrences") or 0) + 1
            row["last_seen"] = now
            if cause:
                row["cause"] = str(cause)[:300]
            if tool:
                row["tool"] = str(tool)[:60]
            if label:
                row["label"] = str(label)[:80]
            store.save_items(target, rows)
            return row
    row = {"sig": str(sig), "tool": str(tool or "")[:60],
           "label": str(label or tool or sig)[:80], "cause": str(cause or "")[:300],
           "fix": "", "verified": False, "occurrences": 1,
           "first_seen": now, "last_seen": now}
    rows.append(row)
    if len(rows) > MAX_ITEMS:
        rows = sorted(rows, key=lambda item: (int(item.get("occurrences") or 0),
                                              str(item.get("last_seen") or "")),
                      reverse=True)[:MAX_ITEMS]
    store.save_items(target, rows)
    return row


def record(project_id, session_id, sig, *, tool="", cause="", label=""):
    """同时写会话账本与项目汇总：同类错误换窗口仍可见。"""
    with _LOCK:
        row = _upsert(path(project_id, session_id), sig, tool=tool, cause=cause, label=label)
        _upsert(project_path(project_id), sig, tool=tool, cause=cause, label=label)
        return row


def mark_verified(project_id, session_id, sig, fix=""):
    target = path(project_id, session_id)
    with _LOCK:
        rows = store.load_items(target)
        for row in rows:
            if row.get("sig") == sig:
                row["verified"] = True
                row["fix"] = str(fix or row.get("fix") or "")[:300]
                row["last_seen"] = store.now()
                store.save_items(target, rows)
                return row
    return None


def top(project_id, session_id, limit=3):
    return _top(store.load_items(path(project_id, session_id)), limit)


def render(project_id, session_id, *, limit=2, max_chars=200):
    lines = []
    used = 0
    for row in top(project_id, session_id, limit):
        label = str(row.get("label") or row.get("tool") or row.get("sig"))
        cause = str(row.get("cause") or "").strip()
        count = int(row.get("occurrences") or 1)
        line = f"- 易错 {label}: {cause}（×{count}）" if cause else f"- 易错 {label}（×{count}）"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def top_project(project_id, limit=3):
    return _top(store.load_items(project_path(project_id)), limit)


def render_project(project_id, *, limit=2, max_chars=200, exclude=()):
    skip = {str(sig) for sig in (exclude or ())}
    lines = []
    used = 0
    for row in top_project(project_id, limit):
        if str(row.get("sig") or "") in skip:
            continue
        label = str(row.get("label") or row.get("tool") or row.get("sig"))
        cause = str(row.get("cause") or "").strip()
        count = int(row.get("occurrences") or 1)
        line = f"- 跨会话易错 {label}: {cause}（×{count}）" if cause             else f"- 跨会话易错 {label}（×{count}）"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
