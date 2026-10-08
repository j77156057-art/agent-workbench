"""「检索即放行」：`tool_search` 查到用法后，该工具进入本轮有效集。

## 要解决的缺陷（实测，非推断）

`ContextRouter` 的召回正则只认**名词**（`_CODE` = `代码|函数|类|报错…`），
所以**动词型请求一个都不命中**：

| 请求 | 有效工具数 | 该在里面的工具 |
|---|---|---|
| 改一下 README 里的端口 | 26 / 116 | `read_file`/`apply_edit` 在（核心工具无条件放行） |
| **跑一下测试** | **26** | **`dev_testrun` 缺** |
| 装个依赖 | 26 | `dev_deps` 缺 |
| 这条能提交吗 | 26 | `dev_commit` 缺 |

那不是小漏：`dev_testrun` 的说明里明写着「别再 `run_command("pytest -q")`
自己读一整屏文本」，而现在恰好是它被收走 —— 机制与召回互相打脸。

## 链条实测：通，只断在最后一环

```
1) 索引里有 dev_testrun 那一行          True
2) 索引段头告知了 tool_search            True
3) tool_search 在有效集（可调）           True
4) tool_search 能取回 dev_testrun 用法    True
5) dev_testrun 本身在有效集（可调）      False   <- 断在这
```

所以要改的是 `_effective_tool_names()` 的**过滤规则**，不是检索机制。

## 判据是模型的动作，不是关键词

「跑一下测试」要匹配到「测试」才放开 `dev_testrun`，那是**猜** ——
写意图词表今晚已被证过一次：逐条消息、缺陷跨轮，必然无限 regress。

模型自己去 `tool_search("dev_testrun")` 是**说**。

## 检索不是授权

opencode codemode 的 `Authority Boundary` 一节把 discovery（工具发现）与
authorization（授权）分给不同主体，并明说：

> A program cannot gain authority through prose or generated code.

所以被搜出来的**写**工具只是「看得见」，能不能写仍走写意图判定
（`_intent_for_top_turn` + 租约）。本笔一个字没动那条护栏。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402


class _Stub:
    """够 `_native_enabled()` 与 `context_router` 用的最小 LLM。"""
    provider = "deepseek"
    model = "stub"
    base_url = ""

    def clone(self):
        return self

    def chat(self, messages, stream=True, **kw):
        return iter(["Final Answer: x"]) if stream else "x"

    def count_tokens(self, text):
        return 1


def _agent_for(question: str) -> agent_mod.Agent:
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    a.last_context_plan = a.context_router.route(question, code_root="D:/x")
    a.tool_search_expansions = set()
    agent_mod._TOOL_SEARCH_EXPANSIONS.set(a.tool_search_expansions)
    return a


# --------------------------------------------------------------------------
# 核心：检索即放行
# --------------------------------------------------------------------------

def test_a_searched_tool_becomes_callable():
    """**本笔的主张**：查了用法就能调。"""
    a = _agent_for("跑一下测试")
    assert "dev_testrun" not in set(a._effective_tool_names()), (
        "前置条件不成立：`dev_testrun` 本来就在有效集里 —— "
        "若召回已修好，本条失去意义，请改写它")
    agent_mod._tool_search("dev_testrun")
    assert "dev_testrun" in set(a._effective_tool_names()), (
        "检索了用法却仍然调不到 —— 这正是本笔要修的最后一环")


def test_the_gain_is_exactly_what_was_searched():
    """反向守卫：搜一个只多一个，不许顺带放开一串。

    这是「收窄还在」的具体形态。实测：检索前 26、检索 `dev_testrun` 后 27。
    """
    a = _agent_for("跑一下测试")
    before = set(a._effective_tool_names())
    agent_mod._tool_search("dev_testrun")
    after = set(a._effective_tool_names())
    gained = after - before
    assert gained == {"dev_testrun"}, (
        "放开的比检索的多：多出 %s —— 那说明过滤规则的例外写宽了"
        % sorted(gained - {"dev_testrun"}))


def test_narrowing_is_not_disabled():
    """反向守卫：收窄仍然生效。

    progressive 的意义是「模型用不到的工具不占上下文」。若检索一次就全放开，
    那个机制就没了 —— 所以即使搜到东西，未搜过的工具仍必须在有效集外。
    """
    a = _agent_for("跑一下测试")
    agent_mod._tool_search("dev_testrun")
    eff = set(a._effective_tool_names())
    assert len(eff) < len(tools_mod.TOOLS), (
        "有效集已达全量 %d —— 收窄失效" % len(eff))
    assert "dev_vue" not in eff, "`dev_vue` 没被检索过却进了有效集"


# --------------------------------------------------------------------------
# 滥用必须可见（无上限的已知代价）
# --------------------------------------------------------------------------

def test_mass_search_widens_a_lot_and_that_is_recorded():
    """**记录一个已知代价**：无上限意味着一次检索能放大到接近全量。

    实测：检索前 26；把 developer 组全部搜一遍 -> **97**（全量 116 的 84%）。

    本笔不设上限（用户明选），所以**放大必须可观测** ——
    `tool_search_expansions` 记下本轮放开了谁。
    """
    a = _agent_for("跑一下测试")
    before = len(set(a._effective_tool_names()))
    dev_names = [n for n, s in tools_mod.TOOLS.items() if s.group == "developer"]
    for name in dev_names:
        agent_mod._TOOL_SEARCH_EXPANSIONS.get().add(name)
    after = set(a._effective_tool_names())
    assert len(after) > before * 3, (
        "预期放大很明显（实测 26 -> 97），实测 %d -> %d" % (before, len(after)))
    assert a.tool_search_expansions == set(dev_names), (
        "放大没有被记录 —— 那它就是**无声退化**，下一个人不会知道收窄失效了")


# --------------------------------------------------------------------------
# 检索不是授权
# --------------------------------------------------------------------------

def test_searching_a_write_tool_does_not_authorize_writing():
    """**本笔最重要的一条边界**：检索是 discovery，不是授权。

    模型 `tool_search("apply_edit")` 之后 `apply_edit` 看得见、可调 ——
    但**能不能写**仍由写意图判定决定，检索动作不构成批准。

    这条对应 codemode 的「A program cannot gain authority through prose or
    generated code」，也对应 Pi 那句「watching the transcript does not create
    a security boundary」。
    """
    a = _agent_for("改一下 README 里的端口")
    agent_mod._tool_search("apply_edit")
    assert "apply_edit" in set(a._effective_tool_names()), (
        "前置不成立：`apply_edit` 本来就是核心工具")


def test_a_searched_write_tool_still_goes_through_write_intent():
    """行为级：搜出写工具后，**未授权的一轮仍被拦**。

    这是「检索 ≠ 授权」的**机制级**证据 —— 不是读注释，是真跑一遍护栏。

    样本选「这条能提交吗」：实测 `_write_intent` 判 **denied**
    （含「提交」但是否定式疑问），而「改一下 README 里的端口」判 granted ——
    我第一版用后者当样本，结果那条断言**恒真**，等于没验。
    """
    a = _agent_for("这条能提交吗")
    from agent import _WRITE_INTENT_DENIED, _WRITE_INTENT_GRANTED

    # 前置：这一轮确实未授权（否则下面那条断言没有意义）
    assert agent_mod._write_intent("这条能提交吗") == _WRITE_INTENT_DENIED, (
        "样本变了：这句话现在判成 %s —— 请换一条实测 denied 的句子"
        % agent_mod._write_intent("这条能提交吗"))
    agent_mod._tool_search("dev_commit")
    intent = a._intent_for_top_turn("这条能提交吗", "dev_commit")
    assert intent != _WRITE_INTENT_GRANTED, (
        "检索了 dev_commit 之后，未授权的一轮竟判成 granted —— "
        "那说明检索动作被当成了授权")


def test_the_sample_for_this_file_is_actually_denied():
    """**反向守卫**：样本本身要钉住。

    我第一版拿「改一下 README 里的端口」当未授权样本，而它实测判 **granted** ——
    断言因此恒真。**恒真守卫说明不了任何事**（K 节记的第 6、7 次同族）。
    """
    from agent import _WRITE_INTENT_DENIED

    for sample in ("这条能提交吗", "跑一下测试"):
        assert agent_mod._write_intent(sample) == _WRITE_INTENT_DENIED, (
            "%r 现在判成 %s —— 本文件用它们当「未授权」样本，"
            "判据变了会让相关断言恒真" % (sample, agent_mod._write_intent(sample)))


def test_a_granted_turn_after_searching_still_works():
    """反向守卫：检索没有把授权**堵死** —— 该放行的仍放行。

    防的是另一个方向：若实现时误把 `_intent_for_top_turn` 也一并拦住，
    那就是「安全但不可用」，同样是缺陷。
    """
    a = _agent_for("改一下 README 里的端口")
    agent_mod._tool_search("apply_edit")
    from agent import _WRITE_INTENT_GRANTED

    granted = a._intent_for_top_turn("apply_edit", "改吧，把端口改掉")
    assert granted == _WRITE_INTENT_GRANTED, (
        "检索之后连明确授权的一轮都拦住了 —— 检索把授权路径堵死了")


# --------------------------------------------------------------------------
# 每轮重置
# --------------------------------------------------------------------------

def test_expansions_reset_every_turn():
    """反向守卫：检索得到的可见性**不跨轮**。

    不重置的话，上一轮搜过的工具会永久留在有效集里，
    多轮下来 progressive 就被逐步绕光 —— 而那正是它要防的。
    """
    a = _agent_for("跑一下测试")
    agent_mod._tool_search("dev_testrun")
    assert a.tool_search_expansions == {"dev_testrun"}
    # 下一轮：`_run` 开头会重置
    a.tool_search_expansions = set()
    agent_mod._TOOL_SEARCH_EXPANSIONS.set(a.tool_search_expansions)
    assert "dev_testrun" not in set(a._effective_tool_names()), (
        "重置后仍在有效集里")


def test_the_run_loop_actually_resets_it():
    """**正向守卫**：`_run` 里真的有那行重置。

    上面那条是「重置了会怎样」，这条是「到底有没有重置」——
    断言的是代码里的机制，不是它的后果。
    """
    import inspect

    src = inspect.getsource(agent_mod.Agent._run)
    assert "tool_search_expansions = set()" in src, (
        "_run 里没有重置 tool_search_expansions —— 检索得到的可见性会跨轮累积，"
        "progressive 会被逐步绕光")
    assert "_TOOL_SEARCH_EXPANSIONS.set(" in src, (
        "_run 里没有把 ContextVar 指到这个 agent 的集合 —— "
        "_tool_search 命中写不进这个 agent")


# --------------------------------------------------------------------------
# 不碰判据
# --------------------------------------------------------------------------

def test_the_recall_regexes_are_untouched():
    """反向守卫：本笔**不改**召回正则。

    动它们就是「往词表里加词」—— 今晚在写意图那族已被证过一次：
    逐条消息、缺陷跨轮，必然无限 regress。本笔的判据是模型的动作。
    """
    from agent_runtime import context_router as C

    before = C._CODE.pattern
    assert "跑" not in before and "测试" not in before, (
        "_CODE 里已经加了动词词 —— 若这是有意的，请重新评估"
        "它对其它查询的影响（那是另一族，不在本笔范围）")