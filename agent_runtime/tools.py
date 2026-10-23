"""Application-independent tool contracts and exception isolation.

The runtime accepts legacy ``{"description": ..., "func": ...}`` entries, but
normalizes them to :class:`ToolSpec` before dispatch.  This keeps the existing
tool implementations working while making authority, side effects and schemas
machine-readable instead of scattering name lists through the Agent loop.
"""
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable

_log = logging.getLogger(__name__)


class Capability(str, Enum):
    READ_LOCAL = "read_local"
    READ_EXTERNAL = "read_external"
    WRITE_LOCAL = "write_local"
    WRITE_EXTERNAL = "write_external"
    EXEC = "exec"
    NETWORK = "network"
    ADMIN = "admin"


class SideEffect(str, Enum):
    PURE = "pure"
    IDEMPOTENT_WRITE = "idempotent_write"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


def _legacy_input_schema(required: bool = True) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": (
                    "工具输入。可以是纯文本，也可以是多行 key: value；具体字段见工具说明。"
                ),
            }
        },
    }
    schema["required"] = ["input"] if required else []
    return schema


@dataclass(frozen=True)
class ToolSpec(Mapping[str, Any]):
    """Typed tool metadata with a mapping-compatible legacy view."""

    name: str
    description: str
    func: Callable
    input_schema: Mapping[str, Any] = field(default_factory=_legacy_input_schema)
    capability: Capability = Capability.READ_LOCAL
    side_effect: SideEffect = SideEffect.PURE
    parallel_safe: bool = True
    applications: frozenset[str] = field(default_factory=lambda: frozenset({"developer"}))
    group: str = "general"
    verbatim: bool = False
    #: 两级分类（见 `agent_runtime.tool_domains`）。
    #:
    #: 与 `capability` **正交**：capability 管「能不能做」（护栏查它），
    #: domain/facet 管「做什么、怎么找」（模型查它）。两者都从声明式的
    #: `TOOL_DOMAINS` 取值，**不按名字推断** —— 推断就是拼词表，会无限 regress。
    domain: str = ""
    facet: str = ""
    #: 该工具返回的观察**本身就是一条算出来的结构化判定**（如 eda_report_gate 的
    #: met/unmet/rollback，来自真取像/报告解析），可直接顶替「文件:行号」作为终答证据。
    #: `output_audit.audit_evidence` 据此在「正文无引用 + 本轮有成功权威观察」时不再按
    #: 定位门凭空判 FAIL。声明在 spec 上、经 `coerce_tool_spec` 透传，**不靠名字表**
    #: （本文件在 `tool_mutates` 处已论证：名字表会因「下一个人加了工具忘了登记」而 regress）。
    verdict_authoritative: bool = False

    @property
    def capability_type(self) -> Capability:
        return self.capability

    @property
    def side_effect_level(self) -> SideEffect:
        return self.side_effect

    @property
    def parallel_safety(self) -> bool:
        return self.parallel_safe

    @property
    def owner_app(self) -> frozenset[str]:
        return self.applications

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "schema": self.input_schema,
            "input_schema": self.input_schema,
            "description": self.description,
            "func": self.func,
            "capability": self.capability,
            "capability_type": self.capability,
            "side_effect": self.side_effect,
            "side_effect_level": self.side_effect,
            "parallel_safe": self.parallel_safe,
            "parallel_safety": self.parallel_safe,
            "applications": self.applications,
            "application": self.applications,
            "owner_app": self.applications,
            "group": self.group,
            "verbatim": self.verbatim,
        }
        if key not in aliases:
            raise KeyError(key)
        return aliases[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("description", "func", "schema", "input_schema", "capability",
                     "capability_type", "side_effect", "side_effect_level",
                     "parallel_safe", "parallel_safety", "applications", "application",
                     "owner_app", "group", "verbatim"))

    def __len__(self) -> int:
        return 15


