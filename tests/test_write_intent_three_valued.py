"""写意图判定要有第三档：「拿不准」，而不是只有「有 / 没有」。

## 这一族

二值判定时，任何判定错误都是**硬失败**，而且两个方向都会出事：

    误判「有写意图」  -> 静默放行（用户明令禁止也照写）      e0bdb53 修的那类
    误判「没有写意图」 -> 硬拦（用户要求改也不改）            后果是「模型不听指令」

而本仓库的核心取向是「harness 给模型兜底、解放模型生产力，**不替模型决定**」。
在写意图这件事上，二值判定恰恰是**替用户决定**。加第三档不是为了更聪明，
是为了让**不确定显式化**，交回用户裁决。

参考 opencode：它的权限默认动作是 `ask`（`rulesets.flat().findLast(…) ?? {action:"ask"}`），
即判定不确定时把选择权交回用户。我们没有「问」这个动作，只有拦和放。

## 三档的定义（顺序有讲究）

    denied     没有任何写信号，或写信号被明确否定 -> 拦，告知「你没要求 / 你说别改」
    uncertain  有疑似写信号但判不准（能力问句、条件式说法）-> 拦，但告知「拿不准，你去问」
    granted    有明确且未被否定的写信号 -> 放行

**denied 这一档必须最宽**：审查类问题（「分析一下架构」）落在这里。若把它们也算成
uncertain，每个审查请求都会变成一次提问 —— 那比现在更糟。实测审查类 10 句、明确禁止
4 句，在三条候选信号上**零误触发**。

## 顺带修掉的一处回归

`_has_write_intent` 原先拿 `_write_intent_text()` 处理**之后**的文本去匹配
`_WRITE_CAPABILITY_QUESTION` —— 而那个处理恰好把「能不能」这类词遮掉了，于是该分支
成了死代码。它靠 `run()` 开头那次 `_ambiguous_write_request` 早退掩盖着，一次条件
变更就会变成活的口子。现在能力问句拿**原文**判。
"""

from __future__ import annotations

import json
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
# denied：最宽的一档，审查类必须落在这里
# --------------------------------------------------------------------------

REVIEW_QUESTIONS = [
    "请审查这份报告",
    "这个实现有什么问题",
    "分析一下架构",
    "看一下这个函数怎么写的",
    "解释一下这段代码",
    "这个 bug 是什么原因",
    "列出所有 TODO",
    "帮我看看这个报错",
    "评估一下这个方案的风险",
    "梳理一下调用链",
    "按步骤执行。",
]

PROHIBITIONS = [
    "不要修改任何文件。",
    "请不要修改任何文件，先把问题列出来。",
    "这次不要提交代码。",
    "禁止删除任何东西。",
    "只读分析，不要动文件。",
    "无需修改。",
]


@pytest.mark.parametrize("question", REVIEW_QUESTIONS)
def test_review_questions_are_denied_not_uncertain(question):
    """反向守卫：审查类**绝不能**变成 uncertain。

    那会让每个审查请求都变成一次「你要我改吗？」——把「更礼貌」换成「更没用」。
    """
    assert intent(question) == D, (
        f"{question!r} 被判成 {intent(question)!r}，应当是 denied —— "
        "审查类问题一旦落进 uncertain，每次都会被追问要不要改"
    )


@pytest.mark.parametrize("question", PROHIBITIONS)
def test_explicit_prohibitions_are_denied(question):
    assert intent(question) == D, f"{question!r} 判成了 {intent(question)!r}"


# --------------------------------------------------------------------------
# granted
# --------------------------------------------------------------------------

GRANTED = [
    "请修改 a.txt。",
    "创建 player.gd。",
    "重构这个模块。",
    "帮我改一下 README。",
    "把这个函数改成异步。",
    "别忘了帮我改一下 README。",
    "不要修改文档，直接重写整个实现。",
    "能不能把 a.txt 改成 b？",
    # 含「之后 / 以后 / 然后」但**没有**「先…再/才」配对 —— 必须仍然是授权
    "改完之后记得重构一下。",
    "修复之后把测试补上。",
    "先看下现有实现，然后重构这个模块。",
]


@pytest.mark.parametrize("question", GRANTED)
def test_clear_write_requests_are_granted(question):
    assert intent(question) == G, (
        f"{question!r} 判成了 {intent(question)!r}，用户已明确要求写入却被拦住"
    )


# --------------------------------------------------------------------------
# uncertain
# --------------------------------------------------------------------------

UNCERTAIN = [
    # 能力问句：问「能不能」不是授权
    "能不能改一下这个文件？",
    "能否帮我修改一下",
    "可不可以重构这个模块？",
    # 条件式：现在还没授权落盘
    "先不要改代码，让我确认方案再修改。",
]


@pytest.mark.parametrize("question", UNCERTAIN)
def test_unclear_authorization_is_uncertain(question):
    assert intent(question) == U, (
        f"{question!r} 判成了 {intent(question)!r}，应当是 uncertain —— "
        "这种要交给用户裁决，不该由我们替他决定"
    )


