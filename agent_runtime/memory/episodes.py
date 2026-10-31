"""L4 结构化回合：只存目标/动作/结果/失败/下一步，不存逐字长文。

逐字原文仍在会话文件里；这里只留可检索、可注入的摘要，避免重演
"每轮问答都变成一条长期记忆"的旧问题。
"""
from __future__ import annotations

import json
import os
import threading
import uuid

from . import store

MAX_TAIL_BYTES = 256 * 1024
MAX_ACTIONS = 40
MAX_FILES = 20
COMPACT_BYTES = 512 * 1024
KEEP_LINES = 200
_LOCK = threading.RLock()


def new_id():
    return "ep-" + uuid.uuid4().hex[:12]


def path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id), "session",
                           store.safe_id(session_id), "episodes.jsonl")


def _clip(value, limit):
    return str(value or "")[:limit]


def append(project_id, session_id, record):
    """追加一条结构化回合；返回回合 id。"""
    item = dict(record or {})
    item.setdefault("id", new_id())
    item["ts"] = store.now()
    item["goal"] = _clip(item.get("goal"), 300)
    item["next"] = _clip(item.get("next"), 300)
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    item["result"] = {"status": _clip(result.get("status"), 40),
                      "summary": _clip(result.get("summary"), 600),
                      "error": _clip(result.get("error"), 300)}
    item["actions"] = [a for a in (item.get("actions") or []) if isinstance(a, dict)][:MAX_ACTIONS]
    item["files"] = [str(f) for f in (item.get("files") or []) if f][:MAX_FILES]
    item["errors"] = [e for e in (item.get("errors") or []) if isinstance(e, dict)][:5]
    item["receipts"] = [str(r) for r in (item.get("receipts") or []) if r][:20]
    target = path(project_id, session_id)
    store.ensure_dir(os.path.dirname(target))
    line = json.dumps(item, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        try:
            if os.path.getsize(target) > COMPACT_BYTES:
                compact(project_id, session_id, keep=KEEP_LINES)
        except OSError:
            pass
    return item["id"]


def compact(project_id, session_id, keep=KEEP_LINES):
    """回合流超过阈值时把旧行移到 archive，保留最近 keep 条；返回归档行数。"""
    target = path(project_id, session_id)
    try:
        lines = [line for line in store.read_text(target).splitlines() if line.strip()]
    except OSError:
        return 0
    keep = max(5, int(keep))
    if len(lines) <= keep:
        return 0
    old, recent = lines[:-keep], lines[-keep:]
    stamp = store.now().replace(":", "").replace("-", "")[:15]
    archive = store.safe_path("projects", store.safe_id(project_id), "archive",
                              "episodes", "%s-%s.jsonl"
                              % (store.safe_id(session_id), stamp))
    store.ensure_dir(os.path.dirname(archive))
    with open(archive, "a", encoding="utf-8", newline="\n") as handle:
        for line in old:
            handle.write(line + "\n")
    store.atomic_write(target, "\n".join(recent) + "\n")
    return len(old)


def latest(project_id, session_id, limit=3):
    """读最近 N 条（新→旧）。只读文件尾部，长会话不会全量加载。"""
    target = path(project_id, session_id)
    try:
        with open(target, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TAIL_BYTES))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
        if len(rows) >= max(1, int(limit)):
            break
    return rows