_NETWORK_TOOLS = {
    "web_search", "web_search_batch", "web_transport", "web_fetch", "web_research", "web_subtitles",
    "eda_report_gate",
    "dev_http_request", "dev_mcp_call", "dev_list_connector_tools",
    "dev_mcp_probe", "dev_mcp_discover",
    "dev_mcp_search",
    # 内置浏览器：真开公网网页并与之交互（点/填/读）。它与 web_fetch 的差别是
    # **能交互**——页面上要点击/展开才拿得到的内容，纯文本抓取拿不到。
    # 必须在表里：按名字求并集的消费者（只读角色护栏等）靠它识别"会跨外网"。
    "browser_action",
}
_EXEC_TOOLS = {"python_exec", "run_command", "game_playtest", "self_verify", "dev_preview_adapter_test",
               # 真跑 EDA 工具（Vivado/KLayout/仿真）：执行面由 eda_tcl 的固定 argv 模板限定，
               # 不是任意 shell；脚本内容另有 TCL 护栏（见 eda_tcl.guard_tcl_script）。
               "eda_run_script",
               # 引擎启停/热重载：会拉起/杀掉进程树并占 GPU 租约，与 game_playtest 同级
               # （都是"执行某个进程"），但**不碰工作区文件**。
               "engine_start", "engine_stop", "engine_reload"}
_ADMIN_TOOLS = {
    "dev_approve", "dev_commit", "dev_commit_all", "dev_rollback_changeset",
    "dev_apply_regions", "dev_add_region", "init_regions", "dev_install_tool",
    "dev_mcp_add", "dev_mcp_decide", "dev_mcp_remove",
    "dev_preview_adapter_approve",
                      "dev_desktop_action", "dev_preview_adapter_test",
    "dev_preview_adapter_rollback",
    "start_workflow",
    # 视觉定位点击：会真的点下去（改的是别人的界面状态），与 dev_desktop_action 同级。
    # 归 ADMIN 而非 WRITE_LOCAL：它不改工作区文件，但确实改外部状态，且必须经用户批准。
    "vision_click",
    # 只改共享状态、不改工作区文件的工具（与 dev_commit 同性质）：
    # 认领/心跳/释放、提案、计划落盘。归 ADMIN 而非 WRITE_LOCAL 是刻意的——
    # 它们不需要用户授权，只需要并发与幂等保护（见 test_tool_taxonomy.py 的
    # StateDirToolsAreNotSourceWritesTests）。
    # **曾经漏在这里**，于是 agent.py 的只读角色护栏按 name 求并集时漏掉了它们。
    "dev_lanes", "dev_plan", "dev_propose",
}
_WRITE_TOOLS = {
    "apply_edit", "create_file", "create_artifact", "dev_region_edit",
    # dev_apply_edits 是 apply_edit 的批量版：一次原子提交多个文件的局部替换。
    # **曾经漏在这里**，于是 coerce_tool_spec 把它推成 read_local / SideEffect.PURE /
    # parallel_safe=True —— 一个批量改文件的工具被当成无害只读：既能和别的工具并发
    # 造成写竞争，又拿不到幂等账本的保护。agent.py 里那份同名死常量是包含它的，
    # 测试也只断言那份死常量，所以两边一起错、绿灯一直亮着（见 test_tool_taxonomy.py）。
    "dev_apply_edits",
    "dev_refactor", "dev_rebuild_index", "dev_asset_register", "dev_capture_bug",
    "dev_update_bug", "game_upsert_task",
    "dev_preview_adapter_create",
    "dev_preview_adapter_refresh",
    "dev_preview_adapter_rollback",
    "dev_desktop_action",
    # 下面这一批在注册项里显式声明了 capability/side_effect（tools.py 的 TOOLS），
    # 于是 coerce_tool_spec 不查表、直接采用声明 —— 分类本身是对的。但**名字表
    # 漏了它们**，而 agent.py 的只读角色护栏正是按 name 求并集算出来的，于是
    # **只读 planner 能拿到 dev_patch / dev_move / memory_save**：角色自称只读、
    # 实际改了文件，而报告里这次改动仍记为只读分析。
    # 同一个注册表里两套并存的分类机制（表 vs 注册声明）已经漂移了 14 个名字。
    "dev_patch", "dev_move", "dev_deps", "dev_pricing", "dev_skill_create",
    "generate_data_file", "memory_save", "memory_forget",
    # 受控越界写（高权限 + 白名单内）。它们的 capability 是 write_external 而非
    # write_local —— 分类仍由注册声明给出，这里补的是**名字表**，让按 name 求并集
    # 的消费者（只读角色护栏等）也能看见它们，不必再手写例外名单。
    "create_external_file", "edit_external_file", "delete_external_file",
}
_IRREVERSIBLE_TOOLS = {"run_command", "game_playtest", "dev_mcp_call"}
_NO_ARG_TOOLS = {
    "dev_list_connectors", "dev_list_regions", "dev_verify_contracts",
    "dev_rebuild_index", "dev_list_changesets", "dev_list_bugs", "init_regions",
}
_VERBATIM_TOOLS = {"python_exec", "gen_video_prompt"}

