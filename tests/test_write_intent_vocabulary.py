"""词表缺口：合法的写请求不该被护栏拦死，审查类问题不该被放行。

## 这一族

`_RE_WRITE_INTENT` 是纯动词表，而护栏「没有写意图就别写」只看这张表命中与否。
于是两个方向都会出事，**这一族只修「合法的写请求被拦死」这个方向**：

    「只改 b.py」        「别动 a.py，只改 b.py」   「动手修。」
    「修一下这个函数。」   「改下 README。」           「帮我把 a.txt 改一改。」

实测：**14 句这样的合法写请求，全部被判成 denied** —— 再明确不过的请求被护栏拦死，
表现为「模型不听指令」。

英文侧同样稀薄：原先只有 `fix / refactor / create / generate / export`，
`edit / delete / remove / update / rename / move / insert / patch` 全缺。
连 `delete the obsolete file` 这种都不被认出来。

## 为什么不是「把裸动词都加上」

裸形式一旦进表，**审查类问题会跟着被放行**。逐个候选实测（收益 / 代价）：

| 候选 | 修好 | 误放行 | 被误放行的句子 |
|---|---|---|---|
| 裸「修」 | 3 | **1** | 这个 bug 怎么修？ |
| 裸「改」 | 4 | **2** | 这个方案怎么改比较好？／如果我改了配置会生效吗 |
| 裸「动」 | 3 | **2** | 解释一下移动端的适配情况／当前的移动策略是怎么实现的 |
| 裸「写」 | 0 | **5** | 看一下这个函数怎么写的／分析一下写操作和读操作的区别／… |
| 「应用」 | 1 | **2** | 这个模式的应用场景有哪些？／说说状态管理方案的应用方式 |

代价大于收益的一律不进表 —— 护栏失效在**审查类**上，那是最不该失效的一类。
「应用」那条是补了名词陷阱句之后才发现有代价的；不补那句就会按「+1 收益 0 代价」
把它加进去。
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
# 补上的词：每一条都有一句实测判错过的真实写请求
# --------------------------------------------------------------------------

NEWLY_FIXED = [
    ("只改", "只改 b.py，a.py 别动。"),
    ("只改", "别动 a.py，只改 b.py。"),
    ("动手修", "动手修。"),
    ("修一下", "修一下这个函数。"),
    ("修好", "修好这个 bug。"),
    ("改下", "改下 README。"),
    ("改一改", "帮我把 a.txt 改一改。"),
    ("edit", "edit config.json to use the new key"),
    ("delete", "delete the obsolete file"),
    ("remove", "remove the unused import"),
    ("update", "update the README"),
    ("rename", "rename player.gd to enemy.gd"),
    ("move", "move utils.py into core/"),
    ("insert", "insert a guard clause at the top"),
    ("patch", "patch the config file"),
]


@pytest.mark.parametrize("word,question", NEWLY_FIXED,
                         ids=[w for w, _ in NEWLY_FIXED])
def test_a_real_write_request_is_no_longer_blocked(word, question):
    assert agent._has_write_intent(question) is True, (
        f"{question!r} 是明确的写请求（用到「{word}」），却被判成没有写意图 —— "
        "护栏会把它拦死，表现为「模型不听指令」"
    )


# --------------------------------------------------------------------------
# 反向守卫：审查/问答一个都不能被放行（含名词陷阱）
# --------------------------------------------------------------------------

MUST_STAY_DENIED = [
    "请审查这份报告",
    "这个实现有什么问题",
    "分析一下架构",
    "解释一下这段代码",
    "这个 bug 是什么原因",
    "列出所有 TODO",
    "帮我看看这个报错",
    "评估一下这个方案的风险",
    "梳理一下调用链",
    "按步骤执行。",
    # 裸动词 + 疑问语气：问的是「怎么做」，不是授权
    "这个 bug 怎么修？",
    "这个方案怎么改比较好？",
    "如果我改了配置会生效吗",
    # 名词用法：这些词长得像动词，但不是授权
    "看一下这个函数怎么写的",
    "为什么这里会写错？",
    "分析一下写操作和读操作的区别",
    "列出所有写工具",
    "写操作失败了会怎样",
    "解释一下移动端的适配情况",
    "当前的移动策略是怎么实现的",
    "这个模式的应用场景有哪些？",
    "说说状态管理方案的应用方式",
]


@pytest.mark.parametrize("question", MUST_STAY_DENIED)
def test_review_and_question_phrases_stay_denied(question):
    assert agent._has_write_intent(question) is False, (
        f"{question!r} 是审查/问答，不是写授权，却被判成有写意图 —— "
        "护栏在**最该保护的那一类**上失效了"
    )


def test_bare_verbs_stay_out_of_the_vocabulary():
    """反向守卫：裸形式不得进表（逐条实测的代价见模块 docstring）。

    这里钉的是**行为**而不是词表内容：这些句子必须仍然是 denied。
    """
    for question, bare in (("这个 bug 怎么修？", "修"),
                           ("这个方案怎么改比较好？", "改"),
                           ("解释一下移动端的适配情况", "动"),
                           ("看一下这个函数怎么写的", "写"),
                           ("这个模式的应用场景有哪些？", "应用")):
        assert agent._has_write_intent(question) is False, (
            f"裸形式「{bare}」似乎被加进词表了 —— "
            f"{question!r} 因此被当成了写授权"
        )


def test_asking_about_effort_is_not_an_authorization():
    """已知限制（**既存缺陷**，另一族）：已列在表里的动词也会被问句带偏。

    「修复这个 bug 一般要几步」「看看重构会带来什么风险」都被判成 granted ——
    因为 `修复` / `重构` 就在词表里。这与裸形式同族但根不同：要判「疑问句 vs
    祈使句」，而不是往表里加词。

    断言写成「**当前是 granted**」—— 那是事实记录，不是认可。将来有人修了它，
    这条会红，那正是我们要的：变化必须被看见。
    """
    for question in ("修复这个 bug 一般要几步", "看看重构会带来什么风险"):
        assert agent._has_write_intent(question) is True, (
            f"{question!r} 现在判成 denied —— 「疑问句 vs 祈使句」那一族被修了，"
            "请把这条记录改成断言它应当 denied 的形式"
        )


def test_the_new_words_do_not_swallow_the_denial_vocabulary():
    """反向守卫：新词不能把否定作用域破坏掉。

    「只改」进了表，于是「不要修改」这类句子里若出现「只改」必须有区分度 ——
    这里只钉最直接的一条：否定句仍然优先。
    """
    for question in ("不要只改 b.py，a.py 也别动。",):
        assert agent._has_write_intent(question) is False, (
            f"{question!r} 被判成了写授权"
        )