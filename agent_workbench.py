"""Agent 卡片墙的只读数据面：花名册 / 对话记录 / 记忆 / 使用台账。

全部读持久化数据（sessions、delegations 快照、agent_trace JSONL、agent_memory SQLite），
不依赖进程内 Agent 实例，因此后端重启后仍能看到真实历史。
缺字段一律返回空值，由前端如实显示「未记录」，不补造数据。
"""
from __future__ import annotations

import re

import agent_memory
import agent_trace
import sessions as session_store

_ROLE_LABELS = {
    "main": "平台助手",
    "researcher": "调研员",
    "research": "调研员",
    "coder": "实现工程师",
    "implementer": "实现工程师",
    "reviewer": "审查员",
    "tester": "测试员",
    "planner": "规划员",
    "explorer": "探索员",
    "writer": "文档员",
    "assistant": "助理",
}
_STATUS_LABELS = {
    "running": "运行中",
    "active": "在线",
    "waiting_child": "等待子任务",
    "pending": "待审批",
    "approved": "已批准",
    "completed": "已完成",
    "failed": "失败",
    "rejected": "已拒绝",
    "unknown": "结果未知",
    "expired": "已过期",
}
_ACTION_RE = re.compile(r"^([a-zA-Z_][\w]*)\s*(?:\((.*)\))?$")

# 使用台账里「未记录」的统一占位，前端直接展示，不伪造数值。
UNRECORDED = "未记录"


def _role_label(role):
    role = str(role or "").strip()
    return _ROLE_LABELS.get(role, role or "未知 agent")


def _agent_name(role, agent_id):
    label = _role_label(role)
    short = str(agent_id or "").replace("ag-", "")[:8]
    return f"{label} · {short}" if short else label


def _status_label(status):
    return _STATUS_LABELS.get(str(status or "").strip(), str(status or "") or "未知")


def _node_message_count(node):
    result = node.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    count = 1 if str(node.get("task") or "").strip() else 0
    if str(result.get("conclusion") or "").strip() or str(node.get("error") or "").strip():
        count += 1
    return count


def _delegation_nodes():
    from agent_runtime import delegation

    try:
        return delegation.all_nodes()
    except Exception:  # noqa: BLE001 —— 快照不可读时宁可空列表，也不编造
        return []


def _has_sub_session(agent_id, project_id):
    data = session_store.load(session_store.subagent_session_id(str(agent_id)), project_id)
    return bool(data.get("turns"))


def roster(project_id, session_id):
    """卡片墙花名册：平台助手 + 所有持久化过的委派 agent。"""
    session = str(session_id or "default")
    data = session_store.load(session, project_id)
    turns = data.get("turns") or []
    main = {
        "agent_id": "main",
        "name": _role_label("main"),
        "role": "main",
        "role_label": _role_label("main"),
        "status": "active",
        "status_label": _status_label("active"),
        "last_active": str(data.get("updated_at") or ""),
        # 与对话 Tab 的口径一致：消息 = 非空 user + 非空 assistant，而不是回合数
        "message_count": sum(
            (1 if str(turn.get("user") or "").strip() else 0)
            + (1 if str(turn.get("assistant") or "").strip() else 0)
            for turn in turns
        ),
        "session_id": session,
        "source": "main",
        "is_main": True,
        "memory_owner": "main",
    }
    cards = [main]
    seen = set()
    for node in _delegation_nodes():
        agent_id = str(node.get("id") or "")
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        role = str(node.get("role") or "")
        status = str(node.get("status") or "")
        cards.append({
            "agent_id": agent_id,
            "name": _agent_name(role, agent_id),
            "role": role,
            "role_label": _role_label(role),
            "status": status,
            "status_label": _status_label(status),
            "last_active": str(node.get("last_seen") or node.get("created_at") or ""),
            "message_count": _node_message_count(node),
            "session_id": str(node.get("session_id") or session),
            "chat_session_id": session_store.subagent_session_id(agent_id),
            "archive_source": "sessions" if _has_sub_session(agent_id, project_id) else "delegation",
            "source": "delegation",
            "is_main": False,
            "memory_owner": agent_id,
            "parent_id": str(node.get("parent_id") or ""),
            "depth": node.get("depth"),
        })
    return {
        "ok": True,
        "session_id": session,
        "items": cards,
        "count": len(cards),
        "source": "sessions + .docmind/delegations/*.json",
    }


def _find_node(agent_id):
    for node in _delegation_nodes():
        if str(node.get("id") or "") == str(agent_id or ""):
            return node
    return None


