# -*- coding: utf-8 -*-
"""外壳与子代理这两层的**跨段共享可变状态**载体。

为什么需要它：拆分 `_run_shell` / `_run_child` 时，有些变量同时被多个阶段读写
（事件泵 / 收尾落库 / 结论报告）。不收进载体的话，每抽一段就要手工搬运一批
变量 —— 漏搬一个就是**静默的行为变化**。

约定（与 `agent_runtime/turn_state.py` 里的 `_TurnState` 一致）：
- 字段名与拆分前**一字不差**（只是挂到了载体上）；
- 载体**不带方法、不做校验、不改变量含义**；
- 只有真正跨阶段共享的字段才进来。

注意（`_ShellState` 特有）：外壳是**生成器**，事件在 `yield` 出去的过程中被
消费者打断（客户端断连）时，生成器里的局部变量会随栈一起消失 —— 所以跨 yield
存活的状态必须挂在载体上，不能只当局部变量用。

改这块时记得看 docs/backend-decomposition.md 的 §6 / §13。
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ShellState:
    """一次问答外壳（`Agent._run_shell`）的跨段共享状态。"""

    # ---- 本轮问答的身份 ------------------------------------------------
    question: str = ""
    turn: Any = None
    deadline: Any = None

    # ---- 结局 ----------------------------------------------------------
    aborted: bool = False
    error: str = ""
    final_text: str = ""
    inner: Any = None

    # ---- 恢复现场（断点续跑）--------------------------------------------
    history_start: int = 0
    recovery_events: list = field(default_factory=list)
    checkpoint_id: Any = None
    journal_state: Any = None
    journal_token: Any = None


@dataclass
class _ChildState:
    """一次子代理运行（`Agent._run_child`）的跨段共享状态。"""

    # ---- 派单输入 ------------------------------------------------------
    role: str = ""
    role_name: str = ""
    spec: Any = None
    ad_hoc_role: bool = False
    task: Any = None
    context: Any = None
    persona: str = ""
    tool_allowlist: Any = None
    mcp_policy: str = "auto"
    max_steps: Any = None
    llm: Any = None
    turn: Any = None
    reflect: bool = True
    step_sink: Any = None
    delegation_ticket: str = ""
    plan_id: str = ""
    task_id: str = ""
    interactive_session: str = ""

    # ---- 工具策略与子代理本体 --------------------------------------------
    child_llm: Any = None
    child: Any = None
    question: str = ""
    cap: int = 0
    allowed: list = field(default_factory=list)
    policy: str = "auto"
    mcp_tools: list = field(default_factory=list)

    # ---- 这一轮跑出来的现场 ----------------------------------------------
    used: int = 0
    final_text: str = ""
    final_degraded: bool = False
    thoughts: list = field(default_factory=list)
    reflections: list = field(default_factory=list)
    last_obs: str = ""
    trace_steps: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    traj: list = field(default_factory=list)
    pending: Any = field(default_factory=deque)
    needs_confirm: dict = field(default_factory=dict)
    file_changes: list = field(default_factory=list)
    seen_file_changes: set = field(default_factory=set)
