"""L4 钩子：压缩后仍要保留的关键事实、用户约定与易错点。

钩子是"防遗忘"而不是"新指令"——注入时明确标注仅供参考，不覆盖用户当前要求。
同一条文本按 kind+文本 去重；上限 80 条，超出先淘汰未 pin 的旧条目。
"""
from __future__ import annotations

import re
import threading
import uuid

from . import store

MAX_ITEMS = 80
MAX_TEXT = 300
_LOCK = threading.RLock()

KIND_LABELS = {"fact": "事实", "decision": "决策", "user_agreement": "约定",
               "error": "易错", "next": "下一步"}


def path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id), "session",
                           store.safe_id(session_id), "hooks.md")


def _key(kind, text):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()[:160]
    return kind + "|" + normalized


def _trim(rows):
    if len(rows) <= MAX_ITEMS:
        return rows
    pinned = [row for row in rows if row.get("pinned")][-MAX_ITEMS:]
    others = [row for row in rows if not row.get("pinned")]
    room = MAX_ITEMS - len(pinned)
    return pinned + (others[-room:] if room > 0 else [])


def items(project_id, session_id, *, pinned_only=False):
    rows = store.load_items(path(project_id, session_id))
    if pinned_only:
        rows = [row for row in rows if row.get("pinned")]
    rows.sort(key=lambda row: (bool(row.get("pinned")), str(row.get("updated") or "")),
              reverse=True)
    return rows


def add(project_id, session_id, kind, text, *, source_turn="", verified=False,
        pinned=True, key=None):
    kind = str(kind or "fact").strip() or "fact"
    text = str(text or "").strip()[:MAX_TEXT]
    if not text:
        return None
    key = key or _key(kind, text)
    target = path(project_id, session_id)
    with _LOCK:
        rows = items(project_id, session_id)
        now = store.now()
        for row in rows:
            if row.get("key") == key:
                row["text"] = text
                row["verified"] = bool(verified or row.get("verified"))
                row["pinned"] = bool(pinned or row.get("pinned"))
                row["updated"] = now
                if source_turn:
                    row["source_turn"] = str(source_turn)
                store.save_items(target, _trim(rows))
                return row
        row = {"id": "hk-" + uuid.uuid4().hex[:10], "key": key, "kind": kind,
               "text": text, "source_turn": str(source_turn or ""),
               "verified": bool(verified), "pinned": bool(pinned),
               "created": now, "updated": now}
        rows.append(row)
        store.save_items(target, _trim(rows))
        return row


def set_pinned(project_id, session_id, item_id, pinned):
    with _LOCK:
        rows = items(project_id, session_id)
        for row in rows:
            if row.get("id") == item_id:
                row["pinned"] = bool(pinned)
                row["updated"] = store.now()
                store.save_items(path(project_id, session_id), rows)
                return row
    return None


def update_item(project_id, session_id, item_id, *, text=None, pinned=None):
    """面板编辑钩子：只改文本/pin，kind 与来源不动。"""
    with _LOCK:
        rows = items(project_id, session_id)
        for row in rows:
            if row.get("id") != item_id:
                continue
            if text is not None:
                value = str(text).strip()[:MAX_TEXT]
                if not value:
                    raise ValueError("钩子内容不能为空")
                row["text"] = value
                row["verified"] = False
            if pinned is not None:
                row["pinned"] = bool(pinned)
            row["updated"] = store.now()
            store.save_items(path(project_id, session_id), rows)
            return row
    return None


def remove(project_id, session_id, item_id):
    with _LOCK:
        rows = items(project_id, session_id)
        kept = [row for row in rows if row.get("id") != item_id]
        if len(kept) == len(rows):
            return False
        store.save_items(path(project_id, session_id), kept)
        return True


def render(project_id, session_id, *, limit=8, max_chars=400, include_errors=False):
    """把 pin 住的钩子渲染成一段有上限的注入文本；错误类默认交给错误账本。"""
    lines = []
    used = 0
    for row in items(project_id, session_id, pinned_only=True):
        kind = str(row.get("kind") or "fact")
        if kind == "error" and not include_errors:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        suffix = "" if row.get("verified") or kind == "user_agreement" else "（未验证）"
        line = f"- [{KIND_LABELS.get(kind, kind)}] {text}{suffix}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        if len(lines) >= limit:
            break
    return "\n".join(lines)
