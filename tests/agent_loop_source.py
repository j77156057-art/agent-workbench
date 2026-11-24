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
import inspect
import re
import sys

from agent_runtime.turn_state import _TurnState

LOOP_METHODS = (
    "_run",
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
_STRIP = [re.compile(r"\bst\.%s\b" % re.escape(name)) for name in _CARRIER_FIELDS]


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
