# -*- coding: utf-8 -*-
"""下游子代理必须拿到**完整的**上游结论——过去每值被 `_clip(str(v), 600)` 砍
一刀，把真正结论的尾巴切掉了。用户判断：「除了坏处基本没好处」。

## 修的是什么

上游值本来就被 `summarize_subagent_result` 限在 ~1200（保留状态+结论+步骤，
去掉工具参数/完整观察），再砍 600 会切掉结论末尾——而末尾往往正是那句
「结论是……」。反思判据里的同一份渲染此前也在切。两处现在共用
`_render_upstream_context`：每值不截断，只在判据侧留一道总量预算（8 条 / 6000）。

## 为什么单独测

- `_run_child` 直接跑一次端到端就能验到「注入到下游 question 里那段是不是完整」——
  这是用户点名的落点。
- `_render_upstream_context` 是纯函数，直接给它一份值、断言输出没有 `_clip` 的
  截断尾巴，比把整段拼装再喂给假 LLM 便宜得多。
- 两条一起看，就能同时锁住「两处不砍每值」与「两处渲染方式对齐」。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


TRUNCATION_SUFFIX = "…（内容过长，已截断）"


class RenderUpstreamContextTests(unittest.TestCase):
    """`_render_upstream_context` 的每值不截断行为。"""

    def test_a_1000_char_value_passes_whole(self):
        """用户点名场景：一段 ~1000 字的上游结论完整传给下游，不再被切。"""
        tail = "【结论尾巴】这里是被切掉过的最后那句话。"
        body = "x" * 1000 + tail
        out = agent._render_upstream_context({"t1": body}, bullet=True)
        self.assertIn(tail, out, "结论尾巴被切了——回归到 600 截断")
        self.assertNotIn(TRUNCATION_SUFFIX, out)
        self.assertTrue(out.startswith("- t1："),
                        "bullet=True 应带清单前缀")

    def test_no_bullet_for_judge(self):
        """反思判据走非清单样式，且尊重 max_items/max_total。"""
        ctx = {f"k{i}": "v" * 50 for i in range(20)}
        out = agent._render_upstream_context(
            ctx, bullet=False, max_items=8, max_total=6000)
        self.assertEqual(out.count("k"), 8, "条数没收到 8")
        self.assertFalse(out.startswith("- "), "bullet=False 不该带清单前缀")

    def test_max_total_still_bounds_the_whole_blob(self):
        """总量层面防灾闸还在：只有当整段超过 max_total 时才用 _clip 尾巴标记。"""
        ctx = {f"k{i}": "z" * 500 for i in range(40)}
        out = agent._render_upstream_context(
            ctx, bullet=False, max_items=40, max_total=1000)
        self.assertIn(TRUNCATION_SUFFIX, out,
                      "总量防灾闸被拆掉了——回归风险")

    def test_per_value_is_not_clipped_when_total_fits(self):
        """单值 900 字、总量 6000 之内：值不该被 _clip 加尾巴。"""
        tail = "<<END>>"
        body = "y" * 900 + tail
        out = agent._render_upstream_context({"a": body}, bullet=True)
        self.assertIn(tail, out)
        self.assertNotIn(TRUNCATION_SUFFIX, out)


class RunChildPassthroughTests(unittest.TestCase):
    """端到端：`_run_child` 拼出的 question 里，上游结论完整出现、不再被切。"""

    def _run_and_capture(self):
        """用假 LLM 跑一次 _run_child，抓到 child.run(question) 的那份 question。"""
        captured: list[str] = []

        class _L:
            provider = "fake"
            model = "x"
            last_usage = {}
            last_tool_calls = []

            def clone(self):
                return self

            def chat(self, messages, stream=True, **kw):
                return iter([{"role": "assistant", "content": "Final Answer: 好"}])

            def count_tokens(self, text):
                return 1

        parent = agent.Agent(llm=_L())
        original = agent.Agent.run

        def spy(self, question, stream=False, *a, **kw):
            captured.append(question)
            return original(self, question, stream=stream, *a, **kw)

        agent.Agent.run = spy
        try:
            tail = "<<上游结论的最后一句：这条被 600 截断切掉过>>"
            long_conclusion = (
                "状态：ok\n结论：设计里血量归零触发死亡判负，边界包括 —— " + "x" * 700 + tail)
            out = parent._run_child(
                "researcher", "读一下这份设计",
                context={"t1": long_conclusion},
                reflect=False)
        finally:
            agent.Agent.run = original
        return out, captured, tail, long_conclusion

    def test_child_question_carries_full_upstream_conclusion(self):
        """child.run 收到的 question 里必须能看到上游结论的尾巴。"""
        _out, captured, tail, long_conclusion = self._run_and_capture()
        q = next((x for x in captured if "【上游子任务结论" in x), None)
        self.assertIsNotNone(q, "根本没抓到带上游结论的 child question")
        self.assertIn("【上游子任务结论（供参考，勿重复劳动）】", q)
        self.assertIn(tail, q,
                      "尾巴被切——说明 _run_child 还在按 600 逐值截断")
        self.assertIn(long_conclusion[-100:], q)
        # 只有当某一段被 _clip 时才会出现这句后缀；question 其他部分都是短字符串，
        # 所以出现后缀基本就是那段被切了。
        self.assertNotIn(TRUNCATION_SUFFIX, q,
                         "question 里出现了截断后缀，说明还有别的坑没修")

    def test_child_output_shape_is_unchanged(self):
        """`_run_child` 返回契约不受渲染改动影响（status/conclusion/steps 键仍在）。"""
        out, _captured, _tail, _long = self._run_and_capture()
        for key in ("status", "conclusion", "steps"):
            self.assertIn(key, out)


class SummarizeBudgetStillHeldTests(unittest.TestCase):
    """回归：`summarize_subagent_result` 的显式 max_chars 边界不能被拆掉——
    它是「压缩后」的预算，用户没点名它，`test_game_workflow.py` 也锁着。"""

    def test_explicit_max_chars_still_binds(self):
        from agent_runtime.context_router import summarize_subagent_result
        text = "诊断" + ("x" * 2000)
        summary, _meta = summarize_subagent_result(
            {"status": "failed", "conclusion": text, "steps": 1,
             "trace": {"steps": [{"ok": False}]}}, 180)
        self.assertLessEqual(len(summary), 180)
        self.assertIn("状态：failed", summary)


if __name__ == "__main__":
    unittest.main()
