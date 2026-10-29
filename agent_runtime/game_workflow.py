"""Game-development Harness workflow with an optional LangGraph backend.

This module owns orchestration *state*, not the domain tools themselves.  The
existing ToolSpec/sandbox/approval layer remains the only place allowed to
perform side effects.  When LangGraph is installed ``build_langgraph`` creates
an equivalent StateGraph; otherwise callers use the deterministic methods on
``GameWorkflowManager`` directly.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Iterable, Mapping, TypedDict

from config import STATE_ROOT
from .acceptance_contract import AcceptanceError, approve as approve_acceptance
from .acceptance_contract import (from_tasks as acceptance_from_tasks,
                                  on_plan_changed as acceptance_on_plan_changed,
                                  revise as revise_acceptance)
from .preview_adapters import build_preview_bundle
from .project_profile import load_profile, merge_profile
from .workflow_reflection import build_self_review
from .project_checkpoint import create_checkpoint, restore_checkpoint
from .independent_review import review_project
from .project_stage import ProjectStageError, apply_stage, cleanup_stage, create_stage
from .enterprise_sandbox import SandboxUnavailable, stage_execution_scope
from .tools import Capability, tool_mutates
from .context_router import (CONTEXT_LAYERS, ContextPlan, ContextRouter,
                             allocate_layer_budgets, compress_context,
                             compress_context_async, compress_layers,
                             close_persistent_compression_worker, compress_layers_durable,
                             layer_snapshot_digest,
                             summarize_subagent_result)
from .workflow_profiles import DEFAULT_KIND, VALID_KINDS, get_profile, normalize_kind

#: 工具生命周期事件的四种 kind。放这里而不是各写一份字面量：
#: 开发舱面板按这几个字面量过滤持久事件流，两边必须同源。
#: 无循环依赖（已实测两个方向的导入顺序都成立）。
from agent import SINK_FORWARDED_HOOKS, SINK_TOOL_HOOKS, _SUBAGENT_ROLES
# 完整的三键提案契约（add / drop / replace，revise/cancel 为别名；含「已执行任务
# 不可删改」的安全边界与逐条 ignored 清单）**只在这里实现过一次**。
# replan_node 曾自己写了份**退化版**：只取第一个非空键、drop 完全不读、
# replace 当成「追加」而不是「原地改写」——于是 prompt 里承诺的能力一半不存在。
# 详见 replan_node 里那段注释。
from orchestrator import apply_proposal

#: 角色「能不能写文件」——按**工具列表**判，不按 hint 措辞。
#:
#: 实测（四次真实工作流）里最常见的失败形状不是模型偷懒，而是**任务要求写文件、
#: 却被派给了只读角色**：`researcher` / `planner` / `dispatcher` / `reviewer`
#: 的工具列表里根本没有 `python_exec` / `create_file`，而它们的 hint 明写
#: 「不要改任何文件」。那种任务在结构上无法完成，模型只能去查证然后报告查不到。
#:
#: 曾把这一现象归因为「模型不肯用指定工具」——**归因错了**。同一批运行里，
#: 任务一旦派给 `coder`（有 `python_exec`），模型立刻就用了。
WRITE_CAPABLE_TOOLS = frozenset({
    "apply_edit", "create_file", "python_exec", "run_command",
    "dev_patch", "dev_move",
})

#: 只读角色：名字本身就是「只做分析、不改文件」的承诺。与 `agent._READ_ONLY_ROLES`
#: 同一份语义，这里按角色表交集收口，避免两份名单各自漂移。
READ_ONLY_ROLES = frozenset(_SUBAGENT_ROLES) & frozenset({"planner", "dispatcher"})

#: 任务文本里的「要写」信号。刻意保守：宁可漏报也不误报——
#: 误报会把正常的只读复核任务也标成「不可能完成」，那比不报更坏。
_WRITE_VERBS = (
    "新建", "创建", "新增", "写入", "写一个", "写一个文件", "改成", "改为",
    "修改", "重命名", "删除", "替换", "补充实现", "实现这个",
    "create", "write", "modify", "rename", "delete", "implement", "edit ",
)


def role_mismatch_tasks(tasks: Iterable[Any]) -> list[dict[str, str]]:
    """列出「要求写文件、角色却不能写」的任务。

    给调用方**指名**用——整图被拒时只说一句"无效"等于把问题原样退回给用户。

    两个维度都要查（2026-10-16 修第二维）：

    1. **任务文本**说「要写」，但角色不能写；
    2. **点名工具**里全是只读角色禁用的工具 —— 即便任务文本不提写。

    第 2 维原先**完全漏了**。实测：planner 被派 `tools=["apply_edit","dev_mcp_call"]`、
    任务文本写「勘察目录与文件结构」，本函数返回 `[]` → 整图放行 →
    子代理零工具开跑 → 报「模型没给出结论」。错因不在文本，在派单点名了什么。

    判据不按名字：`_named_tools_are_all_dropped` 逐个问 `ToolSpec`「会不会改东西」。
    """
    out: list[dict[str, str]] = []
    for item in (tasks or ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("task") or item.get("description") or "")
        task_id = str(item.get("id") or "")[:80]
        if not role:
            continue
        if _named_tools_are_all_dropped(item, role):
            out.append({"id": task_id, "role": role[:60],
                        "why": "点名的工具全部被安全边界筛掉，子代理会零工具"})
        elif text and not role_can_write(role) and task_wants_write(text):
            out.append({"id": task_id, "role": role[:60],
                        "why": "任务要求写文件，但该角色的工具列表里没有写类工具"})
    return out


def _named_tools_are_all_dropped(item: Mapping[str, Any], role: str) -> bool:
    """点名了非空工具集、但对 `role` 而言一个都留不下 → 这条派单必然零工具。

    **只对只读角色查**：非只读角色点名 `["read_file"]` 落空是「名字不存在」，
    属于另一族问题（且运行时 `_run_child` 会 fail-closed 指名），不该在这里拦整图。
    """
    if role not in READ_ONLY_ROLES:
        return False
    named = item.get("tools")
    if isinstance(named, str):
        # 字符串形态（`"read_file"` / `"read_file,grep"`）要与 `_run_child` 一样
        # 逗号切分并去掉空白项。实测踩过：`""` 被当成长度 1 的 `[""]`，
        # 空名查不到 spec 就归为「被筛掉」，把一次**没传工具**的正常派单
        # 判成零工具。
        named = [part.strip() for part in named.split(",")]
    if not isinstance(named, (list, tuple, set)):
        return False
    named = [str(name) for name in named if str(name).strip()]
    if not named:
        return False
    kept = [
        str(name) for name in named
        if not _tool_is_dropped_for_read_only(str(name), role)
    ]
    return not kept


def _lookup_tool_spec(name: str):
    """查工具的 `ToolSpec`；查不到返回 `None`。

    **惰性导入** `tools`：本模块已经在顶层 `from agent import ...`，而 `agent`
    又导入 `tools`；顶层再加一条会把这个环变成硬依赖。函数内导入在首次调用
    （校验派单时）才发生，那时 `tools` 必然已在 `sys.modules` 里。
    """
    from .tools import coerce_tool_spec

    import tools as tools_mod

    value = (tools_mod.TOOLS or {}).get(name)
    if value is None:
        return None
    try:
        return coerce_tool_spec(name, value)
    except TypeError:
        return None


def _tool_is_dropped_for_read_only(name: str, role: str) -> bool:
    """这个工具会不会在只读角色的边界下被丢掉。

    三种丢法，与 `agent._run_child` 逐条对齐（**必须对齐**，否则这里放过、
    运行时仍会零工具 fail-closed，整图白拒）：

    - 不在本会话工具注册表 → 丢；
    - `domain == "mcp"`（连接器工具）→ 丢。只读角色的 `mcp_policy` 恒被收敛成
      `deny`，而**否掉的是整族**，不是某一个名字；
    - 按 `ToolSpec` 会改东西（`tool_mutates`）→ 丢。

    这里原本写的是三个硬编码名字（MCP 三兄弟）。变异测试逮到它为什么是
    **必需的**：`dev_route_connector` 的 spec 是 `read_local` + `pure`，
    `tool_mutates` 判它无害 —— 只按名字拦就漏了，改成「按能力拦」反而
    会把它放进来。真相是判据该用 `domain`，那才是把连接器工具标出来的那一维
    （`agent_runtime/tool_domains.py` 的声明表）。
    """
    spec = _lookup_tool_spec(name)
    if spec is None:
        return True
    if spec.domain == "mcp" or spec.group == "mcp":
        return True
    return tool_mutates(spec)


def role_can_write(role: str) -> bool:
    """该角色的工具列表里有没有任何写类工具。"""
    tools = set((_SUBAGENT_ROLES.get(str(role or ""), {}) or {}).get("tools") or ())
    return bool(tools & WRITE_CAPABLE_TOOLS)


def task_wants_write(task: str) -> bool:
    """任务文本里有没有「要写文件」的信号。"""
    text = str(task or "").lower()
    return any(verb in text for verb in _WRITE_VERBS)

try:  # optional dependency: the harness remains usable without LangGraph
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, Send, interrupt as graph_interrupt
    try:
        from langgraph.checkpoint.memory import MemorySaver
    except Exception:  # pragma: no cover
        MemorySaver = None
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:  # pragma: no cover
        SqliteSaver = None
    # psycopg loads a native libpq extension.  Import it only when a Postgres
    # checkpoint DSN is explicitly configured; importing it on every local
    # run made Python 3.13 intermittently crash during test/module discovery.
    _postgres_dsn_configured = bool(
        (os.getenv("DOCMIND_CHECKPOINT_POSTGRES_DSN") or
         os.getenv("LANGGRAPH_CHECKPOINT_POSTGRES_DSN") or "").strip())
    if _postgres_dsn_configured:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except Exception:  # pragma: no cover
            PostgresSaver = None
        try:
            from psycopg_pool import ConnectionPool
        except Exception:  # pragma: no cover
            ConnectionPool = None
    else:
        PostgresSaver = None
        ConnectionPool = None
except Exception:  # pragma: no cover - exercised when optional package absent
    END = START = StateGraph = None
    MemorySaver = None
    SqliteSaver = None
    PostgresSaver = None
    ConnectionPool = None
    Command = None
    graph_interrupt = None
    Send = None


class WorkflowError(ValueError):
    pass


#: Sentinel used to distinguish "caller did not provide capabilities" from
#: "caller explicitly provided an empty iterable". `_ensure_capability_lease`
#: relies on it; `create_capability_lease` accepts the same sentinel via its
#: default.
_UNSET = object()


CHECKPOINT_SCHEMA_VERSION = 1


class WorkflowGraphState(TypedDict, total=False):
    workflow_id: str
    kind: str
    request: str
    options: list[dict[str, Any]]
    phase: str
    status: str
    selected_option: dict[str, Any]
    approved: bool
    approval_decision: bool
    interrupt_reason: str
    research_findings: str
    research_source: str
    plan_source: str
    plan_offered: int
    event: str
    step_count: int
    replan_count: int
    max_steps: int
    child_max_steps: int
    max_replans: int
    max_context_chars: int
    max_provider_retries: int
    provider_attempts: list[dict[str, Any]]
    research_attempts: list[dict[str, Any]]
    failed_tasks: list[dict[str, Any]]
    review: dict[str, Any]
    tasks: list[dict[str, Any]]
    results: dict[str, Any]
    current_report: dict[str, Any]
    execution_ok: bool
    replan_error: str
    replan_history: list[dict[str, Any]]
    task_threads: list[str]
    sources: list[str]
    research_error: str
    option_source: str
    option_generation_pending: bool
    max_subagents: int
    langsmith_trace: dict[str, Any]


def _merge_result_maps(left: dict[str, Any] | None,
                       right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class TaskFanoutState(TypedDict, total=False):
    tasks: list[dict[str, Any]]
    task: dict[str, Any]
    prior_results: dict[str, Any]
    trace_parent_id: str
    task_thread: str
    task_results: Annotated[dict[str, Any], _merge_result_maps]


# 工作流级步数预算。0 表示不设置固定工具步数上限；取消、失败、重复调用、
# 审批、费用和 deadline 仍然是独立的停止条件。部署侧可用正数恢复预算护栏：
#   DOCMIND_WORKFLOW_STEPS_MIN  自动预算下限
#   DOCMIND_WORKFLOW_STEPS_MAX  硬顶
WORKFLOW_STEPS_MIN = 0
WORKFLOW_STEPS_MAX = 0
# 无法 import agent（极端裁剪安装）时，工作流用的默认子代理步数回退值。
WORKFLOW_CHILD_DEFAULT_STEPS = 24


def _default_max_agents() -> int:
    try:
        from agent_runtime.capacity import get_settings
        return int(get_settings().get("max_agents", 0))
    except Exception:
        return 0


def _default_child_max_steps() -> int:
    try:
        from agent_runtime.capacity import get_settings
        return int(get_settings().get("child_max_steps", 0))
    except Exception:
        return 0


#: 单个任务允许声明的依赖个数上界。
#:
#: 这个上界必须**独立于任务总数上限**（``max_subagents`` / ``max_tasks``）。
#: 此前守卫写成了 ``len(clean_deps) > max_tasks``——拿「依赖个数」去比
#: 「任务总数上限」，两个无关量。而 ``max_tasks=0`` 的约定是**不限**
#: （见 ``max_tasks=max_subagents if max_subagents > 0 else 0``），
#: 于是默认配置（``max_agents`` 未配 -> 0）下：
#:
#:     输入 5 步 DAG（survey -> implement -> testrun -> fix -> report）
#:     实际存活 1 步：['survey']
#:
#: **任何声明了 ``depends_on`` 的任务都被静默丢弃**，多步 DAG 塌成只剩根任务，
#: 而调用方照样把它当合法计划落库、照样发 ``plan`` 事件、照样弹审批框——
#: 没有任何报错。计划生成的验证节点还存在，它只查"落库的计划能否执行"，
#: 查不出"落库的计划已经少了一大半"。
_MAX_TASK_DEPS = 16


def _as_task_list(raw: Any) -> list[Any]:
    """把规划器的原始产出拆成任务列表（不抛）。

    与 `_raw_task_count` 走同一套形状处理：围栏 JSON 字符串、
    ``{"tasks": [...]}``、``{"plan": {"tasks": [...]}}``、裸 list。
    """
    try:
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                              flags=re.I | re.S).strip()
            raw = json.loads(text)
        if isinstance(raw, Mapping):
            inner = raw.get("tasks")
            if inner is None and isinstance(raw.get("plan"), Mapping):
                inner = raw["plan"].get("tasks")
            raw = inner
        return list(raw) if isinstance(raw, list) else []
    except Exception:
        return []


def _raw_task_count(raw: Any) -> int:
    """Best-effort count of how many tasks the model actually offered.

    Only feeds the ``plan_generation_truncated`` diagnostic, so it must never
    raise — a diagnostic that can break planning is worse than no diagnostic.

    形状处理**全部**在 `_as_task_list` 里，这里只取长度。这不是洁癖：
    抽出 `_as_task_list` 之前这段逻辑在本文件里有**两份**，而
    `tests/mutation/battery_plan_dag.py` 的锚点校验当场抓到「锚点在
    ``agent_runtime/game_workflow.py`` 里出现 2 次」——只替换第一处的变异会改错
    地方。§9 的原话：「上限写两处，就等于写了一处旧的」。
    """
    return len(_as_task_list(raw))


def _effective_task_parallelism(requested: int) -> int:
    """Apply the local model/GPU concurrency estimate to workflow fan-out."""
    requested = max(1, int(requested))
    try:
        from config import LLM_MODEL, LLM_PROVIDER, PROVIDERS, get_runtime
        from .local_runtime import effective_parallelism
        provider = str(get_runtime("llm_provider") or LLM_PROVIDER).lower()
        model = str(get_runtime("llm_model") or LLM_MODEL or "")
        base_url = str(get_runtime("llm_base_url") or
                       PROVIDERS.get(provider, {}).get("base_url", ""))
        return effective_parallelism(provider, model, requested, base_url=base_url)
    except Exception:
        return requested


def _env_int(name: str, default: int) -> int:
    """读正整数环境变量；未设置/空白/非法时回退默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def workflow_step_limits() -> tuple[int, int]:
    """返回当前生效的步数预算 (下限, 硬顶)，每次调用实时读取环境变量。

    两项都为 0 时代表无限；配置为正数时，硬顶不小于下限。
    """
    lo = max(0, _env_int("DOCMIND_WORKFLOW_STEPS_MIN", WORKFLOW_STEPS_MIN))
    hi = max(0, _env_int("DOCMIND_WORKFLOW_STEPS_MAX", WORKFLOW_STEPS_MAX))
    if hi == 0:
        return 0, 0
    if lo > hi:
        hi = lo
    return lo, hi


def effective_workflow_max_steps(task_count: int, *, child_default: int = 0,
                                 explicit: int = 0) -> int:
    """推算工作流生效步数预算（可解释、有界）。

    - 工作流上下限均为 0 时返回 0，表示无限；
    - ``explicit`` 为策略里显式设置的正数时保留（配置了硬顶时夹到硬顶）；
    - 否则按任务规模自动推算，并夹在配置区间内。
    """
    lo, hi = workflow_step_limits()
    try:
        explicit_value = int(explicit or 0)
    except (TypeError, ValueError):
        explicit_value = 0
    if explicit_value > 0:
        return min(hi, explicit_value) if hi > 0 else explicit_value
    if hi == 0:
        return 0
    if not child_default:
        try:
            import agent as agent_mod
            child_default = int(getattr(agent_mod, "SUBAGENT_MAX_STEPS",
                                        WORKFLOW_CHILD_DEFAULT_STEPS)) or WORKFLOW_CHILD_DEFAULT_STEPS
        except Exception:  # noqa: BLE001
            child_default = WORKFLOW_CHILD_DEFAULT_STEPS
    try:
        count = max(0, int(task_count))
    except (TypeError, ValueError):
        count = 0
    auto = max(lo, count * max(1, child_default) + 4)
    return min(hi, auto)


@dataclass(frozen=True)
class WorkflowPolicy:
    # 默认值跟随配置下限；0 表示无限。
    max_steps: int = field(default_factory=lambda: workflow_step_limits()[0])
    child_max_steps: int = field(default_factory=_default_child_max_steps)
    max_replans: int = 2
    max_subagent_retries: int = 2
    max_subagents: int = field(default_factory=_default_max_agents)
    max_context_chars: int = 6000
    approval_mode: str = "safe"  # safe | high
    max_tool_failures: int = 3
    provider_retries: int = 2
    hook_failure: str = "continue"  # continue | block

    @classmethod
    def from_state(cls, policy_dict: Mapping[str, Any] | None) -> "WorkflowPolicy":
        """从持久化 policy dict 重建；忽略 step_budget_explicit 等辅助键。"""
        keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (policy_dict or {}).items() if k in keys}).normalized()

    def normalized(self) -> "WorkflowPolicy":
        # 0 表示无限；正数策略才按部署硬顶裁剪。
        _lo, hi = workflow_step_limits()
        mode = self.approval_mode if self.approval_mode in {"safe", "high"} else "safe"
        hook_failure = self.hook_failure if self.hook_failure in {"continue", "block"} else "continue"
        return WorkflowPolicy(
            max_steps=(0 if int(self.max_steps) <= 0 else
                       (min(hi, int(self.max_steps)) if hi > 0 else int(self.max_steps))),
            child_max_steps=(0 if int(self.child_max_steps) <= 0 else
                             min(1000, int(self.child_max_steps))),
            max_replans=max(0, min(10, int(self.max_replans))),
            max_subagent_retries=max(0, min(5, int(self.max_subagent_retries))),
            max_subagents=(0 if int(self.max_subagents) <= 0 else
                           min(64, max(1, int(self.max_subagents)))),
            max_context_chars=max(800, min(50000, int(self.max_context_chars))),
            approval_mode=mode,
            max_tool_failures=max(1, min(20, int(self.max_tool_failures))),
            provider_retries=max(0, min(5, int(self.provider_retries))),
            hook_failure=hook_failure,
        )


@dataclass(frozen=True)
class WorkflowOption:
    id: str
    title: str
    summary: str
    recommended: bool = False
    source: str = "local"
    requires_web: bool = False


# ── 工作流的**三值结局** ─────────────────────────────────────────────
#
# ## 为什么要第三个值
#
# 实测（`wf-80dc902c8cae` 等连续多次只读真跑，四任务全 ok、十项检查全 True、
# `evidence_verified`、引用不过 0 条、change_count=0）里，`status` 一直是
# `failed`，唯一原因是 `review.ok = agent_ok and project.ok` 而
# `project.status == "unverified"`（试做区没有容器级命令隔离）。
#
# 那个声明是**正确的安全声明**，不该被改弱；但把它和「真失败」压成同一个
# 布尔，就会让下游要么谎报失败（已修的四处消费者）、要么整块功能不启动
# （`evaluate_workflow` / `skill_candidate` 要求 `status == "completed"`）。
#
# 所以这里给**后端**一个能表达「完成了但没独立验证」的结局，而不是让每个
# 消费者各自去猜。
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_UNVERIFIED = "completed_unverified"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

#: 终态集合——**全仓唯一**列举处。新增结局时只改这里。
#:
#: 漏掉的后果不是「显示不对」，是**死锁**：`is_terminal()` 返 False →
#: `active_for_project()` 认为这个工作流还活着 → 下一次 `start()` 被互斥拒绝。
#: 所以下面所有终态判断都必须走 `is_terminal_status()`，不许再写字面量集合。
#: 正在后台重建的代码索引，键是 `<集合名>|<根>`。用来防重复启动时并发跑
#: 同一个索引——两个 ingest 同时写一个集合，结果不可预期。
_CODE_INDEX_REBUILDING: set[str] = set()


TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED, STATUS_FAILED, STATUS_INTERRUPTED,
})

#: 「算成功」的结局。用户最终验收、质量评估、经验库都按这个口径。
SUCCESS_STATUSES = frozenset({STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED})


def is_terminal_status(status: Any) -> bool:
    return str(status or "") in TERMINAL_STATUSES


def is_successful_status(status: Any) -> bool:
    return str(status or "") in SUCCESS_STATUSES


def counts_as_ok(item: Any) -> bool:
    """这个任务结果算不算「成功完成」——全仓库**唯一**一份判据。

    ## 为什么 `status == "ok"` 不够

    `agent.py` 在三处给结论打 `degraded`：安全网兜底的 final、连续空响应、
    清洗残句。标的是「这段结论**不是模型自己的答案**」。可 `status` 仍然是
    `"ok"` —— 子代理确实跑完了、确实返回了东西，只是那东西是 harness 自己
    写的诊断（例如「模型连续 1 次调用都没有返回可用正文…」）。

    于是判据只看 `status` 就会把这种任务算成成功完成：时间线上是绿的、计数
    是过的、报告写着「全部完成」，而实际上一个子问题都没答。这就是本仓库
    反复出现的「把没能答记成答了」，只是这次信号在**子代理结果**里活着、
    在**判定**这里被丢掉。

    ## 注意它**不改** `status`

    这里只回答「算不算 ok」，不重写结果。`degraded` 的任务既不计入 `n_ok`，
    也不计入 `n_failed` —— 它没失败，它只是**没答出来**，硬塞进任何一边都
    会让计数撒谎。要改状态是别处的决定，不要在这里顺手做。

    调用方：`_run_task_graph` 的 `wave_complete` 状态、整体 `ok`、`n_ok`，
    以及图循环报告的 `n_ok`。
    """
    data = item if isinstance(item, Mapping) else {}
    return str(data.get("status") or "") == "ok" and not bool(data.get("degraded"))


def unmet_acceptance_items(contract: Any, results: Any) -> list[dict[str, Any]]:
    """用户已批准的**必需**验收条件里，哪些指向的任务没能完成。

    ## 实测事故（`wf-bdb1a54286bf` / `wf-0bbd6c9edf6e`，形状完全一致）

    用户批准的验收条件（`revision=1, approved_revision=1`）要求三个任务：

        accept-research    evidence=['task:research']
        accept-implement   evidence=['task:implement']
        accept-verify      evidence=['task:verify']

    `research` 反思未通过 -> `implement` / `verify` 依次 blocked -> **重规划把整份
    计划换成了单个 `research_retry`**（`replan_history.dropped` 还写着 0）。于是：

        tasks    = [research_retry]
        results  = {research: failed, implement: blocked, verify: blocked,
                    research_retry: ok}
        status   = completed_unverified      <- 「已完成 · 未做独立验证」
        player.gd 含 'dash' = False           <- 用户要的东西一个没做

    `report["ok"]` 只看**重规划之后**的任务表，那张表里唯一剩下的任务是 ok 的，
    于是报「已完成」。**而 `implement` / `verify` 从此再也不会跑。**

    ## 为什么这是完成判定的根问题

    用户批准的是「三条验收条件」，不是「当前这张任务表」。重规划可以换掉任务表，
    但它**不能**让用户批准过的验收条件失效——否则「完成」这个词就不再对应
    任何用户认可过的东西。

    ## 为什么只查 `task:` 证据

    `evidence` 里还有 `task:<id>` 以外的写法（人工检查等），那些由用户自己判断，
    机器不该替他判为「未满足」——那会把「需要你亲自看一眼」误报成失败。

    ## 反向守卫

    · 全部必需条件都被 `ok` / `deduped` / `failed`(已补救) 的任务支撑 -> 门开
      （实测 `wf-80dc902c8cae`、`wf-23be9f18b3c6` 四条全 ok，必须仍能报完成；
      `test_execute_uses_langgraph_for_single_wave_replan_and_context`
      broken->repair 也必须仍能报 completed）
    · 任务 `blocked`（因上游失败**从未被尝试**）-> 算未满足
    · 任务连结果都没有（被重规划删掉）-> 算未满足
    · 该任务 `optional` -> 不算未满足
    · 没有任何必需条件 -> 门开（不拦没有验收条件的工作流）
    """
    items = contract.get("items") if isinstance(contract, Mapping) else None
    if not isinstance(items, list):
        return []
    rows = results.get("results") if isinstance(results, Mapping) and isinstance(
        results.get("results"), Mapping) else results
    if not isinstance(rows, Mapping):
        rows = {}
    runtime = results.get("__runtime__") if isinstance(results, Mapping) else None
    unmet: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or not item.get("required"):
            continue
        missing: list[str] = []
        for token in item.get("evidence") or []:
            text = str(token or "").strip()
            if text == "runtime:engine":
                # 游戏工作流的"真跑过"验收：必须有运行期证据且 ok=true。
                # **缺证据按未满足算**（fail-closed）——否则"没跑过"会被
                # 当成"没问题"，正是要防的假绿。
                if not (isinstance(runtime, Mapping) and runtime.get("ok") is True):
                    missing.append("runtime:engine")
                continue
            if not text.startswith("task:"):
                continue  # 人工检查等由用户自己判断，机器不代判
            task_id = text[len("task:"):].strip()
            if not task_id:
                continue
            row = rows.get(task_id)
            if not isinstance(row, Mapping):
                # 连结果都没有 -> 被重规划从计划里删掉了
                missing.append(task_id)
            elif row.get("optional"):
                continue
            elif str(row.get("status") or "") == "blocked":
                # **只拦 blocked。** 这是本门唯一该管的形态：任务因为上游失败而
                # **从未被尝试**，随后又被重规划从计划里删掉——用户批准过的那份
                # 工作就这么静悄悄地消失了。
                #
                # 刻意**不拦 failed**：任务跑了、失败了、被 replanner 补救成功，
                # 那是重规划的正当用途（实测
                # `tests/test_game_workflow.py::test_execute_uses_langgraph_for_
                # single_wave_replan_and_context` 正是这个形状：broken 失败 ->
                # repair 成功 -> 断言 completed）。第一版把 failed 也算进去，
                # 那条既有测试立刻红。
                #
                # 而且「有任务 failed」这件事本来就已经被拦住了：
                # `all_ok` / `report["ok"]` 那一层就为 False，不需要这道门管。
                missing.append(task_id)
        if missing:
            row = {
                "id": str(item.get("id") or ""),
                "statement": _clean_text(item.get("statement"), 160),
                "tasks": missing,
            }
            if "runtime:engine" in missing and isinstance(runtime, Mapping) and runtime.get("reason"):
                row["reason"] = _clean_text(runtime.get("reason"), 300)
            unmet.append(row)
    return unmet


def acceptance_gate(state: Any) -> tuple[bool, list[dict[str, Any]]]:
    """必需验收条件是否都能被已完成的任务支撑。-> ``(是否放行, 未满足明细)``"""
    try:
        raw = state.results if isinstance(state.results, Mapping) else {}
        # 浅拷贝后在**同一层**附加运行期证据：`unmet_acceptance_items` 仍按
        # 原逻辑解包 results["results"] 取任务映射，不会被多包一层打乱。
        payload = dict(raw)
        payload["__runtime__"] = getattr(state, "runtime_evidence", {}) or {}
        unmet = unmet_acceptance_items(state.acceptance_contract, payload)
    except AttributeError:
        return True, []
    return (not unmet), unmet


def _note_unmet_acceptance(state: Any, unmet: list[dict[str, Any]]) -> None:
    """把「必需验收条件无法满足」记进 review failures，让用户看得见原因。

    不这么做的话，工作流会显示「执行失败」却没有任何一条说明**为什么**——
    而真实原因（重规划把任务丢了）恰恰是用户最需要知道的那件事。
    """
    if not unmet:
        return
    try:
        review = state.review
    except AttributeError:
        return
    if not isinstance(review, dict):
        return
    failures = review.setdefault("failures", [])
    if not isinstance(failures, list):
        return
    names = "、".join(
        "%s（缺任务 %s）" % (item.get("id") or "验收条件",
                              "、".join(item.get("tasks") or []))
        for item in unmet[:4])
    failures.append({
        "kind": "acceptance",
        "message": "已批准的必需验收条件无法满足：%s" % names,
        "recovery": "重规划后任务表里没有这些任务，它们不会自动重跑；"
                    "请重新规划补回，或修改验收条件后重新提交。",
        "items": unmet[:8],
    })


def required_task_ids(contract: Any, *, only_approved: bool = True) -> set[str]:
    """**用户批准的必需验收条件**指着哪些任务 id。

    ## 「已批准」为什么是关键限定

    验收条件有两种来源：
      · `from_tasks` 自动派生（`user_approved` 起初是 False，批准后才 True）
      · 用户自己写的（`user_approved` 直接为 True）

    要守住的是**用户认可过的工作范围**，所以默认只认 `user_approved` 为真的条件。
    连没被批准的条件也保护，等于把系统自己拟的草稿也当成契约。

    ## 为什么只看 `task:` 证据

    `evidence` 里还有「人工检查」这类写法，那些由用户自己判断，不该让机器
    据此认定某个任务必须留在计划里。
    """
    items = contract.get("items") if isinstance(contract, Mapping) else None
    if not isinstance(items, list):
        return set()
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not item.get("required"):
            continue
        if only_approved and not item.get("user_approved"):
            continue
        for token in item.get("evidence") or []:
            text = str(token or "").strip()
            if text.startswith("task:") and text[len("task:"):].strip():
                ids.add(text[len("task:"):].strip())
    return ids


def keep_required_tasks(previous: Iterable[Mapping[str, Any]],
                        proposal: list[dict[str, Any]],
                        protected: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """重规划的提案若丢掉了**受保护**的任务，把它们放回去。-> ``(最终任务表, 保留的 id)``

    ## 实测（wf-58a346c6cbec / wf-02f4a26bf38b，两轮真跑）

    重规划**反复**把计划改小：

        attempt 1:  proposal 1 个任务 -> 丢掉 research/implement/verify
        attempt 2:  proposal 2 个任务 -> 又丢掉 research_retry

    A 轮最终 tasks = [research_files_v2, research_godot_facts_v2]，原始 3 个必需
    任务一个都没剩；B 轮 4 个必需任务全被丢掉。而每次重规划都「成功」了。

    ## 为什么选「放回去」而不是「拒绝这次重规划」

    拒绝的话，用户要的活儿一件没干，这个 harness 的目的是**把活干完**。放回去
    守住的是「用户批准过的范围不许被系统单方面缩小」，而重规划该做的补救
    （新加一个 research_retry）**照样生效**。

    ## 三个必须处理的坑

    1. **依赖悬空**：旧任务的 `depends_on` 可能指向已被丢弃的 id。这里把它们改成
       `context_from`（跨波次上下文输入）而不是调度边——它们本来就跑过或有结果，
       当上下文合理，当依赖会成环或悬空。
    2. **撞 `max_subagents`**：放回的任务可能超上限。超了就**只放回受保护的那几个**，
       并把丢弃的情况写进历史（不能让「放回」又变成一次静默丢弃）。
    3. **打转**：「禁止丢弃」遇上「重规划就是想把它们丢掉」会循环。兜底是现成的
       `max_replans`——重规划次数用完就不再重规划，所以最多循环到上限。

    ## 反向守卫

    · 没有受保护任务（合同未批准 / 无必需条件 / 全是人工检查）-> 原样返回提案
    · 提案**没有**丢任何受保护任务 -> 原样返回，**不**加重复任务
    · 旧任务里那些本来就没被要求的（optional / 不在保护集）-> 不放回
    """
    protected = {str(t) for t in protected if str(t)}
    proposed_ids = {str(item.get("id") or "") for item in proposal}
    lost = sorted(t for t in protected if t not in proposed_ids)
    if not lost:
        return list(proposal), []
    by_id = {str(item.get("id") or ""): dict(item) for item in previous}
    restored: list[dict[str, Any]] = []
    known = set(proposed_ids)
    for task_id in lost:
        source = by_id.get(task_id)
        if not isinstance(source, Mapping):
            continue  # 已经不在上一版计划里，无从放回
        item = dict(source)
        deps = [str(d) for d in (item.get("depends_on") or [])]
        item["context_from"] = sorted(
            {*(str(d) for d in (item.get("context_from") or [])), *deps}
            - known)
        # 依赖只保留「还在计划里」的，避免悬空边
        item["depends_on"] = [d for d in deps if d in known]
        item["restored_after_replan"] = True
        restored.append(item)
    if not restored:
        return list(proposal), []
    return [*restored, *proposal], [str(i.get("id")) for i in restored]


_PROPOSAL_KEYS = ("add", "drop", "replace", "tasks", "revise", "cancel")


def normalize_replan_proposal(proposal: Any) -> dict[str, Any]:
    """把 replanner 的返回值整形成三键对象（`add` / `drop` / `replace`）。

    prompt 里给模型的契约（`agent.py`）是：

        {"add":[任务...],"drop":["要取消的任务id"],"replace":[改写后的任务...]}
        // revise / cancel 是别名

    而模型也可能只回一个**任务数组**（旧格式，`run_plan` 一直兼容）。这里两种都收。
    刻意**不**做「只取第一个非空键」——那正是第一版的 bug：`drop` 从来没被读过、
    `replace` 被当成追加、其余键无声丢弃，而 `accepted` 却数的是四键总和。
    """
    if isinstance(proposal, Mapping):
        out: dict[str, Any] = {}
        for key in ("add", "tasks", "replace", "revise", "drop", "cancel"):
            value = proposal.get(key)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list) and value:
                out[key] = value
        return out
    if isinstance(proposal, list) and proposal:
        return {"add": proposal}
    return {}