# 上面几张表的**公开只读别名**，指向同一份内容（不是副本）。
#
# 加这一层是为了让"单一事实来源"名副其实：`coerce_tool_spec` 读的是上面这些下划线
# 名字，而测试与审计脚本需要引用同一份。以前没有公开入口，于是它们各自抄了agent.py
# 里的副本去断言——两份表对 `dev_apply_edits` 给出相反答案，而测试一直绿着。
# 抄副本的测试断的是抄来的东西，不是行为；只有断真表才有意义。
WRITE_TOOLS = frozenset(_WRITE_TOOLS)
ADMIN_TOOLS = frozenset(_ADMIN_TOOLS)
EXEC_TOOLS = frozenset(_EXEC_TOOLS)
IRREVERSIBLE_TOOLS = frozenset(_IRREVERSIBLE_TOOLS)
NETWORK_TOOLS = frozenset(_NETWORK_TOOLS)
NO_ARG_TOOLS = frozenset(_NO_ARG_TOOLS)
VERBATIM_TOOLS = frozenset(_VERBATIM_TOOLS)

_SCHEMA_OVERRIDES: dict[str, dict[str, Any]] = {
    "search_knowledge": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "search_code": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "web_search": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer", "minimum": 1},
            "end": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    },
    "grep": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
    },
    "list_dir": {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}},
        "required": [],
    },
    "calculate": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
    "dev_use_skill": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    "tool_search": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "engine_logs": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 400,
                      "description": "读取日志尾部的行数，默认 120。"},
            "contains": {"type": "string",
                         "description": "在整个日志里查找该文本（大小写不敏感），"
                                        "如 CLICK_PROBE。tail 里没有某标记时用它搜全日志。"},
        },
        "required": [],
    },
}


def _infer_group(name: str) -> str:
    if name.startswith("web_"):
        return "web"
    if name in {"search_knowledge"}:
        return "knowledge"
    if name in {"search_code", "read_file", "grep", "list_dir"}:
        return "code"
    if "asset" in name or name.startswith("gen_"):
        return "assets"
    if name.startswith("game_") or name.startswith("engine_"):
        return "game"
    if name.startswith("dev_") or name in {
        "apply_edit", "create_file", "create_artifact", "run_command", "self_verify",
    }:
        return "developer"
    if name in {"delegate", "orchestrate"}:
        return "orchestration"
    return "general"


def coerce_tool_spec(name: str, value: Any) -> ToolSpec:
    """Convert a legacy mapping to a typed spec without changing its function."""
    if isinstance(value, ToolSpec):
        return value
    if not isinstance(value, Mapping) or not callable(value.get("func")):
        raise TypeError("invalid tool registration: %s" % name)
    capability = Capability.READ_LOCAL
    side_effect = SideEffect.PURE
    parallel_safe = True
    if name in _NETWORK_TOOLS or name.startswith("web_"):
        capability = Capability.NETWORK
    if name in _EXEC_TOOLS:
        capability = Capability.EXEC
        side_effect = SideEffect.MUTATING
        parallel_safe = False
    if name in _WRITE_TOOLS:
        capability = Capability.WRITE_LOCAL
        side_effect = SideEffect.MUTATING
        parallel_safe = False
    if name in _ADMIN_TOOLS:
        capability = Capability.ADMIN
        side_effect = SideEffect.MUTATING
        parallel_safe = False
    if name in _IRREVERSIBLE_TOOLS:
        side_effect = SideEffect.IRREVERSIBLE
        parallel_safe = False
    if name in {"orchestrate", "preview_project"}:
        parallel_safe = False
    schema = value.get("input_schema") or value.get("schema") or _SCHEMA_OVERRIDES.get(name)
    if schema is None:
        schema = _legacy_input_schema(required=name not in _NO_ARG_TOOLS)
    apps = value.get("applications") or {"developer"}
    return ToolSpec(
        name=name,
        description=str(value.get("description") or ""),
        func=value["func"],
        input_schema=dict(schema),
        capability=Capability(value.get("capability", capability)),
        side_effect=SideEffect(value.get("side_effect", side_effect)),
        parallel_safe=bool(value.get("parallel_safe", parallel_safe)),
        applications=frozenset(apps),
        group=str(value.get("group") or _infer_group(name)),
        verbatim=bool(value.get("verbatim", name in _VERBATIM_TOOLS)),
        domain=str(value.get("domain") or ""),
        facet=str(value.get("facet") or ""),
        verdict_authoritative=bool(value.get("verdict_authoritative", False)),
    )