def _session_messages(data):
    """把 sessions 的 turns 摊平成对话消息（前端 ChatDock 回放用的最小结构）。"""
    messages = []
    for turn in data.get("turns") or []:
        user = str(turn.get("user") or "")
        assistant = str(turn.get("assistant") or "")
        ts = str(turn.get("ts") or "")
        if user:
            messages.append({"role": "user", "text": user, "ts": ts,
                             "turn_id": str(turn.get("turn_id") or "")})
        if assistant:
            messages.append({"role": "assistant", "text": assistant, "ts": ts,
                             "turn_id": str(turn.get("turn_id") or "")})
    return messages


def conversation(project_id, session_id, agent_id):
    """真实对话记录：main 从会话库取；子 agent 优先读它自己的真 session，旧数据回退委派快照。"""
    if str(agent_id or "main") == "main":
        data = session_store.load(str(session_id or "default"), project_id)
        messages = _session_messages(data)
        return {
            "ok": True,
            "available": True,
            "agent_id": "main",
            "session_id": str(session_id or "default"),
            "messages": messages[-120:],
            "total_turns": len(data.get("turns") or []),
            "source": "sessions 会话库",
            "note": "" if messages else "该会话在服务端没有任何已落库的对话回合。",
        }
    # 子 agent 从这一版起有自己的真 session（sub-<ticket>），逐轮记录和 trace 与主 agent
    # 走同一条 sessions 落盘路径；前端的独立对话窗口直接按这个 id 回放。
    sub_id = session_store.subagent_session_id(str(agent_id))
    sub_data = session_store.load(sub_id, project_id)
    sub_turns = sub_data.get("turns") or []
    if sub_turns:
        messages = _session_messages(sub_data)
        return {
            "ok": True,
            "available": True,
            "archived": True,
            "agent_id": str(agent_id),
            "session_id": sub_id,
            "messages": messages[-120:],
            "total_turns": len(sub_turns),
            "source": "sessions 会话库（子 agent 真 session）",
            "note": "",
        }
    node = _find_node(agent_id)
    if not node:
        return {
            "ok": True, "available": False, "agent_id": str(agent_id), "messages": [],
            "note": "委派快照中找不到该 agent（可能已被清理）。",
            "source": UNRECORDED,
        }
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    messages = []
    task = str(node.get("task") or "").strip()
    if task:
        messages.append({"role": "user", "text": task,
                         "ts": str(node.get("created_at") or ""), "kind": "delegated_task"})
    conclusion = str(result.get("conclusion") or "").strip()
    if conclusion:
        messages.append({"role": "assistant", "text": conclusion,
                         "ts": str(node.get("last_seen") or node.get("decided_at") or ""),
                         "kind": "conclusion"})
    elif str(node.get("error") or "").strip():
        messages.append({"role": "assistant", "text": "执行失败：" + str(node.get("error")),
                         "ts": str(node.get("last_seen") or ""), "kind": "error"})
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    status = str(node.get("status") or "")
    note = ("历史未归档：该子 agent 运行于旧版本，逐轮记录没有落库；"
            "这里只展示委派任务与最终结论，没有补造对话。")
    if status in {"running", "waiting_child", "active"}:
        note = ("该子 agent 仍在运行，逐轮记录会在结束后归档；"
                "当前显示委派任务与已有结论。")
    return {
        "ok": True,
        "available": bool(messages),
        "archived": False,
        "agent_id": str(agent_id),
        "session_id": str(node.get("session_id") or session_id or ""),
        "chat_session_id": sub_id,
        "messages": messages,
        "total_turns": len(messages),
        "steps": trace.get("steps") or [],
        "source": ".docmind/delegations 快照（任务 + 结论 + 执行步骤）",
        "note": note,
    }


def memories(project_id, session_id, agent_id):
    """按 agent 归属读取项目长期记忆（真隔离：SQL 层按 agent_id 过滤）。"""
    owner = str(agent_id or "main")
    scope = agent_memory.scope_key(project_id or None)
    rows = agent_memory.audit(scope, agent_id=owner)
    items = []
    legacy = 0
    for row in rows:
        is_legacy = bool(int(row.get("agent_legacy") or 0))
        legacy += 1 if is_legacy else 0
        items.append({
            "id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or "fact"),
            "title": str(row.get("title") or ""),
            "content": str(row.get("content") or "")[:800],
            "evidence": str(row.get("evidence") or "")[:300],
            "source": str(row.get("source") or ""),
            "status": str(row.get("status") or "active"),
            "updated": str(row.get("updated") or ""),
            "agent_id": str(row.get("agent_id") or ""),
            "legacy_unclassified": is_legacy,
        })
    return {
        "ok": True,
        "agent_id": owner,
        "scope": scope,
        "items": items,
        "count": len(items),
        "active_count": sum(1 for item in items if item["status"] == "active"),
        "legacy_unclassified_count": legacy,
        "isolated_dimension": "memories.agent_id",
        "note": ("常驻守则 / 用户画像 / 共享记忆 / 会话钩子仍是项目或会话级内容语义层，"
                 "不按 agent 隔离；本列表只含该 agent 名下的长期记忆（legacy 库）。"
                 "原有 legacy 行统一归到平台助手并标记「历史未分类」；上下文面板里的 183 条"
                 "是 legacy + shared/project/hook 的总库存，不等于本列表条数。"),
        "source": "agent_memory SQLite（scope + agent_id 双维度）",
    }


