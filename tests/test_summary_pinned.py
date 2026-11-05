# -*- coding: utf-8 -*-
"""pinned（固定事实）+ rolling（近期进展）的锁定测试。

背景：旧 `_merge_summary` 每轮把「整段旧摘要 + 新增摘要」重新送 LLM 压缩，
长期事实（项目代号、API 签名约束）会在一轮轮重压中漂移甚至消失。方案 A 把
摘要拆成两节：

- pinned：首次写入后**永不进 LLM**，只接收新的硬约束；满了标注「前缀已满」，
  并且**只增不减**（2026-11-06 起移除“顶掉最旧非约束条目”的逃生口）；
- rolling：只追加新弹出的轮次，超预算时只淘汰最旧段，不碰 pinned。

新的两条不变量（替代旧的「pinned 整块逐字节不变」）：
1. 旧 pinned 的全部内容必须逐字节成为新 pinned 的前缀，不许截断/重排/插省略标记；
2. 新增只允许追加在旧前缀之后（长度不减），且条目数单调不减，除非 actor=user 走 delete_pinned。

全部离线，不调用真实模型。
"""
import json
import os
import shutil
import tempfile
import unittest

import sessions

LIMIT = 600
FACTS = "项目代号 ORCHID-2718；Python 3.9 兼容；三个公开 API 签名不得改动。"


class _CountingLLM:
    """只压 rolling：记录每一次送给模型的提示词，供「pinned 从未进 LLM」断言。"""

    def __init__(self):
        self.prompts = []

    def chat(self, messages, **kwargs):
        prompt = str((messages or [{}])[0].get("content") or "")
        self.prompts.append(prompt)
        return "滚动摘要：第 %d 轮" % len(self.prompts)


def _legacy_prev():
    return FACTS + "\n\n" + "\n\n".join(
        "早期过程 %d：%s" % (i, "细节" * 20) for i in range(20))


def _new_round(n):
    return "\n\n".join("第 %d 轮进展 %d：%s" % (n, i, "过程" * 30)
                       for i in range(3))


class PinnedStabilityTests(unittest.TestCase):

    def test_facts_survive_ten_compactions(self):
        prev = _legacy_prev()
        llm = _CountingLLM()
        for n in range(10):
            prev = sessions._merge_summary(prev, _new_round(n), llm, LIMIT)
        self.assertIn("ORCHID-2718", prev)
        self.assertIn("Python 3.9 兼容", prev)
        self.assertIn("三个公开 API 签名不得改动", prev)
        self.assertIn("【固定事实", prev)

    def test_pinned_never_enters_llm(self):
        prev = _legacy_prev()
        llm = _CountingLLM()
        for n in range(10):
            prev = sessions._merge_summary(prev, _new_round(n), llm, LIMIT)
        self.assertTrue(llm.prompts, "本用例必须真的触发 rolling 压缩（否则断言无意义）")
        for prompt in llm.prompts:
            self.assertNotIn("ORCHID-2718", prompt)
            self.assertNotIn("Python 3.9 兼容", prompt)
            self.assertNotIn("三个公开 API 签名不得改动", prompt)
            self.assertNotIn("【固定事实", prompt)

    def test_rolling_prompt_tolerates_braces(self):
        """rolling 里可能有 JSON/模板花括号，拼接提示词不能被 .format 解析。"""
        prev = _legacy_prev()
        llm = _CountingLLM()
        out = sessions._merge_summary(prev, 'JSON {"a": 1} 与 {placeholder}', llm, LIMIT)
        self.assertIn("【固定事实", out)
        self.assertTrue(llm.prompts)

    def test_rolling_overflow_evicts_oldest_only(self):
        pinned = "PINNED-FACT ORCHID-2718"
        rolling = "\n\n".join("ROLL-%02d %s" % (i, "内容" * 20) for i in range(12))
        prev = sessions._render_summary(pinned, rolling, "")
        # 无标识的普通过程（结构性改造 6 后不会再被当成硬事实吸进 pinned）。
        out = sessions._merge_summary(prev, "新增一条无标识的普通过程", None, 400)
        self.assertEqual(sessions._parse_summary(out)["pinned"], pinned)
        self.assertNotIn("ROLL-00", out)
        self.assertIn("ROLL-11", out)
        self.assertIn("新增一条无标识的普通过程", out)
        self.assertIn("（最旧进展", out)

    def test_pinned_full_keeps_old_entries_and_new_fact_stays_in_rolling(self):
        """新不变量：配额满时不淘汰旧条目，新硬约束留在 rolling。"""
        filler = "过程细节：" + "字" * 140          # 非约束条目
        const = "公开 API 签名不得改动。"
        pinned = filler + "\n\n" + const
        prev = sessions._render_summary(pinned, "", "")
        out = sessions._merge_summary(
            prev, "新增硬约束：不得改动 ORCHID-2718 的对外签名。", None, 300)
        parsed = sessions._parse_summary(out)
        # 旧条目一条都不许少（包括非约束的 filler）。
        self.assertIn(filler, parsed["pinned"])
        self.assertIn(const, parsed["pinned"])
        # 新硬约束装不下 -> 留在 rolling，不进 pinned。
        self.assertNotIn("不得改动 ORCHID-2718 的对外签名", parsed["pinned"])
        self.assertIn("不得改动 ORCHID-2718 的对外签名", out)
        self.assertIn("（固定事实前缀已满）", out)
        self.assertLessEqual(len(out), 300)

    def test_pinned_full_without_evictable_entry_marks_full_and_keeps_text(self):
        pinned = ("必须保持 Python 3.9 兼容，三个公开 API 签名不得改动。"
                  + "，约束细节" * 12)
        prev = sessions._render_summary(pinned, "", "")
        out = sessions._merge_summary(
            prev, "禁止修改 migrations/ 目录下任何文件。", None, 300)
        parsed = sessions._parse_summary(out)
        self.assertNotIn("migrations/", parsed["pinned"])
        self.assertIn("（固定事实前缀已满）", out)
        # 新硬约束没有被吞掉，仍留在摘要（rolling）里。
        self.assertIn("migrations/", out)
        self.assertLessEqual(len(out), 300)


class MigrationCompatTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_pin_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, sid, summary):
        path = os.path.join(self.tmp, sessions._slug(sid) + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"session_id": sid, "summary": summary, "turns": [],
                       "updated_at": "2026-11-05T00:00:00"}, handle, ensure_ascii=False)
        return path

    def test_migration_is_verbatim_idempotent_and_unmarked_readable(self):
        sid = "pin-mig"
        legacy = _legacy_prev() + "\n\n" + "\n\n".join(
            "旧轮 %d：%s" % (i, "细节" * 30) for i in range(20))
        path = self._write(sid, legacy)
        self.assertFalse(sessions._parse_summary(legacy)["structured"])

        rows = sessions.migrate_summaries(400)
        row = [r for r in rows if r["path"] == path]
        self.assertEqual(len(row), 1)
        self.assertTrue(os.path.exists(row[0]["backup"]))
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertNotIn("summary", raw, "新格式不得再落一整段 summary")
        migrated = sessions._summary_state_from_raw(raw)["summary"]
        parsed = sessions._parse_summary(migrated)
        self.assertTrue(parsed["structured"])
        self.assertTrue(legacy.startswith(parsed["pinned"]), "pinned 必须逐字来自原文")
        self.assertIn("pinned_from_legacy_head", migrated)
        self.assertLessEqual(len(migrated), 400)

        with open(path, encoding="utf-8") as handle:
            before = json.load(handle)
        again = sessions.migrate_summaries(400)
        self.assertEqual([r for r in again if r["path"] == path], [])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), before)

    def test_within_limit_legacy_is_zero_change(self):
        sid = "pin-small"
        legacy = "小型旧摘要：ORCHID-2718。"
        path = self._write(sid, legacy)
        rows = [r for r in sessions.migrate_summaries(400) if r["path"] == path]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "split")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertNotIn("summary", raw)
        self.assertEqual(raw["pinned"], [])
        self.assertEqual(raw["rolling"], legacy)
        self.assertEqual(sessions._summary_state_from_raw(raw)["summary"], legacy)

    def test_tombstone_backfill_once_and_empty_pinned_untouched(self):
        """需求 6：老会话缺 pinned_tombstones 只补一次；pinned=[] 不升格。"""
        path = os.path.join(self.tmp, sessions._slug("pin-tomb") + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"session_id": "pin-tomb",
                       "pinned": [sessions._pinned_entry("项目代号 ORCHID-2718。",
                                                         actor="migration")],
                       "rolling": "近期进展。", "summary_structured": True,
                       "turns": [], "updated_at": "2026-11-05T00:00:00"},
                      handle, ensure_ascii=False)
        rows = [r for r in sessions.migrate_summaries(400) if r["path"] == path]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "tombstone_backfill")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["pinned_tombstones"], [])
        with open(path, encoding="utf-8") as handle:
            before = json.load(handle)
        again = [r for r in sessions.migrate_summaries(400) if r["path"] == path]
        self.assertEqual(again, [])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), before)

        empty = os.path.join(self.tmp, sessions._slug("pin-empty") + ".json")
        with open(empty, "w", encoding="utf-8") as handle:
            json.dump({"session_id": "pin-empty", "pinned": [],
                       "rolling": "只有近期进展。", "turns": [],
                       "updated_at": "2026-11-05T00:00:00"},
                      handle, ensure_ascii=False)
        with open(empty, encoding="utf-8") as handle:
            original = handle.read()
        self.assertEqual([r for r in sessions.migrate_summaries(400)
                          if r["path"] == empty], [])
        with open(empty, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original,
                             "pinned=[] 的老会话不得升格或改写")

    def test_unmarked_summary_still_readable_and_merges(self):
        parsed = sessions._parse_summary("无标记旧摘要：ORCHID-2718。")
        self.assertFalse(parsed["structured"])
        out = sessions._merge_summary("无标记旧摘要：ORCHID-2718。", "新一条要点。", None, 1000)
        self.assertIn("ORCHID-2718", out)
        self.assertIn("新一条要点。", out)


