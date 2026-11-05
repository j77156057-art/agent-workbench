"""上下文面板后端：把"本回合实际注入/下回合将注入"的六层内容整理成可读数据，
并提供面板上的编辑入口（全部写审计；密钥一律拒绝，不做静默脱敏）。
"""
from __future__ import annotations

import json
import os

from config import get_runtime

from . import (constitution, episodes, errors, hooks, injection, profile, promote,
               store, taskbook)

# 面板分母必须与真实注入封顶同源：profile 引用 profile.PROFILE_INJECT_MAX_CHARS，
# 其余四项沿用各自模块的实际注入下限（本轮不改它们的行为）。
BUDGETS = {"rule": 600, "profile": profile.PROFILE_INJECT_MAX_CHARS,
           "shared": 800, "taskbook": 400, "hook": 600}
TITLES = {"rule": "常驻守则", "profile": "用户画像", "shared": "共享记忆",
          "taskbook": "任务书", "hook": "会话钩子"}

# 旧 trace 里的提示词层编号 → 新提示词层 key（与图上的 store 分层不是同一套，
# 例如旧 L1 是画像而不是项目长期记忆）。仅用于读历史快照。
_PROMPT_LAYER_ALIASES = {"L0": "rule", "L1": "profile", "L2": "shared",
                         "L3": "taskbook", "L4": "hook"}


def _prompt_layer_key(value):
    text = str(value or "")
    return _PROMPT_LAYER_ALIASES.get(text, text)


def _summary_inject_cap():
    """摘要层的注入封顶：与 sessions 共用同一个派生值，面板不再写死数字。

    没有 Agent 实例的面板预览按**当前运行时模型**的窗口推导（与注入侧
    `agent._prompt_budget()` 同一口径）；取不到模型信息时回退默认 11000。
    """
    try:
        import sessions
        tokens = injection._current_prompt_tokens()
        return int(sessions.summary_char_budget(tokens or None))
    except Exception:  # noqa: BLE001 —— 面板不能因为取不到常量就整页失败
        return 0


def _layer(key, title, text, *, editable, source, kind, budget=None, **extra):
    value = str(text or "")
    if budget is None:
        budget = BUDGETS.get(key)
        if budget is None and key == "summary":
            # 历史摘要现在也有真实注入上限（cap_summary_tail），分母必须显示出来。
            budget = _summary_inject_cap()
    data = {"key": key, "title": title, "text": value, "chars": len(value),
            "budget": budget,
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
    return _layer("profile", "用户画像", text, editable=True, source="用户/系统",
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
    layers.append(_layer("rule", "常驻守则", doc.get("text") or "", editable=True,
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
    block = injection.build_shared_injection(max_chars=caps["shared"])
    retrievable_items = promote.retrievable(project, 30)
    layers.append(_layer("shared", "共享记忆", block[0]["text"] if block else "",
                         budget=caps["shared"],
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
    layers.append(_layer("taskbook", "任务书", taskbook.summary(project, max_chars=caps["project"]),
                         budget=caps["project"], editable=True,
                         source="任务", kind="taskbook",
                         book={"id": book.get("id"), "title": book.get("title"),
                               "status": book.get("status")},
                         items=book.get("items") or []))

    block = injection.build_session_injection(session, project, max_chars=caps["hook"])
    latest = episodes.latest(project, session, 1) if session else []
    layers.append(_layer("hook", "会话钩子", block[0]["text"] if block else "",
                         budget=caps["hook"],
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
            layer_key = _prompt_layer_key(block.get("layer_key") or block.get("layer"))
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


def _agent_scoped_session(session_id, agent_id):
    """把面板的 agent 维度映射到它自己的会话，避免跨 agent 读到主会话 pinned。

    主 agent 用调用方给的 session_id；子 agent 一律用 `sub-<agent_id>` 会话，
    客户端传来的 session_id 不参与，防止绕过 agent 隔离。
    """
    owner = str(agent_id or "main").strip() or "main"
    if owner == "main":
        return owner, str(session_id or "").strip()
    import sessions
    return owner, sessions.subagent_session_id(owner)


def pinned_view(session_id, agent_id="main"):
    """固定事实只读视图（按 agent 作用域解析会话）。"""
    import sessions
    owner, sid = _agent_scoped_session(session_id, agent_id)
    if not sid:
        return {"ok": True, "agent_id": owner, "session_id": "",
                "entries": [], "tombstones": [], "audit": []}
    out = sessions.pinned_view(sid)
    return {"ok": True, "agent_id": owner, **out}


def delete_pinned(entry_id, session_id, agent_id="main", actor="user"):
    """固定事实删除：只有用户路径可达；模型路径在 sessions 层被拒绝。"""
    import sessions
    owner, sid = _agent_scoped_session(session_id, agent_id)
    if not sid:
        raise ValueError("缺少 session_id")
    out = sessions.delete_pinned(entry_id, actor=actor, session_id=sid)
    return {**out, "agent_id": owner, "session_id": sid}

