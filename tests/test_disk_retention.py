# -*- coding: utf-8 -*-
"""磁盘保留期（`agent_runtime/disk_retention`）的判定与执行守卫。

这个模块存在的理由只有一句：`.docmind/project_stages` 单个目录 725 MB、实测合计 5.02 GB，
`project_checkpoints` 0.80 GB，`dist/` 里的一次性构建副本 4.06 GB —— **没有任何代码会回头清它们**，
容量单调增长。所以测试要钉的不是"能不能删"，而是"什么时候绝不许删"：

1. 未终态工作流的副本是恢复入口，删了就毁掉可续跑性 → 必须 keep。
2. 全局最近的试做区永远保留 → 否则保留期一过连回滚点都没了。
   （注意：一个工作流只有一个试做区目录，所以"每区保留最近一份"在这里等于永不回收 ——
   这条规则本身就是踩出来的。）
3. 认不出状态（没有 `workflows/wf-*.json`）→ 进 `needs_review`，不自动删；
   图状态可能在 sqlite 里，"没状态文件"不等于"已结束"。
4. `dist/release` / `installer` / `*.zip` 是发布物；名字仍被跟踪文件引用的也保留。
5. git 不可用时引用扫描**退化成全部保留**，不许退化成全部可删。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime import disk_retention as dr

DAY = 86400.0
NOW = 1_700_000_000.0

#: 测试统一用 `keep_newest=0`，好让"状态 × 闲置"这两个变量单独可测；
#: keep_newest 自己另有一组守卫。
RULES = {dr.STAGES: {"idle_days": 14, "keep_newest": 0},
         dr.CHECKPOINTS: {"idle_days": 14, "keep_per_workflow": 2},
         dr.DIST: {"idle_days": 30}}


def _entry(name, *, mtime, size=1024, workflow_id=None, is_dir=True, path=None):
    row = {"name": name, "mtime": mtime, "bytes": size, "files": 3, "is_dir": is_dir,
           "path": path or ("/state/" + (workflow_id + "/" if workflow_id else "") + name)}
    if workflow_id:
        row["workflow_id"] = workflow_id
    return row


class StagePlanTests(unittest.TestCase):
    def test_terminal_and_idle_is_reclaimable(self):
        rows = dr.plan_stage_dirs([_entry("wf-old", mtime=NOW - 30 * DAY)],
                                  {"wf-old": "failed"}, NOW, RULES)
        self.assertEqual(rows[0]["decision"], "reclaim", rows[0])

    def test_keep_newest_protects_the_most_recent_dirs_globally(self):
        entries = [_entry("wf-%d" % i, mtime=NOW - (20 + i) * DAY) for i in range(4)]
        statuses = {"wf-0": "failed", "wf-1": "failed", "wf-2": "failed", "wf-3": "failed"}
        rows = dr.plan_stage_dirs(entries, statuses, NOW,
                                  {**RULES, dr.STAGES: {"idle_days": 14, "keep_newest": 2}})
        kept = sorted(r["name"] for r in rows if r["decision"] == "keep")
        self.assertEqual(kept, ["wf-0", "wf-1"], "keep_newest 保的不是最近的两个：%s" % kept)
        self.assertEqual(sorted(r["name"] for r in rows if r["decision"] == "reclaim"),
                         ["wf-2", "wf-3"])

    def test_live_status_is_never_reclaimed(self):
        for status in ("interrupted", "awaiting_choice", "running"):
            rows = dr.plan_stage_dirs([_entry("wf-a", mtime=NOW - 400 * DAY)],
                                      {"wf-a": status}, NOW, RULES)
            self.assertEqual(rows[0]["decision"], "keep",
                             "%s 状态的副本被当成可回收" % status)

    def test_missing_state_goes_to_needs_review_not_reclaim(self):
        rows = dr.plan_stage_dirs([_entry("wf-x", mtime=NOW - 90 * DAY)], {}, NOW, RULES)
        self.assertEqual(rows[0]["decision"], "needs_review",
                         "没有状态文件就被自动删了 —— 认不出不许猜")
        loosened = dr.plan_stage_dirs([_entry("wf-x", mtime=NOW - 90 * DAY)], {}, NOW, RULES,
                                      include_orphans=True)
        self.assertEqual(loosened[0]["decision"], "reclaim",
                         "显式 include_orphans 之后还不放行，参数就是摆设")

    def test_within_retention_window_is_kept(self):
        rows = dr.plan_stage_dirs([_entry("wf-a", mtime=NOW - DAY)],
                                  {"wf-a": "completed"}, NOW, RULES)
        self.assertEqual(rows[0]["decision"], "keep", "没过保留期的副本被删了")

    def test_input_order_does_not_change_decisions(self):
        entries = [_entry("wf-a", mtime=NOW - 40 * DAY, path="/state/a"),
                   _entry("wf-b", mtime=NOW - 20 * DAY, path="/state/b")]
        first = sorted((r["name"], r["decision"]) for r in
                       dr.plan_stage_dirs(entries, {"wf-a": "failed", "wf-b": "failed"},
                                          NOW, RULES))
        second = sorted((r["name"], r["decision"]) for r in
                        dr.plan_stage_dirs(list(reversed(entries)),
                                           {"wf-a": "failed", "wf-b": "failed"}, NOW, RULES))
        self.assertEqual(first, second, "判定依赖了传入顺序，不是纯函数")


class CheckpointPlanTests(unittest.TestCase):
    def test_keeps_last_two_batches_per_workflow(self):
        # 年龄全部落在保留期之外，这样"keep 的理由"只剩 keep_per_workflow 一条
        entries = [_entry("cp-%02d" % i, mtime=NOW - (30 + i) * DAY, workflow_id="wf-a")
                   for i in range(5)]
        rows = dr.plan_checkpoint_dirs(entries, {"wf-a": "failed"}, NOW, RULES)
        self.assertEqual(sum(1 for r in rows if r["decision"] == "keep"), 2)
        self.assertEqual(sum(1 for r in rows if r["decision"] == "reclaim"), 3)
        kept = sorted(r["name"] for r in rows if r["decision"] == "keep")
        self.assertEqual(kept, ["cp-00", "cp-01"], "留下的不是最近两批：%s" % kept)

    def test_newest_batch_kept_even_for_terminal_workflow(self):
        rows = dr.plan_checkpoint_dirs(
            [_entry("cp-1", mtime=NOW - 500 * DAY, workflow_id="wf-a")],
            {"wf-a": "failed"}, NOW, RULES)
        self.assertEqual(rows[0]["decision"], "keep")

    def test_one_workflow_does_not_affect_another(self):
        entries = [_entry("cp-1", mtime=NOW - 500 * DAY, workflow_id="wf-a"),
                   _entry("cp-2", mtime=NOW - 400 * DAY, workflow_id="wf-a"),
                   _entry("cp-3", mtime=NOW - 300 * DAY, workflow_id="wf-b"),
                   _entry("cp-4", mtime=NOW - 200 * DAY, workflow_id="wf-b")]
        rows = dr.plan_checkpoint_dirs(entries, {"wf-a": "failed", "wf-b": "running"}, NOW, RULES)
        decisions = {(r["workflow_id"], r["name"]): r["decision"] for r in rows}
        self.assertEqual(decisions[("wf-a", "cp-1")], "keep", "每区各留最近两批")
        self.assertEqual(decisions[("wf-a", "cp-2")], "keep")
        self.assertEqual(decisions[("wf-b", "cp-3")], "keep", "未终态区的检查点被回收了")
        self.assertEqual(decisions[("wf-b", "cp-4")], "keep")


class DistPlanTests(unittest.TestCase):
    def _rows(self, entries, references=()):
        return {r["name"]: (r["decision"], r["reason"]) for r in
                dr.plan_dist(entries, references, NOW, RULES)}

    def test_only_disposable_prefixes_are_reclaimable(self):
        decisions = self._rows([_entry("DocMind_sample_000000", mtime=NOW - 60 * DAY),
                                _entry("regression-sample-000000", mtime=NOW - 60 * DAY),
                                _entry("acceptance-sample-000000", mtime=NOW - 60 * DAY)])
        self.assertEqual({v[0] for v in decisions.values()}, {"reclaim"}, decisions)

    def test_release_installer_and_zip_are_never_touched(self):
        decisions = self._rows([_entry("release", mtime=NOW - 400 * DAY),
                                _entry("installer", mtime=NOW - 400 * DAY),
                                _entry("DocMind-v1.0.0-win64.zip", mtime=NOW - 400 * DAY,
                                       is_dir=False)])
        self.assertEqual({v[0] for v in decisions.values()}, {"keep"}, decisions)

    def test_referenced_name_is_kept(self):
        decisions = self._rows([_entry("regression-sample-000000", mtime=NOW - 90 * DAY)],
                               references={"regression-sample-000000"})
        self.assertEqual(decisions["regression-sample-000000"][0], "keep",
                         "仍被 README/脚本按名字引用的副本被判定可回收")

    def test_unknown_prefix_kept(self):
        decisions = self._rows([_entry("my-random-folder", mtime=NOW - 90 * DAY)])
        self.assertEqual(decisions["my-random-folder"][0], "keep",
                         "白名单外的目录被判定可回收 —— 保留期只管已知的一次性副本")

    def test_recent_build_kept(self):
        decisions = self._rows([_entry("DocMind_new", mtime=NOW - 2 * DAY)])
        self.assertEqual(decisions["DocMind_new"][0], "keep")


class ReferenceScanTests(unittest.TestCase):
    def test_git_failure_fails_closed(self):
        """git 用不了时必须当成「全都引用着」，而不是全都可删。"""
        with patch.object(dr.subprocess, "run", side_effect=OSError("no git")):
            found = dr.referenced_names("/tmp", ["DocMind_p0", "regression-x"])
        self.assertEqual(found, {"DocMind_p0", "regression-x"})

    def test_git_error_code_fails_closed(self):
        class _Result:
            returncode = 2
            stdout = b""
        with patch.object(dr.subprocess, "run", return_value=_Result()):
            self.assertEqual(dr.referenced_names("/tmp", ["DocMind_p0"]), {"DocMind_p0"},
                             "git 报错被当成「没引用」—— 会误删")

    def test_no_names_means_no_probe(self):
        with patch.object(dr.subprocess, "run") as run:
            self.assertEqual(dr.referenced_names("/tmp", []), set())
            run.assert_not_called()

    def test_data_and_prose_hits_are_not_dependencies(self):
        """历史评测输出里出现过这个路径名，不构成"这个目录还要用"。

        实测：整仓 4.06 GB 的一次性副本，唯一的命中文件是 `golden/results_baseline.jsonl`。
        按命中就算引用 = 给这批副本发永久豁免，保留期对 `dist/` 就完全失效。

        夹具样本刻意选成**代码后缀 + 数据目录**（`tests/fixtures/recorder.py`），
        这样挡下它的只可能是数据目录前缀这条规则；用 `.txt` 的话后缀规则就先把
        它判掉了，前缀规则等于没测（第一版就是这么空过的）。
        """
        self.assertFalse(dr._counts_as_reference("golden/results_baseline.jsonl"))
        self.assertFalse(dr._counts_as_reference("golden/recorder.py"))
        self.assertFalse(dr._counts_as_reference("docs/memory-v2-p0.md"))
        self.assertFalse(dr._counts_as_reference("tests/fixtures/recorder.py"))
        self.assertTrue(dr._counts_as_reference("packaging/build_release.ps1"))
        self.assertTrue(dr._counts_as_reference("agent_runtime/visual_acceptance.py"))

    def test_name_appearing_only_in_tests_is_not_a_dependency(self):
        """保留期自己的测试数据不许把真目录判成"仍在用"。

        实测踩过：测试样本里写了 `DocMind_p0_210820`，那三个 dist 目录就全被豁免，
        默认规则下可回收从 4.01 GB 掉到 2.69 GB —— 守卫自己造了个假依赖。
        """
        def runner(cmd, *a, **kw):
            class _R:
                returncode = 0
                stdout = b"tests/test_disk_retention.py\n"
            return _R()
        with patch.object(dr.subprocess, "run", side_effect=runner):
            self.assertEqual(dr.referenced_names("/tmp", ["DocMind_sample_000000"]), set(),
                             "只在 tests/ 里出现就当依赖 —— 白名单等于没有")

    def test_only_code_hits_mark_a_name_as_referenced(self):
        calls = []

        def runner(cmd, *a, **kw):
            calls.append(cmd)
            class _R:
                returncode = 0
                stdout = (b"golden/results_baseline.jsonl\n" if "DocMind_p0" in cmd
                          else b"golden/x.jsonl\npackaging/build.ps1\n" if "DocMind_p1" in cmd
                          else b"")
            return _R()

        with patch.object(dr.subprocess, "run", side_effect=runner):
            found = dr.referenced_names("/tmp", ["DocMind_p0", "DocMind_p1", "regression-z"])
        self.assertEqual(found, {"DocMind_p1"}, found)
        self.assertIn("-F", calls[0], "必须按固定字符串查，副本名里的 . 和 - 会被当正则")
        self.assertTrue(any(spec.endswith(".py") for spec in calls[0]),
                        "没带代码 pathspec，等于又在全仓里搜")


class ScanAndApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dr_test_")
        self.state = os.path.join(self.tmp, ".docmind")
        self.project = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.state, "workflows"))
        os.makedirs(os.path.join(self.project, "dist"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage(self, workflow, *, age_days, status=None):
        base = os.path.join(self.state, "project_stages", workflow)
        os.makedirs(os.path.join(base, "workspace"), exist_ok=True)
        os.makedirs(os.path.join(base, "baseline"), exist_ok=True)
        with open(os.path.join(base, "workspace", "x.txt"), "w", encoding="utf-8") as fh:
            fh.write("x" * 100)
        stamp = NOW - age_days * DAY
        os.utime(base, (stamp, stamp))
        if status is not None:
            with open(os.path.join(self.state, "workflows", workflow + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"status": status}, fh)
        return base

    def _scan(self, statuses, **kwargs):
        return dr.scan(self.state, self.project, now=NOW, statuses=statuses,
                       references=(), rules=RULES, **kwargs)

    def test_scan_separates_reclaim_from_needs_review(self):
        self._stage("wf-dead", age_days=30, status="failed")
        self._stage("wf-live", age_days=30, status="awaiting_choice")
        self._stage("wf-ghost", age_days=30)          # 无状态文件
        plan = self._scan({"wf-dead": "failed", "wf-live": "awaiting_choice"})
        decisions = {i["name"]: i["decision"] for i in plan["items"]}
        self.assertEqual(decisions["wf-dead"], "reclaim", decisions)
        self.assertEqual(decisions["wf-live"], "keep", "未终态的区被判定成可回收")
        self.assertEqual(decisions["wf-ghost"], "needs_review", decisions)
        summary = plan["summary"]
        self.assertEqual(summary["reclaimable_items"], 1, summary)
        self.assertEqual(summary["needs_review_items"], 1, summary)

    def test_apply_dry_run_touches_nothing(self):
        base = self._stage("wf-dead", age_days=30, status="failed")
        plan = self._scan({"wf-dead": "failed"})
        self.assertTrue(any(i["decision"] == "reclaim" for i in plan["items"]),
                        "计划里没有可回收项，这条 dry-run 断言就是空过")
        result = dr.apply_items(plan["items"], state_root=self.state, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertGreater(result["freed_bytes"], 0)
        self.assertTrue(os.path.isdir(base), "dry-run 把目录删了")

    def test_apply_removes_reclaimable_stage_dir_only(self):
        dead = self._stage("wf-dead", age_days=30, status="failed")
        live = self._stage("wf-live", age_days=30, status="running")
        plan = self._scan({"wf-dead": "failed", "wf-live": "running"})
        result = dr.apply_items(plan["items"], state_root=self.state, dry_run=False)
        self.assertFalse(os.path.isdir(dead), "可回收项没被删")
        self.assertTrue(os.path.isdir(live), "未终态的区被删了")
        self.assertEqual(result["failed"], [], result["failed"])
        self.assertGreater(result["freed_bytes"], 0)

    def test_apply_records_failures_instead_of_losing_the_batch(self):
        """一个条目失败不许把整批带走；被守卫挡下的那些必须还在原地。

        用「路径是文件」和「路径越出预期根」两种情况 —— 符号链接在 Windows 上需要
        开发者权限，靠 skipTest 的守卫等于没有守卫。
        """
        good = self._stage("wf-a", age_days=45, status="failed")
        outside = os.path.join(self.tmp, "not_a_copy")
        os.makedirs(os.path.join(outside, "workspace"), exist_ok=True)
        a_file = os.path.join(self.state, "project_checkpoints")
        os.makedirs(a_file, exist_ok=True)
        pointer = os.path.join(a_file, "cp-x")
        with open(pointer, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        items = [
            {"kind": dr.STAGES, "name": "wf-a", "path": good, "bytes": 100,
             "decision": "reclaim"},
            {"kind": dr.CHECKPOINTS, "name": "cp-x", "path": pointer, "bytes": 100,
             "decision": "reclaim"},
            {"kind": dr.STAGES, "name": "wf-outside", "path": outside, "bytes": 999,
             "decision": "reclaim"},
            {"kind": dr.STAGES, "name": "wf-skip", "path": good, "bytes": 1,
             "decision": "keep"},
        ]
        result = dr.apply_items(items, state_root=self.state, dry_run=False)
        self.assertEqual(len(result["removed"]), 1, result)
        self.assertEqual(len(result["failed"]), 2, result["failed"])
        self.assertFalse(os.path.isdir(good), "合法项没删掉")
        self.assertTrue(os.path.isfile(pointer), "路径是文件的条目竟然删成功了")
        self.assertTrue(os.path.isdir(outside), "越出预期根的路径被删了 —— 根校验没生效")

    def test_apply_items_stops_at_the_per_run_cap(self):
        """单次上限兜住"保留天数设成 1 天"这种一口气清几百项的形状。"""
        items = [{"kind": dr.CHECKPOINTS, "name": "cp-%d" % i, "bytes": 10,
                  "decision": "reclaim",
                  "path": self._checkpoint("cp-%d" % i)} for i in range(3)]
        result = dr.apply_items(items, state_root=self.state, dry_run=False, max_items=1)
        self.assertEqual(len(result["removed"]), 1, result)
        self.assertEqual(len(result["skipped"]), 2, result)
        self.assertIn("单次上限", result["skipped"][0]["reason"])
        # 剩下的两项必须还在原处（"下轮再收"不等于"顺手删了"）。
        self.assertTrue(os.path.isdir(items[1]["path"]))
        self.assertTrue(os.path.isdir(items[2]["path"]))

    def _checkpoint(self, name):
        path = os.path.join(self.state, "project_checkpoints", "wf-a", name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_items_skipped_when_not_reclaim(self):
        keep = self._stage("wf-keep", age_days=1, status="completed")
        plan = self._scan({"wf-keep": "completed"})
        result = dr.apply_items(plan["items"], state_root=self.state, dry_run=False)
        self.assertEqual(result["removed"], [], result)
        self.assertTrue(os.path.isdir(keep))

    def test_apply_items_refuses_a_whitelisted_name_even_when_told_to_reclaim(self):
        """动手处再钉一遍白名单：`items` 可能来自别的调用方，不能只信计划阶段那一道。"""
        good = self._stage("wf-keepme", age_days=40, status="completed")
        items = [{"kind": dr.STAGES, "name": "wf-keepme", "path": good, "bytes": 10,
                  "decision": "reclaim"}]
        result = dr.apply_items(items, state_root=self.state, dry_run=False,
                                whitelist=["wf-keepme"])
        self.assertEqual(result["removed"], [], result)
        self.assertIn("白名单", result["skipped"][0]["reason"])
        self.assertTrue(os.path.isdir(good), "用户点名不清的副本被删了")
        # 同一批不带名单时确实删得掉 —— 证明上一条挡住它的是白名单，不是别的守卫。
        dr.apply_items(items, state_root=self.state, dry_run=False)
        self.assertFalse(os.path.isdir(good), "对照组删不掉，挡它的就不是白名单")


if __name__ == "__main__":
    unittest.main()