#: 「这个工具会不会改东西」的权威判据 —— capability 与 side_effect **两个维度都问**。
#:
#: 为什么两个都要：有的工具改的是**共享状态**而不是工作区文件（`dev_lanes` /
#: `dev_commit` 归 ADMIN）；有的声明了**不可逆副作用**。任一为真就说明它会改东西，
#: 只看 `WRITE_*` 会漏掉前一半，只看 `side_effect` 会漏掉显式声明 capability 的新工具。
#:
#: 为什么放在这里：这是**唯一**一份判据。此前 `agent.py` 的只读角色过滤与
#: `game_workflow.WRITE_CAPABLE_TOOLS` 各有一份，两份都会漂 —— 前者曾实测漏 14 个
#: 会改东西的工具（`dev_apply_edits` 漏登记却一直绿灯），后者只有 6 个名字。
#: 名字表是会 regress 的：下一个人加了新工具没人记得往表里补。
_MUTATING_CAPABILITIES = frozenset({
    Capability.WRITE_LOCAL, Capability.WRITE_EXTERNAL,
    Capability.ADMIN, Capability.EXEC,
})


def tool_mutates(spec: ToolSpec | None) -> bool:
    """这个工具**会不会改东西**。判据来自运行期真实分类，不按名字推断。

    `spec=None`（工具不在注册表里）判 `False`：未知不等于危险，且调用方通常
    已经先做了「名字真的存在」这一层检查 —— 这里只回答「存在的东西会不会改」。
    """
    if spec is None:
        return False
    return (spec.capability in _MUTATING_CAPABILITIES
            or spec.side_effect is not SideEffect.PURE)


def _apply_domains(spec: ToolSpec) -> ToolSpec:
    """把 `tool_domains.TOOL_DOMAINS` 里声明的分类贴到 `ToolSpec` 上。

    注册项里**没有**写 `domain=` 时回落到声明表 —— 声明表是唯一真相，
    而它就摆在 `agent_runtime/tool_domains.py` 里，改分类只改那一处。
    """
    if spec.domain and spec.facet:
        return spec
    from .tool_domains import TOOL_DOMAINS

    declared = TOOL_DOMAINS.get(spec.name)
    if not declared:
        return spec
    return replace(spec, domain=declared[0], facet=declared[1])


def _unclassified(spec: ToolSpec) -> str:
    return "" if spec.domain else spec.name


def upgrade_registry(registry: dict[str, Any]) -> dict[str, ToolSpec]:
    for name, value in list(registry.items()):
        registry[name] = _apply_domains(coerce_tool_spec(name, value))
    return registry


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str
    data: Any = None
    error_kind: str = ""
    artifacts: Any = field(default_factory=dict)
    idempotency_key: str = ""
    replayed: bool = False


_IDEMPOTENCY_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar(
    "tool_idempotency_scope", default=None)
