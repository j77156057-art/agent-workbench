# -*- coding: utf-8 -*-
"""Agent 主循环相关方法的源码拼接（源码钉桩型测试统一从这里取）。

拆分 `_run` 之后，主循环分布在多个方法里（`_run` + 若干阶段方法）。
这些测试守的是「这段逻辑还在、还是不是那个形状」，而不是「它住在哪个方法里」
—— 所以统一从这里取源码，钉桩的**牙不变**（缺了照样红）。

还有一层：拆完之后这些共享状态挂在了 `_TurnState` 载体上（`st.audit_steps`），
而钉桩写的是拆分前的形状（`audit_steps`）。所以这里按**载体的字段表**把
`st.<字段>` 还原成裸名字 —— 只还原载体自己的字段，不碰其它 `st.`。
这样钉桩继续对着「逻辑形状」，而不是对着载体接线。

**必须动态取 agent 模块**（不能在本模块 import 时缓存）：变异电池会把改过的
源码装进 sys.modules["agent"] 再跑用例；这里若持有旧引用，读到的就是**磁盘上
未被变异的原文** → 只靠源码形状检测的变异会「活下来」（守卫看起来没牙，
其实是取错了源）。
"""
import ast
import inspect
import re
import sys

from agent_runtime.run_state import _ChildState, _ShellState
from agent_runtime.turn_state import _TurnState

LOOP_METHODS = (
    "_run",
    "_init_turn_state",
    "_run_round_prepare",
    "_run_round_ask",
    "_run_round_parse",
    "_run_round_decide",
    "_run_round_finalize",
    "_run_round_finalize_guards",
    "_run_tool_action",
    "_tool_guard",
    "_tool_execute",
    "_tool_observe",
    "_tool_failure",
    "_tool_wrap",
    "_handle_format_stall",
    "_handle_nudge",
    "_handle_final_fallback",
    "_restore_from_journal",
)

_CARRIER_FIELDS = sorted(_TurnState.__dataclass_fields__)
_SHELL_FIELDS = sorted(_ShellState.__dataclass_fields__)
_CHILD_FIELDS = sorted(_ChildState.__dataclass_fields__)
_STRIP = [re.compile(r"\bst\.%s\b" % re.escape(name)) for name in _CARRIER_FIELDS]
_STRIP += [re.compile(r"\bsh\.%s\b" % re.escape(name)) for name in _SHELL_FIELDS]
_STRIP += [re.compile(r"\bcs\.%s\b" % re.escape(name)) for name in _CHILD_FIELDS]

#: 子代理这一层的几个方法（拆开后的家）。守卫按**合成视图**查它们。
CHILD_METHODS = (
    "_run_child",
    "_child_prepare",
    "_child_pump",
    "_child_build_output",
)


def _agent_module():
    """当前生效的 agent 模块（变异电池会把它换掉，所以每次都现取）。"""
    module = sys.modules.get("agent")
    if module is not None:
        return module
    import agent as _fallback
    return _fallback


def _normalize(src: str) -> str:
    """把载体前缀 `st.<字段>` 还原成裸名字。"""
    for pattern in _STRIP:
        src = pattern.sub(lambda m: m.group(0).split(".", 1)[1], src)
    return src


def agent_module_source() -> str:
    """agent 模块的源码（载体前缀已还原）。

    有些守卫钉的是**整个模块**的形状（而不是某个方法）—— 用这个。
    """
    return _normalize(inspect.getsource(_agent_module()))


def agent_method_source(name: str) -> str:
    """单个方法的源码（同样还原载体前缀）。

    需要精确定位某一段逻辑时用它 —— 拼接视图里同名片段可能出现多次。
    """
    fn = getattr(_agent_module().Agent, name, None)
    if fn is None:
        raise AttributeError("Agent 没有方法 %r" % name)
    return _normalize(inspect.getsource(fn))


def agent_loop_source(*extra: str) -> str:
    """主循环（含 `extra` 指定的额外方法）的源码文本，载体前缀已还原。"""
    agent = _agent_module()
    names = list(extra) + [n for n in LOOP_METHODS if n not in extra]
    parts = []
    for name in names:
        fn = getattr(agent.Agent, name, None)
        if fn is None:
            continue
        parts.append(inspect.getsource(fn))
    return _normalize(chr(10).join(parts))


