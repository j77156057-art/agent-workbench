"""上下文面板后端：把"本回合实际注入/下回合将注入"的六层内容整理成可读数据，
并提供面板上的编辑入口（全部写审计；密钥一律拒绝，不做静默脱敏）。
"""
from __future__ import annotations

import json
import os

from config import get_runtime

from . import (constitution, episodes, errors, hooks, injection, profile, promote,
               store, taskbook)

BUDGETS = {"L0": 600, "L1": 500, "L2": 800, "L3": 400, "L4": 600}
TITLES = {"L0": "宪法", "L1": "用户画像", "L2": "共享记忆", "L3": "任务书", "L4": "会话钩子"}


def _layer(key, title, text, *, editable, source, kind, budget=None, **extra):
    value = str(text or "")
    data = {"key": key, "title": title, "text": value, "chars": len(value),
            "budget": BUDGETS.get(key) if budget is None else budget,
            "editable": bool(editable), "source": source, "kind": kind}
    data.update(extra)
    return data


def _profile_layer():
    try:
        data = profile.load()
        text = profile.context()
        declared = {key: data.get(key) for key in
                    ("display_name", "language", "timezone", "location", "notes",
                     "preferences", "goals")}
        learned = data.get("learned") or {}
    except Exception:  # noqa: BLE001
        declared, learned, text = {}, {}, ""
    return _layer("L1", "用户画像", text, editable=True, source="用户/系统",
                  kind="profile", profile=declared, learned=learned)


def next_view(project_id, session_id, code_root=""):
    """下一回合将注入的内容；与 agent._build_messages 的取数口径一致。"""
    project = store.safe_id(project_id or "default")
    session = str(session_id or "")
    layers = []
    # 与 agent._build_messages 同一套随窗口缩放的天花板，否则面板显示的预算是假的。
    caps = injection.discretionary_budgets()

    doc = constitution.load(ensure=True)
    raw = ""
    if doc.get("path") and os.path.exists(doc["path"]):
        try:
            raw = store.read_text(doc["path"])
        except OSError:
            raw = ""
    layers.append(_layer("L0", "宪法", doc.get("text") or "", editable=True,
                         source="用户", kind="markdown", raw=raw,
                         version=doc.get("version") or 0,
                         hash_ok=bool(doc.get("hash_ok")),
                         gates_ok=bool(doc.get("gates_ok")),
                         missing_gates=doc.get("missing_gates") or [],
                         error=doc.get("error") or "", path=doc.get("path") or ""))

    layers.append(_profile_layer())

    rules = str(get_runtime("project_rules", "") or "")
    layers.append(_layer("rules", "项目规则", rules, editable=False, source="项目",
                         kind="readonly", path="DOCMIND_RULES.md"))

    pinned = promote.pinned_shared(5)
    block = injection.build_shared_injection(max_chars=caps["L2"])
    retrievable_items = promote.retrievable(project, 30)
    layers.append(_layer("L2", "共享记忆", block[0]["text"] if block else "",
                         budget=caps["L2"],
                         editable=True, source="已验证沉淀", kind="items",
                         items=[{"id": item.get("id"), "kind": item.get("kind"),
                                 "title": item.get("title"), "body": item.get("body"),
                                 "pinned": bool(item.get("pinned")),
                                 "confidence": item.get("confidence"),
                                 "importance": item.get("importance"),
                                 "edges": item.get("edges") or []}
                                for item in pinned],
                         retrievable=len(retrievable_items),
                         retrievable_items=retrievable_items))

    book = taskbook.info(project)
    layers.append(_layer("L3", "任务书", taskbook.summary(project, max_chars=caps["L3"]),
                         budget=caps["L3"], editable=True,
                         source="任务", kind="taskbook",
                         book={"id": book.get("id"), "title": book.get("title"),
                               "status": book.get("status")},
                         items=book.get("items") or []))

    block = injection.build_session_injection(session, project, max_chars=caps["L4"])
    latest = episodes.latest(project, session, 1) if session else []
    layers.append(_layer("L4", "会话钩子", block[0]["text"] if block else "",
                         budget=caps["L4"],
                         editable=True, source="本窗口", kind="hooks",
                         items=hooks.items(project, session) if session else [],
                         errors=errors.top(project, session, 5) if session else [],
                         project_errors=errors.top_project(project, 5),
                         next=str((latest[0].get("next") if latest else "") or "")))

    summary = ""
    if session:
        try:
            import sessions
            summary = sessions.summary_text(session, project or None)
        except Exception:  # noqa: BLE001
            summary = ""
    layers.append(_layer("summary", "历史摘要", summary, editable=False,
                         source="系统", kind="readonly"))

    return {"ok": True, "enabled": True, "mode": "next", "project": project,
            "session": session, "layers": layers,
            "total_chars": sum(layer["chars"] for layer in layers)}


