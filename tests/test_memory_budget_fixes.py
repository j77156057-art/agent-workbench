# -*- coding: utf-8 -*-
"""本轮四个修复的锁定测试。

1. 用户画像：面板分母与 profile.py 的真实上限同源；
2. 任务书：超 80 条时淘汰最旧的已完成项，绝不砍掉新任务，未淘汰项顺序不变；
3. 历史摘要：注入侧封顶保头尾、中间标注省略；存量迁移先备份、可重复运行。
"""
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

import config
import sessions
from agent_runtime.memory import context_panel, profile, taskbook


class ProfileBudgetTests(unittest.TestCase):
    def test_panel_denominator_is_single_source(self):
        """500 是显示 bug：面板分母必须引用 profile 的真实上限。"""
        self.assertEqual(profile.PROFILE_INJECT_MAX_CHARS, 1800)
        self.assertEqual(context_panel.BUDGETS["profile"],
                         profile.PROFILE_INJECT_MAX_CHARS)

    def test_context_never_exceeds_real_limit(self):
        huge = {"display_name": "测试", "notes": "超长笔记" * 1000}
        with patch.object(profile, "load", return_value=huge):
            text = profile.context()
        self.assertLessEqual(len(text), profile.PROFILE_INJECT_MAX_CHARS)


class TaskbookEvictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_tb_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def test_keeps_new_tasks_and_drops_oldest_done(self):
        items = []
        for i in range(5):
            items.append({
                "key": "done-%d" % i, "text": "旧完成 %d" % i, "state": "done",
                "done_at": "2026-01-0%dT00:00:00" % (i + 1),
                "updated": "2026-01-0%dT00:00:00" % (i + 1), "created": "",
            })
        for i in range(80):
            items.append({
                "key": "new-%d" % i, "text": "新任务 %d" % i, "state": "todo",
                "done_at": "", "updated": "2026-11-05T00:00:00", "created": "",
            })
        kept = taskbook._evict(items)
        self.assertEqual(len(kept), taskbook.MAX_ITEMS)
        kept_keys = [row["key"] for row in kept]
        self.assertEqual(kept_keys, ["new-%d" % i for i in range(80)])
        # 未淘汰项保持原列表顺序（UI 的待办顺序语义不能乱）。
        self.assertEqual(kept_keys, [row["key"] for row in items[5:]])


