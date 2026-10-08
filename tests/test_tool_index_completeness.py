"""扩展工具索引必须**明示完整度**，且那个数字必须自洽。

## 抄的是哪句

opencode codemode 的 `Discovery` 一节：

> The instructions state exactly **how comprehensive the list is**, both overall
> (`COMPLETE list` vs `PARTIAL - N of M shown`)

我们此前的索引段头只说「以下工具只给名称与概要」—— 没说有多少、也没说
本轮实际能调几个。**那是「失败时没有声音」**：模型看到某个工具的名字，
调不动时没有任何线索解释为什么。

## 抄它时才发现的另一半（更值钱）

同一节还说：

> **Every tool namespace is always listed** with its tool count regardless of
> budget. Selection is **round-robin** across namespaces for fairness.

也就是说 opencode **不按问题相关性挑工具**。我们此前的 `_CODE`/`_WEB`
关键词判定是三个 harness 里唯一的异类，也是唯一失效的那个 ——
详见 `docs/known-defect-context-router-recall.md`。

## 我们比 opencode 多一坑

它只有「目录截断」一层；我们还有 `_effective_tool_names()` 的**运行期按组收窄**。
实测「跑一下测试」那轮：提示里 115 个工具，实际只有 **26** 个可调。

所以明示必须说清**两件事**：一共多少、且本轮可调的比这少。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod  # noqa: E402

_HEAD = "【扩展工具索引】"


def _index_header() -> str:
    text = agent_mod.SYSTEM_PROMPT
    i = text.find(_HEAD)
    assert i >= 0, "系统提示里没有【扩展工具索引】段 —— progressive 模式没生效？"
    return text[i:i + 700]


def _counts() -> tuple[int, int, int]:
    blocks = agent_mod._tool_blocks()
    core = [n for n in blocks if n in agent_mod.CORE_TOOL_NAMES]
    return len(core), len(blocks) - len(core), len(blocks)


# --------------------------------------------------------------------------
# 明示存在，且数字自洽
# --------------------------------------------------------------------------

def test_the_index_header_states_how_many_tools_there_are():
    """**正向守卫**：那个「共 N 个」必须在段头里。"""
    head = _index_header()
    assert re.search(r"共\s*\d+\s*个工具", head), (
        "索引段头没有说明一共有多少工具：%r" % head[:120])


def test_the_stated_total_matches_the_actual_lists():
    """**反向守卫**：段头里的数字必须与实际列表一致。

    这条是我第一版写的 bug 的守卫 —— 我让分母取 `len(tools.TOOLS)`，
    而系统提示在**import 期**构建，那时 `agent.py` 还没注入
    `tool_search`/`delegate`/`orchestrate`/`dev_use_skill` 这 4 个，
    于是报出「共 112 个，以下 115 个」—— **自相矛盾的话**。

    与 22 个工具那次同一个根因：**算数量要交叉核对两个来源**，
    而这里只有一个来源是当下正确的（提示段本身）。
    """
    core_n, index_n, total = _counts()
    head = _index_header()
    m = re.search(r"共\s*(\d+)\s*个工具", head)
    assert m, "段头里没有「共 N 个工具」"
    assert int(m.group(1)) == total, (
        "段头说共 %s 个，实际核心 %d + 索引 %d = %d —— "
        "**自相矛盾**，模型会照着错的数字规划"
        % (m.group(1), core_n, index_n, total))


def test_it_also_states_the_two_sub_counts():
    """反向守卫：核心数与索引数要分别说清，否则「共 115」无从核对。"""
    core_n, index_n, total = _counts()
    head = _index_header()
    assert re.search(r"%d\s*个核心工具" % core_n, head), (
        "段头没说要给完整用法的核心工具数（应为 %d）" % core_n)
    assert re.search(r"以下\s*%d\s*个" % index_n, head), (
        "段头没说要给概要的索引工具数（应为 %d）" % index_n)
    assert core_n + index_n == total


def test_no_claim_of_completeness():
    """反向守卫：索引是**不完整**的，不许说成完整。

    opencode 用 `COMPLETE list` vs `PARTIAL - N of M shown` 区分；
    我们永远是不完整的那档（102 个只有概要）。
    """
    head = _index_header()
    assert "COMPLETE" not in head and "完整列表" not in head, (
        "索引给了概要而非完整用法，却自称完整 —— 那会让模型不再去 tool_search")


# --------------------------------------------------------------------------
# 明示「本轮可调的更少」—— 我们比 opencode 多的一层
# --------------------------------------------------------------------------

def test_it_says_the_runtime_narrows_further():
    """**本笔比抄 opencode 多写的部分**。

    我们还有 `_effective_tool_names()` 的运行期按组收窄，而 opencode 只有
    目录截断。实测「跑一下测试」那轮：提示 115 个，实际 26 个可调。

    不说这一句，模型看到工具却调不动时**没有任何线索**。
    """
    head = _index_header()
    assert "本轮实际可调的通常比这少" in head, (
        "段头没有说明运行期还会进一步收窄 —— 模型会以为列出来的都能调")
    assert "tool_search" in head, "必须给出调不动时的下一步动作"


def test_the_narrowing_claim_is_actually_true():
    """**反向守卫**：那句话本身要属实。

    断言的是**机制**（收窄确实发生且数量确实更少），不是它的后果 ——
    如果哪天取消收窄，这条会红，而那时那句话就该删掉。
    """
    class _Stub:
        provider = "deepseek"
        model = "stub"
        base_url = ""

        def clone(self):
            return self

        def chat(self, messages=None, stream=True, **kw):
            return iter(["x"])

        def count_tokens(self, text):
            return 1

    import tools as tools_mod

    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    a.last_context_plan = a.context_router.route("跑一下测试", code_root="D:/x")
    effective = len(a._effective_tool_names())
    _, _, listed = _counts()
    assert effective < listed, (
        "实测本轮可调 %d 个，不少于列出的 %d 个 —— "
        "段头里「本轮实际可调的通常比这少」就不成立了，应删掉那句"
        % (effective, listed))


# --------------------------------------------------------------------------
# 机制本身：生成逻辑
# --------------------------------------------------------------------------

def test_the_counts_come_from_the_lists_not_from_the_registry():
    """反向守卫：分母不许取注册表。

    取注册表会拿到 **import 期快照**（112）——系统提示在那时构建，
    `agent.py` 注入的 4 个工具还不在。实测我第一版就是这么写的，
    报出「共 112 个，以下 115 个」。
    """
    blocks = agent_mod._tool_blocks()
    core = [n for n in blocks if n in agent_mod.CORE_TOOL_NAMES]
    # 生成函数里的分母就是 core + index
    rebuilt = agent_mod._progressive_tools_text(
        "\n".join(blocks[n] for n in sorted(blocks)))
    m = re.search(r"共\s*(\d+)\s*个工具", rebuilt)
    assert m and int(m.group(1)) == len(core) + (len(blocks) - len(core)), (
        "生成时的分母与列表不一致：说了 %s，实际 %d"
        % (m.group(1) if m else "?", len(blocks)))


def test_full_mode_has_no_index_section():
    """反向守卫：`full` 模式不该冒出 progressive 的段头。"""
    saved = agent_mod._TOOL_EXPANSION
    agent_mod._TOOL_EXPANSION = "full"
    try:
        out = agent_mod._assemble_system_prompt(agent_mod._SYSTEM_PROMPT_FULL)
    finally:
        agent_mod._TOOL_EXPANSION = saved
    assert _HEAD not in out, "full 模式不该有【扩展工具索引】"