def proposal_offer_summary(proposal_map: Mapping[str, Any]) -> dict[str, Any]:
    """提案里**每个键**各提了什么。用于让「未被采纳的键」不再无声消失。"""
    summary: dict[str, Any] = {}
    for key, value in (proposal_map or {}).items():
        if isinstance(value, list):
            summary[key] = [str((item.get("id") if isinstance(item, Mapping) else item) or "")
                            for item in value][:12]
        else:
            summary[key] = value
    return summary


def replan_reject_reason(proposal_map: Mapping[str, Any], delta: Any,
                         max_subagents: int, known_results: int,
                         *, error: str = "") -> str:
    """重规划没采纳任何东西时，**说清是哪一种原因**。

    第一版三种情况都报 `empty_or_invalid_proposal`：
      1. 提案本身为空 / 全是模型编的非法项
      2. 提的全是**已执行**的任务（安全边界拒绝）
      3. **容量用尽**（`max_subagents` 已满）—— 这条最误导：用户会去检查提案怎么写，
         而真因是「没额度了」，改提示词一点用都没有。

    `error` 非空表示**过程本身炸了**（重规划调用抛异常、或规范化 threw）。
    这条必须排在最前面：模型压根没能把提案说出来，报「提案无效」是**谎报**——
    用户会去检查提案怎么写，而真因是服务端/网络/解析炸了。
    """
    if error:
        return "replan_error(%s)" % error
    if not proposal_map:
        return "empty_proposal"
    ignored = list((delta or {}).get("ignored") or []) if isinstance(delta, Mapping) else []
    if ignored and not any((delta or {}).get(k) for k in ("added", "dropped", "replaced")
                           if isinstance(delta, Mapping)):
        return "all_proposals_rejected_by_safety_boundary"
    if max_subagents > 0 and known_results >= max_subagents:
        return "subagent_capacity_exhausted(%d/%d)" % (known_results, max_subagents)
    if ignored:
        # 「既有 ignored、又有采纳」——从**当前唯一调用点**到不了这里：
        # 调用方只在 `not delta["changed"]` 时才问我，而 `apply_proposal` 里
        # `changed = bool(added or dropped or replaced)`，所以 changed 为假时
        # 三者必空，上面那条 `all_proposals_rejected_by_safety_boundary` 会先返回。
        # 留着当防御：将来若多一个调用点，忽略条数这个信息值得报出来。
        return "empty_or_invalid_proposal(ignored=%d)" % len(ignored)
    return "empty_or_invalid_proposal"


def remediated_task_ids(replan_history: Iterable[Mapping[str, Any]] | None,
                       results: Any) -> dict[str, str]:
    """「失败过但已被补救成功」的任务 id -> 补救它的那个任务 id。

    ## 为什么需要它（用户定下的口径）

    > 失败可以先报出来、标红，下一条补一句补救成功，失败的历史留着给用户看；
    > 但既然已经补救成功了，**程序那里可以通过**。

    `experience._OUTCOMES` 里本来就有 `fail-then-fixed` 这个取值——**代码库早就
    承认这种形态，而完成判定里没有它的位置**。于是「broken 失败 → repair 补上」
    只能报 failed，用户看到红色卡片，而活儿其实干完了。

    ## 判据必须可核验，不能靠模型自述

    同时满足三条才算补救成功：

      1. 该任务 `failed`（真的失败过，历史不会被抹掉）
      2. 有一个**由重规划引入**的任务（`adopted.added` / `adopted.replaced`）
      3. 那个补救任务**声明过对它的依赖**（`demoted_deps` 或仍在的依赖边），
         **并且自己跑成了 ok**

    只加一个无关任务不算数——否则模型只要随便追加一个成功任务就能把真失败洗白。

    ## 注意这**不放宽任何安全边界**

    被补救的任务在 `state.subagents` 里仍然是 `failed`（界面照旧标红），
    验收门 `unmet_acceptance_items` 也照旧按它的真实状态判定。
    本函数只回答「最终判定层面能不能通过」。
    """
    rows = results.get("results") if isinstance(results, Mapping) and isinstance(
        results.get("results"), Mapping) else results
    if not isinstance(rows, Mapping):
        return {}
    failed = {str(tid) for tid, row in rows.items()
              if isinstance(row, Mapping) and str(row.get("status") or "") == "failed"}
    if not failed:
        return {}
    out: dict[str, str] = {}
    for entry in replan_history or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("status") or "") not in {"applied", "simulated"}:
            continue
        adopted = entry.get("adopted") if isinstance(entry.get("adopted"), Mapping) else {}
        introduced = {*list(adopted.get("added") or []),
                      *list(adopted.get("replaced") or [])}
        demoted = {str(d.get("task") or ""): [str(x) for x in (d.get("demoted_deps") or [])]
                   for d in (entry.get("demoted_deps") or [])
                   if isinstance(d, Mapping)}
        for task_id in failed:
            if task_id in out:
                continue
            for helper in introduced:
                row = rows.get(helper)
                if not isinstance(row, Mapping) or str(row.get("status") or "") != "ok":
                    continue
                # 声明过依赖：要么被降级成上下文（依赖失败上游），要么依赖边仍在
                declared = task_id in demoted.get(helper, []) or task_id in [
                    str(d) for d in (row.get("depends_on") or [])]
                if declared:
                    out[task_id] = helper
    return out


def review_status(review: Any, *, extra_ok: bool = True) -> str:
    """从一份 `review` 字典定出三值结局。**所有 status 生产者都必须走它。**

    ## 为什么要单独收一层「回退」

    后端只在 **有 project_root 时** 才把两个维度分开存：

        if state.project_root:
            state.review["agent_ok"] = bool(state.review.get("ok"))
            state.review["project"] = self._review_project(state)
            state.review["ok"] = agent_ok and project["ok"]

    没有项目目录时，`agent_ok` / `project` **两个键都不存在**，此时唯一可读的
    就是 `review.ok` 本身。第一版三值实现直接 `bool(review.get("agent_ok"))`
    ——在无 project_root 的工作流上恒为 False，于是把本来 completed 的全部
    判成 failed（实测 10 条测试同时红：`'failed' != 'completed'`）。

    所以回退必须写在这里，而不是让每个调用点各自 `if`——
    三处生产者的口径一旦漂移，`report["ok"]` 的反推就会失真。
    """
    rv = review if isinstance(review, Mapping) else {}
    plain_ok = bool(rv.get("ok"))
    project = rv.get("project") if isinstance(rv.get("project"), Mapping) else {}
    agent_ok = bool(rv.get("agent_ok")) if rv.get("agent_ok") is not None else plain_ok
    project_ok = bool(project.get("ok")) if project else plain_ok
    try:
        change_count = int(project.get("change_count") or 0)
    except (TypeError, ValueError):
        change_count = 0
    changed = bool(project.get("changes")) or change_count > 0
    return settle_status(agent_ok=agent_ok, project_ok=project_ok,
                         project_status=str(project.get("status") or ""),
                         extra_ok=extra_ok, changed=changed)


def _graph_review_status(review: Any) -> str:
    """LangGraph `review_node` 用的三值结局。与直接赋值那侧同一个读取器。"""
    return review_status(review)


def settle_status(*, agent_ok: bool, project_ok: bool,
                  project_status: str = "", extra_ok: bool = True,
                  changed: bool = False) -> str:
    """按两个维度定出三值结局。**所有 status 生产者都必须走它。**

    实测教训：`status` 有**三个**生产者（LangGraph 的 `review_node`、execute
    末尾的直接赋值、试做区重新复核），它们各自算一遍就会漂移——而
    `report["ok"]` 又是从图状态里读 `status` **反推**出来的，任一处漏改都会
    让另一处的判断失真。

    `extra_ok` 是「执行本身有没有成」（`report.ok` / `all_ok`）：
    它为假时无论复核结论如何都是 `failed`。

    ## `changed` 为什么是必要条件（第一版漏了，被既有测试抓住）

    第一版只按 `agent_ok` / `project_ok` 判定，于是
    `tests/test_enterprise_sandbox_review.py::test_workflow_cannot_complete_when_
    independent_review_is_unverified` 立刻红：那一轮
    `DOCMIND_EXECUTION_MODE=enterprise` + `stage_enabled=True`，**runner 真的
    写了文件**，却拿到 `completed_unverified`。

    那条测试的名字就是一条**刻意的安全规则**：改动过项目的东西、独立验证没
    做成，就不许报完成。而 `24c845c` 的原作者也写过「一次纯只读侦察
    （change_count=0）本来就没打算跑测试」——所以这个结局**本来就只属于只读任务**。

    于是收窄成：`completed_unverified` **只在没有任何文件改动时**才给。
    改动过东西而缺独立验证 -> 仍是 `failed`。这是保守方向，也保住了既有守卫。
    """
    if not extra_ok or not agent_ok:
        return STATUS_FAILED
    if project_ok:
        return STATUS_COMPLETED
    # 只有「内容确实过了、缺的正是独立验证、且没动过任何文件」才算完成但未验证。
    # `independent_review` 里另一个 ok=False 的取值是 `failed`
    # （"独立测试失败或沙箱不可用"）——**那是真失败**，不能一起放行。
    if str(project_status or "") == "unverified" and not changed:
        return STATUS_COMPLETED_UNVERIFIED
    return STATUS_FAILED


@dataclass
class WorkflowState:
    workflow_id: str
    project_id: str = ""
    project_root: str = ""
    # 领域画像：generic（通用开发，默认）/ game（游戏）/ eda 等；决定兜底选项、
    # 兜底任务 DAG 与生成器口吻。旧状态文件无此字段时在 _load 迁移为 "game"。
    kind: str = DEFAULT_KIND
    request: str = ""
    status: str = "awaiting_choice"
    phase: str = "clarify"
    sources: list[str] = field(default_factory=list)
    route_messages: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    selected_option: dict[str, Any] | None = None
    tasks: list[dict[str, Any]] = field(default_factory=list)
    pending_tasks: list[dict[str, Any]] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    # Deterministic recovery choices derived from the failed execution evidence.
    recovery: dict[str, Any] = field(default_factory=dict)
    acceptance_contract: dict[str, Any] = field(default_factory=dict)
    # Review 阶段收集的运行期证据：引擎日志是否有输出、是否无错误、是否晚于
    # 最后一次代码改动。游戏工作流的 runtime:engine 验收项只认它。
    runtime_evidence: dict[str, Any] = field(default_factory=dict)
    # 项目级可复用能力摘要；详细凭据和连接参数永不写入这里。
    project_profile: dict[str, Any] = field(default_factory=dict)
    # 终态工作流的脱敏自我复盘，供续跑和用户验收使用。
    self_review: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] = field(default_factory=dict)
    # Project files captured before the first side-effecting execution wave.
    # The orchestration checkpoint above is insufficient to restore files.
    project_checkpoint: dict[str, Any] = field(default_factory=dict)
    stage_enabled: bool = False
    project_stage: dict[str, Any] = field(default_factory=dict)
    # Temporary capability boundary for the current execution wave.
    capability_lease: dict[str, Any] = field(default_factory=dict)
    # 自检沙箱租约：计划获批时一次性批准的边界（仅本项目窗口的鼠标键盘输入，
    # 有 TTL、可收回）。它不授予任何工具能力，终端/前台任意窗口/Win 键仍禁。
    sandbox_lease: dict[str, Any] = field(default_factory=dict)
    visual_feedback: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Longer durable history for the workbench timeline.  ``events`` remains
    # the bounded SSE replay window; timeline keeps enough context to inspect
    # a long run without making the live stream unbounded.
    timeline: list[dict[str, Any]] = field(default_factory=list)
    interrupt_reason: str = ""
    error: str = ""
    replans: int = 0
    subagent_retries: dict[str, int] = field(default_factory=dict)
    steps: int = 0
    policy: dict[str, Any] = field(default_factory=dict)
    experience_enabled: bool = False
    skill_candidate: dict[str, Any] = field(default_factory=dict)
    llm_enabled: bool = True
    execution_session_id: str = ""
    context_layers: dict[str, Any] = field(default_factory=dict)
    graph_state: dict[str, Any] = field(default_factory=dict)
    # Optional LangSmith realtime root/child correlation.  Only IDs and
    # counters are persisted; prompts, tool arguments and outputs stay local.
    langsmith_trace: dict[str, Any] = field(default_factory=dict)
    # Bounded, content-free counters for the live workbench.  Keep this as a
    # first-class projection so clients do not need to rescan/trust event
    # payloads from concurrent Subagent waves.
    observability: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