class SummaryCapTests(unittest.TestCase):
    def _three_part_text(self):
        fact = "已定决策：最终方案是 ORCHID-2718，API 签名不可改。"
        process = "\n\n".join("过程 %d：%s" % (i, "细节" * 30) for i in range(1, 40))
        latest = "最新进展：刚跑完回归测试，结果全部通过。"
        return fact + "\n\n" + process + "\n\n" + latest

    def test_summary_cap_is_derived_from_token_budget(self):
        """上限不是写死的 1200：保留预算 × 摘要占比，且只有这一处来源。"""
        expected = max(256, int(config.PROMPT_TOKEN_BUDGET
                                * config.COMPACT_KEEP_RATIO
                                * sessions._SUMMARY_SHARE))
        self.assertEqual(sessions._SUMMARY_MAX_CHARS, expected)
        self.assertEqual(sessions.SUMMARY_MAX_CHARS, expected)
        self.assertGreaterEqual(sessions._SUMMARY_SHARE, 0.5)
        self.assertLessEqual(sessions._SUMMARY_SHARE, 0.6)

    def test_head_and_tail_kept_middle_marked(self):
        text = self._three_part_text()
        out = sessions.cap_summary_head_tail(text)
        self.assertLessEqual(len(out), sessions._SUMMARY_MAX_CHARS)
        self.assertIn("ORCHID-2718", out)          # 头部事实在
        self.assertIn("最新进展：刚跑完回归测试", out)  # 尾部最新在
        self.assertIn("（中间", out)
        self.assertIn("字已省略）", out)
        # 标记必须在头尾之间：不是最前，也不在尾部之后。
        mid = out.index("（中间")
        self.assertGreater(mid, 0)
        self.assertLess(out.index("最新进展"), len(out) - 1)
        self.assertGreater(mid, out.index("ORCHID-2718"))
        self.assertLess(mid, out.index("最新进展"))

    def test_truncation_is_idempotent(self):
        out = sessions.cap_summary_head_tail(self._three_part_text())
        self.assertEqual(sessions.cap_summary_head_tail(out), out)

    def test_no_cut_inside_marker_and_small_limit_falls_back_to_tail(self):
        out = sessions.cap_summary_head_tail(self._three_part_text())
        self.assertEqual(out.count("（中间"), 1)
        self.assertEqual(out.count("字已省略）"), 1)
        small = sessions.cap_summary_head_tail(self._three_part_text(), 10)
        self.assertEqual(len(small), 10)
        self.assertNotIn("（中间", small)  # 装不下标记时优先保尾

    def test_normalize_blocks_is_idempotent(self):
        raw = "- 事实 A\n- 事实 B\n- 事实 C"
        once = sessions._normalize_summary_blocks(raw)
        self.assertEqual(once, "- 事实 A\n\n- 事实 B\n\n- 事实 C")
        self.assertEqual(sessions._normalize_summary_blocks(once), once)
        self.assertEqual(once.count("\n\n"), 2)

    def test_writeback_and_injection_share_one_function(self):
        """3b/3c：写回（_cap_summary）与注入（cap_summary_head_tail）必须逐字相同。"""
        cases = [
            "短摘要",
            "\n\n".join("轮 %d：%s" % (i, "细节" * 40) for i in range(30)),
            "- 要点一\n- 要点二\n" + "单块超长：" + "字" * 4000,
        ]
        for text in cases:
            for limit in (600, 1200, 2000):
                self.assertEqual(sessions._cap_summary(text, limit),
                                 sessions.cap_summary_head_tail(text, limit))
                self.assertEqual(sessions.cap_summary_tail(text, limit),
                                 sessions.cap_summary_head_tail(text, limit))
        self.assertEqual(sessions.SUMMARY_MAX_CHARS, sessions._SUMMARY_MAX_CHARS)

    def test_merge_fallback_uses_same_cap(self):
        """3b：LLM 合并失败时，确定性回退也必须走 cap_summary_head_tail，而不是裸 "…"。"""
        prev = "\n\n".join("旧段 %d：%s" % (i, "细节" * 40) for i in range(40))
        new_sum = "新段：NEW-MARKER 是最近结论。"
        merged = sessions._merge_summary(prev, new_sum, llm=None)
        self.assertLessEqual(len(merged), sessions._SUMMARY_MAX_CHARS)
        self.assertIn("NEW-MARKER", merged)
        self.assertIn("省略", merged)
        self.assertFalse(merged.rstrip().endswith("…"))

    def test_must_keep_never_squeezed_out(self):
        """3b/改造5：must-keep 结构化进 pinned 前缀，不会被保尾裁剪挤掉。"""
        long_summary = "\n\n".join("轮 %d：%s" % (i, "旧内容" * 30) for i in range(40))
        turns = [{"user": "不要重试，只读不改。样本 X-42 必须保留",
                  "resume_context": "待续"}]
        out = sessions._append_must_keep(long_summary, turns)
        self.assertLessEqual(len(out), sessions._SUMMARY_MAX_CHARS)
        self.assertIn("【未完成现场 must-keep】", out)
        self.assertIn("样本 X-42 必须保留", out)
        # 结构化写入后 must-keep 固定在 pinned 段（早于近期进展），而不是旧实现的尾部。
        self.assertLess(out.index("must-keep"), out.index("【近期进展】"))

    def test_panel_summary_denominator_is_single_source(self):
        """3a：面板历史摘要的分母必须跟 sessions 的注入封顶同源（按当前模型口径）。"""
        from agent_runtime.memory import injection
        expected = sessions.summary_char_budget(
            injection._current_prompt_tokens() or None)
        self.assertEqual(context_panel._summary_inject_cap(), expected)
        layer = context_panel._layer("summary", "历史摘要", "摘要正文",
                                     editable=False, source="系统", kind="readonly")
        self.assertEqual(layer["budget"], expected)


class SummaryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_sess_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir

    def _backups(self, slug):
        return [name for name in sorted(os.listdir(self.tmp))
                if name.startswith(slug + ".json.bak-")]

    def test_migrate_truncates_tail_and_is_idempotent(self):
        sid = "mig-" + uuid.uuid4().hex[:8]
        slug = sessions._slug(sid)
        path = os.path.join(self.tmp, slug + ".json")
        summary = "\n\n".join("旧轮 %d：%s" % (i, "细节" * 30) for i in range(60))
        summary += "\n\n最新轮：TAIL-MARKER 必须保留。"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"session_id": sid, "summary": summary, "turns": [],
                       "updated_at": "2026-11-05T00:00:00"}, handle, ensure_ascii=False)

        first = [row for row in sessions.migrate_summaries() if row["path"] == path]
        self.assertEqual(len(first), 1)
        self.assertGreater(first[0]["before"], sessions._SUMMARY_MAX_CHARS)
        self.assertLessEqual(first[0]["after"], sessions._SUMMARY_MAX_CHARS)
        self.assertTrue(os.path.exists(first[0]["backup"]))

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        # 结构性改造 1：迁移后只写 pinned/rolling，不再保留整段 summary。
        self.assertNotIn("summary", data)
        self.assertIsInstance(data.get("pinned"), list)
        rendered = sessions.summary_text(sid)
        self.assertLessEqual(len(rendered), sessions._SUMMARY_MAX_CHARS)
        self.assertIn("TAIL-MARKER 必须保留。", data.get("rolling") or "")
        self.assertIn("省略", rendered)

        backups_before = self._backups(slug)
        second = [row for row in sessions.migrate_summaries() if row["path"] == path]
        self.assertEqual(second, [])
        again = sessions.summary_text(sid)
        self.assertEqual(again, rendered)
        self.assertEqual(self._backups(slug), backups_before)


if __name__ == "__main__":
    unittest.main()
