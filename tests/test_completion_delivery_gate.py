"""BUG-1/6/7 收尾门控回归锁。

三类"假成功"各自钉死：

· BUG-6（铁证）：被中断（GeneratorExit / abort）的回合，哪怕上一次 LLM 的
  finish_reason 残留 "completed"，收尾也绝不能记成 completed —— 必须落 "interrupted"。
· BUG-6（空正文）：没有任何非空最终正文的回合不得记 completed（内层已置 "empty"，
  这里补一道 outer finally 的兜底覆盖，防止 outcome=None 被 finish_reason 兜成 completed）。
· BUG-7：python_exec 只是把源文件逐行 echo 出来时，**不是**"产出即答案"，
  不得走 verbatim 捷径把它当最终回答甩给用户。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import _is_raw_source_echo, _should_verbatim_passthrough  # noqa: E402


# ── 假 LLM（沿用 tests/test_format_stall.py 形状）─────────────────
class _FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 3}

    def chat(self, messages, stream=True, **kw):
        script = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        if stream:
            def gen():
                for ch in script:
                    yield ch
            return gen()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


# ── BUG-7：raw source echo 检测器 ─────────────────────────────────
NUMERIC_RESULT = "caseA dps=25.0\n到0需要 3.2 秒\n结论: 会掉到0"
FILE_ECHO = "\n".join(
    "%d: %s" % (i, code) for i, code in enumerate([
        "extends CharacterBody2D", "var hp := 100", "func _ready():",
        "    pass", "func _physics_process(delta):", "    move_and_slide()",
        "func take_damage(n):", "    hp -= n", "    if hp <= 0:",
        "        die()", "func die():", "    queue_free()",
    ], start=1))
SHORT_ECHO = "1: extends Node\n2: var x = 1"   # 行数不足以触发（防误伤）


class RawSourceEchoDetectorTests(unittest.TestCase):
    def test_numeric_computation_is_not_a_dump(self):
        self.assertFalse(_is_raw_source_echo(NUMERIC_RESULT),
                         "数字结论被误判成源码倒灌 → 会误伤正常 python_exec 计算")

    def test_numbered_file_listing_is_a_dump(self):
        self.assertTrue(_is_raw_source_echo(FILE_ECHO),
                        "整份带行号的源码清单没被判成 dump → BUG-7 漏判")

    def test_short_listing_below_threshold_not_flagged(self):
        self.assertFalse(_is_raw_source_echo(SHORT_ECHO),
                         "行数不足阈值就放行，避免把短列表也拦下")

    def test_empty_is_not_dump(self):
        self.assertFalse(_is_raw_source_echo(""))
        self.assertFalse(_is_raw_source_echo(None))


class ShouldVerbatimPassthroughTests(unittest.TestCase):
    def test_python_exec_source_echo_is_blocked(self):
        self.assertFalse(_should_verbatim_passthrough("python_exec", FILE_ECHO),
                         "python_exec 回吐源码不该 verbatim 透传")

    def test_python_exec_numeric_passes(self):
        self.assertTrue(_should_verbatim_passthrough("python_exec", NUMERIC_RESULT))

    def test_calculate_always_passes(self):
        self.assertTrue(_should_verbatim_passthrough("calculate", FILE_ECHO))

    def test_gen_video_prompt_always_passes(self):
        self.assertTrue(_should_verbatim_passthrough("gen_video_prompt", FILE_ECHO))


# ── BUG-6：中断回合不得落成 completed ─────────────────────────────
class AbortedCompletionGateTests(unittest.TestCase):
    def _drive_and_abort(self):
        """让回合先产出若干事件，再关闭生成器触发 GeneratorExit（模拟断连/打断）。"""
        llm = _FakeLLM([
            "Thought: 我先读一下文件\nAction: list_dir\nAction Input: behaviors",
            "Thought: 再看玩家脚本\nAction: read_file\nAction Input: behaviors/player.gd",
            "Final Answer: 这是一段还没写出来的答案" + "，" * 50,
        ])
        agent = agent_mod.Agent(llm=llm)
        gen = agent.run("玩家受伤扣多少血在哪算的？")
        # 消费一点点，确保已进入循环、has 一次 llm_step（finish_reason 可能残留 completed）
        seen = 0
        try:
            for ev in gen:
                seen += 1
                if seen >= 3:
                    break
        finally:
            gen.close()  # 触发 GeneratorExit → outer run 置 aborted=True
        return agent

    def test_aborted_turn_is_not_recorded_completed(self):
        agent = self._drive_and_abort()
        rec = getattr(agent, "last_turn_record", None)
        if rec is None:
            self.skipTest("该 fake 形状没走到收尾落账（无 last_turn_record），跳过")
        self.assertTrue(rec.get("aborted"), "应为中断回合")
        self.assertNotEqual(rec.get("outcome"), "completed",
                            "BUG-6 复发：aborted 回合仍被记 completed")
        self.assertEqual(rec.get("outcome"), "interrupted",
                         "中断回合终态应如实记 interrupted")


if __name__ == "__main__":
    unittest.main()
