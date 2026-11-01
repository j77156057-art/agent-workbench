# -*- coding: utf-8 -*-
"""磁盘回收端点：用户设的间隔/保留天数/白名单，加上「立即回收」的确认闸。

`/api/disk-retention*` 是面板唯一的入口，所以这里钉四条：

1. **边界单源** —— 界面 `max` 必须等于后端 `normalize_settings` 用的那批常量，
   否则用户填 99999 天、后端悄悄夹成 3650，界面还显示 99999（agent-capacity 踩过）。
2. **默认不自动清** —— `interval_days=0` 时 `schedule.due` 恒 False；动手的权力只有
   用户设成正数或亲自点按钮才交出来（宪法 C-006）。
3. **白名单只进不出** —— 传空表也带内置项，`release/`（发布物）不许被纳入自动删除。
4. **真删除要 confirm** —— 与 `/api/memory/maintain` 同一道闸，缺了就 400。

判定与执行器本身的守卫在 `tests/test_disk_retention.py`，这里只管接线。
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import api
from agent_runtime import disk_retention as dr

DAY = 86400.0
#: 四个试做区 + 全局保留最近 3 个 → 只有最老的 `wf-a` 落到可回收那一档。
_AGES = {"wf-a": 100, "wf-b": 90, "wf-c": 80, "wf-d": 70}


def _call(coro):
    return asyncio.run(coro)


def _write_last_run(state_root: Path, **fields):
    """直接落一条历史记录：调度是全时间函数，用合成时间比真等 30 天唯一可靠。"""
    row = {"ts": 0.0, "trigger": "scheduled", "applied": False,
           "freed_bytes": 0, "freed_items": 0, "failed_items": 0,
           "skipped_items": 0, "failures": [], "settings": {},
           "plan_summary": {}, "first_seen_ts": 0.0}
    row.update(fields)
    target = Path(dr.last_run_path(str(state_root)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(row), encoding="utf-8")


class DiskRetentionApiTests(unittest.TestCase):
    def setUp(self):
        self.state_root = Path(tempfile.mkdtemp(prefix="dr-state-"))
        self.project_root = Path(tempfile.mkdtemp(prefix="dr-project-"))
        docmind = self.state_root / ".docmind"
        (docmind / "workflows").mkdir(parents=True)
        now = time.time()
        for name, age in _AGES.items():
            stage = docmind / dr.STAGES / name / "workspace"
            stage.mkdir(parents=True)
            (stage / "note.txt").write_text("x" * 2048, encoding="utf-8")
            stamp = now - age * DAY
            os.utime(stage.parent, (stamp, stamp))
            (docmind / "workflows" / ("%s.json" % name)).write_text(
                json.dumps({"id": name, "status": "completed"}), encoding="utf-8")
        self.docmind = docmind
        self._patches = [
            patch.object(api, "STATE_ROOT", str(self.state_root)),
            patch.object(api, "get_runtime", lambda key: str(self.project_root)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for root in (self.state_root, self.project_root):
            shutil.rmtree(root, ignore_errors=True)

    # ---- GET：设置 + 边界 + 到点判定 ----

    def test_get_returns_default_settings_and_backend_ranges(self):
        payload = _call(api.get_disk_retention())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["settings"], dr.normalize_settings(None))
        self.assertEqual(payload["settings"]["interval_days"], 30,
                         "用户要的是「默认按时间回收」；默认 0 等于装好即静默、永不自动清")
        self.assertFalse(payload["schedule"]["due"],
                         "从没有过记录就判「到点」= 升级后第一次体检当场删掉几 GB")
        self.assertIsNone(payload["schedule"]["first_seen_ts"])
        self.assertEqual(payload["builtin_whitelist"], list(dr.BUILTIN_WHITELIST))

    def test_ranges_match_the_backend_clamp_constants(self):
        # 边界的唯一来源是 disk_retention 里那批常量；前端读 payload 设 input 的 max。
        ranges = _call(api.get_disk_retention())["ranges"]
        self.assertEqual(ranges["interval_days_min"], dr.INTERVAL_MIN_DAYS)
        self.assertEqual(ranges["interval_days_max"], dr.INTERVAL_MAX_DAYS)
        self.assertEqual(ranges["idle_days_min"], dr.IDLE_MIN_DAYS)
        self.assertEqual(ranges["idle_days_max"], dr.IDLE_MAX_DAYS)
        self.assertEqual(ranges["whitelist_max_entries"], dr.WHITELIST_MAX_ENTRIES)
        self.assertEqual(ranges["max_items_per_run"], dr.MAX_ITEMS_PER_RUN)

    def test_get_without_state_dir_reports_disabled(self):
        with patch.object(api, "STATE_ROOT",
                          str(self.state_root / "nowhere")):
            payload = _call(api.get_disk_retention())
        self.assertFalse(payload["enabled"])
        # 读不到目录也要给默认设置，否则面板拿 null 去渲染就是白屏。
        self.assertEqual(payload["settings"]["idle_days"], dr.DEFAULT_SETTINGS["idle_days"])

    # ---- POST settings：夹区间 + 只覆盖提交的字段 ----

    def test_settings_are_clamped_to_the_backend_bounds(self):
        resp = _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(interval_days=99999, idle_days=0)))
        self.assertEqual(resp["settings"]["interval_days"], dr.INTERVAL_MAX_DAYS)
        self.assertEqual(resp["settings"]["idle_days"], dr.IDLE_MIN_DAYS,
                         "idle_days=0 等于「终态就删」，一次误设能清掉刚跑完的副本")
        # 回读：夹过的值真的落盘了，UI 显示的就是生效值。
        self.assertEqual(_call(api.get_disk_retention())["settings"]["interval_days"],
                         dr.INTERVAL_MAX_DAYS)

    def test_partial_update_leaves_the_other_fields_alone(self):
        _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(interval_days=5, idle_days=45)))
        _call(api.set_disk_retention_settings(api.DiskRetentionSettingsReq(idle_days=7)))
        settings = _call(api.get_disk_retention())["settings"]
        self.assertEqual(settings["idle_days"], 7)
        self.assertEqual(settings["interval_days"], 5,
                         "只改一格却把间隔写回默认 = 用户设的自动清被悄悄关掉")

    def test_whitelist_cannot_be_emptied(self):
        resp = _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(whitelist=[])))
        for builtin in dr.BUILTIN_WHITELIST:
            self.assertIn(builtin, resp["settings"]["whitelist"],
                          "内置项被清空 → 下次自动清会把发布物一起带走")
        # 非内置项确实被换掉了（不是「只加不减」的假守卫）
        self.assertNotIn("wf-a", resp["settings"]["whitelist"])
        resp = _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(whitelist=["wf-a", "installer"])))
        self.assertIn("wf-a", resp["settings"]["whitelist"])

    def test_junk_whitelist_entries_are_dropped(self):
        resp = _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(whitelist=["  ", "", "a" * 500])))
        self.assertEqual(resp["settings"]["whitelist"], list(dr.BUILTIN_WHITELIST))

    def test_first_pass_only_records_a_baseline(self):
        """首轮体检只记基线：默认间隔 30 天时，升级完不该当场删东西。"""
        _write_last_run(self.docmind, ts=time.time() - 400 * DAY,
                        first_seen_ts=time.time() - 400 * DAY)
        # 有基线且早过 30 天 → 到点
        self.assertTrue(_call(api.get_disk_retention())["schedule"]["due"])
        # 换成"从没有记录" → 不到点（这一轮只负责写基线）
        Path(dr.last_run_path(str(self.docmind))).unlink()
        first = _call(api.get_disk_retention())["schedule"]
        self.assertFalse(first["due"])
        self.assertIsNone(first["first_seen_ts"])

    def test_interval_counts_from_the_baseline_until_a_real_run(self):
        now = time.time()
        _write_last_run(self.docmind, ts=now - 20 * DAY, first_seen_ts=now - 20 * DAY)
        self.assertFalse(_call(api.get_disk_retention())["schedule"]["due"],
                         "20 天 < 30 天间隔，却被判到点")
        next_due = _call(api.get_disk_retention())["schedule"]["next_due_ts"]
        self.assertAlmostEqual(next_due, now - 20 * DAY + 30 * DAY, delta=5)

    def test_dry_runs_do_not_postpone_the_next_automatic_clean(self):
        now = time.time()
        # 真清发生在 40 天前，今天又干跑了一轮（记录 ts 是今天、applied=False）
        _write_last_run(self.docmind, ts=now, applied=False,
                        first_seen_ts=now - 90 * DAY, last_apply_ts=now - 40 * DAY)
        sched = _call(api.get_disk_retention())["schedule"]
        self.assertTrue(sched["due"], "干跑后丢了「上次真动手」→ 自动清被无限推迟")
        self.assertAlmostEqual(sched["last_apply_ts"], now - 40 * DAY, delta=5)
        # 上次真动手在 20 天前 → 没到点
        _write_last_run(self.docmind, ts=now, applied=False,
                        first_seen_ts=now - 90 * DAY, last_apply_ts=now - 20 * DAY)
        self.assertFalse(_call(api.get_disk_retention())["schedule"]["due"])

    def test_record_run_carries_schedule_anchors_forward(self):
        """直接在记账层钉：干跑既不能抹掉「上次真动手」，也不能抹掉基线。"""
        state = str(self.docmind)
        base = {"plan": {"summary": {}}, "result": {}, "settings": dr.normalize_settings(None)}
        first = dr.record_run(state, **base, trigger="scheduled", applied=False, now=1000.0)
        self.assertEqual(first["first_seen_ts"], 1000.0)
        self.assertIsNone(first["last_apply_ts"])
        second = dr.record_run(state, **base, trigger="scheduled", applied=True, now=2000.0)
        self.assertEqual(second["last_apply_ts"], 2000.0)
        self.assertEqual(second["first_seen_ts"], 1000.0)
        third = dr.record_run(state, **base, trigger="manual", applied=False, now=3000.0)
        self.assertEqual(third["last_apply_ts"], 2000.0, "干跑把上次真动手抹掉了")
        self.assertEqual(third["first_seen_ts"], 1000.0, "干跑把基线抹掉了")

    def test_interval_zero_never_becomes_due(self):
        now = time.time()
        _call(api.set_disk_retention_settings(api.DiskRetentionSettingsReq(interval_days=0)))
        _write_last_run(self.docmind, ts=now - 400 * DAY, applied=True,
                        first_seen_ts=now - 400 * DAY)
        sched = _call(api.get_disk_retention())["schedule"]
        self.assertFalse(sched["due"], "用户明确关了自动清，却被判到点")
        self.assertIsNone(sched["next_due_ts"])

    # ---- POST run：确认闸 + 干跑不动手 + 动手时白名单挡得住 ----

    def test_apply_without_confirm_is_rejected(self):
        with self.assertRaises(api.HTTPException) as ctx:
            _call(api.run_disk_retention(
                api.DiskRetentionRunReq(apply=True, confirm=False)))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("confirm=true", str(ctx.exception.detail))
        self._assert_all_stages_present()

    def test_run_on_missing_state_root_is_conflict(self):
        with patch.object(api, "STATE_ROOT", str(self.state_root / "nowhere")):
            with self.assertRaises(api.HTTPException) as ctx:
                _call(api.run_disk_retention(api.DiskRetentionRunReq()))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_dry_run_lists_the_expired_copy_without_touching_it(self):
        out = _call(api.run_disk_retention(api.DiskRetentionRunReq()))
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["applied"])
        self.assertEqual(os.path.basename(out["result"]["removed"][0]["path"]), "wf-a")
        self._assert_all_stages_present()
        self.assertEqual(out["plan"]["reclaimable_items"], 1,
                         "四个区里只该有最老的那个可回收（keep_newest=3 保另外三个）")

    def test_apply_deletes_only_the_expired_copy(self):
        out = _call(api.run_disk_retention(
            api.DiskRetentionRunReq(apply=True, confirm=True)))
        self.assertTrue(out["applied"])
        self.assertTrue(out["result"]["freed_bytes"] > 0)
        self.assertFalse((self.docmind / dr.STAGES / "wf-a").exists())
        self._assert_stages_present("wf-b", "wf-c", "wf-d")
        self.assertEqual(out["record"]["trigger"], "manual")
        self.assertTrue(out["record"]["applied"])

    def test_apply_respects_a_user_whitelist_entry(self):
        _call(api.set_disk_retention_settings(
            api.DiskRetentionSettingsReq(whitelist=["wf-a"])))
        out = _call(api.run_disk_retention(
            api.DiskRetentionRunReq(apply=True, confirm=True)))
        self.assertTrue((self.docmind / dr.STAGES / "wf-a").exists(),
                        "用户点名不清的副本被删了 —— 白名单就是摆设")
        self._assert_stages_present("wf-b", "wf-c", "wf-d")
        self.assertEqual(out["plan"]["blocked_by_whitelist_items"], 1)
        self.assertEqual(out["result"]["removed"], [], "挡下之后仍然把条目排进了删除批次")
        self.assertEqual(out["result"]["freed_bytes"], 0)
        # 面板要能回答"这条为什么不清"，所以命中项回显给用户。
        self.assertEqual(out["blocked"][0]["name"], "wf-a")
        self.assertEqual(out["blocked"][0]["whitelisted_by"], "wf-a")

    def test_run_refreshes_the_cached_plan_the_panel_reads(self):
        before = _call(api.get_disk_retention())
        self.assertEqual(before["last_run"], {}, "没跑过却带回旧记录")
        _call(api.run_disk_retention(api.DiskRetentionRunReq()))
        after = _call(api.get_disk_retention())
        self.assertEqual(after["last_run"]["trigger"], "manual")
        self.assertFalse(after["last_run"]["applied"])
        self.assertEqual(after["plan"]["reclaimable_items"], 1)

    def _assert_all_stages_present(self):
        self._assert_stages_present(*_AGES)

    def _assert_stages_present(self, *names):
        for name in names:
            self.assertTrue((self.docmind / dr.STAGES / name).exists(),
                            "%s 不该被动过" % name)


if __name__ == "__main__":
    unittest.main()