def test_the_conditional_signal_may_span_one_comma():
    """「先不要改代码，让我确认方案再修改」——「先」与「再」之间有逗号。

    跨不过这个逗号就抓不到，而这是极常见的说法。
    （这与「否定不许跨标点」不矛盾：那讲的是**否定作用域**，条件式的作用域本来跨小句。）
    """
    assert intent("先不要改代码，让我确认方案再修改。") == U
    # 这两句的动词必须在 `_RE_WRITE_INTENT` 里，否则判定在「有没有写信号」那一步
    # 就结束了 —— 我第一版写的是「先不要改代码，然后再改」，裸「改」不在词表，
    # 于是它压根没走到条件式分支，我却拿它当条件式的用例。**是我的用例错了。**
    assert intent("先不要重构，然后再重构。") == U
    assert intent("等我确认下再修改。") == U


def test_a_trailing_after_is_not_enough_to_be_uncertain():
    """反向守卫：「之后 / 以后 / 然后」单��出现不构成条件式。

    「改完之后记得重构一下」是明确授权。把它判成 uncertain，等于让用户把一句
    清楚的话再说一遍。
    """
    for question in ("改完之后记得重构一下。",
                     "修复之后把测试补上。",
                     "先看下现有实现，然后重构这个模块。"):
        assert intent(question) == G, (
            f"{question!r} 被判成 {intent(question)!r} —— "
            "「之后/然后」单独出现不构成条件式"
        )


# --------------------------------------------------------------------------
# 兼容层与护栏本体
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 护栏本体：判定函数对了还不够，真正执行它的门也要对
# --------------------------------------------------------------------------

class _ScriptedLLM:
    """按脚本逐轮返回文本协议回复（照 tests/test_agent.py 的现成模式）。"""

    provider = "p"
    model = "m"

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = 0

    def clone(self):
        return self

    def chat(self, messages, stream=True, **kwargs):
        i = min(self.calls, len(self._scripts) - 1)
        self.calls += 1
        return [self._scripts[i]]

    def count_tokens(self, text):
        return 0


def _act(tool, inp):
    return f"Thought: t\nAction: {tool}\nAction Input: {inp}"


_FINAL = "Thought: t\nFinal Answer: 已按你的要求处理。"


def _drive(question: str, monkeypatch=None):
    """真跑一遍主循环，让模型尝试写文件，返回 (是否执行了写工具, 事件列表)。"""
    import agent as agent_mod
    from config import set_runtime

    called: list[str] = []

    def recorder(arg):
        called.append(arg)
        # 带可解析路径，写后自验证才会认领这个目标；fake 校验器给 passed。
        return "已写入 a.txt"

    old_tools = agent_mod.TOOLS
    old_self_verify = agent_mod._SELF_VERIFY_ENABLED
    try:
        agent_mod._SELF_VERIFY_ENABLED = True
        agent_mod.TOOLS = {
            "apply_edit": {"func": recorder},
            "read_file": {"func": lambda _a: "内容"},
            "self_verify": {"func": lambda _a: json.dumps(
                {"ran": ["syntax"], "failures": [], "skipped": []})},
        }
        a = agent_mod.Agent(llm=_ScriptedLLM([_act("apply_edit", "path: x"),
                                                _FINAL]))
        events = list(a.run(question, stream=True))
        return called, events
    finally:
        agent_mod.TOOLS = old_tools
        agent_mod._SELF_VERIFY_ENABLED = old_self_verify
        set_runtime("code_root", "")


def test_the_unclear_request_now_goes_through():
    """「留地板、拆其余」：顶层写意图护栏已拆，判不准的写请求也直接放行。

    旧策略里 uncertain 会被拦并回「拿不准」。现在这道门整个移除，写工具默认执行；
    灾难地板改由 tools.py 的路径/凭据/系统目录硬拦兜底，不在这条链上。
    """
    called, events = _drive("先不要修改代码，然后再修改。")
    assert called, "护栏已拆：写工具应放行"
    obs = [e for e in events if e.get("type") == "observation"]
    assert not any("拿不准" in str(e.get("text", "")) for e in obs), (
        "护栏已拆，不该再出现「拿不准」拦截说明")


def test_a_prohibition_now_goes_through():
    """「留地板、拆其余」：明令禁止的措辞也不再由聊天护栏拦截（地板在 tools.py）。"""
    called, events = _drive("不要修改任何文件。")
    assert called, "护栏已拆：写工具应放行"
    obs = [e for e in events if e.get("type") == "observation"]
    assert not any("没有要求" in str(e.get("text", "")) for e in obs)


def test_the_guardrail_still_lets_a_clear_request_through():
    """反向守卫：**门不能变成无差别拦截。**

    这条守卫是被一次真实事故逼出来的：我把条件从
    `if (是写工具 and not 有写意图)` 改成两层 `if` 之后，原来位于 if **体内**的
    `continue` 绑定到了**内层** if，于是**每一次**写工具调用都被跳过 —— 连明确
    授权的也被跳过。当时我先怀疑是沙箱跑不起来，**把那本该抓住它的守卫删了**。

    删测试来让套件变绿，正是这个仓库反复警告的错法：测试环境问题和代码缺陷混在
    一起时，要先查清是哪一种。真相是 dev_eval 的三道 patch 门禁题红了才暴露出来。
    """
    called, _events = _drive("请修改 a.txt。")
    assert called == ["path: x"], (
        f"明确授权的写请求没有被执行（工具调用记录 {called}）—— "
        "门变成了无差别拦截：三值设计最容易出的错就是过度保守"
    )


