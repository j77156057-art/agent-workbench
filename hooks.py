"""工具/回合钩子注册表（支持热插拔）。

钩子目录：`<STATE_ROOT>/.docmind/hooks/*.py`（可用 `DOCMIND_HOOKS_DIR` 覆盖）。
每个模块可用两种写法之一注册：

  # 写法 A：显式注册
  def register(api):
      api.pre_tool(my_pre)
      api.post_tool(my_post)

  # 写法 B：模块级同名函数（自动收集）
  def pre_tool(name, arg): ...
  def post_tool(name, arg, obs): ...
  def pre_turn(question): ...
  def post_turn(record): ...
  def before_node(payload): ...
  def after_node(payload): ...
  def before_wave(payload): ...
  def after_wave(payload): ...
  def before_approval(payload): ...
  def after_approval(payload): ...
  def recovery(payload): ...

钩子契约（返回值可为 None）：
  pre_tool(name, arg) -> {"block": bool, "reason": str} | {"arg": new_arg}
  post_tool(name, arg, obs) -> {"obs": new_obs}
  pre_turn(question) -> {"question": new_question} | {"block": bool, "reason": str}
  post_turn(record) -> 忽略返回值（用于埋点/通知）

**任何钩子异常都被吞掉并计入 errors，绝不影响主流程。** 目录不存在时全程静默 no-op。
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from config import STATE_ROOT, state_path

HOOKS_DIR = state_path("DOCMIND_HOOKS_DIR", os.path.join(STATE_ROOT, ".docmind", "hooks"))

_lock = threading.Lock()
#: 工作流生命周期钩子的**全集**。`run_workflow` 第一行就是
#: `if kind not in _WORKFLOW_KINDS: return …` —— **不在这个元组里的 kind 会被
#: 静默丢弃，连事件都不会发布**。
#:
#: `llm_wait_start` / `llm_wait_end` 曾因此完全不生效：它们不在这里，于是
#: `_publish_workflow_event` 从没被调用，事件从没到达工作流事件流，而
#: 开发舱时间线上的静默度仍是 99%。
#:
#: 那次为什么没测出来：进程内测试 spy 的是 `run_workflow` **本身**，看到它被
#: 调用就以为通了。**验"函数被调用"不是验"事件到了"** —— 前者是调用边界，
#: 后者才是用户看得到的那条路。
#:
#: 所以新增钩子名时，除了在调用侧发事件，**必须**把名字加进这个元组。
_WORKFLOW_KINDS = ("before_node", "after_node", "before_wave", "after_wave",
                   "before_approval", "after_approval", "recovery",
                   "before_tool", "after_tool", "before_mcp", "after_mcp",
                   "llm_wait_start", "llm_wait_end",
                   "mcp_retry", "network_timeout", "before_subagent", "after_subagent")
_hooks = {"pre_tool": [], "post_tool": [], "pre_turn": [], "post_turn": [],
          **{kind: [] for kind in _WORKFLOW_KINDS}}
_sources = {}     # kind -> [module_name]
_errors = []      # 加载/执行错误（供 /api/hooks 查看）
_loaded = False
_seq = 0
_breakpoints = {}
_WORKFLOW_EVENT_SINK: ContextVar = ContextVar("docmind_workflow_event_sink", default=None)


@contextmanager
def workflow_event_scope(sink):
    """Capture lifecycle hook outcomes for one Agent/Subagent execution.

    The sink receives ``(kind, payload, result)`` and is intentionally scoped
    with a ContextVar so concurrent subagents cannot mix their hook records.
    Hooks continue to receive the same bounded metadata as before.
    """
    token = _WORKFLOW_EVENT_SINK.set(sink if callable(sink) else None)
    try:
        yield
    finally:
        _WORKFLOW_EVENT_SINK.reset(token)


def _publish_workflow_event(kind, payload, result):
    sink = _WORKFLOW_EVENT_SINK.get()
    if not callable(sink):
        return
    try:
        sink(str(kind), dict(payload or {}), dict(result or {}))
    except Exception:
        # Observability must never alter the hook/tool control path.
        pass


def _breakpoints_path():
    return os.path.join(HOOKS_DIR, "breakpoints.json")


def _load_breakpoints():
    global _breakpoints
    try:
        with open(_breakpoints_path(), "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, dict):
            raise ValueError("breakpoints must be an object")
        _breakpoints = {
            str(kind): dict(value) for kind, value in raw.items()
            if kind in _WORKFLOW_KINDS and isinstance(value, dict)
        }
    except FileNotFoundError:
        _breakpoints = {}
    except (OSError, TypeError, ValueError) as exc:
        _breakpoints = {}
        _errors.append({"module": "breakpoints.json", "error": f"{type(exc).__name__}: {exc}"})


def _save_breakpoints():
    os.makedirs(HOOKS_DIR, exist_ok=True)
    path = _breakpoints_path()
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(_breakpoints, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _breakpoint_hit(kind, payload):
    config = dict(_breakpoints.get(kind) or {})
    if not config.get("enabled", True):
        return None
    match = str(config.get("match") or "").strip().lower()
    if match:
        haystack = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str).lower()
        if match not in haystack:
            return None
    return config


def set_breakpoint(kind, *, enabled=True, block=True, match="", reason=""):
    """Create or update one safe declarative workflow breakpoint."""
    kind = str(kind or "").strip()
    if kind not in _WORKFLOW_KINDS:
        raise ValueError("unsupported_hook_kind")
    config = {
        "enabled": bool(enabled), "block": bool(block),
        "match": str(match or "")[:200],
        "reason": str(reason or "用户断点：%s" % kind)[:300],
    }
    with _lock:
        _breakpoints[kind] = config
        _save_breakpoints()
    return {"kind": kind, **config}


def remove_breakpoint(kind):
    kind = str(kind or "").strip()
    with _lock:
        removed = _breakpoints.pop(kind, None) is not None
        _save_breakpoints()
    return {"kind": kind, "removed": removed}


def list_breakpoints():
    _ensure()
    return {kind: dict(value) for kind, value in sorted(_breakpoints.items())}


class _Api:
    """传给 register(api) 的注册门面。"""

    def __init__(self, module_name):
        self.module_name = module_name

    def _add(self, kind, fn):
        _hooks[kind].append(fn)
        _sources.setdefault(kind, []).append(self.module_name)

    def pre_tool(self, fn):
        self._add("pre_tool", fn)

    def post_tool(self, fn):
        self._add("post_tool", fn)

    def pre_turn(self, fn):
        self._add("pre_turn", fn)

    def post_turn(self, fn):
        self._add("post_turn", fn)

    def before_node(self, fn):
        self._add("before_node", fn)

    def after_node(self, fn):
        self._add("after_node", fn)

    def before_wave(self, fn):
        self._add("before_wave", fn)

    def after_wave(self, fn):
        self._add("after_wave", fn)

    def before_approval(self, fn):
        self._add("before_approval", fn)

    def after_approval(self, fn):
        self._add("after_approval", fn)

    def recovery(self, fn):
        self._add("recovery", fn)

    def before_tool(self, fn):
        self._add("before_tool", fn)

    def after_tool(self, fn):
        self._add("after_tool", fn)

    def before_mcp(self, fn):
        self._add("before_mcp", fn)

    def after_mcp(self, fn):
        self._add("after_mcp", fn)

    def mcp_retry(self, fn):
        self._add("mcp_retry", fn)

    def network_timeout(self, fn):
        self._add("network_timeout", fn)

    def before_subagent(self, fn):
        self._add("before_subagent", fn)

    def after_subagent(self, fn):
        self._add("after_subagent", fn)


def _reset():
    global _seq
    for k in _hooks:
        _hooks[k] = []
    _sources.clear()
    _errors.clear()
    _seq += 1


def reload(force=True):
    """重新扫描钩子目录（热插拔）。返回 {loaded, errors, kinds}。"""
    global _loaded
    with _lock:
        _reset()
        _load_breakpoints()
        if not os.path.isdir(HOOKS_DIR):
            _loaded = True
            return {"loaded": 0, "errors": [], "hooks_dir": HOOKS_DIR}
        count = 0
        for fname in sorted(os.listdir(HOOKS_DIR)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(HOOKS_DIR, fname)
            mod_name = f"docmind_hook_{_seq}_{os.path.splitext(fname)[0]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)          # type: ignore[union-attr]
                api = _Api(fname)
                reg = getattr(mod, "register", None)
                if callable(reg):
                    reg(api)
                for kind in _hooks:
                    fn = getattr(mod, kind, None)
                    if callable(fn):
                        api._add(kind, fn)
                count += 1
            except Exception as e:  # noqa: BLE001 —— 坏钩子只记录，不影响其它
                _errors.append({"module": fname, "error": f"{type(e).__name__}: {e}"})
        _loaded = True
        return {"loaded": count, "errors": list(_errors), "hooks_dir": HOOKS_DIR}


def _ensure():
    if not _loaded:
        reload()


def run_pre_tool(name, arg):
    """返回 (blocked, reason, arg)。"""
    _ensure()
    blocked, reason = False, ""
    for fn in list(_hooks["pre_tool"]):
        try:
            out = fn(name, arg)
        except Exception as e:  # noqa: BLE001
            _errors.append({"hook": "pre_tool", "error": f"{type(e).__name__}: {e}"})
            continue
        if isinstance(out, dict):
            if out.get("block"):
                blocked = True
                reason = str(out.get("reason") or "被钩子拦截")
            if out.get("arg") is not None:
                arg = out["arg"]
    return blocked, reason, arg


def run_post_tool(name, arg, obs):
    """返回可能被钩子改写的 obs。"""
    _ensure()
    for fn in list(_hooks["post_tool"]):
        try:
            out = fn(name, arg, obs)
        except Exception as e:  # noqa: BLE001
            _errors.append({"hook": "post_tool", "error": f"{type(e).__name__}: {e}"})
            continue
        if isinstance(out, dict) and out.get("obs") is not None:
            obs = out["obs"]
    return obs


def run_pre_turn(question):
    """返回 (blocked, reason, question)。"""
    _ensure()
    blocked, reason = False, ""
    for fn in list(_hooks["pre_turn"]):
        try:
            out = fn(question)
        except Exception as e:  # noqa: BLE001
            _errors.append({"hook": "pre_turn", "error": f"{type(e).__name__}: {e}"})
            continue
        if isinstance(out, dict):
            if out.get("block"):
                blocked = True
                reason = str(out.get("reason") or "被钩子拦截")
            if out.get("question") is not None:
                question = out["question"]
    return blocked, reason, question


def run_post_turn(record):
    _ensure()
    for fn in list(_hooks["post_turn"]):
        try:
            fn(record)
        except Exception as e:  # noqa: BLE001
            _errors.append({"hook": "post_turn", "error": f"{type(e).__name__}: {e}"})


def run_workflow(kind, payload):
    """Run a workflow lifecycle hook and return block/error metadata.

    Hook exceptions are reported to the caller instead of escaping.  The
    workflow policy decides whether those errors merely audit or block.
    """
    if kind not in _WORKFLOW_KINDS:
        return {"blocked": False, "reason": "", "errors": []}
    _ensure()
    breakpoint = _breakpoint_hit(kind, payload)
    if breakpoint and breakpoint.get("block", True):
        result = {"blocked": True,
                  "reason": str(breakpoint.get("reason") or "用户断点：%s" % kind),
                  "errors": [], "breakpoint": {"kind": kind, **breakpoint}}
        _publish_workflow_event(kind, payload, result)
        return result
    blocked, reason, errors = False, "", []
    for fn in list(_hooks[kind]):
        try:
            out = fn(dict(payload or {}))
        except Exception as e:  # noqa: BLE001
            error = {"hook": kind, "error": f"{type(e).__name__}: {e}"}
            _errors.append(error)
            errors.append(error)
            continue
        if isinstance(out, dict):
            if out.get("block"):
                blocked = True
                reason = str(out.get("reason") or "被工作流钩子拦截")
    result = {"blocked": blocked, "reason": reason, "errors": errors,
              "breakpoint": ({"kind": kind, **breakpoint} if breakpoint else None)}
    _publish_workflow_event(kind, payload, result)
    return result


def list_hooks():
    _ensure()
    legacy_kinds = ("pre_tool", "post_tool", "pre_turn", "post_turn")
    return {
        "hooks_dir": HOOKS_DIR,
        "exists": os.path.isdir(HOOKS_DIR),
        # Keep the original counts shape for existing clients; workflow
        # lifecycle hooks are exposed separately.
        "counts": {k: len(_hooks[k]) for k in legacy_kinds},
        "workflow_counts": {k: len(_hooks[k]) for k in _WORKFLOW_KINDS},
        "workflow_kinds": list(_WORKFLOW_KINDS),
        "breakpoints": list_breakpoints(),
        "sources": {k: list(v) for k, v in _sources.items()},
        "errors": list(_errors),
    }


def has_any():
    _ensure()
    return any(len(v) for v in _hooks.values())
