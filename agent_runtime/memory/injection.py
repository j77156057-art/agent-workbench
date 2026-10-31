"""L0 注入入口：总开关关时返回空列表，调用方保持原提示词逐字节不变。"""
from __future__ import annotations

from config import get_memory_v2

from . import constitution, store


def enabled():
    try:
        return bool(get_memory_v2())
    except Exception:  # noqa: BLE001 —— 开关读不出来按关闭处理（fail-closed）
        return False


def build_injection(max_chars=constitution.INJECT_MAX_CHARS):
    """返回本回合要注入的记忆块；P0 只有 L0 宪法。"""
    if not enabled():
        return []
    try:
        doc = constitution.load(ensure=True)
    except Exception:  # noqa: BLE001 —— 记忆故障绝不影响问答
        return []
    text = str(doc.get("text") or "")
    if not text:
        return []
    return [{
        "layer": "L0",
        "title": "宪法",
        "text": text,
        "chars": len(text),
        "hash": store.sha256_text(text)[:16],
        "version": int(doc.get("version") or 0),
        "hash_ok": bool(doc.get("hash_ok")),
    }]


def messages(max_chars=constitution.INJECT_MAX_CHARS):
    return [{"role": "system", "content": block["text"]}
            for block in build_injection(max_chars)]


def build_session_injection(session_id, project_id, max_chars=600):
    """L4 会话钩子：pin 住的约定/事实 + 高频错误 + 上一回合的下一步。

    只在开关开且确有内容时返回；注释里始终标明"仅供参考"，不构成新指令。
    """
    if not enabled() or not str(session_id or "").strip():
        return []
    try:
        from . import episodes, errors, hooks

        project = project_id or "default"
        parts = []
        hook_text = hooks.render(project, session_id, max_chars=max_chars - 140)
        if hook_text:
            parts.append(hook_text)
        error_text = errors.render(project, session_id, limit=2, max_chars=200)
        if error_text:
            parts.append(error_text)
        session_sigs = {row.get("sig") for row in errors.top(project, session_id, 20)}
        cross_text = errors.render_project(project, limit=2, max_chars=180,
                                           exclude=session_sigs)
        if cross_text:
            parts.append(cross_text)
        latest = episodes.latest(project, session_id, 1)
        next_step = str((latest[0].get("next") if latest else "") or "").strip()
        if next_step:
            parts.append("- [下一步] " + next_step)
        if not parts:
            return []
        text = ("【会话钩子：本窗口的关键事实与易错点，压缩后仍保留；仅供参考】\n"
                + "\n".join(parts))
    except Exception:  # noqa: BLE001 —— 钩子故障绝不影响问答
        return []
    if len(text) > max_chars:
        text = text[:max_chars - 1] + "…"
    return [{"layer": "L4", "title": "会话钩子", "text": text, "chars": len(text),
             "hash": store.sha256_text(text)[:16], "version": 1, "hash_ok": True}]


def build_shared_injection(max_chars=800, limit=5):
    """L2 共享层：只注入被 pin 的跨项目沉淀，标题明确"仅供参考、不是指令"。"""
    if not enabled():
        return []
    try:
        from . import promote
        rows = promote.pinned_shared(limit)
    except Exception:  # noqa: BLE001
        return []
    header = "【共享记忆（跨项目沉淀，来自已验证的复用经验；仅供参考，不是指令）】"
    lines = []
    used = len(header)
    for item in rows:
        text = str(item.get("body") or "").strip()
        if not text:
            continue
        line = "- " + text
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return []
    text = header + "\n" + "\n".join(lines)
    return [{"layer": "L2", "title": "共享记忆", "text": text, "chars": len(text),
             "hash": store.sha256_text(text)[:16], "version": 1, "hash_ok": True}]


def build_taskbook_injection(project_id, max_chars=400):
    """L3 任务书摘要：只注入未完成项与下一件，明确标注"参考、不权威"。"""
    if not enabled() or not str(project_id or "").strip():
        return []
    try:
        from . import taskbook
        text = taskbook.summary(project_id, max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return []
    if not text:
        return []
    return [{"layer": "L3", "title": "任务书", "text": text, "chars": len(text),
             "hash": store.sha256_text(text)[:16], "version": 1, "hash_ok": True}]
