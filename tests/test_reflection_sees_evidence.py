# -*- coding: utf-8 -*-
"""反思判据必须**看得见工具观察**，而不是只数结论复述了多少。

## 实测故障（真实工作流 trace，`wf-f3da9d3ffb32`）

三次任务都做出了正确的字节级写入。`python_exec` 的观察里全是硬凭据：

    WRITE_OK: True
    actual == after: True
    bom_preserved: True
    LF_offsets_identical: True
    all_diff_offsets_vs_before: [121] | bytes: [(121, b'2', b'5')]

而判据给回的是：

    子代理反思未通过：…但**结论被截断**，未展示写后重新读取 actual、
    actual == after、BOM 与全部换行位置和类型…，无法确认已实际写回

**同一件事连错三次**，逼出两轮 replan 把同一件事重做三遍。

## 根因（不是模型的问题，是判据看不见）

`_reflection_result` 把 `traj` 传进来了，但prompt 里只有：

    步骤数：{len(traj)}；失败步骤：{failed_steps}

**工具观察一个字都没进prompt。** 于是判据在结构上只能问
「结论有没有把这些复述一遍」——它压根不知道观察里有什么。

这与 §9 记的「判据可以结构上永远为假，比写反更难发现」同族：
不是判据写错了，是判据**拿不到它要判的东西**。

## 修法

把观察压成有界证据块喂进去，并把判据的职责从「复述了吗」改成
「**结论有没有声称观察里不存在的东西**」。

§9 另一条教训也照做：证据块取**尾部**（子代理的最终写盘验证总在最后几步），
且截断时**如实标注**，不假装给全了——否则判据又会在这段看不到的地方上
做出「没有证据」的结论。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402
from agent_runtime import tools as agent_runtime_tools  # noqa: E402

#: 一次真实成功写入的观察（形状照抄 wf-f3da9d3ffb32 的 trace，不是编的）
REAL_TRAJ = [
    {"action": "list_dir(path: .)", "obs": "behaviors  scripts  ui  values", "ok": True},
    {"action": "grep(pattern: HP_GROWTH)",
     "obs": "values\\level.gd:5: const HP_GROWTH: float = 0.12", "ok": True},
    {"action": "python_exec(...)",
     "obs": "WRITE_OK: True\nactual == after: True\nbom_preserved: True\n"
            "LF_offsets_identical: True\nall_diff_offsets_vs_before: [121] | "
            "bytes: [(121, b'2', b'5')]", "ok": True},
]


class LlmWaitEventTests(unittest.TestCase):
    """模型等待必须有头有尾——尤其**异常时也要有尾**。

    实测 `wf-f3da9d3ffb32`：全程 360 秒里 98% 静默，而每段静默都是一次模型
    调用（87s 等重规划、58s 等结论、41s 等规划器…）。事件流里此前**完全没有**
    对应标记，所以界面上就是一整段空白，状态 346 秒不变，用户以为卡了。

    配对失败比没有事件更糟：那会让「正在等模型」永远停在那里。所以异常路径
    是这一对事件必须覆盖的重点——第一版就把 `llm_wait_end` 放在 for 循环
    **之后**，流中途抛异常时那句话根本走不到。
    """

    def _run(self, chat_impl):
        seen = []
        orig = agent._hooks.run_workflow

        def spy(kind, payload=None, **kw):
            seen.append((kind, dict(payload or {})))
            return orig(kind, payload, **kw)

        agent._hooks.run_workflow = spy

        class _L:
            provider = "fake"
            model = "x"
            last_usage = {}
            last_tool_calls = []

            def clone(self):
                return self

            def chat(self, messages, stream=True, **kw):
                return chat_impl(stream)

            def count_tokens(self, text):
                return 1

        try:
            out = agent.Agent(llm=_L())._run_child("researcher", "查一下")
        finally:
            agent._hooks.run_workflow = orig
        starts = [p for k, p in seen if k == "llm_wait_start"]
        ends = [p for k, p in seen if k == "llm_wait_end"]
        return out, starts, ends

    _ANSWER = "Thought: 看一下" + chr(10) + "Final Answer: 好了。"

    def _boom(self, stream):
        raise RuntimeError("模型调用炸了")

    def test_non_streaming_pairs_up(self):
        out, starts, ends = self._run(lambda st: self._ANSWER)
        self.assertEqual(len(starts), len(ends))
        self.assertTrue(starts, "一次模型调用都没有发 llm_wait_start")

    def test_streaming_pairs_up(self):
        out, starts, ends = self._run(lambda st: (c for c in self._ANSWER))
        self.assertEqual(len(starts), len(ends))
        self.assertTrue(starts)

    def test_a_non_streaming_crash_still_closes_the_pair(self):
        out, starts, ends = self._run(self._boom)
        self.assertEqual(len(starts), 1, "应该已经发出了开始")
        self.assertEqual(len(ends), 1,
                         "模型调用抛异常时没有 llm_wait_end —— 界面会永远停在「等待模型」")
        self.assertEqual(out.get("status"), "failed")

    def test_a_streaming_crash_still_closes_the_pair(self):
        out, starts, ends = self._run(self._boom)
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1,
                         "流中途抛异常时没有 llm_wait_end —— 生成器被 close 也必须发")

    def test_the_end_carries_a_duration(self):
        _out, _starts, ends = self._run(lambda st: self._ANSWER)
        self.assertTrue(ends)
        for payload in ends:
            self.assertIn("duration_ms", payload,
                          "结束事件必须带耗时，否则界面算不出「等了很久」")

    def test_the_purpose_says_which_agent_and_which_turn(self):
        _out, starts, _ends = self._run(lambda st: self._ANSWER)
        self.assertTrue(starts)
        self.assertIn("subagent", str(starts[0].get("purpose") or ""),
                      "purpose 没标出是子代理 —— 界面上分不出是谁在等")

    def test_both_kinds_are_forwarded_to_the_workflow_stream(self):
        """它们必须真的进持久事件流，否则时间线上还是空的。"""
        from agent import SINK_FORWARDED_HOOKS
        for kind in ("llm_wait_start", "llm_wait_end"):
            self.assertIn(kind, SINK_FORWARDED_HOOKS,
                          "%s 没进转发集合 —— 面板按这些字面量过滤" % kind)

    def test_both_kinds_are_declared_workflow_hooks(self):
        """**这一条才是当初漏掉的那条。**

        `hooks.run_workflow` 第一行是
        `if kind not in _WORKFLOW_KINDS: return …` —— 不在那个元组里的 kind
        会被**静默丢弃，连事件都不发布**。

        上一条提交里 `llm_wait_*` 是在 `agent.py` 侧发的，进程内测试 spy
        `run_workflow` 看到它被调用就通过了；而真实跑一次工作流后，
        事件流里 `llm_wait_*` 出现 **0 次**，静默度仍 99%。

        验「函数被调用」不是验「事件到了」—— 前者在调用边界，后者在用户
        看得到的那条路。
        """
        from hooks import _WORKFLOW_KINDS
        for kind in ("llm_wait_start", "llm_wait_end"):
            self.assertIn(kind, _WORKFLOW_KINDS,
                          "%s 不在 hooks._WORKFLOW_KINDS 里，run_workflow 会"
                          "在第一行就 return，事件根本不会发布" % kind)

    def test_run_workflow_actually_publishes_them_to_the_scope_sink(self):
        """再往下一层：确认 `workflow_event_scope` 的 sink 真能收到。"""
        import hooks

        got = []

        def sink(kind, payload, result):
            got.append(kind)

        for kind in ("llm_wait_start", "llm_wait_end"):
            with hooks.workflow_event_scope(sink):
                hooks.run_workflow(kind, {"purpose": "test", "duration_ms": 1})
        self.assertIn("llm_wait_start", got,
                      "sink 没收到 llm_wait_start —— 事件在 run_workflow 里被丢了")
        self.assertIn("llm_wait_end", got)

    def test_an_undeclared_kind_is_silently_dropped(self):
        """把「静默丢弃」这件事本身钉住：它正是这次踩的坑的形状。"""
        import hooks

        got = []

        def sink(kind, payload, result):
            got.append(kind)

        with hooks.workflow_event_scope(sink):
            result = hooks.run_workflow("no_such_hook_kind", {})
        self.assertEqual(got, [], "未声明的 kind 不该发布事件")
        self.assertFalse(result.get("blocked"),
                         "未声明的 kind 必须返回中性的不拦截结果，"
                         "否则调用方会以为钩子真的跑过了")


class StageCwdReportTests(unittest.TestCase):
    """诊断那一行必须**判得对**——第一版把试做区里的路径判成了"不在"。

    写死 `config.STATE_ROOT` 是错的：生产的 state_root 是 `<repo>/.docmind`，
    测试里是临时目录，而 `project_stage.create_stage` 用的是
    `Path(state_root)/"project_stages"/<wid>`。所以按**路径分段**判。
    """

    def test_a_stage_workspace_path_counts_as_inside(self):
        text = agent_runtime_tools.stage_cwd_report(
            r"D:\somewhere\.docmind\project_stages\wf-1\workspace")
        self.assertNotIn("不在试做区内", text)

    def test_the_real_project_counts_as_outside(self):
        text = agent_runtime_tools.stage_cwd_report(r"D:\WorkBuddy\godot_sample")
        self.assertIn("不在试做区内", text)

    def test_it_does_not_depend_on_where_state_root_is(self):
        """同一个 stage 路径换个基址也得判成在内。"""
        for base in (r"C:\tmp\xyz", r"D:\WorkBuddy\rag-agent\.docmind",
                     r"/home/u/.docmind"):
            text = agent_runtime_tools.stage_cwd_report(
                os.path.join(base, "project_stages", "wf-9", "workspace"))
            self.assertNotIn("不在试做区内", text,
                             "基址是 %s 时判错了" % base)


class TrajEvidenceBlockTests(unittest.TestCase):
    """证据块本身：给得够、说得清边界。"""

    def test_it_carries_the_actual_observations(self):
        block = agent._traj_evidence_block(REAL_TRAJ)
        for needle in ("WRITE_OK: True", "bom_preserved: True",
                       "all_diff_offsets_vs_before: [121]",
                       "grep(pattern: HP_GROWTH)"):
            self.assertIn(needle, block,
                          "证据块里没有 %r——判据还是看不见它" % needle)

    def test_an_empty_traj_says_so_instead_of_looking_like_missing_evidence(self):
        block = agent._traj_evidence_block([])
        self.assertIn("无", block)
        self.assertNotIn("未提供", block,
                         "空轨迹不该说成「截断」，那会让判据误以为还有没给的证据")

    def test_truncation_is_declared_not_hidden(self):
        """§9：没给全就必须说没给全，否则判据会在看不见的那段上下错结论。"""
        long = [{"action": "s%d" % i, "obs": "x" * 300, "ok": True} for i in range(30)]
        block = agent._traj_evidence_block(long)
        self.assertIn("未提供", block)
        self.assertIn("最后", block)

    def test_it_takes_the_tail_where_the_write_verification_lives(self):
        long = [{"action": "s%d" % i, "obs": "obs%d" % i, "ok": True}
                for i in range(30)]
        block = agent._traj_evidence_block(long)
        self.assertIn("obs29", block, "尾部没被保留——最终写盘验证就在那里")
        self.assertNotIn("obs0 ", block)

    def test_it_is_bounded(self):
        long = [{"action": "s%d" % i, "obs": "y" * 500, "ok": True} for i in range(60)]
        self.assertLessEqual(len(agent._traj_evidence_block(long)), 2000,
                             "证据块无界——它会被塞进每次反思的 prompt")

    def test_failed_steps_are_marked(self):
        block = agent._traj_evidence_block([
            {"action": "grep(pattern: x)", "obs": "文件不存在", "ok": False}])
        self.assertIn("失败", block)


class ReflectionSeesEvidenceTests(unittest.TestCase):
    """判据的 prompt 必须真的带上观察。"""

    def _prompt(self, traj, conclusion="已完成。"):
        captured = {}

        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, messages, stream=True, **kw):
                captured["prompt"] = messages[-1]["content"]
                return '{"ok": true, "issues": [], "next_step": ""}'

            def count_tokens(self, text):
                return 1

        agent._reflection_result(_LLM(), role="coder", task="改一个常量",
                                 conclusion=conclusion, traj=traj, context={})
        return captured["prompt"]

    def test_the_observations_reach_the_prompt(self):
        prompt = self._prompt(REAL_TRAJ)
        self.assertIn("WRITE_OK: True", prompt,
                      "工具观察没进prompt——判据只能判「结论复述了吗」")
        self.assertIn("bom_preserved: True", prompt)

    def test_the_criterion_is_restated_as_against_observations(self):
        prompt = self._prompt(REAL_TRAJ)
        self.assertIn("工具观察", prompt)
        self.assertIn("不得声称观察里没有的东西", prompt,
                      "判据的职责必须从「复述了吗」改成「有没有超出观察」")

    def test_it_does_not_ask_for_verbatim_restatement_anymore(self):
        """旧判据的实际判据就是「结论有没有逐条复述」，必须明确取消。"""
        prompt = self._prompt(REAL_TRAJ)
        self.assertNotIn("结论是否有证据：", prompt)
        self.assertIn("不必逐字复述", prompt)


class DeterministicFallbackTests(unittest.TestCase):
    """LLM 判据不可用时的确定性兜底也不能变松。"""

    def test_no_conclusion_is_still_not_ok(self):
        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, *a, **kw):
                raise RuntimeError("不可用")

            def count_tokens(self, text):
                return 1

        out = agent._reflection_result(_LLM(), role="coder", task="t",
                                       conclusion="   ", traj=REAL_TRAJ, context={})
        self.assertFalse(out["ok"])
        self.assertIn("没有最终结论", out["issues"])

    def test_failed_tool_steps_still_block_ok(self):
        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, *a, **kw):
                raise RuntimeError("不可用")

            def count_tokens(self, text):
                return 1

        out = agent._reflection_result(
            _LLM(), role="coder", task="t", conclusion="做完了",
            traj=[{"action": "grep(pattern: x)", "obs": "文件不存在", "ok": False}],
            context={})
        self.assertFalse(out["ok"], "有失败工具步骤却判通过——兜底被放松了")


if __name__ == "__main__":
    unittest.main()