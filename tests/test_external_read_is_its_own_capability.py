"""越界读必须与「读项目内」分开记账。

## 这一族

`Agent._lease_blocked`（`agent.py`）是**按 capability 字面值**比对租约的：

    capability = spec.capability.value
    if capability not in lease["capabilities"]: 拦

而 `read_external_file` 一直没声明 capability，于是 `coerce_tool_spec` 按默认值
把它判成 `read_local`。后果是实测出来的，不是推断：

    租约只含 read_local -> read_external_file   拦截=False

也就是**只租用了「读项目内」的工作流，照样能把项目外的文件读出去**。租约本来
就是给工作流划权限边界的工具，而「项目外」和「项目内」在权限上是两件事。

## 为什么要分这一档

`Capability.READ_EXTERNAL` 早就定义了、导出着、**一个工具都没落进去**；前端
`AutonomousCockpit.vue` 也早备好了标签「读取外部资料」。也就是说这一档是预留
好的，只是没人用。

## 守卫的分工

- 归属：`read_external_file` 必须在 `read_external` 档，且不在 `read_local` 档
- 反向：`read_file` 等项目内读**不得**被一起改掉
- 反向：默认租约（授全部 Capability）**不得**因此拦下越界读
- 反向：前端标签表必须有这一档的中文标签（它会真的显示给用户）
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOOLS = ROOT / "tools.py"
VUE = ROOT / "frontend" / "src" / "workbench" / "components" / "AutonomousCockpit.vue"


def _spec(name: str):
    import tools as tools_mod
    from agent_runtime.tools import coerce_tool_spec
    return coerce_tool_spec(name, tools_mod.TOOLS[name])


def _raw_entry(name: str) -> dict:
    """取注册项里**声明了什么** —— 冻结后的 ToolSpec 已经看不出声明了。

    `tools.py` 末尾的 `upgrade_registry(TOOLS)` 就地把原始 dict 换成 ToolSpec，
    所以直接读 `tools.TOOLS` 分不清「表给的」和「注册声明的」。
    """
    src = TOOLS.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(src) if ln.startswith("TOOLS = {"))
    end = next(i for i, ln in enumerate(src[start:], start)
               if ln.startswith("upgrade_registry("))
    ns: dict = {}
    import tools as tools_mod
    exec(compile("\n".join(src[start:end]), "tools.py:TOOLS", "exec"),
         vars(tools_mod), ns)
    return ns["TOOLS"][name]


def _agent(lease_caps: list[str]):
    import agent as agent_mod
    a = agent_mod.Agent(llm=None)
    a.capability_lease = {
        "status": "active",
        "expires_at_epoch": time.time() + 600,
        "capabilities": lease_caps,
    }
    return a


# --------------------------------------------------------------------------
# 归属
# --------------------------------------------------------------------------


def test_reading_outside_the_project_is_its_own_capability():
    spec = _spec("read_external_file")
    assert spec.capability.value == "read_external", (
        f"越界读被判成 {spec.capability.value} —— "
        "只租用了「读项目内」的工作流会因此能读项目外的文件"
    )


def test_it_is_still_a_pure_parallel_safe_read():
    """反向守卫：改档位不等于改性质。它仍然是纯读、仍然可以并发。"""
    spec = _spec("read_external_file")
    assert spec.side_effect.value == "pure", spec.side_effect.value
    assert spec.parallel_safe is True, "纯读工具被判成不可并发，批内白白串行"


def test_the_registration_declares_it_rather_than_inheriting_the_default():
    """反向守卫：删掉声明就退回 read_local，而 `_spec` 那条会红。

    所以这条钉的是「靠声明」，保证下一个人不会以为是默认值的功劳。
    """
    entry = _raw_entry("read_external_file")
    assert entry.get("capability") == "read_external", (
        "read_external_file 没有显式声明 capability，"
        f"实际用的是默认值：{entry.get('capability')!r}"
    )


# --------------------------------------------------------------------------
# 行为：租约
# --------------------------------------------------------------------------


def test_a_lease_scoped_to_the_project_cannot_read_outside_it():
    """**核心守卫**：只租用 read_local 的工作流不得越界读。"""
    a = _agent(["read_local"])
    blocked, reason = a._lease_blocked("read_external_file")
    assert blocked, "只租用了 read_local，却能读项目外的文件——租约形同虚设"
    assert "read_external" in str(reason), reason


def test_but_reading_inside_the_project_still_works_under_that_lease():
    """反向守卫：别把它改成连项目内都读不了。"""
    a = _agent(["read_local"])
    blocked, _ = a._lease_blocked("read_file")
    assert not blocked, "只读租约下项目内读也被拦了——改过头了"


def test_the_default_lease_still_allows_reading_outside():
    """反向守卫：默认租约授**全部** Capability，所以正常路径不能被误伤。

    这一条是「是不是我改过头了」的判据：真被误伤了它就会红。
    """
    from agent_runtime.tools import Capability
    a = _agent([c.value for c in Capability])
    blocked, reason = a._lease_blocked("read_external_file")
    assert not blocked, f"默认租约（授全部能力）竟被拦下：{reason}"


def test_renting_read_external_alone_does_not_buy_writing():
    """反向守卫：档位是分开记的，租 read_external 不该顺带拿到写。"""
    a = _agent(["read_external"])
    blocked, _ = a._lease_blocked("create_external_file")
    assert blocked, "只租了读，却能越界写"


# --------------------------------------------------------------------------
# 反向守卫：别顺手把项目内的读也改了
# --------------------------------------------------------------------------


def test_project_local_readers_are_untouched():
    """`read_file` / `grep` / `list_dir` 仍然是 read_local。

    它们没声明 capability，靠默认值落到 read_local —— 那是对的，不该动。
    """
    import tools as tools_mod
    for name in ("read_file", "grep", "list_dir", "search_code"):
        if name not in tools_mod.TOOLS:
            continue
        assert _spec(name).capability.value == "read_local", (
            f"{name} 不该被改成别的档位"
        )


# --------------------------------------------------------------------------
# 前端：这一档会真的显示给用户
# --------------------------------------------------------------------------


def test_the_ui_has_a_label_for_this_capability():
    """前端按 capability 显示档位；标签表早就有这一档，别把它删了。"""
    src = VUE.read_text(encoding="utf-8")
    m = re.search(r"read_external\s*:\s*'([^']+)'", src)
    assert m, "前端 capability 标签表里没有 read_external，界面会显示原始英文"
    assert not m.group(1).isascii(), (
        f"read_external 的标签仍是英文：{m.group(1)!r}")