"""只读记忆检查器：记录实际注入快照，并把记忆库存与行为证据整理给 UI。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import re


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def capture(agent, *, question, legacy_rows, legacy_text, project_rules=""):
    """在固定提示词块组装完后记录事实快照；不改变任何提示词内容。"""
    history = getattr(agent, "history", []) or []
    initial = str((history[0].get("user") if history else question) or "")
    rows = [dict(row) for row in (legacy_rows or [])]
    blocks = [dict(block) for block in (getattr(agent, "_last_injection", []) or [])]
    rules = [{
        "id": "system-base", "source": "system", "title": "系统运行守则",
        "text": str(getattr(agent, "system_prompt", "") or ""),
    }, {
        "id": "session-initial", "source": "user", "title": "会话最初要求",
        "text": initial,
    }]
    if initial.strip():
        rules[-1]["text"] = initial
    else:
        rules.pop()
    for block in blocks:
        layer_key = str(block.get("layer_key") or block.get("layer") or "")
        if layer_key == "rule":
            rules.append({"id": "user-constitution", "source": "user",
                          "title": "常驻守则", "text": str(block.get("text") or "")})
        elif layer_key == "hook":
            rules.append({"id": "session-hooks", "source": "session",
                          "title": "会话钩子与约定", "text": str(block.get("text") or "")})
    if str(project_rules or ""):
        rules.append({"id": "project-rules", "source": "project", "title": "项目规则",
                      "text": str(project_rules)})
    for index, text in enumerate(getattr(agent, "_request_system_context", ()) or ()):
        if str(text or "").strip():
            rules.append({"id": f"session-context-{index}", "source": "session",
                          "title": "本轮会话上下文", "text": str(text)[:6000]})
    legacy_block = []
    if legacy_text:
        legacy_block = [{
            "layer_key": "legacy", "title": "项目长期记忆", "text": str(legacy_text),
            "chars": len(str(legacy_text)), "version": 1,
            "hash": hashlib.sha256(str(legacy_text).encode("utf-8")).hexdigest()[:16],
            "item_ids": [str(row.get("id") or "") for row in rows if row.get("id")],
        }]
    agent._last_legacy_injection = legacy_block
    agent._last_memory_snapshot = {
        "session_id": str(getattr(agent, "memory_session_id", None)
                          or getattr(agent, "session_id", "") or ""),
        "captured_at": _now(), "question": str(question or ""),
        "initial_requirement": initial, "legacy_rows": rows,
        "prompt_blocks": blocks + legacy_block, "rules": rules,
    }
    # access_count 的口径是“实际进入提示词”。增强召回已在 recall 中计过一次，
    # 默认召回路径则在这里补齐；GET 检查接口本身始终只读。
    if rows:
        try:
            import agent_memory
            if not agent_memory._enhanced_enabled():
                agent_memory._touch_access(agent.memory_scope,
                                           [row["id"] for row in rows if row.get("id")])
        except Exception:  # noqa: BLE001 —— 观测元数据不得影响问答
            pass


def complete(agent, *, final_text, file_changes, record):
    """保存仅供合规判定的本轮行为；正文仍只留内存，不写入 trace。"""
    agent._last_memory_behavior = {
        "turn_id": str((record or {}).get("turn_id") or ""),
        "final_text": str(final_text or ""),
        "file_changes": [dict(row) for row in (file_changes or []) if isinstance(row, dict)],
        "outcome": str((record or {}).get("outcome") or ""),
    }


def _node(row, store):
    from . import layers

    layer_key = layers.layer_for_store(store)
    fallback = store not in layers.STORE_LAYER
    layer_label = layers.label_for(layer_key)
    if store == "legacy":
        return {
            "id": str(row.get("id") or ""), "store": store,
            "layer_key": layer_key, "layer_label": layer_label, "layer_fallback": fallback,
            "kind": str(row.get("kind") or "fact"),
            "title": str(row.get("title") or "未命名记忆"),
            "content": str(row.get("content") or ""), "evidence": str(row.get("evidence") or ""),
            "source": str(row.get("source") or "agent"), "status": str(row.get("status") or "active"),
            "created_at": str(row.get("updated") or ""), "created_at_semantics": "updated_fallback",
            "updated_at": str(row.get("updated") or ""),
            "importance": float(row.get("importance") if row.get("importance") is not None else 0.5),
            "use_count": int(row.get("access_count") or 0),
            "last_used_at": str(row.get("last_accessed") or ""),
            "agent_id": str(row.get("agent_id") or ""),
            "agent_legacy": bool(int(row.get("agent_legacy") or 0)),
        }
    if store == "hook":
        return {
            "id": str(row.get("id") or ""), "store": store,
            "layer_key": layer_key, "layer_label": layer_label, "layer_fallback": fallback,
            "kind": str(row.get("kind") or "fact"),
            "title": str(row.get("text") or "会话钩子")[:80],
            "content": str(row.get("text") or ""), "evidence": str(row.get("source_turn") or ""),
            "source": "session", "status": "active" if row.get("pinned") else "inactive",
            "created_at": str(row.get("created") or row.get("updated") or ""),
            "created_at_semantics": "created", "updated_at": str(row.get("updated") or ""),
            "importance": 0.5, "importance_estimated": True,
            "use_count": 0, "use_count_semantics": "unavailable",
            "last_used_at": "",
        }
    return {
        "id": str(row.get("id") or ""), "store": store,
        "layer_key": layer_key, "layer_label": layer_label, "layer_fallback": fallback,
        "kind": str(row.get("kind") or "fact"),
        "title": str(row.get("title") or row.get("body") or "共享记忆")[:80],
        "content": str(row.get("body") or ""), "evidence": str(row.get("source_turn") or ""),
        "source": str(row.get("scope") or store), "status": str(row.get("status") or "candidate"),
        "created_at": str(row.get("created") or row.get("updated") or ""),
        "created_at_semantics": "created", "updated_at": str(row.get("updated") or ""),
        "importance": float(row.get("importance") if row.get("importance") is not None else 0.5),
        "use_count": int(row.get("usage") or 0), "last_used_at": str(row.get("last_used") or ""),
    }


def _catalog(agent, project, session):
    import agent_memory
    # 真隔离：legacy 库（项目长期记忆）按 agent_id 过滤；shared/project/hook 是
    # 项目/会话级内容语义层，不属于任何单个 agent，原样列出并在 UI 明确标注。
    owner = str(getattr(agent, "agent_id", "") or "main")
    rows = [_node(row, "legacy") for row in agent_memory.audit(agent.memory_scope, agent_id=owner)]
    try:
        from . import hooks, promote
        rows.extend(_node(row, "shared") for row in promote.shared_items(include_inactive=True))
        rows.extend(_node(row, "project") for row in promote.candidates(project))
        if session:
            rows.extend(_node(row, "hook") for row in hooks.items(project, session))
    except Exception:  # noqa: BLE001 —— v2 未启用/不可读时保留旧库
        pass
    return rows


def _store_for_scope(scope):
    return "shared" if str(scope or "") == "global" else "project"


def _edges(memories):
    """把索引 `edges` 表解析成图上的真实邻接；解析不了就如实报，不补合成边。

    `weight` 缺失原因：索引 schema 的 edges 只有 (src, dst, type)，没有权重列，
    这里返回 null，而不是拿一个好看的数字补上。
    """
    from . import index

    try:
        raw = index.read_edges()
    except Exception as exc:  # noqa: BLE001 —— 索引不可读不得拖垮注入视图
        return [], {"readable": False, "error": str(exc)[:200],
                    "source": "agent_runtime/memory/index.py → edges 表（mode=ro 只读）",
                    "in_table": 0, "resolved": 0, "unresolved": 0,
                    "unresolved_rows": [], "by_type": {}, "weight_available": False}
    known = {(str(node["store"]), str(node["id"])) for node in memories}
    edges, unresolved = [], []
    for row in raw:
        src = (_store_for_scope(row["src_scope"]), row["src"])
        dst = (_store_for_scope(row["dst_scope"]), row["dst"])
        if src not in known or dst not in known:
            unresolved.append({"source": row["src"], "target": row["dst"],
                               "type": row["type"]})
            continue
        edges.append({
            "source": f"{src[0]}:{src[1]}", "target": f"{dst[0]}:{dst[1]}",
            "weight": None, "weight_semantics": "edges 表无权重列，不补数",
            "type": row["type"],
        })
    by_type = {}
    for row in raw:
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
    return edges, {
        "readable": True,
        "source": "agent_runtime/memory/index.py → edges 表（mode=ro 只读）",
        "in_table": len(raw), "resolved": len(edges), "unresolved": len(unresolved),
        "unresolved_rows": unresolved[:20], "by_type": by_type,
        "weight_available": False,
    }


def _fallback_snapshot(agent, session):
    try:
        import agent_trace
        for record in reversed(agent_trace.recent(500)):
            if session and str(record.get("session_id") or "") != session:
                continue
            blocks = [dict(row) for row in (record.get("injection") or []) if isinstance(row, dict)]
            if blocks:
                return {"session_id": session, "captured_at": str(record.get("ts") or ""),
                        "turn_id": str(record.get("turn_id") or ""), "initial_requirement": "",
                        "legacy_rows": [], "prompt_blocks": blocks, "rules": [], "stale": True}
    except Exception:  # noqa: BLE001
        pass
    return None


def _language_check(requirement):
    return bool(re.search(r"(?:用|使用|说|回复|回答)[^。；\n]{0,12}中文", requirement)) and not bool(
        re.search(r"(?:不要|不用|别)[^。；\n]{0,8}(?:用|使用|说|回复|回答)?[^。；\n]{0,8}中文", requirement))


def _readonly_check(requirement):
    return bool(re.search(r"只读|不(?:要|许|得)?(?:修改|改动|写文件)|不要改|不改代码", requirement))


def _compliance(rules, behavior):
    out = []
    for index, rule in enumerate(rules):
        requirement = str(rule.get("text") or "").strip()
        if not requirement:
            continue
        base_id = str(rule.get("id") or f"rule-{index}")
        source = str(rule.get("source") or "unknown")

        def add(status, evidence, suffix=""):
            out.append({"id": base_id + suffix, "requirement": requirement, "source": source,
                        "status": status, "behavior": evidence, "evidence": evidence})

        if behavior is None:
            add("unknown", "没有该回合的回答正文与行为快照，无法判定")
            continue
        checked = False
        if _readonly_check(requirement):
            checked = True
            changes = behavior.get("file_changes") or []
            if changes:
                add("fail", "本轮实际文件变更：" + "、".join(
                    str(row.get("path") or "未命名") for row in changes[:6]), ":readonly")
            else:
                add("pass", "本轮真实文件变更为 0 项", ":readonly")
        if _language_check(requirement):
            checked = True
            answer = str(behavior.get("final_text") or "")
            if answer.strip():
                passed = bool(re.search(r"[\u4e00-\u9fff]", answer))
                add("pass" if passed else "fail",
                    "本轮最终回答含中文" if passed else "本轮最终回答未检测到中文", ":language")
            else:
                add("unknown", "本轮没有可核对的最终回答正文", ":language")
        if not checked:
            add("unknown", "没有可自动核验的行为判据")
    return out


def view(agent, *, project, session, agent_id="main"):
    """只读视图。任何缺失都显式降级，不从库存推测本轮已注入。"""
    from . import layers

    snapshot = getattr(agent, "_last_memory_snapshot", None)
    if not isinstance(snapshot, dict) or str(snapshot.get("session_id") or "") != session:
        snapshot = _fallback_snapshot(agent, session)
    memories = _catalog(agent, project, session)
    blocks = list((snapshot or {}).get("prompt_blocks") or [])
    legacy_rows = {str(row.get("id") or ""): row for row in (snapshot or {}).get("legacy_rows") or []}
    if not legacy_rows:
        for block in blocks:
            if layers.normalize_layer(block.get("layer_key") or block.get("layer")) != "legacy":
                continue
            for mid in block.get("item_ids") or []:
                node = next((row for row in memories if row["store"] == "legacy" and row["id"] == mid), None)
                if node:
                    legacy_rows[str(mid)] = {"content": node["content"]}
    l2_text = "\n".join(str(block.get("text") or "") for block in blocks
                        if layers.normalize_layer(block.get("layer_key") or block.get("layer")) == "shared")
    l4_text = "\n".join(str(block.get("text") or "") for block in blocks
                        if layers.normalize_layer(block.get("layer_key") or block.get("layer")) == "hook")
    injected = []
    for node in memories:
        used = False
        if node["store"] == "legacy" and node["id"] in legacy_rows:
            used = True
            node["injected_content"] = str(legacy_rows[node["id"]].get("content") or "")
        elif node["store"] in {"shared", "project"} and node["content"] and node["content"] in l2_text:
            used = True
            node["injected_content"] = node["content"]
        elif node["store"] == "hook" and node["content"] and node["content"] in l4_text:
            used = True
            node["injected_content"] = node["content"]
        node["used"] = used
        if used:
            injected.append(node)
    rules = list((snapshot or {}).get("rules") or [])
    if not rules and not (snapshot or {}).get("stale"):
        history = getattr(agent, "history", []) or []
        if history:
            rules = [{"id": "session-initial", "source": "user", "title": "会话最初要求",
                      "text": str(history[0].get("user") or "")}]
    behavior = getattr(agent, "_last_memory_behavior", None)
    turn_id = str((snapshot or {}).get("turn_id") or (behavior or {}).get("turn_id") or "")
    if behavior and turn_id and str(behavior.get("turn_id") or "") != turn_id:
        behavior = None
    degraded = []
    if not snapshot:
        degraded.append("当前会话还没有实际注入快照；亮起的节点只会在真实组提示词后出现")
    elif snapshot.get("stale"):
        degraded.append("这是最近一条持久化注入快照；项目/会话守则与回答正文未完整持久化，相关合规项只能标为无法判定")
    edges, edge_stats = _edges(memories)
    return {
        "ok": True, "session_id": session, "agent_id": agent_id,
        "layer_config": layers.config(),
        "hub": {"id": "core", "layer_key": "rule", "layer_label": layers.label_for("rule"),
                "label": layers.label_for("rule"),
                "note": "中心 hub 不进环；亮度顺序依据为内容稳定度（产品口径，待确认）"},
        "edges": edges, "edge_stats": edge_stats,
        "captured_at": str((snapshot or {}).get("captured_at") or ""), "turn_id": turn_id,
        "has_snapshot": bool(snapshot), "stale": bool((snapshot or {}).get("stale")),
        "injected": injected, "prompt_blocks": blocks, "rules": rules, "memories": memories,
        "compliance": _compliance(rules, behavior), "degraded": degraded,
    }
