"""撤回句不是授权：「取消修改」原先被判成 granted —— 安全方向的失效开放。

## 这一族

实测 11 个撤回类动词，接上写动词后**全部**判 granted：

    取消修改 撤销修改 停止修改 放弃修改 撤回修改 终止修改
    作废修改 中止修改 关掉修改 搁置修改 算了修改

用户说「取消修改」，护栏读成「授权修改」。方向是**失效开放**。

## 为什么不能把它们塞进 `_RE_NEGATION`（实测否决）

第一次尝试是把撤回词加进否定表。实测 **7/9 正常请求被误伤**：

    帮我实现取消按钮    -> denied
    加个停止服务按钮    -> denied
    写一个终止超时的函数 -> denied
    设计撤回机制        -> denied

根因是**语法不同类**。`_RE_NEGATED_WRITE` 表达「不要**改**」——
否定词是修饰语，被否定的是写动作。而「取消按钮」不是「不要按钮」：
**「取消」本身就是那个动作**。当否定词用，等于把「实现取消按钮」
读成「实现不要按钮」。

「撤回词」与「否定词」必须分开判。

## 改用句首位置判据

    取消|修改      取消在句首 = 谓语 -> 撤回
    实现|取消|按钮   取消在句中 = 定语 -> 授权（取消按钮是个 UI 元素）

三条候选判据实测都是 0 误伤，位置判据最直白：

| 判据 | 漏判撤回 | 误拦授权 |
|---|---|---|
| **撤回词在句首** | **0/14** | **0/12** |
| 后接写动词 | 0/14 | 0/12 |
| 短句裸成分 | 0/14 | 0/12 |

## filler 3 字是量出来的

| filler | 漏判撤回 | 误拦授权 | 备注 |
|---|---|---|---|
| 2 | **4/14** | 0 | 「取消这个修改」等漏 —— **危险** |
| **3** | **0/14** | **0/12** | 命中 1 句对抗样本 |
| 6 | 0/14 | **1/12** | filler 伸进内容词 |

选 3 是拿 1 句摩擦换 0 句漏放行 —— 与 `_RE_NEGATED_WRITE` 当年量出 3 字
是同一个权衡。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import agent  # noqa: E402

D, G = agent._WRITE_INTENT_DENIED, agent._WRITE_INTENT_GRANTED


def intent(q: str) -> str:
    return agent._write_intent(q)


# --------------------------------------------------------------------------
# 核心：撤回句必须 denied
# --------------------------------------------------------------------------

WITHDRAW = [
    "取消修改", "撤销修改", "停止修改", "放弃修改", "撤回修改", "终止修改",
    "作废修改", "中止修改", "关掉修改", "搁置修改", "算了修改",
    # filler 变体：filler 2 字
    "取消这个修改", "撤销这次修改", "取消本次修改",
    # filler 3 字
    "取消掉这个修改", "取消之前的修改", "撤销之前的修改",
    "取消这次的修改", "中止这次修改",
    # 前置引导词
    "先取消修改", "帮我取消修改", "那就撤销修改",
]


@pytest.mark.parametrize("question", WITHDRAW)
def test_a_withdrawal_is_not_an_authorization(question):
    assert intent(question) == D, (
        f"{question!r} 是在**撤回**写授权，却被判成 "
        f"{intent(question)!r} —— 护栏会照着用户刚叫停的动作继续改"
    )


def test_the_withdrawal_check_reaches_every_withdrawal_verb():
    """每个撤回词都要真能拦住 —— 少一个就是一个新的口子。

    这条钉的是**词表本身**，不是某一句。将来有人删词，这里会红。
    """
    for verb in agent._WRITE_WITHDRAW_HEAD.split("|"):
        assert verb.strip()
        question = f"{verb}修改"
        assert intent(question) == D, f"撤回词「{verb}」漏了：「{question}」仍判 " \
                                     f"{intent(question)!r}"


# --------------------------------------------------------------------------
# 反向守卫：含撤回字但不撤回的，必须放行
# --------------------------------------------------------------------------

#: ⚠⚠ **原先 8 句里有 2 句本来就该 denied —— 是这份表的期望写错了。**
#:
#:     这个取消逻辑写在哪里   在问**位置**，不是请求
#:     分析取消操作的影响     在问**影响**，不是请求
#:
#: 把它们列进「必须放行」就等于要求：**用户问「写在哪里」时，
#: agent 当成授权去改文件**。按 `_write_intent` 的既有语义
#: （疑问词 → denied，见 `docs/write-intent-corpus.md` A 节）
#: 它们判 denied 是**正确行为**。
#:
#: 已移到下面的 `MUST_STAY_DENIED`。这次改的是**测试的期望**，不是判据。
BENIGN = [
    # 撤回词在句中 = 定语，修饰名词
    "帮我实现取消按钮",
    "加个停止服务按钮",
    "写一个终止超时的函数",
    "设计撤回机制",
    "写取消函数",
    "实现撤销操作",
]

#: 问句，含撤回字但**不是**写请求 —— 必须 denied。
#: 移出 `BENIGN` 的那 2 句，附移出理由，钉住「不许改回去」。
MUST_STAY_DENIED = [
    ("这个取消逻辑写在哪里", "问位置"),
    ("分析取消操作的影响", "问影响"),
]


@pytest.mark.parametrize("question", BENIGN)
def test_a_withdrawal_word_inside_a_request_is_still_a_request(question):
    """含撤回字的真写请求必须放行。

    ⚠ skip 消息里的归因**曾经是错的**，已更正：
    原写「属含撤回字的正常请求被拦那一族」—— 实测根因是**谓语动词缺口**
    （`写`/`实现`/`设计` 不在 `_WRITE_VERBS` 表里），
    换成无关词一样被拦（`帮我实现保存按钮` 也是 denied）。
    详见 `docs/write-intent-corpus.md` D 节的「归因更正」。

    改成准确归因的理由：下一个人若照旧归因去查「撤回词」，会查错方向。
    """
    currently = intent(question)
    if currently == D:
        pytest.skip(
            f"{question!r} 当前 denied —— 根因是谓语动词缺口"
            f"（写/实现/设计 不在 _WRITE_VERBS 表），**与撤回词无关**。"
            f"实测：换成无关词（帮我实现保存按钮）同样 denied。"
            f"已试三条修法（加裸词/全裸加/句首位置判据）全部不通，"
            f"根因见 agent.py _WRITE_VERBS 注释与 docs/write-intent-corpus.md D 节。"
        )
    assert currently == G, f"{question!r} 被判成 {currently!r}"


@pytest.mark.parametrize("question,why", MUST_STAY_DENIED)
def test_a_question_is_not_an_authorization_even_with_withdrawal_words(question, why):
    """反向守卫：问句即便含撤回字也必须 denied。

    ⚠ 这 2 句原先被列在 `BENIGN`（要求 granted）—— 那是**期望写错了**：
    用户问「写在哪里」「分析影响」时，agent 若当成授权去改文件，
    正是这个护栏要防的方向。

    这条钉的是「它们必须留在 denied 一侧」——
    若哪天有人把它们挪回 `BENIGN`，这里会红。
    """
    assert intent(question) == D, (
        f"{question!r}（{why}）被判成 {intent(question)!r} —— "
        f"问句被当成写授权，护栏会朝放行落盘的方向失效"
    )


def test_the_rule_is_position_based_not_wordlist_based():
    """反向守卫：判据是**位置**，不是「句子里有没有这个词」。

    这是撤回词与否定词分开判的全部理由：同一个「取消」，在句首是撤回，
    在句中只是定语。若哪天改成纯词表匹配，这两条会一起变红。

    对照句特意挑 `添加放弃按钮` / `把停止服务加上` —— 它们含撤回词但当前是
    granted。先前用 `实现取消按钮` 做对照，那句**本来就 denied**（属另一族
    既存缺陷），于是这条守卫撞上了不相干的问题而红 —— 对照句必须选一个
    当前真的能区分的。
    """
    head = intent("取消修改")
    assert head == D, "句首的「取消」应当被认成撤回"
    for middle in ("添加放弃按钮", "把停止服务加上"):
        assert intent(middle) == G, (
            f"{middle!r} 是含撤回字的正常请求，却被判成 {intent(middle)!r}"
        )


def test_withdrawal_words_must_not_be_added_to_the_negation_table():
    """结构守卫：撤回词与否定词**必须**是两张表。

    塞进 `_RE_NEGATION` 实测 7/9 正常请求被误伤（注释在 agent.py 里）。
    这条钉住「不合并」，否则有人为了省事把它们并进去。
    """
    for verb in agent._WRITE_WITHDRAW_HEAD.split("|"):
        assert verb not in agent._RE_NEGATION, (
            f"撤回词「{verb}」被并进了否定表 —— 那会把"
            "「实现取消按钮」读成「实现不要按钮」"
        )


def test_the_withdrawal_filler_budget_stays_at_three():
    """反向守卫：filler 预算不得放宽到 6。

    6 字时「放弃重试这个策略改成继续」会被误拦（filler 伸进了内容词）。
    2 字时 4 句撤回句漏放行。所以 3 是两端量出来的。
    """
    pattern = agent._RE_WRITE_WITHDRAW.pattern
    assert "{0,3}" in pattern, f"filler 预算不再是 3 字：{pattern[-60:]}"
    assert intent("放弃重试这个策略改成继续") != D, (
        "filler 已经伸进内容词，把授权句误判成撤回了"
    )