_SENSITIVE = re.compile(
    r"(?i)(?:api[_-]?key|token|password|passwd|secret|private[_-]?key)\s*[:=]\s*\S+"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return _SENSITIVE.sub(lambda m: m.group(0).split("=", 1)[0] + "=[REDACTED]", text)[:limit]


def _review_code_root(state: WorkflowState) -> str:
    """复核/取证该看哪份代码：试做区存在时是副本，否则是真实项目根。

    试做区启用后，子代理的代码改动、`game_screenshot` 产出的
    `.docmind/screenshots/*.png`、`engine_start` 写入的 `.docmind/engine.log`
    **全在副本里**。此前这里写死 `state.project_root`，于是：

    * `output_audit` 拿相对路径去真实项目找截图 -> 打不开 -> 证据审计失败；
    * `runtime_evidence` 读真实项目的日志 -> 空 -> 运行期证据判「没跑过」。

    这与 `_review_project` 的 `review_root` 是同一个判据。注意只影响**读**
    的根：审批台账与自检沙箱租约仍按 origin（真实项目）记账，见
    `project_stage_scope(origin_root=...)`。
    """
    stage = state.project_stage or {}
    workspace = str(stage.get("workspace_root") or "")
    if workspace and os.path.isdir(workspace):
        return workspace
    return state.project_root


def review_output(result: Mapping[str, Any] | None, *, max_tool_failures: int = 3,
                  question: str = "", code_root: str = "",
                  require_evidence: bool | None = None) -> dict[str, Any]:
    """Programmatic safety/quality gate for a subagent or aggregate result."""
    from .output_audit import audit_final_output
    report = audit_final_output(result, max_tool_failures=max_tool_failures,
                                question=question, code_root=code_root,
                                require_evidence=require_evidence)
    report["failures"] = report.get("tool_failure_count", 0)
    return report


def _experience_outcome(review: Any, status: str) -> tuple[str, str]:
    """这次工作流该按什么结局进经验库 -> ``(outcome, verdict 短句)``。

    ## 为什么不能直接读 `status`

    实测（四次只读真跑落盘的 experience 事件，形状完全一致）：

        {"kind": "experience", "recorded": false, "outcome": "repeated-fail"}

    那一轮四任务全 ok、十项检查全 True、`evidence_verified`、引用不过 0 条
    —— **全程成功**，只因为 `status` 是 `failed`（`review.ok` 含 `project.ok`，
    而它 `unverified`），就被记成「反复失败」。

    这条比展示层的同类问题**更要紧**：经验库会被 `experience.recall_similar()`
    检索出来**指导后续规划**，所以一次完全成功的分析一旦记进去就成了**反面教材**。
    目前 `recorded: false` 只是因为嵌入不可用静默降级（Ollama 没起）、库还是
    空的——**一旦嵌入可用就会真的写进去**。

    ## 为什么映射到 success 而不是新增取值

    `experience._OUTCOMES = ("success", "fail-then-fixed", "repeated-fail")`
    是**封闭集合**，第 91 行就校验。而「缺独立验证」不落在后两者任何一格里：
    活儿干完了，只是这次结果不构成独立验证。保留意见放进 `verdict` 那一行——
    **标签不撒谎，披露也不丢**。

    ## 反向守卫

    `agent_ok` 为假、或 `project.status == "failed"`（独立测试真失败）、
    或 `project` 这一项整个缺失（未知）—— 三种都仍记 `repeated-fail`。
    只有「内容确实过了、且缺的是独立验证」这一种才记 `success`。

    ## 与本族其他三处是同一个判据

    `workflow_reflection._review_wording`（58ae8f5）、
    `frontend reviewHeadlineOf`（ddf1df5）、
    `build_recovery_plan` 的 `_workflow_changed_files`（3a5f8dc）。
    前端无法共享（语言边界），后端这几处各写一行并互相指向——
    这是**已知的重复**，不要在只改一处的情况下以为其他处也修好了。
    """
    rv = review if isinstance(review, Mapping) else {}
    project = rv.get("project") if isinstance(rv.get("project"), Mapping) else {}
    content_only = bool(rv.get("agent_ok") is True
                         and project.get("status") == "unverified")
    if status == "completed" or content_only:
        return "success", ("内容通过·未做独立验证" if content_only else "通过")
    return "repeated-fail", "未通过"


def _workflow_changed_files(raw: Mapping[str, Any]) -> bool:
    """这一轮**实际改动了项目文件**吗？

    ## 为什么要有「未知」这一档

    回滚是一个**破坏性**动作的前置按钮，所以判据的方向必须是「宁可多给」。
    复核结果缺失时（崩溃恢复路径上 `state.review` 可能还是空的）我们**不知道**
    有没有改动——此时必须当作**有改动**处理，否则会把用户真正需要的回滚入口
    一起藏掉。

    实测里唯一能确定「没改动」的依据是独立复核那一项：

        review.project.change_count / changes / diff

    三者一致为 0 才算「没改动」。
    """
    review = raw.get("review")
    if not isinstance(review, Mapping) or not review:
        return True  # 复核没跑过 -> 未知 -> 不抑制
    project_review = review.get("project")
    if not isinstance(project_review, Mapping):
        return True  # 同上：项目复核这一项不存在，别当成「零改动」
    try:
        change_count = int(project_review.get("change_count") or 0)
    except (TypeError, ValueError):
        return True
    return bool(project_review.get("changes")) or change_count > 0


def build_recovery_plan(state: Mapping[str, Any] | WorkflowState | None,
                        *, error: str = "") -> dict[str, Any]:
    """Build bounded, actionable recovery guidance from durable evidence.

    Only task IDs, counts and error categories are exposed.  Tool arguments,
    file contents and model output are deliberately excluded from this public
    projection; the existing task trace remains the detailed evidence view.
    """
    raw = state.public() if isinstance(state, WorkflowState) else dict(state or {})
    report = dict(raw.get("results") or {})
    result_map = report.get("results") if isinstance(report.get("results"), Mapping) else report
    failed: list[str] = []
    uncertain: list[str] = []
    failure_kinds: set[str] = set()
    for task_id, item in (result_map or {}).items():
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        kind = str(item.get("error_kind") or "")
        text = str(item.get("error") or "")
        if status in {"failed", "blocked"}:
            failed.append(str(task_id))
            if kind:
                failure_kinds.add(kind[:60])
        trace = item.get("trace") if isinstance(item.get("trace"), Mapping) else {}
        steps = trace.get("steps") if isinstance(trace, Mapping) else []
        candidates = list(steps) if isinstance(steps, (list, tuple)) else []
        candidates.append(item)
        if any(isinstance(step, Mapping) and (
                step.get("error_kind") == "idempotency_in_doubt" or
                "不确定" in str(step.get("obs") or "") or
                "idempotency_in_doubt" in str(step.get("error") or "")
        ) for step in candidates) or kind == "idempotency_in_doubt" or "不确定执行" in text:
            uncertain.append(str(task_id))
    failed = list(dict.fromkeys(failed))[:32]
    uncertain = list(dict.fromkeys(uncertain))[:32]
    options: list[dict[str, Any]] = []
    if uncertain:
        options.append({
            "id": "inspect_uncertain_side_effect",
            "action": "inspect",
            "title": "先确认不确定的副作用",
            "detail": "工具可能已经执行但结果未落账，系统已阻止重放；请先检查外部状态，再决定是否继续。",
            "task_ids": uncertain,
            "requires_user": True,
        })
    if failed:
        options.append({
            "id": "retry_failed_tasks",
            "action": "retry_failed",
            "title": "重试失败任务",
            "detail": "保留已完成任务，只重试失败或阻塞的任务；每个任务仍受原有重试上限约束。",
            "task_ids": failed,
            "requires_user": True,
        })
    changed = _workflow_changed_files(raw)
    if changed and raw.get("project_checkpoint", {}).get("id"):
        options.append({
            "id": "restore_project_checkpoint",
            "action": "rollback",
            "title": "恢复执行前项目快照",
            "detail": "恢复快照中已有文件，快照之后新建的文件会保留；恢复动作仍需用户确认。",
            "task_ids": [],
            "requires_user": True,
        })
    if not options:
        if not failed and not uncertain and not changed:
            # **什么都没发生就不要编一个「需要用户审核的失败下一步」。**
            #
            # 实测（`wf-f609ea8daf22` / `wf-fc3cf86c5eaf` / `wf-fd543f79c197` /
            # `wf-e042489bf0fe` / `wf-af6a2e4a9b2b`，五次真跑形状完全一致）：
            # 四任务全 `ok`、`failed_task_ids: []`、`uncertain_task_ids: []`、
            # `review.project.change_count = 0`、`changes = []`、`diff = ""`，
            # 却照样产出 `status: "required"` +「恢复执行前项目快照」。
            #
            # 而那一轮 `project_checkpoint.file_count = 87`——那 87 个文件**全都
            # 是改动前就存在的**，这轮一个文件都没改。用户看到的是「失败后的下一步 ·
            # 需要用户审核」外加一个恢复快照按钮，点下去只是把没动过的文件
            # 「恢复」一遍。
            #
            # 这里**不碰** `review.project.status == "unverified"`：那是正确的安全
            # 声明（试做区没有容器级命令隔离），它照旧显示在复核区里。修的是
            # **恢复计划凭空造出不存在的动作**。
            #
            # 注意条件里带了 `not changed`：**有改动但没快照**（所以给不出回滚）
            # 时仍然要留下「查看失败证据」那条路——那时确实有事要用户处理，
            # 一并静默掉就成了另一种「看起来没事」。
            return {}
        options.append({
            "id": "inspect_failure_evidence",
            "action": "inspect",
            "title": "查看失败证据后重新规划",
            "detail": "当前没有可安全自动重试的任务，请先查看工具轨迹和验收条件，再修改计划。",
            "task_ids": [],
            "requires_user": True,
        })
    details = []
    if failed:
        details.append("失败/阻塞任务 %d 个" % len(failed))
    if uncertain:
        details.append("不确定副作用 %d 个" % len(uncertain))
    if failure_kinds:
        details.append("错误类别：" + "、".join(sorted(failure_kinds)[:4]))
    return {
        "status": "required",
        "generated_at": _now(),
        "summary": ("；".join(details) or _clean_text(error, 240) or "执行未通过复核"),
        "evidence": {"failed_task_ids": failed, "uncertain_task_ids": uncertain},
        "options": options[:3],
    }


class GameWorkflowManager:
    """Persistent workflow state manager used by HTTP/UI and tests."""

    def __init__(self, state_root: str | None = None, router: ContextRouter | None = None,
                 checkpoint_dsn: str | None = None):
        self.state_root = Path(state_root or STATE_ROOT) / ".docmind" / "workflows"
        self.router = router or ContextRouter()
        self._states: dict[str, WorkflowState] = {}
        # Callbacks are process-local by design (they may close over an Agent
        # client and must never be persisted into workflow JSON).  Keeping the
        # start-time option generator here lets a direct manager caller get
        # the same post-research LLM refinement as the HTTP adapter.
        self._option_generators: dict[str, Callable[[str, ContextPlan], Any]] = {}
        # Research and plan callbacks are process-local adapters around the
        # existing audited providers/LLM client.  The graph invokes them from
        # durable nodes; only the callback itself is ephemeral.
        self._research_runners: dict[str, Callable[[str], Any]] = {}
        self._task_generators: dict[str, Callable[[str, Mapping[str, Any], ContextPlan], Any]] = {}
        self._execution_callbacks: dict[str, dict[str, Any]] = {}
        self._execution_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None
        self._lock = threading.RLock()
        # ── 实时事件总线（进程内 SSE）─────────────────────────────────────
        # 持久台账 state.events 仍是唯一事实源；这里只做进程内即时 fan-out：
        # 每个 SSE 连接一个有界队列（满则丢最旧，绝不阻塞执行线程）。
        # _event_seq 是单调序号：持久台账裁剪为最后 100 条后，events[0] 的
        # 序号恒为 _event_seq-len(events)+1，SSE 重放据此计算 after 游标。
        self._subscribers: dict[str, set[queue.Queue]] = {}
        self._event_seq: int = 0
        # 子代理逐步轨迹只存内存（不进持久 JSON）：每任务有界环形，终态即清；
        # 迟到/刷新补全走 subagent_complete 事件里携带的有界 trace。
        self._step_rings: dict[str, dict[str, deque]] = {}
        self._step_ring_max = max(8, min(120, int(os.getenv("DOCMIND_WORKFLOW_STEP_RING", "30"))))
        self._checkpoint_conn: sqlite3.Connection | None = None
        self._checkpoint_pool: Any | None = None
        self._lease_owner = "wf-worker-" + uuid.uuid4().hex[:16]
        self._lease_ttl_s = max(30, min(3600, int(os.getenv("DOCMIND_WORKFLOW_LEASE_S", "120"))))
        self._capability_lease_ttl_s = max(
            300, min(7200, int(os.getenv("DOCMIND_CAPABILITY_LEASE_S", "1800"))))
        self._leases: dict[str, tuple[int, int]] = {}
        self._checkpoint_error = ""
        self._checkpoint_backend = "native"
        self._checkpoint_schema_version: int | None = None
        self._checkpoint_health_state = "not_configured"
        configured_dsn = (checkpoint_dsn or os.getenv("DOCMIND_CHECKPOINT_POSTGRES_DSN") or
                          os.getenv("LANGGRAPH_CHECKPOINT_POSTGRES_DSN") or "").strip()
        self._checkpoint_dsn_configured = bool(configured_dsn)
        self._checkpoint_required = os.getenv(
            "DOCMIND_CHECKPOINT_POSTGRES_REQUIRED", "0").strip().lower() in {
                "1", "true", "yes", "on"}
        if configured_dsn and PostgresSaver is not None and ConnectionPool is not None:
            try:
                pool_size = max(1, min(32, int(os.getenv("DOCMIND_CHECKPOINT_POOL_SIZE", "4"))))
                try:
                    pool_timeout = max(0.1, min(120.0, float(
                        os.getenv("DOCMIND_CHECKPOINT_POOL_TIMEOUT_S", "30"))))
                except (TypeError, ValueError):
                    pool_timeout = 30.0
                self._checkpoint_pool = ConnectionPool(
                    conninfo=configured_dsn, min_size=1, max_size=pool_size,
                    kwargs={"autocommit": True}, timeout=pool_timeout, open=True,
                )
                self._graph_checkpointer = PostgresSaver(self._checkpoint_pool)
                # setup() is idempotent and creates the saver tables on a new
                # database.  Existing schemas are left intact for migrations.
                self._graph_checkpointer.setup()
                with self._checkpoint_pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS docmind_checkpoint_schema (
                                component TEXT PRIMARY KEY,
                                version INTEGER NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        """)
                        cur.execute("""
                            SELECT version FROM docmind_checkpoint_schema
                            WHERE component = 'workflow'
                        """)
                        schema_row = cur.fetchone()
                        if schema_row and int(schema_row[0]) > CHECKPOINT_SCHEMA_VERSION:
                            raise RuntimeError(
                                "checkpoint_schema_newer:%s" % int(schema_row[0]))
                        cur.execute("""
                            INSERT INTO docmind_checkpoint_schema
                                (component, version, updated_at)
                            VALUES ('workflow', %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (component) DO UPDATE SET
                                version = EXCLUDED.version,
                                updated_at = CURRENT_TIMESTAMP
                        """, (CHECKPOINT_SCHEMA_VERSION,))
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS docmind_workflow_leases (
                                workflow_id TEXT PRIMARY KEY,
                                owner_id TEXT NOT NULL,
                                fencing_token BIGINT NOT NULL DEFAULT 0,
                                expires_at TIMESTAMPTZ NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        """)
                self._checkpoint_backend = "langgraph-postgres"
                self._checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
                self._checkpoint_health_state = "ok"
            except Exception as exc:
                self._checkpoint_error = type(exc).__name__
                self._checkpoint_health_state = "error"
                pool, self._checkpoint_pool = self._checkpoint_pool, None
                if pool is not None:
                    try:
                        pool.close()
                    except Exception:
                        pass
                if self._checkpoint_required:
                    raise RuntimeError(
                        "postgres_checkpoint_required:%s" % self._checkpoint_error) from exc
        elif configured_dsn:
            self._checkpoint_error = "postgres_dependencies_unavailable"
            self._checkpoint_health_state = "error"
            if self._checkpoint_required:
                raise RuntimeError(
                    "postgres_checkpoint_required:%s" % self._checkpoint_error)
        # The bundled Python 3.13 runtime has shown intermittent native
        # access violations while closing LangGraph's sqlite connection.
        # Workflow state is already persisted as bounded JSON, so keep the
        # in-process checkpointer in memory by default.  Deployments that
        # explicitly need LangGraph's sqlite saver can opt in, and a caller
        # that supplied a checkpoint DSN keeps the existing diagnostic path.
        sqlite_checkpoint_enabled = os.getenv(
            "DOCMIND_LANGGRAPH_SQLITE_CHECKPOINT", "1").strip().lower() in {
                "1", "true", "yes", "on"}
        if (self._checkpoint_backend == "native" and SqliteSaver is not None and
                (sqlite_checkpoint_enabled or bool(configured_dsn))):
            try:
                self.state_root.mkdir(parents=True, exist_ok=True)
                self._checkpoint_conn = sqlite3.connect(
                    str(self.state_root / "langgraph_checkpoints.sqlite"),
                    timeout=30, check_same_thread=False,
                )
                self._checkpoint_conn.execute("PRAGMA journal_mode=WAL")
                self._checkpoint_conn.execute("PRAGMA synchronous=NORMAL")
                self._graph_checkpointer = SqliteSaver(self._checkpoint_conn)
                self._checkpoint_backend = "langgraph-sqlite"
            except Exception as exc:
                self._checkpoint_error = self._checkpoint_error or type(exc).__name__
                if self._checkpoint_conn is not None:
                    self._checkpoint_conn.close()
                    self._checkpoint_conn = None
                self._graph_checkpointer = MemorySaver() if MemorySaver is not None else None
                if self._graph_checkpointer is not None:
                    self._checkpoint_backend = "langgraph-memory"
        else:
            if self._checkpoint_backend == "native":
                self._graph_checkpointer = MemorySaver() if MemorySaver is not None else None
                if self._graph_checkpointer is not None:
                    self._checkpoint_backend = "langgraph-memory"

    def close(self) -> None:
        """Close the persistent checkpoint connection (useful for tests/servers)."""
        conn, self._checkpoint_conn = self._checkpoint_conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        pool, self._checkpoint_pool = self._checkpoint_pool, None
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass
        try:
            close_persistent_compression_worker(str(self.state_root.parent / "context_compression.sqlite3"))
        except Exception:
            pass

    @property
    def checkpoint_backend(self) -> str:
        return self._checkpoint_backend

    @property
    def checkpoint_error(self) -> str:
        return self._checkpoint_error

    @property
    def persistent_checkpoint(self) -> bool:
        return self._checkpoint_backend in {"langgraph-sqlite", "langgraph-postgres"}

    @property
    def distributed_leases(self) -> bool:
        return self._checkpoint_backend == "langgraph-postgres" and self._checkpoint_pool is not None

    def checkpoint_health(self, *, probe: bool = True) -> dict[str, Any]:
        """Return safe checkpoint diagnostics and optionally probe Postgres."""
        if self._checkpoint_backend == "langgraph-postgres" and probe and self._checkpoint_pool is not None:
            try:
                with self._checkpoint_pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                self._checkpoint_health_state = "ok"
            except Exception as exc:
                self._checkpoint_health_state = "error"
                self._checkpoint_error = "health_probe_%s" % type(exc).__name__
        return {
            "backend": self._checkpoint_backend,
            "configured": self._checkpoint_dsn_configured,
            "required": self._checkpoint_required,
            "healthy": self._checkpoint_health_state == "ok" or (
                self._checkpoint_backend in {"langgraph-sqlite", "langgraph-memory", "native"}
                and not self._checkpoint_error),
            "state": self._checkpoint_health_state,
            "schema_version": self._checkpoint_schema_version,
            "error": self._checkpoint_error,
        }

    def _acquire_workflow_lease(self, workflow_id: str) -> int:
        if not self.distributed_leases:
            return 0
        with self._lock:
            active = self._leases.get(workflow_id)
            if active is not None:
                count, token = active
                self._leases[workflow_id] = (count + 1, token)
                return token
        try:
            with self._checkpoint_pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO docmind_workflow_leases
                            (workflow_id, owner_id, fencing_token, expires_at)
                        VALUES (%s, %s, 1,
                                CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'))
                        ON CONFLICT (workflow_id) DO UPDATE SET
                            owner_id = EXCLUDED.owner_id,
                            fencing_token = docmind_workflow_leases.fencing_token + 1,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE docmind_workflow_leases.expires_at < CURRENT_TIMESTAMP
                           OR docmind_workflow_leases.owner_id = EXCLUDED.owner_id
                        RETURNING fencing_token
                    """, (workflow_id, self._lease_owner, self._lease_ttl_s))
                    row = cur.fetchone()
            if not row:
                raise WorkflowError("workflow_lease_unavailable:%s" % workflow_id)
            token = int(row[0])
            with self._lock:
                self._leases[workflow_id] = (1, token)
            return token
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("workflow_lease_error:%s" % type(exc).__name__) from exc

    def _release_workflow_lease(self, workflow_id: str) -> None:
        if not self.distributed_leases:
            return
        with self._lock:
            active = self._leases.get(workflow_id)
            if active is None:
                return
            count, token = active
            if count > 1:
                self._leases[workflow_id] = (count - 1, token)
                return
            self._leases.pop(workflow_id, None)
        try:
            with self._checkpoint_pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        DELETE FROM docmind_workflow_leases
                        WHERE workflow_id = %s AND owner_id = %s AND fencing_token = %s
                    """, (workflow_id, self._lease_owner, token))
        except Exception:
            # The lease will expire if release cannot reach Postgres.
            pass

    def _refresh_workflow_lease(self, workflow_id: str) -> int:
        if not self.distributed_leases:
            return 0
        with self._lock:
            active = self._leases.get(workflow_id)
        if active is None:
            return self._acquire_workflow_lease(workflow_id)
        token = active[1]
        try:
            with self._checkpoint_pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE docmind_workflow_leases
                        SET expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE workflow_id = %s AND owner_id = %s AND fencing_token = %s
                        RETURNING fencing_token
                    """, (self._lease_ttl_s, workflow_id, self._lease_owner, token))
                    row = cur.fetchone()
            if not row:
                raise WorkflowError("workflow_lease_lost:%s" % workflow_id)
            return int(row[0])
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("workflow_lease_refresh_error:%s" % type(exc).__name__) from exc

    @contextmanager
    def _workflow_lease(self, workflow_id: str):
        self._acquire_workflow_lease(workflow_id)
        try:
            yield
        finally:
            self._release_workflow_lease(workflow_id)

    def set_execution_resolver(self, resolver: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None) -> None:
        """Register a process-local callback factory used after restart.

        The resolver receives only persisted public state and may return the
        same runner/synth/replanner callback mapping accepted by ``execute``.
        It is intentionally never serialized into workflow state.
        """
        self._execution_resolver = resolver

    def recover_pending(self) -> list[dict[str, Any]]:
        """Resume approved executions after a process restart.

        Only workflows that already recorded an execution session and were
        approved are eligible.  The resolver rebuilds process-local callbacks;
        failures stay durable and can be retried by the next scheduler pass.
        """
        if self._execution_resolver is None:
            return []
        recovered: list[dict[str, Any]] = []
        with self._lock:
            paths = list(self.state_root.glob("*.json")) if self.state_root.is_dir() else []
        for path in paths:
            workflow_id = path.stem
            try:
                # 扫描只读、不注册：不合格的门控态绝不能因为启动扫描被塞进
                # _states，否则每个历史 awaiting_choice 都会在重启后武装项目
                # 互斥（旧实现这里是反复死锁的根因）。真正恢复时 execute()
                # 内部的 _load 会重新注册。
                state = self._load(workflow_id, register=False)
            except WorkflowError:
                continue
            policy = state.policy or {}
            if state.status not in {"planned", "executing"} or not state.execution_session_id \
                    or policy.get("approval_mode") != "high" or not state.tasks:
                continue
            try:
                with self._workflow_lease(workflow_id):
                    self._workflow_hook(state, "recovery", recovered=False,
                                        session_id=state.execution_session_id)
                    resolved = self._execution_resolver(state.public())
                    if not isinstance(resolved, Mapping) or not callable(resolved.get("runner")):
                        raise WorkflowError("execution_resolver_unavailable")
                    if state.status == "executing":
                        # The previous process may have died after a side effect;
                        # the tool idempotency ledger makes retrying this wave safe.
                        state.status, state.phase = "planned", "execute"
                        self._event(state, "recovery_requeue", session_id=state.execution_session_id)
                        self._save(state)
                    self._execution_callbacks[workflow_id] = dict(resolved)
                    result = self.execute(workflow_id, **dict(resolved),
                                          session_id=state.execution_session_id)
                    self._workflow_hook(state, "recovery", recovered=True,
                                        session_id=state.execution_session_id)
                    recovered.append({"workflow_id": workflow_id,
                                      "status": result.get("status", "unknown")})
            except Exception as exc:
                error_name = type(exc).__name__
                if not str(exc).startswith("workflow_lease_"):
                    self._event(state, "recovery_failed", error=error_name)
                    state.recovery = build_recovery_plan(state, error="进程恢复失败：" + error_name)
                    self._save(state)
                    recovered.append({"workflow_id": workflow_id, "status": "retry_pending",
                                      "error": error_name})
                else:
                    recovered.append({"workflow_id": workflow_id, "status": "lease_busy",
                                      "error": error_name})
        return recovered

    def __del__(self):  # pragma: no cover - interpreter shutdown ordering
        # Avoid touching optional/native resources while CPython is tearing
        # down modules.  The explicit close() path remains authoritative.
        if getattr(sys, "is_finalizing", lambda: False)():
            return
        try:
            self.close()
        except Exception:
            pass

    def _path(self, workflow_id: str) -> Path:
        return self.state_root / (re.sub(r"[^A-Za-z0-9_-]", "", workflow_id) + ".json")

    def _save(self, state: WorkflowState, *, register: bool = True,
              strict: bool = False) -> WorkflowState:
        state.updated_at = _now()
        if state.status in self._TERMINAL_STATUSES:
            self._release_capability_lease(state)
        if state.status in self._TERMINAL_STATUSES or state.status == "interrupted":
            self._release_playtest_sandbox(state)
        if state.results or state.review:
            state.preview = build_preview_bundle(state.public())
        if state.status in self._TERMINAL_STATUSES or state.status == "interrupted":
            state.self_review = build_self_review(state.public())
        if state.project_root:
            try:
                state.project_profile = merge_profile(state.project_root, state.public())
            except (OSError, ValueError, TypeError):
                # 画像是增强信息，不能让主工作流因项目目录暂时不可写而失败。
                if not state.project_profile:
                    state.project_profile = load_profile(state.project_root)
        # Persist bounded content snapshots, not only layer counters. This is
        # the cross-window handoff consumed after a process/checkpoint restart.
        raw_layers = dict(state.context_layers or {})
        raw_layers.update({
            "route": {"sources": list(state.sources),
                      "messages": list(state.route_messages[-8:])},
            "project": {"project_id": state.project_id, "root": state.project_root},
            "task": {"tasks": [dict(item) for item in state.tasks[:16]]},
            "subagent": {"subagents": [dict(item) for item in state.subagents[:16]],
                         "results": {str(key): summarize_subagent_result(value, 900)[0]
                                     for key, value in list((state.results.get("results") or {}).items())[:16]}},
            "output": {"review": state.review, "status": state.status,
                       "merged": str((state.results or {}).get("merged") or "")},
        })
        # 压缩快照是**派生账本**（跨窗口交接用的补充材料），主状态才是事实来源。
        # 所以这里绝不能让它的失败拖垮 `_save`：实测 `wf-3784257fd6d1` 里
        # `context_compression.json` 的一次 `os.replace` 撞上 Windows 的
        # `PermissionError: [WinError 5]`，异常从这行直接冒出 `_save`，
        # **主状态根本没写下去**，`/choose` 于是返回 500。
        #
        # 派生材料写不进去就如实标成 `valid: False`，状态照常落库——
        # 这也符合本文件一贯的取舍：增强信息不能成为主流程的单点。
        try:
            snapshots, snapshot_meta = compress_layers_durable(
                raw_layers, int((state.policy or {}).get("max_context_chars", 6000)),
                db_path=str(self.state_root.parent / "context_compression.sqlite3"))
            snapshot_meta["valid"] = True
        except Exception as exc:  # noqa: BLE001 —— 派生账本失败不得阻断状态落库
            snapshots, snapshot_meta = {}, {
                "valid": False,
                "digest": "",
                "error": "%s: %s" % (type(exc).__name__, exc)[:200],
            }
            self._event(state, "context_snapshot_unavailable",
                        error=snapshot_meta["error"],
                        reason="上下文快照写不进去，本次状态照常保存；"
                               "跨窗口交接会缺少这些快照")
        state.context_layers["snapshots"] = snapshots
        state.context_layers["snapshot_meta"] = snapshot_meta
        state.context_layers["snapshot_digest"] = snapshot_meta["digest"]
        state.context_layers["schema"] = "five-layer-v1"
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            self._path(state.workflow_id).write_text(
                json.dumps(state.public(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            if strict:
                raise WorkflowError("工作流记录保存失败") from exc
        if register or state.workflow_id in self._states or is_terminal_status(state.status):
            self._states[state.workflow_id] = state
        if is_terminal_status(state.status):
            try:
                from .langsmith import export_workflow, finish_workflow
                finish_workflow(state.public())
                # Keep the ledger export as a compatibility fallback for old
                # LangSmith installations and for operators who explicitly
                # want a second immutable summary run.
                if not (state.langsmith_trace or {}).get("root_run_id") or \
                        os.getenv("DOCMIND_LANGSMITH_EXPORT_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}:
                    export_workflow(state.public())
            except Exception:
                pass
            self._option_generators.pop(state.workflow_id, None)
            self._research_runners.pop(state.workflow_id, None)
            self._task_generators.pop(state.workflow_id, None)
            self._execution_callbacks.pop(state.workflow_id, None)
        return state

    def _load(self, workflow_id: str, *, register: bool = True) -> WorkflowState:
        """载入工作流状态。

        register=True（变更路径专用）时把磁盘态注册进进程内 _states，从而
        参与项目互斥；register=False（只读路径：GET/SSE 存在校验/启动扫描）
        时只返回状态、不武装互斥。门控态（awaiting_choice 等）按既定策略不
        随进程重启恢复（见 recover_pending），若只读访问也把它们塞进 _states，
        旧标签页 SSE 重连或启动扫描就会让早已无人审批的历史工作流永久堵死
        同项目的新请求。显式 POST（choose/approve/interrupt/execute…）表示
        用户确实要继续它，这些方法仍走 register=True。
        """
        with self._lock:
            if workflow_id in self._states:
                return self._states[workflow_id]
            path = self._path(workflow_id)
            if not path.is_file():
                raise WorkflowError("工作流不存在：%s" % workflow_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                legacy = "kind" not in raw
                state = WorkflowState(**raw)
                if state.project_root:
                    state.project_profile = load_profile(state.project_root)
                timeline_migrated = False
                self_review_migrated = False
                if state.status in self._TERMINAL_STATUSES and not state.self_review:
                    state.self_review = build_self_review(state.public())
                    self_review_migrated = True
                if not isinstance(state.timeline, list):
                    state.timeline = []
                    timeline_migrated = True
                if not state.timeline and state.events:
                    # Migrate states written before the durable timeline was
                    # introduced.  Keep the same dict objects so sequence tags
                    # added below are visible in both projections.
                    state.timeline = list(state.events)
                    timeline_migrated = True
                elif state.events:
                    known = {int(item.get("seq") or 0) for item in state.timeline
                             if isinstance(item, Mapping)}
                    missing = [item for item in state.events
                               if isinstance(item, Mapping) and
                               int(item.get("seq") or 0) not in known]
                    if missing:
                        state.timeline.extend(missing)
                        state.timeline = state.timeline[-500:]
                        timeline_migrated = True
                if legacy:
                    # 领域画像引入前的历史工作流全部按游戏领域处理，保证旧
                    # 状态恢复后选项/兜底文案与创建时一致，可继续 choose/plan。
                    state.kind = "game"
            except Exception as exc:
                raise WorkflowError("工作流状态损坏：%s" % type(exc).__name__) from exc
            if self._rebuild_context_layers(state):
                # A process/window restart must leave a usable snapshot, not
                # merely rebuild counters.  Older states (and interrupted
                # writes) may contain the logical five layers but no durable
                # snapshot body or a stale digest.  Recreate it synchronously
                # during recovery so the next window gets the same bounded
                # context immediately, even if the background queue is still
                # warming up.
                logical_layers = {
                    key: state.context_layers.get(key, {})
                    for key in CONTEXT_LAYERS
                }
                snapshots, snapshot_meta = compress_layers(
                    logical_layers,
                    int((state.policy or {}).get("max_context_chars", 6000)),
                )
                snapshot_meta = dict(snapshot_meta, valid=True,
                                     job_state="recovery_sync",
                                     result_digest=layer_snapshot_digest(snapshots))
                state.context_layers["snapshots"] = snapshots
                state.context_layers["snapshot_meta"] = snapshot_meta
                state.context_layers["snapshot_digest"] = snapshot_meta["digest"]
                try:
                    path.write_text(json.dumps(state.public(), ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                except OSError:
                    pass
            self._tag_event_seqs(state)
            if timeline_migrated or self_review_migrated:
                try:
                    path.write_text(json.dumps(state.public(), ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                except OSError:
                    pass
            # 终态不参与互斥（_active_workflow_for 显式跳过），但必须缓存：
            # 否则刷新后打开历史终态卡片时 SSE 的 is_terminal() 读不到它，
            # 连接无法自关，退化为永久心跳连接。
            if register or state.status in self._TERMINAL_STATUSES:
                self._states[workflow_id] = state
            return state

    def _tag_event_seqs(self, state: WorkflowState) -> None:
        """给历史台账（无 seq 字段的旧事件）补齐单调序号，并推进全局游标。"""
        advanced = False
        for event in state.events:
            if isinstance(event, Mapping) and not event.get("seq"):
                self._event_seq += 1
                event["seq"] = self._event_seq
                advanced = True
        if advanced:
            self._event_seq = max(self._event_seq, len(state.events))

    def subscribe(self, workflow_id: str, *, maxsize: int = 256) -> queue.Queue:
        """注册一个 SSE 订阅队列（每连接独立，有界，满则丢最旧）。

        原子校验 + 注册：SSE handler 顺序通常是
        ``WORKFLOWS.get(wid) → require_workflow_project → subscribe``。
        如果在 `get` 与 `subscribe` 之间 workflow 已被 `delete_workflow`
        清掉，旧实现会照常注册 queue；后续 SSE 循环里
        `is_terminal(wid)` 永远 False（state 不在内存），但
        `events_since` 返回 `(None, latest_seq)`，循环一直 ping 且
        队列泄漏（`_subscribers[wid]` 持续持有已删 workflow 的 queue）。

        现在 subscribe 在同一 `with self._lock` 内同时校验 state 存
        在 + 注册；state 不在就抛 WorkflowError，由 SSE handler 转 404。
        """
        q: queue.Queue = queue.Queue(maxsize=max(32, min(2048, int(maxsize))))
        with self._lock:
            state = self._states.get(workflow_id)
            if state is None and not self._path(workflow_id).is_file():
                raise WorkflowError("工作流不存在：%s" % workflow_id)
            self._subscribers.setdefault(workflow_id, set()).add(q)
        return q

    def unsubscribe(self, workflow_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(workflow_id)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subscribers.pop(workflow_id, None)

    def events_since(self, workflow_id: str, after: int = 0):
        """重放 seq 大于 after 的持久事件；返回 (events|None, latest_seq)。

        工作流不在内存时返回 (None, seq)，由调用方决定 404 或安静关闭。
        """
        with self._lock:
            state = self._states.get(workflow_id)
            if state is None:
                return None, self._event_seq
            self._tag_event_seqs(state)
            after = max(0, int(after or 0))
            events = [dict(event) for event in state.events
                      if isinstance(event, Mapping) and int(event.get("seq") or 0) > after]
            return events, self._event_seq

    def is_terminal(self, workflow_id: str) -> bool:
        with self._lock:
            state = self._states.get(workflow_id)
            return bool(state and is_terminal_status(state.status))

    def active_for_project(self, project_id: str = "", project_root: str = "") -> dict[str, Any] | None:
        """同项目进程内未终结工作流的公开状态（与 start() 互斥判定同源）。

        供前端在页面刷新/会话切换后发现「后端仍存活、本地卡片已丢失」的孤儿
        工作流。刻意不扫描磁盘 JSON：进程重启后门控态不参与互斥（见 start()
        与 recover_pending），若此处 _load 会把 awaiting_choice 重新注册进
        _states，反而重新制造死锁，故可见性必须与互斥口径完全一致。
        """
        with self._lock:
            state = self._active_workflow_for(project_id, project_root)
            return state.public() if state is not None else None

    _TERMINAL_STATUSES = TERMINAL_STATUSES  # 别名：老引用点保留，值只有一个来源

    @staticmethod
    def _task_done_count(raw: Mapping[str, Any], tasks: list[Any]) -> int:
        """历史列表的“已完成”数：从执行结果统计，不读计划节点。

        `state.tasks` 是规划出来的 DAG，条目本身**没有** status；真正的结局
        在 `state.results["results"][task_id].status`。旧写法数
        `tasks[*].status == "done"` 恒为 0——实测 wf-57af9cdb2f18 卡片显示
        3/3，历史弹层却写「已完成 · 0/3 个任务」。这里与卡片
        `completedCount`（status === 'ok'）保持同一口径。
        """
        results = raw.get("results")
        result_map = results.get("results") if isinstance(results, Mapping) else {}
        if not isinstance(result_map, Mapping):
            result_map = {}
        done = {str(task_id) for task_id, item in result_map.items()
                if isinstance(item, Mapping)
                and str(item.get("status") or "") == "ok"}
        task_ids = [str(item.get("id") or "")
                    for item in tasks if isinstance(item, Mapping)]
        if task_ids and all(task_ids):
            return sum(1 for task_id in task_ids if task_id in done)
        return len(done)

    def list_workflows(self, project_id: str = "", project_root: str = "",
                       limit: int = 50) -> list[dict[str, Any]]:
        """列出项目相关的最近工作流摘要（只读扫盘）。

        工作台「工作流历史」消费：运行中可中断、终态可删除。与
        recover_pending 同原则——纯扫描绝不 register，避免把门控态塞进
        _states 武装互斥。已在内存中的状态（含当前活跃工作流）优先取内存，
        保证看到最新的 status/updated_at。

        ## 2026-10-14：AND 语义 + Windows 路径规范化

        之前用 OR 匹配（任一字段相等即视为同一项目）→ 混绑 state
        （A 项目的 project_id + B 项目的 project_root）会被 A、B 任一
        项目看见。改为所有**已提供**的绑定条件必须全部匹配（AND），
        并对路径做 `os.path.normcase(normpath(...))` 归一，避免
        Windows 大小写/尾斜杠/symlink 绕过。两者皆空时返回所有工作
        流（与 `active_for_project` 同口径），保持离线/测试用法的
        既有用法。
        """
        limit = max(1, min(int(limit or 50), 200))
        want_pid = str(project_id or "").strip()
        want_root = _canonical_project_root(project_root)
        with self._lock:
            paths = list(self.state_root.glob("*.json")) if self.state_root.is_dir() else []
        items: list[dict[str, Any]] = []
        for path in paths:
            wid = path.stem
            try:
                with self._lock:
                    state = self._states.get(wid)
                    raw: Mapping[str, Any] = state.public() if state is not None \
                        else json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, Mapping):
                continue
            raw_pid = str(raw.get("project_id") or "").strip()
            raw_root = _canonical_project_root(raw.get("project_root") or "")
            if (want_pid or want_root):
                # 只要求【已提供】的条件匹配：没提供的维度是通配符，不是
                # 「state 的该字段必须为空」。旧写法
                # `(not want_root and not raw_root)` 把「没问 root」当成
                # 「state 必须没有 root」，于是只传 project_id 时任何带 root
                # 的 state 都被滤掉——工作台历史弹层按项目筛会凭空少条目，
                # 互斥锁在只带 root 的启动路径下也会漏判（同项目双开）。
                # 缺失绑定字段（查询侧或 state 侧）都按通配处理，由另一维约束。
                # 只放宽查询侧不够：对话内 start_workflow 走 code_root、可能
                # 落出没有 project_id 的 state，它对带项目头的查询既不可见、
                # 也不参与互斥 → 同项目双开。
                pid_ok = (not want_pid) or (not raw_pid) or (want_pid == raw_pid)
                root_ok = (not want_root) or (not raw_root) or (want_root == raw_root)
                if not (pid_ok and root_ok):
                    continue
            tasks = raw.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            done = self._task_done_count(raw, tasks)
            items.append({
                "workflow_id": str(raw.get("workflow_id") or wid),
                "project_id": raw_pid,
                "project_root": raw_root,
                "status": str(raw.get("status") or ""),
                "phase": str(raw.get("phase") or ""),
                "kind": str(raw.get("kind") or ""),
                "request": str(raw.get("request") or ""),
                "task_count": len(tasks),
                "task_done": done,
                "error": str(raw.get("error") or ""),
                "interrupt_reason": str(raw.get("interrupt_reason") or ""),
                "created_at": str(raw.get("created_at") or ""),
                "updated_at": str(raw.get("updated_at") or ""),
            })
        items.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        return items[:limit]

    def delete_workflow(self, workflow_id: str) -> dict[str, Any]:
        """删除终态工作流：弹出内存态并清理磁盘 JSON。

        非终态（仍在等待审批/执行）拒绝删除，必须先 interrupt，避免删掉一个
        后台还在跑、之后无 UI 可触达的孤儿。
        """
        with self._lock:
            path = self._path(workflow_id)
            state = self._states.get(workflow_id)
            if state is None and not path.is_file():
                raise WorkflowError("工作流不存在：%s" % workflow_id)
            status = state.status if state is not None else None
            if state is None:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    status = str(raw.get("status") or "") if isinstance(raw, Mapping) else ""
                except (OSError, ValueError):
                    status = ""
            if status not in self._TERMINAL_STATUSES:
                raise WorkflowError(
                    "只能删除已结束（completed/failed/interrupted）的工作流，"
                    "运行中请先中断")
            self._states.pop(workflow_id, None)
            self._subscribers.pop(workflow_id, None)
            self._option_generators.pop(workflow_id, None)
            self._research_runners.pop(workflow_id, None)
            self._task_generators.pop(workflow_id, None)
            self._execution_callbacks.pop(workflow_id, None)
            try:
                path.unlink()
            except OSError as exc:
                raise WorkflowError("工作流文件删除失败：%s" % exc) from exc
        return {"ok": True, "workflow_id": workflow_id}

    def _active_workflow_for(self, project_id: str, project_root: str) -> WorkflowState | None:
        """同一项目下尚未终结的工作流；调用方须持 self._lock。

        两条生产启动路径都会解析出真实 project_root（project_id 为补充匹配）。
        两者皆空（直接实例化的测试/离线调用）时不做互斥，保持单 manager
        多目标的既有用法。

        ## 2026-10-14：AND 语义 + Windows 路径规范化

        之前用 OR 匹配：`state.project_root == project_root or
        state.project_id == project_id`。混绑 state（A 项目 id + B 项目
        root）会被任意一边命中 → 跨项目可见。改为：所有**已提供**的
        绑定条件必须全部匹配（AND），缺一不可；并把路径通过
        `os.path.normcase(normpath(...))` 归一，避免 Windows 大小写/尾
        斜杠/symlink 绕过。`projects.project_id` 也走同样归一，所以
        project_id 与 canonical root 一定相容。
        """
        if not project_root and not project_id:
            return None
        want_pid = str(project_id or "").strip()
        want_root = _canonical_project_root(project_root)
        for state in self._states.values():
            if state.status in self._TERMINAL_STATUSES:
                continue
            have_pid = str(state.project_id or "").strip()
            have_root = _canonical_project_root(state.project_root)
            # 与 list_workflows 同口径：只要求已提供的条件匹配。旧写法要求
            # 「没提供的维度上 state 也必须为空」，于是只带 project_root 的
            # 启动路径（对话内 start_workflow 按 code_root 解析）看不到
            # 带 project_id 的活跃工作流 → 同项目双开，互斥锁失效。
            # 同 list_workflows：state 侧缺失的绑定字段是通配，不能当成不匹配。
            # 只带 project_root 的启动路径产出的 state 没有 project_id，
            # 否则它对带项目头的查询不可见，互斥锁漏判 → 同项目双开。
            pid_ok = (not want_pid) or (not have_pid) or (want_pid == have_pid)
            root_ok = (not want_root) or (not have_root) or (want_root == have_root)
            if pid_ok and root_ok:
                return state
        return None

    def record_agent_step(self, workflow_id: str, task_id: str, role: str,
                          item: Mapping[str, Any]) -> None:
        """记录一条子代理实时轨迹（thought/action/observation），以及工具生命周期事件。

        thought/action/observation 只进内存有界环 + 订阅 fan-out，不写持久台账
        （避免每步落盘/JSON 膨胀）；持久与迟到补全由 subagent_complete 携带的
        trace 负责。

        工具生命周期（before_tool/after_tool/before_mcp/after_mcp）**要写进持久
        事件流**：开发舱「实际工具调用」面板就是按这些 kind 从持久化事件数组里
        过滤的。此前这些事件只进子代理自己的 hook 缓冲，从不落到工作流事件流，
        于是面板恒为「0 条记录」——而同一屏的任务面板里明明列着 read_file /
        grep / python_exec。面板没撒谎，它读的那条流里确实没有工具事件。
        """
        step_type = str((item or {}).get("type") or "")
        if step_type not in {"thought", "action", "observation"} | set(SINK_FORWARDED_HOOKS):
            return
        text = _clean_text((item or {}).get("text") or "", 1200)
        try:
            with self._lock:
                state = self._states.get(workflow_id)
                if state is None:
                    return
                self._event_seq += 1
                seq = self._event_seq
                rings = self._step_rings.setdefault(workflow_id, {})
                ring = rings.get(str(task_id))
                if ring is None:
                    ring = deque(maxlen=self._step_ring_max)
                    rings[str(task_id)] = ring
                ring.append({"seq": seq, "type": step_type, "text": text,
                             "ts": _now()})
                event = {"seq": seq, "ts": _now(), "kind": "subagent_step",
                         "task_id": str(task_id or "")[:80],
                         "role": str(role or "")[:60],
                         "step_type": step_type, "text": text}
                self._fanout_locked(workflow_id, event)
                if step_type in SINK_FORWARDED_HOOKS:
                    # 面板要的是这些 kind 本身，所以在这里落成真事件。
                    # 只记 name/ok/耗时这类摘要，不复制参数与结果正文——
                    # 那两份已经在 observation 里，重复一遍只是双倍计数。
                    self._event(state, step_type,
                                task_id=str(task_id or "")[:80],
                                role=str(role or "")[:60], text=text)
        except Exception:
            pass

    def agent_steps(self, workflow_id: str) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {task_id: list(ring)
                    for task_id, ring in (self._step_rings.get(workflow_id) or {}).items()}

    def _fanout_locked(self, workflow_id: str, event: Mapping[str, Any]) -> None:
        """非阻塞投递给所有订阅者；队列满则丢最旧，执行线程永不等待。"""
        for q in list(self._subscribers.get(workflow_id, ())):
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    @staticmethod
    def _rebuild_context_layers(state: WorkflowState) -> bool:
        """Reconstruct bounded five-layer metadata after process/checkpoint recovery."""
        layers = dict(state.context_layers or {})
        changed = False
        route = dict(layers.get("route") or {})
        if not route.get("messages") and state.route_messages:
            route["messages"] = list(state.route_messages[-8:])
            changed = True
        if not route.get("sources") and state.sources:
            route["sources"] = list(state.sources)
            changed = True
        if route != (layers.get("route") or {}):
            layers["route"] = route
            changed = True
        project = dict(layers.get("project") or {})
        for key, value in (("project_id", state.project_id), ("root", state.project_root)):
            if key not in project and value:
                project[key] = value
                changed = True
        if project != (layers.get("project") or {}):
            layers["project"] = project
            changed = True
        task = dict(layers.get("task") or {})
        if not task.get("ids") and state.tasks:
            task.update({"count": len(state.tasks),
                         "ids": [item.get("id") for item in state.tasks]})
            changed = True
        if task != (layers.get("task") or {}):
            layers["task"] = task
            changed = True
        subagent = dict(layers.get("subagent") or {})
        if not subagent.get("statuses") and state.subagents:
            subagent["statuses"] = {str(item.get("id")): item.get("status")
                                     for item in state.subagents if item.get("id")}
            subagent["count"] = len(state.subagents)
            changed = True
        if subagent != (layers.get("subagent") or {}):
            layers["subagent"] = subagent
            changed = True
        output = dict(layers.get("output") or {})
        if "review_ok" not in output and state.review:
            output.update({"review_ok": bool(state.review.get("ok")), "steps": state.steps})
            changed = True
        if output != (layers.get("output") or {}):
            layers["output"] = output
            changed = True
        budgets = allocate_layer_budgets(int((state.policy or {}).get("max_context_chars", 6000)))
        if layers.get("budgets") != budgets:
            layers["budgets"] = budgets
            changed = True
        if layers.get("schema") != "five-layer-v1":
            layers["schema"] = "five-layer-v1"
            changed = True
        snapshots = layers.get("snapshots")
        snapshot_meta = dict(layers.get("snapshot_meta") or {})
        if isinstance(snapshots, Mapping):
            valid = layer_snapshot_digest(snapshots) == str(snapshot_meta.get("digest") or "")
            if not valid or snapshot_meta.get("valid") != valid:
                snapshot_meta["valid"] = valid
                layers["snapshot_meta"] = snapshot_meta
                changed = True
        else:
            # Missing snapshot bodies are recoverable, but must trigger the
            # synchronous rebuild in _load rather than leaving only counters.
            changed = True
        if changed:
            state.context_layers = layers
        return changed

    def _note_role_capability(self, state: WorkflowState,
                              tasks: Iterable[Mapping[str, Any]]) -> None:
        """派发前报出「任务要写、角色却不能写」的错配（**只报不拦**）。

        先只报不拦：拦下来要改规划器的重试语义，而「哪些动词算要写」这个判据
        本身有误报风险——宁可先让人看见，别先让人动不了。

        实测依据（四次真实工作流）：`wf-9f121dec38a6` 的三个任务全是
        `researcher`/`researcher`/`planner`，**整轮没有一个人有权写文件**，
        而目标就是写一个文件。那不是模型偷懒，是任务在结构上无法完成——
        而界面上看不出这件事，只能看到「任务失败」。

        这与 `_run_child` 里那道闸是同一族问题：它拦住「角色**放大**工具范围」，
        却没人检查「任务是否**超出**角色能力」。两边合起来才完整。
        """
        try:
            items = [item for item in (tasks or ()) if isinstance(item, Mapping)]
        except Exception:
            return
        for item in items:
            role = str(item.get("role") or "")
            task_id = str(item.get("id") or "")
            task_text = str(item.get("task") or "")
            if not role or not task_text or role_can_write(role):
                continue
            if not task_wants_write(task_text):
                continue
            self._event(
                state, "task_role_mismatch", task_id=task_id[:80], role=role[:60],
                reason="任务要求写文件，但该角色的工具列表里没有写类工具"
                       "（%s），它在结构上无法完成这个任务" % role,
                writable_roles=",".join(
                    sorted(name for name in _SUBAGENT_ROLES if role_can_write(name))))

    def _stage_scope_probe(self, state: WorkflowState, task: Mapping[str, Any],
                           token: Any) -> None:
        """试做区隔离的探针：设了根之后，各处读到的根是不是同一个。

        ## 为什么需要

        隔离失效被观测到过两次，两次的表象相同（`python_exec` 报的执行目录是真实
        项目），但**机理可能是两个**，修法完全不同：

        (a) `set_context_code_root` 之后根被**改回去了**（某个并发的兄弟任务或
            中间层用同一个 contextvar 写了别的值）；
        (b) 根设对了，但工具执行发生在**另一个 context** 里（线程没复制
            contextvar），于是它读到的是自己那份默认值。

        这两者在「事件里看到根不对」面前长得一模一样。此前三次静态排查之所以
        判「机制正常」，就是因为只验了 (a) 不成立就直接结了案——而那次验的
        是在**主线程**里做的。

        所以这里在两个位置各读一次：刚 `set` 完（此刻必然是试做区），以及
        派发之前。两处不一致 = (a)；一致但工具仍报真实项目 = (b)。

        探针不阻断：读不出来就如实记「探不到」，不能让诊断把工作流带崩。
        """
        want = str(state.project_stage.get("workspace_root") or "")
        try:
            from tools import _get_code_root
            now = str(_get_code_root() or "")
        except Exception as exc:  # noqa: BLE001 —— 探针不得让工作流失败
            now = "探不到：%s: %s" % (type(exc).__name__, exc)
        try:
            from agent_runtime.tools import stage_scope_active
            scope_on = bool(stage_scope_active())
        except Exception:
            scope_on = None
        self._event(state, "stage_scope_probe",
                    task_id=str(task.get("id") or "")[:80],
                    want_root=want, actual_root=now,
                    scope_active=scope_on,
                    matches=(now == want))

    def _llm_wait_event(self, workflow_id: str, kind: str, text: str) -> None:
        """把「等模型」事件落到**正在执行的那个 state 对象**上。

        必须拿调用方手里同一份 `WorkflowState`——`_event` 是就地改
        `state.events` / `state.timeline` 的，拿到另一个副本就等于写到没人保存
        的对象上。`_load` 返回的正是 `self._states[workflow_id]` 那个对象，
        所以从这里取是安全的（已核：`_load` 第1002-1003 行直接返回缓存）。

        图节点的闭包只拿到 `WorkflowGraphState`（一个 dict），不是
        `WorkflowState`，所以这里按 `workflow_id` 回取，而不是让每个节点
        都自己想办法传。
        """
        wid = str(workflow_id or "")
        if not wid:
            return
        with self._lock:
            state = self._states.get(wid)
        if state is None:
            # 只读路径（GET/SSE/启动扫描）故意不把状态注册进 _states，
            # 那时没有可写的对象——静默跳过即可，不影响执行。
            return
        self._event(state, kind, text=text)

    def _event(self, state: WorkflowState, kind: str, **payload: Any) -> None:
        # LangGraph Send runs subagents concurrently. Serialize the bounded
        # event ledger so live UI polling and durable snapshots never observe
        # a torn list or lose an event during a parallel wave.
        with self._lock:
            self._event_seq += 1
            event = {"seq": self._event_seq, "ts": _now(), "kind": kind, **payload}
            state.events.append(event)
            state.events = state.events[-100:]
            state.timeline.append(dict(event))
            state.timeline = state.timeline[-500:]
            telemetry = dict(state.observability or {})
            now_epoch = time.time()
            telemetry.setdefault("started_epoch", now_epoch)
            for counter in ("events", "subagents_started", "subagents_completed",
                            "tool_events", "mcp_events", "failures", "elapsed_ms",
                            "prompt_tokens", "completion_tokens", "total_tokens",
                            "subagent_tool_steps"):
                telemetry.setdefault(counter, 0)
            telemetry["last_event_at"] = _now()
            telemetry["events"] = int(telemetry.get("events", 0)) + 1
            if kind == "subagent_start":
                telemetry["subagents_started"] = int(telemetry.get("subagents_started", 0)) + 1
            # ``task_complete`` is the DAG bookkeeping echo emitted after the
            # worker's ``subagent_complete`` event; count the worker event once.
            if kind == "subagent_complete":
                telemetry["subagents_completed"] = int(telemetry.get("subagents_completed", 0)) + 1
                # 子代理实际消耗的工具步数按任务归并（重试/重规划后同 id 以最近一次为准，
                # 不重复累计），实时供 GET 状态展示；execute 收尾会再用最终结果校准一次。
                by_task = telemetry.get("subagent_steps_by_task")
                if not isinstance(by_task, dict):
                    by_task = {}
                task_key = str(payload.get("task_id") or "")
                if task_key:
                    try:
                        by_task[task_key] = max(0, int(payload.get("steps") or 0))
                    except (TypeError, ValueError):
                        by_task[task_key] = 0
                    telemetry["subagent_steps_by_task"] = by_task
                    telemetry["subagent_tool_steps"] = int(
                        sum(int(v or 0) for v in by_task.values()))
            if kind in {"before_tool", "after_tool", "tool_call"}:
                telemetry["tool_events"] = int(telemetry.get("tool_events", 0)) + 1
            if kind in {"before_mcp", "after_mcp", "mcp_retry", "mcp_call"}:
                telemetry["mcp_events"] = int(telemetry.get("mcp_events", 0)) + 1
            if payload.get("status") in {"failed", "blocked"} or kind in {
                    "task_blocked", "hook_error", "replan_failed", "auto_execute_reconstruct_failed"}:
                telemetry["failures"] = int(telemetry.get("failures", 0)) + 1
            elapsed = payload.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                telemetry["elapsed_ms"] = int(telemetry.get("elapsed_ms", 0)) + max(0, int(elapsed))
            usage = payload.get("tokens") or payload.get("token_usage")
            if isinstance(usage, Mapping):
                for source, target in (("in", "prompt_tokens"), ("input", "prompt_tokens"),
                                       ("out", "completion_tokens"), ("output", "completion_tokens"),
                                       ("total", "total_tokens")):
                    value = usage.get(source)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        telemetry[target] = int(telemetry.get(target, 0)) + max(0, int(value))
            telemetry["duration_ms"] = max(0, int((now_epoch - float(telemetry["started_epoch"])) * 1000))
            state.observability = telemetry
            try:
                from .langsmith import record_workflow_event
                record_workflow_event(state.public(), kind, payload)
                if state.langsmith_trace:
                    state.langsmith_trace["event_count"] = int(
                        state.langsmith_trace.get("event_count", 0)) + 1
            except Exception:
                pass
            # SSE 实时 fan-out（最后做：此时事件已落台账，重放/实时两路顺序一致）
            self._fanout_locked(state.workflow_id, event)
            if is_terminal_status(state.status):
                # 终态后实时环不再需要（完整 trace 已随 subagent_complete 落台账）
                self._step_rings.pop(state.workflow_id, None)

    def _workflow_hook(self, state: WorkflowState, kind: str, **payload: Any) -> None:
        """Run a lifecycle hook with the workflow's explicit failure policy."""
        try:
            import hooks as workflow_hooks
            result = workflow_hooks.run_workflow(kind, {
                "workflow_id": state.workflow_id,
                "phase": state.phase,
                "status": state.status,
                **{str(key): value for key, value in payload.items()
                   if str(key) in {"node", "wave", "task_count", "attempt", "approved",
                                   "session_id", "recovered", "error"}},
            })
        except Exception as exc:
            result = {"blocked": False, "reason": "", "errors": [
                {"hook": kind, "error": type(exc).__name__}]}
        errors = list(result.get("errors") or [])
        if errors:
            self._event(state, "hook_error", hook=kind, error_count=len(errors))
        blocked = bool(result.get("blocked"))
        policy = (state.policy or {}).get("hook_failure", "continue")
        if blocked or (errors and policy == "block"):
            reason = str(result.get("reason") or "工作流钩子失败")[:300]
            state.status = "interrupted"
            state.interrupt_reason = reason
            self._event(state, "hook_blocked", hook=kind, reason=reason)
            self._save(state)
            raise WorkflowError("workflow_hook_blocked:%s" % kind)

    def _ensure_capability_lease(self, state: WorkflowState,
                                 *, ttl_seconds: int | float | None = None,
                                 capabilities: Iterable[str] | _UNSET = _UNSET) -> dict[str, Any]:
        """Create or reuse the temporary capability scope for execution.

        `capabilities` 三种含义：
        - 未传（sentinel）→ 默认授予全部 Capability；
        - 显式可迭代对象 → 严格按调用方列出的能力授予；
        - 显式空列表/集合 → **零权限**，与未传区分。

        之前的实现把 `None` 和 `[]` 混为一谈：调用方明确传空集合也会被
        解读为「给我全部」，等于显式零权限被扩成全权限，这是同一族
        fail-open 风险。
        """
        now = time.time()
        current = dict(state.capability_lease or {})
        try:
            current_expiry = float(current.get("expires_at_epoch") or 0)
        except (TypeError, ValueError):
            current_expiry = 0.0
        if current.get("status") == "active" and current_expiry > now + 5:
            return current
        try:
            ttl = float(ttl_seconds if ttl_seconds is not None else self._capability_lease_ttl_s)
        except (TypeError, ValueError):
            ttl = float(self._capability_lease_ttl_s)
        ttl = max(300.0, min(7200.0, ttl))
        if capabilities is _UNSET:
            # 自动重建（例如过期后续跑/重试）沿用旧租约的能力集，不能借机
            # 把受限租约升成全能力；没有旧租约才是首次授予（全能力）。
            previous = [str(item) for item in (current.get("capabilities") or []) if str(item)]
            selected = previous if current else [item.value for item in Capability]
        else:
            selected = [str(item) for item in (capabilities or ()) if str(item)]
        expires = now + ttl
        lease = {
            "id": "lease-" + uuid.uuid4().hex[:16],
            "capabilities": list(dict.fromkeys(selected)),
            "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds"),
            "expires_at_epoch": expires,
            "status": "active",
            "source": "workflow",
        }
        state.capability_lease = lease
        self._event(state, "capability_lease_created", lease_id=lease["id"],
                    capabilities=lease["capabilities"], expires_at=lease["expires_at"])
        return lease

    def create_capability_lease(self, workflow_id: str, *, ttl_seconds: int | float | None = None,
                                capabilities: Iterable[str] | _UNSET = _UNSET) -> dict[str, Any]:
        """Explicitly arm a workflow capability lease for callers/tests.

        `capabilities` 同样区分未传（默认全能力）与显式空集合（零权限）。
        """
        state = self._load(workflow_id)
        if state.status in self._TERMINAL_STATUSES:
            raise WorkflowError("终态工作流不能创建工具权限租约")
        lease = self._ensure_capability_lease(state, ttl_seconds=ttl_seconds,
                                              capabilities=capabilities)
        return self._save(state).public().get("capability_lease") or dict(lease)

    def refresh_capability_lease(self, workflow_id: str, *,
                                 ttl_seconds: int | float | None = None) -> dict[str, Any]:
        """滑动续期：把仍在执行的 active 租约到期时间往后推。

        工作流动辄跑几小时，固定 30 分钟窗口会在中途把工具全部拦死。续期只
        延后 `expires_at`，保留原租约 id 与 capabilities（避免把受限租约升权）；
        已释放的租约、终态/中断状态不复活。
        """
        with self._lock:
            state = self._load(workflow_id)
            lease = dict(state.capability_lease or {})
            if not lease or lease.get("status") != "active":
                return lease
            if state.status in self._TERMINAL_STATUSES or state.status == "interrupted":
                return lease
            try:
                ttl = float(ttl_seconds if ttl_seconds is not None
                            else self._capability_lease_ttl_s)
            except (TypeError, ValueError):
                ttl = float(self._capability_lease_ttl_s)
            ttl = max(300.0, min(7200.0, ttl))
            now_ts = time.time()
            try:
                current_expiry = float(lease.get("expires_at_epoch") or 0)
            except (TypeError, ValueError):
                current_expiry = 0.0
            # 多个并行子代理会在同一窗口各调一次；租约还新鲜时不重复写盘。
            if current_expiry > now_ts + ttl * 0.5:
                return lease
            expires = now_ts + ttl
            lease["expires_at_epoch"] = expires
            lease["expires_at"] = datetime.fromtimestamp(
                expires, timezone.utc).isoformat(timespec="seconds")
            lease["renewed_at"] = _now()
            state.capability_lease = lease
            self._event(state, "capability_lease_renewed", lease_id=lease.get("id"),
                        expires_at=lease["expires_at"])
            self._save(state)
            return dict(lease)

    @staticmethod
    def _release_capability_lease(state: WorkflowState) -> None:
        lease = dict(state.capability_lease or {})
        if lease and lease.get("status") != "released":
            lease["status"] = "released"
            lease["released_at"] = _now()
            state.capability_lease = lease

    @staticmethod
    def _sandbox_approval_notice(state: WorkflowState) -> str:
        """游戏工作流在审批卡上展示的沙箱边界（为空=不授予）。"""
        if normalize_kind(state.kind) != "game" or not state.project_root:
            return ""
        return ("批准后同时开启自检沙箱：仅本项目引擎/嵌入窗口的点击与输入，"
                "30 分钟自动过期，工作流结束或中断即收回；"
                "终端、任意前台窗口与 Win 键仍禁用")

    def _grant_playtest_sandbox(self, state: WorkflowState, *, source: str) -> dict[str, Any]:
        """计划获批时开启一次自检沙箱；失败只记事件，绝不阻断审批本身。"""
        if normalize_kind(state.kind) != "game" or not state.project_root:
            return {}
        try:
            import game_workbench
            lease = game_workbench.grant_playtest_sandbox(
                state.project_root, "workflow:%s" % source,
                workflow_id=state.workflow_id)
        except Exception as exc:  # noqa: BLE001 - 沙箱失败=退回逐次审批
            self._event(state, "playtest_sandbox_error", error=type(exc).__name__)
            return {}
        state.sandbox_lease = dict(lease)
        self._event(state, "playtest_sandbox_granted",
                    lease_id=lease.get("id"),
                    expires_at=lease.get("expires_at"),
                    targets=list((lease.get("scope") or {}).get("targets") or []))
        return lease

    @staticmethod
    def _release_playtest_sandbox(state: WorkflowState) -> None:
        """工作流结束/中断时收回租约（幂等；进程重启后也能按磁盘状态收回）。"""
        if normalize_kind(state.kind) != "game" or not state.project_root:
            return
        try:
            import game_workbench
            lease = game_workbench.revoke_playtest_sandbox(
                state.project_root,
                reason="workflow_%s" % (state.status or "finished"))
        except Exception:  # noqa: BLE001 - 收回失败也不影响工作流收尾
            return
        if lease:
            state.sandbox_lease = dict(lease)

    def _graph_config(self, state: WorkflowState) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": state.workflow_id},
            "recursion_limit": max(
                32, int((state.policy or {}).get("max_replans", 2)) * 4 + 16),
        }

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def _graph_payload(self, state: WorkflowState, **updates: Any) -> WorkflowGraphState:
        payload: WorkflowGraphState = {
            "workflow_id": state.workflow_id,
            "kind": normalize_kind(state.kind),
            "request": state.request,
            "sources": list(state.sources),
            "options": list(state.options),
            "phase": state.phase,
            "status": state.status,
            "selected_option": state.selected_option or {},
            "approved": (state.policy or {}).get("approval_mode") == "high",
            "interrupt_reason": state.interrupt_reason,
            "tasks": [dict(task) for task in state.tasks],
            "results": dict(state.results.get("results") or {}) if state.results else {},
            "step_count": int(state.graph_state.get("step_count", state.steps or 0)),
            "replan_count": int(state.graph_state.get("replan_count", state.replans or 0)),
            "max_steps": int((state.policy or {}).get(
                "max_steps", workflow_step_limits()[0])),
            "max_replans": int((state.policy or {}).get("max_replans", 2)),
            "max_subagents": int((state.policy or {}).get("max_subagents", _default_max_agents())),
            "child_max_steps": int((state.policy or {}).get("child_max_steps", _default_child_max_steps())),
            "max_context_chars": int((state.policy or {}).get("max_context_chars", 6000)),
            "max_provider_retries": int((state.policy or {}).get("provider_retries", 2)),
            "langsmith_trace": dict(state.langsmith_trace or {}),
        }
        payload.update(updates)
        return payload

    def _graph_view(self, graph: Any, state: WorkflowState) -> dict[str, Any]:
        snapshot = graph.get_state(self._graph_config(state))
        values = dict(snapshot.values or {})
        pending: list[dict[str, Any]] = []
        for task in snapshot.tasks or ():
            for item in getattr(task, "interrupts", ()) or ():
                pending.append({
                    "id": str(getattr(item, "id", "")),
                    "value": self._json_safe(getattr(item, "value", None)),
                })
        values["pending_interrupts"] = pending
        values["checkpoint_backend"] = self._checkpoint_backend
        if self._checkpoint_error:
            values["checkpoint_error"] = self._checkpoint_error
        return self._json_safe(values)

    def _invoke_graph(self, state: WorkflowState, value: Any, *, graph: Any | None = None) -> dict[str, Any]:
        graph = graph or self.build_langgraph(workflow_id=state.workflow_id)
        if graph is None:
            return {}
        with self._workflow_lease(state.workflow_id):
            with self._lock:
                self._workflow_hook(state, "before_node", node="graph")
                graph.invoke(value, config=self._graph_config(state))
                view = self._graph_view(graph, state)
                state.graph_state = view
                self._workflow_hook(state, "after_node", node="graph")
                self._save(state)
                return view

    def _ensure_gate(self, state: WorkflowState, expected_kind: str) -> tuple[Any, dict[str, Any]]:
        graph = self.build_langgraph(workflow_id=state.workflow_id)
        if graph is None:
            return None, {}
        view = self._graph_view(graph, state)
        pending = list(view.get("pending_interrupts") or [])
        if not pending:
            self._invoke_graph(state, self._graph_payload(state), graph=graph)
            view = self._graph_view(graph, state)
            pending = list(view.get("pending_interrupts") or [])
        kind = (((pending[0] if pending else {}).get("value") or {}).get("kind"))
        if kind != expected_kind:
            raise WorkflowError("LangGraph 等待的是 %s，而不是 %s" % (kind or "无人工输入", expected_kind))
        return graph, view

    def _resume_gate(self, state: WorkflowState, expected_kind: str,
                     response: Mapping[str, Any]) -> dict[str, Any]:
        if Command is None:
            return {}
        graph, _view = self._ensure_gate(state, expected_kind)
        if graph is None:
            return {}
        return self._invoke_graph(state, Command(resume=dict(response)), graph=graph)

    def _ensure_code_index(self, state: WorkflowState, project_root: str,
                           project_id: str = "") -> None:
        """启动时保证「代码索引可用」——至少不能**静默**为空。

        ## 为什么需要

        实测（真跑 `wf-0c6374c4d612` / `wf-37f43a4c042f`）：探测项目从未 ingest，
        `search_code` 返回空，而**没有任何提示**。子代理拿不到任何代码证据，转去
        追 `search_knowledge` 里的无关文档，五步全花掉、任务失败。

        `ingest_code_directory` 只被 `/api/ingest_code` 调用（api.py:1913），
        工作流启动路径上完全没有索引检查——「项目没索引过 -> 工作流静默瞎跑」
        是默认行为。更糟的是 `tests/test_retrieval_degradation.py` 记着：嵌入
        后端不可用时 ingest 会**降级返回**（0 个代码向量也算成功），所以
        「调过 ingest」本身也不是保证。

        ## 三条不做

        1. **不改写全局 `code_root` 指针**——那是
           `tests/test_ingest_code_root_guard.py` 守着的���变量（带非当前项目头
           的请求绝不能污染它）。自动补索引用的可能是**别的**项目。
        2. **不停掉其它项目的引擎**——那是 `/api/ingest_code` 的「切换项目」
           语义；后台停别人的引擎是不可接受的副作用。
        3. **不在启动路径上同步做**——ingest 是重活（大仓要 walk + embedding），
           同步做会把「启动工作流」拖住。改成 daemon 后台线程。

        失败只记事件，**绝不让工作流失败**：索引是辅助能力
        （`test_retrieval_degradation.py` 同一句话——宁可没有，也不能让辅助能力
        绑住主能力）。
        """
        root = os.path.realpath(str(project_root or "").strip())
        if not root or not os.path.isdir(root):
            return
        try:
            import projects as _projects
            import vectorstore as _vs
            from ingest import ingest_code_directory as _ingest

            if not _projects.enabled():
                return
            pid = str(project_id or _projects.current_project_id() or "")
            if not pid:
                return
            collection = _projects.code_collection(pid)
            if _vs.count(collection) > 0:
                return
        except Exception:  # noqa: BLE001 —— 取不到就当没这条路径，不阻断启动
            return
        # 上面已经确认索引为空：先**说**清楚，再去补。
        self._event(state, "code_index_missing", root=root, collection=collection,
                    project=pid, note="代码索引为空，子代理用 search_code 检索不到"
                                      "任何内容；正在后台重建")
        key = "%s|%s" % (collection, root)
        with self._lock:
            if key in _CODE_INDEX_REBUILDING:
                return
            _CODE_INDEX_REBUILDING.add(key)

        def _rebuild() -> None:
            try:
                n = _ingest(root, collection=collection)
                self._event(state, "code_index_rebuilt", root=root,
                            collection=collection, project=pid, files=n,
                            note="代码索引已在后台重建完成")
            except Exception as exc:  # noqa: BLE001 —— 辅助能力失败不得牵连工作流
                self._event(state, "code_index_rebuild_failed", root=root,
                            collection=collection, project=pid,
                            error=type(exc).__name__,
                            note="代码索引重建失败；检索能力会退化，"
                                 "但工作流照常继续")
            finally:
                with self._lock:
                    _CODE_INDEX_REBUILDING.discard(key)

        threading.Thread(target=_rebuild, name="code-index-rebuild",
                         daemon=True).start()


    def start(self, request: str, *, project_id: str = "", project_root: str = "",
              web_enabled: bool = False, experience_enabled: bool = False,
              llm_enabled: bool = True, kind: str | None = None,
              skill_items: Iterable[dict[str, Any]] = (),
              policy: WorkflowPolicy | None = None,
              option_generator: Callable[[str, ContextPlan], Any] | None = None,
              research_runner: Callable[[str], Any] | None = None,
              task_generator: Callable[[str, Mapping[str, Any], ContextPlan], Any] | None = None,
              defer_option_generation: bool = False,
              stage_enabled: bool = False) -> dict[str, Any]:
        request = _clean_text(request, 12000)
        if not request:
            raise WorkflowError("开发目标不能为空")
        # 同一项目同时只允许一个未终结的工作流：HTTP 显式启动与对话内
        # start_workflow 工具共用本入口，否则会出现多个人工审批门叠加、多组
        # 子代理并发修改同一代码库，且其中一个卡片切走后审批永久失联。
        with self._lock:
            existing = self._active_workflow_for(project_id, project_root)
        if existing is not None:
            raise WorkflowError(
                "该项目已有进行中的工作流（ID：%s，状态：%s）。请先在对话中完成它的"
                "审批/执行或中断后，再启动新的工作流。"
                % (existing.workflow_id, existing.status))
        # 记录调用方是否显式指定步数预算（默认值与自动下限不可区分，视为未指定）；
        # 规划时显式低值也必须尊重，不能被自动预算覆盖。
        steps_lo, _steps_hi = workflow_step_limits()
        steps_explicit = (policy is not None
                          and int(getattr(policy, "max_steps", steps_lo) or 0)
                          != steps_lo)
        policy = (policy or WorkflowPolicy()).normalized()
        kind = normalize_kind(kind)
        profile = get_profile(kind)
        plan = self.router.route(request, code_root=project_root, web_enabled=web_enabled,
                                 experience_enabled=experience_enabled,
                                 skill_items=skill_items, token_budget=policy.max_context_chars)
        options = [WorkflowOption(**row) for row in
                   profile.option_dicts(request, plan.sources)]
        option_source = "deterministic"
        # With LangGraph available, option generation is performed by the
        # choice node and checkpointed alongside the first human gate.  Keep a
        # native fallback for installations that intentionally omit LangGraph.
        if StateGraph is None and llm_enabled and option_generator is not None:
            try:
                parsed = self._parse_generated_options(option_generator(request, plan), plan)
                if parsed:
                    options, option_source = parsed, "llm"
            except Exception as exc:
                plan = ContextPlan(sources=plan.sources, tool_groups=plan.tool_groups,
                                   skill_names=plan.skill_names,
                                   messages=tuple(plan.messages) + ("LLM 方案生成失败，已回退本地模板：" + type(exc).__name__,),
                                   budget_chars=plan.budget_chars, used_chars=plan.used_chars)
        wid = "wf-" + uuid.uuid4().hex[:12]
        state = WorkflowState(
            workflow_id=wid, project_id=project_id, project_root=project_root,
            stage_enabled=bool(stage_enabled and project_root),
            kind=kind,
            request=request, sources=list(plan.sources), route_messages=list(plan.messages),
            options=[asdict(item) for item in options], policy=asdict(policy),
            experience_enabled=bool(experience_enabled),
            llm_enabled=bool(llm_enabled),
            context_layers={"route": {"sources": list(plan.sources), "messages": list(plan.messages)},
                            "project": {"project_id": project_id, "root": project_root},
                            "task": {}, "subagent": {}, "output": {},
                            "budgets": allocate_layer_budgets(policy.max_context_chars),
                            "schema": "five-layer-v1"},
            created_at=_now(), updated_at=_now())
        state.policy["step_budget_explicit"] = bool(steps_explicit)
        defer_options = bool(defer_option_generation and llm_enabled and option_generator is not None
                             and StateGraph is not None and graph_interrupt is not None)
        # 索引为空要**说**出来并在后台补一次——见 _ensure_code_index 的 docstring。
        # 放在这里（所有路径都经过），不要放进 defer_options 分支：
        # 放进分支里就只有「延迟生成选项」那条路会检查索引，实测漏掉了。
        try:
            self._ensure_code_index(state, project_root, project_id)
        except Exception:  # noqa: BLE001 —— 索引是辅助能力，绝不阻断启动
            pass
        if defer_options:
            # Return the deterministic candidates immediately.  The HTTP layer
            # schedules the optional LLM refinement after the response so a
            # slow/temporarily unavailable provider cannot freeze the workbench.
            state.status = "generating_options"
            state.phase = "clarify"
            self._event(state, "options_pending", option_count=len(options),
                        option_source=option_source)
        try:
            from .langsmith import start_workflow
            state.langsmith_trace = start_workflow(state.public())
        except Exception:
            state.langsmith_trace = {}
        self._event(state, "clarify", option_count=len(options), sources=list(plan.sources),
                    option_source=option_source)
        with self._lock:
            if llm_enabled and option_generator is not None:
                self._option_generators[wid] = option_generator
            if research_runner is not None:
                self._research_runners[wid] = research_runner
            if task_generator is not None:
                self._task_generators[wid] = task_generator
            self._save(state)
            if StateGraph is not None and graph_interrupt is not None and not defer_options:
                graph_view = self._invoke_graph(state, self._graph_payload(
                    state, option_generation_pending=bool(llm_enabled and option_generator is not None)))
                pending_value = (((graph_view.get("pending_interrupts") or [{}])[0]).get("value") or {})
                generated_options = pending_value.get("options") or graph_view.get("options")
                if generated_options:
                    state.options = [dict(item) for item in generated_options]
                    self._event(state, "options_generated",
                                option_source=pending_value.get("option_source") or
                                graph_view.get("option_source") or "deterministic")
        return state.public()

    def generate_options(self, workflow_id: str) -> dict[str, Any]:
        """Complete deferred initial option generation in a worker thread."""
        state = self._load(workflow_id)
        if state.status != "generating_options":
            return state.public()
        graph = self.build_langgraph(workflow_id=workflow_id)
        if graph is None:
            state.status, state.phase = "awaiting_choice", "clarify"
            state.interrupt_reason = ""
            self._event(state, "options_generated", option_source="deterministic")
            return self._save(state).public()
        try:
            graph_view = self._invoke_graph(
                state,
                self._graph_payload(state, option_generation_pending=True),
                graph=graph,
            )
            generated_options = graph_view.get("options") or state.options
            state.options = [dict(item) for item in generated_options]
            state.status, state.phase = "awaiting_choice", "clarify"
            state.interrupt_reason = ""
            self._event(
                state,
                "options_generated",
                option_source=graph_view.get("option_source") or "deterministic",
                option_count=len(state.options),
            )
        except Exception as exc:  # noqa: BLE001 - deterministic options remain usable
            state.status, state.phase = "awaiting_choice", "clarify"
            state.interrupt_reason = ""
            state.error = "方案生成失败，已保留本地方案：" + type(exc).__name__
            self._event(state, "options_generation_failed", error=type(exc).__name__)
        return self._save(state).public()

    @staticmethod
    def _options(request: str, plan: ContextPlan) -> list[WorkflowOption]:
        """历史兼容入口：等价于 game 画像（改造前逐字段行为）。"""
        return [WorkflowOption(**row) for row in
                get_profile("game").option_dicts(request, plan.sources)]

    @staticmethod
    def _profile_options(kind: str, request: str,
                         sources: Iterable[str]) -> list[WorkflowOption]:
        return [WorkflowOption(**row) for row in
                get_profile(kind).option_dicts(request, tuple(sources or ()))]

    @staticmethod
    def _parse_generated_options(raw: Any, plan: ContextPlan) -> list[WorkflowOption]:
        """Validate strict JSON-ish model output and keep a safe fallback contract."""
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
            raw = json.loads(text)
        if isinstance(raw, Mapping):
            raw = raw.get("options")
        if not isinstance(raw, list):
            return []
        out: list[WorkflowOption] = []
        seen: set[str] = set()
        for item in raw[:6]:
            if not isinstance(item, Mapping):
                continue
            oid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or "option"))[:40]
            title = _clean_text(item.get("title"), 180)
            summary = _clean_text(item.get("summary"), 700)
            if not oid or oid in seen or not title or not summary:
                continue
            seen.add(oid)
            out.append(WorkflowOption(oid, title, summary, bool(item.get("recommended")),
                                       _clean_text(item.get("source") or "llm", 80),
                                       bool(item.get("requires_web"))))
        if not out:
            return []
        # Normalize the model's recommendation flag to exactly one option.
        # This keeps the UI contract deterministic even when a model marks
        # several candidates as recommended.
        recommended_seen = False
        normalized: list[WorkflowOption] = []
        for item in out:
            mark = bool(item.recommended) and not recommended_seen
            recommended_seen = recommended_seen or mark
            normalized.append(WorkflowOption(item.id, item.title, item.summary, mark,
                                              item.source, item.requires_web))
        if not recommended_seen:
            first = normalized[0]
            normalized[0] = WorkflowOption(first.id, first.title, first.summary, True,
                                            first.source, first.requires_web)
        out = normalized
        if not any(item.id == "custom" for item in out):
            out.append(WorkflowOption("custom", "我自己描述目标", "由用户补充更具体的效果、限制或参考作品。", False, "user"))
        if "web" not in plan.sources and not any(item.id == "web_research" for item in out):
            out.append(WorkflowOption("web_research", "联网补充资料后再选", "搜索最新资料，再重新生成方案选项。", False, "web", True))
        return out

    def choose(self, workflow_id: str, choice: str, *, custom_request: str = "",
               auto_execute: bool = False) -> dict[str, Any]:
        state = self._load(workflow_id)
        allowed = {"awaiting_choice", "generating_options"} if auto_execute \
            else {"awaiting_choice"}
        if state.status not in allowed:
            raise WorkflowError("当前工作流不在等待方案选择：%s" % state.status)
        choice = (choice or "").strip()
        if state.status == "generating_options" and choice in {"web_research", "custom"}:
            raise WorkflowError("方案仍在生成中，请直接选择一个已有方案或稍候再试")
        if choice == "web_research":
            state.status, state.phase = "awaiting_research", "research"
            state.interrupt_reason = "等待联网检索结果"
            graph_view = self._resume_gate(state, "choice", {"choice": choice})
            # A registered research runner completes inside the graph and the
            # next durable interrupt is already the refreshed choice gate.
            if graph_view.get("status") == "awaiting_choice" and graph_view.get("research_findings"):
                state.status, state.phase = "awaiting_choice", "clarify"
                state.interrupt_reason = ""
                state.options = [dict(item) for item in graph_view.get("options") or state.options]
                findings = _clean_text(graph_view.get("research_findings"), 8000)
                source = _clean_text(graph_view.get("research_source") or "web", 80)
                state.route_messages.append("【外部检索摘要】" + findings)
                if source not in state.sources:
                    state.sources.append(source)
                self._event(state, "research", source=source,
                            option_source=graph_view.get("option_source") or "deterministic",
                            findings_chars=len(findings), graph_node=True,
                            error=graph_view.get("research_error") or "")
            else:
                self._event(state, "interrupt", reason=state.interrupt_reason)
        elif choice == "custom":
            custom_request = _clean_text(custom_request, 12000)
            if not custom_request:
                raise WorkflowError("自定义方案需要补充描述")
            state.request = custom_request
            state.status, state.phase = "awaiting_choice", "clarify"
            state.options = [asdict(item) for item in self._profile_options(
                state.kind, custom_request, ("direct",))]
            self._resume_gate(state, "choice", {
                "choice": choice, "request": custom_request, "options": state.options,
            })
            self._event(state, "clarify", reason="custom_request")
        else:
            selected = next((item for item in state.options if item.get("id") == choice), None)
            if not selected:
                raise WorkflowError("未知方案选项：%s" % choice)
            state.selected_option = selected
            state.status, state.phase = "planning", "plan"
            state.interrupt_reason = ""
            if auto_execute:
                # 选方案即授权：HTTP 立即返回 planning，LangGraph 规划（慢 LLM）
                # 与后续审批/执行全部转入守护线程。规划耗时或整段子代理执行都
                # 不能再把前端卡片锁在「提交中」——进度经 SSE/轮询自然推进。
                self._event(state, "choice", option=selected)
                self._save(state)
                threading.Thread(
                    target=self._auto_plan_and_execute,
                    args=(workflow_id, choice, dict(selected)),
                    name="workflow-auto-run", daemon=True).start()
                return state.public()
            graph_view = self._resume_gate(state, "choice", {"choice": choice, "selected_option": selected})
            # A registered graph plan generator may have completed the plan
            # node during this resume. Mirror its durable result into the
            # public workflow state so clients can proceed directly to
            # approval/execute without a second planning request.
            if graph_view.get("tasks") and graph_view.get("status") in {"planned", "awaiting_approval"}:
                state.tasks = [dict(item) for item in graph_view.get("tasks") or []]
                state.acceptance_contract = acceptance_from_tasks(
                    state.tasks, runtime_required=(state.kind == "game"))
                state.status = "planned"
                state.phase = "execute"
                state.interrupt_reason = ""
                state.subagents = self._subagent_records(
                    state.tasks, int((state.policy or {}).get("child_max_steps", _default_child_max_steps())))
                state.context_layers["task"] = {"count": len(state.tasks),
                                                 "ids": [t.get("id") for t in state.tasks]}
                self._event(state, "plan", task_count=len(state.tasks),
                            task_source=graph_view.get("plan_source") or "llm", graph_node=True)
                self._note_plan_truncation(state, graph_view)
            self._event(state, "choice", option=selected)
        return self._save(state).public()

    def _auto_plan_and_execute(self, workflow_id: str, choice: str,
                               selected: Mapping[str, Any]) -> None:
        """Worker for ``choose(auto_execute=True)``: plan, auto-approve, execute.

        All slow work (planner LLM, task DAG execution) runs here so the HTTP
        choice request returns immediately.  Progress reaches the card through
        the durable SSE event stream; failures land in a durable ``failed``
        state instead of hanging the UI in "submitting".
        """
        try:
            state = self._load(workflow_id)
            if state.status != "planning":
                return  # interrupted or superseded while queued
            graph_view = self._resume_gate(
                state, "choice", {"choice": choice, "selected_option": dict(selected)})
            # The planner LLM runs under the manager lock; an interrupt requested
            # while waiting is applied as soon as it is released. Re-check
            # before mirroring the plan so a user stop is not overwritten.
            state = self._load(workflow_id)
            if state.status == "interrupted":
                return
            if not (graph_view.get("tasks")
                    and graph_view.get("status") in {"planned", "awaiting_approval"}):
                # Without a usable plan the workflow simply stays in planning;
                # the card still offers explicit planning/approval actions.
                self._save(state)
                return
            state.tasks = [dict(item) for item in graph_view.get("tasks") or []]
            state.acceptance_contract = acceptance_from_tasks(
                state.tasks, runtime_required=(state.kind == "game"))
            state.status = "planned"
            state.phase = "execute"
            state.interrupt_reason = ""
            state.subagents = self._subagent_records(
                state.tasks, int((state.policy or {}).get("child_max_steps", _default_child_max_steps())))
            state.context_layers["task"] = {"count": len(state.tasks),
                                             "ids": [t.get("id") for t in state.tasks]}
            self._event(state, "plan", task_count=len(state.tasks),
                        task_source=graph_view.get("plan_source") or "llm", graph_node=True)
            self._note_plan_truncation(state, graph_view)
            self._save(state)
            if self._load(workflow_id).status == "interrupted":
                return
            callbacks = self._execution_callbacks.get(workflow_id)
            if callbacks is None and self._execution_resolver is not None:
                try:
                    resolved = self._execution_resolver(state.public())
                    if isinstance(resolved, Mapping) and callable(resolved.get("runner")):
                        callbacks = dict(resolved)
                except Exception as exc:  # noqa: BLE001 - surfaced below as a gate
                    self._event(state, "auto_execute_reconstruct_failed",
                                error=type(exc).__name__)
            if callbacks is None:
                # Process-local runner is unavailable (e.g. unusual host): keep
                # the plan gate so the user can still press execute manually.
                self._event(state, "auto_execute_unavailable",
                            reason="执行回调不可用，等待手动执行")
                return
            # Under the default (safe) policy execute() first parks at
            # awaiting_approval; approve(auto_execute=True) passes the gate and
            # moves the real DAG run onto its own worker thread.
            self.execute(workflow_id, **callbacks)
            state = self._load(workflow_id)
            if state.status != "awaiting_approval":
                return  # user interrupt landed while passing the gate
            self.approve(workflow_id, True, auto_execute=True)
        except Exception as exc:  # noqa: BLE001 - durable failure, never a dead UI
            self._fail_background_workflow(workflow_id, exc, phase="plan")

    def _run_approved_execution(self, workflow_id: str,
                                callbacks: Mapping[str, Any]) -> None:
        """Worker for ``approve(auto_execute=True)``: run the approved DAG."""
        try:
            if self._load(workflow_id).status == "interrupted":
                return
            self.execute(workflow_id, **dict(callbacks))
        except Exception as exc:  # noqa: BLE001 - keep the terminal state durable
            if self._load(workflow_id).status == "interrupted":
                return
            self._fail_background_workflow(workflow_id, exc, phase="execute")

    def _fail_background_workflow(self, workflow_id: str, exc: Exception, *,
                                  phase: str) -> None:
        try:
            state = self._load(workflow_id)
        except Exception:
            return
        if state.status in self._TERMINAL_STATUSES:
            return
        state.status, state.phase = "failed", phase
        state.interrupt_reason = "后台推进中断：" + type(exc).__name__
        self._event(state, "fail", error=type(exc).__name__,
                    reason=_clean_text(str(exc), 200))
        self._save(state)

    def apply_research(self, workflow_id: str, findings: str, *, source: str = "web",
                       option_generator: Callable[[str, ContextPlan], Any] | None = None) -> dict[str, Any]:
        state = self._load(workflow_id)
        if state.status != "awaiting_research":
            raise WorkflowError("当前工作流不在等待联网检索")
        findings = _clean_text(findings, 8000)
        if not findings:
            raise WorkflowError("联网检索结果为空")
        if option_generator is None and state.llm_enabled:
            option_generator = self._option_generators.get(workflow_id)
        state.route_messages.append("【外部检索摘要】" + findings)
        if source not in state.sources:
            state.sources.append(source)
        research_plan = ContextPlan(
            sources=tuple(dict.fromkeys(state.sources)),
            tool_groups=frozenset({"general", "web", "orchestration"}),
            skill_names=(), messages=("【外部检索摘要】" + findings,),
            budget_chars=800, used_chars=min(800, len(findings)),
        )
        options = self._profile_options(
            state.kind, state.request + "\n" + findings, research_plan.sources)
        option_source = "deterministic"
        if state.llm_enabled and option_generator is not None:
            try:
                generated = option_generator(state.request + "\n" + findings, research_plan)
                parsed = self._parse_generated_options(generated, research_plan)
                if parsed:
                    options, option_source = parsed, "llm"
            except Exception as exc:
                state.route_messages.append("LLM 检索后方案生成失败，已回退本地模板：" + type(exc).__name__)
        state.options = [asdict(item) for item in options]
        state.status, state.phase, state.interrupt_reason = "awaiting_choice", "clarify", ""
        self._resume_gate(state, "research", {
            "findings": findings, "source": source, "options": state.options,
        })
        self._event(state, "research", source=source, option_source=option_source,
                    findings_chars=len(findings))
        return self._save(state).public()

    @staticmethod
    def _parse_generated_tasks(raw: Any, *, max_tasks: int = 16) -> list[dict[str, Any]]:
        """Parse and validate a model-produced task DAG.

        The model is never trusted with executable fields.  Only a small,
        auditable task contract is retained; malformed/ambiguous graphs return
        an empty list so callers can use the deterministic fallback.
        """
        from orchestrator import _strict_int_steps

        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
            raw = json.loads(text)
        if isinstance(raw, Mapping):
            raw = raw.get("tasks") or (raw.get("plan") or {}).get("tasks")
        if not isinstance(raw, list) or not raw:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        limit = len(raw) if int(max_tasks) <= 0 else max(1, int(max_tasks))
        for item in raw[:limit]:
            if not isinstance(item, Mapping):
                continue
            task_id = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or ""))[:48]
            role = _clean_text(item.get("role") or "coder", 60)
            description = _clean_text(item.get("task") or item.get("description"), 1200)
            if not task_id or task_id in seen or not description:
                continue
            deps = item.get("depends_on", item.get("dependencies", []))
            if isinstance(deps, str):
                deps = [deps]
            if not isinstance(deps, (list, tuple)):
                deps = []
            clean_deps = [re.sub(r"[^A-Za-z0-9_-]", "", str(dep))[:48]
                          for dep in deps if str(dep).strip()]
            if task_id in clean_deps or len(clean_deps) > _MAX_TASK_DEPS:
                continue
            seen.add(task_id)
            task = {"id": task_id, "role": role, "task": description,
                    "depends_on": list(dict.fromkeys(clean_deps))}
            for key in ("optional", "parallel_safe", "tools", "persona", "mcp", "reflection"):
                if key in item:
                    value = item[key]
                    if key in {"optional", "parallel_safe"}:
                        value = bool(value)
                    elif key == "reflection":
                        value = bool(value)
                    elif key == "mcp":
                        value = str(value or "auto").strip().lower()
                        if value not in {"auto", "allow", "deny"}:
                            value = "auto"
                    elif key == "persona":
                        value = _clean_text(value, 500)
                    elif isinstance(value, (list, tuple)):
                        value = [str(x)[:80] for x in value[:16]]
                    else:
                        value = _clean_text(value, 300)
                    task[key] = value
            # 逐任务加步申请：只接受 1..1000 的非布尔整数；0 留给全局设置表示不限。
            # 布尔/10.7 浮点/非数字串/越界值直接丢弃该字段，回退子代理默认步数。
            steps_value = _strict_int_steps(item.get("max_steps"))
            if steps_value is not None and 1 <= steps_value <= 1000:
                task["max_steps"] = steps_value
            out.append(task)
        if not out:
            return []
        valid_ids = {task["id"] for task in out}
        # Unknown dependencies make the graph ambiguous; reject the entire
        # generated graph instead of silently changing execution semantics.
        if any(dep not in valid_ids for task in out for dep in task["depends_on"]):
            return []
        # A small Kahn pass catches cycles before they reach the executor.
        remaining = {task["id"]: set(task["depends_on"]) for task in out}
        done: set[str] = set()
        while remaining:
            ready = [tid for tid, deps in remaining.items() if deps <= done]
            if not ready:
                return []
            done.update(ready)
            for tid in ready:
                remaining.pop(tid, None)
        # 角色能力错配：任务要求写文件，而它的角色工具列表里没有任何写类工具
        # （`researcher` / `planner` / `dispatcher` / `reviewer` 都是只读的，
        #  hint 里明写「不要改任何文件」）。这类任务在**结构上无法完成**——
        # 实测 `wf-9f121dec38a6` 整轮三个任务全是只读角色，而目标就是写一个文件。
        #
        # 与上面「未知依赖」同一条处置：**整图拒绝**，不静默删掉那几条。
        # 静默删任务正是本会话修掉的那个病（多步 DAG 被悄悄截成只剩根任务，
        # 而调用方照样落库、照样发事件、照样弹审批，零报错）。
        # 整图被拒后调用方回退到确定性画像计划——实测那条路径的角色分配是对的
        # （researcher 只读且无写动词 / coder 能写 / tester 能写），不会立刻再错一次。
        if role_mismatch_tasks(out):
            return []
        return out

    @staticmethod
    def _subagent_records(tasks: Iterable[Mapping[str, Any]], child_max_steps: int | None = None) -> list[dict[str, Any]]:
        """Expose bounded persona/capability policy alongside each task."""
        if child_max_steps is None:
            child_max_steps = _default_child_max_steps()
        rows = []
        for task in tasks or ():
            tools = task.get("tools") or []
            if isinstance(tools, str):
                tools = [tools]
            try:
                task_steps = int(task.get("max_steps") or child_max_steps)
            except (TypeError, ValueError):
                task_steps = child_max_steps
            if child_max_steps > 0:
                task_steps = min(child_max_steps, task_steps if task_steps > 0 else child_max_steps)
            rows.append({
                "id": task.get("id"),
                "role": task.get("role", "coder"),
                "persona": _clean_text(task.get("persona") or "", 500),
                "tools": [str(item)[:80] for item in list(tools)[:16]],
                "mcp": str(task.get("mcp") or "auto"),
                "reflection": bool(task.get("reflection", True)),
                "max_steps": task_steps,
                "status": "pending",
            })
        return rows

    def _note_plan_truncation(self, state: WorkflowState,
                              graph_view: Mapping[str, Any]) -> None:
        """把「模型给了 N 步、只落库 k 步」记成事件，不让截断再隐身。

        解析器会静默丢弃不合规的任务（重 id、空描述、依赖未知）。全部丢弃时它
        返回 []、调用方回退到确定性计划，那看得见；**部分丢弃看不见**——计划照样
        落库、照样发 ``plan`` 事件、照样弹审批框，只是少了一部分步骤。

        这不是理论风险：本文件里那个 ``len(clean_deps) > max_tasks`` 的守卫就是这么
        藏下来的——它让默认配置（``max_agents`` 未配 -> 0）下的 5 步 DAG 只剩 1 步，
        全程零报错。事件的意义就是让下一次同类问题在 trace 里直接可见。
        """
        try:
            offered = int(graph_view.get("plan_offered") or 0)
            kept = len(state.tasks)
        except Exception:
            return
        if offered > kept:
            self._event(state, "plan_generation_truncated",
                        offered=offered, kept=kept, dropped=offered - kept)

    def _required_task_ids_for_graph(self, state: Any) -> set[str]:
        """从真WorkflowState 取「用户批准的必需任务」。图状态里没有验收条件。

        `_graph_payload` 带的只有 workflow_id / tasks / results / 计数，
        **不含 acceptance_contract**，所以必须按 `hooked_node` 的做法从
        `self._states` 取回。取不到就返回空集——**保守方向**：保护不了就
        等于不保护（而 `_note_replan_narrowing` 那个诊断事件会照样把丢弃
        记进 trace，用户仍然看得见）。
        """
        try:
            workflow_id = str((state or {}).get("workflow_id") or "") \
                if isinstance(state, Mapping) else ""
            runtime_state = self._states.get(workflow_id) if workflow_id else None
            if runtime_state is None:
                return set()
            return required_task_ids(runtime_state.acceptance_contract)
        except Exception:  # noqa: BLE001 - 取不到就退化成「不保护」，不阻断重规划
            return set()

    def _note_replan_narrowing(self, state: Any, previous_ids: list[str],
                               new_ids: list[str]) -> list[str]:
        """重规划把**上一版计划里的任务**弄丢了就发事件。-> 消失的任务 id。

        ## 为什么必须有

        `replan_history` 里那个 `dropped` 比的是 **`raw_tasks` vs `parsed`**，也就是
        「LLM 提案内部被解析器丢掉多少」。它**没有**和上一版计划比过。所以当 LLM 自己
        只提了 1 个任务时：

            offered=1  parsed=1  dropped=0        而计划是 3 -> 1

        那段注释写着「重排同样会静默丢任务…差额记进持久化历史，否则『重排后只剩
        两步』没人看得见」——而它偏偏在这个场景失效：**提案本身变小了，差额是 0。**

        实测（`wf-bdb1a54286bf` / `wf-0bbd6c9edf6e`）：

            tasks   = [research_retry]        （原 research/implement/verify）
            results = {research: failed, implement: blocked, verify: blocked,
                       research_retry: ok}
            status  = completed_unverified

        ## 与 `_note_plan_truncation` 是同一件事

        那条注释写着「整图被拒时看得见；**部分丢弃看不见**——计划照样落库、照样发
        plan 事件、照样弹审批框，只是少了一部分步骤…这不是理论风险」。这次是**换了个
        站点**的同一个病。

        ## `state` 是**图状态 dict**，不是 WorkflowState（第一版的坑）

        `replan_node` 拿到的 `state` 是 LangGraph 的图状态（dict），而 `_event` 需要
        `WorkflowState`——它第一行就 `state.events.append(event)`。第一版把图状态直接
        传进来，于是抛 `AttributeError`，而那句 `except Exception: pass` 把它**静默
        吞了**：函数返回了正确的消失列表，事件却一个都没落进 trace。而当时的测试把
        `_event` 换成了记录器，只证明了「调用发生过」。

        所以这里按 `hooked_node` 的既有做法，从 `self._states` 取回真正的
        `WorkflowState`。取不到就**不吞**——把失败暴露出来，不能让一个诊断事件
        静默失效（那正是它本该防的那类问题）。

        ## 这里只报，不拦

        「重规划被允许丢掉用户批准的任务吗」是**权限模型**问题（要不要弹审批门），
        不该在这里偷着改。本函数只负责让它**看得见**。
        """
        kept = {str(t) for t in new_ids}
        vanished = [t for t in previous_ids if str(t) not in kept]
        if not vanished:
            return []
        workflow_id = str((state or {}).get("workflow_id") or "") \
            if isinstance(state, Mapping) else getattr(state, "workflow_id", "")
        runtime_state = self._states.get(workflow_id) if workflow_id else None
        if runtime_state is None:
            raise RuntimeError(
                "重规划诊断事件无法落账：拿不到 workflow_id=%r 的 WorkflowState"
                "（图状态里只有 id）。**不要**用 try/except 吞掉它——"
                "本事件就是为了让「计划被静默改小」看得见，"
                "它自己静默失效就等于什么都没有。" % workflow_id)
        self._event(runtime_state, "graph_replan_narrowed",
                    dropped_tasks=vanished[:12],
                    dropped_count=len(vanished),
                    previous_count=len(previous_ids),
                    kept_count=len(new_ids),
                    reason="重规划后的任务表里没有这些任务；它们不会自动重跑，"
                           "而已批准的验收条件仍指向它们")
        return vanished

    def plan(self, workflow_id: str, tasks: Iterable[Mapping[str, Any]] | None = None,
             task_generator: Callable[[str, Mapping[str, Any], ContextPlan], Any] | None = None) -> dict[str, Any]:
        state = self._load(workflow_id)
        if state.status == "planned" and tasks is None:
            # The LangGraph plan node may already have generated and
            # checkpointed the task DAG while resuming the user's choice.
            return state.public()
        if state.status not in {"planning", "planned", "awaiting_approval", "interrupted"}:
            raise WorkflowError("当前工作流不能规划：%s" % state.status)
        policy = WorkflowPolicy.from_state(state.policy)
        if task_generator is not None:
            self._task_generators[workflow_id] = task_generator
        task_source = "provided"
        if tasks is None and task_generator is not None:
            try:
                route_plan = ContextPlan(
                    sources=tuple(state.sources), tool_groups=frozenset({"general", "orchestration", "web"} if "web" in state.sources else {"general", "orchestration"}),
                    skill_names=(), messages=tuple(state.route_messages[-8:]) +
                    (("【项目上下文】project_id=%s root=%s" % (state.project_id, state.project_root)),),
                    budget_chars=policy.max_context_chars,
                    used_chars=sum(len(x) for x in state.route_messages[-8:]) +
                    len(state.project_id) + len(state.project_root) + 26,
                )
                generated = task_generator(state.request, state.selected_option or {}, route_plan)
                parsed = self._parse_generated_tasks(generated, max_tasks=policy.max_subagents)
                if not parsed:
                    # 整图被拒时**指名**原因。只说「计划无效」等于把问题原样退回
                    # 给用户——他看到的是一份凭空消失的计划，不是有问题的计划。
                    mismatch = role_mismatch_tasks(
                        [item for item in _as_task_list(generated) if isinstance(item, Mapping)])
                    if mismatch:
                        self._event(state, "plan_rejected_role_mismatch",
                                    rejected=len(mismatch),
                                    tasks=",".join("%s(%s)" % (m["id"], m["role"])
                                                   for m in mismatch[:6]),
                                    reason="这些任务要求写文件，但它们的角色是只读的"
                                           "（工具列表里没有 create_file / apply_edit /"
                                           " python_exec），在结构上无法完成")
                if parsed:
                    tasks, task_source = parsed, "llm"
                    # 解析器会**静默丢弃**不合规的任务（重 id、空描述、依赖未知等）。
                    # 全部丢弃时它返回 []、调用方回退到确定性计划，看得见；
                    # **部分丢弃看不见**——计划照样落库、照样发事件、照样弹审批，
                    # 只是少了一部分步骤。上面那个 max_tasks/依赖个数守卫就是这么
                    # 藏了不知道多久的：5 步计划只剩 1 步，全程零报错。
                    # 所以这里把差额显式记成事件，不让截断再隐身。
                    offered = _raw_task_count(generated)
                    if offered and len(parsed) < offered:
                        self._event(state, "plan_generation_truncated",
                                    offered=offered, kept=len(parsed),
                                    dropped=offered - len(parsed))
            except Exception as exc:
                self._event(state, "plan_generation_failed", error=type(exc).__name__)
        if tasks is None:
            task_source = "deterministic"
            tasks = get_profile(state.kind).fallback_tasks()
        previous_plan = [(item.get("id"), item.get("task")) for item in state.tasks]
        task_items = list(tasks)
        state.tasks = ([dict(t) for t in task_items] if policy.max_subagents <= 0 else
                       [dict(t) for t in task_items[:policy.max_subagents]])
        if policy.max_subagents > 0 and len(task_items) > policy.max_subagents:
            self._event(state, "plan_generation_truncated",
                        offered=len(task_items), kept=len(state.tasks),
                        dropped=len(task_items) - len(state.tasks))
        next_plan = [(item.get("id"), item.get("task")) for item in state.tasks]
        if previous_plan != next_plan or not state.acceptance_contract.get("items"):
            state.acceptance_contract = acceptance_on_plan_changed(
                state.acceptance_contract, state.tasks,
                runtime_required=(state.kind == "game"))
            if state.acceptance_contract["revision"] > 1:
                state.policy["approval_mode"] = "safe"
        # 步数预算随任务规模放大（几个 Agent 并行调工具，总调用次数成倍增加）：
        # 调用方显式给过 max_steps（含低于自动下限的低值）时尊重显式值；
        # 否则按 n*子代理默认+4 自动推算并夹在配置区间内。
        _plan_lo, plan_hi = workflow_step_limits()
        requested_max_steps = max(0, int((state.policy or {}).get("max_steps", _plan_lo) or 0))
        base_max_steps = (0 if requested_max_steps == 0 else
                          (min(plan_hi, requested_max_steps) if plan_hi > 0 else requested_max_steps))
        if (state.policy or {}).get("step_budget_explicit"):
            effective_steps, budget_source = base_max_steps, "explicit"
        else:
            effective_steps = effective_workflow_max_steps(len(state.tasks))
            budget_source = "auto"
            state.policy["max_steps"] = effective_steps
        state.subagents = self._subagent_records(state.tasks, policy.child_max_steps)
        state.context_layers["task"] = {"count": len(state.tasks),
                                        "ids": [t.get("id") for t in state.tasks]}
        state.context_layers["step_budget"] = {
            "effective_max_steps": effective_steps, "source": budget_source,
            "task_count": len(state.tasks)}
        state.status, state.phase = "planned", "execute"
        _sandbox_notice = self._sandbox_approval_notice(state)
        if _sandbox_notice:
            state.interrupt_reason = "计划等待审核；" + _sandbox_notice
        # The plan gate is a real LangGraph interrupt.  Resume it after the
        # manager has validated the task DAG so the durable graph owns the
        # transition into approval instead of merely mirroring a native flag.
        if StateGraph is not None and graph_interrupt is not None:
            self._resume_gate(state, "plan", {"tasks": state.tasks, "source": task_source})
        self._event(state, "plan", task_count=len(state.tasks), max_subagents=policy.max_subagents,
                    max_steps=effective_steps, step_budget_source=budget_source,
                    task_source=task_source)
        return self._save(state).public()

    @staticmethod
    def _call_graph_replanner(replanner: Callable[..., Any], failed: list[dict[str, Any]],
                              results: dict[str, Any], attempt: int,
                              emit: Callable[[str, Mapping[str, Any]], Any]) -> Any:
        emit("graph_replan_start", {"attempt": attempt,
                                     "failed": [str(item.get("id")) for item in failed]})
        proposal = replanner(failed, results, attempt)
        shaped = normalize_replan_proposal(proposal)
        # `accepted` 第一版是 `count > 0`，而 count 数的是 add/tasks/replace/revise
        # **四个键的总和**——实测 `task_count=3` 而实际只会采纳一个键里的 1 条，
        # `accepted` 却为 true。于是 trace 上出现「accepted: true」紧接着
        # 「rejected: empty_or_invalid_proposal」，读的人会懵。
        #
        # 现在 `accepted` 只表示「提案非空」，真正的采纳结果由 replan_node 写进
        # `replan_history.adopted`（added / dropped / replaced / ignored）。
        emit("graph_replan_proposal", {
            "attempt": attempt,
            "accepted": bool(shaped),
            "offered": proposal_offer_summary(shaped),
            "keys": sorted(shaped),
        })
        return proposal

    def _run_task_dag_langgraph(
        self,
        tasks: list[dict[str, Any]],
        runner: Callable[[dict[str, Any], dict[str, str]], Mapping[str, Any]],
        *,
        prior_results: Mapping[str, Any] | None = None,
        max_steps: int = 0,
        max_parallel: int = 4,
        max_subagents: int = 16,
        max_context_chars: int = 6000,
        on_event: Callable[[str, Mapping[str, Any]], Any] | None = None,
        thread_id: str = "task-wave",
        trace_parent_id: str = "",
    ) -> dict[str, Any]:
        """Execute one task DAG with LangGraph ``Send`` fan-out per wave."""
        from orchestrator import (dedup_tasks, extract_dispatch_tasks, parse_plan,
                                  topological_waves)

        started = time.monotonic()
        original = {str(item.get("id")): dict(item) for item in tasks}
        normalized, deduped_ids, dedup_remap = dedup_tasks(
            tasks, max_tasks=max_subagents if max_subagents > 0 else 0)
        for item in normalized:
            item["context_from"] = list((original.get(item["id"]) or {}).get("context_from") or [])
        waves = topological_waves(normalized)
        by_id = {item["id"]: item for item in normalized}
        external = dict(prior_results or {})
        results: dict[str, Any] = {
            task_id: {"status": "deduped", "conclusion": "", "steps": 0,
                      "error": "", "elapsed_ms": 0, "kept": dedup_remap.get(task_id),
                      "optional": True}
            for task_id in deduped_ids
        }
        blocked: dict[str, str] = {}
        executed_waves: list[list[str]] = []
        task_threads: list[str] = []
        steps_used = 0
        dispatches: list[dict[str, Any]] = []

        def emit(kind: str, payload: Mapping[str, Any]) -> None:
            if on_event is not None:
                try:
                    on_event(kind, payload)
                except Exception:
                    pass

        def dispatch_node(_state: TaskFanoutState) -> dict[str, Any]:
            return {}

        def dispatch_tasks(fanout_state: TaskFanoutState):
            parent_thread = str(fanout_state.get("task_thread") or "task-wave")
            return [Send("subagent", {"task": task,
                                      "prior_results": fanout_state.get("prior_results") or {},
                                      "trace_parent_id": fanout_state.get("trace_parent_id") or "",
                                      "task_thread": "%s:%s" % (parent_thread, str(task.get("id") or "task"))})
                    for task in fanout_state.get("tasks") or []]

        def subagent_node(fanout_state: TaskFanoutState) -> dict[str, Any]:
            task = dict(fanout_state.get("task") or {})
            known = dict(fanout_state.get("prior_results") or {})
            context = dict(task.pop("_prepared_context", {}) or {})
            if not context:
                dependency_ids = list(task.get("depends_on") or []) + list(task.get("context_from") or [])
                for dep in dependency_ids:
                    summary, _summary_meta = summarize_subagent_result(
                        known.get(str(dep)) or {}, max_chars=1200)
                    if summary:
                        context[str(dep)] = summary
            began = time.monotonic()
            task_id = str(task.get("id") or "task")
            emit("subagent_start", {
                "task_id": task_id,
                "role": str(task.get("role") or "coder")[:60],
                "task": _clean_text(task.get("task") or task.get("description") or "", 1200),
                "persona": _clean_text(task.get("persona") or "", 240),
                "tools": list(task.get("tools") or [])[:16],
                "mcp": str(task.get("mcp") or "auto"),
                "max_steps": task.get("max_steps"),
                "reflection_required": bool(task.get("reflection", True)),
                "context_keys": list(context)[:16],
                "context_chars": sum(len(str(value)) for value in context.values()),
                "task_thread": str(fanout_state.get("task_thread") or ""),
            })
            try:
                from .langsmith import child_span
                with child_span(
                        str(fanout_state.get("trace_parent_id") or ""),
                        "subagent.%s" % str(task.get("id") or "task"),
                        run_type="chain",
                        inputs={"task_id": str(task.get("id") or "")[:80],
                        "role": str(task.get("role") or "")[:40]}):
                    output = dict(runner(task, context) or {})
            except Exception as exc:
                output = {"status": "failed", "conclusion": "",
                          "error": "%s: %s" % (type(exc).__name__, exc)}
            output.setdefault("status", "ok")
            output.setdefault("conclusion", "")
            output.setdefault("steps", 0)
            if task.get("_context_compression") is not None:
                output["context_compression"] = dict(task.get("_context_compression") or {})
            output["elapsed_ms"] = int((time.monotonic() - began) * 1000)
            # trace 为 _child_trace 产出的有界结构（≤6 步 action+obs 摘要、
            # 少量 thoughts/reflections、用量），供 UI 在子代理完成后补全全过程。
            complete_trace = output.get("trace")
            if not isinstance(complete_trace, Mapping):
                complete_trace = {}
            emit("subagent_complete", {
                "task_id": task_id,
                "role": str(task.get("role") or "coder")[:60],
                "task": str(output.get("task") or task.get("task") or "")[:1200],
                "status": str(output.get("status") or "unknown"),
                # `degraded` 是 harness 自己打的标（`agent.py` 三处：安全网兜底的 final、
                # 连续空响应、清洗残句），标的是「这段结论**不是模型自己的答案**」。
                # 之前它只走到子代理结果（`agent.py` 的 `{"degraded": ...}`）就断了 ——
                # 这里的事件没带，于是时间线上那条任务是「ok + 有一句结论」，
                # 而那句话其实是 harness 写的诊断（例如「模型连续 1 次调用都没有返回
                # 可用正文…」）。信号在到达判据前被丢掉，和本仓库反复出现的
                # 「把没能答记成答了」是同一族。
                "degraded": bool(output.get("degraded")),
                "steps": int(output.get("steps") or 0),
                "max_steps": int(output.get("max_steps") or 0),
                "elapsed_ms": int(output.get("elapsed_ms") or 0),
                "conclusion": _clean_text(output.get("conclusion") or "", 1600),
                "error": _clean_text(output.get("error") or "", 400),
                "reflection": dict(output.get("reflection") or {}),
                "context_compression": dict(output.get("context_compression") or {}),
                "trace": dict(complete_trace),
                "task_thread": str(fanout_state.get("task_thread") or ""),
            })
            return {"task_results": {str(task.get("id")): output}}

        fanout = StateGraph(TaskFanoutState)
        fanout.add_node("dispatch", dispatch_node)
        fanout.add_node("subagent", subagent_node)
        fanout.add_edge(START, "dispatch")
        fanout.add_conditional_edges("dispatch", dispatch_tasks)
        fanout.add_edge("subagent", END)
        compiled = fanout.compile(checkpointer=self._graph_checkpointer)

        if deduped_ids:
            emit("dedup", {"dropped": deduped_ids, "remap": dedup_remap,
                           "backend": "langgraph_send"})

        wave_index = 0
        while True:
            # Recompute waves after a dispatcher returns. This is what lets a
            # read-only decomposition agent add exactly the execution tasks it
            # judged necessary, instead of forcing a fixed team size.
            waves = topological_waves(normalized)
            pending = next((ids for ids in waves
                            if any(task_id not in results
                                   and task_id not in external for task_id in ids)), None)
            if pending is None:
                break
            wave_ids = pending
            runnable: list[dict[str, Any]] = []
            for task_id in wave_ids:
                # `external`（prior_results）里的任务**已经跑过了**，不能再派发一次。
                #
                # 以前这里只判 `task_id in results`，于是任何带 prior_results 的续跑
                # 都会把已执行的任务重跑一遍——`external` 只在下面查依赖状态时用过，
                # 从没参与「是否已完成」的判定。副作用是实打实的：重跑会再写一遍文件、
                # 再花一遍 token，而调用序列里出现重复项。
                #
                # 这条以前没被抓出来，是因为失败任务会被重规划顺带从计划里删掉；
                # 改成走 `apply_proposal` 完整契约后失败任务必须留在计划里（不能丢），
                # 这个重复派发就露出来了。
                if task_id in results or task_id in external:
                    continue
                task = by_id[task_id]
                reason = ""
                for dependency in task.get("depends_on") or []:
                    dependency_result = results.get(dependency) or external.get(dependency)
                    dependency_optional = bool((by_id.get(dependency) or {}).get("optional") or
                                               (dependency_result or {}).get("optional"))
                    if dependency_result is not None and dependency_result.get("status") != "ok" \
                            and not dependency_optional:
                        reason = "上游 %s %s" % (dependency, dependency_result.get("status"))
                        break
                if reason:
                    blocked[task_id] = reason
                    results[task_id] = {"status": "blocked", "conclusion": "", "steps": 0,
                                        "error": reason, "elapsed_ms": 0}
                    emit("task_blocked", {"task_id": task_id, "role": (by_id.get(task_id) or {}).get("role"),
                                           "reason": reason, "status": "blocked"})
                else:
                    runnable.append(task)
            if not runnable:
                continue
            if max_steps and steps_used >= max_steps:
                for task in runnable:
                    task_id = task["id"]
                    blocked[task_id] = "达到工作流步数上限"
                    results[task_id] = {"status": "blocked", "conclusion": "", "steps": 0,
                                        "error": blocked[task_id], "elapsed_ms": 0}
                    emit("task_blocked", {"task_id": task_id, "role": task.get("role"),
                                           "reason": blocked[task_id], "status": "blocked"})
                emit("step_limit", {"limit": max_steps, "used": steps_used,
                                     "blocked": [task["id"] for task in runnable]})
                break
            known_results = {**external, **results}
            compression_futures: dict[str, Any] = {}
            for task in runnable:
                dependency_ids = list(task.get("depends_on") or []) + list(task.get("context_from") or [])
                dependency_context: dict[str, str] = {}
                for dependency in dependency_ids:
                    summary, _summary_meta = summarize_subagent_result(
                        known_results.get(str(dependency)) or {}, max_chars=1200)
                    if summary:
                        dependency_context[str(dependency)] = summary
                compression_futures[str(task.get("id"))] = compress_context_async(
                    dependency_context, max(240, int(max_context_chars or 6000) // 3))
            prepared_runnable: list[dict[str, Any]] = []
            compression_meta: dict[str, Any] = {}
            for task in runnable:
                task_copy = dict(task)
                bounded, metadata = compression_futures[str(task.get("id"))].result()
                task_copy["_prepared_context"] = bounded
                task_copy["_context_compression"] = metadata
                compression_meta[str(task.get("id"))] = metadata
                prepared_runnable.append(task_copy)
            emit("context_prepared", {"wave": wave_index, "task_count": len(prepared_runnable),
                                       "compression": compression_meta})
            emit("wave", {"wave": wave_index, "tasks": [task["id"] for task in prepared_runnable],
                          "backend": "langgraph_send"})
            wave_thread = "%s:%s" % (thread_id, wave_index)
            task_threads.append(wave_thread)
            emit("wave_start", {"wave": wave_index,
                                 "tasks": [task["id"] for task in prepared_runnable],
                                 "task_thread": wave_thread,
                                 "max_parallel": max(1, int(max_parallel)),
                                 "context_compression": compression_meta})
            snapshot = compiled.invoke(
                {"tasks": prepared_runnable, "prior_results": known_results, "task_results": {},
                 "trace_parent_id": trace_parent_id, "task_thread": wave_thread},
                config={"configurable": {"thread_id": wave_thread},
                        "max_concurrency": max(1, int(max_parallel)), "recursion_limit": 16},
            )
            batch = dict(snapshot.get("task_results") or {})
            results.update(batch)
            executed_waves.append([task["id"] for task in runnable])
            emit("wave_complete", {"wave": wave_index,
                                    "tasks": [task["id"] for task in prepared_runnable],
                                    "task_thread": wave_thread,
                                    # 判据走 `counts_as_ok`：`degraded` 的结论是 harness
                                    # 写的诊断，不是模型的答案，不能算这一波成功。
                                    "status": "ok" if all(
                                        counts_as_ok(item)
                                        for item in batch.values()) else "failed"})
            for task_id, output in batch.items():
                steps_used += int((output or {}).get("steps") or 0)
                task_meta = by_id.get(task_id) or {}
                emit("task_complete", {"task_id": task_id, "role": task_meta.get("role"),
                               "persona": _clean_text(task_meta.get("persona") or "", 240),
                               "tools": list(task_meta.get("tools") or [])[:16],
                               "mcp": str(task_meta.get("mcp") or "auto"),
                               "reflection": dict((output or {}).get("reflection") or {}),
                               "status": (output or {}).get("status"),
                               # 同上：这条也是事件流里的一条，不带就等于信号在这里又断一次。
                               "degraded": bool((output or {}).get("degraded")),
                               "steps": (output or {}).get("steps"),
                               "elapsed_ms": (output or {}).get("elapsed_ms"),
                               "task_thread": wave_thread,
                               "backend": "langgraph_send"})

            # Materialize a dispatcher/planner's structured assignment only
            # after its wave completed. The main Agent validates the proposal
            # and owns the actual insertion into the DAG.
            for task in runnable:
                if task.get("role") not in {"dispatcher", "planner"}:
                    continue
                output = results.get(task["id"]) or {}
                if output.get("status") != "ok":
                    continue
                proposed = extract_dispatch_tasks(output.get("conclusion"),
                                                  planner_id=task["id"])
                if not proposed:
                    continue
                dispatched = len(set(external) | {str(item.get("id")) for item in normalized})
                room = max(0, max_subagents - dispatched) if max_subagents > 0 else None
                try:
                    parsed = ([] if room == 0 else parse_plan(
                        proposed, known_ids=set(by_id), max_tasks=room or 0))
                except Exception:
                    parsed = []
                added = []
                for item in parsed:
                    if item["id"] in by_id or item["id"] in results:
                        continue
                    by_id[item["id"]] = item
                    normalized.append(item)
                    added.append(item["id"])
                if added:
                    record = {"kind": "dispatch", "planner": task["id"],
                              "added": added}
                    dispatches.append(record)
                    emit("dispatch", record)
            wave_index += 1

        ok = all(counts_as_ok(item) or bool(item.get("optional")) or
                 bool((by_id.get(task_id) or {}).get("optional"))
                 for task_id, item in results.items())
        return {
            "ok": ok, "waves": executed_waves, "order": list(results), "results": results,
            "tasks": [dict(item) for item in normalized],
            "blocked": sorted(blocked), "merged": "", "replans": 0,
            "steps_used": steps_used, "revisions": list(dispatches),
            "dispatches": dispatches,
            "deduped": {"dropped": deduped_ids, "remap": dedup_remap},
            "dropped": [], "n_tasks": len(normalized),
            "n_ok": sum(1 for item in results.values() if counts_as_ok(item)),
            "n_failed": sum(1 for item in results.values() if item.get("status") == "failed"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "backend": "langgraph_send", "task_threads": task_threads,
            "context_compression": {
                str(task_id): (results.get(task_id) or {}).get("context_compression")
                for task_id in results if (results.get(task_id) or {}).get("context_compression")
            },
        }

    def execute(self, workflow_id: str, runner: Callable[[dict[str, Any], dict[str, str]], Mapping[str, Any]],
                *, synth_runner: Callable[[list[dict[str, Any]], dict[str, Any]], str] | None = None,
                replanner: Callable[..., Any] | None = None, on_event: Callable[..., Any] | None = None,
                session_id: str = "") -> dict[str, Any]:
        with self._workflow_lease(workflow_id):
            return self._execute_unleased(
                workflow_id, runner, synth_runner=synth_runner,
                replanner=replanner, on_event=on_event, session_id=session_id)

    def _mark_remediated(self, state: WorkflowState,
                         result_map: dict[str, Any],
                         remediated: Mapping[str, str]) -> None:
        """把「失败被补救成功」写进结果行与事件流。

        ## 失败状态**不动**

        被补救的任务在 `results` 与 `subagents` 里仍然是 `failed`，界面照旧标红——
        用户要看得见那次失败。本方法只**补一句**说明后来由谁补上了。

        ## 为什么抽成一处

        这段原先在主 execute 路径和「单任务重试」路径各写了一份。两份的文案已经
        漂移过一次（重试路径那句少了「界面照旧标红」），而只守其中一份的变异
        电池会在另一份上**报活下来**——那不是守卫没牙，是压根有一份没人守。
        判据只有一份（`remediated_task_ids`），留痕也只该有一份。
        """
        for dead_id, helper in remediated.items():
            row = result_map.get(dead_id)
            if isinstance(row, dict):
                row.setdefault("remediated_by", helper)
                row.setdefault("remediation_note",
                               "该任务失败后由「%s」补救成功；失败记录保留。" % helper)
            self._event(state, "subagent_remediated", task_id=dead_id, by=helper,
                        note="失败历史保留（界面照旧标红），最终判定按补救成功计")

    def retry_subagent(self, workflow_id: str, task_id: str,
                       runner: Callable[[dict[str, Any], dict[str, str]], Mapping[str, Any]],
                       *, synth_runner: Callable[[list[dict[str, Any]], dict[str, Any]], str] | None = None,
                       replanner: Callable[..., Any] | None = None,
                       on_event: Callable[..., Any] | None = None,
                       session_id: str = "") -> dict[str, Any]:
        """Retry exactly one failed/blocked Subagent.

        ``replanner`` 之前**不在签名里**，而 ``workflow_callbacks`` 一直返回它，
        端点把三个回调一起 splat 进来 —— 于是 UI 上的「重试该成员」按钮
        每次都 500（``TypeError: got an unexpected keyword argument 'replanner'``），
        前端只能显示「响应不是合法JSON（HTTP 500）」。
        签名与调用方脱节，却没有任何测试覆盖这个端点。

        Successful task results are kept as durable context.  The selected
        task is executed in a one-task LangGraph wave, and the retry counter is
        persisted per task so a UI retry button cannot create an unbounded
        loop.  Downstream blocked tasks are intentionally not auto-rerun: the
        user or the main Agent can retry them separately after inspecting the
        repaired upstream result.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            raise WorkflowError("缺少要重试的 task_id")
        with self._workflow_lease(workflow_id):
            state = self._load(workflow_id)
            task = next((dict(item) for item in state.tasks
                         if str(item.get("id") or "") == task_id), None)
            if task is None:
                raise WorkflowError("不存在的 Subagent 任务：%s" % task_id)
            result_map = dict((state.results or {}).get("results") or {})
            previous = dict(result_map.get(task_id) or {})
            previous_status = str(previous.get("status") or "pending")
            if previous_status not in {"failed", "blocked"}:
                raise WorkflowError("只有失败或阻塞的 Subagent 才能单独重试：%s" % task_id)
            policy = WorkflowPolicy.from_state(state.policy)
            retries = dict(state.subagent_retries or {})
            attempt = int(retries.get(task_id, 0)) + 1
            if attempt > policy.max_subagent_retries:
                raise WorkflowError("Subagent %s 已达到最大重试次数 %s" %
                                    (task_id, policy.max_subagent_retries))

            state.status, state.phase = "executing", "execute"
            state.interrupt_reason = ""
            state.execution_session_id = _clean_text(session_id or state.execution_session_id, 160)
            self._ensure_capability_lease(state)
            self._event(state, "subagent_retry_start", task_id=task_id,
                        attempt=attempt, previous_status=previous_status,
                        max_retries=policy.max_subagent_retries)
            self._save(state)

            def emit(kind: str, payload: Mapping[str, Any]) -> None:
                self._event(state, kind, **dict(payload or {}))
                if on_event:
                    try:
                        on_event(kind, payload)
                    except Exception:
                        pass

            def scoped_runner(retry_task: dict[str, Any], context: dict[str, str]):
                from .tools import project_stage_scope, tool_idempotency_scope
                from config import reset_context_code_root, set_context_code_root
                with tool_idempotency_scope(
                        "%s:%s:retry%s" % (workflow_id, task_id, attempt),
                        self.state_root):
                    if not state.project_stage:
                        return runner(retry_task, context)
                    token = set_context_code_root(state.project_stage["workspace_root"])
                    try:
                        # 文件/进程操作走试做区副本；审批台账与自检沙箱租约
                        # 按真实项目记账，所以把 origin 一起带进作用域。
                        with project_stage_scope(origin_root=state.project_root):
                            return runner(retry_task, context)
                    finally:
                        reset_context_code_root(token)

            if StateGraph is not None:
                report = self._run_task_dag_langgraph(
                    [task], scoped_runner, prior_results=result_map,
                    max_steps=(0 if policy.max_steps <= 0 else max(0, policy.max_steps - state.steps)),
                    max_parallel=1, max_subagents=policy.max_subagents,
                    max_context_chars=policy.max_context_chars,
                    # 重试的那一项再失败时，也允许规划器提恢复任务——和自动路径一致。
                    # 此前这里不传 replan，于是重试只有一次机会，再失败就直接判死。
                    replan=(lambda failed, results, attempt: self._call_graph_replanner(
                        replanner, failed, results, attempt, emit)) if replanner is not None else None,
                    on_event=emit, thread_id="%s:retry%s" % (workflow_id, attempt),
                    trace_parent_id=str((state.langsmith_trace or {}).get("root_run_id") or ""))
            else:
                # Native fallback: dependencies are already durable results;
                # inject their bounded summaries directly into this one task.
                from orchestrator import run_plan
                task_copy = dict(task)
                task_copy["depends_on"] = []
                context = {}
                for dep in task.get("depends_on") or []:
                    summary, _meta = summarize_subagent_result(result_map.get(str(dep)) or {}, 1200)
                    if summary:
                        context[str(dep)] = summary
                report = run_plan([task_copy],
                                  lambda _task, _ctx: scoped_runner(task, context),
                                  max_parallel=1,
                                  max_steps=(0 if policy.max_steps <= 0 else max(0, policy.max_steps - state.steps)),
                                  on_event=emit)

            retried = dict((report.get("results") or {}).get(task_id) or {})
            if not retried:
                # 报实际拿到的键，而不是一句「未返回结果」：若规划器把这一波换成了
                # 别的任务，键名就是唯一能看出「它其实跑了别的」的线索。
                got = sorted((report.get("results") or {}).keys())
                retried = {"status": "failed", "conclusion": "",
                           "error": "单任务重试未返回 %s 的结果（本次实际返回：%s）"
                                    % (task_id, got or "无"),
                           "steps": 0}
            result_map[task_id] = retried
            retries[task_id] = attempt
            state.subagent_retries = retries
            merged_report = dict(state.results or {})
            merged_report.update({"results": result_map, "retry_task": task_id,
                                  "retry_attempt": attempt,
                                  "retry_history": list(merged_report.get("retry_history") or []) + [{
                                      "task_id": task_id, "attempt": attempt,
                                      "previous_status": previous_status,
                                      "status": retried.get("status"),
                                      "elapsed_ms": retried.get("elapsed_ms", 0),
                                  }]})
            state.results = merged_report
            state.steps += int(report.get("steps_used") or 0)
            state.subagents = self._subagent_records(
                state.tasks, policy.child_max_steps)
            for row in state.subagents:
                item = result_map.get(str(row.get("id"))) or {}
                row["status"] = item.get("status", row.get("status"))
                row["steps"] = int(item.get("steps") or 0)
                row["elapsed_ms"] = int(item.get("elapsed_ms") or 0)
                row["retry_count"] = int(retries.get(str(row.get("id")), 0))
                if item.get("error"):
                    row["error"] = _clean_text(item.get("error"), 300)
                if item.get("reflection"):
                    row["reflection_result"] = dict(item.get("reflection") or {})
            merged = str(merged_report.get("merged") or "") or "\n".join(
                str((item or {}).get("conclusion") or "") for item in result_map.values())
            # 「失败被补救成功」要在 review_output **之前**算好：`status_ok`
            # 这一项就取自这里的判定（漏掉的话 verdict 再怎么放行也还是 failed，
            # 实测踩过）。判据只有一份，见 `remediated_task_ids`。
            #
            # 失败记录本身**不动**：row["status"] 仍是 failed、界面照旧标红。
            remediated = remediated_task_ids(
                (state.graph_state or {}).get("replan_history"),
                {"results": result_map})
            self._mark_remediated(state, result_map, remediated)
            state.review = review_output(
                {"merged": merged,
                 "status": "ok" if all(
                     (item or {}).get("status") in {"ok", "deduped"}
                     or bool((item or {}).get("optional"))
                     or str(task_id) in remediated
                     for task_id, item in result_map.items()) else "failed"},
                max_tool_failures=policy.max_tool_failures,
                question=state.request, code_root=_review_code_root(state))
            if state.project_root:
                state.review["agent_ok"] = bool(state.review.get("ok"))
                state.review["project"] = self._review_project(state)
                state.review["ok"] = bool(state.review.get("ok")) and bool(state.review["project"]["ok"])
            all_ok = all((item or {}).get("status") in {"ok", "deduped"}
                         or bool((item or {}).get("optional"))
                         or str(task_id) in remediated
                         for task_id, item in result_map.items())
            self._collect_runtime_evidence(state)
            gate_ok, unmet = acceptance_gate(state)
            _note_unmet_acceptance(state, unmet)
            state.status = review_status(state.review, extra_ok=all_ok and gate_ok)
            state.phase = "review"
            state.recovery = (build_recovery_plan(state, error="单任务重试仍未通过")
                              if state.status == STATUS_FAILED else {})
            self._event(state, "subagent_retry_complete", task_id=task_id,
                        attempt=attempt, previous_status=previous_status,
                        status=retried.get("status"), workflow_status=state.status,
                        max_retries=policy.max_subagent_retries)
            return self._save(state).public()

    def _execute_unleased(self, workflow_id: str,
                runner: Callable[[dict[str, Any], dict[str, str]], Mapping[str, Any]],
                *, synth_runner: Callable[[list[dict[str, Any]], dict[str, Any]], str] | None = None,
                replanner: Callable[..., Any] | None = None, on_event: Callable[..., Any] | None = None,
                session_id: str = "") -> dict[str, Any]:
        from orchestrator import run_plan
        state = self._load(workflow_id)
        if state.status != "planned":
            raise WorkflowError("当前工作流不能执行：%s" % state.status)
        policy = WorkflowPolicy.from_state(state.policy)
        if state.stage_enabled and not state.project_stage:
            try:
                state.project_stage = create_stage(
                    state.project_root, str(self.state_root.parent), workflow_id)
            except (OSError, ValueError, ProjectStageError, SandboxUnavailable) as exc:
                raise WorkflowError("创建隔离试做区失败：%s" % str(exc)[:180]) from exc
            self._event(state, "project_stage_created",
                        file_count=state.project_stage.get("file_count", 0))
            self._save(state)
        # Capture the project before entering the approval gate.  Planning is
        # side-effect free, so this baseline remains valid while the user
        # reviews the plan and survives a later process restart.
        if state.project_root and not state.project_checkpoint:
            self.create_project_checkpoint(workflow_id)
            state = self._load(workflow_id)
        # Keep callbacks process-local.  They may close over an Agent and are
        # deliberately never persisted; after a restart the configured
        # resolver rebuilds them from the durable session identifier.
        self._execution_callbacks[workflow_id] = {
            "runner": runner, "synth_runner": synth_runner,
            "replanner": replanner, "on_event": on_event,
        }
        if session_id:
            state.execution_session_id = _clean_text(session_id, 160)
        if policy.approval_mode == "safe":
            state.status, state.phase = "awaiting_approval", "execute"
            state.interrupt_reason = "执行计划包含可能产生副作用的操作，等待用户审核"
            _sandbox_notice = self._sandbox_approval_notice(state)
            if _sandbox_notice:
                state.interrupt_reason += "；" + _sandbox_notice
            if StateGraph is not None and graph_interrupt is not None:
                self._invoke_graph(state, self._graph_payload(state, approved=False))
            self._event(state, "approval_required", reason=state.interrupt_reason)
            return self._save(state).public()
        self._ensure_capability_lease(state)
        self._save(state)
        state.status = "executing"
        self._event(state, "execute_start", task_count=len(state.tasks))
        def emit(kind, payload):
            payload = dict(payload or {})
            # Keep the public subagent roster live while a wave is running.
            # The event ledger remains the source of truth, while this bounded
            # projection lets the UI render running/blocked/failed members
            # before the whole LangGraph execution returns.
            task_id = str(payload.get("task_id") or "")
            if task_id and kind in {"subagent_start", "subagent_complete", "task_blocked", "task_complete"}:
                with self._lock:
                    row = next((item for item in state.subagents
                                if str(item.get("id") or "") == task_id), None)
                    if row is None and kind == "subagent_start":
                        # A replanner can introduce a task that was not present
                        # in the original plan. Register it immediately so the
                        # live roster and durable task list show the re-dispatch.
                        row = {
                            "id": task_id,
                            "role": str(payload.get("role") or "coder"),
                            "persona": _clean_text(payload.get("persona") or "", 500),
                            "tools": [str(item)[:80] for item in list(payload.get("tools") or [])[:16]],
                            "mcp": str(payload.get("mcp") or "auto"),
                            "reflection": bool(payload.get("reflection_required", True)),
                            "max_steps": payload.get("max_steps"),
                            "status": "pending",
                        }
                        state.subagents.append(row)
                        if not any(str(item.get("id") or "") == task_id for item in state.tasks):
                            state.tasks.append({
                                "id": task_id,
                                "role": row["role"],
                                "task": _clean_text(payload.get("task") or "", 1200),
                                "persona": row["persona"],
                                "tools": list(row["tools"]),
                                "mcp": row["mcp"],
                                "reflection": row["reflection"],
                                "depends_on": [],
                            })
                    if row is not None:
                        if kind == "subagent_start":
                            row["status"] = "running"
                            row["task_thread"] = payload.get("task_thread")
                        else:
                            row["status"] = payload.get("status") or ("blocked" if kind == "task_blocked" else row.get("status"))
                            if payload.get("steps") is not None:
                                row["steps"] = payload.get("steps")
                            if payload.get("max_steps"):
                                row["max_steps"] = payload.get("max_steps")
                            if payload.get("elapsed_ms") is not None:
                                row["elapsed_ms"] = payload.get("elapsed_ms")
                            if payload.get("reason"):
                                row["error"] = _clean_text(payload.get("reason"), 300)
                            if payload.get("error"):
                                row["error"] = _clean_text(payload.get("error"), 300)
                            if payload.get("reflection"):
                                row["reflection_result"] = dict(payload.get("reflection") or {})
            self._event(state, kind, **dict(payload or {}))
            self._save(state)
            if on_event:
                try:
                    on_event(kind, payload)
                except Exception:
                    pass
        def scoped_runner(task, context):
            # The durable ledger is keyed by workflow + task.  If the process
            # dies after a mutating tool returns but before the graph writes its
            # node checkpoint, recovery will replay the recorded result rather
            # than perform the side effect a second time.
            from .tools import project_stage_scope, tool_idempotency_scope
            from config import reset_context_code_root, set_context_code_root
            prepared = task.get("_prepared_context") if isinstance(task, Mapping) else None
            if isinstance(prepared, Mapping):
                bounded_context = {str(key): str(value) for key, value in prepared.items()}
                compression = dict(task.get("_context_compression") or {})
            else:
                bounded_context, compression = compress_context(
                    context, max(240, policy.max_context_chars // 3))
            if compression.get("truncated"):
                task = dict(task)
                task["context_compression"] = compression
            else:
                task = dict(task)
            try:
                requested_child_steps = int(task.get("max_steps") or 0)
            except (TypeError, ValueError):
                requested_child_steps = 0
            if policy.child_max_steps > 0:
                task["max_steps"] = min(
                    policy.child_max_steps,
                    requested_child_steps if requested_child_steps > 0 else policy.child_max_steps,
                )
            else:
                task["max_steps"] = requested_child_steps
            with tool_idempotency_scope(
                    "%s:%s" % (workflow_id, str(task.get("id") or "task")),
                    self.state_root):
                if not state.project_stage:
                    self._event(state, "stage_scope_missing", task_id=str(
                        task.get("id") or "")[:80],
                        reason="project_stage 为空——这个子代理直接跑在项目根上，"
                               "试做区隔离对它完全不生效")
                    return runner(task, bounded_context)
                token = set_context_code_root(state.project_stage["workspace_root"])
                try:
                    # 文件/进程操作走试做区副本；审批台账与自检沙箱租约
                    # 按真实项目记账，所以把 origin 一起带进作用域。
                    with project_stage_scope(origin_root=state.project_root):
                        # 诊断：设置之后、派发之前，各读一次真实生效的根。
                        # 此前这条路径被静态排查过三次都判「机制正常」，而实测
                        # `wf-7cc2e86620ce` 里 python_exec 报的执行目录是**真实
                        # 项目**。分不清是「没设」还是「设了但没生效」，所以两头
                        # 都读一次：两者不一致就说明中间有人把它改回去了。
                        self._stage_scope_probe(state, task, token)
                        return runner(task, bounded_context)
                finally:
                    reset_context_code_root(token)
        if StateGraph is not None:
            # LangGraph owns the bounded review → replan → execute loop.
            # The nested task graph uses ``Send`` for each parallel subagent;
            # there is only one source of truth for replan counters.
            graph_wave_index = 0
            def execute_wave(tasks, prior_results, remaining_steps):
                nonlocal graph_wave_index
                self._refresh_workflow_lease(workflow_id)
                # 能力租约按 wave 滑动续期：长工作流（小时级）不能死在固定窗口上。
                self.refresh_capability_lease(workflow_id)
                self._note_role_capability(state, tasks)
                # remaining_steps == 0 means unlimited when policy.max_steps == 0;
                # it means exhausted only for a positive configured budget.
                if policy.max_steps > 0 and remaining_steps <= 0:
                    return {"ok": False, "results": {}, "steps_used": 0,
                            "blocked": [str(task.get("id")) for task in tasks],
                            "error": "达到工作流步数上限"}
                wave_thread = "%s:exec%s" % (workflow_id, graph_wave_index)
                graph_wave_index += 1
                self._workflow_hook(state, "before_wave", wave=graph_wave_index - 1,
                                    task_count=len(tasks))
                requested_parallel = (len(tasks) if policy.max_subagents <= 0
                                      else min(len(tasks), policy.max_subagents))
                report = self._run_task_dag_langgraph(
                    tasks, scoped_runner, prior_results=prior_results,
                    max_steps=remaining_steps,
                    max_parallel=_effective_task_parallelism(requested_parallel),
                    max_subagents=policy.max_subagents,
                    max_context_chars=policy.max_context_chars,
                    on_event=emit, thread_id=wave_thread,
                    trace_parent_id=str((state.langsmith_trace or {}).get("root_run_id") or ""),
                )
                self._workflow_hook(state, "after_wave", wave=graph_wave_index - 1,
                                    task_count=len(tasks),
                                    status="ok" if report.get("ok") else "failed")
                return report

            graph = self.build_langgraph(
                checkpointer=self._graph_checkpointer,
                execute_wave=execute_wave,
                replan=(lambda failed, results, attempt: self._call_graph_replanner(
                    replanner, failed, results, attempt, emit)) if replanner is not None else None)
            payload: WorkflowGraphState = {
                "workflow_id": workflow_id, "phase": "execute", "status": "executing",
                "selected_option": state.selected_option or {}, "approved": True,
                "tasks": [dict(task) for task in state.tasks], "results": {},
                "step_count": int(state.steps or 0), "replan_count": 0,
                "max_steps": policy.max_steps, "max_replans": policy.max_replans,
                "max_subagents": policy.max_subagents,
                "child_max_steps": policy.child_max_steps,
                "langsmith_trace": dict(state.langsmith_trace or {}),
            }
            graph_state = graph.invoke(
                payload,
                config={"configurable": {"thread_id": workflow_id},
                        "recursion_limit": max(32, policy.max_replans * 4 + 16)},
            )
            current_report = graph_state.get("current_report") or {}
            report = {
                "ok": is_successful_status(graph_state.get("status")),
                "tasks": list(graph_state.get("tasks") or current_report.get("tasks") or state.tasks),
                "results": graph_state.get("results") or {},
                "merged": "", "replans": int(graph_state.get("replan_count", 0)),
                "task_backend": current_report.get("backend", "langgraph_send"),
                "task_threads": list(graph_state.get("task_threads") or []),
                "steps_used": int(graph_state.get("step_count", 0)) - int(state.steps or 0),
                "waves": [], "blocked": [],
                "revisions": list(graph_state.get("replan_history") or []),
                "n_tasks": len(graph_state.get("results") or {}),
                "n_ok": sum(1 for item in (graph_state.get("results") or {}).values()
                            if counts_as_ok(item)),
                "n_failed": sum(1 for item in (graph_state.get("results") or {}).values()
                                 if (item or {}).get("status") == "failed"),
            }
            state.graph_state = dict(graph_state)
            self._event(state, "graph_loop", status=graph_state.get("status"),
                        replans=graph_state.get("replan_count", 0),
                        steps=graph_state.get("step_count", 0))
        else:
            requested_parallel = (len(state.tasks) if policy.max_subagents <= 0
                                  else min(len(state.tasks), policy.max_subagents))
            report = run_plan(state.tasks, runner, synth_runner=synth_runner,
                              max_parallel=_effective_task_parallelism(requested_parallel),
                              max_replans=policy.max_replans,
                              max_steps=policy.max_steps, replanner=replanner, on_event=emit)
        if StateGraph is not None and synth_runner and report.get("results"):
            try:
                report["merged"] = synth_runner(state.tasks, report["results"]) or ""
            except Exception as exc:
                report["merged"] = "（结果合成失败：%s）" % type(exc).__name__
        state.results = report
        state.preview = build_preview_bundle(state.public())
        # A dispatcher can expand the task graph during execution. Keep the
        # durable workflow/task roster aligned with the effective graph so the
        # workbench shows the actual members rather than the initial seed only.
        if report.get("tasks"):
            state.tasks = [dict(item) for item in report.get("tasks") or []]
        state.steps += int(report.get("steps_used") or 0)
        state.replans = int(report.get("replans") or 0)
        result_map = dict(report.get("results") or {})
        # 「失败被补救成功」必须在这里算，而不是等到复核之后：
        #  1. `subagent_rows` 要带补救说明（界面照旧把失败标红，另起一条说补救成功）；
        #  2. 下面的 `review_output` 的 status 判定就取自它（实测漏掉会让 status_ok
        #     为假，verdict 再怎么放行也还是 failed）。
        #
        # 失败记录本身**不动**：`row["status"]` 仍是 failed，`results` 里也原样保留。
        remediated = remediated_task_ids(
            (state.graph_state or {}).get("replan_history"), {"results": result_map})
        self._mark_remediated(state, result_map, remediated)
        subagent_rows = self._subagent_records(state.tasks, policy.child_max_steps)
        for row in subagent_rows:
            result = dict(result_map.get(str(row.get("id"))) or {})
            row["status"] = result.get("status", row.get("status"))
            row["steps"] = int(result.get("steps") or 0)
            if result.get("max_steps"):
                row["max_steps"] = int(result.get("max_steps") or 0) or row.get("max_steps")
            row["elapsed_ms"] = int(result.get("elapsed_ms") or 0)
            if result.get("remediated_by"):
                # 失败仍标红，但把「后来补上了」写进行里，界面要能显示这句。
                row["remediated_by"] = str(result.get("remediated_by"))
                row["remediation_note"] = _clean_text(
                    result.get("remediation_note"), 200)
            if result.get("error"):
                row["error"] = _clean_text(result.get("error"), 300)
            if result.get("reflection"):
                row["reflection_result"] = dict(result.get("reflection") or {})
        state.subagents = subagent_rows
        # 用最终结果校准子代理工具步数总量（事件流按任务归并已是同值，这里防止
        # 非 LangGraph 回退路径或事件丢失导致的偏差），GET 状态直接可读。
        observability = dict(state.observability or {})
        observability["subagent_tool_steps"] = int(sum(
            max(0, int((item or {}).get("steps") or 0))
            for item in result_map.values()))
        state.observability = observability
        merged = report.get("merged") or "\n".join(
            str((item or {}).get("conclusion") or "")
            for item in result_map.values())
        state.review = review_output({"merged": merged,
                                      "status": "ok" if report.get("ok") else "failed",
                                      "trace": {"steps": sum((r.get("trace", {}).get("steps", []) for r in report.get("results", {}).values()), [])}},
                                     max_tool_failures=policy.max_tool_failures,
                                     question=state.request, code_root=_review_code_root(state))
        if state.project_root:
            state.review["agent_ok"] = bool(state.review.get("ok"))
            state.review["project"] = self._review_project(state)
            state.review["ok"] = bool(state.review.get("ok")) and bool(state.review["project"]["ok"])
        state.context_layers["subagent"] = {
            "count": len(report.get("results") or {}),
            "statuses": {key: (value or {}).get("status")
                         for key, value in result_map.items()},
            "reflections": {key: dict((value or {}).get("reflection") or {})
                            for key, value in result_map.items()
                            if (value or {}).get("reflection")},
            "compression": dict(report.get("context_compression") or {}),
        }
        state.context_layers["output"] = {
            "chars": len(merged), "review_ok": bool(state.review.get("ok")),
            "steps": report.get("steps_used", 0),
        }
        self._collect_runtime_evidence(state)
        gate_ok, unmet = acceptance_gate(state)
        _note_unmet_acceptance(state, unmet)
        # `remediated` 在建 subagent 行时已经算好了（那里必须先算，见上面的注释）。
        # 这里只消费，不重算——同一份判据算两遍，迟早会有一遍口径变了而另一遍没跟上。
        all_ok = all((item or {}).get("status") in {"ok", "deduped"}
                     or bool((item or {}).get("optional"))
                     or str(task_id) in remediated
                     for task_id, item in result_map.items())
        state.status = review_status(state.review,
                                     extra_ok=(bool(report.get("ok")) or all_ok)
                                     and gate_ok)
        state.phase = "review"
        state.recovery = (build_recovery_plan(state, error="执行结果未通过复核")
                          if state.status == STATUS_FAILED else {})
        self._event(state, "review", ok=state.review.get("ok"), status=state.status,
                    agent_ok=bool(state.review.get("agent_ok")))
        if state.experience_enabled and not any(e.get("kind") == "experience" for e in state.events):
            try:
                from experience import record_episode
                # 「只做了内容复核、没做独立验证」不是失败——判据与理由见
                # `_experience_outcome`（同文件）。这里只负责把 verdict 拼进
                # decision 那一行，让保留意见跟着进经验库。
                outcome, verdict = _experience_outcome(state.review, state.status)
                recorded = record_episode(
                    state.project_id or "default",
                    get_profile(state.kind).experience_title + state.request,
                    "方案=%s；重规划=%s；复核=%s" % (
                        (state.selected_option or {}).get("id", "未选择"),
                        state.replans, verdict),
                    outcome, lesson="仅保存脱敏的流程摘要；具体代码和凭据不入经验库")
                self._event(state, "experience", recorded=bool(recorded), outcome=outcome)
            except Exception:
                self._event(state, "experience", recorded=False, outcome="unavailable")
        if is_successful_status(state.status) and state.experience_enabled and not state.skill_candidate:
            from .workflow_eval import evaluate_workflow
            skill_evaluation = evaluate_workflow(state.public())
            profile = get_profile(state.kind)
            option_id = str((state.selected_option or {}).get("id") or "workflow")
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", option_id).strip("-") or "workflow"
            state.skill_candidate = {
                "id": "skill-candidate-" + uuid.uuid4().hex[:12],
                "name": profile.skill_name_prefix + slug,
                "description": profile.skill_description_prefix + _clean_text(
                    (state.selected_option or {}).get("title") or "原型开发与验证", 120),
                "body": profile.skill_body
                        + "\n## 脱敏流程摘要\n\n"
                        + "- 任务数：%s\n- 重规划次数：%s\n- 复核通过：是\n" % (
                            len(state.tasks), state.replans),
                "evaluation": {"passed": bool(skill_evaluation.get("passed")),
                                "score": skill_evaluation.get("score"),
                                "checks": [item.get("name") for item in skill_evaluation.get("checks", [])
                                           if item.get("ok")]},
                "status": "pending_approval",
            }
            self._event(state, "skill_candidate", candidate_id=state.skill_candidate["id"])
        return self._save(state).public()

    def _promote_to_approval_gate(self, workflow_id: str,
                                  state: WorkflowState) -> WorkflowState:
        """把停在 `planned` 的工作流推进到**真正的**审批门。

        为什么需要它：界面把 `planned` 与 `awaiting_approval` **都**标成「等待计划审核」
        （`AutonomousCockpit.vue:701` 的 `STATUS_LABEL`），而用户点批准走的就是
        `approve()`、**从不先调 `execute()`**（同文件 2587 行）。原先 `approve()` 只认
        `awaiting_approval`，于是屏幕写着「等待你批准执行计划」、点下去却得到一句
        「当前工作流没有待审核执行」。

        探针实测（`.tmp/probe_approve_matrix.py`）：

            真实=planned          界面报=planned（等待计划审核）-> 拒绝：没有待审核执行
            真实=awaiting_approval 界面报=awaiting_approval      -> 放行

        而 `planned -> awaiting_approval` 那一步在 `execute()` 里，带着三件副作用，
        缺一件就会出问题：

        1. `interrupt_reason` —— 用户看到的「为什么停在这儿」
        2. LangGraph 侧的 approval interrupt —— 不打它，后面
           `_resume_gate(state, "approval", ...)` 会撞上「LangGraph 等待的是 choice，
           而不是 approval」（探针实测的另一种拒绝）
        3. 试做区与项目快照 —— `execute()` 在进门前就建好，用户审计划期间基线不变

        **不复用 `execute()`**：它要求调用方提供 runner/synth_runner/replanner，
        而「点批准」这条路上那些回调可能还没登记（进程重启后就丢了）。真正的执行在
        `approve(auto_execute=True)` 分支里才会用到回调。
        """
        if WorkflowPolicy.from_state(state.policy).approval_mode != "safe":
            # 高自主模式本来就没有审批门，planned 之后直接就是执行。
            return state
        try:
            if state.stage_enabled and not state.project_stage:
                state.project_stage = create_stage(
                    state.project_root, str(self.state_root.parent), workflow_id)
                self._event(state, "project_stage_created",
                            file_count=state.project_stage.get("file_count", 0))
            if state.project_root and not state.project_checkpoint:
                self.create_project_checkpoint(workflow_id)
                state = self._load(workflow_id)
        except (OSError, ValueError, ProjectStageError, SandboxUnavailable) as exc:
            raise WorkflowError("准备审批门失败：%s" % str(exc)[:180]) from exc
        # 执行回调若一个都没有，登记一个空壳占位；auto_execute 分支会自己去 resolver 取。
        self._execution_callbacks.setdefault(workflow_id, {})
        state.status, state.phase = "awaiting_approval", "execute"
        state.interrupt_reason = "执行计划包含可能产生副作用的操作，等待用户审核"
        _sandbox_notice = self._sandbox_approval_notice(state)
        if _sandbox_notice:
            state.interrupt_reason += "；" + _sandbox_notice
        if StateGraph is not None and graph_interrupt is not None:
            self._invoke_graph(state, self._graph_payload(state, approved=False))
        self._event(state, "approval_required", reason=state.interrupt_reason)
        return self._save(state)

    def approve(self, workflow_id: str, approved: bool, *, auto_execute: bool = False) -> dict[str, Any]:
        state = self._load(workflow_id)
        # `planned` 也算数 —— 界面把它和 `awaiting_approval` 都标成「等待计划审核」，
        # 而点批准走的正是这里。详见 `_promote_to_approval_gate` 的 docstring。
        if state.status == "planned":
            state = self._promote_to_approval_gate(workflow_id, state)
        if state.status != "awaiting_approval":
            # 报错要说清**现在在哪、下一步做什么**。原先那句「没有待审核执行」
            # 只会让人盯着屏幕猜 —— 而界面上还写着「等待你批准执行计划」。
            raise WorkflowError(
                "工作流当前在「%s」，没有待批准的执行计划。"
                "若要继续，请在任务列表里启动工作流，或在对话里让它继续。"
                % (state.status or "未知状态"))
        self._workflow_hook(state, "before_approval", approved=bool(approved),
                            task_count=len(state.tasks))
        if not approved:
            state.status, state.phase = "interrupted", "execute"
            state.interrupt_reason = "用户拒绝执行当前计划"
            if StateGraph is not None and graph_interrupt is not None:
                self._resume_gate(state, "approval", {"approved": False})
            self._event(state, "approval_denied")
        else:
            # The plan and its acceptance conditions are approved together.
            # Model-generated criteria cannot approve themselves.
            if state.acceptance_contract.get("items"):
                try:
                    state.acceptance_contract = approve_acceptance(state.acceptance_contract)
                except AcceptanceError as exc:
                    raise WorkflowError(str(exc)) from exc
            state.status, state.phase = "planned", "execute"
            state.policy["approval_mode"] = "high"
            state.interrupt_reason = ""
            if StateGraph is not None and graph_interrupt is not None:
                self._resume_gate(state, "approval", {"approved": True})
            self._event(state, "approval_granted")
            # 计划与验收条件一起获批；游戏工作流同时获批一次自检沙箱边界。
            # 沙箱写不进去只退回逐次审批，不能因此让审批失败。
            self._grant_playtest_sandbox(state, source="plan_approval")
            if auto_execute:
                callbacks = self._execution_callbacks.get(workflow_id)
                # _promote_to_approval_gate 会登记一个空壳 {} 占位；它是「没有
                # 回调」而不是「有回调」。不把空壳当缺省会让下面的 resolver 永不
                # 触发，最后由后台线程调用 execute() 时缺 runner 直接 TypeError。
                if not (isinstance(callbacks, Mapping)
                        and callable((callbacks or {}).get("runner"))):
                    callbacks = None
                if callbacks is None and self._execution_resolver is not None:
                    try:
                        resolved = self._execution_resolver(state.public())
                        if isinstance(resolved, Mapping) and callable(resolved.get("runner")):
                            callbacks = dict(resolved)
                            self._execution_callbacks[workflow_id] = callbacks
                            self._event(state, "auto_execute_reconstructed",
                                        session_id=state.execution_session_id or "workflow")
                    except Exception as exc:
                        self._event(state, "auto_execute_reconstruct_failed",
                                    error=type(exc).__name__)
                if callbacks is None:
                    self._event(state, "auto_execute_unavailable", reason="进程重启后执行回调不可恢复")
                else:
                    self._event(state, "auto_execute_start")
                    self._workflow_hook(state, "after_approval", approved=True,
                                        task_count=len(state.tasks))
                    self._save(state)
                    # The DAG run can take minutes (multiple child agents);
                    # never hold the approval HTTP request until it finishes.
                    # The worker publishes execute_start/subagent_*/complete
                    # events that drive the card via SSE.
                    threading.Thread(
                        target=self._run_approved_execution,
                        args=(workflow_id, dict(callbacks)),
                        name="workflow-execute", daemon=True).start()
                    return state.public()
        self._workflow_hook(state, "after_approval", approved=bool(approved),
                            task_count=len(state.tasks))
        return self._save(state).public()

    def set_acceptance_contract(self, workflow_id: str,
                                items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """User edits criteria at the plan gate; active execution is immutable."""
        with self._lock:
            state = self._load(workflow_id)
            if state.status not in {"planned", "awaiting_approval", "planning", "interrupted"}:
                raise WorkflowError("当前阶段不能修改验收条件")
            try:
                state.acceptance_contract = revise_acceptance(
                    state.acceptance_contract, items)
            except AcceptanceError as exc:
                raise WorkflowError(str(exc)) from exc
            if state.policy.get("approval_mode") == "high" and state.status == "planned":
                state.policy["approval_mode"] = "safe"
            self._event(state, "acceptance_revised",
                        revision=state.acceptance_contract["revision"],
                        item_count=len(state.acceptance_contract["items"]))
            return self._save(state).public()

    def decide_acceptance(self, workflow_id: str, approved: bool,
                          note: str = "") -> dict[str, Any]:
        """Record the user's final decision; a Harness review is only evidence."""
        with self._lock:
            state = self._load(workflow_id)
            if not is_successful_status(state.status):
                # 「完成了但没独立验证」**允许**用户最终验收——而且正该允许：
                # 独立验证缺失时，用户本人就是最后一道核验。把它挡在外面等于
                # 说「这次结果不值得你签字」，那既不成立也会让用户无法收尾。
                raise WorkflowError("工作流尚未完成，不能最终验收")
            contract = dict(state.acceptance_contract or {})
            if not contract.get("items"):
                raise WorkflowError("工作流没有验收条件")
            if contract.get("final_decision") == "accepted":
                raise WorkflowError("工作流已经验收")
            contract["final_decision"] = "accepted" if approved else "rejected"
            contract["final_note"] = _clean_text(note, 2000)
            state.acceptance_contract = contract
            self._event(state, "acceptance_decided", approved=bool(approved))
            return self._save(state).public()

    def record_visual_feedback(self, workflow_id: str,
                               feedback: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a bounded user feedback record with the workflow evidence."""
        with self._lock:
            state = self._load(workflow_id, register=False)
            note = _clean_text(feedback.get("note") or feedback.get("message"), 4000)
            if not note:
                raise WorkflowError("视觉反馈不能为空")
            region = feedback.get("region") if isinstance(feedback.get("region"), Mapping) else None
            safe_region = None
            if region:
                try:
                    values = {key: float(region.get(key) or 0) for key in ("x", "y", "width", "height")}
                    if not all(math.isfinite(value) for value in values.values()):
                        raise ValueError("nonfinite region")
                    safe_region = {key: max(0.0, min(100.0, value)) for key, value in values.items()}
                    safe_region["width"] = min(safe_region["width"], 100 - safe_region["x"])
                    safe_region["height"] = min(safe_region["height"], 100 - safe_region["y"])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WorkflowError("标注区域坐标无效") from exc
            feedback_id = str(feedback.get("id") or uuid.uuid4().hex)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", feedback_id):
                raise WorkflowError("反馈标识无效")
            existing = next((row for row in state.visual_feedback if row.get("id") == feedback_id), None)
            if existing is not None:
                return {"feedback": dict(existing), "items": state.visual_feedback}
            item = {
                "id": feedback_id,
                "artifact_id": _clean_text(feedback.get("artifact_id"), 100),
                "label": _clean_text(feedback.get("label") or "实时画面", 240),
                "note": note,
                "region": safe_region,
                "screenshot": False,
                "sent_at": _clean_text(feedback.get("sent_at") or _now(), 64),
                "status": "pending",
                "snapshots": {},
            }
            state.visual_feedback = [item] + [
                dict(row) for row in state.visual_feedback
                if isinstance(row, Mapping) and str(row.get("id") or "") != item["id"]
            ]
            state.visual_feedback = state.visual_feedback[:20]
            self._event(state, "visual_feedback_recorded", feedback_id=item["id"],
                        screenshot=item["screenshot"])
            saved = self._save(state, register=False, strict=True).public()
            return {"feedback": item, "items": saved.get("visual_feedback") or []}

    def update_visual_feedback(self, workflow_id: str, feedback_id: str,
                               status: str, detail: str = "") -> dict[str, Any]:
        from .visual_feedback import FEEDBACK_STATUSES
        if status not in FEEDBACK_STATUSES:
            raise WorkflowError("反馈状态无效")
        with self._lock:
            state = self._load(workflow_id, register=False)
            item = next((row for row in state.visual_feedback if row.get("id") == feedback_id), None)
            if item is None:
                raise WorkflowError("视觉反馈不存在")
            item.update(status=status, detail=_clean_text(detail, 1000), updated_at=_now())
            self._event(state, "visual_feedback_updated", feedback_id=feedback_id, feedback_status=status)
            self._save(state, register=False, strict=True)
            return {"feedback": dict(item)}

    def save_visual_snapshot(self, workflow_id: str, feedback_id: str,
                             phase: str, image_base64: str) -> dict[str, Any]:
        from .visual_feedback import decode_snapshot, evidence_path
        try:
            image, metadata = decode_snapshot(image_base64)
            path = evidence_path(self.state_root, workflow_id, feedback_id, phase)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        with self._lock:
            state = self._load(workflow_id, register=False)
            item = next((row for row in state.visual_feedback if row.get("id") == feedback_id), None)
            if item is None:
                raise WorkflowError("视觉反馈不存在")
            if phase == "before" and (item.get("snapshots") or {}).get("before"):
                raise WorkflowError("修改前截图已保存，不能覆盖")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(image)
                temporary.replace(path)
            except OSError as exc:
                raise WorkflowError("截图保存失败") from exc
            snapshots = dict(item.get("snapshots") or {})
            snapshots[phase] = dict(metadata, captured_at=_now())
            item.update(snapshots=snapshots, screenshot=bool(snapshots.get("before")))
            self._event(state, "visual_snapshot_saved", feedback_id=feedback_id, snapshot_phase=phase)
            self._save(state, register=False, strict=True)
            return {"feedback": dict(item)}

    def visual_snapshot_path(self, workflow_id: str, feedback_id: str, phase: str) -> Path:
        from .visual_feedback import evidence_path
        with self._lock:
            state = self._load(workflow_id, register=False)
            item = next((row for row in state.visual_feedback if row.get("id") == feedback_id), None)
            if item is None or not (item.get("snapshots") or {}).get(phase):
                raise WorkflowError("截图不存在")
            try:
                path = evidence_path(self.state_root, workflow_id, feedback_id, phase)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            if not path.is_file():
                raise WorkflowError("截图文件不存在")
            return path

    def interrupt(self, workflow_id: str, reason: str = "用户请求中断") -> dict[str, Any]:
        state = self._load(workflow_id)
        state.status, state.interrupt_reason = "interrupted", _clean_text(reason, 500)
        self._event(state, "interrupt", reason=state.interrupt_reason)
        return self._save(state).public()

    def resume(self, workflow_id: str) -> dict[str, Any]:
        state = self._load(workflow_id)
        if state.status != "interrupted":
            raise WorkflowError("当前工作流不是中断状态")
        if state.pending_tasks:
            raise WorkflowError("新的任务 DAG 仍在等待审核")
        state.status = "planning" if not state.tasks else "planned"
        state.interrupt_reason = ""
        self._event(state, "resume")
        return self._save(state).public()

    def request_dag_revision(self, workflow_id: str,
                             tasks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Stage a user DAG change without mutating an active execution wave."""
        state = self._load(workflow_id)
        if state.status not in {"planned", "awaiting_approval", "executing", "interrupted"}:
            raise WorkflowError("当前工作流不能修改任务 DAG：%s" % state.status)
        policy = WorkflowPolicy.from_state(state.policy)
        parsed = self._parse_generated_tasks(list(tasks or []), max_tasks=policy.max_subagents)
        if not parsed:
            raise WorkflowError("新的任务 DAG 无效：请检查唯一 id、依赖和环")
        previous_status = state.status
        state.pending_tasks = parsed
        state.status = "interrupted"
        state.phase = "execute"
        state.interrupt_reason = "收到新的任务 DAG，等待用户审核后安全重排"
        self._event(state, "dag_revision_requested", task_count=len(parsed),
                    previous_status=previous_status)
        return self._save(state).public()

    def approve_dag_revision(self, workflow_id: str, approved: bool) -> dict[str, Any]:
        state = self._load(workflow_id)
        if not state.pending_tasks:
            raise WorkflowError("当前工作流没有待审核的 DAG 修改")
        # 下面 else 分支要用 policy.child_max_steps 重建 subagent 记录。
        # 这行曾经是**缺失**的（从 request_dag_revision 复制过来时漏掉），于是
        # 「用户批准 DAG 修订」一走到就抛 NameError——而这正是 vibecoding 主循环
        # 里最核心的一次交互：模型改计划 → 用户点批准 → 真正开跑。
        # 修的是这行，不是把测试改绿。
        policy = WorkflowPolicy.from_state(state.policy)
        self._workflow_hook(state, "before_approval", approved=bool(approved),
                            task_count=len(state.pending_tasks), node="dag_revision")
        if not approved:
            state.pending_tasks = []
            state.interrupt_reason = "用户拒绝新的任务 DAG"
            self._event(state, "dag_revision_denied")
        else:
            state.tasks = [dict(item) for item in state.pending_tasks]
            state.acceptance_contract = acceptance_on_plan_changed(
                state.acceptance_contract, state.tasks,
                runtime_required=(state.kind == "game"))
            state.policy["approval_mode"] = "safe"
            state.pending_tasks = []
            state.results = {}
            state.review = {}
            state.subagents = self._subagent_records(
                state.tasks, policy.child_max_steps)
            state.status, state.phase, state.interrupt_reason = "planned", "execute", ""
            state.context_layers["task"] = {"count": len(state.tasks),
                                             "ids": [item.get("id") for item in state.tasks],
                                             "revision": True}
            self._event(state, "dag_revision_approved", task_count=len(state.tasks))
            # 修订计划也是用户对边界的一次确认：刷新沙箱租约的有效期。
            self._grant_playtest_sandbox(state, source="dag_revision_approval")
        self._workflow_hook(state, "after_approval", approved=bool(approved),
                            task_count=len(state.tasks), node="dag_revision")
        return self._save(state).public()

    def get(self, workflow_id: str) -> dict[str, Any]:
        # 只读通道（GET 详情、SSE 建连校验、skill-candidate/evaluation）：
        # 不得把磁盘门控态武装进互斥；终态仍会在 _load 内缓存以便 SSE 自关。
        return self._load(workflow_id, register=False).public()

    def evaluate(self, workflow_id: str) -> dict[str, Any]:
        from .workflow_eval import evaluate_workflow
        state = self._load(workflow_id)
        evaluation = evaluate_workflow(state.public())
        try:
            from .langsmith import record_workflow_event
            record_workflow_event(state.public(), "evaluation", {
                "score": evaluation.get("score"),
                "passed": evaluation.get("passed"),
                "failed_tasks": (evaluation.get("metrics") or {}).get("failed_tasks", 0),
                "blocked_tasks": (evaluation.get("metrics") or {}).get("blocked_tasks", 0),
                "idempotency_in_doubt": (evaluation.get("metrics") or {}).get("idempotency_in_doubt", 0),
            })
        except Exception:
            pass
        return evaluation

    def approve_skill_candidate(self, workflow_id: str, approved: bool) -> dict[str, Any]:
        state = self._load(workflow_id)
        candidate = dict(state.skill_candidate or {})
        if not candidate or candidate.get("status") != "pending_approval":
            raise WorkflowError("当前工作流没有待审批的 Skill 候选")
        if not approved:
            candidate["status"] = "rejected"
            state.skill_candidate = candidate
            self._event(state, "skill_candidate_rejected")
            return self._save(state).public()
        try:
            import skills
            saved = skills.save_user_skill(candidate.get("name", ""),
                                           candidate.get("description", ""),
                                           candidate.get("body", ""))
        except Exception as exc:
            self._event(state, "skill_save_failed", error=type(exc).__name__)
            raise WorkflowError("Skill 保存失败：%s" % type(exc).__name__) from exc
        candidate.update(saved, status="saved")
        try:
            import skills
            evaluation = candidate.get("evaluation") or {}
            skills.record_result(candidate.get("name", ""), bool(evaluation.get("passed")),
                                 score=evaluation.get("score"))
        except Exception:
            pass
        state.skill_candidate = candidate
        self._event(state, "skill_saved", name=saved.get("name"))
        return self._save(state).public()

    def checkpoint(self, workflow_id: str, *, approved: bool | None = None) -> dict[str, Any]:
        """Return the durable LangGraph snapshot without advancing side effects."""
        state = self._load(workflow_id)
        if approved is not None and state.status == "awaiting_approval":
            self.approve(workflow_id, bool(approved))
            state = self._load(workflow_id)
        graph = self.build_langgraph(workflow_id=state.workflow_id)
        if graph is None:
            return {"backend": "native", "state": {"phase": state.phase, "status": state.status}}
        snapshot = self._graph_view(graph, state)
        if not snapshot.get("workflow_id"):
            self._invoke_graph(state, self._graph_payload(state), graph=graph)
            snapshot = self._graph_view(graph, state)
        state.graph_state = snapshot
        self._save(state)
        return {"backend": "langgraph", "state": snapshot}

    def _collect_runtime_evidence(self, state: WorkflowState) -> None:
        """Review 前收集"真跑过"证据；只在契约需要时执行。

        契约里没有 `runtime:engine`（非游戏/用户自写条件）时直接跳过，
        不读日志、不改变任何现有工作流的行为。

        fail-closed：读不到日志/没有输出/解析出错都记 `ok=False` + 中文 reason，
        绝不让"没跑过"被当成"没问题"。异常也不上抛——review 本身要能继续，
        未满足明细会如实展示原因，下一步的 replan 会看到它。
        """
        items = (state.acceptance_contract or {}).get("items") or []
        needed = any(
            str(token or "").strip() == "runtime:engine"
            for item in items if isinstance(item, Mapping)
            for token in (item.get("evidence") or [])
        )
        if not needed:
            state.runtime_evidence = {}
            return
        if not state.project_root:
            state.runtime_evidence = {"ok": False, "fresh": False,
                                      "reason": "工作流没有绑定项目目录，无法取得运行期证据。"}
            return
        try:
            from game_workbench import runtime_evidence as _runtime_evidence
            evidence = _runtime_evidence(_review_code_root(state), limit=300)
        except Exception as exc:  # noqa: BLE001 - review 不应因取证失败而崩
            evidence = {"ok": False, "fresh": False,
                        "reason": "运行期证据收集失败：%s: %s" % (type(exc).__name__, str(exc)[:160])}
        evidence = dict(evidence or {})
        evidence["checked_at"] = _now()
        state.runtime_evidence = evidence
        self._event(state, "runtime_evidence", ok=bool(evidence.get("ok")),
                    fresh=bool(evidence.get("fresh")),
                    log_lines=int(evidence.get("log_lines") or 0),
                    errors=len(evidence.get("errors") or []),
                    reason=str(evidence.get("reason") or "")[:300])

    def _review_project(self, state: WorkflowState) -> dict[str, Any]:
        profile = dict(state.project_profile or load_profile(state.project_root))
        stage = state.project_stage or {}
        if not stage:
            return {"ok": True, "status": "legacy", "independent": False,
                    "message": "未启用隔离试做区，沿用兼容模式复核"}
        review_root = str(stage.get("workspace_root") or state.project_root)
        baseline = stage or state.project_checkpoint
        if stage:
            with stage_execution_scope():
                return review_project(
                    review_root, baseline, str(self.state_root.parent),
                    list(profile.get("acceptance_scripts") or []),
                )
        return review_project(
            review_root, baseline, str(self.state_root.parent),
            list(profile.get("acceptance_scripts") or []),
        )

    def create_project_checkpoint(self, workflow_id: str) -> dict[str, Any]:
        """Capture a bounded project baseline for this workflow once."""
        state = self._load(workflow_id)
        if state.project_checkpoint:
            return dict(state.project_checkpoint)
        if not state.project_root:
            raise WorkflowError("当前工作流没有绑定项目目录")
        requested: list[str] = []
        for task in state.tasks or []:
            if isinstance(task, Mapping):
                requested.extend(str(item) for item in task.get("files") or [])
        try:
            manifest = create_checkpoint(
                state.project_root, str(self.state_root.parent), workflow_id,
                requested,
            )
        except (OSError, ValueError) as exc:
            raise WorkflowError("项目快照创建失败：%s" % type(exc).__name__) from exc
        state.project_checkpoint = manifest
        self._event(state, "project_checkpoint_created",
                    checkpoint_id=manifest.get("id"),
                    file_count=manifest.get("file_count", 0),
                    bytes=manifest.get("bytes", 0),
                    skipped=len(manifest.get("skipped") or []))
        self._save(state)
        return dict(manifest)

    def rollback_project_checkpoint(self, workflow_id: str, *, approved: bool = False,
                                    paths: Iterable[str] = ()) -> dict[str, Any]:
        """Restore the baseline, optionally limited to selected project files."""
        paths = tuple(str(item) for item in paths if str(item or "").strip())
        state = self._load(workflow_id)
        if not state.project_checkpoint:
            raise WorkflowError("当前工作流没有可恢复的项目快照")
        if not approved:
            raise WorkflowError("项目回滚需要用户明确批准")
        if not state.project_root:
            raise WorkflowError("当前工作流没有绑定项目目录")
        try:
            result = restore_checkpoint(
                state.project_root, str(self.state_root.parent), workflow_id,
                state.project_checkpoint, selected_paths=paths,
            )
        except (OSError, ValueError) as exc:
            raise WorkflowError("项目快照恢复失败：%s" % type(exc).__name__) from exc
        self._event(state, "project_checkpoint_restored",
                    checkpoint_id=state.project_checkpoint.get("id"),
                    restored=len(result.get("restored") or []),
                    failed=len(result.get("failed") or []),
                    selected=len(list(paths or [])))
        self._save(state)
        result["workflow_id"] = workflow_id
        return result

    def apply_project_stage(self, workflow_id: str, *, approved: bool = False) -> dict[str, Any]:
        """Merge reviewed staged changes into the original project after approval."""
        state = self._load(workflow_id)
        if not approved:
            raise WorkflowError("应用试做区改动需要用户明确批准")
        if not state.project_stage:
            raise WorkflowError("当前工作流没有隔离试做区")
        review = dict(state.review or {}).get("project") or {}
        if not state.review.get("ok") or review.get("status") != "passed" or not review.get("independent"):
            raise WorkflowError("独立终审未通过，不能把试做区改动应用到项目")
        try:
            result = apply_stage(state.project_stage, list(review.get("changes") or []))
        except (OSError, ValueError, ProjectStageError, SandboxUnavailable) as exc:
            raise WorkflowError("应用试做区改动失败：%s" % str(exc)[:180]) from exc
        state.project_stage["status"] = "applied"
        self._event(state, "project_stage_applied", changed=len(result.get("paths") or []))
        self._save(state)
        result["workflow_id"] = workflow_id
        return result

    def rerun_project_review(self, workflow_id: str) -> dict[str, Any]:
        """Recheck the same staged bytes after a failed or incomplete review."""
        state = self._load(workflow_id)
        if state.status not in self._TERMINAL_STATUSES or (state.project_stage or {}).get("status") != "draft":
            raise WorkflowError("只能对已停止且尚未应用的试做区重新复核")
        if not state.results:
            raise WorkflowError("工作流尚无执行结果")
        project_review = self._review_project(state)
        state.review["project"] = project_review
        agent_ok = state.review.get("agent_ok")
        if agent_ok is None:
            agent_ok = bool(state.results.get("ok")) and not state.review.get("failures")
        state.review["ok"] = bool(agent_ok) and bool(project_review.get("ok"))
        self._collect_runtime_evidence(state)
        gate_ok, unmet = acceptance_gate(state)
        _note_unmet_acceptance(state, unmet)
        state.status = settle_status(
            agent_ok=bool(agent_ok),
            project_ok=bool(project_review.get("ok")),
            project_status=str(project_review.get("status") or ""),
            extra_ok=bool(state.results.get("ok")) and gate_ok,
            changed=(bool(project_review.get("changes"))
                     or int(project_review.get("change_count") or 0) > 0))
        if is_successful_status(state.status):
            state.recovery = {}
        else:
            state.recovery = build_recovery_plan(state, error="重新复核未通过")
        self._event(state, "project_stage_reviewed", status=project_review.get("status"),
                    tests=len(project_review.get("tests") or []))
        self._save(state)
        return project_review

    def cleanup_project_stage(self, workflow_id: str) -> dict[str, Any]:
        state = self._load(workflow_id)
        if state.status not in self._TERMINAL_STATUSES or not state.project_stage:
            raise WorkflowError("只能清理已停止的工作流试做区")
        if state.project_stage.get("status") == "discarded":
            return {"ok": True, "status": "discarded", "workflow_id": workflow_id}
        try:
            result = cleanup_stage(state.project_stage, str(self.state_root.parent))
        except (OSError, ValueError, ProjectStageError, SandboxUnavailable) as exc:
            raise WorkflowError("清理试做区失败：%s" % str(exc)[:180]) from exc
        state.project_stage["status"] = "discarded"
        self._event(state, "project_stage_discarded")
        self._save(state)
        return result

    def build_langgraph(self, *, checkpointer=None, workflow_id: str | None = None,
                        execute_wave: Callable[[list[dict[str, Any]], dict[str, Any], int], Mapping[str, Any]] | None = None,
                        replan: Callable[[list[dict[str, Any]], dict[str, Any], int], Any] | None = None,
                        research_runner: Callable[[str], Any] | None = None,
                        option_generator: Callable[[str, ContextPlan], Any] | None = None,
                        task_generator: Callable[[str, Mapping[str, Any], ContextPlan], Any] | None = None):
        """Build the bounded workflow graph when LangGraph is available.

        Without callbacks it is a side-effect-free checkpoint/inspection
        adapter.  With ``execute_wave`` it owns the real bounded
        execute → review → replan loop; tools still run through the existing
        runner and its ToolSpec/approval boundaries.
        """
        if StateGraph is None:
            return None

        # Resolve process-local adapters at graph construction time.  They are
        # never serialized into checkpoint state, while their normalized
        # outputs are written by the node that produced them.
        if workflow_id:
            research_runner = research_runner or self._research_runners.get(workflow_id)
            option_generator = option_generator or self._option_generators.get(workflow_id)
            task_generator = task_generator or self._task_generators.get(workflow_id)

        graph = StateGraph(WorkflowGraphState)

        def provider_call(state: Mapping[str, Any], name: str, callback: Callable[[], Any]) -> Any:
            """Trace one provider call as a bounded LLM/tool child run.

            同时给**模型调用**发一对 `llm_wait_start` / `llm_wait_end`。
            `agent.py` 侧那对只覆盖了 ReAct 循环里的调用，而实测最大的几段静默
            根本不在那里（`wf-f3da9d3ffb32`：等重规划 87s、等规划器 41s、
            等方案选项 23s，三段合计 151s，占全部静默的大头）——那些是本图节点的
            LLM 调用，只能在这里补。

            只对 `llm.*` 发：`tool.research` 与 `execute.wave` 不是模型调用，
            标成「等待模型」就是错的。
            """
            if not name.startswith("llm."):
                try:
                    from .langsmith import workflow_span
                except Exception:
                    return callback()
                with workflow_span(state, name, run_type="llm"):
                    return callback()

            wid = str(state.get("workflow_id") or workflow_id or "")
            started = time.monotonic()
            self._llm_wait_event(wid, "llm_wait_start",
                                 "等待模型 · %s" % name)
            try:
                try:
                    from .langsmith import workflow_span
                except Exception:
                    # Tracing must never alter provider behavior.
                    return callback()
                with workflow_span(state, name, run_type="llm"):
                    return callback()
            finally:
                # 放finally：抛异常时那一段等待同样真实存在，
                # 而 UI 恰恰在这时候最需要知道「刚才那下等了多久」。
                self._llm_wait_event(
                    wid, "llm_wait_end",
                    "模型已返回 · %s · %.1f 秒"
                    % (name, time.monotonic() - started))

        def route_node(state):
            if state.get("status") == "awaiting_research" or state.get("phase") == "research":
                if state.get("event") == "research_gate" or state.get("research_error"):
                    return {"phase": "research", "status": "awaiting_research", "event": "route_research_gate"}
                return {"phase": "research", "status": "awaiting_research", "event": "route_research"}
            if state.get("option_generation_pending") and option_generator is not None:
                return {"phase": "clarify", "status": "generating_options",
                        "event": "route_options"}
            if state.get("status") in {"planned", "awaiting_approval", "executing"} or \
                    (state.get("selected_option") and state.get("approved")):
                return {"phase": "execute", "status": "awaiting_approval" if not state.get("approved") else "executing",
                        "event": "approval_required" if not state.get("approved") else "route_execute"}
            return {"phase": "clarify", "status": "awaiting_choice", "event": "choice_gate"}

        def options_node(state):
            options = list(state.get("options") or [])
            option_source = str(state.get("option_source") or "deterministic")
            generation_error = ""
            provider_attempts: list[dict[str, Any]] = []
            if option_generator is not None:
                plan = ContextPlan(
                    sources=tuple(state.get("sources") or ("direct",)),
                    tool_groups=frozenset({"general", "orchestration", "web"}
                                          if "web" in (state.get("sources") or [])
                                          else {"general", "orchestration"}),
                    skill_names=(), messages=(),
                    budget_chars=int(state.get("max_context_chars", 6000) or 6000), used_chars=0,
                )
                attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                for attempt in range(1, attempts + 1):
                    try:
                        parsed = self._parse_generated_options(
                            provider_call(state, "llm.options", lambda: option_generator(
                                str(state.get("request", "")), plan)), plan)
                        if not parsed:
                            raise ValueError("invalid_options_json")
                        options, option_source = [asdict(item) for item in parsed], "llm"
                        provider_attempts.append({"provider": "options", "attempt": attempt, "ok": True})
                        break
                    except Exception as exc:
                        generation_error = type(exc).__name__
                        provider_attempts.append({"provider": "options", "attempt": attempt,
                                                  "ok": False, "error": generation_error})
            return {"phase": "clarify", "status": "awaiting_choice", "event": "options_generated",
                    "options": options, "option_source": option_source,
                    "option_generation_pending": False,
                    "provider_attempts": provider_attempts,
                    "generation_error": generation_error}

        def research_node(state):
            # When a provider is registered, research is a real graph step:
            # execute it, bound/clean the result, then regenerate options with
            # strict JSON validation.  Any provider/model failure is recorded
            # in the node output and falls back to deterministic options.
            findings = ""
            source = "web"
            research_error = ""
            research_attempts: list[dict[str, Any]] = []
            provider_attempts: list[dict[str, Any]] = []
            if research_runner is not None:
                attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                for attempt in range(1, attempts + 1):
                    try:
                        raw = provider_call(state, "tool.research", lambda: research_runner(
                            str(state.get("request", ""))))
                        if isinstance(raw, Mapping):
                            findings = _clean_text(raw.get("findings") or raw.get("text") or raw, 8000)
                            source = _clean_text(raw.get("source") or "web", 80)
                        else:
                            findings = _clean_text(raw, 8000)
                        if not findings:
                            raise ValueError("empty_research_result")
                        research_attempts.append({"attempt": attempt, "ok": True})
                        break
                    except Exception as exc:
                        error = type(exc).__name__
                        research_error = error
                        research_attempts.append({"attempt": attempt, "ok": False, "error": error})
            if not findings:
                return {"phase": "research", "status": "awaiting_research",
                        "event": "research_gate", "interrupt_reason": "等待联网检索结果",
                        "research_error": research_error,
                        "research_attempts": research_attempts}
            research_plan = ContextPlan(
                sources=tuple(dict.fromkeys([*(state.get("sources") or []), source, "web"])),
                tool_groups=frozenset({"general", "web", "orchestration"}),
                skill_names=(), messages=(("【外部检索摘要】" + findings,) if findings else ()),
                budget_chars=800, used_chars=min(800, len(findings)),
            )
            options = [asdict(item) for item in self._profile_options(
                state.get("kind") or DEFAULT_KIND,
                str(state.get("request", "")) + ("\n" + findings if findings else ""),
                research_plan.sources)]
            option_source = "deterministic"
            if findings and option_generator is not None:
                attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                for attempt in range(1, attempts + 1):
                    try:
                        parsed = self._parse_generated_options(
                            provider_call(state, "llm.research_options", lambda: option_generator(
                                str(state.get("request", "")) + "\n" + findings, research_plan)), research_plan)
                        if not parsed:
                            raise ValueError("invalid_options_json")
                        options, option_source = [asdict(item) for item in parsed], "llm"
                        provider_attempts.append({"provider": "research_options", "attempt": attempt, "ok": True})
                        break
                    except Exception as exc:
                        research_error = research_error or type(exc).__name__
                        provider_attempts.append({"provider": "research_options", "attempt": attempt,
                                                  "ok": False, "error": type(exc).__name__})
            return {
                "phase": "clarify", "status": "awaiting_choice", "event": "research",
                "research_findings": findings, "research_source": source,
                "options": options, "interrupt_reason": research_error,
                "research_error": research_error, "option_source": option_source,
                "research_attempts": research_attempts,
                "provider_attempts": provider_attempts,
            }

        def research_gate_node(state):
            response = graph_interrupt({
                "kind": "research", "request": state.get("request", ""),
                "query": state.get("request", ""),
            })
            response = response if isinstance(response, Mapping) else {"findings": str(response)}
            findings = _clean_text(response.get("findings") or "", 8000)
            if not findings:
                return {"phase": "research", "status": "awaiting_research",
                        "event": "research_gate", "interrupt_reason": "联网检索结果为空"}
            source = _clean_text(response.get("source") or "web", 80)
            plan = ContextPlan(
                sources=tuple(dict.fromkeys([*(state.get("sources") or []), source, "web"])),
                tool_groups=frozenset({"general", "web", "orchestration"}),
                skill_names=(), messages=("【外部检索摘要】" + findings,),
                budget_chars=800, used_chars=min(800, len(findings)),
            )
            options = [asdict(item) for item in self._profile_options(
                state.get("kind") or DEFAULT_KIND,
                str(state.get("request", "")) + "\n" + findings, plan.sources)]
            option_source = "deterministic"
            error = ""
            provider_attempts: list[dict[str, Any]] = []
            if option_generator is not None:
                attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                for attempt in range(1, attempts + 1):
                    try:
                        parsed = self._parse_generated_options(
                            provider_call(state, "llm.research_options", lambda: option_generator(
                                str(state.get("request", "")) + "\n" + findings, plan)), plan)
                        if not parsed:
                            raise ValueError("invalid_options_json")
                        options, option_source = [asdict(item) for item in parsed], "llm"
                        provider_attempts.append({"provider": "research_options", "attempt": attempt, "ok": True})
                        break
                    except Exception as exc:
                        error = type(exc).__name__
                        provider_attempts.append({"provider": "research_options", "attempt": attempt,
                                                  "ok": False, "error": error})
            return {"phase": "clarify", "status": "awaiting_choice", "event": "research",
                    "research_findings": findings, "research_source": source,
                    "options": options, "option_source": option_source,
                    "research_error": error, "interrupt_reason": "",
                    "provider_attempts": provider_attempts}

        def choice_node(state):
            options = list(state.get("options") or [])
            option_source = str(state.get("option_source") or "deterministic")
            generation_error = ""
            provider_attempts: list[dict[str, Any]] = []
            if state.get("option_generation_pending") and option_generator is not None:
                plan = ContextPlan(
                    sources=tuple(state.get("sources") or ("direct",)),
                    tool_groups=frozenset({"general", "orchestration", "web"}
                                          if "web" in (state.get("sources") or [])
                                          else {"general", "orchestration"}),
                    skill_names=(), messages=(),
                    budget_chars=int(state.get("max_context_chars", 6000) or 6000), used_chars=0,
                )
                attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                for attempt in range(1, attempts + 1):
                    try:
                        parsed = self._parse_generated_options(
                            provider_call(state, "llm.options", lambda: option_generator(
                                str(state.get("request", "")), plan)), plan)
                        if not parsed:
                            raise ValueError("invalid_options_json")
                        options, option_source = [asdict(item) for item in parsed], "llm"
                        provider_attempts.append({"provider": "options", "attempt": attempt, "ok": True})
                        break
                    except Exception as exc:
                        generation_error = type(exc).__name__
                        provider_attempts.append({"provider": "options", "attempt": attempt,
                                                  "ok": False, "error": generation_error})
            response = graph_interrupt({
                "kind": "choice", "request": state.get("request", ""),
                "options": options,
                "option_source": option_source,
                "generation_error": generation_error,
                "provider_attempts": provider_attempts,
            })
            response = response if isinstance(response, Mapping) else {"choice": str(response)}
            choice = str(response.get("choice") or "").strip()
            if choice == "web_research":
                return {"phase": "research", "status": "awaiting_research",
                        "event": "research_gate", "interrupt_reason": "等待联网检索结果",
                        "options": options, "option_generation_pending": False,
                        "option_source": option_source}
            if choice == "custom":
                return {"phase": "clarify", "status": "awaiting_choice", "event": "clarify",
                        "request": str(response.get("request") or state.get("request") or ""),
                        "options": list(response.get("options") or options),
                        "option_generation_pending": False, "option_source": option_source}
            selected = response.get("selected_option")
            if not isinstance(selected, Mapping):
                selected = next((item for item in options
                                 if item.get("id") == choice), {})
            return {"phase": "plan", "status": "planning", "event": "choice",
                    "selected_option": dict(selected or {}), "interrupt_reason": "",
                    "options": options, "option_generation_pending": False,
                    "option_source": option_source}

        def plan_node(state):
            tasks = list(state.get("tasks") or [])
            offered_tasks = 0
            if not tasks:
                source = "provided"
                if task_generator is not None:
                    plan = ContextPlan(
                        sources=tuple(state.get("sources") or ("direct",)),
                        tool_groups=frozenset({"general", "orchestration"}),
                        skill_names=(), messages=(), budget_chars=int(state.get("max_context_chars", 6000) or 6000), used_chars=0)
                    attempts = 1 + max(0, int(state.get("max_provider_retries", 2)))
                    for attempt in range(1, attempts + 1):
                        try:
                            # 把生成器的**原始产出**留下来，才能判断解析器丢了多少。
                            # 解析器会静默丢弃重 id / 空描述 / 未知依赖的任务；全部丢弃
                            # 时它返回 []、这里抛 invalid_task_dag 走确定性回退，看得见；
                            # **部分丢弃看不见**——计划照样往下走，只是少了一部分步骤。
                            raw_plan = {}

                            def _call_plan():
                                raw = task_generator(
                                    str(state.get("request", "")),
                                    state.get("selected_option") or {}, plan)
                                raw_plan["raw"] = raw
                                return raw

                            parsed = self._parse_generated_tasks(
                                provider_call(state, "llm.plan", _call_plan),
                                max_tasks=int(state.get("max_subagents", _default_max_agents())))
                            if not parsed:
                                raise ValueError("invalid_task_dag")
                            tasks, source = parsed, "llm"
                            offered_tasks = _raw_task_count(raw_plan.get("raw"))
                            state.setdefault("provider_attempts", []).append({
                                "provider": "plan", "attempt": attempt, "ok": True})
                            break
                        except Exception as exc:
                            state.setdefault("provider_attempts", []).append({
                                "provider": "plan", "attempt": attempt,
                                "ok": False, "error": type(exc).__name__})
                            tasks = []
                if not tasks:
                    if task_generator is not None:
                        # An invalid model plan is recoverable inside the
                        # graph.  Keep the same deterministic contract used
                        # by the native manager and continue to approval.
                        tasks = get_profile(
                            state.get("kind") or DEFAULT_KIND).fallback_tasks()
                        source = "deterministic"
                    else:
                        response = graph_interrupt({
                            "kind": "plan", "request": state.get("request", ""),
                            "selected_option": state.get("selected_option") or {},
                        })
                        if isinstance(response, Mapping):
                            tasks = [dict(item) for item in (response.get("tasks") or [])
                                     if isinstance(item, Mapping)]
                            source = str(response.get("source") or source)
                        else:
                            tasks = []
                if not tasks:
                    # A resumed plan gate without a valid task list remains
                    # paused; callers can submit a corrected plan later.
                    return {"phase": "plan", "status": "planning", "event": "plan_gate"}
                return {"phase": "execute", "status": "planned", "event": "plan",
                        "tasks": tasks, "plan_source": source,
                        "plan_offered": offered_tasks,
                        "provider_attempts": list(state.get("provider_attempts") or [])}
            return {"phase": "execute", "status": "planned", "event": "plan"}

        def approval_node(state):
            if state.get("approved"):
                return {"phase": "execute", "status": "executing" if execute_wave is not None else "planned",
                        "event": "approval_granted", "interrupt_reason": ""}
            response = graph_interrupt({
                "kind": "approval", "request": state.get("request", ""),
                "tasks": list(state.get("tasks") or []),
                "reason": state.get("interrupt_reason") or "执行计划需要用户审核",
            })
            approved = bool(response.get("approved")) if isinstance(response, Mapping) else bool(response)
            return {"phase": "execute", "status": "executing" if approved and execute_wave is not None else
                    ("planned" if approved else "interrupted"),
                    "approved": approved, "event": "approval_granted" if approved else "approval_denied",
                    "interrupt_reason": "" if approved else "用户拒绝执行当前计划"}

        def execute_wave_node(state):
            """Run one execution wave in the real runtime.

            The graph adapter remains side-effect free; the native manager
            still performs the actual ``run_plan`` call.  Counters live in the
            graph state so a future side-effectful node can use the same
            bounded loop without introducing a second outer while-loop.
            """
            if execute_wave is None:
                steps = int(state.get("step_count", 0)) + 1
                return {"phase": "review", "status": "reviewing", "event": "execute_wave",
                        "step_count": steps}
            configured_max = int(state.get("max_steps", workflow_step_limits()[0]) or 0)
            remaining = (0 if configured_max <= 0 else
                         max(0, configured_max - int(state.get("step_count", 0))))
            report = dict(provider_call(
                state, "execute.wave", lambda: execute_wave(
                    list(state.get("tasks") or []),
                    dict(state.get("results") or {}), remaining)) or {})
            merged_results = dict(state.get("results") or {})
            merged_results.update(report.get("results") or {})
            effective_tasks = list(report.get("tasks") or state.get("tasks") or [])
            # 「已被补救成功」的任务不再算失败。
            #
            # 不修这里图永远收敛不到完成态。实测是**三重拦截**，每一处单独修都不够：
            #  1. `run_plan` 的 report["ok"] 是 all(status==ok) -> False
            #  2. `failed_tasks` 每波都把仍 failed 的 broken 算进去 -> 又被送去重规划
            #  3. `review_node` 在 execution_ok 为假时**无条件**返回 review_failed，
            #     `failed` 空不空它根本不看
            # 于是「repair 明明跑成功了，图却落到 event=fail」。
            #
            # 失败记录本身**不动**（results/subagents 里它仍是 failed、界面照旧标红），
            # 这里只是不再让它继续驱动流程。判据见 `remediated_task_ids`。
            _remediated = remediated_task_ids(
                state.get("replan_history"), {"results": merged_results})
            # 按 merged_results **全量**算，不用 `current`——只看本波会漏掉更早波次
            # 留下的失败。`blocked` 也算没做完（跟 run_plan 的 ok 口径一致：
            # 只有 ok / deduped / 可选任务才算完）。
            # `optional` 同时看**结果行**和**任务定义**：被阻断的可选任务在结果行里
            # 没带 optional 标记，只有定义里有。
            _opt_ids = {str(t.get("id")) for t in effective_tasks
                        if isinstance(t, Mapping) and t.get("optional")}
            _live_failed = sorted(
                str(tid) for tid, row in merged_results.items()
                if isinstance(row, Mapping)
                and str(row.get("status") or "") not in {"ok", "deduped"}
                and str(tid) not in _remediated
                and not row.get("optional") and str(tid) not in _opt_ids)
            _live_set = set(_live_failed)
            failed = [dict(task) for task in effective_tasks
                      if str(task.get("id")) in _live_set]
            return {
                "phase": "review", "status": "reviewing", "event": "execute_wave",
                "step_count": int(state.get("step_count", 0)) + int(report.get("steps_used") or 0),
                "results": merged_results, "current_report": report,
                "tasks": effective_tasks,
                "failed_tasks": failed,
                # execution_ok 必须**两个条件同时成立**：
                #   1. `report["ok"]` —— 本波自己没报错（步数上限、波级错误会在这里
                #      变成 False，而那些任务压根没进 results，光看 _live_failed
                #      会漏掉，于是撞上限反而报「完成」）；
                #   2. `_live_failed` 为空 —— **全量**结果里没有仍然失败的活儿。
                #
                # 第 2 条以前是多余的：老代码每波都把已跑过的失败任务**再派一次**，
                # 于是 report["ok"] 天然覆盖全量。不再重复派发（那会重写文件、
                # 重花 token）之后，report["ok"] 只代表本波——不补这条，
                # 「无关的补救任务」就能把真失败洗白（实测 completed）。
                "execution_ok": bool(report.get("ok")) and not _live_failed,
                "remediated": dict(sorted(_remediated.items())),
                "task_threads": list(state.get("task_threads") or []) +
                (list(report.get("task_threads") or [])),
            }

        def review_node(state):
            review = state.get("review") or {}
            if execute_wave is not None:
                if state.get("execution_ok"):
                    return {"phase": "review", "status": "completed", "event": "review",
                            "review": {"ok": True}}
                failed = list(state.get("failed_tasks") or [])
                if replan is not None and failed and int(state.get("replan_count", 0)) < int(state.get("max_replans", 2)) \
                        and (int(state.get("max_steps", workflow_step_limits()[0]) or 0) <= 0 or
                             int(state.get("step_count", 0)) < int(state.get("max_steps", workflow_step_limits()[0]))):
                    return {"phase": "review", "status": "replanning", "event": "review_replan",
                            "review": {"ok": False}}
                return {"phase": "review", "status": "failed", "event": "review_failed",
                        "review": {"ok": False}}
            if review.get("ok") is True:
                return {"phase": "review", "status": STATUS_COMPLETED, "event": "review"}
            failed = list(state.get("failed_tasks") or [])
            if review.get("ok") is False or failed:
                max_replans = int(state.get("max_replans", 2))
                max_steps = int(state.get("max_steps", workflow_step_limits()[0]))
                if failed and int(state.get("replan_count", 0)) < max_replans \
                        and (max_steps <= 0 or int(state.get("step_count", 0)) < max_steps):
                    return {"phase": "review", "status": "replanning", "event": "review_replan"}
                # 图状态里也用三值结局：`unverified` 不是失败。`report["ok"]` 是从
                # 这里反推的（`is_successful_status(graph_state["status"])`），所以
                # 这里若只回 "failed"，直接赋值那侧就再也拿不到「完成了」这个事实。
                return {"phase": "review", "status": _graph_review_status(review),
                        "event": ("review_unverified"
                                  if _graph_review_status(review) == STATUS_COMPLETED_UNVERIFIED
                                  else "review_failed")}
            # A checkpoint-only invocation has no execution result yet.  Keep
            # the adapter inspectable and terminate this dry run cleanly.
            return {"phase": "review", "status": "completed", "event": "review"}

        def replan_node(state):
            attempt = int(state.get("replan_count", 0)) + 1
            history = list(state.get("replan_history") or [])
            failed_ids = [str(item.get("id")) if isinstance(item, Mapping) else str(item)
                          for item in (state.get("failed_tasks") or [])]
            if replan is None:
                history.append({"attempt": attempt, "failed": failed_ids,
                                "status": "simulated"})
                return {"phase": "execute", "status": "executing", "event": "replan",
                        "replan_count": attempt, "failed_tasks": [],
                        "replan_history": history}
            proposal_map: dict[str, Any] = {}
            refused_drop: list[str] = []
            delta: Any = None
            # 下面这条「拒绝」分支会读上面这几个变量，而它们全都只在 `try` 里
            # 赋值——于是**任何**在赋值之前抛出的异常（重规划调用炸了、规范化炸了）
            # 都会让这条分支自己先崩：`UnboundLocalError`，整个图节点挂掉。
            #
            # 实测被探针 `.tmp/probe_rejected_tasks.py` 逮到，连撞三次
            # （`proposal_map` → `error` → `max_subagents`），每次补一个。
            # 一次给全，比一条条试省事。
            error = ""
            known_results: dict[str, Any] = dict(state.get("results") or {})
            max_subagents = int(state.get("max_subagents", _default_max_agents()))
            try:
                proposal = provider_call(
                    state, "llm.replan", lambda: replan(
                        list(state.get("failed_tasks") or []),
                        dict(state.get("results") or {}), attempt))
                # 提案是**三键对象**（add / drop / replace，revise/cancel 为别名），
                # 这一点 prompt 里写得很清楚（agent.py 的 "请给出回溯式修订方案"）。
                #
                # 第一版只取**第一个非空键**，于是：
                #   · `drop` 从来没被读过——prompt 说「取消尚未执行的任务」的能力
                #     **完全不存在**；
                #   · `replace`（prompt 说它「常用来把被阻断的下游任务救回来」）被当成
                #     「追加到计划」，同 id 变成重复，而**任务正文根本没被改写**——
                #     而被阻断的下游任务正是我们这几轮反复撞上的那个失败。
                #   · 只用第一个键，其余键**无声丢弃**，而 `accepted` 却数的是四键总和
                #     （实测 task_count=3 / proposed_ids 只有 1 个 / accepted=true）。
                #
                # 完整契约**仓库里早就实现了**：`orchestrator.apply_proposal`
                # （原地改写 role/task/deps、drop 取消未执行任务、add 追加、
                #  已执行任务不可删改的安全边界、以及逐条 ignored 清单）。
                # 这里不再写第三份实现，直接用它。
                proposal_map = normalize_replan_proposal(proposal)
                # 「乙」：用户批准过的必需任务不许被 drop 取消。模型对可选任务该取消
                # 仍然可以——但验收条件是用户明确签字的东西，系统不该单方面撤掉。
                protected = self._required_task_ids_for_graph(state)
                refused_drop: list[str] = []
                if protected:
                    asked = [str(t).strip() for t in
                             (proposal_map.get("drop") or []) if str(t).strip()]
                    refused_drop = [t for t in asked if t in protected]
                    if refused_drop:
                        proposal_map = dict(proposal_map)
                        proposal_map["drop"] = [t for t in asked if t not in protected]
                known_results = dict(state.get("results") or {})
                max_subagents = int(state.get("max_subagents", _default_max_agents()))
                demoted: list[dict[str, Any]] = []
                # 交给完整契约去应用。`by_id` 与 `task_list` 共享同一批 dict 对象，
                # 所以 apply_proposal 的原地改写会体现在 task_list 上。
                by_id = {str(item.get("id") or ""): dict(item)
                         for item in (state.get("tasks") or []) if isinstance(item, Mapping)}
                task_list = [by_id[tid] for tid in by_id]
                delta = apply_proposal(proposal_map, by_id, task_list,
                                       known_results,
                                       max_subagents if max_subagents > 0 else 0)
                # 已跑过但**没成功**的依赖 -> 降级成跨波次上下文输入。
                #
                # 这条以前是个**副作用**：`ids` 只含本次提案的 id，于是
                # `item["depends_on"] = [d for d in deps if d in ids]` 顺手把任何
                # 「不在本次提案里」的依赖都摘掉了——包括指向失败任务的。效果对，
                # 但它同时也会摘掉**合法**的跨任务依赖，属于误伤。
                #
                # 现在写成明确规则，作用域限定在**本次重规划引入的任务**
                # （added / replaced），判据是「那个依赖已跑过且没成功」：
                #   · 依赖**没跑过** -> 保留为调度边（DAG 正常语义）
                #   · 依赖已成功   -> 保留为调度边
                #   · 依赖已失败/阻塞/被取消 -> 降级为 context_from
                #
                # 为什么需要降级：补救任务若依赖一个失败的上游，按级联规则它会
                # **必然**被阻断 —— 那 `add` 式补救就救不回任何东西了（不依赖失败
                # 任务的新任务，本来也不需要它的上下文）。
                #
                # 而 prompt 教模型用 `replace` 亲手去掉那条依赖，现在 `replace`
                # 真的生效了（此前它被整个丢弃），所以两条路都通。
                introduced = {*list(delta.get("added") or []),
                              *list(delta.get("replaced") or [])}
                unavailable = {tid for tid, row in known_results.items()
                               if isinstance(row, Mapping)
                               and str(row.get("status") or "") != "ok"}
                demoted: list[dict[str, Any]] = []
                for item in task_list:
                    tid = str(item.get("id") or "")
                    if tid not in introduced:
                        continue
                    deps = [str(d) for d in (item.get("depends_on") or [])]
                    bad = [d for d in deps if d in unavailable]
                    if not bad:
                        continue
                    item["depends_on"] = [d for d in deps if d not in unavailable]
                    item["context_from"] = sorted(
                        {*(str(d) for d in (item.get("context_from") or [])), *bad})
                    demoted.append({"task": tid, "demoted_deps": bad})
            except Exception as exc:
                delta = None
                error = type(exc).__name__
                demoted = []
            if not (delta or {}).get("changed"):
                # 「无效」的原因要分清：提案本身空 vs 被安全边界拒绝 vs 容量用尽
                # vs **过程炸了**。
                # 第一版三种都报 `empty_or_invalid_proposal`，其中**容量用尽**报成
                # 「提案无效」最误导——用户会去检查提案怎么写，而真因是没额度了。
                # 过程异常同理，而且更严重：那一次模型**压根没说出提案**，
                # 报「提案无效」是谎报。`error` 必须在最前面那条判据里。
                detail = replan_reject_reason(proposal_map, delta, max_subagents,
                                              len(known_results), error=error)
                history.append({"attempt": attempt, "failed": failed_ids,
                                "status": "rejected", "error": detail,
                                "offered": proposal_offer_summary(proposal_map),
                                "refused_drop": refused_drop,
                                "proposal_raw": _clean_text(json.dumps(
                                        proposal_map, ensure_ascii=False, default=str), 800)})
                return {"phase": "fail", "status": "failed", "event": "replan_failed",
                        "replan_count": attempt, "replan_error": detail,
                        # **不要**在这里回 `"tasks": []`。图随后落到 fail，计划
                        # 不会再被派发——但 `graph_state` 是落盘的审计记录，里面
                        # 同时躺着这批任务的 results 和一份空计划，读的人（或读状态
                        # 的工具/测试）会以为「压根没有任务」。真跑实测：
                        # `graph.tasks=[]` 而 `state.tasks` 有 3 个、
                        # `graph.results` 有 3 条。省略这个键即让 LangGraph 保留原值。
                        "replan_history": history}
            previous_ids = [str(item.get("id") or "") for item in (state.get("tasks") or [])
                            if isinstance(item, Mapping)]
            # 第二张网：即使 drop 已被过滤（`protected`），仍兜一次——
            # 理由与三个坑见 `keep_required_tasks` 的 docstring。
            protected = self._required_task_ids_for_graph(state)
            merged, restored_ids = keep_required_tasks(
                previous_ids and [dict(item) for item in (state.get("tasks") or [])
                                  if isinstance(item, Mapping)] or [],
                task_list, protected)
            if restored_ids:
                task_list = merged
            parsed = task_list
            vanished_ids = self._note_replan_narrowing(
                state, previous_ids, [str(item.get("id") or "") for item in parsed])
            history.append({"attempt": attempt, "failed": failed_ids, "status": "applied",
                            "tasks": [item.get("id") for item in parsed],
                            # `offered` 现在说清**每个键**各提了什么，而不再是一个
                            # 会被四键总和撑大的数字（实测 task_count=3 而
                            # proposed_ids 只有 1 个、accepted 却为 true）。
                            "offered": proposal_offer_summary(proposal_map),
                            # **实际采纳了什么**——这是第一版完全没有的字段。
                            "adopted": {"added": list(delta.get("added") or []),
                                        "dropped": list(delta.get("dropped") or []),
                                        "replaced": list(delta.get("replaced") or [])},
                            "ignored": list(delta.get("ignored") or [])[:12],
                            "refused_drop": refused_drop,
                            # 「补救任务依赖了失败上游 -> 降级为上下文」是**有意**的
                            # 规则（原实现里它是 `ids` 过滤的副作用，还会误伤合法依赖）。
                            # 写进历史，别让它又变成看不见的事。
                            "demoted_deps": demoted[:8],
                            "plan_dropped": vanished_ids,
                            "plan_kept": len(previous_ids) - len(vanished_ids),
                            # 留痕写在**这里**，不写在 task 的字段上：`_parse_
                            # generated_tasks` 会「intentionally drop unknown
                            # fields」，所以 `restored_after_replan` 那个标记活不到
                            # 落盘（实测 wf-b49a04ee5594：tasks 上查不到）。
                            "restored_required": restored_ids})
            return {"phase": "execute", "status": "executing", "event": "replan",
                    "replan_count": attempt, "failed_tasks": [], "tasks": parsed,
                    "results": known_results,
                    "replan_error": "", "replan_history": history}

        def complete_node(state):
            return {"phase": "complete", "status": "completed", "event": "complete"}

        def fail_node(state):
            return {"phase": "fail", "status": "failed", "event": "fail"}

        def hooked_node(name: str, callback: Callable[[Mapping[str, Any]], Mapping[str, Any]]):
            """Attach lifecycle hooks to each concrete LangGraph node."""
            def wrapped(state):
                runtime_state = self._states.get(workflow_id) if workflow_id else None
                if runtime_state is not None:
                    self._workflow_hook(runtime_state, "before_node", node=name,
                                        status=state.get("status"), phase=state.get("phase"))
                try:
                    value = dict(callback(state) or {})
                except BaseException as exc:
                    if runtime_state is not None:
                        self._workflow_hook(runtime_state, "after_node", node=name,
                                            status="error", error=type(exc).__name__)
                    raise
                if runtime_state is not None:
                    self._workflow_hook(runtime_state, "after_node", node=name,
                                        status=value.get("status") or state.get("status"))
                return value
            return wrapped

        def after_choice(state):
            if state.get("status") == "awaiting_research":
                return "research"
            if state.get("status") == "awaiting_choice":
                return "choice"
            if state.get("status") == "planning" and state.get("selected_option"):
                return "plan"
            return END

        def after_research(state):
            if state.get("status") == "awaiting_research":
                return "research_gate"
            if state.get("status") == "awaiting_choice":
                return "choice"
            return END

        def after_route(state):
            if state.get("status") == "awaiting_research":
                return "research"
            if state.get("status") == "generating_options":
                return "options"
            if state.get("status") in {"planned", "awaiting_approval", "executing"} or \
                    (state.get("selected_option") and state.get("approved")):
                return "approval"
            return "choice"

        def after_approval(state):
            return "execute_wave" if state.get("approved") else END

        def after_review(state):
            event = state.get("event")
            if event == "review_replan":
                return "replan"
            if is_successful_status(state.get("status")):
                return "complete"
            return "fail"

        def after_replan(state):
            if execute_wave is None:
                return "execute_wave" if state.get("event") == "replan" else "fail"
            return "execute_wave" if state.get("event") == "replan" and state.get("tasks") else "fail"

        graph.add_node("route", hooked_node("route", route_node))
        graph.add_node("options", hooked_node("options", options_node))
        graph.add_node("research", hooked_node("research", research_node))
        graph.add_node("research_gate", hooked_node("research_gate", research_gate_node))
        graph.add_node("choice", hooked_node("choice", choice_node))
        graph.add_node("plan", hooked_node("plan", plan_node))
        graph.add_node("approval", hooked_node("approval", approval_node))
        graph.add_node("execute_wave", hooked_node("execute_wave", execute_wave_node))
        graph.add_node("review", hooked_node("review", review_node))
        graph.add_node("replan", hooked_node("replan", replan_node))
        graph.add_node("complete", hooked_node("complete", complete_node))
        graph.add_node("fail", hooked_node("fail", fail_node))
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", after_route,
                                    {"research": "research", "options": "options",
                                     "choice": "choice", "approval": "approval"})
        graph.add_edge("options", "choice")
        graph.add_conditional_edges("research", after_research,
                                    {"research_gate": "research_gate", "choice": "choice", END: END})
        graph.add_edge("research_gate", "choice")
        graph.add_conditional_edges("choice", after_choice,
                                    {"research": "research", "choice": "choice", "plan": "plan", END: END})
        graph.add_edge("plan", "approval")
        graph.add_conditional_edges("approval", after_approval, {"execute_wave": "execute_wave", END: END})
        graph.add_edge("execute_wave", "review")
        graph.add_conditional_edges("review", after_review,
                                    {"replan": "replan", "complete": "complete", "fail": "fail"})
        graph.add_conditional_edges("replan", after_replan,
                                    {"execute_wave": "execute_wave", "fail": "fail"})
        graph.add_edge("complete", END)
        graph.add_edge("fail", END)
        return graph.compile(checkpointer=checkpointer or self._graph_checkpointer)


def _canonical_project_root(value: Any) -> str:
    """把项目根路径归一为「同一项目」恒等的形式。

    `os.path.normpath` 处理 `..` / 多余斜杠；`os.path.normcase` 在 Windows
    上是 `lower()`，保证 `D:/A` 与 `d:\\a\\` 视为同一根。空串原样返回。

    `projects.project_id()` 走 `os.path.normcase(os.path.abspath(...))`；
    这里用 `normpath` + `normcase`（不去 `abspath`），因为 `state.project_root`
    在写入时已是绝对路径，去 `abspath` 反而会改变既有行为。两侧只要同
    形归一就能匹配。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.normpath(text))
    except (OSError, ValueError):
        return text


WORKFLOWS = GameWorkflowManager()

__all__ = [
    "GameWorkflowManager", "WorkflowError", "WorkflowOption", "WorkflowPolicy",
    "WorkflowState", "WorkflowGraphState", "WORKFLOWS", "review_output",
]
