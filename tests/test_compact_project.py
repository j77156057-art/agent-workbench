# -*- coding: utf-8 -*-
"""P3 收尾回归：`sessions.maybe_compact` 的摘要读写必须按项目桶隔离。

背景（P3 残留风险）：`maybe_compact` 内部通过 `summary_text(session_id)` 读取「上一版摘要」，
若不透传 project_id，会按「当前项目」读取——当请求指向**非当前项目**时，会把别的项目的
摘要读进来、并据它拼接出新摘要写回去，破坏跨项目隔离。

覆盖：
① 触发压缩时（读 `prev` + 写回），摘要必须基于**本项目**的旧摘要，不得混入它项目文本；
② `project_id=None` 仍按「当前项目」工作（回归，等价改动前）；
③ `maybe_compact` 的三处 `summary_text` 读取分支（未触发 / kept 未变 / 触发压缩）
   都遵守项目参数——逐一验证，防止漏传。

全部离线、隔离：把 `config.STATE_FILE` 与 `sessions.SESSIONS_DIR` 指到临时目录。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import projects  # noqa: E402
import sessions  # noqa: E402


class CompactProjectBase(unittest.TestCase):
    SID = "sX"
    SUM_A = "A-OLD-摘要-仅属于A"
    SUM_B = "B-SECRET-摘要-仅属于B"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_compact_")
        self._old_state_file = config.STATE_FILE
        self._old_sessions_dir = sessions.SESSIONS_DIR
        self._old_runtime = dict(config._RUNTIME)
        config.STATE_FILE = os.path.join(self.tmp, ".docmind_state.json")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        self._env = mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "1"})
        self._env.start()

        self.pid_a = projects.ensure_project(self._mkdir("Alpha"))
        self.pid_b = projects.ensure_project(self._mkdir("Beta"))
        # ⚠⚠ 前提变了（2026-10-10 乙方案）：一个 session_id 就是一个会话，
        # 摘要跟着**会话**走，不属于项目。旧前提「同一 SID 在 A/B 各存一份
        # 不同摘要」已不成立 —— 第二次 save 会读回第一次的内容再覆盖。
        # 现在改用**两个不同 session_id** 来验证「互不串」。
        self.SID_A = "compact-sid-a"
        self.SID_B = "compact-sid-b"
        self.assertTrue(sessions.save(self.SID_A, [{"user": "a1", "assistant": "r1"}],
                                      summary=self.SUM_A))
        self.assertTrue(sessions.save(self.SID_B, [{"user": "b1", "assistant": "r1"}],
                                      summary=self.SUM_B))

    def tearDown(self):
        self._env.stop()
        config.STATE_FILE = self._old_state_file
        sessions.SESSIONS_DIR = self._old_sessions_dir
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mkdir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _turns(n):
        return [{"user": f"问题{i}-需要被压缩进摘要", "assistant": f"回答{i}"} for i in range(n)]

    def _trigger(self, sid, n=6):
        """强制触发压缩：trigger/keep 取极小值，轮数 > KEEP_TURNS_MIN。"""
        return sessions.maybe_compact(sid, self._turns(n), llm=None,
                                      trigger_tokens=1, keep_tokens=1)


class CompactIsolationTests(CompactProjectBase):
    def test_compaction_reads_and_keeps_own_session_summary(self):
        """压缩必须基于**这个会话自己的**旧摘要。
        旧断言「A 项目的摘要」已改为「SID_A 的摘要」—— 摘要不属于项目。"""
        kept, summary = self._trigger(self.SID_A)
        self.assertIn(self.SUM_A, summary, "压缩结果必须基于 SID_A 自己的旧摘要")
        self.assertNotIn(self.SUM_B, summary, "不得把别的会话的摘要混入")
        self.assertLess(len(kept), 6, "应确实发生了压缩")
        # 未写回另一个会话：它的旧摘要保持不变
        self.assertEqual(sessions.summary_text(self.SID_B), self.SUM_B)

    def test_compaction_for_other_session_isolated(self):
        """另一个会话的压缩不得看到本会话的摘要（隔离换了维度：session_id）。"""
        _, summary_b = self._trigger(self.SID_B)
        self.assertIn(self.SUM_B, summary_b)
        self.assertNotIn(self.SUM_A, summary_b)
        self.assertEqual(sessions.summary_text(self.SID_A), self.SUM_A)

    def test_summary_is_not_scoped_by_project(self):
        """乙方案的核心：摘要跟会话走，换项目仍然读得到。
        旧前提「不带 project_id 时按当前项目」已推翻 —— project_id 不再
        影响存储位置，同一 session_id 在任何项目下都是同一份。"""
        self.assertTrue(projects.set_current(self.pid_a))
        _, s_a = sessions.maybe_compact(self.SID_A, self._turns(6), llm=None,
                                        trigger_tokens=1, keep_tokens=1)
        self.assertIn(self.SUM_A, s_a)
        self.assertNotIn(self.SUM_B, s_a)
        # 当前切到 B：同一 session_id 的摘要仍在（不再随项目切换）
        self.assertTrue(projects.set_current(self.pid_b))
        _, s_b = sessions.maybe_compact(self.SID_A, self._turns(6), llm=None,
                                        trigger_tokens=1, keep_tokens=1)
        self.assertIn(self.SUM_A, s_b,
                      "切项目后读不到同一会话的摘要 —— 又成分桶了")
        self.assertNotIn(self.SUM_B, s_b,
                         "读到了别的会话的摘要 —— 隔离丢了")


class CompactAllReadBranchesTests(CompactProjectBase):
    """逐一覆盖 maybe_compact 内三处 summary_text 读取分支。

    旧契约「读取遵守 project_id」已改为**遵守 session_id**：摘要属于会话。
    """

    def test_branch_not_triggered_returns_session_summary(self):
        # 不触发（trigger 极大）→ 走第一个 return 分支
        turns = self._turns(3)
        out_turns, summary = sessions.maybe_compact(self.SID_A, turns, llm=None,
                                                    trigger_tokens=10 ** 9,
                                                    keep_tokens=10 ** 9)
        self.assertEqual(out_turns, turns, "未触发时应原样返回 turns")
        self.assertEqual(summary, self.SUM_A)
        _, summary_b = sessions.maybe_compact(self.SID_B, turns, llm=None,
                                              trigger_tokens=10 ** 9, keep_tokens=10 ** 9)
        self.assertEqual(summary_b, self.SUM_B)

    def test_branch_kept_unchanged_returns_session_summary(self):
        # over 阈但轮数 ≤ KEEP_TURNS_MIN → 走「kept >= len(turns)」分支
        turns = self._turns(sessions.KEEP_TURNS_MIN)
        out_turns, summary = sessions.maybe_compact(self.SID_A, turns, llm=None,
                                                    trigger_tokens=1, keep_tokens=1)
        self.assertEqual(out_turns, turns)
        self.assertEqual(summary, self.SUM_A)
        _, summary_b = sessions.maybe_compact(self.SID_B, turns, llm=None,
                                              trigger_tokens=1, keep_tokens=1)
        self.assertEqual(summary_b, self.SUM_B)


if __name__ == '__main__':
    unittest.main()
