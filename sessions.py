"""会话隔离 + 持久化 + 滚动摘要。

把 Agent 的对话记忆从"进程内单例的一个 list"提升为**按 session_id 落盘**的会话：
每个会话一个 JSON 文件，保存最近若干轮问答 + 一段"早期对话摘要"。

- 隔离：不同 session_id 互不可见，解决 api.py 复用同一 Agent 导致历史串台的问题。
- 持久化：重启服务后同一 session_id 仍能续聊。
- 摘要压缩：轮数/字符超阈值时，把最早的若干轮交给 LLM 压缩成一段摘要，
  只保留最近 KEEP 轮原文 —— 长会话不会把上下文预算吃光。

存储：一个 session_id 就是**一个会话**，全局唯一，落在
`<STATE_ROOT>/.docmind_sessions/<slug>.json`。

## 为什么从「按项目分桶」改成全局（2026-10-10，乙方案）

P2 起会话按 `<project_id>/<slug>.json` 分桶。实测（带新代码的隔离实例）：
同一个 session_id 在项目 A 下问一次、切到项目 B 再问一次，
两次分别绑定 A 和 B —— **原缺陷「切项目顶掉会话的库」换个形式重演**。

根因：`_agent_key` 用 `pid::sid` 把 **Agent 实例**按项目分开了，而
`session_id` 本身还是同一个字符串。「同一个会话跨项目」这件事
在「按项目分桶」的磁盘上**根本不存在** —— B 项目读不到 A 桶的文件，
于是当成新会话重新绑。

会话级代码库隔离的全部前提就是「**这个会话一直用它绑过的那个库**」。
分桶让这个前提不成立，所以分桶必须去掉。

**读穿透**：旧安装的分桶文件仍在 `<SESSIONS_DIR>/<pid>/<slug>.json`。
读取时并上这些历史文件，不搬文件、零数据风险；首次写盘即落到全局路径，
自然完成迁移。

同名冲突：两个项目桶里有同一 slug 时，`code_root` 取**最早绑定的那份**
（`bind_code_root` 起会写 `code_root_at` 时间戳；老记录没有该字段时
回退按 `updated_at` 最早）。turns / summary / snapshots 取 `updated_at`
最新的那份。合并只发生在迁移那一次，之后全局只有一份。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime

from config import (STATE_ROOT, COMPACT_KEEP_RATIO, PROMPT_TOKEN_BUDGET,
                    state_path)
import projects
from file_locks import cross_process_lock

logger = logging.getLogger(__name__)

SESSIONS_DIR = state_path("DOCMIND_SESSIONS_DIR", os.path.join(STATE_ROOT, ".docmind_sessions"))

# pinned（固定事实）的审计与墓碑限制：审计只保留最近 N 条，避免会话文件无限膨胀。
_PINNED_AUDIT_LIMIT = 500

# 子 agent 的会话也用同一个落盘路径（不另造存储），但 id 统一加前缀：
# - 主列表默认过滤掉，避免一次委派在「会话历史」里塞进一堆子会话；
# - 卡片的独立对话窗口仍可按 id 直接 load。
SUBAGENT_SESSION_PREFIX = "sub-"


def subagent_session_id(agent_id: str) -> str:
    """委派 ticket → 真会话 id（确定可推导，重启后仍能找回）。"""
    key = str(agent_id or "").strip()
    if not key:
        key = "anon-" + uuid.uuid4().hex[:12]
    return SUBAGENT_SESSION_PREFIX + key


def is_subagent_session(session_id: str) -> bool:
    return str(session_id or "").startswith(SUBAGENT_SESSION_PREFIX)


# 逐轮「推理轨迹」旁路存储：每会话一个 JSON 文件，键为 turn_id，值为该轮完整步骤
# （thought/action/observation/reflection 的正文，不截断）。刷新页面 / 重开工作台时，
# /api/sessions 按 turn_id 把 trace 合回对应轮，前端据此重建「推理过程」步骤流。
# 刻意与会话 JSON 分离：正文可能很大且含工具读到的文件内容，绝不能进 LLM 上下文，
# 也不该被每轮都 load 的会话文件反复读写。
TURN_TRACE_DIR = state_path("DOCMIND_TURN_TRACE_DIR", os.path.join(STATE_ROOT, ".docmind_turn_traces"))
TURN_TRACE_MAX_TURNS = int(os.getenv("DOCMIND_TURN_TRACE_MAX_TURNS", "200"))  # 每会话最多留存多少轮的轨迹
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


def _bucket_dirs() -> list:
    """会话存储目录：**全局扁平目录**（唯一写入目标）。

    返回单元素列表，只为让「读穿透」的调用方保持统一形状。
    `project_id` 一律忽略 —— 一个 session_id 就是一个会话，与项目无关。
    """
    return [SESSIONS_DIR]


def _legacy_bucket_dirs() -> list:
    """旧的按项目分桶目录（P2 期间写入的 `<SESSIONS_DIR>/<pid>/`）。

    只用于**读穿透与清理**，不再写入。这些子目录里只会有 `.json`
    （分桶目录从不含子目录），所以只扫一层、且只收目录。
    """
    out = []
    try:
        names = sorted(os.listdir(SESSIONS_DIR))
    except OSError:
        return out
    for name in names:
        sub = os.path.join(SESSIONS_DIR, name)
        if os.path.isdir(sub):
            out.append(sub)
    return out


def _path(session_id, project_id=None) -> str:
    """会话文件的**唯一**路径（全局扁平；project_id 仅为签名兼容而保留）。"""
    return os.path.join(SESSIONS_DIR, _slug(session_id) + ".json")


def _legacy_dir() -> str:
    """旧的扁平会话目录（= 现在的全局目录；留名供历史调用方）。"""
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


def _pinned_text(entries) -> str:
    """结构化 pinned 条目 → 渲染用正文（条目之间空行分隔）。"""
    out = []
    for item in entries or []:
        text = str(item.get("text") if isinstance(item, dict) else item or "").strip()
        if text:
            out.append(text)
    return "\n\n".join(out)


def _pin_fingerprint(text) -> str:
    """pinned 规范化指纹：NFKC（全半角统一）+ 去空白 + 去标点。

    只做「同一条事实的排版归一」，不做语义近似：改一个词就会得到不同指纹。
    代价是措辞改写的同一条事实仍可能被重复追加（见报告）；收益是不会误杀
    用户没有删除过的其他条目。阈值等价于归一化后完全相等（1.0）。
    """
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[^\w]+", "", value, flags=re.UNICODE)
    return value


def _pin_id(text) -> str:
    fp = _pin_fingerprint(text)
    if not fp:
        return ""
    return "pin-" + hashlib.sha1(fp.encode("utf-8")).hexdigest()[:16]


def _pinned_entry(text, source="", at="", *, actor="", entry_id="", added_at=None) -> dict:
    """pinned 条目：正文 + 来源 + 首次写入时间 + id 指纹 + actor。"""
    text = str(text or "").strip()
    added = str(added_at if added_at is not None else at or "")
    return {"id": str(entry_id or "") or _pin_id(text),
            "text": text,
            "source": str(source or ""),
            "added_at": added,
            "actor": str(actor or "")}


def _normalize_pinned_item(item, *, default_actor="migration") -> dict:
    """把磁盘/入参里的 pinned 条目归一成五字段；兼容旧字段 `at`。"""
    if isinstance(item, dict):
        return _pinned_entry(
            item.get("text"), item.get("source"),
            item.get("added_at") or item.get("at") or "",
            actor=item.get("actor") or default_actor,
            entry_id=item.get("id") or "",
        )
    return _pinned_entry(item, actor=default_actor)


def _normalize_tombstones(rows) -> list:
    """墓碑：按规范化指纹记录被用户删除的条目，用于阻断自动回流。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        fingerprint = str(row.get("fingerprint") or "") or _pin_fingerprint(row.get("text"))
        if not fingerprint:
            continue
        out.append({
            "fingerprint": fingerprint,
            "id": str(row.get("id") or ""),
            "text": str(row.get("text") or ""),
            "deleted_at": str(row.get("deleted_at") or row.get("at") or ""),
            "deleted_by": str(row.get("deleted_by") or "user"),
        })
    return out


def _pinned_audit(data, action, *, actor, entry_id="", origin="", text="") -> None:
    """每次增/删追加一行可查记录；独立字段落盘，不新增事件表（R10 白名单不动）。"""
    rows = data.get("pinned_audit")
    if not isinstance(rows, list):
        rows = []
    rows.append({
        "action": str(action),
        "actor": str(actor or ""),
        "id": str(entry_id or ""),
        "at": datetime.now().isoformat(timespec="seconds"),
        "origin": str(origin or ""),
        "chars": len(str(text or "")),
    })
    data["pinned_audit"] = rows[-_PINNED_AUDIT_LIMIT:]


def _merge_pinned_entries(existing, incoming, *, actor="model", origin="",
                          tombstones=()) -> tuple:
    """pinned 唯一合并语义：只增不减、旧条目逐字节保留。

    - 已有 id 直接跳过，绝不改写其 text/source/added_at/actor；
    - 非用户来源收到更短入参（说明上游丢了条目）→ 整批忽略并上报；
    - 非用户来源命中墓碑指纹 → 丢弃并上报，调用方记 pinned_readd_suppressed。
    """
    actor = str(actor or "model")
    existing_norm = [_normalize_pinned_item(e) for e in (existing or [])]
    incoming_norm = [_normalize_pinned_item(e, default_actor="")
                     for e in (incoming or [])]
    report = {"added": [], "suppressed": [], "shrink_rejected": False,
              "before": len(existing_norm), "incoming": len(incoming_norm)}
    if actor != "user" and len(incoming_norm) < len(existing_norm):
        report["shrink_rejected"] = True
        report["reason"] = "pinned_shrink_rejected"
        report["total"] = len(existing_norm)
        return existing_norm, report

    tomb_fps = {t["fingerprint"] for t in _normalize_tombstones(tombstones)}
    merged = list(existing_norm)
    seen = {e["id"] for e in merged if e["id"]}
    now = datetime.now().isoformat(timespec="seconds")
    for item in incoming_norm:
        text = item["text"]
        if not text:
            continue
        entry_id = item["id"] or _pin_id(text)
        if entry_id in seen:
            continue
        fingerprint = _pin_fingerprint(text)
        if not fingerprint:
            continue
        if actor != "user" and fingerprint in tomb_fps:
            report["suppressed"].append({"id": entry_id, "text": text,
                                         "fingerprint": fingerprint})
            continue
        source = item["source"] or origin
        added_at = item["added_at"]
        # must-keep 条目的正文自带来源/时间标记，首次入库时解析出来。
        if text.startswith("【must-keep"):
            match = re.search(r"source=([^｜\]]+)", text)
            if match and not source:
                source = match.group(1).strip()
            match = re.search(r"at=([0-9T:\-\.\+]+)", text)
            if match and not added_at:
                added_at = match.group(1)
        entry = {"id": entry_id, "text": text,
                 "source": source or "auto:summary",
                 "added_at": added_at or now,
                 "actor": item["actor"] or actor}
        merged.append(entry)
        seen.add(entry_id)
        report["added"].append(entry)
    report["total"] = len(merged)
    return merged, report