_PROJECT_STAGE_SCOPE: ContextVar[bool] = ContextVar("project_stage_scope", default=False)
#: 试做区对应的真实项目根。试做区里文件/进程操作走 ``_get_code_root()``（副本），
#: 但审批台账与自检沙箱租约是**按真实项目根**记账的；没有这一条，stage 里的
#: ``dev_desktop_action`` / ``vision_click`` 会去 stage 目录下找租约，永远查不到，
#: 于是「默认开启试做区」反而让自检动作全被拦截（实测 wf-57af9cdb2f18 的成因之一）。
_PROJECT_STAGE_ORIGIN: ContextVar[str] = ContextVar("project_stage_origin", default="")
#: 用户本轮消息明确指定了桌面动作（「在(400,300)点一下」「输入 hello」等），
#: 等同于口头授权——`dev_desktop_action` / `vision_click` 可跳过 per-action
#: 审批门直接执行。由 `agent.py` 回合开始时置位，回合结束 reset。
#: 安全边界：仅 target=engine/embedded 受益；foreground 永远走审批。
_DESKTOP_PRE_AUTHORIZED: ContextVar[bool] = ContextVar("desktop_pre_authorized", default=False)
#: 本轮聊天是否允许发起工作流。默认 True —— 聊天回合之外的一切调用（直接调工具、
#: 单测、前端显式 POST /api/agent/workflow/start）都不受影响，照常放行。
#: 由 `agent.py` 在回合开始时按「用户是否显式要求工作流」置位：用户没点名要
#: 工作流时设为 False，于是 `start_workflow` 工具软拦截、引导 agent 直接用
#: 内联的游戏/开发工具，而不是自作主张升级为跨窗口工作流。回合结束复位。
_CHAT_WORKFLOW_ALLOWED: ContextVar[bool] = ContextVar("chat_workflow_allowed", default=True)
_IDEMPOTENCY_LOCK = threading.RLock()


def stage_scope_active() -> bool:
    """本线程是否在项目试做区作用域内。

    刻意与 ``project_stage_scope`` 的置位点放在一起：那个 ContextVar 只表示
    「作用域开着」，不表示**代码根真的被切到了试做区**——而后者才是隔离生效
    的真条件（``python_exec`` 用 ``_get_code_root()`` 当cwd）。
    """
    return bool(_PROJECT_STAGE_SCOPE.get())


def stage_origin_root() -> str:
    """当前试做区对应的真实项目根；不在试做区作用域时返回空串。

    只给「按真实项目记账」的东西用（审批台账、自检沙箱租约）。文件读写和
    引擎进程仍应使用 ``_get_code_root()``，那才是试做区副本。
    """
    return str(_PROJECT_STAGE_ORIGIN.get() or "")


def desktop_pre_authorized() -> bool:
    """本回合用户是否已口头授权桌面动作（详见 ``_DESKTOP_PRE_AUTHORIZED``）。

    ``dev_desktop_action`` / ``vision_click`` 的审批门读这一条：True 时
    ``target=engine/embedded`` 的当场指定动作可跳过审批直接执行。
    """
    return bool(_DESKTOP_PRE_AUTHORIZED.get())


def chat_workflow_allowed() -> bool:
    """本轮聊天是否允许发起工作流（详见 ``_CHAT_WORKFLOW_ALLOWED``）。

    ``start_workflow`` 工具读这一条：False 时软拦截并引导 agent 改用内联工具。
    聊天回合之外（直接调工具 / 单测 / 前端显式 POST）默认 True，不受影响。
    """
    return bool(_CHAT_WORKFLOW_ALLOWED.get())


@contextmanager
def desktop_pre_authorization(active: bool):
    """回合级置位/复位桌面动作口头授权标志。

    由 ``agent.py`` 在回合开始时按 :func:`_desktop_intent_for_turn` 的判定调用；
    ``active=False`` 时不改作用域（不余一个多余的 token），退出保证复位。
    """
    if not active:
        yield
        return
    token = _DESKTOP_PRE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _DESKTOP_PRE_AUTHORIZED.reset(token)


def stage_cwd_report(cwd: str) -> str:
    """在试做区里执行时，说清相对路径究竟落在哪个目录。

    **为什么需要这一行**（实测，``wf-f3da9d3ffb32``）：一次真实工作流里，
    子代理用 ``open("values/level.gd", "wb")`` 改了文件并报告
    ``WRITE_OK: True``，而事后核对发现——

        真实项目 values/level.gd   10-03 22:53:12   ← 改了
        试做区 workspace           09-11 18:55:51   ← 从没被碰过

    也就是说**写入绕过了试做区**，而 UI 仍在报「项目改动位于临时试做区」。
    光看结果无法区分两种可能：(a) 执行时 cwd 就是真实项目（隔离没生效），
    (b) cwd 是试做区、事后有别处把它拷回了真实项目。这两者修法完全不同，
    而试做区 workspace 的 mtime 从未被更新——倾向于 (a)，但那是推断不是证据。

    所以让工具**自报实际执行目录**。这一行只在试做区激活时出现，
    平时运行不改变任何输出形状。
    """
    root = os.path.normcase(os.path.abspath(cwd or ""))
    # 按**路径分段**判，不硬编码基址：`project_stage.create_stage` 用的是
    # `Path(state_root)/"project_stages"/<wid>`，而生产的 state_root 是
    # `<repo>/.docmind`、测试里是临时目录——写死 `config.STATE_ROOT` 会把
    # 明明在试做区里的路径判成"不在"（第一版就是这么错的，探针当场抓到）。
    inside = "project_stages" in root.replace("\\", "/").split("/")
    return ("执行目录：%s%s"
            % (cwd, "" if inside else "  ← **不在试做区内，相对路径会写进真实项目**"))

