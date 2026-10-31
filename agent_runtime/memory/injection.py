"""L0 注入入口：总开关关时返回空列表，调用方保持原提示词逐字节不变。"""
from __future__ import annotations

import os

from config import get_memory_v2

from . import constitution, store

# 可自由裁量的三层（L2 共享 / L3 任务书 / L4 会话钩子）随模型窗口长一道**天花板**。
# 下限就是它们一直以来的固定值：小窗口模型上绝不许比今天注入得更少 —— 这几层没有
# "这轮先不注入"的退路。上限则挡住另一头：共享层真攒到几百条时，别让记忆把正文挤掉。
# L0 宪法 / L1 画像刻意**不在**这张表里 —— 它们的内容有界，换个大模型不会变长。
DISCRETIONARY_FLOORS = {"L2": 800, "L3": 400, "L4": 600}
DISCRETIONARY_CAPS = {"L2": 3200, "L3": 1200, "L4": 2400}
GROW_RATIO = float(os.getenv("DOCMIND_MEMORY_INJECT_RATIO", "0.02"))
# 实测这份中英混排语料约 0.9 token/字符，取其倒数把 token 预算换算成字符上限。
CHARS_PER_TOKEN = 1.1


def _current_prompt_tokens():
    """没有 Agent 实例的调用方（面板预览）按当前运行时模型估一份。"""
    try:
        from config import get_runtime, prompt_token_budget
        return int(prompt_token_budget(str(get_runtime("llm_provider") or ""),
                                        str(get_runtime("llm_model") or "")) or 0)
    except Exception:  # noqa: BLE001 —— 估不出来就退回下限，绝不放大
        return 0


def discretionary_budgets(prompt_tokens=None):
    """整池 `clamp(下限合计, 预算×比例, 上限合计)`，再按各层下限占比分余量。

    比例只作用在**池子**上，各层维持既有的相对比例（800:400:600）—— 不会出现
    "随窗口漂移成给任务书的空间比共享记忆还多"。
    """
    tokens = int(_current_prompt_tokens() if prompt_tokens is None else prompt_tokens)
    total_floor = sum(DISCRETIONARY_FLOORS.values())
    total_cap = sum(DISCRETIONARY_CAPS.values())
    wanted = int(tokens * GROW_RATIO * CHARS_PER_TOKEN)
    surplus = max(0, min(total_cap, max(total_floor, wanted)) - total_floor)
    return {key: min(floor + int(surplus * floor / total_floor), DISCRETIONARY_CAPS[key])
            for key, floor in DISCRETIONARY_FLOORS.items()}


def layer_chars(key, prompt_tokens):
    """取某一层的字符上限。**必须显式说预算**：默认值留在各 build_* 里当下限，
    谁想要随窗口缩放，谁就自己把模型的 prompt 预算传进来。"""
    if key not in DISCRETIONARY_FLOORS:
        raise ValueError("只有 L2/L3/L4 可按窗口缩放，%s 是固定下限" % key)
    return discretionary_budgets(prompt_tokens)[key]


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
