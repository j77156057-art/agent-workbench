"""会话定时器：模型可设置的后台提醒，到期后在模型下一次准备发言前注入上下文。

设计约束：
- 一个会话一个 JSON 文件，单进程锁；定时器只做提醒，不直接执行工具。
- 重复定时器在每次到期后自动推后一个 interval；模型完成任务后必须显式取消，
  系统不会因为一次“看起来完成”的回合自动关闭。
- 模型只能在有活动会话时设置；没有会话的工具调用直接拒绝。
- 上限只数**活动**定时器：终态行（fired/cancelled）留最近几条供回看，其余在
  新建时清掉，否则一个长会话挂满一次性提醒后就再也设不动了。
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .memory import store

MAX_ACTIVE = 24
TERMINAL_KEEP = 12
MAX_FILE_ROWS = MAX_ACTIVE + TERMINAL_KEEP
MAX_MESSAGE = 500
MAX_DELAY_SECONDS = 30 * 24 * 3600
_LOCK = threading.RLock()
_SCOPE_PROJECT = contextvars.ContextVar("docmind_timer_project", default="")
_SCOPE_SESSION = contextvars.ContextVar("docmind_timer_session", default="")


def bind(project_id="", session_id=""):
    return (_SCOPE_PROJECT.set(str(project_id or "")),
            _SCOPE_SESSION.set(str(session_id or "")))


def reset(tokens):
    for token, var in zip(tokens, (_SCOPE_PROJECT, _SCOPE_SESSION)):
        try:
            var.reset(token)
        except (LookupError, ValueError):
            pass


def scope():
    return _SCOPE_PROJECT.get(), _SCOPE_SESSION.get()


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id or "default"),
                           "session", store.safe_id(session_id or "global"),
                           "timers.json")


def _load(project_id, session_id):
    try:
        with open(path(project_id, session_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    rows = [item for item in (data.get("timers") or []) if isinstance(item, dict)]
    return rows[:MAX_FILE_ROWS]


def _save(project_id, session_id, rows):
    target = path(project_id, session_id)
    store.ensure_dir(os.path.dirname(target))
    store.atomic_write(target, json.dumps(
        {"version": 1, "timers": rows[:MAX_FILE_ROWS]}, ensure_ascii=False, indent=2) + "\n")


def _prune(rows):
    """活动行全留，终态行只留最近 `TERMINAL_KEEP` 条（保持原有先后）。"""
    active = [row for row in rows if row.get("status") == "active"]
    terminal = [row for row in rows if row.get("status") != "active"]
    return active + terminal[-TERMINAL_KEEP:]


def list_timers(project_id, session_id, include_inactive=False):
    rows = _load(project_id, session_id)
    if not include_inactive:
        rows = [row for row in rows if row.get("status") == "active"]
    rows.sort(key=lambda row: str(row.get("next_due") or ""))
    return rows


def create(project_id, session_id, *, message, delay_seconds=60,
           interval_seconds=0, task="", repeat=None, source="model"):
    text = str(message or "").strip()[:MAX_MESSAGE]
    if not text:
        raise ValueError("message 不能为空")
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("没有活动会话，不能设置定时器")
    now = _now()
    try:
        delay = float(delay_seconds if delay_seconds is not None else 60)
    except (TypeError, ValueError):
        delay = 60.0
    try:
        interval = float(interval_seconds or 0)
    except (TypeError, ValueError):
        interval = 0.0
    delay = min(MAX_DELAY_SECONDS, max(0.0, delay))
    interval = min(MAX_DELAY_SECONDS, max(0.0, interval))
    should_repeat = bool(repeat) if repeat is not None else interval > 0
    if should_repeat and interval <= 0:
        interval = max(delay, 1.0)
    with _LOCK:
        rows = _prune(_load(project_id, session_id))
        if len([row for row in rows if row.get("status") == "active"]) >= MAX_ACTIVE:
            raise ValueError("本会话活动定时器已达到上限，先取消不需要的")
        item = {
            "id": "tm-" + uuid.uuid4().hex[:12],
            "message": text,
            "task": str(task or "")[:120],
            "source": str(source or "model")[:40],
            "interval_seconds": int(interval) if should_repeat else 0,
            "repeat": should_repeat,
            "next_due": _iso(now + timedelta(seconds=delay)),
            "last_fired": "",
            "fired": 0,
            "status": "active",
            "created": _iso(now),
            "updated": _iso(now),
        }
        rows.append(item)
        _save(project_id, session_id, rows)
    return item


def cancel(project_id, session_id, timer_id):
    timer_id = str(timer_id or "").strip()
    if not timer_id:
        return None
    with _LOCK:
        rows = _load(project_id, session_id)
        hit = None
        for row in rows:
            if str(row.get("id") or "") == timer_id:
                row["status"] = "cancelled"
                row["updated"] = _iso(_now())
                hit = row
                break
        if hit is not None:
            _save(project_id, session_id, rows)
        return hit


def due(project_id, session_id, now=None):
    """取到期定时器：重复项自动推后，单次项标记 fired；返回提醒正文。"""
    now = now or _now()
    reminders = []
    with _LOCK:
        rows = _load(project_id, session_id)
        changed = False
        for row in rows:
            if row.get("status") != "active":
                continue
            due_at = _parse(row.get("next_due"))
            if due_at is None or due_at > now:
                continue
            row["last_fired"] = _iso(now)
            row["fired"] = int(row.get("fired") or 0) + 1
            row["updated"] = _iso(now)
            repeat = bool(row.get("repeat"))
            interval = int(row.get("interval_seconds") or 0)
            if repeat and interval > 0:
                row["next_due"] = _iso(now + timedelta(seconds=interval))
            else:
                row["status"] = "fired"
                row["next_due"] = ""
            changed = True
            task = str(row.get("task") or "").strip()
            hint = ("请检查相关任务是否完成：完成则调用 timer_cancel(timer_id=\"%s\")；"
                    "未完成则继续处理，本定时器会按间隔再次提醒。" % row["id"])
            if not repeat:
                hint = "这是一次性提醒；若仍未完成，可按需重新 timer_set。"
            reminders.append({
                "id": row["id"], "message": row.get("message") or "",
                "task": task, "repeat": repeat, "fired": row["fired"],
                "reminder": ("【定时提醒 %s】%s%s\n%s"
                             % (row["id"], (task + "：") if task else "",
                                row.get("message") or "", hint)),
            })
        if changed:
            _save(project_id, session_id, rows)
    return reminders
