"""疑问句带着写动词时，那是在**问**，不是在授权。

## 这一族

`重构` / `修复` / `修改` 都在写动词表里，于是**问句**里带这些动词就被判成授权：

    修复这个 bug 一般要几步          重构这个模块会引入哪些问题
    为什么要修改这里                删除这个字段安全吗
    生成报告大概多久                重构和直接改哪个更好

实测：**10 句这样的问句，改动前全部被判成 granted** —— 护栏在审查类上失效开放，
而审查类正是它最该保护的那一类。

与「裸动词」同族但**根不同**：裸动词只能靠加词解决，而加词只会让误放行更多
（「这个 bug 怎么修？」会跟着被放行）。这一族要靠**判疑问句**解决。

## 关键难点：疑问句 ≠ 没授权

    帮我改一下这个文件，可以吗？      问句，但是请求
    能不能把 a.txt 改成 b？          问句，也是请求（而且给了具体路径与转换）
    删除这个字段安全吗？              问句，但不是请求

所以分三层，各有实测依据：

    能力问句（能不能/能否/可否…）  -> uncertain   早已有，且必须排在最前
    疑问词（为什么/几步/多少/…）   -> denied      9/10 抓住、**0 误伤**
    语气词与句末问号（吗/呢/吧/？）  -> uncertain   同时出现在合法请求里，
                                                  判 denied 会把上一笔修好的
                                                  方向倒回去

再叠一条：**有具体细节（路径 / old→new）时不算拿不准** —— 与能力问句分支同一判据。
否则「能不能把 a.txt 改成 b？」会因为句末问号被降级，那是个明确的请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402

G, D, U = (agent._WRITE_INTENT_GRANTED, agent._WRITE_INTENT_DENIED,
           agent._WRITE_INTENT_UNCERTAIN)


def intent(q: str) -> str:
    return agent._write_intent(q)


# --------------------------------------------------------------------------
# 核心：问工作量 / 问风险 / 问方法 / 问影响，都不是授权
# --------------------------------------------------------------------------

EFFORT_QUESTIONS = [
    "修复这个 bug 一般要几步",
    "看看重构会带来什么风险",
    "重构这个模块会引入哪些问题",
    "为什么要修改这里",
    "修改这个文件会影响什么",
    "创建新文件需要哪些步骤",
    "生成报告大概多久",
    "这个 bug 修复难不难",
    "重构和直接改哪个更好",
    "删除这个文件有什么风险",
    "导出成 csv 怎么实现",
]


@pytest.mark.parametrize("question", EFFORT_QUESTIONS)
def test_asking_about_a_write_is_not_asking_for_one(question):
    assert intent(question) == D, (
        f"{question!r} 是在问代价/风险/方法，不是请求写入，却被判成 "
        f"{intent(question)!r} —— 护栏在审查类上失效开放"
    )


@pytest.mark.parametrize("question", EFFORT_QUESTIONS)
def test_effort_questions_are_also_not_write_authorized_booleans(question):
    assert agent._has_write_intent(question) is False, question


# --------------------------------------------------------------------------
# 反向守卫：疑问形式但确实是请求的，不能被降成 denied
# --------------------------------------------------------------------------


def test_a_capability_question_stays_uncertain_not_denied():
    """能力问句优先级必须最高。

    若问号检查排在它前面，「能不能帮我改一下这个文件？」会从 uncertain 变成
    denied —— 模型被告知「用户没有要求」，而用户其实问的是「能不能」。

    第三句是**为了让顺序可观测**才加的：它同时含能力词（能不能）与疑问词（哪些）。
    只含能力词的句子两种顺序行为完全一样（能力问句里本来就没有疑问词），
    那样这条优先级守卫就是个**永远咬不动的摆设** —— 变异实测过，确实没咬住。
    """
    for question in ("能不能帮我改一下这个文件？",
                     "能否重构这个模块？",
                     "能不能改一下？需要改哪些文件？"):
        assert intent(question) == U, (
            f"{question!r} 判成了 {intent(question)!r}，"
            "能力问句被降级了 —— 检查顺序必须让能力问句优先于疑问词"
        )


def test_a_concrete_request_is_not_demoted_by_a_trailing_question_mark():
    """反向守卫：**有具体细节**的问句是请求，不是拿不准。

    「能不能把 a.txt 改成 b？」带着明确的路径与转换动作。判据与能力问句分支一致：
    够具体就不追问。
    """
    for question in ("能不能把 a.txt 改成 b？",
                     "可不可以把 player.gd 改成 enemy.gd？"):
        assert intent(question) == G, (
            f"{question!r} 给了具体路径与转换动作，是明确的请求，却被判成 "
            f"{intent(question)!r}"
        )


def test_a_hedged_request_becomes_uncertain_rather_than_denied():
    """反向守卫：句末语气词只能降成 uncertain。

    「帮我改一下这个文件，可以吗？」确实是请求，但问法不确定。判 denied 等于
    告诉模型「用户没有要求」—— 那是把我们的不确定说成用户的意思。
    """
    for question in ("帮我改一下这个文件，可以吗？",
                     "可以删除 obsolete 文件吗？"):
        assert intent(question) == U, (
            f"{question!r} 判成了 {intent(question)!r}，"
            "语气词只该降成 uncertain（去问用户），不该断言用户没要求"
        )


def test_plain_imperatives_are_still_granted():
    """反向守卫：问句检测不能变成「一律拦」。"""
    for question in ("请修改 a.txt。",
                     "重构这个模块。",
                     "删除 obsolete 文件。",
                     "修改完之后记得跑测试。",
                     "修改 a.txt 并提交代码。",
                     "帮我把 a.txt 改一改。"):
        assert intent(question) == G, (
            f"{question!r} 是明确的祈使句，却被判成 {intent(question)!r}"
        )


def test_a_sentence_with_only_a_bare_verb_is_not_granted():
    """记录一条**已知限制**：裸动词仍然不认。

    「改完之后记得跑测试」里的「改」是裸形式 —— 上一笔刻意没加（加了就会误放行
    「这个方案怎么改比较好？」）。所以它在「有没有写信号」那一步就落进 denied。

    断言写成「当前是 denied」：那是事实记录，不是认可。词表若补了裸形式，这条会红，
    那正是我们要的 —— 变化必须被看见。

    （我在这轮里第三次把「裸动词句子」误放进「必须 granted」清单里而被自己的守卫
      抓住，所以这里顺手把这件事钉下来。）
    """
    assert intent("改完之后记得跑测试。") == D, (
        "裸动词句子现在被判成 granted —— 词表可能被补了裸形式，"
        "请重新评估它对「这个方案怎么改比较好？」这类问句的影响"
    )


def test_the_question_words_must_not_overlap_with_real_requests():
    """反向守卫：疑问词表与合法请求零重叠。

    这条是「疑问词 -> denied」敢这么判的依据：若哪天有人往疑问词表里塞了一个
    会出现在祈使句里的词，这条会红。
    """
    wh = agent._RE_WRITE_INTENT_QUESTION.pattern
    for word in ("请", "帮我", "把", "一下"):     # 祈使句里高频、绝不能当疑问词
        assert word not in wh, f"疑问词表里混进了祈使句常用词「{word}」"