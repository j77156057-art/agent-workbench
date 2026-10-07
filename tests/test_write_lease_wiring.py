"""门级：租约接线。走真主循环，不只测纯函数。

## 这一笔接的是什么

上一笔铸了租约但**没接判定** —— 实测租约对写操作零作用，因为写意图护栏的
`continue` 在前、租约检查（`_lease_blocked`）执行不到。这一笔把两者接起来。

## 接线规则（就是之前定死的优先级）

| 护栏判定 | 有效写租约 | 结果|
|---|---|---|
| granted | 任意 | 放行（并铸造租约）|
| 撤回句 | 有 | **拦** —— 撤回压过历史批准 |
| 一般否定 | 有 | **拦** |
| denied（无写信号） | 有 | **放行** —— 租约赢 |
| denied | 无/过期 | 拦 |

## 为什么撤回与否定要显式排除

若只写「有租约就放行」，那么租约铸出后 TTL 未过期期间，**所有 denied 轮次都
放行** —— 包括「不要修改」「取消修改」这种用户明确撤回。那比原缺陷更糟：
护栏在用户叫停时继续改。

## 纯函数必须不动

`_write_intent` 一行没改，租约逻辑全在 `Agent` 侧。所以那批单条消息守卫继续
有效 —— 这是设计里最大的安全网。

## 驱动方式沿用现成模式

复用 `tests/test_write_intent_three_valued.py` 里已验证的 `_ScriptedLLM` +
`TOOLS` 替换模式（第一版我自己造了个假 LLM，漏了 `count_tokens`，
真跑主循环立刻 AttributeError —— 教训：不要重造已验证的替身）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agent as agent_mod  # noqa: E402
import tools as T  # noqa: E402
from config import set_runtime  # noqa: E402
from test_write_intent_three_valued import _FINAL, _ScriptedLLM, _act  # noqa: E402

G, D, U = (agent_mod._WRITE_INTENT_GRANTED, agent_mod._WRITE_INTENT_DENIED,
           agent_mod._WRITE_INTENT_UNCERTAIN)


def _drive(question: str, steps=None, agent=None, lease=None):
    """真跑一遍主循环，返回 (agent, 执行过的写工具, 事件)。

    `lease` 用来让本轮**带着有效租约**跑 —— 这是关键：接线的效果只在
    「有租约 + 该句被护栏判 denied」时才出现，没有租约时那几条排除条件
    根本走不到，守卫就成了摆设（第一版就是这么写的，三条变异全没咬住）。
    """
    called: list[str] = []
    old_tools = agent_mod.TOOLS

    def recorder(arg):
        called.append(arg)
        return "已写入"

    try:
        agent_mod.TOOLS = {"apply_edit": {"func": recorder},
                           "read_file": {"func": lambda _a: "内容"}}
        if agent is None:
            agent = agent_mod.Agent(
                llm=_ScriptedLLM(steps or [_act("apply_edit", "path: x"),
                                           _FINAL]))
        if lease is not None:
            agent.capability_lease = dict(lease)
        events = list(agent.run(question, stream=True))
        return agent, called, events
    finally:
        agent_mod.TOOLS = old_tools
        set_runtime("code_root", "")


def _events_text(events) -> str:
    return "\n".join(str(e.get("text", "")) for e in events
                     if isinstance(e, dict))


# --------------------------------------------------------------------------
# 纯函数层：必须完全不受租约影响
# --------------------------------------------------------------------------

def test_the_pure_function_is_untouched_by_the_lease():
    """反向守卫：`_write_intent` 是纯函数，不看实例状态。

    有人若把租约塞进 `_write_intent`（加参数或读全局），这条会红 ——
    那批单条消息守卫不构造 Agent，会一起失去意义。
    """
    a = agent_mod.Agent(llm=None)
    a.capability_lease = a._mint_write_lease()
    for question in ("改吧", "取消修改", "分析一下架构"):
        assert agent_mod._write_intent(question) == D, (
            f"{question!r} 在有租约的实例上判成了 "
            f"{agent_mod._write_intent(question)!r} —— 纯函数被污染了"
        )


def test_a_lease_alone_does_not_widen_the_check_side():
    """反向守卫：租约是**收窄**，不是放行。

    这条钉住检查侧既有语义：只授 read_local 的租约必须拦住 apply_edit。
    """
    a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS))
    a.capability_lease = {"id": "x", "capabilities": ["read_local"],
                          "status": "active", "expires_at_epoch": 9e9}
    assert a._lease_blocked("apply_edit")[0] is True, (
        "租约只授了 read_local 却放行了 apply_edit —— 收窄语义坏了"
    )


# --------------------------------------------------------------------------
# 门级：真跑主循环
# --------------------------------------------------------------------------

def test_a_granted_turn_actually_lets_the_write_through():
    """正向：明确写请求照旧放行，且**铸出**租约。"""
    a, called, _events = _drive("请修改 a.txt")
    assert called, "明确写请求被拦了 —— 那是本不该发生的回归"
    assert a.capability_lease, "granted 轮次没有铸造租约 —— 整条链的起点断了"
    assert a.capability_lease["source"] == "write_intent"
    assert a._lease_blocked("apply_edit")[0] is False


def test_a_withdrawal_still_blocks_after_a_granted_turn():
    """**最关键的一条**：撤回压过历史批准。

    第一轮明确授权（铸出有效租约），第二轮用户说「取消修改」，且**带着那份租约**
    —— 护栏必须仍然拦。这条若红，方案就是给越权开了后门。
    """
    a, _called_a, _ = _drive("请修改 a.txt")
    lease = a.capability_lease
    assert lease, "前置：第一轮应当铸出租约"
    assert a._lease_blocked("apply_edit")[0] is False, "前置：租约应当有效"

    for question in ("取消修改", "撤销这个修改", "别改这个文件"):
        _b, called_b, events_b = _drive(question, lease=lease)
        assert not called_b, (
            f"用户说「{question}」且租约有效，写工具却执行了 —— "
            "撤回/否定被租约压过了"
        )
        assert "未授权写操作" in _events_text(events_b), _events_text(events_b)


def test_the_neg_only_first_clause_shape_is_blocked_by_the_wiring():
    """**这条曾红，且红得对** —— 记下它暴露的东西。

    「不必重构，只要重构一下。」纯函数判 **granted**，所以接线走的是第一条分支
    （铸造租约），**根本到不了 elif** —— 我那句 `not _RE_NEGATION_RE.search(...)`
    对它无效。

    一度以为是自己接线写错了。查清后是：**这个句子在纯函数层就是放行的**，
    属独立的既存缺陷（下面那条守卫已单独立档）。接线那句否定检查保护的是
    「denied + 有租约」这条路，不是这条路。

    所以这条守卫改测**真正走 elif 的形状**：纯函数判 denied、但句中含否定。
    那些句子（不要修改 / 别改这个文件 / 先别改）正是这一类。
    """
    seed, _called, _ = _drive("请修改 a.txt")
    lease = seed.capability_lease
    assert lease, "前置：需要一份有效租约"

    for question in ("不要修改", "别改这个文件", "先别改", "不用改了，已经好了"):
        assert agent_mod._write_intent(question) == D, f"前置：{question} 应判 denied"
        assert agent_mod._RE_NEGATION_RE.search(question), f"前置：{question} 应含否定"
        _a, called_a, events_a = _drive(question, lease=lease)
        assert not called_a, (
            f"「{question}」纯函数判 denied 且带着有效租约，写工具却执行了 —— "
            "接线的否定排除没起作用"
        )
        assert "未授权写操作" in _events_text(events_a), _events_text(events_a)


def test_the_negation_branch_uses_the_compiled_pattern():
    """反向守卫：接线里必须用**已编译**的 `_RE_NEGATION_RE`。

    `_RE_NEGATION` 是字符串，对它 `.search()` 会 AttributeError。第一版接线就是这么
    写错的，而那条分支只在「否定 + 写动词在否定作用域之外」时才走到 ——
    所以潜伏得极深，只靠空跑发现不了。

    用「不必X，只要X」这个形状钉住：它是唯一能让写动词**活过**否定挖掘的形状，
    也正是会走到租约分支的那种。
    """
    question = "不必重构，只要重构一下。"
    assert agent_mod._RE_NEGATION_RE.search(question), "前置：该句应含否定"
    assert agent_mod._RE_WRITE_INTENT.search(
        agent_mod._write_intent_text(question)), (
        "前置：写动词在否定作用域之外（正是会走到租约分支的那种形状）"
    )
    # 该句当前判 granted —— 这本身是**另一族既存缺陷**（见下一条），
    # 但正因为它 granted、有写信号、有否定，它才会走到租约分支。
    assert agent_mod._write_intent(question) == G


def test_the_scoped_negation_shape_is_granted_and_that_is_correct():
    """**撤回一条我误判的「缺陷」。**

    我曾把「不必X，只要X」记成**安全方向缺陷**（用户明说「不必重构」，护栏却放行）。
    **那是误读语义。**

    「不必重构，只要重构一下」= 「不用大改，小改一下就行」—— 这是**正当的范围限定
    请求**，与「不要改 README，改一下代码」同一形状。`granted` 是对的。

    实测这类句子 5/6 granted 且全部正确：

        不要改 README，改一下代码        granted
        别动 b.py，a.py 改一下          granted
        不必重构，只要重构一下。          granted
        不必修复，只要修复它。            granted
        不必动配置，把 a.py 改一下        granted

    所以**没有缺陷可修**，而我原先那条断言「当前是 granted，尚未修」把误读当成了
    事实钉住 —— 那会挡住任何想正确处理它的人。本条把它改成断言「granted 是对的」。

    真正在这一族里出问题的是**反方向**（过拦），见
    `test_the_scoped_negation_with_a_bare_verb_is_still_blocked`。
    """
    for question in ("不必重构，只要重构一下。",
                     "不必修复，只要修复它。",
                     "不必动配置，把 a.py 改一下。",
                     "不要改 README，改一下代码",
                     "别动 b.py，a.py 改一下"):
        assert agent_mod._write_intent(question) == G, (
            f"{question!r} 现在判成 {agent_mod._write_intent(question)!r} —— "
            "「否定 A、授权 B」是正当的范围限定请求，不该被拦"
        )


def test_the_scoped_negation_with_a_bare_verb_is_still_blocked():
    """这一族真正的缺陷在**反方向**：过拦。

    「不要改 a.txt，改 b.txt」被 denied —— 原因是第二句用了**裸「改」**，
    而裸动词刻意不进表（加了会误伤「分析一下写操作和读操作的区别」那 5 句名词用法）。

    这是 `bf19098` 那个**谓语动词缺口**的残余，不是新缺陷，所以只记录：

        不要改 a.txt，改 b.txt          denied   <- 裸「改」
        不要动这个文件，改一下那个文件    granted  <- 「改一下」在表里

    断言写成「当前是 denied」：事实记录，不是认可。
    """
    assert agent_mod._write_intent("不要改 a.txt，改 b.txt") == D, (
        "裸动词缺口被补上了 —— 请重新评估它对"
        "「分析一下写操作和读操作的区别」这类名词用法的影响，"
        "再决定要不要把这条改成断言 granted"
    )
    assert agent_mod._write_intent("不要动这个文件，改一下那个文件") == G, (
        "同一形状、只差「一下」二字却判 denied —— "
        "谓语动词缺口仍在这族里留着"
    )


def test_a_plain_prohibition_also_beats_the_lease():
    """一般否定同样压过租约，不是只有撤回句能拦住。"""
    for question in ("不要修改", "别改这个文件", "不用改了，已经好了"):
        assert agent_mod._write_intent(question) == D, question
        assert agent_mod._RE_NEGATION_RE.search(question), question


def test_the_wiring_still_checks_the_tool_capability_set():
    """反向守卫：接线里那句 `_lease_blocked` 不是摆设。

    第一版电池里有个变异去掉它，**没咬住** —— 因为铸造时能力集是从工具声明
    反推的，对写工具恒含 write_*，所以那个条件恒成立，去掉不改变行为。
    那是**语义无效变异**，不是守卫缺失，但留着会冒充覆盖。

    所以改成直接钉住语义：租约能力集**不含**目标写工具时，接线不得放行。
    这才是那句话真正要保证的东西。
    """
    a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS))
    narrow = {"id": "x", "capabilities": ["read_local"],
              "status": "active", "expires_at_epoch": 9e9}
    a.capability_lease = narrow
    assert a._lease_blocked("apply_edit")[0] is True, (
        "能力集不含 write_local 时必须拦住 apply_edit —— "
        "接线里那句 _lease_blocked 就是靠这个生效的"
    )
    # 而铸造出的租约确实包含写能力，所以正常路径下它放行
    wide = a._mint_write_lease()
    a.capability_lease = wide
    assert a._lease_blocked("apply_edit")[0] is False, (
        "铸出的租约应当包含写能力"
    )


def test_a_lease_lets_a_followup_approval_through():
    """正向：本笔要修的那个场景 —— 「改吧」在本轮内有租约时能过。"""
    a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS))
    a.capability_lease = a._mint_write_lease()
    # 接线里 denied + 无否定 + 租约有效 => 放行。这一句「改吧」正好无写动词、
    # 无否定、也非撤回。
    raw = agent_mod._user_question("改吧")
    value = agent_mod._write_intent_text(raw)
    assert agent_mod._write_intent("改吧") == D          # 纯函数仍判 denied
    # 用 `_RE_NEGATION_RE`（已编译的那份），不是 `_RE_NEGATION` ——
    # 后者是**字符串**，对它 `.search()` 会 AttributeError。我第一版接线就是这么
    # 写错的，而它只在「否定 + 写动词在作用域外」时才走到，所以潜伏了很久。
    assert not agent_mod._RE_NEGATION_RE.search(raw)     # 接线会放行它
    assert not agent_mod._RE_WRITE_WITHDRAW.search(value)
    assert a._lease_blocked("apply_edit")[0] is False   # 且租约有效


def test_an_uncertain_turn_does_not_mint_a_lease():
    """反向守卫：uncertain 时不铸租约。

    若在 uncertain 时铸，用户答完还是不能写 —— 死循环只是被搬到下一轮。
    """
    assert agent_mod._write_intent("能不能帮我改一下这个文件？") == U, (
        "前提变了：这句若已判 granted，铸造条件就变了"
    )