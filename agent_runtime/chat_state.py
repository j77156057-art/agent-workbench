# -*- coding: utf-8 -*-
"""`POST /api/chat`（`api.py:chat`）这一层的**跨段共享可变状态**载体。

## 为什么单独一个文件

`agent.py` 的 `_TurnState`（`agent_runtime/turn_state.py`）管的是**一轮问答**（`_run`）
的内部状态；这一份管的是**一次 HTTP 问答请求**（`api.py:chat` → `event_stream`）的
内部状态。两者生命周期不同、字段不重叠，放一起会互相污染。

## 字段名的约定

**与拆分前一字不差**（连下划线都照抄）。理由是源码钉桩型守卫查的正是
`_file_changes.append` / `question = spec["hint"]` 这类**原样形状**——载体一旦把
名字"洗"干净（例如 `_file_changes` → `file_changes`），那些守卫就会当场失明，
而失明是静默的。

## 两个载体的分工

- `_ChatState`：**前段 → 后段**的接口，以及前段各方法之间跨界的那几个变量。
- `_ChatStreamState`：SSE 流内部跨块共享的那几个（worker 句柄、跨线程队列、
  在飞回合账本、pending stream 句柄、断连信号）。

改这块时记得看 docs/backend-decomposition.md §14。
"""
from dataclasses import dataclass
from typing import Any


@dataclass
class _ChatState:
    """一次问答请求的跨段共享状态（字段名与拆分前一字不差）。"""

    # ---- 请求身份（进段时的原始值）----
    request: Any = None
    question: str = ""
    ui_context: str = ""
    visual_timeline: str = ""
    workflow_id: str = ""
    feedback_id: str = ""
    session_id: str = ""
    tool_mode: str = ""
    plan_mode: str = ""
    web_mode: str = ""
    thinking_mode: str = ""
    thinking_level: str = ""
    narration_mode: str = ""
    images: Any = None
    turn_id: str = ""
    resume_checkpoint: str = ""

    # ---- 视觉链路 ----
    b64_images: Any = None
    vision_images: Any = None
    vision_context: Any = None
    vision_audit: Any = None

    # ---- 路由与云端 ----
    routing: Any = None
    is_cloud: bool = False
    cloud_llm: Any = None
    _cloud_plan: Any = None
    search_degraded_notice: str = ""

    # ---- 逐请求覆盖参数 ----
    tool_mode_override: Any = None
    plan_mode_override: Any = None
    web_enabled_override: Any = None
    thinking_enabled_override: Any = None
    thinking_level_override: Any = None
    narration_override: Any = None

    # ---- 运行对象与上下文 ----
    selected_agent: Any = None
    _redir_key: str = ""
    request_context: Any = None
    agent_context: Any = None

    # ---- 前段各方法之间跨界的那几个 ----
    desktop_review_hint: str = ""
    app_guided_inspect: bool = False
    inspect_guidance: str = ""
    hints: list = None


@dataclass
class _ChatStreamState:
    """SSE 流内部跨块共享的状态（worker 句柄 / 队列 / 账本 / 断连信号）。"""

    gen: Any = None
    worker: Any = None
    worker_stop: Any = None
    events: Any = None
    wf_holder: Any = None
    inflight: Any = None
    cloud_question: str = ""
    cloud_context: Any = None
    #: 客户端断连 / 用户点停止 → 事件泵提前退出。**原语义是不发 done 帧**，
    #: 所以它必须一路带回事件流的最外层，不能在泵里就地 return。
    stopped: bool = False
