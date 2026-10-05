# -*- coding: utf-8 -*-
"""「检索无匹配」不得把子代理熔断掉 —— 端到端守卫。

## 为什么单测 `_is_failure` 不够

本族第一次修的时候，只证明了 `_is_failure` 的返回值变了。真正的伤害发生在
**下游**：`_tool_ok=False` -> 反思提示 -> `retry_budget_cost` 计入 ->
`_TOOL_FAIL_LIMIT=3` 熔断 -> 子代理强制收尾。所以这里直接跑一次子代理，
断言它**跑完脚本里所有步骤**。

## 实测的修复前形态

    设 code_root + retriever 返回空 -> n_steps=3（脚本要求 9 步）
    结论='（工具 search_code 已连续失败 3 次，模型未能换出可用的调用方式。）'

而步数上限是 10——也就是说**熔断比上限先触发**。

## 判据口径

用户定的：**无匹配不算失败**。所以：
- 空检索结果不消耗「连续失败」配额
- 不触发「工具未返回有效结果」那条反思
- 子代理能跑完脚本里的 9 步
- **真失败仍然熔断**（反向守卫）

## 层归属

纯层：假 LLM + 替换 retriever，不碰向量库、不起网络。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _ScriptLLM:
    """按序回放脚本（形状对齐 `tests/test_orchestrator.py` 里的同名类）。

    必须逐项对齐 provider / model / clone / 流式 / count_tokens——
    少一项子代理就会在**第一次 LLM 调用之前**失败，探针本身测不到东西。
    """

    provider = "fake"
    model = "script"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def clone(self):
        return _ScriptLLM(self.scripts)

    def chat(self, messages, stream=True, **kw):
        s = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 2}
        self.last_tool_calls = []
        if stream:
            def g():
                for ch in s:
                    yield ch
            return g()
        return s

    def count_tokens(self, text):
        return 1


class NoMatchIsNotFailureTests(unittest.TestCase):
    def setUp(self):
        self.old_root = tools_mod.get_runtime("code_root")
        self.old_cap = agent_mod.SUBAGENT_MAX_STEPS
        self.old_steps = agent_mod.ORCH_TRACE_STEPS
        self.addCleanup(self._restore)

        # 有 code_root -> search_code 走真检索；把 retriever 换成返回空
        tools_mod.set_runtime("code_root", REPO)
        agent_mod.SUBAGENT_MAX_STEPS = 10
        agent_mod.ORCH_TRACE_STEPS = 3
        retriever = mock.Mock(invoke=lambda q: [])
        self._patches = [
            mock.patch.object(tools_mod, "get_retriever", lambda **_k: retriever),
            mock.patch.object(tools_mod, "_get_emb", lambda: None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _restore(self):
        tools_mod.set_runtime("code_root", self.old_root)
        agent_mod.SUBAGENT_MAX_STEPS = self.old_cap
        agent_mod.ORCH_TRACE_STEPS = self.old_steps

    def _run(self, n_actions, action="search_code", query="t%d"):
        """跑一个子代理，回放 n_actions 步 + 一句 Final Answer。

        只用**真工具**、不 mock `execute_tool`：第一版为了控制观察文本去打了
        `agent.execute_tool` 的桩，而那些 patch 是手工 start/stop、没有注册
        cleanup，于是**泄漏到后续用例**——同一类里后面的用例拿到的是上一条
        桩返回的文本，两条守卫测的是空气（n_steps=1、obs 为空）。

        「真失败仍熔断」那条守卫改用**真的会失败**的 `read_file`（读一个不存在
        的路径），这样既不需要桩，也更接近真实形态。
        """
        # `query` 是**格式串**，必须应用 `% i`——第一版直接把 `"t%d"` 拼进去，
        # `Action Input: query: t%d` 里那个字面 `%` 会让参数解析失败。
        arg_name = "query" if action == "search_code" else "path"
        scripts = ["Action: %s\nAction Input: %s: %s"
                   % (action, arg_name, query % i) for i in range(n_actions)]
        scripts.append("Final Answer: 完成")
        a = agent_mod.Agent(llm=_ScriptLLM(scripts))
        out = a._run_child("researcher", "查")
        return out, (out or {}).get("trace") or {}

    def test_reverse_guard_empty_retrieval_must_not_melt_the_subagent_down(self):
        """**反向守卫**：空检索不得触发熔断。

        修复前：`n_steps=3`（脚本要求 9 步），而步数上限是 10——熔断比上限
        先触发，结论还误导成「模型未能换出可用的调用方式」。
        """
        _out, tr = self._run(9)

        self.assertEqual(
            tr.get("n_steps"), 9,
            "空检索被判成失败，第 3 次就熔断了（n_steps=%r）" % tr.get("n_steps"),
        )

    def test_the_observation_still_tells_the_model_there_was_no_match(self):
        """不许为了让它不算失败就把提示语一起弄没——模型需要知道「查不到」。"""
        _out, tr = self._run(9)

        self.assertIn("未找到相关内容", str((tr.get("steps") or [{}])[0].get("obs")))

    def test_a_real_failure_still_melts_it_down(self):
        """**反向守卫**：真失败仍然必须熔断，否则就把护栏拆了。

        用**真的会失败**的 `read_file`（读一个不存在的路径，拿到真的
        「文件不存在」），不 mock 任何工具——既避免 patch 跨用例泄漏，
        也更接近真实形态。
        """
        _out, tr = self._run(9, action="read_file", query="src/missing-%d.py")

        self.assertLess(
            tr.get("n_steps") or 0, 9,
            "真实失败没有触发熔断，护栏被拆了（n_steps=%r）" % tr.get("n_steps"),
        )

    def test_the_misleading_conclusion_is_gone(self):
        """**反向守卫**：不许再出现「模型未能换出可用的调用方式」这种结论。

        它把「确实没有匹配」说成「模型不会用工具」——用户看到的是误导。
        """
        out, _tr = self._run(9)

        self.assertNotIn("未能换出可用的调用方式", str(out.get("conclusion") or ""))


if __name__ == "__main__":
    unittest.main()