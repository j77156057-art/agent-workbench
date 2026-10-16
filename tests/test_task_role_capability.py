# -*- coding: utf-8 -*-
"""任务要写文件、角色却不能写——派发前报出来（**只报不拦**）。

## 实测依据（四次真实工作流，不是设想）

`wf-9f121dec38a6` 的三个任务全是 `researcher` / `researcher` / `planner`：

| 角色 | 工具列表有 `python_exec` 吗 | hint 写的什么 |
|---|---|---|
| `researcher` | **没有** | 「只负责查证…**不要改任何文件**」 |
| `planner` | **没有** | 「只做分析和规划，**不修改文件**」 |

**整轮没有一个人有权写文件**，而目标就是写一个文件。

而 `wf-f3da9d3ffb32` 里任务一旦派给 `coder`（有 `python_exec`），模型立刻就用了
——`WRITE_OK: True` 就是它写出来的。

**所以曾把这件事归因为「模型不肯用指定工具」是错的。** 真实形状是
**任务在结构上无法完成**，而界面上只看得到「任务失败」，看不出这是注定的。

## 为什么先只报不拦

拦下来要改规划器的重试语义，而「哪些动词算要写」这个判据本身有误报风险——
误报会把正常的只读复核任务也判成不可能完成。**宁可先让人看见，别先让人动不了。**

## 这与 `_run_child` 那道闸是同一族

它拦住「角色**放大**工具范围」（planner 不得给只读角色加写工具）；
没人检查「任务是否**超出**角色能力」。两边合起来才完整。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import (  # noqa: E402
    GameWorkflowManager, _tool_is_dropped_for_read_only, role_can_write,
    role_mismatch_tasks, task_wants_write,
)


class RoleCapabilityTests(unittest.TestCase):
    def test_the_known_read_only_roles_cannot_write(self):
        for role in ("researcher", "planner", "dispatcher", "reviewer"):
            self.assertFalse(role_can_write(role),
                             "%s 应当判为不能写" % role)

    def test_the_write_roles_can(self):
        for role in ("coder", "tester", "artist", "designer"):
            self.assertTrue(role_can_write(role),
                            "%s 应当判为能写" % role)

    def test_an_unknown_role_is_not_assumed_writable(self):
        """未知角色一律按不能写处理——**fail-closed**。

        猜它能写会导致「以为能写、实际不能」，那正好是这条要治的病。
        """
        self.assertFalse(role_can_write(""))
        self.assertFalse(role_can_write("no_such_role"))


class WriteVerbDetectionTests(unittest.TestCase):
    """判据宁可漏报也不误报：误报会把正常的只读复核判成不可能完成。"""

    def test_it_catches_the_real_mismatch_texts(self):
        for text in ("修复仓库说明文档并新建一个 CHANGELOG",
                     "新建 probe_isolation.gd 并写入两行",
                     "把 HP_GROWTH 改成 0.15"):
            self.assertTrue(task_wants_write(text),
                            "漏了真实错配文本：%r" % text)

    def test_it_does_not_fire_on_read_only_work(self):
        """这三条都取自真实运行里的只读任务，误报会把它们全判成不可能完成。"""
        for text in ("只读核查：检查 enemy.gd 里 CONTACT_DAMAGE 的值并给出 文件:行号",
                     "评审这段代码，指出风险",
                     "检索并列出所有用到 HP_GROWTH 的位置",
                     "把目标拆成三个子任务并派发",
                     "运行测试并报告结果"):
            self.assertFalse(task_wants_write(text),
                             "误报了正常的只读任务：%r" % text)


class MismatchEventTests(unittest.TestCase):
    """事件必须真的落进工作流事件流（别又只 spy 调用边界）。"""

    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]
        self.manager.choose(self.wid, "recommended")

    def _kinds(self):
        return [e.get("kind") for e in self.manager.get(self.wid)["events"]]

    def test_a_mismatched_task_emits_the_event(self):
        state = self.manager._load(self.wid)
        self.manager._note_role_capability(state, [
            {"id": "t1", "role": "researcher",
             "task": "新建 values/probe.gd 并写入两行内容"}])
        self.assertIn("task_role_mismatch", self._kinds())

    def test_a_capable_task_does_not_emit(self):
        state = self.manager._load(self.wid)
        self.manager._note_role_capability(state, [
            {"id": "t1", "role": "coder", "task": "新建 values/probe.gd"}])
        self.assertNotIn("task_role_mismatch", self._kinds())

    def test_a_read_only_task_does_not_emit(self):
        state = self.manager._load(self.wid)
        self.manager._note_role_capability(state, [
            {"id": "t1", "role": "researcher",
             "task": "检索并列出所有用到 HP_GROWTH 的位置"}])
        self.assertNotIn("task_role_mismatch", self._kinds())

    def test_the_event_names_the_role_and_lists_who_can_write(self):
        """只说「不行」不给出路，等于把问题原样退回给用户。"""
        state = self.manager._load(self.wid)
        self.manager._note_role_capability(state, [
            {"id": "t1", "role": "researcher", "task": "新建 x.gd"}])
        ev = [e for e in self.manager.get(self.wid)["events"]
              if e.get("kind") == "task_role_mismatch"][0]
        self.assertEqual(ev.get("role"), "researcher")
        self.assertIn("coder", str(ev.get("writable_roles") or ""),
                      "没给出哪些角色能写——这条信息没用")

    def test_it_does_not_block_the_wave(self):
        """只报不拦是本轮刻意的取舍：拦下来要改规划器重试语义。"""
        state = self.manager._load(self.wid)
        before_tasks = len(state.tasks)
        before_status = state.status
        before_phase = state.phase
        self.manager._note_role_capability(state, [
            {"id": "t1", "role": "researcher", "task": "新建 x.gd"}])
        self.assertEqual(len(state.tasks), before_tasks,
                         "它竟然改动了任务列表——这一轮只报不拦")
        self.assertEqual(state.status, before_status,
                         "它竟然改了状态——这一轮只报不拦")
        self.assertEqual(state.phase, before_phase,
                         "它竟然改了阶段——这一轮只报不拦")
        # 事件本身要落下来，否则「什么都没发生」和「报了但没人看见」分不开。
        self.assertIn("task_role_mismatch",
                      [e.get("kind") for e in state.events],
                      "没报也没拦——那这条诊断等于不存在")

    def test_malformed_tasks_do_not_crash_it(self):
        state = self.manager._load(self.wid)
        self.manager._note_role_capability(state, [
            None, "not a mapping", {"role": None, "task": None},
            {"id": "x", "role": "coder", "task": "新建 a.gd"}])
        self.assertNotIn("task_role_mismatch", self._kinds())


class GraphIsRejectedNotTrimmedTests(unittest.TestCase):
    """①：整图拒绝，**不静默删掉那几条**。

    静默删任务正是本会话修掉的病（`e0d182d`：多步 DAG 被悄悄截成只剩根任务，
    调用方照样落库、照样发事件、照样弹审批，零报错）。所以处置必须与
    「未知依赖」同款——整图拒绝，让调用方回退到确定性计划。

    而那条回退路径的角色分配是对的（实测 `generic` 画像派
    `researcher` 只读且无写动词 / `coder` 能写 / `tester` 能写），
    不会立刻再错一次。
    """

    def _graph(self):
        return {"tasks": [
            {"id": "impl", "role": "coder", "task": "实现最小可运行版本",
             "depends_on": []},
            {"id": "bad", "role": "researcher",
             "task": "新建 values/probe.gd 并写入两行", "depends_on": ["impl"]},
        ]}

    def test_one_mismatched_task_rejects_the_whole_graph(self):
        got = GameWorkflowManager._parse_generated_tasks(self._graph(), max_tasks=0)
        self.assertEqual(got, [],
                         "没有整图拒绝——那样会静默删掉两条任务，"
                         "而调用方会把它当成一份合法计划落库")

    def test_a_fully_capable_graph_still_parses(self):
        good = {"tasks": [
            {"id": "impl", "role": "coder", "task": "实现最小版本", "depends_on": []},
            {"id": "verify", "role": "tester", "task": "运行测试并报告结果",
             "depends_on": ["impl"]},
        ]}
        got = GameWorkflowManager._parse_generated_tasks(good, max_tasks=0)
        self.assertEqual([t["id"] for t in got], ["impl", "verify"],
                         "正常计划被这条守卫误伤了")

    def test_the_rejection_names_the_offending_task(self):
        bad = role_mismatch_tasks(self._graph()["tasks"])
        self.assertEqual([b["id"] for b in bad], ["bad"])
        self.assertEqual(bad[0]["role"], "researcher")

    def test_it_handles_fenced_json_and_nested_shapes(self):
        """规划器的产出形状有四种，全部要能取出任务来指名。"""
        import json as _json

        from agent_runtime.game_workflow import _as_task_list
        raw = self._graph()["tasks"]
        for shaped in (raw, {"tasks": raw}, {"plan": {"tasks": raw}},
                       "```json\n" + _json.dumps({"tasks": raw}, ensure_ascii=False) + "\n```"):
            self.assertEqual(_as_task_list(shaped), raw, "形状没被正确拆开")

    def test_a_malformed_shape_yields_an_empty_list_not_a_crash(self):
        from agent_runtime.game_workflow import _as_task_list
        for bad in (None, 42, "not json", {"nope": 1}):
            self.assertEqual(_as_task_list(bad), [])


class DeterministicFallbackIsSoundTests(unittest.TestCase):
    """回退计划自己不能带同样的错，否则「拒绝→回退」只是换一种方式失败。"""

    def test_the_profile_fallback_has_no_role_mismatch(self):
        from agent_runtime.workflow_profiles import get_profile
        tasks = get_profile("generic").fallback_tasks()
        shaped = [{"id": t.get("id"), "role": t.get("role"),
                   "task": t.get("task") or t.get("desc") or ""} for t in tasks]
        self.assertEqual(role_mismatch_tasks(shaped), [],
                         "确定性回退计划自己就带角色能力错配——"
                         "那样拒绝 LLM 计划只会换一种方式失败")
        self.assertTrue(shaped, "回退计划为空")


class ZeroToolDispatchTests(unittest.TestCase):
    """第二维：**任务文本不提写，点名的工具却全被筛掉**。

    ## 实测依据（`.tmp/repro_planner_zero_tools.py`，2026-10-16）

    派单 `role=planner, task="勘察目录与文件结构", tools=["apply_edit","dev_mcp_call"]`
    时，`role_mismatch_tasks` 原先返回 `[]` —— 整图放行 → 子代理被建出来 →
    有效工具集为空 → 对着空工具集跑模型 → 上层看到
    「模型连续 2 轮没有给出可判定的动作或结论」。

    **归因完全错了**：真因是派单点名了写工具与 MCP 工具，却被只读边界和
    `MCP=deny` 全筛掉。子代理静悄悄地什么都没做、还把锅甩给模型。

    原判据只查任务文本里的写动词，而这条派单的文本**恰好是纯只读的**
    （"勘察目录与文件结构" 不含任何写动词）——所以它从旁边溜了过去。

    这一族钉住**两维都要查**，且第二维不得误伤合法派单。
    """

    def test_the_real_zero_tool_dispatch_is_now_reported(self):
        got = role_mismatch_tasks([{
            "id": "a", "role": "planner",
            "task": "勘察目录与文件结构，决定分工",
            "tools": ["apply_edit", "dev_mcp_call"],
        }])
        self.assertEqual([g["id"] for g in got], ["a"],
                         "零工具派单没有被报出来——它会一路滑到运行期")

    def test_the_reason_says_why_rather_than_only_that_it_failed(self):
        got = role_mismatch_tasks([{
            "id": "a", "role": "planner", "task": "勘察目录",
            "tools": ["apply_edit"],
        }])
        self.assertTrue(got)
        self.assertIn("零工具", got[0]["why"],
                      "只说「不匹配」等于把问题原样退回给用户")

    def test_a_mixed_list_is_not_zero_tools(self):
        """点名 `[apply_edit, read_file]` 对 planner 仍留下 `read_file` —— 能干活。"""
        self.assertEqual(role_mismatch_tasks([{
            "id": "a", "role": "planner", "task": "勘察目录",
            "tools": ["apply_edit", "read_file"],
        }]), [], "把「有一部分工具被丢」误判成零工具，会拒掉能跑的派单")

    def test_an_omitted_or_empty_tool_list_is_not_a_mismatch(self):
        """不传 / 传空 = 用角色默认只读集，那条路本来就能干活。"""
        for tools in (None, [], (), ""):
            got = role_mismatch_tasks([{
                "id": "a", "role": "planner", "task": "勘察目录", "tools": tools,
            }])
            self.assertEqual(got, [], "tools=%r 不该被判零工具" % (tools,))

    def test_a_read_only_list_on_a_read_only_role_passes(self):
        self.assertEqual(role_mismatch_tasks([{
            "id": "a", "role": "planner", "task": "勘察目录",
            "tools": ["read_file", "grep", "search_code"],
        }]), [], "planner 点名只读工具是最正常的用法，不该被拒")

    def test_non_read_only_roles_are_not_judged_by_this_dimension(self):
        """`coder` 点名写工具 / `tester` 点名 MCP 工具都是正常派单。

        名字不存在的情形属于**另一族**问题：由运行期 `_run_child` fail-closed
        指名（单任务失败、其余任务照跑）。在这里整图拒掉会误伤：一次笔误
        不该让整份计划作废。
        """
        for task in (
            {"id": "a", "role": "coder", "task": "实现功能", "tools": ["apply_edit"]},
            {"id": "b", "role": "tester", "task": "跑测试", "tools": ["dev_mcp_call"]},
            {"id": "c", "role": "sre-bot", "task": "查日志", "tools": ["run_command"]},
            {"id": "d", "role": "coder", "task": "实现功能", "tools": ["no_such_tool"]},
        ):
            self.assertEqual(role_mismatch_tasks([task]), [],
                             "非只读角色被这一维误伤了：%r" % task)

    def test_a_tools_field_written_as_a_string_still_works(self):
        """规划器把 `tools` 写成 `"read_file"` 而不是 `["read_file"]` 是可能的形状。"""
        self.assertEqual(role_mismatch_tasks([{
            "id": "a", "role": "planner", "task": "勘察目录", "tools": "read_file",
        }]), [], "字符串形态的 tools 没被处理")

    def test_it_rejects_the_whole_graph_like_the_text_dimension(self):
        got = GameWorkflowManager._parse_generated_tasks({"tasks": [
            {"id": "probe", "role": "planner", "task": "勘察目录与文件结构",
             "tools": ["apply_edit"], "depends_on": []},
            {"id": "impl", "role": "coder", "task": "实现最小版本",
             "depends_on": ["probe"]},
        ]}, max_tasks=0)
        self.assertEqual(got, [], "零工具派单没有被整图拒绝")

    def test_every_connector_tool_is_dropped_for_a_read_only_role(self):
        """**整个连接器族**都得被丢，不只是 `dev_mcp_call`。

        变异测试逮到的洞：这条判据原先写死三个 MCP 工具名，而
        `dev_route_connector` 的 `ToolSpec` 是 `read_local` + `pure`
        （纯读、路由一个连接器）—— 只按「会不会改东西」判，它会被**放进来**，
        于是只读 planner 能拿到连接器路由能力。真相是判据该用 `domain == "mcp"`，
        那才是把这一族标出来的那一维。

        所以这里逐个点名，而不是只测 `dev_mcp_call`：只测后者的话，
        那个洞永远不会被发现（`dev_mcp_call` 是 `irreversible`，被另一层拦住了）。
        """
        import tools as tools_mod
        from agent_runtime.tools import coerce_tool_spec, tool_mutates

        connectors = [n for n, v in tools_mod.TOOLS.items()
                      if coerce_tool_spec(n, v).domain == "mcp"]
        self.assertGreaterEqual(len(connectors), 3,
                                "连接器工具不足三个——本测试的前提变了，请重新评估")
        for name in connectors:
            spec = coerce_tool_spec(name, tools_mod.TOOLS[name])
            self.assertTrue(
                _tool_is_dropped_for_read_only(name, "planner"),
                "只读 planner 能拿到连接器工具 %s（domain=%s cap=%s side=%s）"
                % (name, spec.domain, spec.capability.value, spec.side_effect.value))

    def test_the_read_only_role_set_comes_from_one_place(self):
        """只读角色名单不允许两份各自漂。"""
        from agent_runtime.game_workflow import READ_ONLY_ROLES
        self.assertEqual(READ_ONLY_ROLES, frozenset({"planner", "dispatcher"}))


class ZeroToolRunFailsClosedTests(unittest.TestCase):
    """运行期：零工具子代理必须**当场失败**，不能静悄悄地跑。

    这是「只报不拦」在运行期的对偶：任务图校验是**预防**（整图拒），
    而 `delegate` 这种单任务直调绕过任务图——那条路上唯一的闸就是
    `_run_child` 这里的 fail-closed。

    原实现让零工具子代理照常建出来、照常拿模型，于是 `status=ok` +
    结论写成「模型没给出可判定动作」——**把护栏故障记成了模型的错**。
    """

    def _parent(self):
        import agent as agent_mod
        from agent_runtime.tools import Capability, SideEffect

        class _NoLLM:
            provider = "fake"
            model = "fake"
            capability: dict = {}

            def clone(self):
                return self

            def count_tokens(self, text):
                return 1

            def chat(self, messages, stream=True, **kwargs):
                # 消息里**不要**出现「零工具」三个字：下面两条反向守卫就用这三个字
                # 判「这条闸有没有触发」，写在异常消息里会把量具自己污染掉。
                raise AssertionError("LLM_WAS_CALLED_UNEXPECTEDLY")

        registry = {
            "read_file": {"func": lambda _a: ""},
            "grep": {"func": lambda _a: ""},
            "apply_edit": {"func": lambda _a: "",
                           "capability": Capability.WRITE_LOCAL,
                           "side_effect": SideEffect.MUTATING, "parallel_safe": False},
            "dev_mcp_call": {"func": lambda _a: "",
                             "capability": Capability.NETWORK,
                             "side_effect": SideEffect.IRREVERSIBLE, "parallel_safe": False},
        }
        return agent_mod.Agent(llm=_NoLLM(), tool_registry=registry)

    def test_zero_tools_fails_instead_of_silently_succeeding(self):
        res = self._parent()._run_child(
            "planner", "勘察目录与文件结构",
            tool_allowlist=["apply_edit", "dev_mcp_call"],
            mcp_policy="allow", reflect=False)
        self.assertEqual(res["status"], "failed",
                         "零工具子代理仍然报成功——那等于把护栏故障记成模型没干活")
        self.assertIn("零工具", res["error"])

    def test_the_error_names_the_dropped_tools_and_the_way_out(self):
        res = self._parent()._run_child(
            "planner", "勘察目录", tool_allowlist=["apply_edit"],
            mcp_policy="deny", reflect=False)
        self.assertEqual(res["dropped_tools"], ["apply_edit"])
        self.assertIn("coder", res["error"],
                      "只说「不行」不给改法，等于把问题原样退回给调用方")

    def _hit_the_zero_tools_gate(self, res):
        """这条闸有没有触发？——看 error 文本，不看别的字段。

        不用 `_NoLLM` 抛不抛异常来间接判断：闸**不**触发时子代理会正常走到调
        模型那一步，异常就混进 `error`，和闸触发的 `error` 长得一样。
        """
        return "零工具" in str(res.get("error") or "")

    def test_a_survivable_list_still_runs(self):
        """反向守卫：留得下工具时**不许**被这条闸拦住。"""
        res = self._parent()._run_child(
            "planner", "勘察目录", tool_allowlist=["read_file", "apply_edit"],
            mcp_policy="deny", reflect=False)
        self.assertFalse(self._hit_the_zero_tools_gate(res),
                         "planner 点名 [read_file, apply_edit] 仍有 read_file 可用，"
                         "不该被判零工具")

    def test_an_empty_allowlist_is_still_respected_as_intent(self):
        """显式传空名单是调用方的意图，不归这条闸管。"""
        res = self._parent()._run_child(
            "planner", "勘察目录", tool_allowlist=[], mcp_policy="deny", reflect=False)
        self.assertFalse(self._hit_the_zero_tools_gate(res),
                         "显式传空名单被当成零工具故障——那是调用方的意图")

    def test_an_omitted_allowlist_still_gets_the_role_defaults(self):
        res = self._parent()._run_child(
            "planner", "勘察目录", mcp_policy="deny", reflect=False)
        self.assertFalse(self._hit_the_zero_tools_gate(res),
                         "不传 allowlist 时用的是 planner 的只读默认集，本该有工具")


class ToolMutatesIsTheSingleAuthorityTests(unittest.TestCase):
    """「这个工具会不会改东西」只能有一份判据。

    合并前 `agent.py` 的只读过滤与 `game_workflow.WRITE_CAPABLE_TOOLS` 各有一份，
    两份都按名字列工具名，两份都会漂：前者曾实测漏 14 个会改东西的工具
    （`dev_apply_edits` 漏登记却一直绿灯），后者只有 6 个名字。
    下一个人加了新工具，没人记得往名字表里补 —— 护栏就这么静悄悄地失效。
    """

    def test_it_reads_capability_and_side_effect(self):
        from agent_runtime.tools import (Capability, SideEffect, ToolSpec,
                                         tool_mutates)
        def _spec(**kw):
            base = {"func": lambda _a: ""}
            base.update(kw)
            return ToolSpec(name="t", description="", **{
                "func": base["func"], **kw})

        self.assertFalse(tool_mutates(_spec(capability=Capability.READ_LOCAL)))
        self.assertTrue(tool_mutates(_spec(capability=Capability.WRITE_LOCAL)))
        self.assertTrue(tool_mutates(_spec(capability=Capability.WRITE_EXTERNAL)))
        # 改**共享状态**而不是工作区文件的那一半：只看 WRITE_* 会漏掉它们。
        self.assertTrue(tool_mutates(_spec(capability=Capability.ADMIN)))
        self.assertTrue(tool_mutates(_spec(capability=Capability.EXEC)))
        # 声明了不可逆副作用的那一半。
        self.assertTrue(tool_mutates(
            _spec(capability=Capability.READ_LOCAL,
                  side_effect=SideEffect.IRREVERSIBLE)))

    def test_an_unknown_tool_is_not_assumed_dangerous(self):
        from agent_runtime.tools import tool_mutates
        self.assertFalse(tool_mutates(None))

    def test_it_agrees_with_the_registry_on_the_real_write_tools(self):
        """拿真实注册表过一遍：凡是 `_SUBAGENT_ROLES` 判为可写的角色，
        其默认工具集里至少有一个被 `tool_mutates` 认成会改东西。"""
        from agent_runtime.tools import coerce_tool_spec, tool_mutates
        import tools as tools_mod
        from agent_runtime.game_workflow import role_can_write

        for role in ("coder", "tester", "artist", "designer"):
            self.assertTrue(role_can_write(role), "%s 本该可写" % role)
            names = tools_mod.TOOLS
            role_tools = __import__("agent")._SUBAGENT_ROLES[role]["tools"]
            self.assertTrue(
                any(tool_mutates(coerce_tool_spec(n, names[n]))
                    for n in role_tools if n in names),
                "%s 的默认工具集里一个会改东西的都没有——角色表与真实分类对不上"
                % role)


if __name__ == "__main__":
    unittest.main()