"""逐轮 trace + token 账本。

每个 Agent 回合（一次 /api/chat）落一条 JSONL 记录，字段：
  turn_id / session_id / provider / model / route / messages 哈希 / messages 条数 /
  工具调用序列 / tokens in-out / LLM 调用次数与耗时 / 各工具步延迟 /
  finish_reason / 是否被客户端断连中止 / 总耗时。

设计原则（重要）：
- **只记元数据，不记正文**：prompt 与回答只记字符数，正文永不落盘（避免把用户代码/密钥写进日志）。
- append-only JSONL，超过上限自动轮转为 `<file>.1`，不会无限增长。
- 默认开启；设 `DOCMIND_TRACE=0` 可整体关闭（写盘失败一律静默降级，不影响主流程）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime

# 并行工具批次会从多个线程写同一个 Turn，故对累加操作加锁（计数很小，开销可忽略）

from config import STATE_ROOT, state_path

TRACE_FILE = state_path("DOCMIND_TRACE_FILE", os.path.join(STATE_ROOT, ".docmind_traces.jsonl"))
TRACE_ENABLED = os.getenv("DOCMIND_TRACE", "1") != "0"
MAX_BYTES = int(os.getenv("DOCMIND_TRACE_MAX_BYTES", str(8 * 1024 * 1024)))

_lock = threading.Lock()

_USAGE_IN_KEYS = ("prompt_tokens", "in", "prompt_eval_count", "input_tokens")
_USAGE_OUT_KEYS = ("completion_tokens", "out", "eval_count", "output_tokens")
# prompt caching 字段（平铺键 + 嵌套 prompt_tokens_details.* 两种口径都兼容）
_CACHE_READ_KEYS = ("cache_read_input_tokens", "cache_read_tokens", "cached_tokens")
_CACHE_CREATION_KEYS = ("cache_creation_input_tokens", "cache_creation_tokens")


def _pick(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _pick_cache(d, flat_keys):
    """取缓存 token 计数：先查顶层平铺键，再查 prompt_tokens_details / input_tokens_details 嵌套。"""
    v = _pick(d, flat_keys)
    if v:
        return v
    details = d.get("prompt_tokens_details") or d.get("input_tokens_details") or {}
    if isinstance(details, dict):
        for k in flat_keys:
            dv = details.get(k)
            if isinstance(dv, (int, float)):
                return int(dv)
    return 0


def messages_hash(messages) -> str:
    """对整段消息做稳定哈希（只用于判断"同样输入"与去重，不可逆）。"""
    h = hashlib.sha256()
    for m in messages or []:
        h.update(str(m.get("role", "")).encode("utf-8", "ignore"))
        h.update(b"\x00")
        content = m.get("content")
        if isinstance(content, str):
            h.update(content.encode("utf-8", "ignore"))
        elif content is not None:
            h.update(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8", "ignore"))
        h.update(b"\x01")
    return h.hexdigest()[:16]


class Turn:
    """一个 Agent 回合的埋点累加器。由 agent.Agent.run 创建、on-finish 落盘。"""

    def __init__(self, session_id="default", provider="", model="", route=None, question=""):
        self.turn_id = uuid.uuid4().hex[:12]
        self.session_id = session_id or "default"
        self.provider = provider
        self.model = model
        self.route = route
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.question_chars = len(question or "")
        self.messages_hash = None
        self.messages_count = 0
        # 本回合实际注入的记忆分层块（P0 只有 L0 宪法）；空列表不写进 record，
        # 保证记忆 v2 关闭时 trace 与旧版逐字节一致。
        self.injection = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0      # prompt caching 命中（按折扣价计费）
        self.cache_creation_tokens = 0  # prompt caching 首次写入（按溢价计费）
        self.llm_calls = 0
        self.llm_ms = 0
        self.steps = []
        self.actions = []
        self.reflections = 0
        self.outcome = None      # 回合结局：completed/max_steps/truncated/evidence_fallback/...
        self.verified = False    # 写后自验证是否通过（Phase 1 闭环；仅当回合含写操作且校验通过才置 True）
        self.verification_targets = {}  # In-memory only: latest evidence per written file.
        # ---- Provenance 供给层（纯内存·fail-closed·不进 to_record，仿 verification_targets）----
        # 记录本回合哪些数字是 derive 工具（calculate/python_exec）产出的，按池密度分级：
        #   numbers_strong —— 确定性强归因（calculate 单值 / 低密度且非源码 echo 的 python_exec）；
        #   numbers_weak   —— 工具确实吐出了这些数，但稠密/源码 echo，**不授予正向背书**（fail-closed）。
        # superseded_this_turn —— 本轮 memory_save 真作废了某条旧记忆（tools.py 回执含 "superseded"）。
        # had_derive_tool —— 本轮确有 derive 工具跑过（含 calculate 出「成立/不成立」这种无数字结论），
        #   供消费方区分「算了但没留数」与「压根没算」。
        # 消费方（CALC②徽章/W-C信任重排/W-A1）在回合内即时读此结构；本基建只提供信号、不改任何外显。
        self.tool_evidence = {"numbers_strong": [], "numbers_weak": [],
                              "superseded_this_turn": False, "had_derive_tool": False}
        # ---- Phase 3 经验记录辅助字段（均由 agent._run 在推理循环中写入）----
        self.failure_count = 0       # 本回合失败总次数（单调累计，成功不清零）
        self.max_tool_streak = 0     # 单工具连续失败的最大次数
        self.max_consec_failures = 0 # 全局连续失败的最大长度（fail_total 的历史峰值）
        self.last_failure = None     # 最近一次失败观察（已截断，脱敏后用于经验决策）
        self.last_failure_tool = ""  # 产生上次失败的工具（钩子/经验归属用，避免张冠李戴）
        # 测试命令证据：通过后仍需确认之后没再改文件，才接入收尾验证门。
        self.test_command_ok = False
        self.test_command = ""
        self.test_command_targets = {}
        self.experience = None       # 回合收尾经验记录结果 {recorded, outcome}
        self.finish_reason = None
        self.final_chars = 0
        self.cost_cny = 0.0     # 本轮按 provider 计价的花费（元）
        self.task_budget = None  # 主 Agent + 子 Agent + 辅助调用的共享预算快照
        self.error = None
        self.aborted = False
        self._t0 = time.monotonic()
        self._done = False
        self._lk = threading.Lock()

    # ---- 采集 ----
    def snapshot_prompt(self, messages):
        self.messages_hash = messages_hash(messages)
        self.messages_count = len(messages or [])
        return self.messages_hash

    def add_injection(self, blocks):
        """记录命中的记忆块；只留摘要字段与截断文本，供面板对账与审计。"""
        try:
            self.injection = [
                {"layer": str(block.get("layer") or ""),
                 "chars": int(block.get("chars") or 0),
                 "hash": str(block.get("hash") or ""),
                 "version": block.get("version"),
                 "text": str(block.get("text") or "")[:2000]}
                for block in (blocks or []) if isinstance(block, dict)
            ]
        except Exception:  # noqa: BLE001 —— 埋点失败绝不影响回合
            self.injection = []

    def add_usage(self, usage):
        """累加一次 LLM 调用的 token 用量（键名兼容 OpenAI / Ollama / Anthropic）。"""
        if not usage:
            return
        with self._lk:
            self.prompt_tokens += _pick(usage, _USAGE_IN_KEYS)
            self.completion_tokens += _pick(usage, _USAGE_OUT_KEYS)
            self.cache_read_tokens += _pick_cache(usage, _CACHE_READ_KEYS)
            self.cache_creation_tokens += _pick_cache(usage, _CACHE_CREATION_KEYS)

    def llm_step(self, latency_ms, finish_reason=None):
        self.llm_calls += 1
        self.llm_ms += int(latency_ms or 0)
        if finish_reason:
            self.finish_reason = finish_reason

    def tool_step(self, action, arg="", latency_ms=0, obs="", ok=True):
        with self._lk:
            self.actions.append(action)
            self.steps.append({
                "i": len(self.steps),
                "action": action,
                "arg_chars": len(arg or ""),
                "latency_ms": int(latency_ms or 0),
                "obs_chars": len(obs or ""),
                "ok": bool(ok),
            })

    def note_reflection(self):
        self.reflections += 1

    def add_tool_evidence(self, *, numbers=(), strong=True, superseded=False, derive=False):
        """累加一条 derive 工具证据（纯内存·不进 record）。并行批次会从多线程写同一
        Turn，故与其余累加操作一样持锁。numbers 归一化为字符串去重后按 STRONG/WEAK 分桶；
        superseded/derive 单向 OR（一旦本轮有取代/derive 工具即置真）。"""
        if not numbers and not superseded and not derive:
            return
        with self._lk:
            if superseded:
                self.tool_evidence["superseded_this_turn"] = True
            if derive:
                self.tool_evidence["had_derive_tool"] = True
            bucket = self.tool_evidence["numbers_strong" if strong else "numbers_weak"]
            seen = set(self.tool_evidence["numbers_strong"]) | set(self.tool_evidence["numbers_weak"])
            for n in numbers:
                s = str(n)
                if s and s not in seen:
                    bucket.append(s)
                    seen.add(s)

    # ---- 收尾 ----
    def finish(self, reason=None, final_text="", error=None, aborted=False):
        if self._done:
            return
        self._done = True
        self.finish_reason = reason or self.finish_reason or "completed"
        self.final_chars = len(final_text or "")
        self.error = error
        self.aborted = bool(aborted)

    def to_record(self):
        record = {
            "turn_id": self.turn_id,
            "ts": self.started_at,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "route": self.route,
            "question_chars": self.question_chars,
            "messages_hash": self.messages_hash,
            "messages_count": self.messages_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_cny": round(self.cost_cny, 8),
            "task_budget": self.task_budget,
            "llm_calls": self.llm_calls,
            "llm_ms": self.llm_ms,
            "steps": self.steps,
            "actions": self.actions,
            "n_steps": len(self.steps),
            "reflections": self.reflections,
            "outcome": self.outcome or self.finish_reason or "completed",
            "verified": self.verified,
            "verification": {
                "targets": len(self.verification_targets),
                "passed": sum(bool(v) for v in self.verification_targets.values()),
                "status": ("passed" if self.verified else "unverified")
                          if self.verification_targets else "not_run",
            },
            "failure_count": self.failure_count,
            "max_tool_streak": self.max_tool_streak,
            "experience": self.experience,
            "finish_reason": self.finish_reason,
            "final_chars": self.final_chars,
            "aborted": self.aborted,
            "error": self.error,
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
        }
        if self.injection:
            record["injection"] = self.injection
        return record


# ---------------------------------------------------------------------------
# 落盘 / 读取
# ---------------------------------------------------------------------------
def _rotate_if_needed():
    try:
        if os.path.isfile(TRACE_FILE) and os.path.getsize(TRACE_FILE) > MAX_BYTES:
            bak = TRACE_FILE + ".1"
            if os.path.isfile(bak):
                os.remove(bak)
            os.replace(TRACE_FILE, bak)
    except OSError:
        pass


def record(rec) -> bool:
    """追加一条 trace。任何异常都静默降级（埋点绝不能影响主流程）。"""
    if not TRACE_ENABLED:
        return False
    try:
        with _lock:
            _rotate_if_needed()
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def recent(limit=50):
    """返回最近 limit 条 trace（按时间正序）。损坏行跳过，不抛异常。"""
    if not os.path.isfile(TRACE_FILE):
        return []
    try:
        with _lock:
            with open(TRACE_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summary():
    """全账本聚合：回合数、token 总量、平均耗时、失败/中止计数、按 provider 分解。"""
    rows = recent(limit=100000)
    agg = {
        "turns": len(rows),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "total_cost_cny": 0.0,
        "cache_saved_cny": 0.0,
        "avg_elapsed_ms": 0,
        "aborted": 0,
        "errors": 0,
        "by_provider": {},
    }
    if not rows:
        return agg
    elapsed = 0
    for r in rows:
        p = int(r.get("prompt_tokens") or 0)
        c = int(r.get("completion_tokens") or 0)
        cr = int(r.get("cache_read_tokens") or 0)
        cc = int(r.get("cache_creation_tokens") or 0)
        agg["prompt_tokens"] += p
        agg["completion_tokens"] += c
        agg["cache_read_tokens"] += cr
        agg["cache_creation_tokens"] += cc
        agg["total_tokens"] += p + c
        cost = float(r.get("cost_cny") or 0.0)
        agg["total_cost_cny"] += cost
        elapsed += int(r.get("elapsed_ms") or 0)
        if r.get("aborted"):
            agg["aborted"] += 1
        if r.get("error"):
            agg["errors"] += 1
        key = r.get("provider") or "?"
        slot = agg["by_provider"].setdefault(key, {
            "turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tokens": 0, "cost_cny": 0.0, "model": r.get("model") or "",
            "provider": key, "cache_saved_cny": 0.0,
        })
        slot["turns"] += 1
        slot["prompt_tokens"] += p
        slot["completion_tokens"] += c
        slot["cache_read_tokens"] += cr
        slot["cache_creation_tokens"] += cc
        slot["tokens"] += p + c
        slot["cost_cny"] += cost
        # 逐行用**这一行自己的 model** 算节省，而不是用该 provider 最后见到的 model：
        # 同一个 provider 可能换过模型（deepseek-chat → deepseek-reasoner），
        # 统一用最后一个会把之前的行按错价折算。
        try:
            import pricing as _pricing
            saved = _pricing.cache_saved_cny(key, r.get("model") or "", p, cr, cc)
        except Exception:  # noqa: BLE001 - 节省只是附注，取不到不影响统计
            saved = 0.0
        slot["cache_saved_cny"] += saved
        agg["cache_saved_cny"] += saved
    agg["total_cost_cny"] = round(agg["total_cost_cny"], 8)
    agg["cache_saved_cny"] = round(agg["cache_saved_cny"], 8)
    for slot in agg["by_provider"].values():
        slot["cost_cny"] = round(slot["cost_cny"], 8)
        slot["cache_saved_cny"] = round(slot["cache_saved_cny"], 8)
    # 费用为 0 时补上计价状态：这个 0 到底是「免费」「本地」还是「没配置单价」，
    # 三者在界面上原本完全一样，用户无从分辨（见 pricing.pricing_state 的说明）。
    for key, slot in agg["by_provider"].items():
        try:
            import pricing as _pricing
            slot["pricing_state"] = _pricing.pricing_state(
                slot.get("provider") or key, slot.get("model") or "").get("state", "")
        except Exception:  # noqa: BLE001 - 状态只是附注，取不到不影响统计
            slot["pricing_state"] = ""
    agg["avg_elapsed_ms"] = int(elapsed / len(rows))
    return agg


def clear() -> bool:
    try:
        with _lock:
            if os.path.isfile(TRACE_FILE):
                os.remove(TRACE_FILE)
            bak = TRACE_FILE + ".1"
            if os.path.isfile(bak):
                os.remove(bak)
        return True
    except OSError:
        return False