_STAGE_ALLOWED_TOOLS = frozenset({
    "read_file", "search_code", "grep", "list_dir", "search_knowledge",
    "calculate", "apply_edit", "create_file", "python_exec", "run_command",
    "self_verify", "dev_list_regions", "dev_region_read", "dev_region_edit",
    "dev_region_verify", "web_search", "web_search_batch", "web_fetch",
    "web_research", "tool_search", "inspect_data_file", "generate_data_file",
    "eda_report_gate",
    "preview_project", "game_screenshot", "dev_desktop_capture",
    # 自检必须能在**试做区副本**里真跑起来并观察。这些工具都是项目作用域的：
    # 引擎进程按当前代码根登记（stage 里就是副本），项目窗口只按本项目解析；
    # 输入动作仍要过审批台账/自检沙箱租约，不会因为放开白名单就获得任意桌面控制。
    # 之前默认开启试做区时，子代理连 engine_start/engine_logs 都调不了，
    # 自检只能在 stage_enabled=false 下跑——等于默认配置无法完成它要求的验证。
    "engine_start", "engine_stop", "engine_reload", "engine_logs",
    "dev_desktop_action", "vision_locate_click", "vision_click",
})


@contextmanager
def project_stage_scope(origin_root: str = ""):
    """Allow only project-scoped and read-only tools during a staged workflow.

    ``origin_root`` 是试做区所对应的**真实项目根**：审批台账与自检沙箱租约都
    记在它名下，工具需要查这两样时用 :func:`stage_origin_root`；文件读写和引擎
    进程仍走当前代码根（试做区副本）。
    """
    origin_token = _PROJECT_STAGE_ORIGIN.set(str(origin_root or ""))
    token = _PROJECT_STAGE_SCOPE.set(True)
    try:
        from .enterprise_sandbox import stage_execution_scope
        with stage_execution_scope():
            yield
    finally:
        _PROJECT_STAGE_SCOPE.reset(token)
        _PROJECT_STAGE_ORIGIN.reset(origin_token)


@contextmanager
def tool_idempotency_scope(namespace: str, storage_dir: str | Path):
    """Attach durable exactly-once protection to mutating tool calls.

    The scope is deliberately explicit and task-sized.  Read-only tools are
    never cached.  A process crash after a side effect but before a graph node
    checkpoint therefore leaves a ``running`` ledger row; recovery blocks the
    ambiguous replay instead of executing the mutation a second time.
    """
    root = Path(storage_dir).resolve()
    token = _IDEMPOTENCY_SCOPE.set((str(namespace), str(root / "tool_calls.sqlite")))
    try:
        yield
    finally:
        _IDEMPOTENCY_SCOPE.reset(token)


