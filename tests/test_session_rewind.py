"""会话 rewind / fork —— 补上「回到某一轮」这个一直缺失的能力。

## 为什么这个文件存在

会话历史是一条**不断被覆写的链**：每轮问答都 `save` 一次，只留最新状态。
于是"回到第 3 轮看看当时是什么样"这件事**根本做不到**——那个状态已经被覆盖。

这跟竞品差一档：Claude Code 有 checkpoints，Codex 用 worktree + resume。
`agent_memory` 里虽然有 `checkpoint()`，但它记的是**未完成轮次的现场**
（用于崩溃恢复，只保留最近 100 条），不是可回溯的轮次快照。

## 三条设计决定

1. **快照与会话同文件**（`snapshots` 字段），不另开文件。rewind 必须和会话同生共死，
   否则会出现"会话删了、快照还在"的孤儿状态。代价是文件变大，用 SNAPSHOT_LIMIT 兜住。

2. **快照存副本而非引用**。存引用的话后续 `save` 会把它一起改掉——那就不是快照而是别名，
   这个 bug 会表现为"rewind 回去发现还是现在的内容"，极难察觉。

3. **rewind 会顺带丢弃被回退掉的快照**。它们的 seq 已对应不上，留着会让下一次
   restore 回到不属于这条链的状态。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sessions  # noqa: E402


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rewind-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp
        self.addCleanup(setattr, sessions, "SESSIONS_DIR", self._old)
        self.sid = "sess-rewind"

    def _turn(self, n):
        return {"user": "问题%d" % n, "assistant": "回答%d" % n}


class SnapshotTests(Base):

    def test_a_snapshot_survives_later_saves(self):
        """这是本文件最核心的一条：存副本，不是引用。

        存引用的话，后续 save 会把快照一起改掉——rewind 回去会发现"还是现在的
        内容"，而且不报错，极难察觉。
        """
        sessions.save(self.sid, [self._turn(1)], "")
        sessions.snapshot(self.sid, [self._turn(1)], label="第一轮之后")
        # 继续聊三轮，每轮都覆写会话
        for n in (2, 3, 4):
            sessions.save(self.sid, [self._turn(1), self._turn(n)], "")
        got = sessions.restore_snapshot(self.sid, 1)
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["turns"]), 1, "快照被后续 save 改掉了——存的是引用不是副本")
        self.assertEqual(got["turns"][0]["user"], "问题1")

    def test_it_is_listed_without_dumping_every_turn(self):
        """列表接口带全量 turns 会让响应大到不可用。"""
        for n in range(1, 4):
            sessions.save(self.sid, [self._turn(i) for i in range(1, n + 1)], "")
            sessions.snapshot(self.sid, [self._turn(i) for i in range(1, n + 1)],
                              label="第%d轮" % n)
        items = sessions.snapshots(self.sid)
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertNotIn("turns", item, "列表项不该带完整 turns")
            self.assertIn("seq", item)
        self.assertEqual(items[-1]["question"], "问题3")

    def test_it_keeps_only_the_recent_ones(self):
        """快照无上限会让会话文件无限变大。"""
        for n in range(sessions.SNAPSHOT_LIMIT + 12):
            sessions.snapshot(self.sid, [self._turn(n)], label="n%d" % n)
        items = sessions.snapshots(self.sid)
        self.assertLessEqual(len(items), sessions.SNAPSHOT_LIMIT)

    def test_no_snapshot_yields_an_empty_list_not_an_error(self):
        self.assertEqual(sessions.snapshots(self.sid), [])


class RewindTests(Base):

    def _seed(self):
        """三个快照，分别对应 1 / 2 / 3 轮。"""
        for n in (1, 2, 3):
            turns = [self._turn(i) for i in range(1, n + 1)]
            sessions.save(self.sid, turns, "摘要%d" % n)
            sessions.snapshot(self.sid, turns, label="第%d轮" % n)
        # 当前已经聊到第 4 轮
        sessions.save(self.sid, [self._turn(i) for i in range(1, 5)], "摘要4")

    def test_rewind_restores_that_moments_history(self):
        self._seed()
        got = sessions.restore_snapshot(self.sid, 2)
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["turns"]), 2)
        self.assertEqual(got["turns"][-1]["user"], "问题2")

    def test_rewind_also_restores_the_summary(self):
        """摘要与 turns 必须同时回到那一刻，否则模型会拿着"第4轮的摘要"配"第2轮的历史"。"""
        self._seed()
        got = sessions.restore_snapshot(self.sid, 2)
        self.assertEqual(got["summary"], "摘要2")

    def test_it_persists(self):
        """回退必须真的落盘——否则刷新页面又变回去了。"""
        self._seed()
        sessions.restore_snapshot(self.sid, 1)
        self.assertEqual(len(sessions.history(self.sid)), 1)

    def test_a_missing_snapshot_explains_itself(self):
        """返回能直接显示给用户的话，而不是 404 或空串。"""
        self._seed()
        got = sessions.restore_snapshot(self.sid, 99)
        self.assertFalse(got["ok"])
        self.assertIn("找不到", got["reason"])

    def test_rewinding_with_no_snapshots_says_so(self):
        sessions.save(self.sid, [self._turn(1)], "")
        got = sessions.restore_snapshot(self.sid, 1)
        self.assertFalse(got["ok"])
        self.assertIn("还没有任何快照", got["reason"])

    def test_it_drops_the_snapshots_after_the_target(self):
        """它们的 seq 已对应不上，留着会让下次 restore 回到不属于这条链的状态。"""
        self._seed()
        sessions.restore_snapshot(self.sid, 1)
        remaining = [item["seq"] for item in sessions.snapshots(self.sid)]
        self.assertEqual(remaining, [1])


class ForkTests(Base):

    def _seed(self):
        for n in (1, 2, 3):
            turns = [self._turn(i) for i in range(1, n + 1)]
            sessions.save(self.sid, turns, "摘要%d" % n)
            sessions.snapshot(self.sid, turns, label="第%d轮" % n)

    def test_fork_leaves_the_original_untouched(self):
        """分叉的第一条不变量：原会话一个字都不能变。"""
        self._seed()
        before = sessions.history(self.sid)
        got = sessions.fork_session(self.sid, "sess-copy")
        self.assertTrue(got["ok"])
        self.assertEqual(sessions.history(self.sid), before)

    def test_fork_carries_the_history_over(self):
        self._seed()
        sessions.fork_session(self.sid, "sess-copy")
        self.assertEqual(len(sessions.history("sess-copy")), 3)

    def test_fork_from_a_snapshot_takes_that_moment(self):
        self._seed()
        got = sessions.fork_session(self.sid, "sess-old", at_seq=1)
        self.assertTrue(got["ok"])
        self.assertEqual(len(sessions.history("sess-old")), 1)

    def test_forking_from_a_snapshot_does_not_rewind_the_original(self):
        """fork 与 rewind 必须分开：fork 只读快照，rewind 才改原会话。"""
        self._seed()
        sessions.save(self.sid, [self._turn(i) for i in range(1, 5)], "摘要4")
        sessions.fork_session(self.sid, "sess-old", at_seq=1)
        self.assertEqual(len(sessions.history(self.sid)), 4,
                         "fork 把原会话也回退了——两条语义被混在一起了")

    def test_the_fork_does_not_inherit_the_snapshot_chain(self):
        """快照语义属于各自那条链；混用会让 restore 回到不属于它的状态。"""
        self._seed()
        sessions.fork_session(self.sid, "sess-copy")
        self.assertEqual(sessions.snapshots("sess-copy"), [])

    def test_an_empty_new_id_is_refused_with_a_reason(self):
        self._seed()
        got = sessions.fork_session(self.sid, "  ")
        self.assertFalse(got["ok"])
        self.assertIn("不能为空", got["reason"])

    def test_forking_an_empty_session_is_refused(self):
        got = sessions.fork_session("never-existed", "sess-copy")
        self.assertFalse(got["ok"])


class ApiSurfaceTests(unittest.TestCase):
    """端点必须真的挂在 app 上——模块写了没接线是最常见的"看起来做完了"。"""

    def setUp(self):
        self.api_src = self._read("api.py")

    def _read(self, rel):
        with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as h:
            return h.read()

    def test_the_three_endpoints_exist(self):
        for path in ("/api/sessions/{session_id}/snapshots",
                     "/api/sessions/{session_id}/rewind",
                     "/api/sessions/{session_id}/fork"):
            self.assertIn(path, self.api_src, "缺少端点 %s" % path)

    def test_the_old_detail_endpoint_is_not_broken(self):
        """新端点是同级新增，不能顶掉原来的 GET/DELETE。"""
        self.assertIn('@app.get("/api/sessions/{session_id}")', self.api_src)
        self.assertIn('@app.delete("/api/sessions/{session_id}")', self.api_src)

    def test_rewind_also_drops_the_live_agent_history(self):
        """内存里的 Agent 历史必须一起退，否则重开工作台旧历史又回来。"""
        self.assertIn("_SESSION_AGENTS", self.api_src)


if __name__ == "__main__":
    unittest.main()