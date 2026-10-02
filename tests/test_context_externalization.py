"""上下文压缩是**有损**的：摘要有取舍，摘得越多、任务细节退化越明显。

所以"让长对话不退化"的正解不是压得更狠，而是**把任务状态搬出上下文**。
这里钉住这件事真的成立——否则它只是一句听起来很对的设计说明。

验证方式刻意选了**最强的那一版**：先建清单 → 把对话历史清空到只剩摘要
（模拟压缩已经把细节摘掉）→ 新一轮仍然知道要做什么。

如果只测"清单存在时能读出来"，会漏掉整个设计的核心：压缩之后还在。
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import sessions  # noqa: E402
from agent_runtime import plan_state  # noqa: E402


class CompactionResilienceTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="compact-plan-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._old = config.get_runtime("code_root", "")
        config.set_runtime("code_root", self.root)
        self.addCleanup(config.set_runtime, "code_root", self._old)

    def _agent(self):
        import agent as agent_mod
        return agent_mod.Agent(llm=None)

    def test_the_goal_survives_a_wiped_conversation(self):
        """核心断言：历史被摘空之后，下一步仍然明确。"""
        plan_state.open_plan(self.root, "修登录超时",
                              ["定位超时点", "加日志", "补测试", "跑一遍"])
        plan_state.set_done(self.root, "定位超时点", True)

        a = self._agent()
        # 模拟压缩之后：历史只剩一句摘要，具体细节已被摘掉
        a.history = [{"role": "user", "content": "上一轮在弄登录超时"},
                     {"role": "assistant", "content": "（细节已被摘要压缩）"}]
        a.summary = "之前在处理登录超时问题。"

        joined = " ".join(str(m.get("content") or "") for m in a._build_messages("继续"))
        self.assertIn("加日志", joined,
                      "对话被压缩后仍必须知道下一件事——否则外置清单白做了")
        self.assertIn("下一件未完成", joined.replace("下一件事", "下一件未完成")
                      if "下一件未完成" in joined else "未完成")

    def test_it_does_not_depend_on_conversation_history_at_all(self):
        """更强的版本：历史完全为空也要能用。"""
        plan_state.open_plan(self.root, "T", ["第一步", "第二步"])
        a = self._agent()
        a.history = []
        a.summary = ""
        joined = " ".join(str(m.get("content") or "") for m in a._build_messages("开始"))
        self.assertIn("第一步", joined)

    def test_finished_items_do_not_come_back_after_compaction(self):
        """压缩后把已完成项重新提示出来，等于诱导模型重做一遍。"""
        plan_state.open_plan(self.root, "T", ["a", "b", "c"])
        plan_state.set_done(self.root, "a", True)
        plan_state.set_done(self.root, "b", True)
        a = self._agent()
        a.history = [{"role": "user", "content": "很久以前"}]
        joined = " ".join(str(m.get("content") or "") for m in a._build_messages("继续"))
        self.assertNotIn("- [ ] a", joined)
        self.assertNotIn("- [ ] b", joined)
        self.assertIn("c", joined)


class CompactionBudgetInteractionTests(unittest.TestCase):
    """清单是每轮注入的，所以它自己占 token——必须小。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="compact-budget-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._old = config.get_runtime("code_root", "")
        config.set_runtime("code_root", self.root)
        self.addCleanup(config.set_runtime, "code_root", self._old)

    def test_a_long_plan_does_not_blow_up_the_injection(self):
        """40 项的计划若全量注入，每轮都在为重复信息付费。"""
        plan_state.open_plan(self.root, "大计划",
                              ["任务%02d" % i for i in range(40)])
        hint = plan_state.summary_for_prompt(self.root)
        self.assertLessEqual(
            len(hint), 1200,
            "注入 %d 字符，超出预算：长清单必须截断并提示去 read 看全量" % len(hint))
        self.assertIn("还有", hint, "截断后要告诉模型还有多少项、以及去哪看全量")

    def test_the_injection_is_smaller_than_the_raw_plan(self):
        plan_state.open_plan(self.root, "T", ["条目" * 20] * 30)
        raw = plan_state.render(plan_state.read(self.root))
        hint = plan_state.summary_for_prompt(self.root)
        self.assertLess(len(hint), len(raw),
                        "注入版必须比全量清单更短，否则注入反而是负担")

    def test_no_plan_means_zero_tokens(self):
        self.assertEqual(plan_state.summary_for_prompt(self.root), "")


class MaybeCompactStillWorksTests(unittest.TestCase):
    """加了清单**不能**改坏既有的摘要压缩——那是另一条独立机制。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="compact-sess-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp
        self.addCleanup(setattr, sessions, "SESSIONS_DIR", self._old)

    def test_maybe_compact_reduces_history(self):
        # 历史条目的结构是 {"user","assistant"}（不是 role/content）——`_turns_tokens`
        # 按 t["user"]/t["assistant"] 取值，写错结构时它读到空串，于是 token 恒为 0、
        # 压缩永不触发，测试会以"没压缩"的样子失败。所以这里用真实结构。
        history = []
        for i in range(40):
            history.append({"user": "问题%d " % i + "内容" * 60,
                            "assistant": "回答%d " % i + "内容" * 60})
        kept, _summary = sessions.maybe_compact(
            "sess-compact", history, None,
            trigger_tokens=200, keep_tokens=10, project_id=None)
        self.assertLess(len(kept), len(history),
                        "超过 keep_tokens 后应弹出最旧的条目")
        self.assertGreaterEqual(len(kept), sessions.KEEP_TURNS_MIN,
                                "最近 KEEP_TURNS_MIN 条有免死金牌，不能全弹光")


if __name__ == "__main__":
    unittest.main()