# -*- coding: utf-8 -*-
"""Agent 一轮问答（_run）里的**跨块共享可变状态**载体。

为什么需要它：拆分 _run 时，有些变量同时被多个阶段读写（工具执行段、
格式纠偏段、续写纠偏段、终态兜底段、日志恢复段）。不收进载体的话，
每抽一段就要手工搬运一批变量 —— 漏搬一个就是**静默的行为变化**。

约定（刻意保持克制）：
- 字段名与拆分前**一字不差**；
- 载体**不带方法、不做校验、不改变量含义**；
- 只有真正跨阶段共享的字段才进来（字段表是 AST 实测出来的，不是拍脑袋列的）。

改这块时记得看 docs/backend-decomposition.md 的 §6 与 §12。
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _TurnState:
    """一轮问答（_run）的跨阶段共享状态。"""

    # ---- 消息与本轮轨迹 ------------------------------------------------
    head: list[dict] = field(default_factory=list)
    trail: list[dict] = field(default_factory=list)
    question: str = ""
    turn: Any = None

    # ---- 本轮模型的累积输出 --------------------------------------------
    acc: str = ""
    parsed: dict = field(default_factory=dict)
    truncated: bool = False
    has_real_final: bool = False
    native_calls_this_turn: bool = False

    # ---- 工具执行账本 ---------------------------------------------------
    executed: set = field(default_factory=set)
    tool_steps: int = 0
    repeats: int = 0
    tool_fail_streak: dict = field(default_factory=dict)
    web_family_failures: dict = field(default_factory=dict)
    _permanent_failures: dict = field(default_factory=dict)
    fail_total: int = 0
    failures: int = 0
    last_action: Any = None
    last_obs: Any = None
    last_exec_obs: Any = None
    ran_python_exec: bool = False

    # ---- 纠偏与强制收尾 -------------------------------------------------
    nudges: int = 0
    format_stalls: int = 0
    forced_finals: int = 0
    forced_final_reason: str = ""
    final_continuations: int = 0
    final_text: str = ""
    iterations: int = 0
    _journal_hist_len: int = 0
    evidence_nudges: int = 0
    verbatim_dump_nudges: int = 0
    evidence_dump_nudges: int = 0
    observation_echo_nudges: int = 0
    continuation_nudges: int = 0
    marker_echoes: int = 0
    plan_emitted: bool = False
    _last_ctx_at: float = 0.0
    _resume_tip: str = ""

    # ---- 证据与文件改动 -------------------------------------------------
    evidence: list = field(default_factory=list)
    audit_steps: list = field(default_factory=list)
    _file_changes: list = field(default_factory=list)
    _seen_file_changes: set = field(default_factory=set)

    # ---- 导航预算 -------------------------------------------------------
    nav_free_used: int = 0

    # ---- 一轮（主循环的一次迭代）里的量 --------------------------------
    # 「一轮」= 主循环的一次迭代 = 准备 → 取模型输出 → 决定下一步。
    # 下面这些是**轮内**量：每轮刷新，但跨三个阶段传递。
    messages: list = field(default_factory=list)
    finish_reason: Any = None
    native_override: Any = None
    use_tools: bool = False
    tools_arg: Any = None
    eff_thinking: bool = False
    eff_level: Any = None
    pending_final: Any = None
    continuation_incomplete: bool = False

    # ---- 这一轮问答的固定配额（每轮问答算一次，轮内不变）----------------
    tool_step_limit: int = 0
    observation_limit: int = 0
    lookup_output_limit: int = 0
    max_nudges: int = 0
    max_final_continuations: int = 0
    narration_filter: Any = None

    # ---- 当前这次工具调用的上下文（工具段各阶段共享）----------------------
    action_name: str = ""
    action_arg: str = ""
    sig: Any = None
    _nav_is_free: bool = False
    _tool_ok: bool = False
    _result: Any = None
    obs: Any = None
    _duration_ms: Any = None
    _t_tool: Any = None
    obs_images: Any = None
