"""Application-independent tool contracts and exception isolation.

The runtime accepts legacy ``{"description": ..., "func": ...}`` entries, but
normalizes them to :class:`ToolSpec` before dispatch.  This keeps the existing
tool implementations working while making authority, side effects and schemas
machine-readable instead of scattering name lists through the Agent loop.
"""
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable


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
    "dev_http_request", "dev_mcp_call", "dev_list_connector_tools",
    "dev_mcp_probe", "dev_mcp_discover",
    "dev_mcp_search",
}
_EXEC_TOOLS = {"python_exec", "run_command", "game_playtest", "self_verify", "dev_preview_adapter_test"}
_ADMIN_TOOLS = {
    "dev_approve", "dev_commit", "dev_commit_all", "dev_rollback_changeset",
    "dev_apply_regions", "dev_add_region", "init_regions", "dev_install_tool",
    "dev_mcp_add", "dev_mcp_decide", "dev_mcp_remove",
    "dev_preview_adapter_approve",
                      "dev_desktop_action", "dev_preview_adapter_test",
    "dev_preview_adapter_rollback",
    "start_workflow",
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
    if name.startswith("game_"):
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
    )


def upgrade_registry(registry: dict[str, Any]) -> dict[str, ToolSpec]:
    for name, value in list(registry.items()):
        registry[name] = coerce_tool_spec(name, value)
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
_IDEMPOTENCY_LOCK = threading.RLock()


def stage_scope_active() -> bool:
    """本线程是否在项目试做区作用域内。

    刻意与 ``project_stage_scope`` 的置位点放在一起：那个 ContextVar 只表示
    「作用域开着」，不表示**代码根真的被切到了试做区**——而后者才是隔离生效
    的真条件（``python_exec`` 用 ``_get_code_root()`` 当cwd）。
    """
    return bool(_PROJECT_STAGE_SCOPE.get())


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
    "preview_project", "game_screenshot", "dev_desktop_capture",
})


@contextmanager
def project_stage_scope():
    """Allow only project-scoped and read-only tools during a staged workflow."""
    token = _PROJECT_STAGE_SCOPE.set(True)
    try:
        from .enterprise_sandbox import stage_execution_scope
        with stage_execution_scope():
            yield
    finally:
        _PROJECT_STAGE_SCOPE.reset(token)


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
        except Exception:
            # Never execute a mutating/irreversible tool without its durable
            # claim.  A transient filesystem/SQLite failure is safer as a
            # blocked tool result than as an untracked side effect.
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
        result = ToolResult(False, "工具执行失败（%s）。" % exc_name,
                            error_kind="timeout" if is_timeout else "exception")
    if key:
        result = ToolResult(result.ok, result.text, data=result.data,
                            error_kind=result.error_kind, artifacts=result.artifacts,
                            idempotency_key=key, replayed=False)
        try:
            _idempotency_complete(db_path, key, result)
        except Exception:
            # Keep the side-effect result observable, but leave the claim in
            # ``running`` so a later recovery refuses an ambiguous replay.
            pass
    return result
