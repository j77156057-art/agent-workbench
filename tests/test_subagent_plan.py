# -*- coding: utf-8 -*-
"""子代理委派与 plan 模式（离线，走真实 Agent.run 接线）。"""
import os
import tempfile
import unittest

import agent as agent_mod
import agent_trace
import sessions


class _SeqLLM:
    """按脚本顺序回放的假 LLM（逐字符流式）。"""
    provider = "fake"
    model = "seq"

    def __init__(self, scripts):
        self.scripts = scripts
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, **kw):
        s = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 3, "completion_tokens": 1}
        self.last_tool_calls = []
        if stream:
            def g():
                for ch in s:
                    yield ch
            return g()
        return s

    def count_tokens(self, text):
        return max(1, len(text) // 3)


class _Iso(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_sub_")
        self._t = agent_trace.TRACE_FILE
        self._s = sessions.SESSIONS_DIR
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "t.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "s")

    def tearDown(self):
        agent_trace.TRACE_FILE = self._t
        sessions.SESSIONS_DIR = self._s


class DelegateTests(_Iso):
    def test_delegate_runs_subagent_and_returns_conclusion(self):
        llm = _SeqLLM([
            "Thought: 交给检索专员\nAction: delegate\nAction Input: role: researcher\ntask: 查 _ready 在哪",
            "Final Answer: 子代理结论：_ready 在 player.gd:18",
            "Final Answer: 汇总：_ready 在 player.gd:18",
        ])
        a = agent_mod.Agent(llm=llm)
        events = list(a.run("_ready 在哪", stream=True))
        obs = [e for e in events if e["type"] == "observation"]
        self.assertTrue(any("子代理 researcher 结论" in o["text"] for o in obs),
                        [o["text"][:60] for o in obs])
        self.assertEqual(events[-1]["type"], "final")
        self.assertIn("汇总", events[-1]["text"])
        # delegate 只算一步工具
        self.assertEqual(agent_trace.recent(1)[0]["n_steps"], 1)

    def test_depth_limit_stops_recursion(self):
        a = agent_mod.Agent(llm=_SeqLLM([]), depth=agent_mod.SUBAGENT_MAX_DEPTH)
        out = a._delegate("role: researcher\ntask: x")
        self.assertIn("最大嵌套深度", out)

    def test_an_ad_hoc_role_is_accepted(self):
        """自造角色**不再**被拒 —— 主 agent 需要什么角色就说什么角色。

        这条以前叫 `test_unknown_role_is_rejected`，断言 `role: wizard` 会被拒。
        那正是「harness 替模型决定该派什么角色」的那一层，已按「harness 兜底而非
        限制」的取向移除（角色名不再限那 11 个枚举，见
        `tests/test_agent_subagent_autonomy.py`）。

        改的不只是断言方向：**被拒的原因**也换了。原来拒绝发生在「角色不认识」
        这一步，现在它合法了，所以要么真正跑起来（成功），要么因为别的原因失败
        （比如这里的假 LLM 被问空）。关键是不能**因为角色名**而失败。
        """
        a = agent_mod.Agent(llm=_SeqLLM([]))
        out = a._delegate("role: wizard\ntask: x")
        self.assertNotIn("角色错误", out,
                         "自造角色不该再被当成角色错误拒绝：%r" % out)
        self.assertNotIn("未知角色", out,
                         "自造角色不该再被当成未知角色：%r" % out)

    def test_a_missing_role_name_is_still_refused(self):
        """反向守卫：放开的是「自造角色」，不是「什么都不填」。

        完全不给角色名要说清怎么办 —— 那不是解放，那是把输入校验也扔了。

        注意这条**不**该断言文案是「参数错误」：缺角色名是在 `_run_child` 里
        被拦的（那里才知道有哪些内置角色、也能告诉主 agent 它可以自造），
        `_delegate` 只校验「有没有 role: 这一行」。实测断言「参数错误」会红——
        那是断言写错，不是行为错（实测输出见该用例的错误信息）。
        """
        a = agent_mod.Agent(llm=_SeqLLM([]))
        out = a._delegate("task: x")
        self.assertIn("没给角色名", out,
                      "缺角色名应被拒绝并说清怎么办：%r" % out)
        # 提示要给出路：既列出内置角色，也说明可以自己起一个
        self.assertIn("也可以自己起一个", out)

    def test_missing_task_is_rejected(self):
        a = agent_mod.Agent(llm=_SeqLLM([]))
        out = a._delegate("role: researcher")
        self.assertIn("参数错误", out)

    def test_subagent_allowlist_blocks_other_tools(self):
        llm = _SeqLLM([
            "Action: calculate\nAction Input: 1+1",
            "Final Answer: done",
        ])
        a = agent_mod.Agent(llm=llm, tool_allowlist=["read_file"])
        events = list(a.run("q", stream=True))
        obs = [e["text"] for e in events if e["type"] == "observation"]
        self.assertTrue(any("[受限]" in t for t in obs), obs)


class PlanModeTests(_Iso):
    def test_plan_mode_emits_plan_event_once(self):
        llm = _SeqLLM(["Plan:\n1. 先检索相关文件\n2. 再给出结论\nFinal Answer: 计划完成"])
        a = agent_mod.Agent(llm=llm, plan_mode=True)
        events = list(a.run("帮我分析", stream=True))
        plans = [e for e in events if e["type"] == "plan"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["steps"], ["先检索相关文件", "再给出结论"])
        self.assertEqual(events[-1]["type"], "final")

    def test_plan_mode_adds_system_instruction(self):
        a = agent_mod.Agent(llm=_SeqLLM([]), plan_mode=True)
        msgs = a._build_messages("q")
        self.assertTrue(any("计划模式" in (m.get("content") or "") for m in msgs))

    def test_plan_disabled_by_default(self):
        a = agent_mod.Agent(llm=_SeqLLM([]))
        msgs = a._build_messages("q")
        self.assertFalse(any("计划模式" in (m.get("content") or "") for m in msgs))


if __name__ == "__main__":
    unittest.main()
