"""工具分类必须只有一个事实来源——否则"绿灯"会和真实行为分叉。

这个文件来自一次真实的假保障。`agent.py` 里曾有一批同名常量副本：

    _WRITE_TOOLS / _WEB_TOOLS / _VERBATIM_TOOLS / _NO_PARALLEL_TOOLS
    _NO_ARG_TOOLS / _OPTIONAL_ARG_TOOLS

运行期**一份都不读**——判定全部走 `ToolSpec`：

    写工具    Agent._is_write_tool   -> spec.capability ∈ {WRITE_LOCAL, WRITE_EXTERNAL}
    外网工具  Agent._web_blocked     -> spec.group == "web"
    免参工具  Agent._arg_required    -> spec.input_schema["required"]
    逐字透传  Agent._is_verbatim_tool -> spec.verbatim
    能否并发  _parallel_safe         -> spec.parallel_safe

于是副本与运行期各说各话，而**测试断言的正是副本**，绿灯一直亮着。真实后果：

    dev_apply_edits（批量原子改文件）在死副本里被列为"不可并发"，
    在运行期的 _WRITE_TOOLS 里却没有 → 被判成 read_local / SideEffect.PURE /
    parallel_safe=True —— 一个改文件的工具被当成无害只读，既能和别的工具并发写，
    又因为 PURE 拿不到幂等账本的保护。

死副本已删除。本文件的作用是让"又有人加一张自己的表"或"又漏登记一个写工具"
立刻变红，而不是安静地错下去。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent  # noqa: E402
import tools  # noqa: E402
from agent_runtime.tools import (  # noqa: E402
    ADMIN_TOOLS, EXEC_TOOLS, WRITE_TOOLS, Capability, SideEffect,
    coerce_tool_spec,
)


class NoShadowTaxonomyTests(unittest.TestCase):
    """agent.py 不得再持有自己的工具分类副本。"""

    #: 这些名字曾在 agent.py 里各有一份死副本。
    SHADOW_NAMES = ("_WRITE_TOOLS", "_WEB_TOOLS", "_VERBATIM_TOOLS",
                    "_NO_PARALLEL_TOOLS", "_NO_ARG_TOOLS", "_OPTIONAL_ARG_TOOLS")

    def test_agent_module_has_no_shadow_tables(self):
        for name in self.SHADOW_NAMES:
            self.assertFalse(
                hasattr(agent, name),
                "agent.%s 又出现了。它不在运行路径上，断言它等于断言一份与真实行为"
                "无关的清单——这正是 dev_apply_edits 漏登记却一直绿灯的原因。"
                "分类只应在 agent_runtime.tools 与各注册项的声明里存在。" % name)

    def test_the_authoritative_tables_are_exported(self):
        """单一事实来源必须可被测试直接引用，否则测试又会去别处找一份。"""
        for table in (WRITE_TOOLS, ADMIN_TOOLS, EXEC_TOOLS):
            self.assertTrue(table, "分类表不应为空")


class WriteToolRegistrationTests(unittest.TestCase):
    """每个会落盘的工具都必须被登记为写工具。漏一个就少一层护栏。"""

    def _spec(self, name):
        value = tools.TOOLS.get(name)
        self.assertIsNotNone(value, "工具未注册：%s" % name)
        return coerce_tool_spec(name, value)

    def test_the_batch_editor_is_a_write_tool(self):
        """dev_apply_edits 曾漏登记 → 批量改文件被判成只读 + 可并发 + PURE。"""
        spec = self._spec("dev_apply_edits")
        self.assertEqual(spec.capability, Capability.WRITE_LOCAL)
        self.assertEqual(spec.side_effect, SideEffect.MUTATING)
        self.assertFalse(spec.parallel_safe)

    def test_it_agrees_with_its_single_edit_sibling(self):
        batch, single = self._spec("dev_apply_edits"), self._spec("apply_edit")
        for field in ("capability", "side_effect", "parallel_safe"):
            self.assertEqual(getattr(batch, field), getattr(single, field), field)

    def test_no_write_tool_is_mistakenly_parallel_safe(self):
        """写工具一律不可并发：两个写工具并跑就是竞态。"""
        for name in WRITE_TOOLS:
            if name not in tools.TOOLS:
                continue
            spec = self._spec(name)
            self.assertFalse(
                spec.parallel_safe,
                "%s 是写工具却被判成可并发，批内会与其它工具同时写" % name)
            self.assertEqual(spec.side_effect, SideEffect.MUTATING, name)

    def test_no_admin_tool_is_mistakenly_parallel_safe(self):
        for name in ADMIN_TOOLS:
            if name not in tools.TOOLS:
                continue
            self.assertFalse(self._spec(name).parallel_safe,
                             "%s 是管理类工具却可并发" % name)


class NameHeuristicAgreesWithTaxonomyTests(unittest.TestCase):
    """按名字猜"这是写工具"的启发式，必须和真实分类一致。

    这类启发式很容易和分类表分叉：注册表加新工具时通常只改一处。
    分叉的后果是**有的写工具被护栏放行、有的只读工具被误拦**，而且没人会发现。
    """

    #: 名字里带这些词的工具，几乎必然落盘。用来抓"忘了登记"的漏网之鱼。
    WRITE_NAME_HINTS = ("edit", "apply", "create", "write", "patch",
                        "refactor", "rebuild", "upsert", "register")

    def test_write_looking_names_are_actually_registered_as_writes(self):
        suspects = []
        for name in tools.TOOLS:
            if not name.startswith(("dev_", "create_", "apply_")):
                continue
            spec = coerce_tool_spec(name, tools.TOOLS[name])
            is_write = spec.capability in (Capability.WRITE_LOCAL,
                                           Capability.WRITE_EXTERNAL,
                                           Capability.ADMIN)
            looks_like_write = any(h in name for h in self.WRITE_NAME_HINTS)
            if looks_like_write and not is_write:
                suspects.append((name, spec.capability.value))
        self.assertEqual(
            suspects, [],
            "这些工具名字像写工具、却没被登记为写/管理类，会绕过写意图护栏：%s"
            % suspects)


class StateDirToolsAreNotSourceWritesTests(unittest.TestCase):
    """只写项目**状态目录**的工具不是"源码写"，不该被写意图护栏拦。

    这条来自 dev_eval 的一次真实回归：把 dev_lanes 从 read_local 改成 write_local
    之后，两道 lane 题全被"[安全拦截] 用户没有要求创建或修改文件"挡掉。

    但它确实改共享状态（认领/心跳/释放），所以必须 mutating 且不可并发——
    两个 lane 并发认领会互相覆盖。也就是它需要的是**并发与幂等保护**，
    不是**用户授权**。这两件事用同一个 capability 表达不了，于是归 ADMIN：
    既避开写意图护栏，又拿到 mutating + 不可并发。
    """

    STATE_DIR_TOOLS = ("dev_lanes", "dev_propose")

    def test_they_are_not_treated_as_source_writes(self):
        a = agent.Agent(llm=None)
        for name in self.STATE_DIR_TOOLS:
            with self.subTest(tool=name):
                spec = coerce_tool_spec(name, tools.TOOLS[name])
                self.assertNotIn(spec.capability,
                                 (Capability.WRITE_LOCAL, Capability.WRITE_EXTERNAL),
                                 "%s 只写状态目录，不该被当成源码写" % name)
                self.assertFalse(a._is_write_tool(name))

    def test_they_still_get_concurrency_protection(self):
        """归 ADMIN 的代价是拿不到写意图护栏，所以并发保护必须还在。"""
        for name in self.STATE_DIR_TOOLS:
            with self.subTest(tool=name):
                spec = coerce_tool_spec(name, tools.TOOLS[name])
                self.assertEqual(spec.side_effect, SideEffect.MUTATING, name)
                self.assertFalse(spec.parallel_safe, name)

    def test_real_source_writes_are_still_guarded(self):
        """对照组：真改源码的工具必须仍在写意图护栏下。"""
        a = agent.Agent(llm=None)
        for name in ("dev_patch", "dev_move", "dev_apply_edits",
                     "create_external_file", "edit_external_file"):
            with self.subTest(tool=name):
                self.assertTrue(a._is_write_tool(name),
                                "%s 改文件，必须受写意图护栏" % name)


class WriteIntentGuardrailCoverageTests(unittest.TestCase):
    """写意图护栏只覆盖 WRITE_LOCAL/WRITE_EXTERNAL——这是已知且**刻意**的缺口。

    `_is_write_tool` 不含 ADMIN 与 EXEC。这不是疏忽：`_RE_WRITE_INTENT` 全是文件动词
    （修改/创建/提交代码…），直接套到管理类工具上会误伤"提交一下""回滚到上个版本"
    这类请求——护栏宁可放过，也不能把正常操作拦死。

    ADMIN/EXEC 另有控制：命令黑名单 + RUN_COMMAND_ALLOW 白名单 + git 写子命令审批门
    （tools.py 的 `_cmd_is_blocked` / `_BLOCKED_CMDS`）。

    但"放过"必须有名字，否则它会被无声地扩大或缩小。这里把它钉住。
    """

    MUTATING = (Capability.WRITE_LOCAL, Capability.WRITE_EXTERNAL,
                Capability.ADMIN, Capability.EXEC)

    def _mutating_tools(self):
        return [n for n in tools.TOOLS
                if coerce_tool_spec(n, tools.TOOLS[n]).capability in self.MUTATING]

    def test_the_guardrail_covers_both_write_capabilities(self):
        a = agent.Agent(llm=None)
        for name in self._mutating_tools():
            if coerce_tool_spec(name, tools.TOOLS[name]).capability in (
                    Capability.WRITE_LOCAL, Capability.WRITE_EXTERNAL):
                self.assertTrue(a._is_write_tool(name),
                                "%s 应受写意图护栏保护" % name)

    def test_admin_and_exec_are_uncovered_by_design(self):
        a = agent.Agent(llm=None)
        uncovered = sorted(n for n in self._mutating_tools()
                           if not a._is_write_tool(n))
        self.assertIn("run_command", uncovered)
        self.assertTrue(uncovered, "应有管理/执行类工具不受写意图护栏")

    def test_read_only_tools_are_not_blocked(self):
        a = agent.Agent(llm=None)
        for name in ("read_file", "list_dir", "grep", "search_code"):
            if name in tools.TOOLS:
                self.assertFalse(a._is_write_tool(name),
                                 "%s 是只读工具，不该被写护栏拦" % name)


class WriteIntentVocabularyTests(unittest.TestCase):
    """写意图靠**词表**判定，所以题面/用例必须用词表里的词。

    这不是凑合：dev_eval 的三道补丁题曾统一用默认问题"按步骤执行。"，
    `应用`/`补丁` 都不在 `_RE_WRITE_INTENT` 里，于是被护栏拦掉。当时看起来像
    "新加了护栏把门禁搞红了"，实际是**题面不真实**——真实用户要求打补丁时
    说的是"改/应用补丁"，护栏拦的只应该是"用户没要求改却想改"的情况。
    """

    def test_the_default_eval_question_has_no_write_intent(self):
        self.assertFalse(agent._has_write_intent("按步骤执行。"))

    def test_a_realistic_patch_request_does_express_write_intent(self):
        for question in ("请应用补丁，修改 a.txt 与 b.txt。",
                         "请修改仓库里的文件：把下面的补丁应用上去。"):
            with self.subTest(q=question):
                self.assertTrue(agent._has_write_intent(question),
                                "要求打补丁的真实说法必须被识别为写入意图，"
                                "否则护栏会把正常请求也拦了")

    def test_review_questions_stay_blocked(self):
        """护栏存在的意义：审查类问题不许顺手改代码。"""
        for question in ("请审查这份报告", "这个实现有什么问题", "分析一下架构"):
            with self.subTest(q=question):
                self.assertFalse(agent._has_write_intent(question))


class NoShadowTaxonomyTests_Ordering(unittest.TestCase):
    """顺序无关性：分类正确时，改动不该影响 dev_eval 的门禁。"""

    def test_patch_fixture_questions_carry_write_intent(self):
        """三道补丁题必须自带 question，且必须真的表达写入意图。"""
        from agent_runtime import dev_eval
        patch_cases = [c for c in dev_eval.DEV_DATASET
                       if c.get("fixture") == "patch"
                       and any("dev_patch" in str(s) for s in c.get("steps", []))]
        self.assertTrue(patch_cases, "应存在补丁相关门禁题")
        for case in patch_cases:
            with self.subTest(case=case.get("id")):
                question = case.get("question")
                self.assertTrue(question,
                                "%s 用了会触发写意图护栏的补丁题，必须显式给 question"
                                % case.get("id"))
                self.assertTrue(agent._has_write_intent(question),
                                "%s 的题面「%s」不表达写入意图，会被护栏拦"
                                % (case.get("id"), question))


if __name__ == "__main__":
    unittest.main()