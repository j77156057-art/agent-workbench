# -*- coding: utf-8 -*-
"""长会话「不再删历史」回归：压缩弹出的更早轮次完整归档，UI 回灌完整逐轮。

## 背景（用户报的 Bug）
「历史记录保存的好像只有前几个，对话一多会删除前面的对话」。

根因：`agent.py` 的长会话压缩（`sessions.maybe_compact`）一旦触发，会把弹出的
最早若干轮直接 `save(kept)` 覆盖回会话文件——`turns` 里早期轮次被真删，只留一段
有损摘要。UI 重载时读的就是这份 `turns`，于是前面的对话消失。

## 修法（本文件钉的契约）
压缩逻辑本身不动（喂 LLM 的工作窗口 `turns` 仍按压缩结果走，行为逐字节不变）；
新增一个只增不删的 `archived` 字段：每次压缩把弹出的前缀累进它。UI 与列表读
`archived + turns` = 完整逐轮。

覆盖：
① save 落 archived / 不带 archived 时**保留**已存归档（常规 save 绝不抹掉归档）；
② full_history = archived + turns（时间序）；history() 仍只返回工作窗口（LLM 口径不变）；
③ list_sessions 的轮数与标题按完整历史算（标题取归档里的首问，不再跳到中途）；
④ snapshot 捕获 archived；restore_snapshot 连 archived 一起回退，且结果带出 archived；
⑤ fork at_seq 用快照那一刻的 archived，不继承原会话的完整归档；
⑥ 压缩算术：maybe_compact 返回的 kept 恒是输入 turns 的后缀，
   dropped = turns[:len(turns)-len(kept)]，archived(dropped) + kept 完整还原原始历史。

全部离线、隔离：把 sessions.SESSIONS_DIR 指到临时目录。
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
        self.tmp = tempfile.mkdtemp(prefix="archive-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp
        self.addCleanup(setattr, sessions, "SESSIONS_DIR", self._old)
        self.sid = "sess-archive"

    def _turn(self, n):
        return {"user": "问题%d" % n, "assistant": "回答%d" % n}


class SaveArchiveTests(Base):

    def test_save_persists_archived_and_reads_back(self):
        sessions.save(self.sid, [self._turn(3), self._turn(4)], "摘要",
                      archived=[self._turn(1), self._turn(2)])
        self.assertEqual([t["user"] for t in sessions.archived_turns(self.sid)],
                         ["问题1", "问题2"])

    def test_plain_save_does_not_wipe_stored_archived(self):
        """最关键的不变量：不带 archived 的常规 save 绝不能抹掉已归档的完整历史。"""
        sessions.save(self.sid, [self._turn(1)], "",
                      archived=[self._turn(0)])
        # 之后一轮普通问答的 save（不关心归档）
        sessions.save(self.sid, [self._turn(1), self._turn(2)], "")
        self.assertEqual([t["user"] for t in sessions.archived_turns(self.sid)],
                         ["问题0"], "常规 save 把归档清空了——又丢历史")

    def test_save_can_still_update_archived_explicitly(self):
        sessions.save(self.sid, [self._turn(2)], "", archived=[self._turn(1)])
        sessions.save(self.sid, [self._turn(3)], "",
                      archived=[self._turn(1), self._turn(2)])
        self.assertEqual([t["user"] for t in sessions.archived_turns(self.sid)],
                         ["问题1", "问题2"])


class FullHistoryTests(Base):

    def test_full_history_is_archived_plus_turns_in_order(self):
        sessions.save(self.sid, [self._turn(3), self._turn(4)], "摘要",
                      archived=[self._turn(1), self._turn(2)])
        full = sessions.full_history(self.sid)
        self.assertEqual([t["user"] for t in full],
                         ["问题1", "问题2", "问题3", "问题4"])

    def test_history_llm_view_is_only_working_window(self):
        """history() 是给 LLM 回放用的工作窗口，必须**不含**归档（口径与改动前一致）。"""
        sessions.save(self.sid, [self._turn(3), self._turn(4)], "摘要",
                      archived=[self._turn(1), self._turn(2)])
        self.assertEqual([t["user"] for t in sessions.history(self.sid)],
                         ["问题3", "问题4"])

    def test_legacy_session_without_archived_field_is_fine(self):
        sessions.save(self.sid, [self._turn(1)], "")
        self.assertEqual(sessions.archived_turns(self.sid), [])
        self.assertEqual([t["user"] for t in sessions.full_history(self.sid)],
                         ["问题1"])


class ListSessionsTests(Base):

    def test_count_and_title_use_full_transcript(self):
        """压缩后列表不能「轮数变少、标题跳到中途那一问」。"""
        sessions.save(self.sid, [self._turn(5), self._turn(6)], "摘要",
                      archived=[self._turn(1), self._turn(2), self._turn(3), self._turn(4)])
        rows = {r["session_id"]: r for r in sessions.list_sessions(limit=100)}
        row = rows[self.sid]
        self.assertEqual(row["turns"], 6, "列表轮数必须按完整历史算")
        self.assertEqual(row["title"], "问题1", "标题必须是首问，不能是被归档前的中途轮")


class SnapshotArchiveTests(Base):

    def test_snapshot_captures_and_restore_returns_archived(self):
        sessions.save(self.sid, [self._turn(3)], "", archived=[self._turn(1), self._turn(2)])
        sessions.snapshot(self.sid, [self._turn(3)], label="第三轮")
        # 继续聊并再归档一轮
        sessions.save(self.sid, [self._turn(4)], "",
                      archived=[self._turn(1), self._turn(2), self._turn(3)])
        got = sessions.restore_snapshot(self.sid, 1)
        self.assertTrue(got["ok"])
        self.assertEqual([t["user"] for t in got["archived"]], ["问题1", "问题2"])
        # 落盘也一致：full_history 回到那一刻
        self.assertEqual([t["user"] for t in sessions.full_history(self.sid)],
                         ["问题1", "问题2", "问题3"])

    def test_fork_at_snapshot_uses_snapshot_archived_not_current(self):
        sessions.save(self.sid, [self._turn(3)], "", archived=[self._turn(1), self._turn(2)])
        sessions.snapshot(self.sid, [self._turn(3)], label="第三轮")
        sessions.save(self.sid, [self._turn(9)], "",
                      archived=[self._turn(1), self._turn(2), self._turn(3), self._turn(8)])
        sessions.fork_session(self.sid, "sess-forked", at_seq=1)
        self.assertEqual([t["user"] for t in sessions.full_history("sess-forked")],
                         ["问题1", "问题2", "问题3"],
                         "按快照分叉却带上了原会话的完整归档——历史错位")


class CompactionArithmeticTests(Base):
    """钉死 agent 归档逻辑依赖的算术前提：kept 是 turns 的后缀，
    dropped = turns[:len(turns)-len(kept)]，archived+kept 完整还原。"""

    def test_kept_is_suffix_and_reconstructs_full_history(self):
        turns = [{"user": "问题%d-需要压缩进摘要" % i, "assistant": "回答%d" % i}
                 for i in range(1, 9)]
        kept, _summary = sessions.maybe_compact(
            self.sid, turns, llm=None, trigger_tokens=1, keep_tokens=1)
        self.assertLess(len(kept), len(turns), "本用例必须真的触发压缩")
        dropped = turns[:len(turns) - len(kept)]
        # kept 是后缀
        self.assertEqual(kept, turns[len(dropped):])
        # 归档 + 工作窗口 == 完整原始历史，逐字不丢
        self.assertEqual(dropped + kept, turns)


if __name__ == "__main__":
    unittest.main()