def _idempotency_key(namespace: str, tool_name: str, argument: str) -> str:
    payload = json.dumps(
        {"namespace": namespace, "tool": tool_name, "argument": argument},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _idempotency_claim(tool_name: str, argument: str) -> tuple[str, str, ToolResult | None]:
    scope = _IDEMPOTENCY_SCOPE.get()
    if not scope or not tool_name:
        return "", "", None
    namespace, db_path = scope
    key = _idempotency_key(namespace, tool_name, argument)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _IDEMPOTENCY_LOCK, closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_calls ("
            "idempotency_key TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
            "tool_name TEXT NOT NULL, argument_hash TEXT NOT NULL, status TEXT NOT NULL, "
            "ok INTEGER, text TEXT, error_kind TEXT, updated_at REAL NOT NULL)"
        )
        row = conn.execute(
            "SELECT status, ok, text, error_kind FROM tool_calls WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row:
            status, ok, text, error_kind = row
            if status == "completed":
                return key, db_path, ToolResult(
                    bool(ok), str(text or ""), error_kind=str(error_kind or ""),
                    idempotency_key=key, replayed=True,
                )
            return key, db_path, ToolResult(
                False,
                "工具调用已在先前执行中开始，但完成状态未知；为避免重复副作用，本次未重放。",
                error_kind="idempotency_in_doubt", idempotency_key=key, replayed=True,
            )
        conn.execute(
            "INSERT INTO tool_calls VALUES (?, ?, ?, ?, 'running', NULL, NULL, NULL, ?)",
            (key, namespace, tool_name,
             hashlib.sha256(argument.encode("utf-8")).hexdigest(), time.time()),
        )
        conn.commit()
    return key, db_path, None


def _idempotency_complete(db_path: str, key: str, result: ToolResult) -> None:
    if not db_path or not key:
        return
    with _IDEMPOTENCY_LOCK, closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.execute(
            "UPDATE tool_calls SET status = 'completed', ok = ?, text = ?, "
            "error_kind = ?, updated_at = ? WHERE idempotency_key = ?",
            (int(result.ok), result.text, result.error_kind, time.time(), key),
        )
        conn.commit()


def execute_tool(function: Callable, argument: str, legacy_failure: Callable, *,
                 tool_name: str = "", side_effect: SideEffect = SideEffect.PURE) -> ToolResult:
    if _PROJECT_STAGE_SCOPE.get() and tool_name not in _STAGE_ALLOWED_TOOLS:
        return ToolResult(False, "试做区禁止调用会修改外部环境的工具；请在验收后单独执行。",
                          error_kind="project_stage_blocked")
    key = db_path = ""
    if side_effect != SideEffect.PURE:
        try:
            key, db_path, replay = _idempotency_claim(tool_name, argument)
        except Exception as exc:
            # Never execute a mutating/irreversible tool without its durable
            # claim.  A transient filesystem/SQLite failure is safer as a
            # blocked tool result than as an untracked side effect.
            _log.warning("幂等 claim 失败：tool=%s, exc=%s",
                          tool_name, type(exc).__name__, exc_info=True)
            return ToolResult(False, "幂等保护不可用，工具未执行。",
                              error_kind="idempotency_unavailable")
        if replay is not None:
            return replay
    try:
        value = function(argument)
        if isinstance(value, ToolResult):
            result = value
        elif not isinstance(value, str):
            result = ToolResult(False, "工具执行失败：返回值不符合工具协议。", error_kind="invalid_result")
        else:
            ok = not legacy_failure(value)
            result = ToolResult(ok, value, error_kind="" if ok else "legacy_failure")
    except Exception as exc:
        # Exception messages may contain credentials or arbitrary external data.
        exc_name = type(exc).__name__
        exc_text = str(exc).lower()
        reason = getattr(exc, "reason", None)
        reason_name = type(reason).__name__.lower() if reason is not None else ""
        is_timeout = (isinstance(exc, TimeoutError) or "timeout" in exc_name.lower()
                      or "timed out" in exc_text or "timeout" in reason_name)
        # 工具真实异常 traceback 进日志（安全方向不变：对外仍只暴露
        # 类型名，不暴露 message）。用户侧排查时按 tool_name 检索日志
        # 即可看到完整栈；不写日志 = 永久只有「工具执行失败（X）」一行。
        _log.warning("工具执行异常：tool=%s, exc_name=%s", tool_name, exc_name,
                      exc_info=True)
        result = ToolResult(False, "工具执行失败（%s）。" % exc_name,
                            error_kind="timeout" if is_timeout else "exception")
    if key:
        result = ToolResult(result.ok, result.text, data=result.data,
                            error_kind=result.error_kind, artifacts=result.artifacts,
                            idempotency_key=key, replayed=False)
        try:
            _idempotency_complete(db_path, key, result)
        except Exception as exc:
            # Keep the side-effect result observable, but leave the claim in
            # ``running`` so a later recovery refuses an ambiguous replay.
            # 现在记日志：之前完全静默 → 长期 `idempotency_in_doubt` 没
            # 任何线索，靠用户报「上次执行好像没成功」来发现。
            _log.warning(
                "幂等 complete 失败：tool=%s, key=%s, db=%s, exc=%s",
                tool_name, key, db_path, type(exc).__name__,
                exc_info=True,
            )
    return result