class WritebackInjectionTests(unittest.TestCase):

    def test_writeback_and_injection_are_byte_identical(self):
        prev = _legacy_prev()
        merged = sessions._merge_summary(prev, _new_round(1), None, LIMIT)
        self.assertLessEqual(len(merged), LIMIT)
        self.assertEqual(sessions.inject_summary_text(merged, LIMIT), merged)
        # 结构性改造 4：cap 系列只能收到 rolling，含 pinned 的整段必须报错。
        with self.assertRaises(ValueError):
            sessions._cap_summary(merged, LIMIT)
        with self.assertRaises(ValueError):
            sessions.cap_summary_head_tail(merged, LIMIT)
        with self.assertRaises(ValueError):
            sessions.cap_summary_tail(merged, LIMIT)

    def test_small_limit_does_not_split_markers(self):
        pinned = "固定事实：" + "\n\n".join("事实 %d %s" % (i, "内容" * 20)
                                          for i in range(3))
        rolling = "\n\n".join("进展 %d %s" % (i, "过程" * 30) for i in range(6))
        text = sessions._render_summary(pinned, rolling, "pinned_from_legacy_head")
        out = sessions.inject_summary_text(text, 256)
        self.assertLessEqual(len(out), 256)
        self.assertIn("【固定事实｜pinned_from_legacy_head】", out)
        self.assertIn("【近期进展】", out)
        self.assertEqual(out.count("【固定事实"), 1)
        self.assertEqual(out.count("【近期进展"), 1)
        self.assertIn(pinned, out, "pinned 必须逐字保留，不能被裁剪")
        self.assertNotIn("（中间", out.split("【近期进展】")[0])


class DerivedLimitTests(unittest.TestCase):

    def test_two_windows_get_two_different_limits(self):
        small = sessions.summary_char_budget(11000)
        large = sessions.summary_char_budget(702034)
        self.assertEqual(small, sessions._SUMMARY_MAX_CHARS)
        self.assertGreater(large, small)
        self.assertEqual(sessions._resolve_summary_limit(None, 11000), small)
        self.assertEqual(sessions._resolve_summary_limit(None, 702034), large)
        # 不给预算 = 默认口径，默认行为不变。
        self.assertEqual(sessions._resolve_summary_limit(None, None), small)


