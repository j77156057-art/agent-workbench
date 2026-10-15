"""工具暴露**不再按问题相关性收窄** —— 只有用户的联网开关是闸门。

## 为什么撤

`ContextRouter` 原先靠 `_CODE`/`_WEB` 命中与否决定给哪些工具组，而那几个正则
只认**名词**（`代码|函数|类|报错…`）。实测：

| 请求 | 撤除前有效工具 | 缺什么 |
|---|---|---|
| 跑一下测试 | 26 / 116 | `dev_testrun` |
| 装个依赖 | 26 / 116 | `dev_deps` |
| 这条能提交吗 | 26 / 116 | `dev_commit` |

不是小漏：`dev_testrun` 的说明里明写「别再 `run_command("pytest -q")` 自己读一整屏
文本」，而现在恰好是它被收走 —— **机制与召回互相打脸**。

## 核实三家 harness 都不这么做

| harness | 怎么决定暴露什么 |
|---|---|
| **Pi** | 不涉及工具选择（安全模型是进程边界） |
| **Hermes** | 用户/平台显式 toolsets，零自动召回 |
| **opencode codemode** | 预算截断 + **轮转**公平 + 检索 |

opencode `Discovery` 原文：

> **Every tool namespace is always listed** with its tool count regardless of
> budget. Selection is **round-robin** across namespaces for fairness.

我们的关键词判定是**三家里唯一的异类**，也是唯一失效的那个。

## 代价（实测，原生通道 tool schema，每次模型调用都付）

```
撤除前   26 工具 /  2947 tok
撤除后  109 工具 / 13832 tok   （联网关）
        116 工具 / 14490 tok   （联网开）
```

**4.7 倍。** 这是明确的产品取舍，不是 bug 修复。

## 联网开关现在是唯一闸门

原先要求 `web_enabled and _WEB.search(query)`，而 `_WEB` 同样只认名词 ——
实测「查下高铁票」「查高铁」都不命中，于是**用户把联网开关打开了，web 工具却调不到**。
那是授权已给而能力不可达，属于静默失效。

现在 `web_enabled` 本身决定，开关就是闸门。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402

#: 撤除前实测收走的工具，按「请求形状」而不是按工具名组织 ——
#: 因为丢的是**动词型请求**下的整组工具，不是个别工具。
VERB_SHAPES = ("跑一下测试", "装个依赖", "这条能提交吗", "改一下 README 里的端口", "你好")


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


def _agent(web_enabled: bool = False) -> agent_mod.Agent:
    a = agent_mod.Agent(llm=_Stub(), tool_registry=dict(tools_mod.TOOLS))
    a.web_enabled = web_enabled
    a.last_context_plan = a.context_router.route(
        "跑一下测试", code_root="D:/x", web_enabled=a.web_enabled)
    return a


# --------------------------------------------------------------------------
# 核心：动词型请求不再丢工具
# --------------------------------------------------------------------------

def test_a_verb_shaped_request_no_longer_loses_tools():
    """**本笔的主张**：撤除后各类请求拿到同一套工具。

    撤除前实测只有 26 个，且 `dev_testrun`/`dev_deps`/`dev_commit` 全在外面。
    """
    a = _agent()
    baseline = None
    for question in VERB_SHAPES:
        a.last_context_plan = a.context_router.route(
            question, code_root="D:/x", web_enabled=a.web_enabled)
        effective = set(a._effective_tool_names())
        if baseline is None:
            baseline = effective
        assert effective == baseline, (
            "%r 拿到 %d 个工具，与第一条的 %d 个不一致 —— "
            "收窄还在按问题变化" % (question, len(effective), len(baseline)))


def test_the_tools_that_used_to_be_dropped_are_present():
    """**反向守卫**：钉住那三个具体名字。

    数量对但名字换了是另一种失效 —— 所以记名字而不只记数量。
    """
    a = _agent()
    effective = set(a._effective_tool_names())
    for name in ("dev_testrun", "dev_deps", "dev_commit", "apply_edit",
                 "dev_find_references", "game_screenshot"):
        assert name in effective, (
            "%r 又不在有效集里了 —— 撤除没生效，或被别的东西挡了" % name)


def test_no_relevance_group_is_computed_from_keywords():
    """**反向守卫**：撤除的是**按关键词决定组**，不是碰巧这几轮问对了。

    断言的是**机制**：`developer`/`code`/`general`/`game`/`assets`/
    `knowledge`/`orchestration` 必须在**任何**问题下都在。
    """
    a = _agent()
    for question in ("你好", "今天天气不错", "随便聊聊", "1+1等于几",
                     "跑一下测试", "这个词不该命中任何正则xyzzy"):
        a.last_context_plan = a.context_router.route(
            question, code_root="D:/x", web_enabled=a.web_enabled)
        groups = set(a.last_context_plan.tool_groups)
        for required in ("developer", "code", "general", "orchestration"):
            assert required in groups, (
                "%r 下缺 %r 组 —— 相关性收窄还在（原始正则 %r 命中=%s）"
                % (question, required,
                   "开发者语境", bool(__import__(
                       "agent_runtime.context_router", fromlist=["x"])._CODE.search(question))))


def test_the_web_switch_is_the_only_gate():
    """**反向守卫**：联网开关必须真的起作用。

    撤除前要求 `web_enabled and _WEB.search(query)`，而实测「查下高铁票」
    不命中 —— 用户把开关打开了却调不到 web 工具，那是授权已给而能力不可达。
    """
    for enabled, want in ((False, False), (True, True)):
        a = _agent(web_enabled=enabled)
        a.last_context_plan = a.context_router.route(
            "查下高铁票", code_root="D:/x", web_enabled=enabled)
        effective = set(a._effective_tool_names())
        assert ("web_search" in effective) is want, (
            "web_enabled=%s 时 web_search %s（期望%s）"
            % (enabled, "在" if "web_search" in effective else "不在",
               "在" if want else "不在"))


def test_the_web_switch_is_consulted_without_the_keyword_regex():
    """**正向守卫**：开关生效**不得**依赖正则命中。

    这条防的是「改回 `web_enabled and _WEB.search(query)`」——
    那样单个问题能过，但「查下高铁票」那种又会失效。
    """
    import agent_runtime.context_router as C

    a = _agent(web_enabled=True)
    a.last_context_plan = a.context_router.route(
        "查下高铁票", code_root="D:/x", web_enabled=True)
    assert not C._WEB.search("查下高铁票"), (
        "前提变了：`_WEB` 现在能命中「查下高铁票」了 —— "
        "若这是有意的，请重新评估是否还需要撤相关性")
    assert "web" in a.last_context_plan.tool_groups, (
        "正则没命中而开关开着，web 组仍应加入 —— 开关是唯一闸门")


# --------------------------------------------------------------------------
# 代价是明确的，且必须被记录
# --------------------------------------------------------------------------

def test_the_cost_is_the_documented_one():
    """**记录代价**：4.7 倍，每次模型调用都付。

    这条存在的意义是：将来有人看到 `13832` 这个数字想「优化」回去时，
    会先撞到它，从而知道自己在撤掉什么。
    """
    a = _agent(web_enabled=False)
    schemas = tools_mod.tool_schemas(
        sorted(a._effective_tool_names()), registry=a.tools) or []
    cost = len(json.dumps(schemas, ensure_ascii=False)) // 4
    assert 13000 < cost < 14500, (
        "撤除后的 schema 成本实测 %d tokens，与记录的 13832 差得远 —— "
        "要么工具数变了（请更新文档里的数字），要么撤除没生效" % cost)
    assert len(schemas) >= 100, "有效工具数远低于记录的 109：%d" % len(schemas)


def test_progressive_prompt_compression_still_applies():
    """**反向守卫**：撤的是**原生通道**的收窄，系统提示的 progressive 仍在。

    撤相关性不等于撤预算截断 —— 那是 opencode 明确保留的那一半
    （「Every tool namespace is always listed ... budget」）。
    """
    text = agent_mod.SYSTEM_PROMPT
    assert "【扩展工具索引】" in text, (
        "系统提示里没有扩展工具索引 —— progressive 分层被误撤了")
    assert "共 " in text and "个工具" in text, "索引段头的完整度明示不见了"


# --------------------------------------------------------------------------
# 不许靠放宽联网绕过安全开关
# --------------------------------------------------------------------------

def test_web_tools_stay_blocked_when_the_switch_is_off():
    """**反向守卫**：撤相关性**不许**撤掉联网安全开关。

    这是本笔最容易顺手弄坏的一条：把「相关性」当成「限制」一起清掉，
    就等于让关掉联网的用户照样能调外网工具。
    """
    a = _agent(web_enabled=False)
    a.last_context_plan = a.context_router.route(
        "查下高铁票", code_root="D:/x", web_enabled=False)
    effective = set(a._effective_tool_names())
    leaked = [n for n in effective if tools_mod.TOOLS[n].group == "web"]
    assert not leaked, (
        "联网关闭时这些外网工具仍可调：%s —— "
        "撤相关性不等于撤安全开关" % leaked)
    assert a._web_blocked("web_search") is True, "_web_blocked 判定被改坏了"


def test_network_capability_is_blocked_when_web_is_disabled():
    """联网开关拦的是 group=="web"，capability=NETWORK 只服务于超时分类。

    2026-10-15 修正契约：本用例曾经造一个「只有 capability=NETWORK、没有 group」
    的工具并断言它被拦。但那正好是 bug 本体——`mcp_bridge` 给所有内联 MCP
    工具标 NETWORK（保守标记，给 `_is_network_tool` 超时钩子用），旧规则
    `group=="web" or capability==NETWORK` 于是把 **stdio 本地连接器**
    （godot/unity）的工具也在联网关闭时全部滤掉，而开关默认关 ⇒
    9b801d5 的连接器一等工具功能默认失效。

    正确契约：group 才表达「是否跨外网」。bridge 自己就分
    `"web" if http else "mcp"`。所以：
      · group=="web"（真上网）→ 开关关时拦（安全属性保留）；
      · capability==NETWORK 但 group=="mcp"（stdio 本地）→ 不拦。
    """
    import agent as agent_mod
    from agent_runtime.tools import Capability, SideEffect

    a = agent_mod.Agent(llm=None)
    a.tools = {
        "remote_tool": {
            "func": lambda _arg: "should not run",
            "capability": Capability.NETWORK,
            "side_effect": SideEffect.IRREVERSIBLE,
            "parallel_safe": False,
            "group": "web",
        },
        "mcp__godot__run": {
            "func": lambda _arg: "local engine bridge",
            "capability": Capability.NETWORK,
            "side_effect": SideEffect.MUTATING,
            "parallel_safe": False,
            "group": "mcp",
        },
    }
    a.web_enabled = False
    assert a._web_blocked("remote_tool") is True, "group=web 的真上网工具必须被拦"
    assert a._web_blocked("mcp__godot__run") is False, \
        "stdio 本地连接器工具不该被联网开关拦（默认关=功能全灭）"
    a.web_enabled = True
    assert a._web_blocked("remote_tool") is False
    assert a._web_blocked("mcp__godot__run") is False