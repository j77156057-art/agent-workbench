"""Plan 模式决策账本：每个会话一份 JSONL，供用户事后逐条审查。

PlanRunner 自动记录每轮目标/动作/结论；模型可用 `plan_decision` 主动记录
关键取舍。这里只追加，不在运行中删除；超过大小阈值时保留最近一段。
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
from datetime import datetime, timezone

from .memory import store

MAX_BYTES = 1024 * 1024
KEEP_LINES = 2000
_LOCK = threading.RLock()
_CTX_PROJECT = contextvars.ContextVar("docmind_plan_project", default="")
_CTX_SESSION = contextvars.ContextVar("docmind_plan_session", default="")


def bind(project_id="", session_id=""):
    return (_CTX_PROJECT.set(str(project_id or "")),
            _CTX_SESSION.set(str(session_id or "")))


def reset(tokens):
    for token, var in zip(tokens, (_CTX_PROJECT, _CTX_SESSION)):
        try:
            var.reset(token)
        except (LookupError, ValueError):
            pass


def scope():
    return _CTX_PROJECT.get(), _CTX_SESSION.get()


def path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id or "default"),
                           "session", store.safe_id(session_id or "global"),
                           "plan-log.jsonl")


def append(project_id, session_id, kind, text, **fields):
    if not str(session_id or "").strip():
        return None
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": str(kind or "note")[:40],
        "text": str(text or "")[:2000],
        "project": str(project_id or ""),
        "session": str(session_id or ""),
    }
    row.update({key: value for key, value in fields.items() if value is not None})
    target = path(project_id, session_id)
    with _LOCK:
        store.ensure_dir(os.path.dirname(target))
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            if os.path.getsize(target) > MAX_BYTES:
                lines = [line for line in store.read_text(target).splitlines() if line.strip()]
                store.atomic_write(target, "\n".join(lines[-KEEP_LINES:]) + "\n")
        except OSError:
            pass
    return row


def rows(project_id, session_id, limit=200):
    target = path(project_id, session_id)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
    except OSError:
        return []
    out = []
    for line in reversed(lines[-max(1, int(limit)):]):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def decision(text, rationale="", task="", project_id="", session_id=""):
    project, session = (project_id, session_id) if session_id else scope()
    row = append(project, session, "decision", text,
                 rationale=str(rationale or "")[:1200], task=str(task or "")[:120])
    return row
