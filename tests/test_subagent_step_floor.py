# -*- coding: utf-8 -*-
"""子代理必须有兜底上限——「0 = 不限」不能字面实现。

## 事故

`tests/test_orchestrator.py::test_child_without_final_degrades_instead_of_empty`
**无限挂死**：单独跑超8 分钟、单线程满负荷（CPU 持续增长，不是死锁等IO）。

## 根因（逐个量出来的，不是读注释猜的）

`_run` 的主循环是 `while True:`，而它**唯一**的终止条件是：

```python
if tool_step_limit > 0 and iterations > tool_step_limit + …:
```

而 `tool_step_limit` 的来源链上，三处默认值全是 0：

```
MAX_AGENT_STEPS = 0    SUBAGENT_MAX_STEPS = 0    SUBAGENT_STEPS_HARD_CAP = 0
settings: main_max_steps=0  child_max_steps=0
=> 子代理真正生效的步数上限 = 0（0 的约定是「不设上限」）
```

于是 `tool_step_limit == 0` 时循环**结构上无界**。

两道防空转守卫都拦不住那个假模型（`Thought: 继续检索第 N 轮 / Action: search_code`）：

- `executed` 按 `(工具, 参数)` 去重 —— 它每轮换`query: termN`
- `_repeated_sentence` 要求逐字重复 —— 它每轮换措辞与轮次号

## 为什么这条不能只改测试

主 Agent 还有格式失守兜底（`format_stalls`）和用户可点的「中断」按钮；
子代理是工作流并发扇出的，**没有逐个停掉它的入口**。一个不收尾的子代理
会把整轮工作流永远挂住，而用户只能去杀进程。

所以修的是产品：`_run_child` 在 `cap == 0` 时落 `SUBAGENT_STEP_FLOOR`。
这不是把 0 夹回 1（那正是 2026-10 那次改动要消除的拦截）——0 仍然表示
「不人为收紧」，只是不再等于「字面无限」。

## 为什么这个文件以前没人跑

它不在常用的文件名字符串过滤器里，于是每一轮回归都自然地把它排除在外。
**一个会挂死的测试文件会把自己挤出回归范围，而且没人发现。**
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


class NeverFinalLLM:
    """格式完全正确、但永远不肯给 Final Answer 的模型。

    刻意每轮换参数与措辞：`executed`（按工具+参数去重）与
    `_repeated_sentence`（逐字重复）这两道防空转守卫都拦不住它。
    """

    provider = "fake"
    model = "never-final"

    def __init__(self):
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def clone(self):
        return NeverFinalLLM()

    def chat(self, messages, stream=True, **kw):
        self.last_usage = {"prompt_tokens": 3, "completion_tokens": 1}
        self.last_tool_calls = []
        self.i += 1
        text = ("Thought: 继续检索第 %d 轮，先看看相关实现\n"
                "Action: list_dir\nAction Input: path: sub%d" % (self.i, self.i))
        if stream:
            return (ch for ch in text)
        return text

    def count_tokens(self, text):
        return 1


class SubagentStepFloorTests(unittest.TestCase):
    """兜底上限必须真的兜住，否则下一个人会把它改回去。"""

    def test_the_floor_is_a_positive_finite_number(self):
        self.assertGreater(agent.SUBAGENT_STEP_FLOOR, 0,
                           "兜底上限为 0 等于没有兜底")
        self.assertLess(agent.SUBAGENT_STEP_FLOOR, 100000,
                       "兜底上限大到等于没有上限")

    def test_the_child_gets_an_override_even_when_cap_resolves_to_zero(self):
        """cap=0 时也必须设 tool_step_override —— 否则 `_run` 结构上无界。"""
        self.assertEqual(agent._resolve_child_max_steps(None), 0,
                         "前提变了：_resolve_child_max_steps(None) 已不再是 0，"
                         "这条用例要改成验'用户显式传 0'那条路径")

        a = agent.Agent(llm=NeverFinalLLM())
        seen = {}

        # 不真跑 ReAct：只要能读到子代理实际拿到的 override 就够验这件事了。
        orig_run = agent.Agent.run

        def spy_run(self, question, stream=False):
            seen["override"] = self.tool_step_override
            return iter([{"type": "final", "text": "收尾"}])

        agent.Agent.run = spy_run
        try:
            a._run_child("researcher", "查一些东西")
        finally:
            agent.Agent.run = orig_run

        self.assertEqual(seen.get("override"), agent.SUBAGENT_STEP_FLOOR,
                         "cap=0 时子代理没有被兜底上限兜住")


class SubagentRunTerminatesTests(unittest.TestCase):
    """真的跑一遍：不给 max_steps 也必须停下来。"""

    def test_a_child_that_never_concludes_still_terminates(self):
        """以前这条会**无限挂死**——所以它给 60 秒硬上限，红了就说明没兜住。"""
        started = time.monotonic()
        a = agent.Agent(llm=NeverFinalLLM())
        out = a._run_child("researcher", "查一些东西")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 60.0,
                        "没有在 60 秒内收尾——兜底上限没起作用")
        self.assertTrue(out.get("conclusion", "").strip(),
                        "收尾了但结论是空的——下游会拿到空上下文")
        self.assertLess(out.get("steps", 0), agent.SUBAGENT_STEP_FLOOR + 5,
                        "跑的步数超过了兜底上限——上界没生效")

    def test_the_floor_is_not_the_old_one_step_clamp(self):
        """0 不该被夹回 1：那正是 2026-10 那次改动要消除的拦截。"""
        self.assertNotEqual(agent.SUBAGENT_STEP_FLOOR, 1,
                            "兜底上限被压成 1 步——那比不放更糟")


if __name__ == "__main__":
    unittest.main()