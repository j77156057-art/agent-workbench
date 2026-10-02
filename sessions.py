"""会话隔离 + 持久化 + 滚动摘要。

把 Agent 的对话记忆从"进程内单例的一个 list"提升为**按 session_id 落盘**的会话：
每个会话一个 JSON 文件，保存最近若干轮问答 + 一段"早期对话摘要"。

- 隔离：不同 session_id 互不可见，解决 api.py 复用同一 Agent 导致历史串台的问题。
- 持久化：重启服务后同一 session_id 仍能续聊。
- 摘要压缩：轮数/字符超阈值时，把最早的若干轮交给 LLM 压缩成一段摘要，
  只保留最近 KEEP 轮原文 —— 长会话不会把上下文预算吃光。

存储：写只写 <STATE_ROOT>/.docmind_sessions/<project_id>/<slug>.json（P2 起按项目分桶；
项目功能关闭或无当前项目时写旧的 .docmind_sessions/<slug>.json 扁平路径）。
**读穿透**：读取时并上旧扁平目录的同名文件，使迁移前的历史会话不丢失（不搬文件）。
只存用户问题与模型回答，非敏感配置另存。写盘失败一律静默降级，不影响主流程。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from config import STATE_ROOT, state_path
import projects

SESSIONS_DIR = state_path("DOCMIND_SESSIONS_DIR", os.path.join(STATE_ROOT, ".docmind_sessions"))
# 压缩以 token 占用为准（阈值由 Agent 按模型真实窗口换算后传入），轮数只留两道
# 硬保险：轮数上限防失控；保留轮数下限保证最近的上下文不被摘要掉。
MAX_TURNS = int(os.getenv("DOCMIND_SESSION_MAX_TURNS", "200"))       # 轮数硬上限（超过必压缩）
KEEP_TURNS_MIN = int(os.getenv("DOCMIND_SESSION_KEEP_TURNS_MIN", "2"))  # 压缩至少保留的原文轮数
# 直接调用 maybe_compact 且未传 token 阈值时的兜底（约等于旧 8000 字符的保守口径）
DEFAULT_TRIGGER_TOKENS = int(os.getenv("DOCMIND_COMPACT_TRIGGER_TOKENS", "7000"))
DEFAULT_KEEP_TOKENS = int(os.getenv("DOCMIND_COMPACT_KEEP_TOKENS", "3500"))

_lock = threading.Lock()

# 早期 API 版本曾把路由上下文直接拼在用户问题前，并写入会话历史。
# 读取时只清理这种“从开头开始的完整旧前缀”，不改动用户正文中的普通提及。
_LEGACY_PROMPT_PREFIX = re.compile(r"^\s*【系统提示】.*?用户问题：\s*", re.S)


def _clean_legacy_prompt(value) -> str:
    text = str(value or "")
    match = _LEGACY_PROMPT_PREFIX.match(text)
    return text[match.end():].lstrip() if match else text


def _slug(session_id) -> str:
    """把任意 session_id 归一成安全文件名（防目录穿越 / 非法字符）。"""
    s = re.sub(r"[^0-9A-Za-z_.-]", "_", str(session_id or "default"))
    s = s.strip("._") or "default"
    return s[:64]


def _bucket_dir(project_id=None) -> str:
    """会话落盘目录：项目功能开启且能确定项目时用 `<SESSIONS_DIR>/<pid>/`，否则用旧的扁平目录。

    - 功能关闭 → 旧扁平目录（`DOCMIND_PROJECTS=0` 一键回滚，行为与今日完全一致）；
    - 未显式传 project_id → 取当前项目；无当前项目（未登记/legacy）→ 旧扁平目录，
      以保持「项目功能刚开启、还没切过项目」时对既有会话的向后兼容。
    """
    if not projects.enabled():
        return SESSIONS_DIR
    pid = project_id if project_id is not None else projects.current_project_id()
    if not pid or pid == projects.LEGACY_ID:
        return SESSIONS_DIR
    return os.path.join(SESSIONS_DIR, _slug(pid))


def _path(session_id, project_id=None) -> str:
    return os.path.join(_bucket_dir(project_id), _slug(session_id) + ".json")


def _legacy_dir() -> str:
    """旧的扁平会话目录（P2 之前所有会话都在这里）。"""
    return SESSIONS_DIR


def _legacy_path(session_id) -> str:
    return os.path.join(_legacy_dir(), _slug(session_id) + ".json")


def _read_raw(path):
    """读一个会话 JSON 文件；缺失/损坏/非 dict 返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _apply_raw(data: dict, raw) -> dict:
    """把磁盘记录填进会话结构（raw 为 None → 保持空结构）。"""
    if isinstance(raw, dict):
        turns = raw.get("turns")
        data["summary"] = raw.get("summary") or ""
        data["turns"] = [
            {
                **t,
                "user": _clean_legacy_prompt(t.get("user", "")),
                "assistant": _clean_legacy_prompt(t.get("assistant", "")),
            }
            for t in turns if isinstance(t, dict)
        ] if isinstance(turns, list) else []
        data["updated_at"] = raw.get("updated_at") or ""
        # 快照必须一起搬进来。这里漏掉它时症状特别隐蔽：snapshot() 写入成功、
        # 没有任何报错，但下一次 load 读回来的 data 里没有 snapshots，
        # 于是 restore_snapshot 永远说「还没有任何快照」——写得进、读不回来。
        snaps = raw.get("snapshots")
        data["snapshots"] = [s for s in snaps if isinstance(s, dict)] \
            if isinstance(snaps, list) else []
    return data