def _step_entry(step, ts, default_agent):
    action = str(step.get("action") or "")
    match = _ACTION_RE.match(action)
    action_name = match.group(1) if match else action
    inline_arg = (match.group(2) or "") if match else ""
    kind = str(step.get("kind") or agent_trace.classify_action(action_name))
    name = str(step.get("name") or "")
    if not name or name == action:
        name = action_name or name
    summary = str(step.get("arg_preview") or "")
    if not summary and inline_arg and action_name in agent_trace.PREVIEW_ACTIONS:
        summary = inline_arg.strip()[:agent_trace.PREVIEW_CHARS]
    ok = step.get("ok")
    return {
        "type": kind,
        "name": name or action_name or "tool",
        "summary": summary,
        "status": ("成功" if ok else "失败") if isinstance(ok, bool) else UNRECORDED,
        "duration_ms": int(step.get("latency_ms")) if isinstance(step.get("latency_ms"), (int, float)) else None,
        "agent_id": str(step.get("agent_id") or default_agent),
        "ts": str(step.get("ts") or ts or ""),
        "obs_chars": int(step.get("obs_chars") or 0),
    }


def _trace_usage(session, agent_id, limit):
    entries = []
    for record in agent_trace.recent(8000):
        rec_agent = str(record.get("agent_id") or "")
        rec_session = str(record.get("session_id") or "")
        if agent_id == "main":
            if rec_session != session:
                continue
            if rec_agent and rec_agent != "main":
                continue
        elif rec_agent != agent_id:
            continue
        for step in record.get("steps") or []:
            entry = _step_entry(step, record.get("ts"), agent_id)
            entry["agent_id"] = rec_agent or agent_id
            entries.append(entry)
    entries.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return entries[:limit]


def _snapshot_usage(node, limit):
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    hooks = trace.get("hooks") or []
    after = [hook for hook in hooks if str(hook.get("kind") or "") == "after_tool"]
    steps = trace.get("steps") or []
    ts = str(node.get("last_seen") or node.get("created_at") or "")
    entries = []
    for index, step in enumerate(steps):
        entry = _step_entry(step, ts, str(node.get("id") or ""))
        if index < len(after):
            payload = after[index].get("payload") or {}
            raw_ok = str(payload.get("ok") or "").lower()
            if raw_ok in {"true", "false"}:
                entry["status"] = "成功" if raw_ok == "true" else "失败"
            try:
                entry["duration_ms"] = int(payload.get("duration_ms"))
            except (TypeError, ValueError):
                entry["duration_ms"] = None
        entries.append(entry)
    entries.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return entries[:limit]


def usage(project_id, session_id, agent_id, limit=200):
    """按时间倒序的使用台账（搜索 / skill / MCP / 工具）。"""
    owner = str(agent_id or "main")
    lim = max(1, min(int(limit or 200), 500))
    if owner == "main":
        entries = _trace_usage(str(session_id or "default"), owner, lim)
        source = "agent_trace JSONL（按 session_id 归属主 agent）"
        note = ""
    else:
        node = _find_node(owner)
        entries = _trace_usage("", owner, lim) if not node else []
        entries = entries or (_snapshot_usage(node, lim) if node else [])
        source = ("agent_trace JSONL（按 record.agent_id）" if entries and
                  any(item.get("obs_chars") for item in entries) else
                  ".docmind/delegations 快照 trace.steps / hooks")
        note = ("旧快照缺少逐条时间戳与入参正文：时间取该次委派批次时间，"
                "入参摘要与耗时缺失处显示「未记录」。")
    counts = {"search": 0, "skill": 0, "mcp": 0, "tool": 0}
    for item in entries:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {
        "ok": True,
        "agent_id": owner,
        "session_id": str(session_id or "default"),
        "items": entries,
        "count": len(entries),
        "counts": counts,
        "source": source,
        "note": note,
    }
