"""任务级星级反馈：用户满意度的外部信号。

它只影响记忆的置信/晋升，不直接改提示词；忽略不等于差评。
P0 只落盘与回读；归因到具体记忆的权重在 P4 接晋升时消费。
"""
from __future__ import annotations

import json
import os
import uuid

from . import store

PATH_NAME = "feedback.jsonl"


def path(project_id, session_id=""):
    if session_id:
        return store.safe_path("projects", store.safe_id(project_id), "session",
                               store.safe_id(session_id), PATH_NAME)
    return store.safe_path("projects", store.safe_id(project_id), PATH_NAME)


def record(project_id, *, stars, session="", task="", note="",
           memories_used=(), memories_written=()):
    try:
        score = int(stars)
    except (TypeError, ValueError) as exc:
        raise ValueError("stars 必须是 1-5 的整数") from exc
    if not 1 <= score <= 5:
        raise ValueError("stars 必须是 1-5 的整数")
    row = {
        "id": "fb-" + uuid.uuid4().hex[:10],
        "ts": store.now(),
        "project": store.safe_id(project_id),
        "session": store.safe_id(session, ""),
        "task": str(task or "")[:120],
        "stars": score,
        "note": str(note or "")[:500],
        "memories_used": [str(item) for item in (memories_used or [])][:50],
        "memories_written": [str(item) for item in (memories_written or [])][:50],
    }
    target = path(project_id, session)
    store.ensure_dir(os.path.dirname(target))
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def latest(project_id, session=""):
    target = path(project_id, session)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def list_for_task(project_id, task, session="", limit=20):
    target = path(project_id, session)
    rows = []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and str(value.get("task") or "") == str(task):
                    rows.append(value)
    except OSError:
        return []
    return rows[-max(1, int(limit)):]


def max_stars(project_id, task):
    """跨该项目的所有会话找这个任务的最高星级；无反馈返回 0。"""
    import glob
    base = store.safe_path("projects", store.safe_id(project_id))
    best = 0
    for target in glob.glob(os.path.join(base, "**", PATH_NAME), recursive=True):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    if task and str(value.get("task") or "") != str(task):
                        continue
                    best = max(best, int(value.get("stars") or 0))
        except OSError:
            continue
    return best
