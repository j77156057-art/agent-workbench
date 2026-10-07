"""否定句不能被读成「写授权」。

## 这一族

`_RE_WRITE_INTENT`（`agent.py`）是纯动词表，`agent.py` 里那道写操作同意护栏是

    if self._is_write_tool(action) and action not in {"memory_save", "memory_forget"} \\
            and not _has_write_intent(question):   # <- 没有写意图才拦

也就是**只要句子里出现写动词就放行**。于是用户明说「不要修改任何文件」，因为含
「修改」，被判成**有**写意图 → 护栏不拦 → 用户��确禁止被读成明确授权。

方向很糟：这一族本体是「把没做的记成做了 / 把禁止的当授权」，而这里的失效是
**朝着危险的那一侧**。实测 12 句否定问法里 **9 句**是这个方向的误判。

## 修法为什么是「挖掉被否定的那个动词」

不是「句子里有否定词就整句清零」。中文否定只覆盖它自己管的那个动词：

    「不要修改文档，直接重写整个实现」  否定「修改」，但「重写」被授权
    「不要修改。」                    什么都没授权

整句清零会把第一句也判成没意图，于是模型在该写的时候不写 —— 那是反方向的事故，
而且更隐蔽（表现为「模型不听指令」而不是「模型乱改」）。

「挖掉」这个方向天然安全：它只会**移除**已有的授权，不可能凭空造出授权。

## 守卫的分工

- 否定问法一律判成「无写意图」（这一族本体）
- 没被否定的写请求一个都不能漏判（护栏宁可放过，也不能把正常请求拦死）
- **修法本身的边界**：否定不跨标点、不吃掉另一个被授权的动词、
  「别忘了 X」不算否定
- 两处判读（`_has_write_intent` / `_ambiguous_write_request`）必须一致
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402


# --------------------------------------------------------------------------
# 本体：明令禁止不能被读成授权
# --------------------------------------------------------------------------

NEGATIVE_ONLY = [
    "不要修改任何文件。",
    "请不要修改任何文件，先把问题列出来。",
    "无需修改。",
    "不必重构，只要解释清楚设计。",
    "不要创建新文件。",
    "禁止删除任何东西。",
    "勿修改配置。",
    "不许新增功能。",
    "这次不要提交代码。",
    "只需要一份报告，不要任何改动。",
    "我不想让你修改文件，帮我看看问题在哪。",
    "别改代码，我只想要一份评估报告。",
    "只读分析，不要动文件。",
    "先不要改，看看哪里有问题就行。",
]


@pytest.mark.parametrize("question", NEGATIVE_ONLY)
def test_a_prohibition_is_not_an_authorization(question):
    assert agent._has_write_intent(question) is False, (
        "用户明说不要改，却因为句子里有写动词被判成有写意图 —— "
        "护栏会因此放行落盘"
    )


def test_the_bare_negated_sentence_is_covered():
    """最短的那句也要盖住：它是这一族最容易被「参数化测试写漏」的形状。"""
    assert agent._has_write_intent("不要修改。") is False
    assert agent._has_write_intent("禁止创建。") is False
    assert agent._has_write_intent("别删除。") is False


# --------------------------------------------------------------------------
# 反向守卫：该写的一定要判真，一个都不能漏
# --------------------------------------------------------------------------

POSITIVE_ONLY = [
    "请修改 a.txt。",
    "创建 player.gd。",
    "重构这个模块。",
    "删除 obsolete 文件。",
    "generate a PDF report",
    "fix the crash in main.py",
    "帮我改一下 README。",
    "把这个函数改成异步。",
    "补全缺失的实现。",
    "导出结果到 x.csv。",
    "移除那行死代码。",
    "提交代码。",
    "替换成新版本。",
    "生成配置文件。",
    # 带「不」但**不是**否定授权的写法，最容易被过度否定误伤
    "不改了，帮我重写。",
    "不用谢，帮我改一下。",
    "不一定呢，先帮我创建个文件。",
]


@pytest.mark.parametrize("question", POSITIVE_ONLY)
def test_an_ordinary_write_request_is_still_authorized(question):
    assert agent._has_write_intent(question) is True, (
        f"{question!r} 是明确的写请求，却判成没有写意图 —— "
        "护栏会把正常操作拦死"
    )


# --------------------------------------------------------------------------
# 修法本身的边界：否定只取消它自己那个动词
# --------------------------------------------------------------------------


def test_a_negation_does_not_cancel_the_whole_sentence():
    """反向守卫：整句清零也是「看起来对」的错误修法。"""
    for question, why in (
        ("不要修改文档，直接重写整个实现。", "「重写」是被授权的"),
        ("不用新建文件，把现有的改一下就行。", "「改一下」是被授权的"),
        ("不要删东西，帮我改一下配置。", "「帮我改一下」是被授权的"),
        ("别修改了，直接重写。", "「重写」是被授权的"),
    ):
        assert agent._has_write_intent(question) is True, (
            f"{question!r} 判成了没有写意图，但 {why} —— "
            "否定只该取消它自己管的那个动词"
        )


def test_a_negation_never_reaches_across_punctuation():
    """反向守卫：filler 若能跨标点，就会把**后面子句**的授权一起吃掉。

    「不需要解释，直接修改」—— 否定到逗号就该停，后面的「修改」是明确授权。
    """
    for question in (
        "不需要解释，直接修改。",
        "不要讨论，直接重构这个模块。",
        "不用再解释，请创建新文件。",
    ):
        assert agent._has_write_intent(question) is True, (
            f"{question!r} 判成了没有写意图 —— 否定跨过了标点，"
            "把后面明确授权的动词一起吃掉了"
        )


def test_the_negation_reach_is_short_on_purpose():
    """反向守卫：否定与它管的动词之间只允许很短的填充。

    变异实测过：把 filler 预算从 3 字放大，**「别磨蹭了快帮我改一下」就会被吃掉** ——
    否定会越过「磨蹭了快」（4 字）伸到「帮我改」，而那里的「别」否定的是
    「磨蹭」、不是「帮我改」。所以这个上界不是随手定的：

        下界要容得下真实填充 —— 「不需要重新生成」「不许擅自重构」「不要去修改」
        上界不能伸进内容词 —— 「别磨蹭了快帮我改一下」

    预算放大后，否定会一路吃到那个被授权的动词上，整句被判成没意图 —— 于是
    模型在用户明确要求修改时不动手。方向与本族相反，但确实是同一个正则的副作用。
    """
    for question in (
        "不要犹豫太久直接修改吧。",
        "别磨蹭了快帮我改一下。",
        "不要解释太多先创建个文件。",
    ):
        assert agent._has_write_intent(question) is True, (
            f"{question!r} 判成了没有写意图 —— 否定的「射程」被放得太远，"
            "吃掉了后面被明确授权的动词"
        )


def test_dont_forget_is_an_authorization_not_a_negation():
    """反向守卫：**「别忘了 X」是在授权 X**。

    这是第一版修法自己踩的坑：`别` 被当成否定词，filler「忘了」顺手把
    「帮我改」一起吃掉，于是「禁止删除，别忘了帮我改一下 README」这种明明
    授权了的句子被判成没意图 —— 方向与本族相反，但确实是那次改动引入的。
    """
    for question in (
        "别忘了帮我改一下 README。",
        "别忘了把配置重构一下。",
        "别说我没提醒你，帮我改一下。",
        "别急，帮我重写这个文件。",
        "禁止删除，别忘了帮我改一下 README。",
    ):
        assert agent._has_write_intent(question) is True, (
            f"{question!r} 判成了没有写意图 —— 「别忘了」不是否定，"
            "它在授权后面那个动作"
        )


def test_negation_words_are_matched_longest_first():
    """反向守卫：否定词表若把「不」排在「不要」前面，就只吃掉一个字。

    「不要修改」先匹到「不」的话，「要修改」留在原地，照样被判成写意图 ——
    于是这一族最典型的句子会漏过去，而表面上「有否定处理」这个改动看着是对的。
    """
    assert agent._has_write_intent("不要修改任何文件。") is False
    assert agent._has_write_intent("不要创建新文件。") is False
    assert agent._has_write_intent("不许新增功能。") is False
    assert agent._has_write_intent("无需修改。") is False


# --------------------------------------------------------------------------
# 两处判读必须一致
# --------------------------------------------------------------------------


def test_the_clarification_prompt_also_respects_a_prohibition():
    """`_ambiguous_write_request` 会对「能不能改」这类问句回一句
    「可以修改文件，但当前还没有执行任何写操作」。

    如果它不先挖掉被否定的动词，用户说「不要修改，能不能直接改」时，
    我们会把用户的明令禁止**原样顶回去**。
    """
    for question in ("不要修改。", "不要修改，能不能直接改。",
                     "禁止创建，能不能帮我建一个。"):
        reply = agent._ambiguous_write_request(question)
        assert reply == "", (
            f"{question!r} 触发了澄清话术：{reply[:60]!r} —— "
            "它在用户明令禁止时仍然提议「可以修改文件」"
        )


def test_the_clarification_prompt_still_fires_for_real_capability_questions():
    """反向守卫：上面那条不能靠「整个函数都不触发」来通过。"""
    reply = agent._ambiguous_write_request("能不能改一下这个文件？")
    assert reply, "真正的能力问句必须仍然触发澄清话术"
    assert "可以修改文件" in reply, reply[:80]