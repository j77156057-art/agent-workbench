"""`tool_search` 只做「按需读长用法」，**不再**影响工具暴露。

## 这个文件为什么被重写（2026-10-08）

它原本守的是「检索即放行」——`0fd31d2` 让本轮被 `tool_search` 取回过用法的
工具进入有效集，为的是修「模型查得到用法、却调不到」。

**而 `e8db3ea` 撤除关键词收窄之后，那条例外彻底不生效。** 实测
**8 种场景（4 类请求 × 联网开/关）全部零变化**：

```
跑一下测试 / 你好 / 改一下README / 查下高铁票   ×  web=False/True
联网关 109 -> 109    联网开 116 -> 116    变化集合恒为空
```

子代理白名单场景（`tool_allowlist=['tool_search','read_file']`）同样零变化。

**而它想做的事在白名单场景下恰恰是越权** —— `names` 从白名单取，
检索把白名单外的工具捞回来 = 子代理靠检索扩权。codemode 的 `Authority Boundary`
也明说 discovery 与 authorization 分属不同主体。

所以那条例外与配套的 `_TOOL_SEARCH_EXPANSIONS` / `tool_search_expansions`
**一并删除**。留着一条从不生效、且有已知代价（无上限，实测一次检索能把
developer 组 84% 捞回来）的机制，比删掉更糟 —— 下一个人会以为它在起作用。

## `tool_search` 本身保留

它的价值没有随那条例外消失：系统提示里 `dev_vue` 的说明有 **1167 字符**、
`dev_page_action` 1046 —— 模型不必一开始就背，需要时取回完整用法即可。
那是 opencode codemode `Discovery`「预算截断 + 检索」里的**检索那一半**。

## 保留的两条边界守卫

原先 11 条里，只有这两条讲的是**检索本身的性质**（与收窄无关），所以留下：

* 检索**不是授权** —— 搜出写工具不改变写意图判定
* `errorText` 之类实质内容不得被匹配逻辑吞掉
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
    provider = "deepseek"
    model = "stub"
    base_url = ""

    def clone(self):
        return self

    def chat(self, messages=None, stream=True, **kw):
        return iter(["x"])

    def count_tokens(self, text):
        return 1


# --------------------------------------------------------------------------
# 机制已删除：钉住这个事实
# --------------------------------------------------------------------------

def test_the_expansion_mechanism_is_gone():
    """**反向守卫**：那套「命中即放行」的机制已被**删除**，不许复活。

    它在 `e8db3ea` 撤除关键词收窄之后彻底不生效（实测 8 种场景零变化），
    且在 `tool_allowlist` 场景下是越权。谁要恢复它，先读
    `docs/known-defect-context-router-recall.md` §11。
    """
    import inspect

    src = inspect.getsource(agent_mod.Agent._effective_tool_names)
    assert "tool_search_expansions" not in src, (
        "`_effective_tool_names()` 又出现 tool_search_expansions —— "
        "「检索即放行」已被删除且彻底不生效，复活它只会让人误以为它在起作用")
    # ⚠ 判据必须是**实例**上有没有这个属性，不是 `hasattr(Agent, ...)`。
    # 第一版写的是后者，而那是对**类**的检查 —— 实例属性不构成类属性，
    # 所以哪怕 `__init__` 里又加回了 `self.tool_search_expansions = set()`，
    # 这条断言仍然恒真。变异实测就是这么漏过去的。
    probe = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    assert not hasattr(probe, "tool_search_expansions"), (
        "Agent 实例上又有 tool_search_expansions 属性 —— 配套状态没删干净")
    assert not hasattr(agent_mod, "_TOOL_SEARCH_EXPANSIONS"), (
        "模块级 `_TOOL_SEARCH_EXPANSIONS` 仍在 —— 收集了却没人读，纯负担")


def test_tool_search_still_returns_the_real_usage():
    """**正向守卫**：删除那条例外**没有**弄坏 `tool_search` 本身。

    它仍要能取回完整用法 —— 那才是它现在唯一的职责
    （`dev_vue` 的说明 1167 字符，模型不该一开始就背）。
    """
    out = str(agent_mod._tool_search("dev_vue"))
    assert "dev_vue" in out, out[:120]
    assert len(out) > 200, (
        "返回内容太短（%d 字符）—— 取回的是概要而不是完整用法" % len(out))


def test_tool_search_accepts_keyed_and_json_arguments():
    """**实测事故的回归守卫**：keyed / JSON 入参必须能命中。

    工作台里模型按其他工具的约定写成 `tool_search(query: browser_action)`，
    旧实现把「query: browser_action」整串当关键词，回「没有匹配」——
    模型据此断言 `browser_action` 不存在并放弃整轮任务。参数归一化不是宽容，
    是可用性：检索失败会被模型当作「能力不存在」的证据。
    """
    for arg in ("query: browser_action", "tool: browser_action",
                "name: browser_action", '{"query": "browser_action"}',
                "  browser_action  "):
        out = str(agent_mod._tool_search(arg))
        assert out.startswith("- browser_action("), (arg, out[:160])


def test_searching_changes_nothing_about_exposure():
    """**记录一个实测事实**：检索对工具暴露**零影响**。

    这条不是「应该零影响」的期望，而是**现状的如实记录** ——
    撤除收窄后那条例外已删，所以检索确实不再改变有效集。
    将来若有人重新引入某种「检索影响暴露」的设计，这条会红并要求改写。
    """
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    a.last_context_plan = a.context_router.route(
        "跑一下测试", code_root="D:/x", web_enabled=False)
    before = set(a._effective_tool_names())
    agent_mod._tool_search("dev_testrun")
    after = set(a._effective_tool_names())
    assert before == after, (
        "检索改变了工具暴露：多出 %s —— 那条例外已被删除，"
        "要么它复活了，要么本文件该改写" % sorted(after - before))


def test_searching_cannot_widen_a_subagent_allowlist():
    """**反向守卫**：检索**不得**把子代理白名单外的工具捞进来。

    这是删除那条例外的**主要理由** —— 它在白名单场景下是越权。
    """
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS),
                        tool_allowlist=["tool_search", "read_file"])
    a.last_context_plan = a.context_router.route(
        "跑一下测试", code_root="D:/x", web_enabled=False)
    agent_mod._tool_search("dev_testrun")
    effective = set(a._effective_tool_names())
    assert "dev_testrun" not in effective, (
        "子代理靠 tool_search 把 `dev_testrun` 扩进了白名单 —— 那是越权")
    assert effective <= {"tool_search", "read_file"}, sorted(effective)


# --------------------------------------------------------------------------
# 保留的边界：检索不是授权
# --------------------------------------------------------------------------

def test_the_sample_for_this_file_is_actually_denied():
    """**反向守卫**：样本本身要钉住。

    我第一版拿「改一下 README 里的端口」当未授权样本，而它实测判 **granted** ——
    断言因此恒真。**恒真守卫说明不了任何事。**
    """
    from agent import _WRITE_INTENT_DENIED

    for sample in ("这条能提交吗", "跑一下测试"):
        assert agent_mod._write_intent(sample) == _WRITE_INTENT_DENIED, (
            "%r 现在判成 %s —— 本文件用它们当「未授权」样本，"
            "判据变了会让相关断言恒真" % (sample, agent_mod._write_intent(sample)))


def test_the_top_turn_gate_is_removed_and_always_granted():
    """「留地板、拆其余」：顶层聊天回合的写意图护栏已拆，恒放行。

    纯函数 `_write_intent` 仍把「这条能提交吗」判 denied（分类器没动），但
    `_intent_for_top_turn` 不再据此拦写——灾难地板改由 tools.py 的路径/凭据/
    系统目录硬拦兜底，不在这条链上。
    """
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    from agent import _WRITE_INTENT_DENIED, _WRITE_INTENT_GRANTED

    sample = "这条能提交吗"
    assert agent_mod._write_intent(sample) == _WRITE_INTENT_DENIED, (
        "纯函数分类器仍应判 denied（它没被改动）")
    agent_mod._tool_search("dev_commit")
    assert a._intent_for_top_turn(sample, "dev_commit") == _WRITE_INTENT_GRANTED, (
        "护栏已拆：顶层回合恒放行")
    assert a._intent_for_top_turn("请修改 a.txt", "dev_commit") == _WRITE_INTENT_GRANTED


def test_a_granted_turn_after_searching_still_works():
    """反向守卫：检索没有把授权**堵死** —— 该放行的仍放行。

    防的是另一个方向：若实现时误把写意图判定也一并拦住，
    那就是「安全但不可用」，同样是缺陷。

    ⚠ **样本必须是实测 granted 的。** 我第一版用「改吧，把端口改掉」，
    它实测判 **denied** —— 而那**不是检索造成的**，是已记录的
    **谓语动词缺口**（裸「改」不在词表，`6069386` 定的限制，
    见 `docs/write-intent-corpus.md` B 节）。换了实测 granted 的句子。
    """
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    from agent import _WRITE_INTENT_GRANTED

    sample = "改一下端口"
    assert agent_mod._write_intent(sample) == _WRITE_INTENT_GRANTED, (
        "样本变了：%r 现在判 %s —— 请换一条实测 granted 的句子"
        % (sample, agent_mod._write_intent(sample)))
    agent_mod._tool_search("apply_edit")
    assert a._intent_for_top_turn(sample, "apply_edit") == _WRITE_INTENT_GRANTED, (
        "检索之后连明确授权的一轮都拦住了 —— 检索把授权路径堵死了")


# --------------------------------------------------------------------------
# 匹配本身的质量
# --------------------------------------------------------------------------

def test_an_exact_name_returns_itself_first():
    """**正向守卫**：精确名命中时，第一个结果必须是它自己。

    子串匹配的固有毛病：精确名在名字里匹配不上时会退到 description 匹配，
    而说明里常提到别的工具名 —— 于是返回的是**别的**工具的用法，
    且格式与正确情况完全一样（无「这是模糊匹配」标注）。
    """
    blocks = agent_mod._tool_blocks()
    for name in ("dev_patch", "web_weather", "memory_forget", "dev_mcp_add"):
        assert name in blocks, "%r 不在 blocks 里" % name
        text = str(agent_mod._tool_search(name))
        assert blocks[name][:40] in text, (
            "tool_search(%r) 没有返回它自己的用法" % name)