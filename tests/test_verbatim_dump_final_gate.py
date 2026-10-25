"""BUG-7 终态守卫回归锁：模型把带行号的源码整段裸铺当 Final Answer 时，
先逼它**综合一次**，仍裸铺就**如实标 degraded** 交付——绝不把原始 dump 冒充综合结论。

钻测实据（X / T / U / I / J）：跨多文件批量 gather 后，模型的"最终答案"就是一屏
`12: 代码` 清单、几乎无提炼，而 status 仍是 done（假完成）。此前 python_exec 捷径已由
`_should_verbatim_passthrough` 挡下，但**模型在自己的 Final Answer 里重贴源码**这条
路径没有任何拦截。本锁钉住 agent.py 新增的终态门（`_is_raw_source_echo` + 一次 nudge
+ 仍裸铺则 degraded）。

注意：本仓库对"定位类"问题还有一道**既有的证据核验门**（`_audit_live_answer`），它也会
补一轮、发自己的 reflection。所以本锁**只认自己守卫独有的信号**——`裸铺` 那条 reflection、
`degraded` 标记、以及交付文本里的 `原始源码清单` 注记——不靠调用次数判定，避免与证据门互相干扰。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402

# 一屏带行号的源码清单：≥10 行、逐行都是 `N: 代码`，稳稳命中 _is_raw_source_echo。
_RAW_DUMP = "Final Answer:\n" + "\n".join(
    "%d:\tvar node_%d = get_node(\"n%d\")" % (i, i, i) for i in range(1, 16)
)

# 同一段被**综合成结论**后的样子：叙述 + 点到为止的行号引用，非逐行清单。
_SYNTHESIZED = (
    "Final Answer:\n"
    "player.gd 与 enemy.gd 各自负责移动与追击：take_damage 只在 player.gd:36 声明、"
    "函数体不再读取 source（是死参数）；伤害计算集中在 enemy.gd:44 调用点传入。"
)

_MARK = "裸铺"       # 只有本守卫的 reflection 会含这个词
_NOTE = "原始源码清单"  # 只有本守卫的 degraded 交付会追加这句


class _ScriptedFakeLLM:
    """按次序吐出预设回答的假模型；越界后重复最后一条，用来驱动到 Final Answer 分支。"""
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def chat(self, messages, stream=True, **kw):
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        if stream:
            def gen():
                for ch in text:
                    yield ch
            return gen()
        return text

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class VerbatimDumpFinalGateTests(unittest.TestCase):
    def _run(self, scripts):
        llm = _ScriptedFakeLLM(scripts)
        agent = agent_mod.Agent(llm=llm)
        events = list(agent.run("这套战斗逻辑整体是怎么运作的，帮我理一理"))
        return llm, events

    @staticmethod
    def _guard_reflections(events):
        return [e for e in events
                if e.get("type") == "reflection" and _MARK in str(e.get("text", ""))]

    def test_dump_then_synthesized_delivers_clean_final(self):
        # 第一次裸铺 → 本守卫逼综合一次；第二次给出真结论 → 正常交付，不打 degraded。
        _llm, events = self._run([_RAW_DUMP, _SYNTHESIZED, _SYNTHESIZED])
        refl = self._guard_reflections(events)
        self.assertEqual(len(refl), 1, "本守卫应恰好触发一次综合纠偏 reflection")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertFalse(finals[0].get("degraded"), "综合成功后不该标 degraded")
        self.assertNotIn(_NOTE, finals[0]["text"])

    def test_still_dump_after_nudge_delivered_as_is_no_stamp(self):
        # 「留地板、拆其余」：逼综合一次后仍裸铺 → 原样交付，不再挂事后横幅。
        # 一次性的综合纠偏 reflection（"裸铺"）保留（不可见、不冒充结论），
        # 但用户可见的 degraded 章 + "原始源码清单"注记已拆（那是"事后盖章"）。
        _llm, events = self._run([_RAW_DUMP])  # 每次都返回裸铺
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1, "终态守卫只应产出一条 final")
        self.assertFalse(finals[0].get("degraded"),
                         "事后 degraded 章已拆，不再打降级标记")
        self.assertNotIn(_NOTE, finals[0]["text"],
                         "事后『原始源码清单』注记已拆，不再追加")
        # 有界 nudge：本守卫的 reflection 至多一次，不能反复逼问形成死循环。
        self.assertLessEqual(len(self._guard_reflections(events)), 1)

    def test_pure_conclusion_answer_never_touched_by_gate(self):
        # 反向锁：一句话结论/数字结论绝不该被这条保守判据误伤（本守卫 reflection 不出现、
        # 也不打 degraded）。既有证据门可能另发它自己的 reflection，但那不含 `裸铺` 标记。
        _llm, events = self._run([
            "Final Answer:\n所以 9.9 更大，答案就是 9.9（caseB=3.2 不成立）。"])
        self.assertEqual(self._guard_reflections(events), [],
                         "结论型回答不该触发源码裸铺守卫的综合 nudge")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertNotIn(_NOTE, finals[0]["text"])


if __name__ == "__main__":
    unittest.main()
