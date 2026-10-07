"""写租约：把「用户已批准」物化成 `_lease_blocked` 能消费的结构。

## 这一族要修什么

`_write_intent` 是**逐条消息、无状态**的纯函数，只看当前这一句话。于是多轮会话里
最自然的批准语全部失效：

    denied : 改吧 动手吧 那就修吧 写吧 开始改 就这么改 你改吧 修吧 改 修
    granted: 动手改 改一下 动手修

后果是死循环：用户明确批准了，agent 仍然不动，而用户已经答过了，无从再答。

## 为什么不是往词表里加词（已实测否决）

临时把裸「改」「修」加进 `_WRITE_VERBS`，**3 条守卫红**：

    test_a_sentence_with_only_a_bare_verb_is_not_granted
    test_the_clarification_prompt_also_respects_a_prohibition   <- 否定作用域
    test_a_conditional_sentence_with_an_unlisted_verb_falls_back_to_denied  <- 条件式

根因是结构性的：词表逐条消息，而缺陷跨轮。往里加跨轮批准语，必然要开始处理
「否定+批准语」「条件式+批准语」的组合 —— 那是无限 regress。

所以改成在**判定结果**上做文章：granted 轮次铸一张租约，之后由 `_lease_blocked`
做**结构性**判定。词表一个字都不用动。

## 关键约束：租约只能**放宽**，绝不能收窄到读不了文件

`_lease_blocked` 的语义是「不在 capabilities 里就拦」。所以一份只含
`write_local` 的租约会把 `read_file` 一起拦掉 —— agent 连要改的文件都读不到。
实测确认过，这是比原缺陷更糟的方向。

因此铸造时必须**一并授予只读能力**，但只加只读：授权写不蕴含授权跑命令或改配置。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import agent  # noqa: E402
import tools as T  # noqa: E402
from agent_runtime.tools import Capability  # noqa: E402


@pytest.fixture()
def agent_with_tools():
    return agent.Agent(llm=None, tool_registry=dict(T.TOOLS))


# --------------------------------------------------------------------------
# 核心：租约的字段必须与检查侧认的结构一致
# --------------------------------------------------------------------------

def test_the_minted_lease_has_exactly_the_fields_the_check_side_reads():
    """字段错一个，`_lease_blocked` 就会静默放行或误拦。

    检查侧读的是 `status` / `expires_at_epoch` / `capabilities`
    （`agent.py` 的 `_lease_blocked`），少一个都不行 —— 尤其
    `expires_at_epoch`：缺了它会被当成 0.0，也就是**当场过期**。
    """
    a = agent.Agent(llm=None, tool_registry=dict(T.TOOLS))
    lease = a._mint_write_lease()
    assert lease, "铸造返回了空租约"
    for field in ("id", "capabilities", "created_at", "expires_at",
                  "expires_at_epoch", "status", "source"):
        assert field in lease, f"租约缺字段 {field!r} —— 检查侧读它时不会报错，只会静默"
    assert lease["status"] == "active"
    assert lease["source"] == "write_intent", (
        "source 应当标明这是写意图铸的，不是工作流租约 —— "
        "game_workflow 那边铸的租约 source=workflow，两者要能区分"
    )


def test_the_lease_is_not_expired_the_moment_it_is_minted():
    """反向守卫：`expires_at_epoch` 必须是**未来**。

    少了这条，一个填错的 TTL 会铸出「当场就过期」的租约，而症状是
    「授权好像没生效」—— 极难定位。
    """
    a = agent.Agent(llm=None, tool_registry=dict(T.TOOLS))
    before = time.time()
    lease = a._mint_write_lease()
    assert lease["expires_at_epoch"] > before, "租约铸出来就已经过期了"
    assert lease["expires_at_epoch"] > time.time()


# --------------------------------------------------------------------------
# 关键约束：租约绝不能把 agent 锁在「能写但不能读」
# --------------------------------------------------------------------------

def test_a_write_lease_must_still_allow_reading(agent_with_tools):
    """这是最容易写错、后果最严重的一处。

    `_lease_blocked` 的语义是**收窄**：不在 capabilities 里就拦。所以一份只含
    write_local 的租约会把 read_file 一起拦掉，agent 连要改的文件都读不到。
    """
    agent_with_tools.capability_lease = agent_with_tools._mint_write_lease()
    blocked, reason = agent_with_tools._lease_blocked("read_file")
    assert blocked is False, f"写租约把只读拦了：{reason}"


def test_a_write_lease_does_not_grant_unrelated_capabilities(agent_with_tools):
    """反向守卫：写授权**不蕴含**执行、改配置、联网。

    这三类各有自己的边界。要是这份租约把它们一并放开，那它就不是「批准写一次」，
    而是「把权限闸门拆了」。
    """
    agent_with_tools.capability_lease = agent_with_tools._mint_write_lease()
    for tool, capability in (("run_command", "exec"),
                             ("dev_commit", "admin"),
                             ("web_search", "network")):
        if tool not in agent_with_tools.tools:
            continue
        blocked, reason = agent_with_tools._lease_blocked(tool)
        assert blocked is True, f"{tool}（{capability}）被写租约放行了 —— 授权写不该蕴含它"
        assert capability in reason, f"拦截理由应当点名 {capability}，实际：{reason}"


def test_the_write_capabilities_are_derived_from_tool_declarations():
    """能力集必须**从工具声明反推**，不能硬编码。

    硬编码一份列表 = 第二份真相。工具改��质而列表没改，授权边界就悄悄错了。
    """
    a = agent.Agent(llm=None, tool_registry=dict(T.TOOLS))
    lease = a._mint_write_lease()
    granted = set(lease["capabilities"])
    assert Capability.WRITE_LOCAL.value in granted
    assert Capability.WRITE_EXTERNAL.value in granted
    # 声明侧确实存在这些能力 —— 证明「反推」不是碰巧写对了
    declared = {s.capability.value for s in
                (a._tool_spec(n) for n in a.tools) if s is not None}
    assert granted - {Capability.READ_LOCAL.value,
                      Capability.READ_EXTERNAL.value} <= declared


# --------------------------------------------------------------------------
# TTL：租约是「本轮批准过」，不是长期授权
# --------------------------------------------------------------------------

def test_the_ttl_is_clamped_so_one_approval_cannot_last_forever(agent_with_tools):
    """反向守卫：有人传一个极大的 TTL 进来时，必须被夹住。

    「改吧」只批准了这一次。租约活过一整个会话，就成了长期授权。
    """
    lease = agent_with_tools._mint_write_lease(ttl_seconds=10 ** 9)
    ttl = lease["expires_at_epoch"] - time.time()
    assert ttl <= agent._WRITE_LEASE_MAX_TTL_SECONDS + 5, (
        f"TTL 被放大到 {ttl:.0f} 秒，未夹到上限"
    )


def test_a_negative_ttl_is_clamped_up_rather_than_producing_a_dead_lease(
        agent_with_tools):
    """负 TTL 会铸出「一出生就过期」的租约。必须夹住下界。"""
    lease = agent_with_tools._mint_write_lease(ttl_seconds=-100)
    assert lease["expires_at_epoch"] > time.time(), "负 TTL 产出了当场过期的租约"


def test_an_expired_lease_is_blocked(agent_with_tools):
    """过期即失效 —— 与 `_lease_blocked` 已有的守卫 `test_capability_lease` 同义，
    这里从铸造侧再钉一次，确保铸造出来的结构能被检查侧判成过期。"""
    agent_with_tools.capability_lease = agent_with_tools._mint_write_lease(
        ttl_seconds=1)
    assert agent_with_tools._lease_blocked("apply_edit")[0] is False
    expired = dict(agent_with_tools.capability_lease)
    expired["expires_at_epoch"] = time.time() - 1
    agent_with_tools.capability_lease = expired
    blocked, reason = agent_with_tools._lease_blocked("apply_edit")
    assert blocked is True, "过期租约仍放行"
    assert "过期" in reason


def test_a_released_lease_is_blocked(agent_with_tools):
    """release 状态必须同样被认 —— 与检查侧既有的 released 守卫对齐。"""
    agent_with_tools.capability_lease = dict(
        agent_with_tools._mint_write_lease(), status="released")
    blocked, reason = agent_with_tools._lease_blocked("apply_edit")
    assert blocked is True, "已释放的租约仍放行"
    assert "释放" in reason


# --------------------------------------------------------------------------
# 无写工具时不该铸出空壳租约
# --------------------------------------------------------------------------

def test_no_lease_is_minted_when_no_write_capability_exists():
    """只读工具集不该铸出写租约 —— 空租约会让检查侧开始拦只读操作。"""
    read_only = {n: s for n, s in T.TOOLS.items()
                 if s.capability in (Capability.READ_LOCAL, Capability.READ_EXTERNAL)}
    a = agent.Agent(llm=None, tool_registry=read_only)
    assert a._mint_write_lease() == {}, "没有写能力却铸出了租约"


def test_an_explicit_capability_list_is_honoured(agent_with_tools):
    """显式传入能力集时按传入的来 —— 但只读能力仍要补上。"""
    lease = agent_with_tools._mint_write_lease(
        capabilities=[Capability.WRITE_LOCAL.value])
    granted = set(lease["capabilities"])
    assert granted == {Capability.WRITE_LOCAL.value,
                       Capability.READ_LOCAL.value,
                       Capability.READ_EXTERNAL.value}, (
        f"实际授了 {sorted(granted)}"
    )