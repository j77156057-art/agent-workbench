"""L3 任务书：跨窗口共享的任务状态；JSON 为真，markdown 只渲染。

完成态不由模型宣布：`done` 必须挂"测试/验收通过的回执"和一条合并记录；
只有回执没有合并 → `done_pending_merge`；两者都没有 → 拒绝标记完成。
P0 阶段以现有的 `dev_plan` 作为模型自评入口：计划 ≥3 项才成书；长任务跑满
15 步仍无任务书时由系统从回合现场草拟一份（见 maybe_draft）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid

from . import receipts, store

MAX_ITEMS = 80
MAX_TEXT = 400
STATES = {"todo", "doing", "done_pending_merge", "done"}
_STATE_ORDER = {"doing": 0, "done_pending_merge": 1, "todo": 2, "done": 3}
_LABELS = {"todo": " ", "doing": "~", "done_pending_merge": "待验收", "done": "x"}
_LOCK = threading.RLock()


def _dir(project_id):
    return store.safe_path("projects", store.safe_id(project_id))


def book_path(project_id):
    return os.path.join(_dir(project_id), "taskbook.json")


def events_path(project_id):
    return os.path.join(_dir(project_id), "events.jsonl")


def md_path(project_id):
    return os.path.join(_dir(project_id), "taskbook.md")


def _key_of(text):
    slug = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", str(text or "").strip().lower())
    return slug.strip("-")[:48] or "item-" + uuid.uuid4().hex[:8]


def _load(project_id):
    try:
        with open(book_path(project_id), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "version": 1,
        "id": str(value.get("id") or ("tb-" + uuid.uuid4().hex[:10])),
        "title": str(value.get("title") or ""),
        "status": str(value.get("status") or "open"),
        "created": str(value.get("created") or store.now()),
        "updated": str(value.get("updated") or ""),
        "items": [item for item in (value.get("items") or []) if isinstance(item, dict)][:MAX_ITEMS],
    }


def _event(project_id, kind, **payload):
    try:
        target = events_path(project_id)
        store.ensure_dir(os.path.dirname(target))
        record = {"ts": store.now(), "event": str(kind)}
        record.update({key: value for key, value in payload.items() if value is not None})
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 —— 事件日志失败不阻断任务书
        return False
    return True


def _render_md(book):
    lines = ["# " + (book.get("title") or "任务书"), ""]
    for item in book.get("items") or []:
        state = item.get("state")
        mark = _LABELS.get(state, " ")
        suffix = "（用户强制完成）" if item.get("user_override") else ""
        lines.append(f"- [{mark}] {item.get('text')}{suffix}  <!-- {item.get('key')} -->")
    return "\n".join(lines) + "\n"


def _save(project_id, book):
    book["updated"] = store.now()
    store.atomic_write(book_path(project_id),
                       json.dumps(book, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    store.atomic_write(md_path(project_id), _render_md(book))


def _new_item(text, state="todo"):
    now = store.now()
    return {"key": _key_of(text), "text": str(text or "").strip()[:MAX_TEXT],
            "state": state, "claim": None, "receipts": [], "merge": "",
            "done_at": "", "user_override": False, "created": now, "updated": now}


def open_book(project_id, title, items):
    """新建/覆盖任务书（保留同名条目的状态）。"""
    with _LOCK:
        book = _load(project_id)
        previous = {item.get("key"): item for item in book.get("items") or []}
        fresh = []
        for raw in items or []:
            text = str(raw.get("text") if isinstance(raw, dict) else raw or "").strip()
            if not text:
                continue
            item = _new_item(text)
            old = previous.get(item["key"])
            if old:
                for key in ("state", "claim", "receipts", "merge", "done_at", "user_override"):
                    item[key] = old.get(key)
            fresh.append(item)
        book["id"] = "tb-" + uuid.uuid4().hex[:10]
        book["title"] = str(title or "").strip()[:MAX_TEXT]
        book["status"] = "open"
        book["created"] = store.now()
        book["items"] = fresh[:MAX_ITEMS]
        _save(project_id, book)
        _event(project_id, "open", task=book["id"], title=book["title"], items=len(fresh))
        return book


def sync_from_plan(project_id, code_root):
    """把当前 dev_plan 镜像成任务书；计划 <3 项且尚无任务书时不建（小任务不需要）。"""
    try:
        from agent_runtime import plan_state
        plan = plan_state.read(code_root) if code_root else {"items": []}
    except Exception:  # noqa: BLE001
        return None
    raw_items = [item for item in plan.get("items") or [] if isinstance(item, dict)]
    if not raw_items:
        return _load(project_id) if os.path.exists(book_path(project_id)) else None
    with _LOCK:
        book = _load(project_id)
        exists = bool(book.get("items")) or os.path.exists(book_path(project_id))
        if not exists and len(raw_items) < 3:
            return None
        previous = {item.get("key"): item for item in book.get("items") or []}
        items = []
        for raw in raw_items:
            text = str(raw.get("text") or "").strip()[:MAX_TEXT]
            if not text:
                continue
            key = str(raw.get("key") or "").strip() or _key_of(text)
            old = previous.get(key)
            if old:
                item = dict(old)
                item["text"] = text
                if raw.get("done") and item.get("state") in ("todo", "doing"):
                    item["state"] = "done_pending_merge"
                elif not raw.get("done") and item.get("state") in ("done", "done_pending_merge"):
                    item["state"] = "todo"
                    item["done_at"] = ""
            else:
                item = _new_item(text, "done_pending_merge" if raw.get("done") else "todo")
                item["key"] = key
            item["updated"] = store.now()
            items.append(item)
        changed = json.dumps(items, ensure_ascii=False, sort_keys=True) != \
            json.dumps(book.get("items"), ensure_ascii=False, sort_keys=True)
        book["items"] = items[:MAX_ITEMS]
        if not book.get("title"):
            book["title"] = str(plan.get("title") or "任务书")[:MAX_TEXT]
        if changed:
            _save(project_id, book)
            _event(project_id, "sync", task=book["id"], items=len(book["items"]))
        return book


def maybe_draft(project_id, question, next_step="", actions_count=0):
    """长任务兜底：跑满 15 步仍没有任务书，就从回合现场草拟一份。"""
    if actions_count < 15 or os.path.exists(book_path(project_id)):
        return None
    text = str(next_step or "").strip() or ("继续：" + str(question or "").strip()[:120])
    with _LOCK:
        book = _load(project_id)
        book["id"] = "tb-" + uuid.uuid4().hex[:10]
        book["title"] = str(question or "长任务").strip()[:60] or "长任务"
        item = _new_item(text, "doing")
        book["items"] = [item]
        _save(project_id, book)
        _event(project_id, "fallback_draft", task=book["id"], items=1)
        return book


def items(project_id):
    return _load(project_id).get("items") or []


def current_id(project_id):
    if not os.path.exists(book_path(project_id)):
        return ""
    return str(_load(project_id).get("id") or "")


def pending(project_id):
    rows = [item for item in items(project_id) if item.get("state") != "done"]
    rows.sort(key=lambda item: _STATE_ORDER.get(item.get("state"), 9))
    return rows


def summary(project_id, max_chars=400):
    book = _load(project_id)
    rows = pending(project_id)
    if not book.get("items") or not rows:
        return ""
    lines = ["【任务书（参考，不权威；完成以回执+合并为准）】" + (book.get("title") or "")]
    for item in rows[:8]:
        state = item.get("state")
        label = {"doing": "进行中", "done_pending_merge": "待验收"}.get(state, "待办")
        lines.append(f"- [{label}] {item.get('text')}")
    if len(rows) > 8:
        lines.append(f"- …… 还有 {len(rows) - 8} 项")
    lines.append("下一件：" + rows[0]["text"])
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 1] + "…"
    return text


def claim(project_id, key, session, ttl=1800):
    from datetime import datetime, timedelta, timezone
    with _LOCK:
        book = _load(project_id)
        item = next((row for row in book["items"] if row.get("key") == key), None)
        if item is None:
            return {"ok": False, "reason": "未找到条目"}
        if item.get("state") == "done":
            return {"ok": False, "reason": "条目已完成"}
        holder = item.get("claim") or {}
        now = datetime.now(timezone.utc)
        until = str(holder.get("until") or "")
        if holder.get("session") and holder.get("session") != session and until > now.isoformat():
            return {"ok": False, "reason": "被其他窗口占用", "claimed_by": holder.get("session")}
        item["claim"] = {"session": str(session or ""),
                         "until": (now + timedelta(seconds=int(ttl))).isoformat(timespec="seconds")}
        item["state"] = "doing"
        item["updated"] = store.now()
        _save(project_id, book)
        _event(project_id, "claim", task=book["id"], key=key, session=session)
        return {"ok": True, "item": item}


def complete(project_id, key, *, receipt_ids=(), merge="", user_override=False):
    """标记完成：必须有 pass 的 test/verify 回执 + 合并记录；缺合并 → 待验收。"""
    with _LOCK:
        book = _load(project_id)
        item = next((row for row in book["items"] if row.get("key") == key), None)
        if item is None:
            raise ValueError(f"未找到条目: {key}")
        valid = [rid for rid in receipt_ids if receipts.get(project_id, rid)]
        passed = [rid for rid in valid
                  if receipts.get(project_id, rid).get("type") in ("test", "verify")
                  and receipts.get(project_id, rid).get("result") == "pass"]
        merged = bool(merge) or any(
            receipts.get(project_id, rid).get("type") == "merge"
            and receipts.get(project_id, rid).get("result") == "pass" for rid in valid)
        if user_override:
            item["state"] = "done"
            item["user_override"] = True
        elif passed and merged:
            item["state"] = "done"
        elif passed:
            item["state"] = "done_pending_merge"
        else:
            raise ValueError("完成需要至少一张测试/验收通过的回执")
        item["receipts"] = valid
        item["merge"] = str(merge or item.get("merge") or "")
        item["done_at"] = store.now() if item["state"] == "done" else ""
        item["claim"] = None
        item["updated"] = store.now()
        _save(project_id, book)
        _event(project_id, "complete", task=book["id"], key=key,
               state=item["state"], merge=item["merge"], override=bool(user_override))
        return item


def reopen(project_id, key):
    with _LOCK:
        book = _load(project_id)
        item = next((row for row in book["items"] if row.get("key") == key), None)
        if item is None:
            return None
        item["state"] = "todo"
        item["done_at"] = ""
        item["updated"] = store.now()
        _save(project_id, book)
        _event(project_id, "reopen", task=book["id"], key=key)
        return item


def edit_text(project_id, key, text):
    with _LOCK:
        book = _load(project_id)
        item = next((row for row in book["items"] if row.get("key") == key), None)
        if item is None:
            return None
        value = str(text or "").strip()[:MAX_TEXT]
        if not value:
            raise ValueError("条目内容不能为空")
        item["text"] = value
        item["updated"] = store.now()
        _save(project_id, book)
        _event(project_id, "edit", task=book["id"], key=key)
        return item


def info(project_id):
    book = _load(project_id)
    return {"id": book.get("id"), "title": book.get("title"),
            "status": book.get("status"), "updated": book.get("updated"),
            "items": book.get("items") or []}


def render(project_id):
    return _render_md(_load(project_id))