def _dirs_for_read(bucket: str) -> list:
    """读取时要覆盖的目录：项目桶优先，其次旧扁平目录（二者相同则只一个，避免重复计数）。"""
    legacy = _legacy_dir()
    return [bucket] if bucket == legacy else [bucket, legacy]


def _default() -> dict:
    return {"session_id": "", "updated_at": "", "summary": "", "turns": []}


def load(session_id, project_id=None) -> dict:
    """读取一个会话记录；缺失/损坏返回空结构（调用方无需处理异常）。

    P2 读穿透：先读项目桶；桶内没有该文件、或 turns 为空时，回退读**旧扁平目录**的同名
    文件（既有安装在迁移后仍能读到旧历史会话——不搬文件、零数据风险）。
    """
    data = _default()
    data["session_id"] = str(session_id or "default")
    p = _path(session_id, project_id)
    raw = _read_raw(p)
    if isinstance(raw, dict) and raw.get("turns"):
        return _apply_raw(data, raw)
    lp = _legacy_path(session_id)
    if lp != p:
        raw2 = _read_raw(lp)
        if isinstance(raw2, dict) and raw2.get("turns"):
            return _apply_raw(data, raw2)
        if raw is None:
            raw = raw2
    return _apply_raw(data, raw)


def _write(session_id, data, project_id=None) -> bool:
    try:
        d = _bucket_dir(project_id)
        os.makedirs(d, exist_ok=True)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        p = os.path.join(d, _slug(session_id) + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def history(session_id, project_id=None):
    """返回给 Agent 回放的 turns 列表（list[{user, assistant}]）。"""
    return [{"user": t.get("user", ""), "assistant": t.get("assistant", ""),
             **({"resume_context": str(t["resume_context"])[-6000:]} if "resume_context" in t else {}),
             **{key: str(t[key])[:1200] for key in ("checkpoint_id", "failure_reason") if t.get(key)}}
            for t in load(session_id, project_id).get("turns", []) if isinstance(t, dict)]


def summary_text(session_id, project_id=None) -> str:
    return load(session_id, project_id).get("summary", "") or ""


def save(session_id, turns, summary=None, project_id=None) -> bool:
    """整体落盘一个会话（turns = [{user, assistant}]）。"""
    with _lock:
        data = load(session_id, project_id)
        data["session_id"] = str(session_id or "default")
        if summary is not None:
            data["summary"] = summary
        data["turns"] = [
            {"user": t.get("user", ""), "assistant": t.get("assistant", ""),
             "ts": t.get("ts") or datetime.now().isoformat(timespec="seconds"),
             **({"resume_context": str(t["resume_context"])[-6000:]} if "resume_context" in t else {}),
             **{key: str(t[key])[:1200] for key in ("checkpoint_id", "failure_reason") if t.get(key)}}
            for t in (turns or []) if isinstance(t, dict)
        ]
        return _write(session_id, data, project_id)


def _est_tokens(text: str) -> int:
    """无 LLM 客户端时的保守 token 估算：CJK 约 1.1 token/字，其余约 3.2 字符/token。

    与 llm.LLMClient._heuristic_tokens 同口径，但不含每条 +40 的固定底数
    （这里是整段历史一次计数，加底数会严重高估短历史）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return int(cjk * 1.1 + other / 3.2) + 1


def _turns_tokens(turns, counter) -> int:
    text = "\n".join(
        (t.get("user", "") or "") + "\n" + (t.get("assistant", "") or "")
        + ("\n" + str(t["resume_context"]) if t.get("resume_context") else "")
        for t in (turns or []) if isinstance(t, dict)
    )
    return int(counter(text) or 0)


_SUMMARY_PROMPT = (
    "你是对话压缩器。把下面这段【早期对话】压缩成不超过 300 字的中文要点，"
    "务必保留：已确认的结论、涉及的文件路径与行号、用户的偏好与约束、尚未解决的问题。"
    "不要复述寒暄，不要编造未出现的信息。只输出要点本身。\n\n早期对话：\n"
)


def _naive_summary(turns) -> str:
    """LLM 不可用时的确定性兜底：抽取每轮问题首句拼成要点。"""
    bits = []
    for t in turns:
        q = (t.get("user") or "").strip().replace("\n", " ")[:60]
        if q:
            bits.append("· " + q)
    return "（早期对话摘要·本地兜底）\n" + "\n".join(bits)


def _llm_summary(turns, llm) -> str:
    if llm is None:
        return _naive_summary(turns)
    convo = "\n".join(
        f"用户：{(t.get('user') or '')[:400]}\n助手：{(t.get('assistant') or '')[:400]}"
        for t in turns
    )
    try:
        out = llm.chat(
            [{"role": "user", "content": _SUMMARY_PROMPT + convo}],
            stream=False, temperature=0.2,
        )
        out = (out or "").strip()
        return out or _naive_summary(turns)
    except Exception:  # noqa: BLE001 —— 压缩失败绝不能打断正常问答
        return _naive_summary(turns)


def maybe_compact(session_id, turns, llm=None, trigger_tokens=None, keep_tokens=None,
                  project_id=None):
    """历史 token 占用超过模型预算阈值时，把最早的若干轮压缩进 summary。

    - trigger_tokens：历史总 token 达到该值即压缩（由 Agent 按模型真实窗口 ×
      COMPACT_TRIGGER_RATIO 换算；1M 窗口模型与 16k 本地模型用各自的阈值）；
    - keep_tokens：压缩后最近原文轮次的 token 目标体积（× COMPACT_KEEP_RATIO），
      至少保留 KEEP_TURNS_MIN 轮；
    - 轮数超过 MAX_TURNS 硬上限也必压缩（与 token 阈值是「或」关系）；
    - project_id：摘要读写的项目桶（P3）；缺省 None = 「当前项目」，行为与改动前一致。
    返回 (turns, summary)；未触发压缩时原样返回。
    """
    turns = list(turns or [])
    trigger = int(trigger_tokens or DEFAULT_TRIGGER_TOKENS)
    keep = int(keep_tokens or DEFAULT_KEEP_TOKENS)
    counter = getattr(llm, "count_tokens", None) or _est_tokens

    over_tokens = _turns_tokens(turns, counter) > trigger
    over_turns = len(turns) > MAX_TURNS
    if not over_tokens and not over_turns:
        return turns, summary_text(session_id, project_id)

    # 从最旧轮次开始弹出，直到最近轮次装进 keep_tokens（轮数硬上限同时收敛），
    # 但给最近 KEEP_TURNS_MIN 轮免死金牌——再超也由每轮 _fit_budget 裁剪兜底。
    kept = list(turns)
    while (
        len(kept) > KEEP_TURNS_MIN
        and (len(kept) > MAX_TURNS or _turns_tokens(kept, counter) > keep)
    ):
        kept.pop(0)
    if len(kept) >= len(turns):
        return turns, summary_text(session_id, project_id)

    old = turns[: len(turns) - len(kept)]
    prev = summary_text(session_id, project_id)
    new_sum = _llm_summary(old, llm)
    summary = (prev + "\n" + new_sum).strip() if prev else new_sum
    return kept, summary


# ---------------------------------------------------------------------------
# 轮次快照（rewind / fork 的地基）
# ---------------------------------------------------------------------------
# 为什么需要：会话历史是**一条不断被覆写**的链——每轮问答都 save 一次，
# 只留最新状态。于是"回到第 3 轮看看当时是什么样"这件事**根本做不到**，
# 因为那个状态已经被覆盖掉了。这跟竞品（Claude Code 的 checkpoints、
# Codex 的 worktree + resume）在能力上差一档。
#
# 快照记在**同一份会话文件**里而不是新开文件：rewind 必须和会话同生共死，
# 否则会出现"会话删了、快照还在"的孤儿状态。代价是会话文件变大，
# 所以 SNAPSHOT_LIMIT 保留最近若干条。
SNAPSHOT_LIMIT = 50


def snapshot(session_id, turns, *, label="", summary="", project_id=None) -> bool:
    """在当前轮次落一个快照，供之后 rewind 回来。

    快照存的是**当刻的 turns 副本**，不是引用——否则后续 save 会把它一起改掉，
    那就不是快照而是别名。
    """
    turns = list(turns or [])
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list):
        snaps = []
    snaps.append({
        "seq": len(snaps) + 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "label": str(label or "")[:200],
        "turn_count": len(turns),
        "turns": turns,
        "summary": str(summary or data.get("summary") or ""),
    })
    data["snapshots"] = snaps[-SNAPSHOT_LIMIT:]
    return _write(session_id, data, project_id)


def snapshots(session_id, project_id=None) -> list:
    """列出快照（元信息 + 该轮次的问答摘要，不带完整 turns 以免响应过大）。"""
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list):
        return []
    out = []
    for item in snaps:
        if not isinstance(item, dict):
            continue
        turns = item.get("turns") or []
        last_user = ""
        for turn in reversed(turns):
            if isinstance(turn, dict) and str(turn.get("user") or "").strip():
                last_user = str(turn.get("user") or "").strip()
                break
        out.append({
            "seq": item.get("seq"),
            "at": item.get("at") or "",
            "label": item.get("label") or "",
            "turn_count": item.get("turn_count", len(turns)),
            "question": last_user[:200],
        })
    return out


def restore_snapshot(session_id, seq, *, project_id=None) -> dict:
    """回到第 seq 个快照那一刻的状态（截断掉之后的轮次）。

    返回 `{"ok": bool, "turns": [...], "summary": str, "reason": str}`。
    找不到时不抛异常——调用方据此给用户一句能读懂的说明，而不是 500。
    """
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        return {"ok": False, "turns": [], "summary": "",
                "reason": "这个会话还没有任何快照，没法回退。"}
    target = None
    for item in snaps:
        if isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0):
            target = item
            break
    if target is None:
        return {"ok": False, "turns": [], "summary": "",
                "reason": "找不到 seq=%s 的快照（当前保留 %d 条）。" % (seq, len(snaps))}
    turns = [dict(t) for t in (target.get("turns") or []) if isinstance(t, dict)]
    summary = str(target.get("summary") or "")
    data["turns"] = turns
    data["summary"] = summary
    # 被回退掉的快照一并丢弃：它们的 seq 已经对应不上了，留着会误导。
    data["snapshots"] = [i for i in snaps
                         if isinstance(i, dict) and int(i.get("seq") or 0) <= int(seq)]
    _write(session_id, data, project_id)
    return {"ok": True, "turns": turns, "summary": summary,
            "reason": "已回到第 %s 个快照（%s 轮）。" % (seq, len(turns))}


def fork_session(session_id, new_id, *, at_seq=None, project_id=None) -> dict:
    """从某个快照分叉出一条新会话；不动原会话。

    竞品对照：Codex 用 git worktree 隔离并行任务，Claude Code 用 checkpoints。
    这里走的是"新会话 id + 复制 turns"的路子——它不需要 git，因此也不受
    「当前不是 git 工作树」的限制（handoff 工具在那种场景下只能明说做不到）。
    """
    new_id = str(new_id or "").strip()
    if not new_id:
        return {"ok": False, "reason": "新会话 id 不能为空。"}
    data = load(session_id, project_id=project_id)
    if not data.get("turns") and not data.get("snapshots"):
        return {"ok": False, "reason": "原会话没有可分叉的历史。"}
    turns, summary = list(data.get("turns") or []), data.get("summary") or ""
    if at_seq is not None:
        got = restore_snapshot_snapshot_only(session_id, at_seq, project_id=project_id)
        if not got.get("ok"):
            return got
        turns, summary = got["turns"], got["summary"]
    data["session_id"] = new_id
    data["turns"] = turns
    data["summary"] = summary
    # 分叉出来的新会话**不继承快照序列**：它的快照语义与原会话不同，
    # 混用会让 restore 回到不属于这条链的状态。
    data["snapshots"] = []
    saved = _write(new_id, data, project_id)
    return {"ok": bool(saved), "reason": "已分叉为新会话 %s。" % new_id if saved
            else "分叉失败：新会话写盘未成功。", "session_id": new_id,
            "turns": turns}


def restore_snapshot_snapshot_only(session_id, seq, *, project_id=None) -> dict:
    """只算不写：给 fork 用，避免分叉顺带改掉原会话的快照链。"""
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        return {"ok": False, "turns": [], "summary": "", "reason": "没有可用快照。"}
    for item in snaps:
        if isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0):
            return {"ok": True,
                    "turns": [dict(t) for t in (item.get("turns") or [])
                              if isinstance(t, dict)],
                    "summary": str(item.get("summary") or ""), "reason": ""}
    return {"ok": False, "turns": [], "summary": "",
            "reason": "找不到 seq=%s 的快照。" % seq}


def list_sessions(limit=50, project_id=None):
    """列出会话摘要（用于 /api/sessions 概览）。

    读取范围 = 「当前项目桶 ∪ 旧扁平目录」：同一 session_id 两处都有时**项目桶优先**、
    稳定去重（不因两处内容不同而抖动）；再按 updated_at 倒序取前 limit。
    """
    bucket = _bucket_dir(project_id)
    seen = {}
    for d in _dirs_for_read(bucket):
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = _read_raw(os.path.join(d, name))
            if not isinstance(raw, dict):
                continue
            sid = raw.get("session_id") or name[:-5]
            if sid in seen:
                continue  # 项目桶先遍历 → 桶条目优先
            turns = raw.get("turns") or []
            first_user = ""
            for turn in turns:
                if isinstance(turn, dict) and str(turn.get("user") or "").strip():
                    first_user = _clean_legacy_prompt(turn.get("user", "")).strip()
                    break
            preview = " ".join(first_user.split())
            seen[sid] = {
                "session_id": sid,
                "turns": len(turns),
                "has_summary": bool(raw.get("summary")),
                "updated_at": raw.get("updated_at") or "",
                # 首条用户问题作为可读标题；不保存额外数据，兼容旧会话文件。
                "title": preview[:72] or "未命名对话",
                "preview": preview[:180],
            }
    rows = list(seen.values())
    rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return rows[:max(1, int(limit))]


def delete(session_id, project_id=None) -> bool:
    """删除会话：项目桶与其旧扁平目录里的同名文件一并删除（对老会话也生效）。"""
    ok = True
    for p in {_path(session_id, project_id), _legacy_path(session_id)}:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            ok = False
    return ok