def append_pinned(entries, actor="model", origin="", *, session_id=None,
                  project_id=None, state=None) -> dict:
    """pinned 的唯一写入口：任何来源都只能追加，不能删除/替换。

    - `state`：直接合并进一个会话结构（测试/内存路径）；
    - `session_id`：加跨进程锁后读盘、合并、落盘；
    - 两者都不给 → ValueError。
    模型路径没有删除接口；删除只能走 `delete_pinned(actor="user")`。
    """
    items = [entries] if isinstance(entries, (str, dict)) else list(entries or [])
    if state is not None:
        merged, report = _merge_pinned_entries(
            state.get("pinned") or [], items, actor=actor, origin=origin,
            tombstones=state.get("pinned_tombstones") or [])
        state["pinned"] = merged
        for item in report["added"]:
            _pinned_audit(state, "add", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
        for item in report["suppressed"]:
            _pinned_audit(state, "readd_suppressed", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
            logger.info("pinned_readd_suppressed id=%s actor=%s origin=%s",
                        item["id"], actor, origin)
        if report["shrink_rejected"]:
            _pinned_audit(state, "shrink_rejected", actor=actor, origin=origin)
        return report
    if not session_id:
        raise ValueError("append_pinned 需要 session_id 或 state")
    with cross_process_lock(_path(session_id, project_id)):
        data = load(session_id, project_id)
        merged, report = _merge_pinned_entries(
            data.get("pinned") or [], items, actor=actor, origin=origin,
            tombstones=data.get("pinned_tombstones") or [])
        data["pinned"] = merged
        for item in report["added"]:
            _pinned_audit(data, "add", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
        for item in report["suppressed"]:
            _pinned_audit(data, "readd_suppressed", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
            logger.info("pinned_readd_suppressed id=%s actor=%s origin=%s",
                        item["id"], actor, origin)
        if report["shrink_rejected"]:
            _pinned_audit(data, "shrink_rejected", actor=actor, origin=origin)
        if data.get("summary_structured") or merged:
            data["summary"] = _render_summary(
                _pinned_text(merged), data.get("rolling") or "",
                data.get("pinned_provenance") or "", bool(data.get("pinned_full")))
        _write(session_id, data, project_id)
        return report


def delete_pinned(entry_id, actor="user", *, session_id, project_id=None) -> dict:
    """用户删除单条固定事实：物理移除 + 写墓碑 + 审计。

    `actor` 必须是 `user`；模型路径调用只会被拒绝，条目数不变。
    """
    if str(actor) != "user":
        return {"ok": False, "deleted": False, "reason": "actor_not_user"}
    if not session_id:
        return {"ok": False, "deleted": False, "reason": "missing_session_id"}
    entry_id = str(entry_id or "")
    with cross_process_lock(_path(session_id, project_id)):
        data = load(session_id, project_id)
        entries = [_normalize_pinned_item(e) for e in (data.get("pinned") or [])]
        target = next((e for e in entries if e["id"] == entry_id), None)
        if target is None:
            return {"ok": False, "deleted": False, "reason": "not_found"}
        remaining = [e for e in entries if e["id"] != entry_id]
        data["pinned"] = remaining
        tombstone = {
            "fingerprint": _pin_fingerprint(target["text"]),
            "id": target["id"],
            "text": target["text"],
            "deleted_at": datetime.now().isoformat(timespec="seconds"),
            "deleted_by": "user",
        }
        tombstones = [t for t in _normalize_tombstones(data.get("pinned_tombstones"))
                      if t["fingerprint"] != tombstone["fingerprint"]]
        tombstones.append(tombstone)
        data["pinned_tombstones"] = tombstones
        _pinned_audit(data, "delete", actor="user", entry_id=target["id"],
                      origin="memory_panel", text=target["text"])
        if data.get("summary_structured") or remaining:
            data["summary"] = _render_summary(
                _pinned_text(remaining), data.get("rolling") or "",
                data.get("pinned_provenance") or "", bool(data.get("pinned_full")))
        else:
            data["summary"] = str(data.get("rolling") or "")
        _write(session_id, data, project_id)
    return {"ok": True, "deleted": True, "id": entry_id, "remaining": len(remaining)}


def pinned_view(session_id, project_id=None) -> dict:
    """只读视图：条目、墓碑、最近审计。供面板与排障使用。"""
    data = load(session_id, project_id) if session_id else _default()
    return {"session_id": str(session_id or ""),
            "entries": data.get("pinned") or [],
            "tombstones": data.get("pinned_tombstones") or [],
            "audit": (data.get("pinned_audit") or [])[-20:]}


def _summary_state_from_raw(raw) -> dict:
    """从磁盘记录读出 pinned/rolling 两段（结构性改造 1）。

    - 新格式：`pinned`（条目列表，兼容旧的纯文本）+ `rolling`（字符串）；
    - 旧格式：只有 `summary` 一整段 → 有分节标记的按标记拆，没有标记的**整段
      先放 rolling**，等首次超限时才按 40% 头部搬进 pinned（与既定规则一致）。
    返回值也带 `summary`（渲染结果），供旧调用方零改动读取。
    """
    pinned_raw = raw.get("pinned") if isinstance(raw, dict) else None
    rolling_raw = raw.get("rolling") if isinstance(raw, dict) else None
    legacy = raw.get("summary") if isinstance(raw, dict) else ""
    provenance = str(raw.get("pinned_provenance") or "") if isinstance(raw, dict) else ""
    full = bool(raw.get("pinned_full")) if isinstance(raw, dict) else False
    structured = False
    entries = []
    if isinstance(pinned_raw, list) or isinstance(pinned_raw, str) or rolling_raw is not None:
        flag = raw.get("summary_structured") if isinstance(raw, dict) else None
        structured = bool(flag) if flag is not None else True
        if isinstance(pinned_raw, list):
            for item in pinned_raw:
                entry = _normalize_pinned_item(item)
                if entry["text"]:
                    entries.append(entry)
        elif isinstance(pinned_raw, str) and pinned_raw.strip():
            entries = [_pinned_entry(b, actor="migration")
                       for b in _summary_entries(pinned_raw)]
        rolling = str(rolling_raw or "")
    else:
        parsed = _parse_summary(legacy or "")
        structured = bool(parsed.get("structured"))
        provenance = parsed.get("provenance") or provenance
        rolling = parsed.get("rolling") or ""
        if structured:
            entries = [_pinned_entry(b, source="legacy")
                       for b in _summary_entries(parsed.get("pinned"))]
        else:
            rolling = str(legacy or "")
    summary = (_render_summary(_pinned_text(entries), rolling, provenance, full)
               if structured else rolling)
    return {"pinned": entries, "rolling": rolling, "pinned_full": full,
            "pinned_provenance": provenance, "structured": structured,
            "summary": summary}


def _store_summary_state(data, state, *, actor="model", origin="summary_writeback",
                         merge=True) -> None:
    """把 pinned/rolling 写进会话结构；`summary` 只是派生值，不落盘。

    `merge=True` 时 pinned 只走「旧列表 + 新增」合并：旧条目原样保留，非用户
    来源的缩短入参整批拒绝（这是新不变量，替代旧的 pinned 逐字节不变断言）。
    `merge=False` 只用于从磁盘装载（装载不是一次写入）。
    """
    incoming = [_normalize_pinned_item(e, default_actor="")
                for e in (state.get("pinned") or [])]
    if merge:
        merged, report = _merge_pinned_entries(
            data.get("pinned") or [], incoming, actor=actor, origin=origin,
            tombstones=data.get("pinned_tombstones") or [])
        if report["shrink_rejected"]:
            _pinned_audit(data, "shrink_rejected", actor=actor, origin=origin)
        for item in report["added"]:
            _pinned_audit(data, "add", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
        for item in report["suppressed"]:
            _pinned_audit(data, "readd_suppressed", actor=actor, entry_id=item["id"],
                          origin=origin, text=item["text"])
            logger.info("pinned_readd_suppressed id=%s actor=%s origin=%s",
                        item["id"], actor, origin)
    else:
        merged = incoming
    data["pinned"] = merged
    data["rolling"] = str(state.get("rolling") or "")
    data["pinned_full"] = bool(state.get("pinned_full"))
    data["pinned_provenance"] = str(state.get("pinned_provenance") or "")
    data["summary_structured"] = bool(state.get("structured")) or bool(merged)
    if merge and (state.get("structured") or merged):
        data["summary"] = _render_summary(
            _pinned_text(merged), data["rolling"],
            data["pinned_provenance"], data["pinned_full"])
    else:
        data["summary"] = str(state.get("summary") or "")



def _summary_snapshot_fields(text) -> dict:
    """快照里的摘要同构字段（pinned/rolling），不存整段文本。"""
    state = _summary_state_from_raw({"summary": str(text or "")})
    return {"pinned": state["pinned"], "rolling": state["rolling"],
            "pinned_full": state["pinned_full"],
            "pinned_provenance": state["pinned_provenance"]}


def _summary_snapshot_text(item) -> str:
    """从快照读回渲染摘要（兼容只有 summary 的老快照）。"""
    if not isinstance(item, dict):
        return ""
    if "pinned" in item or "rolling" in item:
        return str(_summary_state_from_raw(item)["summary"])
    return str(item.get("summary") or "")


def _set_summary(data, text, *, actor="model", origin="summary_writeback") -> None:
    """兼容入口：把一段渲染文本拆回 pinned/rolling 后存进结构。

    pinned 只做「旧列表 + 新增」合并；模型/自动路径无法通过这里删条目。
    """
    state = _summary_state_from_raw({"summary": str(text or "")})
    _store_summary_state(data, state, actor=actor, origin=origin, merge=True)


def _apply_raw(data: dict, raw) -> dict:
    """把磁盘记录填进会话结构（raw 为 None → 保持空结构）。"""
    if isinstance(raw, dict):
        turns = raw.get("turns")
        # 摘要从 pinned/rolling 两段读入；旧文件只有 summary 整段时在这里兼容拆分。
        _store_summary_state(data, _summary_state_from_raw(raw), merge=False)
        # 墓碑/审计是 pinned 的附属字段：老会话缺失时按空处理，不做升格写入。
        data["pinned_tombstones"] = _normalize_tombstones(raw.get("pinned_tombstones"))
        data["pinned_audit"] = [row for row in (raw.get("pinned_audit") or [])
                                if isinstance(row, dict)][-_PINNED_AUDIT_LIMIT:]
        data["turns"] = [
            {
                **t,
                "user": _clean_legacy_prompt(t.get("user", "")),
                "assistant": _clean_legacy_prompt(t.get("assistant", "")),
            }
            for t in turns if isinstance(t, dict)
        ] if isinstance(turns, list) else []
        # archived：长会话压缩时被弹出工作窗口（turns）的更早轮次，完整逐字保留，
        # 供 UI 回灌完整历史。老会话没这个字段 -> 空列表（行为与改动前一致）。
        archived = raw.get("archived")
        data["archived"] = [
            {
                **t,
                "user": _clean_legacy_prompt(t.get("user", "")),
                "assistant": _clean_legacy_prompt(t.get("assistant", "")),
            }
            for t in archived if isinstance(t, dict)
        ] if isinstance(archived, list) else []
        data["updated_at"] = raw.get("updated_at") or ""
        # ⚠⚠ **会话要记住自己用的是哪个代码库**（2026-10-10 加）。
        #
        # 实测事故：mean 题那次的探针目录被设成**全局** code_root；四天后在同一项目
        # 里开新会话问「优化界面」，中间件某一环没设成上下文 -> 静默回落到那个全局值
        # -> research 在 `calc.py`（mean 题的代码）底下搜 mean -> 5 个子任务 0/5 全败。
        #
        # 会话隔离**做到了**（不同 session_id、不同文件）。没做到的是代码库隔离 ——
        # 代码库从来没「跟着会话走」这个概念，只挂在项目/全局上，于是四天后被继承。
        #
        # 这里记下会话建立时的代码库，之后该会话一直用它。老会话没这字段时为空串，
        # 调用方回落原有优先级 —— **行为与改动前完全一致**，不做静默改写。
        raw_root = raw.get("code_root")
        data["code_root"] = raw_root.strip() if isinstance(raw_root, str) else ""
        # 绑定时刻：合并多份历史记录时按它判「谁绑在前面」。老记录没有 -> 空串，
        # _merge_candidates 会回退按 updated_at。必须一起搬，否则永久性承诺
        # 在下次合并时失去判据。
        raw_at = raw.get("code_root_at")
        data["code_root_at"] = raw_at.strip() if isinstance(raw_at, str) else ""
        # 会话的**归属项目**（丙方案，2026-10-11）。
        #
        # 为什么需要：乙方案把会话存储改成全局一份，「同一个 session_id 跨项目
        # 仍是同一个会话」。但 `/api/sessions` 有产品语义 —— 用户在项目 B 里
        # 不该看到项目 A 的会话列表。以前这靠「按项目分桶」天然做到；
        # 分桶去掉后就得**显式记住归属**。
        #
        # 归属在会话首次建立时定下，之后不改（与 code_root 的绑定同一次）。
        # 老会话没有这个字段 -> 空串 -> 调用方按「无归属」处理。
        raw_owner = raw.get("owner_project_id")
        data["owner_project_id"] = raw_owner.strip() if isinstance(raw_owner, str) else ""
        # 快照必须一起搬进来。这里漏掉它时症状特别隐蔽：snapshot() 写入成功、
        # 没有任何报错，但下一次 load 读回来的 data 里没有 snapshots，
        # 于是 restore_snapshot 永远说「还没有任何快照」——写得进、读不回来。
        snaps = raw.get("snapshots")
        data["snapshots"] = [s for s in snaps if isinstance(s, dict)] \
            if isinstance(snaps, list) else []
        manifest = raw.get("compaction_manifest")
        if isinstance(manifest, dict):
            entries = manifest.get("entries")
            data["compaction_manifest"] = {
                "schema": int(manifest.get("schema") or 1),
                "entries": [dict(e) for e in entries if isinstance(e, dict)]
                if isinstance(entries, list) else [],
                "evidence_available": bool(manifest.get("evidence_available", True)),
            }
    return data


def _default() -> dict:
    # code_root / code_root_at 见 _apply_raw 的说明：会话级代码库归属与绑定时刻，
    # 老会话为空串（行为与改动前完全一致，不做静默改写）。
    # owner_project_id 见 _apply_raw 的说明：会话的**归属项目**（丙方案）。
    return {"session_id": "", "updated_at": "", "summary": "",
            "pinned": [], "rolling": "", "pinned_full": False,
            "pinned_provenance": "", "summary_structured": False,
            "pinned_tombstones": [], "pinned_audit": [],
            "turns": [], "archived": [], "code_root": "", "code_root_at": "",
            "owner_project_id": "",
            "compaction_manifest": {"schema": 1, "entries": [], "evidence_available": True}}


_FILE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?[\w.@+-]+(?:[\\/][\w.@+-]+)*\."
    r"(?:py|md|jsonl?|ts|tsx|js|vue|css|html|ya?ml|toml|txt|csv|pdf|docx|"
    r"gd|unity|scene|cs|cpp|h|java|go|rs|sql|sh|bat|ps1)\b", re.I)


def _compaction_entry(turns, traces) -> dict:
    """记录本批摘要的原文指针；trace 回收后由加载/回收路径刷新可用性。"""
    source_ids = [str(t.get("turn_id") or "") for t in turns if t.get("turn_id")]
    timestamps = sorted(str(t.get("ts")) for t in turns if t.get("ts"))
    text = "\n".join(str(t.get(key) or "") for t in turns
                     for key in ("user", "assistant", "resume_context"))
    evidence = [{"kind": "turn_trace", "turn_id": tid, "available": True}
                for tid in source_ids if tid in traces]
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_turn_ids": source_ids,
        "time_range": {"start": timestamps[0] if timestamps else "",
                       "end": timestamps[-1] if timestamps else ""},
        "files": sorted({path.replace("\\", "/") for path in _FILE_PATH_RE.findall(text)})[:100],
        "evidence": evidence,
        "evidence_available": True,
    }


def _refresh_manifest_availability(data, traces=None) -> bool:
    """按当前 trace 真源刷新证据可用性；返回是否有变化。"""
    manifest = data.get("compaction_manifest")
    if not isinstance(manifest, dict) or not manifest.get("entries"):
        return False
    traces = load_turn_traces(data.get("session_id") or "") if traces is None else traces
    changed = False
    for entry in manifest.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("evidence") or []:
            if not isinstance(item, dict) or item.get("kind") != "turn_trace":
                continue
            available = str(item.get("turn_id") or "") in traces
            if item.get("available") is not available:
                item["available"] = available
                changed = True
        available = all(item.get("available") is not False
                        for item in entry.get("evidence") or [] if isinstance(item, dict))
        if entry.get("evidence_available") is not available:
            entry["evidence_available"] = available
            changed = True
    available = all(entry.get("evidence_available") is not False
                    for entry in manifest.get("entries") or [] if isinstance(entry, dict))
    if manifest.get("evidence_available") is not available:
        manifest["evidence_available"] = available
        changed = True
    return changed


def _read_all_candidates(session_id) -> list:
    """同一个 session_id 在所有存储位置上的记录（全局扁平 + 旧项目分桶）。

    返回 `[(来源标签, raw_dict), ...]`；读取失败的静默跳过。
    顺序即优先级：**全局扁平在前**（迁移后的唯一真源），旧分桶在后。
    """
    slug = _slug(session_id) + ".json"
    out = []
    for d in _bucket_dirs() + _legacy_bucket_dirs():
        p = os.path.join(d, slug)
        raw = _read_raw(p)
        if isinstance(raw, dict):
            out.append((d, raw))
    return out


def _merge_candidates(records: list) -> dict:
    """把同一 session_id 的多份历史记录合并成一份（只在迁移时发生）。

    规则（写死在注释里，不许随手改）：

    * `code_root` —— **最早绑定的那份**。绑定是永久承诺（`bind_code_root`
      的语义），迁移时不许因为「哪份最近被写过」就改掉它。有 `code_root_at`
      按它取最早；老记录没有该字段时回退按 `updated_at` 最早。
    * `turns` / `summary` / `snapshots` / `updated_at` —— `updated_at` 最新的那份，
      **平手时取 records 里靠前的那份**（`_read_all_candidates` 的顺序即优先级：
      全局扁平在前，旧分桶在后）。

    ⚠ 不用 `sorted(...)[-1]`：时间戳相同时它靠排序稳定性碰运气，实测会把旧
    分桶盖到全局文件上。`records` 是 `[(来源目录, raw_dict), ...]`，顺序即优先级。
    """
    if not records:
        return {}
    if len(records) == 1:
        return records[0][1]

    def _root_at(raw):
        return str(raw.get("code_root_at") or raw.get("updated_at") or "")

    # turns/summary/snapshots：updated_at 最新；平手保留靠前的
    newest = records[0][1]
    for _src, raw in records[1:]:
        if str(raw.get("updated_at") or "") > str(newest.get("updated_at") or ""):
            newest = raw

    # code_root：最早绑定的一份；平手保留靠前的
    bound = [raw for _src, raw in records
             if str(raw.get("code_root") or "").strip()]
    if bound:
        best = bound[0]
        for raw in bound[1:]:
            if _root_at(raw) < _root_at(best):
                best = raw
    else:
        best = None

    merged = dict(newest)
    if best is not None:
        merged["code_root"] = str(best.get("code_root") or "").strip()
        if best.get("code_root_at"):
            merged["code_root_at"] = best["code_root_at"]
    # owner 同样取"最早绑定的那份"—— 绑定是永久承诺，和 code_root 同理。
    # 漏了它时的症状特别隐蔽：turns/summary 都正常，但会话在项目列表里
    # 悄悄消失（`list_sessions(project_id=pid)` 按 owner 过滤）。
    owners = [raw for _src, raw in records
              if str(raw.get("owner_project_id") or "").strip()]
    if owners:
        first_owner = min(owners, key=lambda r: str(
            r.get("code_root_at") or r.get("updated_at") or ""))
        merged["owner_project_id"] = str(
            first_owner.get("owner_project_id") or "").strip()
    return merged


def load(session_id, project_id=None) -> dict:
    """读取一个会话记录；缺失/损坏返回空结构（调用方无需处理异常）。

    **全局唯一**：一个 session_id 就是一个会话，与项目无关（乙方案）。
    读穿透 = 全局扁平目录 + 所有旧项目分桶目录的同名文件合并；
    合并规则见 `_merge_candidates` 的注释，迁移完成后只剩全局一份。
    """
    data = _default()
    data["session_id"] = str(session_id or "default")
    records = _read_all_candidates(session_id)
    raw = _merge_candidates(records) if records else None
    data = _apply_raw(data, raw)
    _refresh_manifest_availability(data)
    return data


def _write(session_id, data, project_id=None) -> bool:
    """写全局唯一的那份会话文件（project_id 忽略，仅为签名兼容保留）。"""
    try:
        d = _bucket_dirs()[0]
        os.makedirs(d, exist_ok=True)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        p = os.path.join(d, _slug(session_id) + ".json")
        tmp = p + ".tmp"
        # 结构性改造 1：摘要落盘只有 pinned/rolling 两段，`summary` 是加载时
        # 由它们渲染出来的派生值，绝不能写回磁盘（否则又是一段文本，两套真相）。
        persists = {k: v for k, v in data.items() if k != "summary"}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(persists, f, ensure_ascii=False, indent=1)
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


def archived_turns(session_id, project_id=None) -> list:
    """长会话压缩时被弹出工作窗口的更早轮次（完整逐字，list[dict]）。

    Agent 在初始化时读它进 self.archived，之后每次压缩把新弹出的前缀累进来再落盘——
    这样跨轮、跨进程都不会丢已归档的历史。老会话没这个字段时返回空列表。
    """
    return [t for t in load(session_id, project_id).get("archived", []) if isinstance(t, dict)]


def full_history(session_id, project_id=None) -> list:
    """给 UI 回灌用的**完整**逐轮记录 = 归档（更早轮次）+ 当前工作窗口。

    顺序即时间序：先 archived，再 turns。压缩只收缩喂给 LLM 的工作窗口（turns），
    完整对话始终在此可见——这正是修掉的「长会话会删掉前面的对话」问题。
    """
    return archived_turns(session_id, project_id) + history(session_id, project_id)


def summary_text(session_id, project_id=None) -> str:
    return load(session_id, project_id).get("summary", "") or ""


def inject_summary_text(summary, limit=None) -> str:
    """注入侧封顶（结构性改造 4）：只压 rolling，pinned 原样拼回、永不裁剪。

    与写回共用 `_parse_summary` / `_render_summary` / `_fit_rolling`，保证
    注入与落盘逐字一致。传入老式无标记摘要时整段视为 rolling。
    """
    limit = _resolve_summary_limit(limit)
    text = str(summary or "")
    if not text:
        return ""
    return _fit_summary_text(text, None, limit)


def bind_code_root(session_id, root, project_id=None) -> bool:
    """把该会话的代码库**固定**为 `root`；已绑定的会话**不改**。

    绑一次，终身沿用 —— 这是整个隔离的核心。若每轮跟着当前项目变，
    等于没隔离，那正是本缺陷本身。

    同时定下**归属项目**（丙方案）：会话首次建立时它属于哪个项目。
    之后不随请求项目变化 —— 否则用户在项目 B 的列表里会看到 A 的会话。

    ⚠ 现在 load 是**全局**的：同一个 session_id 在项目 A 绑过之后，
    切到项目 B 再问，这里读到的是 A 的那份绑定 -> 直接返回 False，
    会话继续用 A。这正是乙方案要的行为（实测曾反过来：分桶时 B 会重新绑）。
    """
    with _lock:
        data = load(session_id, project_id)
        if data.get("code_root"):
            return False            # 已绑定：不动
        now = datetime.now().isoformat(timespec="seconds")
        data["code_root"] = str(root or "").strip()
        # 绑定时刻。合并多份历史记录时按它判「谁绑在前面」（见 _merge_candidates）。
        data["code_root_at"] = now
        # 归属项目：只在还没有时写一次。调用方没给就用「当前项目」。
        if not data.get("owner_project_id"):
            owner = (project_id if project_id is not None
                     else projects.current_project_id()) or ""
            data["owner_project_id"] = str(owner).strip()
        return _write(session_id, data, project_id)


def owned_by(session_id, project_id=None) -> str:
    """这个会话的归属项目；老会话（没这个字段）返回空串。"""
    if not session_id:
        return ""
    try:
        return str(load(session_id, project_id).get("owner_project_id") or "").strip()
    except Exception:               # noqa: BLE001 - 读不到不是错误
        return ""


def backfill_owner(session_id, project_id) -> bool:
    """给**老会话**补归属字段（一次性迁移用）。

    只在还没有归属时写。新会话走 `bind_code_root` 自动带上，不需要这个。
    """
    with _lock:
        data = load(session_id, project_id)
        if data.get("owner_project_id"):
            return False
        data["owner_project_id"] = str(project_id or "").strip()
        return _write(session_id, data, project_id)


def current_code_root(session_id, project_id=None) -> str:
    """这个会话记住的代码库；没记住则空串（调用方回落项目/全局）。"""
    if not session_id:
        return ""
    try:
        return str(load(session_id, project_id).get("code_root") or "").strip()
    except Exception:               # noqa: BLE001 - 读不到不是错误，回落即可
        return ""


def _turn_sig(user, assistant) -> str:
    """轮内容签名：user+assistant 拼起来取 sha1，用于跨 save 继承 turn_id。"""
    h = hashlib.sha1()
    h.update(str(user or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update(str(assistant or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def _inherit_turn_id(t, sig, carry_map) -> str:
    """给一轮定 turn_id：自带就留；否则按内容签名复用盘上已有的；都没有才 mint 新的。

    carry_map 是 {sig: [turn_id, ...]}（同签名多轮时按顺序消费，pop(0)）。
    """
    tid = t.get("turn_id")
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    queue = carry_map.get(sig)
    if queue:
        return queue.pop(0)
    return uuid.uuid4().hex


def _save_unlocked(session_id, turns, summary=None, project_id=None, archived=None,
                   compaction_turns=None) -> bool:
    """整体落盘一个会话（turns = [{user, assistant}]）。

    archived：长会话压缩弹出的更早轮次（完整逐字），仅在压缩点由调用方传入并累进；
    传 None 表示**保持磁盘上已有的 archived 不变**——绝大多数常规 save 不关心归档，
    绝不能因为它们没带 archived 就把已归档的完整历史抹掉。

    turn_id：每轮带一个稳定 id（旁路推理轨迹的关联键）。Agent 每轮会用**不含 turn_id
    的内存 history 整体重写**，所以这里按内容签名从盘上既有轮继承 turn_id，只给真正
    的新轮 mint —— 保证同一轮跨多次 save、跨重启都拿同一个 id，轨迹不会错挂或被抹掉。
    """
    with _lock:
        data = load(session_id, project_id)
        data["session_id"] = str(session_id or "default")
        # ⚠ 会话**首次**落盘时把当时的代码库记下来，之后不再变。
        # 这里刻意不读 get_runtime —— 那会形成循环（config 依赖 sessions），
        # 而且 sessions 是纯存储层，不该知道"当前项目是什么"。
        # 真正的绑定由 api 入口调用 bind_code_root() 完成。
        if summary is not None:
            # 兼容入口：调用方仍可传一整段渲染文本；这里拆成 pinned/rolling
            # 再落盘（模型产出的 pinned 段落已在 sessions 内部被丢弃）。
            _set_summary(data, summary)
        # Agent 工作窗口不携带 ts/turn_id；归档时按原轮继承，不能拿压缩时刻冒充原时刻。
        carry_map: dict = {}
        carry_ts: dict = {}
        for _existing in (data.get("turns") or []) + (data.get("archived") or []):
            if not isinstance(_existing, dict):
                continue
            _sig = _turn_sig(_existing.get("user", ""), _existing.get("assistant", ""))
            _tid = _existing.get("turn_id")
            if isinstance(_tid, str) and _tid.strip():
                carry_map.setdefault(_sig, []).append(_tid.strip())
                carry_ts["id:" + _tid.strip()] = str(_existing.get("ts") or "")
            carry_ts.setdefault("sig:" + _sig, str(_existing.get("ts") or ""))

        def _ser(t, ts_default):
            sig = _turn_sig(t.get("user", ""), t.get("assistant", ""))
            tid = str(t.get("turn_id") or "").strip()
            inherited_ts = carry_ts.get("id:" + tid) or carry_ts.get("sig:" + sig)
            has_prior = ("id:" + tid) in carry_ts or ("sig:" + sig) in carry_ts
            fresh_ts = "" if has_prior else datetime.now().isoformat(timespec="seconds")
            out = {"user": t.get("user", ""), "assistant": t.get("assistant", ""),
                   "ts": t.get("ts") or inherited_ts or ts_default or fresh_ts,
                   "turn_id": _inherit_turn_id(t, sig, carry_map)}
            if "resume_context" in t:
                out["resume_context"] = str(t["resume_context"])[-6000:]
            for key in ("checkpoint_id", "failure_reason"):
                if t.get(key):
                    out[key] = str(t[key])[:1200]
            return out

        data["turns"] = [
            _ser(t, datetime.now().isoformat(timespec="seconds"))
            for t in (turns or []) if isinstance(t, dict)
        ]
        if archived is not None:
            data["archived"] = [_ser(t, "") for t in (archived or []) if isinstance(t, dict)]
            if compaction_turns:
                count = min(len(compaction_turns), len(data["archived"]))
                source = data["archived"][-count:] if count else []
                traces = load_turn_traces(session_id)
                if source:
                    manifest = data.setdefault("compaction_manifest", {
                        "schema": 1, "entries": [], "evidence_available": True})
                    manifest.setdefault("entries", []).append(_compaction_entry(source, traces))
                    _refresh_manifest_availability(data, traces)
        return _write(session_id, data, project_id)


def save(session_id, turns, summary=None, project_id=None, archived=None,
         compaction_turns=None) -> bool:
    """跨进程串行化会话写：同一 STATE_ROOT 的两个实例不会交错替换同一文件。

    注意：这只能防止写文件交错，不能把两份过期快照合并成完整历史；同一会话
    真正的并发写由 api._session_run_scope 的跨进程 run 租约挡住。
    """
    with cross_process_lock(_path(session_id, project_id)):
        return _save_unlocked(session_id, turns, summary, project_id, archived,
                              compaction_turns=compaction_turns)


def _turn_trace_path(session_id) -> str:
    return os.path.join(TURN_TRACE_DIR, _slug(session_id) + ".json")


def load_turn_traces(session_id) -> dict:
    """读某会话的逐轮轨迹旁路表 {turn_id: [steps...]}；缺失/损坏一律返回 {}，绝不抛。"""
    try:
        with open(_turn_trace_path(session_id), "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_turn_traces(session_id, store: dict) -> bool:
    try:
        os.makedirs(TURN_TRACE_DIR, exist_ok=True)
        p = _turn_trace_path(session_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def _apply_turn_trace(session_id, turn_id, steps) -> bool:
    """写入并回收一轮轨迹。**调用方须已持有 _lock**（save_turn_trace / attach 都持锁）。"""
    store = load_turn_traces(session_id)
    store[turn_id] = steps
    data = load(session_id)
    alive = {t.get("turn_id") for t in
             (data.get("turns") or []) + (data.get("archived") or [])
             if isinstance(t, dict) and t.get("turn_id")}
    if alive:
        store = {k: v for k, v in store.items() if k in alive or k == turn_id}
    if len(store) > TURN_TRACE_MAX_TURNS:
        keep = list(store.keys())[-TURN_TRACE_MAX_TURNS:]
        if turn_id not in keep:
            keep = keep[1:] + [turn_id]
        store = {k: store[k] for k in keep if k in store}
    if _refresh_manifest_availability(data, store):
        _write(session_id, data)
    return _write_turn_traces(session_id, store)


def save_turn_trace(session_id, turn_id, steps) -> bool:
    """写/更新一轮的完整轨迹，并顺带清理孤儿、限总量。

    - 只保留当前会话仍存活的 turn_id（轮被删/回退后，其轨迹随之回收）；
    - 超过 TURN_TRACE_MAX_TURNS 时按插入顺序丢最旧的（刚写入的这条永不丢）。
    全程静默降级，写轨迹失败绝不影响对话主流程。
    """
    if not (isinstance(turn_id, str) and turn_id.strip()):
        return False
    if not isinstance(steps, list):
        return False
    try:
        with _lock:
            return _apply_turn_trace(session_id, turn_id.strip(), steps)
    except Exception:               # noqa: BLE001 - 轨迹是锦上添花，绝不冒泡
        return False


def attach_turn_trace(session_id, user, assistant, steps) -> bool:
    """把一轮的完整轨迹挂到该会话里 user 命中的**最后一条**轮上。

    供 api.py 在 SSE 收尾时调用：agent 已在流内 save 过这轮（带 turn_id），这里按问题
    文本找回同一轮（最后一条命中即本轮）的 turn_id 再写旁路。老会话轮没有 turn_id 时，
    就地补 mint 一个并回写盘，以便下次刷新能按它关联。找不到匹配轮返回 False。
    （assistant 参数保留仅为调用方签名兼容，匹配以 user 为准。）
    """
    if not isinstance(steps, list) or not steps:
        return False
    try:
        with _lock:
            data = load(session_id)
            all_turns = (data.get("turns") or []) + (data.get("archived") or [])
            target = None
            for t in reversed(all_turns):
                if not isinstance(t, dict):
                    continue
                if str(t.get("user", "")) == str(user or ""):
                    target = t
                    break
            if target is None:
                return False
            tid = target.get("turn_id")
            if not (isinstance(tid, str) and tid.strip()):
                tid = uuid.uuid4().hex
                target["turn_id"] = tid
                _write(session_id, data)
            return _apply_turn_trace(session_id, tid, steps)
    except Exception:               # noqa: BLE001
        return False


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
    "你是对话压缩器。把下面这段【早期对话】压缩成不超过 {limit} 字的中文要点。"
    "只保留：已确认的项目事实、精确标识符/签名/错误码、涉及的文件路径与行号、"
    "用户长期有效的偏好与约束、真正尚未解决的问题。"
    "不要保留：一次性的操作步骤、工具运行过程/命令回显、寒暄、已经解决的事项、"
    "用户只针对某一轮格式的要求、一次性的“先读某文件/不要重试/只读不改”等指令、"
    "环境警告（缓存权限/依赖弃用/插件配置）。原文被截断时明确标注，不要编造。"
    "只输出要点本身。"
    "\n\n早期对话：\n"
)
_SUMMARY_TURN_CHARS = 1200
# 摘要上限不再写死：由「保留预算」按 token 比例派生（中文 1 字 ≈ 0.9~1.1 token，
# 故字符上限可直接用 token 数换算）：
#   SUMMARY_MAX_CHARS = PROMPT_TOKEN_BUDGET × COMPACT_KEEP_RATIO × SUMMARY_SHARE
# - PROMPT_TOKEN_BUDGET：单轮 prompt 的兜底预算。运行期 Agent 会用
#   config.prompt_token_budget 按模型真实窗口缩放（1M 窗口 ≈ 786k）；这里取模块
#   常量作全局默认口径，保证写回/注入/面板三处只有一个来源。
# - COMPACT_KEEP_RATIO：压缩后留给原文历史的目标占比（默认 0.35）。
# - SUMMARY_SHARE 取 0.55（0.5~0.6 区间的中值）：摘要约占 prompt 预算的 19%，
#   既装得下长期事实/约束，又不与 kept 原文抢同一块保留预算。
_SUMMARY_SHARE = 0.55
# pinned（固定事实）配额：上限的 40%（结构性改造 4）。pinned 只进不出、绝不裁剪；
# 配额满了就停止吸收新事实并显式标注「前缀已满」。
_SUMMARY_PIN_RATIO = 0.4
_PINNED_HEADER = "【固定事实】"
_PINNED_LEGACY_HEADER = "【固定事实｜pinned_from_legacy_head】"
_ROLLING_HEADER = "【近期进展】"
_PINNED_FULL_NOTE = "（固定事实前缀已满）"
_ROLLING_EVICT_FMT = "（最旧进展 %d 字已省略）"
# 硬约束判定：确定性关键词，不依赖 LLM（LLM 失败时也不影响已有内容）。
# 未命中的新事实留在 rolling，不会因为判定保守而被丢弃。
_HARD_FACT_RE = re.compile(r"必须|不得|禁止|保持|不允许|不能|务必|严禁|不可")
_HARD_IMPERATIVE_RE = re.compile(
    r"^(请|必须|不得|禁止|保持|不允许|不能|务必|严禁|不可)")
_HARD_PROPER_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.-]{2,}"
    r"|[0-9]+(?:\.[0-9]+)*"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*"
    r"|v[0-9]+(?:\.[0-9]+)+")
_HARD_NEGATIVE_RE = re.compile(
    r"不是长期|非长期|不是规则|不构成长期|不应视为|不要视为"
    r"|仅用于对比|仅供对比|仅用于回顾|仅供回顾"
    r"|例如|示例|比如|按上一批|上一批口径|本次只|这一轮只"
    r"|只是为了|别当成|不作为长期|只能靠|否则|本次不会"
    r"|实施计划|行动计划|执行计划|排期"
    r"|未完成现场|must-keep")


def _is_hard_entry(entry) -> bool:
    """判定一条摘要条目是否值得永久固定（pinned）。

    结构性改造 6：先排除负例（否定句/举例/批次口径/一次性现场），再要求
    带硬标识（专有名词/数字/路径/版本）或以祈使词开头。旧规则只看到
    “保持/必须”四个字就固定，把 `旧代码保持原样…不是长期规则` 也焊进 pinned。
    """
    text = str(entry or "").strip()
    if not text:
        return False
    if _HARD_NEGATIVE_RE.search(text):
        return False
    compact = re.sub(r"\s+", " ", text)
    if _HARD_IMPERATIVE_RE.match(compact):
        return True
    # 仅有专有名词/数字还不够（“PostgreSQL 15 部署在杭州”是普通事实）；
    # 必须同时带义务语义（必须/不得/保持…），再以硬标识佐证它可长期复用。
    if not _HARD_FACT_RE.search(compact):
        return False
    if _HARD_PROPER_RE.search(compact):
        return True
    # 中文里没有 ASCII 专有名词的约束（“禁止把调试开关带入发布构建”）：
    # 义务词 + 足够具体的对象才收；8 个汉字是实测能挡住“保持格式”这类泛泛表述。
    return len(re.findall(r"[\u4e00-\u9fff]", compact)) >= 8


def summary_char_budget(prompt_tokens=None) -> int:
    """摘要字符上限：由该实例的 prompt 预算按保留比例派生。

    - prompt_tokens 为空 → 回退模块兜底 PROMPT_TOKEN_BUDGET（默认 11000），
      无模型信息时行为与改动前一致；
    - 中文 1 字 ≈ 0.9~1.1 token，所以字符上限直接取 token 预算的乘积，
      不再乘第二个换算系数。
    """
    try:
        tokens = int(prompt_tokens or PROMPT_TOKEN_BUDGET)
    except (TypeError, ValueError):
        tokens = int(PROMPT_TOKEN_BUDGET)
    return max(256, int(tokens * COMPACT_KEEP_RATIO * _SUMMARY_SHARE))


_SUMMARY_MAX_CHARS = summary_char_budget()
# 对外只读别名：面板分母/测试要跟注入封顶同源，不允许调用方再抄一个数字。
SUMMARY_MAX_CHARS = _SUMMARY_MAX_CHARS


def _resolve_summary_limit(limit=None, prompt_budget=None) -> int:
    """resolve 顺序：显式 limit > 实例 prompt 预算 > 模块默认（11000 口径）。"""
    if limit is not None:
        return max(1, int(limit))
    if prompt_budget:
        return summary_char_budget(prompt_budget)
    return _SUMMARY_MAX_CHARS


# rolling 的压缩目标由预算计算（0.9×），不再留写死的 400 字。
_SUMMARY_ROLLING_PROMPT = (
    "你是对话压缩器。把下面这段【近期进展】压缩成不超过 {limit} 字的中文要点。"
    "保留长期事实、精确标识符/签名/错误码、约束和真正未决项；去重；"
    "删除一次性操作细节、一次性指令和环境警告；不要编造。只输出要点本身。"
    "\n\n近期进展：\n{rolling}"
)


def _clip_with_mark(text, limit):
    """裁到 limit，但绝不"静默腰斩"：原样更长时显式加"…（截断）"标记，
    避免把 ALPHA-7731-OMEGA 这类精确标识符截成"看着完整的错值"（LONGCTX-Compaction-FactMangle）。
    切点若落在一段较短（<32 字符）的字母数字/_/- token 末尾，则回退到其起点，
    尽量不腰斩 token；残 token 过长则不强退（交给标记兜底）。返回 (串, 是否被截断)。"""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    m = re.search(r"[A-Za-z0-9_\-]+$", cut)
    if m and m.start() >= limit - 32 and m.start() > 0:
        cut = cut[:m.start()].rstrip(" ,，。;；:：")
    return cut + "…（截断）", True


def _naive_summary(turns) -> str:
    """LLM 不可用/失败/返回空时的确定性兜底：抽取每轮【问答双方】首段拼要点。
    截断一律显式标"…（截断）"，并把助手侧要点也纳入（此前完全丢弃 assistant 事实）；
    头注明"可能不完整/被截断，勿当已验证事实"——防下游把残缺/错值当完整事实用。"""
    bits = []
    for t in turns:
        uq, _ = _clip_with_mark(t.get("user") or "", 160)
        ans, _ = _clip_with_mark(t.get("assistant") or "", 120)
        seg = []
        if uq:
            seg.append("问：" + uq)
        if ans:
            seg.append("答：" + ans)
        if seg:
            bits.append("· " + " ／ ".join(seg))
    return ("（早期对话摘要·本地兜底，可能不完整/被截断，勿当已验证事实；"
            "需要精确标识符/数值请重读来源或追问）\n" + "\n".join(bits))


def _llm_summary(turns, llm, limit=None) -> str:
    if llm is None:
        return _naive_summary(turns)
    target = max(1, int(_resolve_summary_limit(limit) * 0.5))
    # 与 _naive_summary 一致：切片一律走 _clip_with_mark，超长时显式带"…（截断）"，
    # 不再用裸 [:400] 把落在 400 字之后的精确标识符静默腰斩后喂给摘要器
    # （LONGCTX-LLMSlice-Leak：修复此前只装了兜底、主路径漏装）。
    convo = "\n".join(
        "用户：%s\n助手：%s" % (
            _clip_with_mark(t.get("user") or "", _SUMMARY_TURN_CHARS)[0],
            _clip_with_mark(t.get("assistant") or "", _SUMMARY_TURN_CHARS)[0],
        )
        for t in turns
    )
    try:
        out = llm.chat(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(limit=target) + convo}],
            stream=False, temperature=0.2,
        )
        out = (out or "").strip()
        return _normalize_summary_blocks(out) if out else _naive_summary(turns)
    except Exception:  # noqa: BLE001 —— 压缩失败绝不能打断正常问答
        return _naive_summary(turns)


def _assert_rolling_only(text, caller):
    """结构性改造 4 的防御性断言：裁剪函数永远不得收到含 pinned 的串。

    pinned 只进不出、绝不裁剪；一旦有人把「pinned + rolling」整段丢进
    `cap_summary_*`，这里立即报错，而不是静默把约束削成半句（实测
    DET sr5 就是这样把 299 字削到 258 并插入「中间 55 字已省略」的）。
    """
    value = str(text or "")
    for marker in (_PINNED_HEADER, _PINNED_LEGACY_HEADER,
                   _ROLLING_HEADER, _PINNED_FULL_NOTE):
        if marker in value:
            raise ValueError(
                "%s 收到含 pinned/分节标记的串（%s）：裁剪只能作用于 rolling 正文"
                % (caller, marker))


def _cap_summary(text, limit=_SUMMARY_MAX_CHARS):
    """写回（生成）与注入（消费）共用同一套裁剪：保头部与尾部、丢中间。

    只允许接收 **rolling 正文**；含 pinned/分节标记的串会被
    `_assert_rolling_only` 直接拒绝（结构性改造 4）。
    """
    _assert_rolling_only(text, "_cap_summary")
    return cap_summary_head_tail(text, limit)


_SUMMARY_MID_FMT = "（中间 %d 字已省略）"
# 按 7 位数字预留标记宽度，宁可少留几个字也不让总长超限。
_SUMMARY_MID_RESERVE = len(_SUMMARY_MID_FMT % 9999999)
_SUMMARY_HEAD_RATIO = 0.4
_SUMMARY_BULLET_RE = re.compile(r"^\s*- ")


def _normalize_summary_blocks(text) -> str:
    """把相邻的 `- ` 要点之间补空行，让裁剪的丢弃单位是整条要点而不是半句。

    只动空行、不动内容和顺序，幂等（已有空行不会重复补）。_naive_summary 用的是
    "· " 单换行紧凑格式，不属于 `- ` 要点，保持原样不归一化。
    """
    value = str(text or "")
    if not value:
        return value
    out = []
    for line in value.split("\n"):
        if out and _SUMMARY_BULLET_RE.match(line) and _SUMMARY_BULLET_RE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _cut_head(value, budget) -> str:
    """头部按预算取，优先在换行/句末断开，避免把一条约束切成半句。"""
    window = value[:budget]
    if not window:
        return ""
    best = -1
    for ch in ("\n", "。", "；", "！", "？"):
        pos = window.rfind(ch)
        if pos > best:
            best = pos
    # 断点太靠前会把头部预算白白浪费掉，此时才硬切。
    if best >= budget // 2:
        return window[:best].rstrip() if window[best] == "\n" else window[:best + 1].rstrip()
    return window.rstrip()


def _cut_tail(value, budget) -> str:
    """尾部按预算取，优先从靠前的换行之后开始，避免以半句开头。"""
    if budget >= len(value):
        return value.strip()
    start = len(value) - budget
    window = value[start:]
    pos = window.find("\n")
    if 0 <= pos <= max(1, budget // 5):
        return window[pos + 1:].strip()
    return window.strip()


def cap_summary_head_tail(text, limit=_SUMMARY_MAX_CHARS) -> str:
    """rolling 正文的统一裁剪：保头部（约 40%）+ 保尾部（约 60%）、丢中间。

    摘要的典型结构是"事实/约束在前、过程在中、最新进展在后"，所以两头都要保。
    被丢的中段用 `（中间 N 字已省略）` 标注并放在头尾之间；总长度不超过 limit。
    上限小到装不下标记时退化为**优先保尾部**（与旧行为一致）。

    **结构性改造 4**：本函数只处理 rolling；pinned 由调用方在渲染时原样拼回，
    绝不进入这里。带分节标记的串一律报错，防止再次出现 DET sr5 那种把 pinned
    腰斩成 299→258 的破坏。
    """
    _assert_rolling_only(text, "cap_summary_head_tail")
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    content = limit - _SUMMARY_MID_RESERVE - 2  # 两个换行
    if content < 2:
        # 装不下标记：优先保尾部，退化行为在此写明。
        return value[-limit:]
    head_budget = max(1, int(content * _SUMMARY_HEAD_RATIO))
    tail_budget = max(1, content - head_budget)
    head = _cut_head(value, head_budget)
    tail = _cut_tail(value, tail_budget)
    omitted = len(value) - len(head) - len(tail)
    if omitted <= 0:
        return value[:limit]
    out = head + "\n" + (_SUMMARY_MID_FMT % omitted) + "\n" + tail
    if len(out) > limit:
        # 标记实际位数比预留少时才可能有极小溢出，再从尾部开头收一次。
        overflow = len(out) - limit
        tail = tail[overflow:] if overflow < len(tail) else ""
        omitted = len(value) - len(head) - len(tail)
        out = head + "\n" + (_SUMMARY_MID_FMT % omitted) + "\n" + tail
    return out


def cap_summary_tail(text, limit=_SUMMARY_MAX_CHARS) -> str:
    """兼容旧名：转发到 cap_summary_head_tail（全局只有这一份实现）。"""
    return cap_summary_head_tail(text, limit)


_SUMMARY_ROLLING_BLOCK_OVERHEAD = len(_ROLLING_HEADER) + 4  # 头尾各两个换行


def _parse_summary(text) -> dict:
    """解析 pinned/rolling 分节。无标记的旧摘要返回 structured=False（照常可读）。"""
    value = str(text or "")
    if _PINNED_HEADER not in value and _ROLLING_HEADER not in value:
        return {"pinned": "", "rolling": "", "structured": False, "provenance": ""}
    pinned, rolling, extra = [], [], []
    section = ""
    provenance = ""
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("【固定事实"):
            section = "pinned"
            if "pinned_from_legacy_head" in stripped:
                provenance = "pinned_from_legacy_head"
            continue
        if stripped.startswith(_ROLLING_HEADER):
            section = "rolling"
            continue
        if section == "pinned":
            # 「前缀已满」是我们自己渲染出来的注解，不是 pinned 正文：解析时必须
            # 丢掉，否则它会被当成条目继续留在 pinned 里，下一轮再渲染一次就重复
            # （实测出现两个相同注解，且被算进 pinned 配额）。
            if stripped == _PINNED_FULL_NOTE:
                continue
            pinned.append(line)
        elif section == "rolling":
            rolling.append(line)
        else:
            extra.append(line)
    # 分节标记之前的残留正文归入 pinned，绝不因为解析而丢内容。
    if any(line.strip() for line in extra):
        pinned = extra + pinned
    return {"pinned": "\n".join(pinned).strip(),
            "rolling": "\n".join(rolling).strip(),
            "structured": True, "provenance": provenance}


def _render_summary(pinned, rolling="", provenance="", full=False) -> str:
    pinned = str(pinned or "").strip()
    rolling = str(rolling or "").strip()
    header = (_PINNED_LEGACY_HEADER if provenance == "pinned_from_legacy_head"
              else _PINNED_HEADER)
    parts = [header]
    if pinned:
        parts += ["", pinned]
    if full:
        parts += ["", _PINNED_FULL_NOTE]
    if rolling:
        parts += ["", _ROLLING_HEADER, "", rolling]
    return "\n".join(parts).strip()


def _summary_entries(body) -> list:
    """把一段要点拆成条目：优先空行分隔；单块时再按 `- ` / `· ` 行首拆。

    `· ` 是 `_naive_summary`（确定性兜底）的输出前缀；不拆会让整段兜底摘要
    被当成**一个**条目 —— pinned 只吸走或者一条都装不下，硬事实粒度全错。
    """
    value = str(body or "").strip()
    if not value:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", value) if b.strip()]
    if len(blocks) > 1:
        return blocks
    entries, current = [], []
    for line in value.splitlines():
        if line.lstrip().startswith(("- ", "· ")) and current:
            entries.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current).strip())
    return [entry for entry in entries if entry]


def _legacy_to_pinned(prev, limit):
    """旧摘要首次超限：头部 40% 逐字搬进 pinned（标注来源），剩余全部进 rolling。

    只截取原文，绝不送 LLM 改写；pinned 同时受 60% 配额约束。
    """
    quota = max(1, int(limit * _SUMMARY_PIN_RATIO))
    reserve = len(_render_summary("", "", "pinned_from_legacy_head")) + 2
    budget = max(1, min(int(len(prev) * 0.4), quota - reserve))
    head = _cut_head(prev, budget)
    return head, prev[len(head):].strip()


_HARD_LINE_MAX = 200
_ENTRY_LABEL_RE = re.compile(
    r"^(硬性约束|补充一条硬性要求|用户长期约束|用户长期要求|用户要求"
    r"|实施计划|长期约束|约束|要求|注意)")


def _entry_fingerprint(text) -> str:
    """条目指纹：去掉标点/空白/常见标签与“的”后的小写串，用于跨来源去重。

    同一约束经常以两种措辞到达：一份来自 LLM 摘要（带 `- ` 前缀和不同标点），
    一份来自原始轮次（逐字原文）。只看子串会漏掉“；/。”与“的”这种差异，
    导致同一条约束在 pinned 里占两份配额。`不变` 与 `不得改动` 也按同义归一。
    """
    value = str(text or "").lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    value = _ENTRY_LABEL_RE.sub("", value)
    return value.replace("的", "").replace("版式", "").replace("不变", "不得改动")


def _hard_fact_candidates(turns) -> list:
    """从被弹出窗口的**原始轮次**里确定性提取硬约束（不经过任何 LLM）。

    为什么必须有这一步：pinned 已规定永不进 LLM 输入，但首次压缩时 pinned 还是
    空的，早期约束只存在于被弹出的原始轮次里；如果只从 LLM 摘要里提取，模型
    一旦丢了主语（实测 `项目代号 ORCHID-2718` 被丢），pinned 就永远补不回来。
    按**行**切而不是按句号切，是为了保住“项目代号 + 约束”同一行里的上下文。
    """
    out = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("user") or "")
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if 8 <= len(line) <= _HARD_LINE_MAX and _is_hard_entry(line):
                out.append(line)
    return out


def _add_hard_facts(pinned, rolling, new_sum, limit, provenance, candidates=None):
    """把新增里的硬约束逐字加入 pinned；**只增不减**，配额满时不再淘汰。

    结构性改造 6：判定见 `_is_hard_entry`（专有名词/数字/路径/版本 或 祈使句开头，
    且必须不命中负例）。未命中的新事实仍留在 rolling，不丢内容。

    新不变量：pinned 条目数只增不减（除非 actor=user 走 delete_pinned）。
    因此旧实现里「顶掉最旧非约束条目移入 rolling」的逃生口被移除：配额满时
    新约束留在 rolling，既有条目原样保留。
    """
    quota = max(1, int(limit * _SUMMARY_PIN_RATIO))
    pinned = str(pinned or "").strip()
    full = len(pinned) >= quota
    existing = _summary_entries(pinned)
    existing_fps = [fp for fp in (_entry_fingerprint(item) for item in existing) if fp]
    # 先处理来自原始轮次的候选（更完整、带上下文），再处理 LLM 摘要片段：
    # 这样“项目启动。项目代号…”会先入 pinned，后来的短片段被包含判定跳过，
    # 不需要为了收全信息去改写旧条目（新不变量禁止替换）。
    entries = list(candidates or []) + list(_summary_entries(new_sum))
    for entry in entries:
        entry = entry.strip()
        if not _is_hard_entry(entry):
            continue
        fp = _entry_fingerprint(entry)
        if not fp:
            continue
        # 已有同源条目（含更完整/更碎片）就跳过；绝不替换或删除旧条目。
        if any(fp == old or fp in old or (len(old) >= 8 and old in fp)
               for old in existing_fps):
            continue
        candidate = (pinned + "\n\n" + entry).strip() if pinned else entry
        if len(candidate) <= quota:
            pinned = candidate
            existing.append(entry)
            existing_fps.append(fp)
            rolling = rolling.replace(entry, "", 1).strip()
            continue
        # 配额满：不淘汰、不改写；新约束留在 rolling，内容不丢。
        full = True
    full = full or len(pinned) >= quota
    return pinned, rolling, full


def _fit_rolling(rolling, room, llm=None) -> str:
    """rolling 超预算：先让 LLM 压到 room×0.9，失败则只淘汰最旧的 rolling 段。

    pinned 不参与这里：本函数只接收 rolling，调用方也不会把它传进来。
    """
    rolling = str(rolling or "").strip()
    if room <= 0:
        return ""
    if len(rolling) <= room:
        return rolling
    if llm is not None:
        target = max(1, int(room * 0.9))
        try:
            prompt = (_SUMMARY_ROLLING_PROMPT.replace("{limit}", str(target))
                      .replace("{rolling}", rolling))
            out = llm.chat(
                [{"role": "user", "content": prompt}],
                stream=False, temperature=0.2,
            )
            out = _normalize_summary_blocks(str(out or "").strip())
            if out:
                return _cap_summary(out, target)
        except Exception:  # noqa: BLE001 —— 压缩失败退化为确定性淘汰
            pass
    segments = _summary_entries(rolling)
    if len(segments) > 1:
        omitted = 0
        while len(segments) > 1 and len("\n\n".join(segments)) > room:
            omitted += len(segments.pop(0)) + 2
        body = "\n\n".join(segments)
        if omitted:
            body = (_ROLLING_EVICT_FMT % omitted) + "\n\n" + body
        if len(body) <= room:
            return body
        return _cap_summary(body, room)
    return _cap_summary(rolling, room)


def _fit_summary_text(text, llm, limit) -> str:
    """把「pinned + rolling」整段压进上限：只压 rolling，pinned 原样拼回。

    这是唯一允许接收结构化整段的入口；它先解析，再把 rolling 交给
    `_fit_rolling`，`cap_summary_*` 永远看不到 pinned。
    """
    parsed = _parse_summary(text)
    if not parsed.get("structured"):
        return _cap_summary(text, limit)
    provenance = parsed.get("provenance") or ""
    full = _PINNED_FULL_NOTE in str(text or "")
    pinned = parsed.get("pinned") or ""
    base = len(_render_summary(pinned, "", provenance, full))
    room = max(0, limit - base - _SUMMARY_ROLLING_BLOCK_OVERHEAD)
    rolling = _fit_rolling(parsed.get("rolling") or "", room, llm)
    return _render_summary(pinned, rolling, provenance, full)


def _cap_structured(text, limit) -> str:
    """已废弃：结构化整段不得进入裁剪函数（结构性改造 4 的硬防线）。"""
    raise ValueError("_cap_structured 已废弃：含 pinned 的整段不得裁剪；"
                     "请改用 _fit_summary_text")


def _sanitize_llm_output(text) -> str:
    """结构性改造 3：模型无权产出 pinned —— 任何 `【固定事实】` 段落一律丢弃。

    模型可能把旧摘要整段复述回来；只有代码（确定性硬事实提取 + 结构化
    must-keep）能写 pinned。这里把 pinned 段落连同标题一起删掉，只保留
    标记之后的 rolling（若模型没有输出 rolling 标记，则整段丢弃）。
    """
    value = str(text or "")
    if (_PINNED_HEADER not in value and _PINNED_LEGACY_HEADER not in value
            and "【未完成现场" not in value and "【must-keep" not in value):
        return value.replace(_PINNED_FULL_NOTE, "").strip()
    out = []
    skip = False
    skip_block = False
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("【固定事实"):
            skip = True
            continue
        if stripped.startswith(_ROLLING_HEADER):
            skip = False
            continue
        if stripped == _PINNED_FULL_NOTE:
            continue
        # 模型可能会复述我们喂给它的 must-keep 现场块；那份内容只能由代码
        # 以结构化条目写入 pinned，模型回传的副本一律丢弃（防二次固定）。
        if stripped.startswith("【未完成现场") or stripped.startswith("【must-keep"):
            skip_block = True
            continue
        if skip_block:
            if not stripped:
                skip_block = False
            continue
        if not skip:
            out.append(line)
    return "\n".join(out).strip()


def _merge_summary(prev, new_sum, llm=None, limit=None, source_turns=None):
    """合并摘要：pinned 只写一次、永不重压；rolling 追加后按预算压缩或淘汰。"""
    limit = _resolve_summary_limit(limit)
    prev = str(prev or "").strip()
    new_sum = _sanitize_llm_output(_normalize_summary_blocks(str(new_sum or "").strip()))
    candidates = _hard_fact_candidates(source_turns)
    if not prev:
        if not new_sum and not candidates:
            return ""
        # 首次压缩也必须走硬事实提取：最早期（最远早于任何压缩）的约束正是在这一批
        # 里被弹出窗口的。若直接 `_cap_summary(new_sum)` 保头保尾，约束会被腰斩成
        # 半句（实测 pinned 里出现“…兼容；”而丢了后半条），之后再也补不回来。
        pinned, rolling, full = _add_hard_facts("", "", new_sum, limit, "", candidates)
        if not pinned.strip():
            if not new_sum:
                return ""
            return new_sum if len(new_sum) <= limit else _cap_summary(new_sum, limit)
        room = max(0, limit - len(_render_summary(pinned, "", "", full))
                   - _SUMMARY_ROLLING_BLOCK_OVERHEAD)
        rolling = _fit_rolling(rolling, room, llm)
        out = _render_summary(pinned, rolling, "", full)
        return out if len(out) <= limit else _fit_summary_text(out, llm, limit)
    if not new_sum:
        parsed = _parse_summary(prev)
        if not parsed["structured"]:
            return prev if len(prev) <= limit else _cap_summary(prev, limit)
        return _render_summary(parsed["pinned"], parsed["rolling"],
                              parsed["provenance"], _PINNED_FULL_NOTE in prev)

    parsed = _parse_summary(prev)
    if parsed["structured"]:
        pinned = parsed["pinned"]
        rolling = parsed["rolling"]
        provenance = parsed["provenance"]
        full = _PINNED_FULL_NOTE in prev
    else:
        # 旧摘要未超限时保持原样（三个真实会话零变化）；首次超限才转 pinned。
        if len(prev + "\n" + new_sum) <= limit:
            return _normalize_summary_blocks(prev + "\n" + new_sum)
        pinned, rolling = _legacy_to_pinned(prev, limit)
        provenance = "pinned_from_legacy_head"
        full = False

    rolling = (rolling + "\n\n" + new_sum).strip() if rolling else new_sum
    pinned, rolling, full = _add_hard_facts(
        pinned, rolling, new_sum, limit, provenance, candidates)
    room = max(0, limit - len(_render_summary(pinned, "", provenance, full))
               - _SUMMARY_ROLLING_BLOCK_OVERHEAD)
    rolling = _fit_rolling(rolling, room, llm)
    out = _render_summary(pinned, rolling, provenance, full)
    if len(out) > limit:
        overflow = len(out) - limit
        rolling = _fit_rolling(rolling, max(0, room - overflow), llm)
        out = _render_summary(pinned, rolling, provenance, full)
    return out


def _must_keep_summary(turns) -> str:
    """只为未完成轮次保留可接续字段，已完成轮次仍走原有有损摘要规则。"""
    blocks = []
    for t in turns:
        if not (t.get("resume_context") or t.get("failure_reason")):
            continue
        lines = ["【未完成现场 must-keep】"]
        if t.get("user"):
            lines.append("当前约束/待办：" + _clip_with_mark(str(t.get("user")), 260)[0])
        if t.get("failure_reason"):
            lines.append("failure_reason：" + _clip_with_mark(str(t.get("failure_reason")), 180)[0])
        if t.get("checkpoint_id"):
            lines.append("checkpoint_id：" + str(t.get("checkpoint_id")))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _append_must_keep(summary, turns, limit=None) -> str:
    """把未完成现场写成 **pinned 条目**（结构性改造 5），不再往文本尾部追加。

    条目自带来源与时间，重复的现场按正文去重；pinned 配额满时整条移入 rolling
    头部（内容仍在），绝不对含 pinned 的整段做 `cap_summary_head_tail`。
    """
    limit = _resolve_summary_limit(limit)
    required = _cap_summary(_must_keep_summary(turns), limit)
    if not required:
        return summary
    entry_text = ("【must-keep｜source=resume_context｜at=%s】\n%s"
                  % (datetime.now().isoformat(timespec="seconds"), required))
    state = _summary_state_from_raw({"summary": str(summary or "")})
    pinned = _pinned_text(state["pinned"])
    rolling = state["rolling"]
    if entry_text in pinned or required in pinned:
        rendered = _render_summary(pinned, rolling, state["pinned_provenance"],
                                   state["pinned_full"])
        return _fit_summary_text(rendered, None, limit)
    quota = max(1, int(limit * _SUMMARY_PIN_RATIO))
    candidate = (pinned + "\n\n" + entry_text).strip() if pinned else entry_text
    if len(candidate) <= quota:
        pinned = candidate
        full = False
    else:
        # 配额满：该条目的正文移入 rolling 头部（不丢内容），并标满。
        full = True
        rolling = (entry_text + "\n\n" + rolling).strip() if rolling else entry_text
    rendered = _render_summary(pinned, rolling, state["pinned_provenance"], full)
    return _fit_summary_text(rendered, None, limit)


def _suppress_readded(summary, session_id, project_id=None):
    """按墓碑再过滤一次本次提取出的条目，避免用户删过的事实回流到注入文本。

    存储层（`_store_summary_state`）也会做同样的过滤，但那是落盘时；如果
    `maybe_compact` 直接把未过滤的字符串返回给 Agent，下一轮提示词里仍会出现
    被删除的条目，所以内存路径必须同口径。
    """
    try:
        data = load(session_id, project_id)
    except Exception:  # noqa: BLE001 —— 读不到就按“没有墓碑”处理
        return summary
    tombstones = _normalize_tombstones(data.get("pinned_tombstones"))
    if not tombstones:
        return summary
    state = _summary_state_from_raw({"summary": str(summary or "")})
    entries = state["pinned"]
    fps = {item["fingerprint"] for item in tombstones}
    kept = [entry for entry in entries
            if _pin_fingerprint(entry["text"]) not in fps]
    suppressed = [entry for entry in entries
                  if _pin_fingerprint(entry["text"]) in fps]
    if not suppressed:
        return summary
    # 审计要在这里记：条目在进存储前就被丢掉了，落盘时的合并看不到它。
    try:
        with cross_process_lock(_path(session_id, project_id)):
            data = load(session_id, project_id)
            for entry in suppressed:
                _pinned_audit(data, "readd_suppressed", actor="model",
                              entry_id=entry["id"], origin="summary_extract",
                              text=entry["text"])
                logger.info("pinned_readd_suppressed id=%s origin=summary_extract",
                            entry["id"])
            _write(session_id, data, project_id)
    except Exception:  # noqa: BLE001 —— 审计写不进去也不能挡住压缩
        logger.warning("pinned readd audit write failed", exc_info=True)
    return _render_summary(_pinned_text(kept), state["rolling"],
                           state["pinned_provenance"], state["pinned_full"])


def maybe_compact(session_id, turns, llm=None, trigger_tokens=None, keep_tokens=None,
                  project_id=None, prompt_budget=None):
    """历史 token 占用超过模型预算阈值时，把最早的若干轮压缩进 summary。

    - trigger_tokens：历史总 token 达到该值即压缩（由 Agent 按模型真实窗口 ×
      COMPACT_TRIGGER_RATIO 换算；1M 窗口模型与 16k 本地模型用各自的阈值）；
    - keep_tokens：压缩后最近原文轮次的 token 目标体积（× COMPACT_KEEP_RATIO），
      至少保留 KEEP_TURNS_MIN 轮；
    - 轮数超过 MAX_TURNS 硬上限也必压缩（与 token 阈值是「或」关系）；
    - project_id：摘要读写的项目桶（P3）；缺省 None = 「当前项目」，行为与改动前一致；
    - prompt_budget：本实例模型的单轮 prompt token 预算。给了就按它派生摘要字符
      上限（滚动压缩目标随之缩放）；缺省 None = 沿用模块默认口径（11000），
      行为与改动前一致。
    返回 (turns, summary)；未触发压缩时原样返回。
    """
    turns = list(turns or [])
    trigger = int(trigger_tokens or DEFAULT_TRIGGER_TOKENS)
    keep = int(keep_tokens or DEFAULT_KEEP_TOKENS)
    counter = getattr(llm, "count_tokens", None) or _est_tokens
    limit = _resolve_summary_limit(None, prompt_budget)

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
    new_sum = _llm_summary(old, llm, limit)
    summary = _merge_summary(prev, new_sum, llm, limit, source_turns=old)
    merged = _append_must_keep(summary, old, limit)
    return kept, _suppress_readded(merged, session_id, project_id)


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
        "archived": [dict(t) for t in (data.get("archived") or []) if isinstance(t, dict)],
        # 与主摘要同构：快照里也只存 pinned/rolling 两段（结构性改造 1）。
        **_summary_snapshot_fields(summary or data.get("summary") or ""),
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
        archived = item.get("archived") or []
        turn_count = int(item.get("turn_count", len(turns)) or 0)
        archived_count = len(archived) if isinstance(archived, list) else 0
        out.append({
            "seq": item.get("seq"),
            "at": item.get("at") or "",
            "label": item.get("label") or "",
            "turn_count": turn_count,
            "archived_count": archived_count,
            "total_turn_count": turn_count + archived_count,
            "question": last_user[:200],
        })
    return out


def restore_snapshot(session_id, seq, *, project_id=None, preserve_snapshot_seq=None) -> dict:
    """回到第 seq 个快照那一刻的状态（截断掉之后的轮次）。

    preserve_snapshot_seq 仅供安全回退保留刚写入的备份，不保留其它未来快照。
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
    summary = _summary_snapshot_text(target)
    archived = [dict(t) for t in (target.get("archived") or []) if isinstance(t, dict)]
    data["turns"] = turns
    _set_summary(data, summary)
    data["archived"] = archived
    # 被回退掉的快照一并丢弃；安全回退只额外保留刚写入的当前状态备份。
    data["snapshots"] = [i for i in snaps if isinstance(i, dict) and (
        int(i.get("seq") or 0) <= int(seq)
        or (preserve_snapshot_seq is not None
            and int(i.get("seq") or 0) == int(preserve_snapshot_seq)))]
    if not _write(session_id, data, project_id):
        return {"ok": False, "turns": [], "summary": "", "archived": [],
                "reason": "回退写入失败，会话未更新。"}
    return {"ok": True, "turns": turns, "summary": summary, "archived": archived,
            "reason": "已回到第 %s 个快照（%s 轮）。" % (seq, len(turns) + len(archived))}


def rewind_with_backup(session_id, seq, *, project_id=None) -> dict:
    """先保存当前状态，再执行有损回退；备份可用于之后无损分叉。"""
    data = load(session_id, project_id=project_id)
    turns = data.get("turns") or []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        status = str(turn.get("status") or "").strip().lower()
        if (status in {"streaming", "running", "pending", "in_progress"}
                or turn.get("completed") is False or turn.get("done") is False):
            return {"ok": False, "reason": "会话里有未完成的轮次，请完成或停止后再回退。"}
    snaps = data.get("snapshots") or []
    if not any(isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0)
               for item in snaps):
        return {"ok": False, "reason": "找不到 seq=%s 的可用快照，未执行回退。" % seq}
    label = "回退前自动保存（目标 #%s）" % seq
    if not snapshot(session_id, turns, label=label,
                    summary=data.get("summary") or "", project_id=project_id):
        return {"ok": False, "reason": "回退前快照保存失败，未执行回退。"}
    backup_seq = max((int(item.get("seq") or 0) for item in snapshots(session_id, project_id)),
                     default=0)
    result = restore_snapshot(session_id, seq, project_id=project_id,
                              preserve_snapshot_seq=backup_seq)
    if result.get("ok"):
        result["backup_seq"] = backup_seq
    return result


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
        # 按快照分叉：归档也要退回该快照那一刻，否则 fork 出的会话会带上
        # 原会话的完整归档，与快照 turns 拼出重复/错位的完整历史。
        data["archived"] = got.get("archived") or []
    data["session_id"] = new_id
    data["turns"] = turns
    _set_summary(data, summary)
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
        return {"ok": False, "turns": [], "summary": "", "archived": [], "reason": "没有可用快照。"}
    for item in snaps:
        if isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0):
            return {"ok": True,
                    "turns": [dict(t) for t in (item.get("turns") or [])
                              if isinstance(t, dict)],
                    "summary": _summary_snapshot_text(item),
                    "archived": [dict(t) for t in (item.get("archived") or [])
                                 if isinstance(t, dict)],
                    "reason": ""}
    return {"ok": False, "turns": [], "summary": "", "archived": [],
            "reason": "找不到 seq=%s 的快照。" % seq}


def list_sessions(limit=50, project_id=None, include_subagents=False):
    """列出会话摘要（用于 /api/sessions 概览）。

    **全局唯一**：一个 session_id 就是一个会话，读取范围 = 全局扁平目录 ∪
    所有旧项目分桶。同一 session_id 在多处出现时**全局扁平优先**（迁移后的
    唯一真源），稳定去重；再按 updated_at 倒序取前 limit。

    `project_id` 是**归属过滤**（丙方案，2026-10-11），不是存储路径：
    会话存储已全局化，但「用户在项目 B 里不该看到 A 的会话」这条产品语义还在。
    以前靠按项目分桶天然做到；现在改成读 `owner_project_id` 字段。

    * 传了 pid -> 只返回归属为 pid 的会话；
    * 不传 -> 全部返回。

    **归属从哪来（2026-10-11 收窄）**：先取文件里的 `owner_project_id`，
    缺失时回退到**旧分桶的目录名**（`prj-<hex>`）。此前是「无归属一律放行」，
    那条豁免把 308 个没有该字段的旧副本一起放行了 —— 实测只有 4 个会话的
    项目，列表里出现 336 条，过滤形同虚设。归属本来就在目录名里，读出来即可。
    真正无归属的（`golden-*` 基线夹具，住在扁平目录、无对应桶）按用户拍板
    **不再出现在项目列表里**，但不传 pid 时仍全都可见 —— 数据不会消失。

    ⚠⚠ 为什么无归属的老会话在显式过滤时**也要返回**：P2 的明确意图是
    「确保迁移后用户既有历史不消失（用户原始 Bug 不复发）」
    （见 `16b1010` 的提交说明与 `tests/test_projects_qa.py::B10`）。
    第一版丙方案在这里把无归属会话一刀切掉 —— 那条既有判据立刻红，
    证明那是个**真回归**：升级一次，历史会话全从列表里消失。

    代价是：项目 B 的列表里会混进无归属的老会话。这比"历史消失"好得多，
    而且是一次性的 —— 跑过 `backfill_owner` 之后就没有无归属会话了。
    结果行里带 `owner_project_id`，前端若想区分可以自己判空。
    """
    seen = {}
    want_pid = str(project_id or "").strip() or None
    for d in _bucket_dirs() + _legacy_bucket_dirs():
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        # 旧分桶目录名本身就是归属（`prj-<hex>`）。`bind_code_root` 是丙方案
        # 之后才写的，所以这些历史文件里**没有** `owner_project_id` 字段 ——
        # 但归属并没有丢，它就在目录名里。不读出来的话，缺字段的会话会被
        # 当成「无归属 → 放行」，把归属过滤彻底淹没（实测：只有 4 个会话的
        # 项目，列表里出现 336 条）。
        dir_owner = ""
        base = os.path.basename(os.path.normpath(d))
        if base.startswith("prj-"):
            dir_owner = base
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = _read_raw(os.path.join(d, name))
            if not isinstance(raw, dict):
                continue
            sid = raw.get("session_id") or name[:-5]
            # 子 agent 会话走同一落盘路径，但不进主会话历史：
            # 它们由工作台的 agent 卡片单独打开（/api/agents/{id}/conversation）。
            if not include_subagents and is_subagent_session(sid):
                continue
            if sid in seen:
                continue  # 全局先遍历 → 全局条目优先
            # 归属：**文件里的字段优先**，缺失时回退到目录名。
            owner = str(raw.get("owner_project_id") or "").strip() or dir_owner
            # 归属过滤（丙）。**与我要的不一致就排除** —— 包括「无归属」。
            #
            # ⚠ 此前是「有归属且不是我要的才排除」，无归属一律放行。那条豁免
            # 是为保 B10 的用户历史不消失，但它把 308 个没有该字段的旧副本
            # 一起放行了，过滤形同虚设（实测：只有 4 个会话的项目，列表里
            # 出现 336 条）。
            # 现在归属从目录名读出来了，旧历史照样读得到（B10 仍成立）；
            # 而真正无归属的（`golden-*` 基线夹具，按用户拍板）不再出现。
            # ⚠ 两条判据盯着这次改动：`tests/test_owner_filter_narrow.py`
            #   与 `tests/test_projects_qa.py::B10`。前者是收窄，后者是读穿透。
            if want_pid and owner != want_pid:
                continue
            turns = raw.get("turns") or []
            archived = raw.get("archived") or []
            # 完整逐轮 = 归档 + 工作窗口：压缩只收缩喂 LLM 的 turns，列表里的轮数
            # 与标题（首条用户问题）都必须按完整历史算，否则长会话会「轮数变少、
            # 标题跳到中途那一问」。
            all_turns = [t for t in archived if isinstance(t, dict)] + \
                        [t for t in turns if isinstance(t, dict)]
            first_user = ""
            for turn in all_turns:
                if isinstance(turn, dict) and str(turn.get("user") or "").strip():
                    first_user = _clean_legacy_prompt(turn.get("user", "")).strip()
                    break
            preview = " ".join(first_user.split())
            seen[sid] = {
                "session_id": sid,
                "turns": len(all_turns),
                "has_summary": bool(raw.get("pinned") or raw.get("rolling")
                                    or raw.get("summary")),
                "updated_at": raw.get("updated_at") or "",
                # 归属项目（丙）。**回退到桶目录名** —— 见上面 dir_owner。
                # 读穿透拿到的旧记录因此也带着归属，不再是一串空串。
                "owner_project_id": owner,
                # 首条用户问题作为可读标题；不保存额外数据，兼容旧会话文件。
                "title": preview[:72] or "未命名对话",
                "preview": preview[:180],
            }
    rows = list(seen.values())
    rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return rows[:max(1, int(limit))]


def migrate_summaries(limit=_SUMMARY_MAX_CHARS) -> list:
    """把旧会话的整段 `summary` 迁成 pinned/rolling 两字段（幂等、可回滚）。

    规则（结构性改造 1）：
    - 已是新格式（有 `pinned`/`rolling` 字段）→ 跳过，第二次运行返回空列表；
    - 旧格式且未超限 → `pinned=[]`、`rolling=原文`（零内容变化，只是落盘换形）；
    - 旧格式且超限 → 头部 40% 逐字搬进 pinned（来源 `pinned_from_legacy_head`），
      其余进 rolling；**不调用 LLM**，pinned 逐字来自原文。
    - 旧格式但已带分节标记 → 按标记拆成 pinned 条目 + rolling。

    写盘前用 `shutil.copy2` 备份原文件；只动摘要字段，不动 updated_at/turns。
    """
    changes = []
    d = _bucket_dirs()[0]
    if not os.path.isdir(d):
        return changes
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        with cross_process_lock(path):
            raw = _read_raw(path)
            if not isinstance(raw, dict):
                continue
            if isinstance(raw.get("pinned"), (list, str)) or raw.get("rolling") is not None:
                # 已迁移的会话：只补墓碑字段缺失（老会话没有 pinned_tombstones）。
                # pinned=[] 的老会话不升格；补过一次后第二次运行不再命中。
                if (isinstance(raw.get("pinned"), list) and raw.get("pinned")
                        and "pinned_tombstones" not in raw):
                    backup = path + ".bak-" + stamp
                    suffix = 1
                    while os.path.exists(backup):
                        backup = "%s.bak-%s-%d" % (path, stamp, suffix)
                        suffix += 1
                    shutil.copy2(path, backup)
                    raw["pinned_tombstones"] = []
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(raw, f, ensure_ascii=False, indent=1)
                    os.replace(tmp, path)
                    changes.append({"path": path, "backup": backup,
                                    "mode": "tombstone_backfill",
                                    "before": len(str(raw.get("summary") or "")),
                                    "after": len(str(raw.get("summary") or "")),
                                    "pinned_entries": len(raw.get("pinned") or []),
                                    "pinned_chars": len(_pinned_text(raw.get("pinned"))),
                                    "rolling_chars": len(str(raw.get("rolling") or "")),
                                    "truncated": False, "dropped": 0})
                continue                      # 已迁移
            current = str(raw.get("summary") or "")
            if not current:
                continue
            state = _summary_state_from_raw({"summary": current})
            if state["structured"]:
                mode = "structured"
                pinned, rolling = _pinned_text(state["pinned"]), state["rolling"]
                provenance = state["pinned_provenance"] or "pinned_from_legacy_head"
                full = state["pinned_full"]
            elif len(current) <= limit:
                mode = "split"
                pinned, rolling = "", current
                provenance, full = "", False
            else:
                mode = "truncate"
                pinned, rolling = _legacy_to_pinned(current, limit)
                provenance, full = "pinned_from_legacy_head", False
                room = max(0, limit - len(_render_summary(pinned, "", provenance, False))
                           - _SUMMARY_ROLLING_BLOCK_OVERHEAD)
                rolling = _fit_rolling(rolling, room, None)
            after = _render_summary(pinned, rolling, provenance, full)
            if len(after) > limit:
                after = _fit_summary_text(after, None, limit)
                parsed = _parse_summary(after)
                pinned, rolling = parsed["pinned"], parsed["rolling"]
                provenance = parsed["provenance"]
                full = _PINNED_FULL_NOTE in after
            backup = path + ".bak-" + stamp
            suffix = 1
            while os.path.exists(backup):
                backup = "%s.bak-%s-%d" % (path, stamp, suffix)
                suffix += 1
            shutil.copy2(path, backup)
            raw.pop("summary", None)
            raw["pinned"] = [_pinned_entry(b, source="legacy_head" if provenance
                                           else "rolling_only")
                             for b in _summary_entries(pinned)]
            raw["rolling"] = rolling
            raw["pinned_full"] = full
            raw["pinned_provenance"] = provenance
            raw["summary_structured"] = bool(pinned.strip()) or mode != "split"
            # 迁移一次即补齐墓碑字段，保证第二次运行 0 变更。
            raw.setdefault("pinned_tombstones", [])
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            changes.append({"path": path, "backup": backup, "mode": mode,
                            "before": len(current), "after": len(after),
                            "pinned_entries": len(raw["pinned"]),
                            "pinned_chars": len(pinned),
                            "rolling_chars": len(rolling),
                            "truncated": len(after) < len(current),
                            "dropped": max(0, len(current) - len(after))})
    return changes


if __name__ == "__main__":
    import sys
    if "--migrate-summaries" in sys.argv:
        rows = migrate_summaries()
        if not rows:
            print("没有需要迁移的摘要（已是 pinned/rolling 两字段）。")
        for row in rows:
            print("%s: [%s] 原 %d 字 → 新 %d 字；pinned %d 条/%d 字；"
                  "rolling %d 字；截断 %s；备份 %s"
                  % (row["path"], row["mode"], row["before"], row["after"],
                     row["pinned_entries"], row["pinned_chars"],
                     row["rolling_chars"], "是" if row["truncated"] else "否",
                     row["backup"]))


def delete(session_id, project_id=None) -> bool:
    """删除会话：全局文件与所有旧项目分桶里的同名文件一并删除。

    一个 session_id 就是一个会话，所以删要删干净 —— 只删全局那份会留下
    分桶里的旧文件，下次 load 的读穿透又把它捞回来（用户看到"删了又出现"）。
    """
    ok = True
    targets = {_path(session_id, project_id), _legacy_path(session_id)}
    for d in _legacy_bucket_dirs():
        targets.add(os.path.join(d, _slug(session_id) + ".json"))
    for p in targets:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            ok = False
    return ok