class PinnedGovernanceTests(unittest.TestCase):
    """用例 20/21/22 + 需求 5/6 的独立锁定。

    这些用例都走真实落盘路径（append_pinned / save / delete_pinned），
    不做纯函数假设，避免“测试通过但线上路径绕过守卫”。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_pin_gov_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp
        self.sid = "pin-gov"

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        entries = [
            {"text": "项目代号：ＯＲＣＨＩＤ-２７１８！", "source": "user:1",
             "added_at": "2026-01-01T00:00:00"},
            {"text": "Python 3.9 兼容，不得升级。", "source": "user:2",
             "added_at": "2026-01-02T00:00:00"},
            {"text": "不得改动三个公开 API 签名。", "source": "user:3",
             "added_at": "2026-01-03T00:00:00"},
        ]
        sessions.append_pinned(entries, actor="model", origin="seed", session_id=self.sid)
        return sessions.load(self.sid)["pinned"]

    @staticmethod
    def _frozen(entries):
        return [(e["id"], e["text"], e["source"], e["added_at"]) for e in entries]

    def test_20_model_path_cannot_delete_or_shrink_pinned(self):
        before = self._seed()
        frozen = self._frozen(before)

        # 模型路径“删除”必须被拒绝。
        blocked = sessions.delete_pinned(before[0]["id"], actor="model",
                                         session_id=self.sid)
        self.assertFalse(blocked["deleted"])
        self.assertEqual(blocked["reason"], "actor_not_user")

        # 模型路径传入更短列表 -> 整批忽略。
        report = sessions.append_pinned(
            [{"text": before[0]["text"], "id": before[0]["id"]}],
            actor="model", origin="model_short", session_id=self.sid)
        self.assertTrue(report["shrink_rejected"])

        # 旧的 save 路径（文本回写）传更短摘要 -> 条目数不减、字段不变。
        sessions.save(self.sid, [],
                      sessions._render_summary(before[0]["text"], "", ""))
        after = sessions.load(self.sid)["pinned"]
        self.assertEqual(len(after), len(before))
        self.assertEqual(self._frozen(after), frozen)

        # 直接调用 _store_summary_state 也不能绕过。
        data = sessions.load(self.sid)
        short = sessions._summary_state_from_raw(
            {"summary": sessions._render_summary(before[0]["text"], "", "")})
        sessions._store_summary_state(data, short, actor="model")
        self.assertEqual(len(data["pinned"]), len(before))
        self.assertIn("shrink_rejected",
                      [row["action"] for row in data["pinned_audit"]])

    def test_21_user_delete_then_full_extraction_does_not_reflow(self):
        before = self._seed()
        target = before[0]
        result = sessions.delete_pinned(target["id"], session_id=self.sid)
        self.assertTrue(result["deleted"])
        self.assertNotIn(target["id"],
                         [e["id"] for e in sessions.load(self.sid)["pinned"]])

        # 模拟下一次完整提取：旧事实被原样/轻微改写（全半角、标点）再次产出。
        variant = "项目代号: ORCHID-2718"
        with self.assertLogs("sessions", level="INFO") as captured:
            report = sessions.append_pinned(
                [{"text": e["text"], "source": e["source"]} for e in before[1:]]
                + [{"text": variant, "source": "auto:extract"}],
                actor="model", origin="auto_extract", session_id=self.sid)
        self.assertTrue(report["suppressed"])
        loaded = sessions.load(self.sid)
        self.assertNotIn(target["id"], [e["id"] for e in loaded["pinned"]])
        self.assertNotIn("ORCHID-2718",
                         [e["text"] for e in loaded["pinned"]])
        self.assertTrue(any(row["action"] == "readd_suppressed"
                            and row["id"] == target["id"]
                            for row in loaded["pinned_audit"]))
        self.assertTrue(any("pinned_readd_suppressed" in line
                            for line in captured.output))

    def test_22_ninety_compactions_keep_old_bytes_and_monotonic_count(self):
        limit = 1200
        before = self._seed()
        frozen = {e["id"]: (e["text"], e["source"], e["added_at"])
                  for e in before}
        counts = [len(before)]
        for n in range(90):
            prev = sessions.summary_text(self.sid)
            extra = ""
            if n % 10 == 0:
                extra = ("\n\n第 %d 轮硬约束：必须保持模块 mod%d.py 的接口版本 v1.%d。"
                         % (n, n, n))
            merged = sessions._merge_summary(
                prev, "第 %d 轮进展：%s" % (n, "过程" * 40) + extra,
                None, limit)
            sessions.save(self.sid, [], merged)
            counts.append(len(sessions.load(self.sid)["pinned"]))

        after = {e["id"]: e for e in sessions.load(self.sid)["pinned"]}
        for entry_id, fields in frozen.items():
            self.assertIn(entry_id, after, "旧条目不得因压缩消失")
            self.assertEqual((after[entry_id]["text"], after[entry_id]["source"],
                              after[entry_id]["added_at"]), fields,
                             "旧条目的 text/source/added_at 必须逐字节不变")
        self.assertEqual(len(counts), 91)
        for previous, current in zip(counts, counts[1:]):
            self.assertGreaterEqual(current, previous,
                                    "非用户路径的 pinned 条目数只能增不能减")

    def test_must_keep_never_caps_or_annotates_old_pinned_prefix(self):
        """任务二：`_append_must_keep` 的裁剪路径不得碰旧 pinned 前缀。

        must-keep 只能作为新条目追加；任何裁剪函数都不应收到 pinned 正文，
        更不允在旧前缀里插入「（中间 N 字已省略）」这类标记。
        """
        pinned = ("必须保持 Python 3.9 兼容，三个公开 API 签名不得改动。\n\n"
                  "禁止在 migrations/ 目录下新增破坏性变更。\n\n"
                  "配置键前缀固定为 dm_，环境变量不得覆盖 data/ 下的运行时文件。")
        sessions.save(self.sid, [],
                      sessions._render_summary(pinned, "旧进展：完成脚手架。", ""))
        before = sessions._parse_summary(sessions.summary_text(self.sid))["pinned"]
        self.assertTrue(before)

        seen = []
        original = sessions.cap_summary_head_tail

        def recorder(text, limit=sessions._SUMMARY_MAX_CHARS):
            seen.append(str(text or ""))
            return original(text, limit)

        sessions.cap_summary_head_tail = recorder
        try:
            out = sessions._append_must_keep(
                sessions.summary_text(self.sid),
                [{"user": "索引构建未完成，下一轮必须继续。",
                  "resume_context": "索引构建未完成"}],
                limit=600)
        finally:
            sessions.cap_summary_head_tail = original

        parsed = sessions._parse_summary(out)
        self.assertTrue(parsed["pinned"].startswith(before),
                        "旧 pinned 必须逐字节成为新 pinned 的前缀")
        self.assertGreaterEqual(len(parsed["pinned"]), len(before),
                                "新 pinned 不得比旧 pinned 短")
        self.assertNotIn("（中间", parsed["pinned"][:len(before)],
                         "旧前缀里不得出现裁剪省略标记")
        for text in seen:
            self.assertNotIn("【固定事实", text,
                             "pinned 正文不得进入任何裁剪函数")
            self.assertNotIn("【近期进展", text,
                             "结构化摘要整段不得进入任何裁剪函数")

    def test_req5_pinned_entries_carry_id_source_time_actor(self):
        entries = self._seed()
        for entry in entries:
            self.assertTrue(entry["id"].startswith("pin-"))
            self.assertTrue(entry["source"])
            self.assertTrue(entry["added_at"])
            self.assertEqual(entry["actor"], "model")

    def test_req6_negative_examples_never_enter_pinned(self):
        prev = sessions._render_summary("必须保持 Python 3.9 兼容。", "", "")
        negative = "旧代码保持原样，仅用于对比，不是长期规则。"
        out = sessions._merge_summary(prev, negative, None, 1200)
        parsed = sessions._parse_summary(out)
        self.assertNotIn("旧代码保持原样", parsed["pinned"])
        self.assertIn("旧代码保持原样", out)


if __name__ == "__main__":
    unittest.main()