def last_view(session_id):
    """上一回合实际注入的快照（来自 turn trace 的 injection 字段）。"""
    try:
        import agent_trace
        rows = agent_trace.recent(500)
    except Exception:  # noqa: BLE001
        rows = []
    session = str(session_id or "")
    record = None
    for row in reversed(rows):
        if not isinstance(row, dict) or not row.get("injection"):
            continue
        if session and str(row.get("session_id") or "") != session:
            continue
        record = row
        break
    layers = []
    if record:
        for block in record.get("injection") or []:
            if not isinstance(block, dict):
                continue
            layer_key = str(block.get("layer") or "")
            text = str(block.get("text") or "")
            layers.append(_layer(layer_key, TITLES.get(layer_key, layer_key), text,
                                 editable=False, source="上一回合快照", kind="readonly",
                                 hash=block.get("hash"), version=block.get("version")))
    return {"ok": True, "enabled": True, "mode": "last", "session": session,
            "turn_id": (record or {}).get("turn_id"), "ts": (record or {}).get("ts"),
            "layers": layers, "total_chars": sum(layer["chars"] for layer in layers)}


def save_constitution(text, base_version=None):
    doc = constitution.save_text(str(text or ""), base_version=base_version)
    return {"version": doc["version"], "hash_ok": doc["hash_ok"]}


def update_profile(patch, user_id=None):
    allowed = ("display_name", "language", "timezone", "location", "preferences", "goals", "notes")
    clean = {key: patch[key] for key in allowed if key in patch}
    if promote.secret_hit(json.dumps(clean, ensure_ascii=False)):
        raise ValueError("内容含密钥/令牌，未保存")
    return profile.save(clean, user_id)


def edit_shared(item_id, *, text=None, pinned=None):
    if text is not None and promote.secret_hit(text):
        raise ValueError("内容含密钥/令牌，未保存")
    return promote.edit_shared(item_id, text=text, pinned=pinned)


def delete_shared(item_id):
    return promote.delete_shared(item_id)


def add_hook(project_id, session_id, kind, text, pinned=True):
    if promote.secret_hit(text):
        raise ValueError("内容含密钥/令牌，未保存")
    return hooks.add(project_id, session_id, kind, text,
                     verified=False, pinned=bool(pinned))


def edit_hook(project_id, session_id, item_id, *, text=None, pinned=None):
    if text is not None and promote.secret_hit(text):
        raise ValueError("内容含密钥/令牌，未保存")
    return hooks.update_item(project_id, session_id, item_id, text=text, pinned=pinned)


def delete_hook(project_id, session_id, item_id):
    return hooks.remove(project_id, session_id, item_id)


def edit_task_item(project_id, key, text):
    if promote.secret_hit(text):
        raise ValueError("内容含密钥/令牌，未保存")
    return taskbook.edit_text(project_id, key, text)


def complete_task_item(project_id, key, *, receipt_ids=(), merge="", override=False):
    return taskbook.complete(project_id, key, receipt_ids=receipt_ids,
                            merge=merge, user_override=bool(override))


def reopen_task_item(project_id, key):
    return taskbook.reopen(project_id, key)


def reindex():
    from . import index
    return index.rebuild(verify=True)