def test_a_review_question_now_goes_through():
    """「留地板、拆其余」：审查类措辞的写工具调用不再被拦（护栏已移除）。"""
    called, _events = _drive("分析一下架构")
    assert called, "护栏已拆：写工具应放行"


def test_a_capability_question_now_reaches_the_model():
    """「留地板、拆其余」重构：能力问句门已从 run() 聊天路径拆掉。

    旧行为：`run()` 开头用 `_ambiguous_write_request` 正则认出"能不能改文件？"这类
    能力问句，在调模型之前就弹澄清、标 degraded（实测 llm 轮数 = 0）。

    新行为：那道门靠正则猜意图、把一句正常的话挡在模型之前，是用户抱怨的"处处受限"。
    拆掉后，能力问句照常进模型 —— 脚本化模型发出 apply_edit，写工具就执行了。
    真拿不准时由模型自己决定要不要反问，而不是 harness 用正则替它决定。
    （函数 `_ambiguous_write_request` 保留，直接调用它的测试仍在钉判据。）
    """
    called, events = _drive("能不能改一下这个文件？")
    assert called == ["path: x"], (
        f"能力问句门已拆，应交给模型并执行写工具，实际 called={called}")
    assert events and events[-1].get("type") == "final", events[-3:]
    assert not events[-1].get("clarification_required"), events[-1].get("text", "")[:80]
    assert not events[-1].get("degraded"), events[-1].get("text", "")[:80]


def test_the_boolean_projection_only_accepts_granted():
    """反向守卫：布尔层仍是「只认明确授权」，二值时代的调用方行为不变。"""
    assert agent._has_write_intent("请修改 a.txt。") is True
    assert agent._has_write_intent("能不能改一下这个文件？") is False
    assert agent._has_write_intent("先不要改代码，让我确认方案再修改。") is False
    assert agent._has_write_intent("分析一下架构") is False


def test_denied_and_uncertain_must_speak_differently():
    """**核心守卫**：两种拦截不能共用一句话。

    对「拿不准」说「用户没有要求」，是把我们的判不准说成用户的意思 —— 那是这一族
    本体的近亲：把不知道的说成知道了。
    """
    assert agent._WRITE_BLOCKED_OBS != agent._WRITE_UNCERTAIN_OBS
    assert "没有" in agent._WRITE_BLOCKED_OBS, agent._WRITE_BLOCKED_OBS[:60]
    assert "没有" not in agent._WRITE_UNCERTAIN_OBS, (
        "「拿不准」的说法里出现了「没有要求」—— 那是在替用户下结论"
    )
    assert "确认" in agent._WRITE_UNCERTAIN_OBS, (
        "「拿不准」的说法必须指引模型去问用户，而不是让它自行推测"
    )


def test_the_capability_check_reads_the_unmasked_text():
    """反向守卫：能力问句必须拿**原文**判。

    `_write_intent_text()` 会把「能不能」这类词遮掉，在遮罩后的文本上搜等于搜一个
    已经不存在的东西 —— 那个分支曾经是死代码，靠 `run()` 开头的早退掩盖着。
    这里用行为钉住：能力问句必须落进 uncertain（而不是被当成明确授权放行）。
    """
    for question in ("能不能改一下这个文件？", "能否帮我修改一下",
                     "可不可以重构这个模块？"):
        assert intent(question) == U, (
            f"{question!r} 判成了 {intent(question)!r} —— "
            "如果这里读的是遮罩后的文本，这个分支就永远不会执行"
        )


# --------------------------------------------------------------------------
# 已知限制（词表缺口，不是这一族）
# --------------------------------------------------------------------------


def test_a_conditional_sentence_with_an_unlisted_verb_falls_back_to_denied():
    """记录一条**已知限制**，别让它以后悄悄变好或变坏而没人知道。

    「先看看这个 bug，确认之后再修」里的「修」是裸形式、不在 `_RE_WRITE_INTENT`，
    所以判定在「有没有写信号」那一步就结束，落进 denied，而不是 uncertain。

    这是**词表缺口**（另一族：动词表只有「动手改」没有「动手修」、没有裸「改」），
    不是条件式逻辑的问题。补上词表之后，这两句会自动变成 uncertain。

    断言写成「当前是 denied」而不是「应该是 uncertain」—— 前者是事实，后者是愿望；
    这一族只负责把事实钉住。
    """
    for question in ("先看看这个 bug，确认之后再修。", "等我确认下再改。"):
        assert intent(question) == D, (
            f"{question!r} 现在判成 {intent(question)!r} —— "
            "若词表已补上，这里应当变成 uncertain；这条断言就是为了让变化被看见"
        )