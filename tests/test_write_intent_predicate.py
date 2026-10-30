"""谓语动词缺口：用户说「加个打开文件的按钮」，agent 不动手。

## 这一族与「撤回句」**不是同一族**

曾把 `帮我实现取消按钮`、`设计撤回机制` 这批句子记成「含取消/回滚/终止字的
正常请求被拦」。**那个归因是错的。**

逐句查了它倒在哪一步：否定没挖掉任何东西、撤回规则没命中、疑问词没命中、
能力问句没命中。它落在**第 2 步「没有任何写信号 -> denied」** —— 因为这些句里
**一个写动词都没有**：`实现`/`写`/`加`/`设计` 全都不在词表里。

证据很简单：把「取消」换成完全不相关的词，一样被拦：

    帮我实现取消按钮   denied
    帮我实现保存按钮   denied     <- 与「取消」无关
    写一个读取文件的函数 denied    <- 纯名词性，没有取消字
    帮我实现导出按钮   granted    <- 只因「导出」在表里

所以这是**谓语动词缺口**，与撤回句、与整族写意图判定都无关。

## 方向：过拦

不会误改文件，代价是模型不动手。症状表现为「模型不听指令」。

## 实测（`MUST_STAY_DENIED` 22 句 + `NEWLY_FIXED` 15 句真语料，不自编）

    +加|设计|构建|搭建         守卫误放行 0/22  破坏合法 0/15  目标放行  6/15
    +加|设计|构建|搭建|实现    守卫误放行 0/22  破坏合法 0/15  目标放行 10/15
    再 +写                     守卫误放行 2/22 ✗

**裸「写」不能加**：它在 22 句必须 denied 里出现 5 次 ——
「看一下这个函数怎么写的」「为什么这里会写错？」「分析一下写操作和读操作的区别」
「列出所有写工具」「写操作失败了会怎样」。加了必然误伤。

    再 +写                     守卫误放行 2/22 ✗
    +写[一个段份些篇首封]|写下  守卫误放行 0/22   vibecoding 目标放行 6/6 ✓

上面那 5 句的「写」全是**名词/疑问**用法；真正的祈使写法几乎总带量词
（写一个 / 写个 / 写一段 / 写一份 / 写下），所以给「写」加的是**量词作用域**
而不是词本身。How-to 问句（「怎么写一个……」）由疑问词分支兜住。

**裸「实现」也不加**：「这个实现有什么问题」「当前的移动策略是怎么实现的」
—— 名字像动词，但是名词/副词用法。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import agent  # noqa: E402

G, D = agent._WRITE_INTENT_GRANTED, agent._WRITE_INTENT_DENIED

# 这些句子用到的谓语动词，全部要在表里
PREDICATE = ["加", "构建", "搭建"]


def intent(q: str) -> str:
    return agent._write_intent(q)


@pytest.mark.parametrize("word", PREDICATE)
def test_the_predicate_verb_is_in_the_vocabulary(word):
    """反向守卫：这批谓语动词必须在表里。

    钉的是**词表内容**，不是某一句 —— 少一个就是一个新的「模型不听指令」口子。
    """
    assert word in agent._WRITE_VERBS, (
        f"谓语动词「{word}」不在词表里 —— 「{word}这个项目」这类句子"
        "会在「没有任何写信号」那一步就 denied"
    )


@pytest.mark.parametrize("question", [
    "加个停止服务按钮",
    "构建这个项目",
    "搭建一个服务",
    "加个打开文件的按钮",
])
def test_a_request_with_only_a_predicate_verb_is_still_a_request(question):
    assert intent(question) == G, (
        f"{question!r} 是明确的写请求（谓语动词在词表里），却被判成 "
        f"{intent(question)!r} —— 表现为「模型不听指令」"
    )


def test_the_added_verbs_do_not_swallow_the_bare_noun_traps():
    """反向守卫：新词不得把名词/副词用法一起放行。

    与裸「写」是同一类陷阱：词形像动词，但不是授权。加入裸「写」时 22 句守卫
    实测漏 2 句，所以这里把最危险的那 5 句全钉住。

    判据是**词表项**而不是子串 —— 用 `in` 对整个 `_WRITE_VERBS` 字符串做子串
    判断会误判：「重写」这个已存在的词条里含「写」，会让「裸写不在表里」这条假红。
    """
    entries = agent._WRITE_VERBS.split("|")
    assert "写" not in entries, (
        "裸「写」进了词表 —— 下面 5 句会被误放行"
    )
    for question in ("分析一下写操作和读操作的区别",
                     "列出所有写工具",
                     "看一下这个函数怎么写的",
                     "为什么这里会写错？",
                     "写操作失败了会怎样"):
        assert intent(question) == D, f"{question!r} 被放行了"


def test_the_verbs_are_nouns_in_review_context():
    """反向守卫：「这个实现有什么问题」必须仍是 denied。

    裸「实现」不能加的理由 ——「实现」在这两句里是名词/副词。
    """
    for question in ("这个实现有什么问题",
                     "当前的移动策略是怎么实现的"):
        assert intent(question) == D, f"{question!r} 被放行了"


def test_design_stays_out_because_of_a_negation_scope_collision():
    """反向守卫：**「设计」不得进词表** —— 实测它撞了否定作用域。

    「不必重构，只要解释清楚设计。」：用户明说「不必」，而「设计」落在逗号之后、
    不在「不必」的 3 字作用域内，于是整句被判成有写意图，护栏会**放行落盘**。
    这是安全方向的缺陷。

    加它能修好 2 句（「设计撤回机制」「设计这个方案」），但代价是这一条。
    拿 1 条放行落盘的缺陷换 2 句过拦改善，不划算。

    这条守卫的作用是：**别让下一个人顺手把「设计」加回来**。
    """
    assert "设计" not in agent._WRITE_VERBS.split("|"), (
        "「设计」进词表了 —— 「不必重构，只要解释清楚设计。」会被判成有写意图，"
        "护栏会在用户明说「不必」时放行落盘"
    )
    assert intent("不必重构，只要解释清楚设计。") == D, (
        "否定 + 新词的跨分句形状破了 —— 用户明说不必，护栏却放行"
    )


def test_write_plus_a_quantifier_is_now_a_request_and_that_is_recorded():
    """记录**已修**：「写 + 量词」现在放行，裸「写」仍不认。

    2026-10-15 真机 vibecoding 实测：
    「帮我在 vibe_test/ 下写一个单文件 HTML 贪吃蛇」被 `create_file` 拦死
    （`[安全拦截] 用户没有要求创建或修改文件`）—— 这是 vibecoding 最高频的说法，
    卡死的是整个主用法。

    裸「写」仍**不能**进表（22 句必须 denied 里它出现 5 次，全是名词/疑问用法）；
    改成带作用域的正则 `写[一个段份些篇首封]|写下`：
        守卫误放行 0/22    目标句漏放行 0/6
    How-to 问句（「怎么写一个这样的功能？」）由疑问词分支兜住，仍 denied。

    2026-10-30 补上了**上下文模式**：不带量词的「写取消函数」也放行，同时
    「写操作 / 写工具 / 写在哪里 / 怎么写的」等名词/疑问用法仍 denied。
    """
    assert intent("写一个读取文件的函数") == G, (
        "「写 + 量词」退回去了 —— vibecoding 最高频说法会重新被 create_file 拦死"
    )
    assert intent("写取消函数") == G, (
        "带对象的裸「写」仍被拦 —— 检查 _WRITE_VERBS 里的上下文排除模式"
    )


def test_contextual_predicates_cover_real_requests_without_noun_traps():
    """实现/设计/写 的上下文模式：真实写请求放行，名词/疑问陷阱不放行。

    方向是「过拦」，所以放行这几句不会误改文件；但名词陷阱若被放行，护栏会
    在审查/提问时允许落盘。
    """
    for question in ("帮我实现取消按钮", "设计撤回机制",
                     "写取消函数", "实现回滚功能"):
        assert intent(question) == G, f"{question!r} 是明确写请求，仍被拦"
    for question in ("这个实现有什么问题", "当前的移动策略是怎么实现的",
                     "分析一下写操作和读操作的区别", "列出所有写工具",
                     "看一下这个函数怎么写的", "解释清楚设计。",
                     "评估这个设计方案"):
        assert intent(question) == D, f"{question!r} 是名词/疑问陷阱，被放行了"


def test_the_quantifier_pattern_does_not_leak_into_howto_questions():
    """反向守卫：「怎么写一个……」是提问，不是授权。

    「写 + 量词」这条正则是按**子串**命中的，How-to 句子里同样含「写一个」，
    必须靠疑问词分支压住，否则「怎么写一个这样的功能？」会变成放行落盘。

    判据是**「不得放行」**而不是「必须判 denied」：这些句子会因句首的疑问/商量
    词（`怎么`/`如何`/`能不能`）先落进三值判定的 `uncertain` 档，而 `uncertain`
    在护栏里走的是**同一条拦截分支**（`_WRITE_UNCERTAIN_OBS`），只是给的文案是
    「拿不准用户是否授权」。写操作一样没执行 —— 所以钉 `!= G` 才是真判据。
    """
    for question in ("怎么写一个这样的功能？",
                     "如何写一个读取文件的函数？",
                     "能不能讲讲怎么写一个插件"):
        assert intent(question) != G, f"{question!r} 被放行落盘了"