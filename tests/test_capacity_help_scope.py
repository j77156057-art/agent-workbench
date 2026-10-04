# -*- coding: utf-8 -*-
"""额度咨询短路必须看**用户/任务自己的话**，不是整段提示词里恰好出现的词。

## 实测事故（五条同一个根因）

`tests/test_workflow_step_budget.py::NavFreeStepsTests` 四条 +
`tests/test_agent.py::test_planner_cannot_widen_tools_or_enable_mcp`：

    status = ok    steps = 0    工具调用 0 次
    conclusion = "这是本地能力说明，不会消耗模型 Token。…四项默认都是 0＝不限。"

模型**根本没被调用**，而 `status` 是 `ok` —— 上游因此认为子代理成功了。
**这比失败更糟：它静悄悄地什么都没做。**

## 命中的不是任务文本

「勘察目录与文件结构」本身**不触发**任何关键词。命中的是它前面那段角色说明：

    你是【任务拆解与分工规划专员】：…决定是否以及**如何**派发后续执行**代理**。
    工具策略：MCP=deny；允许工具=read_file, list_dir

    子任务：勘察目录与文件结构

`如何 … 代理` 命中 `_CAPACITY_HELP_RE` 的第二个分支。**每个 `planner` 子代理必然
命中** —— 不是偶发。

## 为什么不用「是不是子代理」划线

试过 `is_subagent`，它废掉了一条**真实能力**：
`test_capacity_help_is_available_after_task_token_budget_is_exhausted`
刻意用 `depth=1` 要求「共享任务预算耗尽后，子代理仍要能回答额度问题」。

那是真需求。子代理也会被问到额度（上游预算耗尽后让它自查），那时它同样需要
一条**不消耗模型**的答复——预算已经耗尽，走模型只会再失败一次。

**判据应该是「这是不是一句真问句」，而不是「谁在问」。**

## 与 `_user_question` 是同一件事

`/api/chat` 在用户问题前拼【系统提示】，`_user_question` 用 `用户问题：` 剥掉，
护栏文案只回带真实用户问题。子代理拼的是**角色说明**，标记是 `子任务：`。
同一个模式、同一个理由，本文件照它扩展。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402

PLANNER_PROMPT = (
    "你是【任务拆解与分工规划专员】：先检查项目目录、相关文件和依赖边界，"
    "把总目标拆成互不冲突、可验收的子任务，并为每个任务给出角色、依赖、工具和验收标准。"
    "只做分析和规划，不修改文件，不替主 Agent 执行实现。"
    "输出应是有限长度的任务清单或 JSON，供主 Agent 决定是否以及如何派发后续执行代理。\n"
    "工具策略：MCP=deny；允许工具=read_file, list_dir\n\n"
    "子任务：勘察目录与文件结构"
)

RESEARCHER_PROMPT = (
    "你是【检索专员】：只读代码与资料，产出可核验的结论。"
    "需要时可**查看**项目配置与预算上限说明。\n\n"
    "子任务：用分段读取策略提取 behaviors/player.gd 关键逻辑"
)


class ChildTaskTextTests(unittest.TestCase):
    def test_it_strips_the_role_preamble(self):
        self.assertEqual(agent._child_task_text(PLANNER_PROMPT), "勘察目录与文件结构")

    def test_a_plain_question_is_returned_unchanged(self):
        self.assertEqual(agent._child_task_text("怎么提高 token 上限？"),
                         "怎么提高 token 上限？")

    def test_it_takes_the_last_marker_when_several_appear(self):
        text = "子任务：旧的\n更多上下文\n子任务：真正的任务"
        self.assertEqual(agent._child_task_text(text), "真正的任务")


class RolePreamblesNoLongerTripTheGateTests(unittest.TestCase):
    """角色说明里的固定措辞不该触发额度咨询短路。"""

    def test_the_planner_preamble_alone_does_not_match(self):
        """实测根因：`如何 … 代理` 命中了第二个分支。"""
        preamble = PLANNER_PROMPT.split("子任务：")[0]
        self.assertTrue(
            agent._CAPACITY_HELP_RE.search(preamble),
            "这条前置断言是本文件的全部前提：角色说明**确实**会命中。"
            "若将来正则收窄到不再命中，本文件就失去了意义——那时应删掉它，"
            "而不是让它静默变成空跑")
        self.assertIsNone(agent._capacity_help_response(PLANNER_PROMPT),
                          "planner 子代理被短路成帮助页——工具调用 0 次、status=ok")

    def test_the_researcher_preamble_also_does_not_trip_it(self):
        self.assertIsNone(agent._capacity_help_response(RESEARCHER_PROMPT))

    def test_a_normal_child_task_is_not_a_capacity_question(self):
        self.assertIsNone(
            agent._capacity_help_response(PLANNER_PROMPT + "子任务：逐文件核对"))


class RealCapacityQuestionsStillWorkTests(unittest.TestCase):
    """**反过来也必须成立**——不能把这扇门焊死。"""

    def test_a_capacity_question_still_matches_after_the_preamble(self):
        text = PLANNER_PROMPT + "子任务：怎么提高 token 上限？"
        self.assertIsNotNone(
            agent._capacity_help_response(text),
            "子代理真被问到额度时也该零 token 回答——"
            "预算耗尽后走模型只会再失败一次")

    def test_a_subagent_asking_directly_still_gets_help(self):
        """预算耗尽后的应答能力是**子代理**的刚需，不是可选。"""
        self.assertIsNotNone(agent._capacity_help_response("怎么提高 token 上限？"))

    def test_a_plain_user_question_still_matches(self):
        for question in ("怎么提高 token 上限？", "预算还剩多少？",
                         "子代理步数上限是多少"):
            with self.subTest(question=question):
                self.assertIsNotNone(agent._capacity_help_response(question),
                                     "真问句被误伤了")

    def test_the_api_chat_preamble_is_still_stripped(self):
        """`_user_question` 的既有能力不能因为这次改动而失效。"""
        text = "【系统提示】步骤数上限怎么调\n用户问题：预算还剩多少？"
        self.assertIsNotNone(agent._capacity_help_response(text))


class OrdinaryWorkNeverLooksLikeACapacityQuestionTests(unittest.TestCase):
    def test_a_handful_of_real_tasks(self):
        for question in (
                "请检查项目代码并修复这个报错",
                "读 behaviors/player.gd 与 behaviors/enemy.gd，输出机制要点",
                "实现一个 dash 组件并补测试",
                "重构检索模块，把 dense 腿换成稀疏实现",
                "排查视口跟随抖动的问题",
        ):
            with self.subTest(question=question):
                self.assertIsNone(agent._capacity_help_response(question),
                                  "%r 被误判成额度咨询——会白跑一轮模型" % question)


class KnownOverBreadthIsPinnedRatherThanHiddenTests(unittest.TestCase):
    """正则**仍然**过宽的一处：如实钉住，不假装它不存在。

    写这条时我第一版把它当��「普通开发任务」放进了上面那个用例，实测它**确实**
    命中——因为它字面就是在「调高 token 上限」。那是我的期望写错了。

    但它暴露一个真实边界：**一句话里只要提到「调高 token 上限」，整句就被当成纯
    额度问句**，而它后半句「然后跑一遍测试」是真正的活儿。于是模型不会被调用，
    用户只拿到一张帮助页。

    这与本次修的**不是同一个缺陷**：那次是**角色说明**里的固定措辞必然命中
    （每个 planner 都中），这次是**用户自己**把两件事写在一句话里。后者少见，
    且要收窄就得动正则的第一分支、可能伤到真正的额度问句——那需要另一个决定，
    不该顺手夹带。
    """

    def test_a_mixed_request_is_still_treated_as_capacity_only(self):
        mixed = "把 settings 里的 token 上限调高一些，然后跑一遍测试"
        self.assertIsNotNone(
            agent._capacity_help_response(mixed),
            "这一处过宽**当前仍然存在**。若将来收窄了，请更新本用例的说明，"
            "而不是让它静默变成空跑。")

    def test_the_plain_capacity_part_alone_still_works(self):
        """即便收窄混合句，也必须保住纯额度问句。"""
        self.assertIsNotNone(agent._capacity_help_response("把 token 上限调高一些"))


if __name__ == "__main__":
    unittest.main()