#: `api.py` 里 chat 这一层拆出来的方法（拆分前是一个 656 行的 `chat`）。
CHAT_METHODS = (
    "_chat_normalize",
    "_chat_guard_input",
    "_chat_prepare_vision",
    "_chat_resolve_provider",
    "_chat_resolve_context",
    "_chat_resolve_overrides",
    "_chat_open_context",
    "_chat_event_stream",
    "_chat_stream_prepare",
    "_chat_stream_open",
    "_chat_worker_produce",
    "_chat_stream_pump",
    "_chat_stream_fail",
    "_chat_stream_finish",
)

#: SSE 那一层。顺序**有讲究**：`_chat_event_stream` 必须排第一（它含
#: `except BaseException as e:`），`_chat_stream_fail` 紧随其后（守卫要在这两段
#: 之间找 `worker_stop.is_set()` / `ClientDisconnected` 的窗口）。
CHAT_STREAM_METHODS = (
    "_chat_event_stream",
    "_chat_stream_fail",
    "_chat_stream_pump",
    "_chat_stream_prepare",
    "_chat_stream_open",
    "_chat_worker_produce",
    "_chat_stream_finish",
)


def agent_chat_source(*names: str) -> str:
    """`api.py` 里 chat 那一层的源码拼接。

    拆分 `api.py:chat` 之后，provider 判定与事件流分家了；守卫不该因此失明 ——
    这里的拼接视图让它们按**逻辑形状**检查，而不是「住在哪个函数里」。
    不传名字时给整个 chat 层。
    """
    import api  # 局部导入：变异电池可能替换 sys.modules 里的模块

    wanted = tuple(names) if names else CHAT_METHODS
    parts = []
    for name in wanted:
        fn = getattr(api, name, None)
        if fn is None:
            continue
        parts.append(inspect.getsource(fn))
    return "\n".join(parts)


def agent_chat_stream_source() -> str:
    """SSE 那一层的源码拼接（顺序见 `CHAT_STREAM_METHODS`）。"""
    return agent_chat_source(*CHAT_STREAM_METHODS)



def _carrier_prefixes():
    """载体前缀 → 它的字段表（cs / sh）。"""
    return {"cs": set(_CHILD_FIELDS), "sh": set(_SHELL_FIELDS)}


def _strip_carrier_fields(node):
    """AST 级别把 cs.<字段> / sh.<字段> 还原成裸名字。

    守卫查的是「这段逻辑还在、还是不是那个形状」，而不是「它住在哪个方法里」
    —— 所以拆开之后要把载体前缀摘掉，钉桩的**牙不变**。
    """
    prefixes = _carrier_prefixes()

    def carrier_of(value):
        """这个 Name 是载体前缀吗？是的话返回它的字段表。"""
        if isinstance(value, ast.Name) and value.id in prefixes:
            return prefixes[value.id]
        return None

    def plain(item):
        """载体字段访问 → 裸名字；不是载体字段就原样返回。"""
        fields = carrier_of(item.value) if isinstance(item, ast.Attribute) else None
        if fields is not None and item.attr in fields:
            return ast.Name(id=item.attr, ctx=ast.Load())
        return item

    for n in ast.walk(node):
        for field, value in list(ast.iter_fields(n)):
            if isinstance(value, ast.Attribute):
                setattr(n, field, plain(value))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.Attribute):
                        value[i] = plain(item)
    return node


def child_composite_ast(tree):
    """子代理相关的几个方法合成一个「函数体」，供守卫按老形状检查。

    拆分 _run_child 之后，造子代理的逻辑分家了（准备 / 事件泵 / 出报告）。
    守卫不该因此失明 —— 这里的合成视图把各方法的**函数体**按顺序拼成一个
    函数，载体前缀也一并还原，于是老守卫的形状检查原样可用。
    """
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            funcs.setdefault(node.name, node)
    body = []
    for name in CHILD_METHODS:
        fdef = funcs.get(name)
        if fdef is None:
            continue
        body.extend(fdef.body)
    if not body:
        raise AssertionError(
            "子代理方法一个都没找到：%s" % (", ".join(CHILD_METHODS),))
    comp = ast.parse("def _child_composite():\n    pass\n").body[0]
    comp.body = body
    return _strip_carrier_fields(comp)
