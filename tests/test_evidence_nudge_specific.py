# -*- coding: utf-8 -*-
"""证据门禁的纠偏必须说清**是哪一项没过**。

## 实测浪费

模型第一轮已经完整读了 enemy.gd(53行) / player.gd(49行) / hud.gd(36行)，被判证据
未过，于是被要求把这三个文件**带区间又读了一遍**——多花四次工具调用。

而那句纠偏是**写死的**：

    Evidence check: 这是定位类问题，但最终回答缺少可核验的 read_file 原文。

不管实际失败的是哪一项。如果真正的问题是「引用路径打不开」（例如写成 `hud.gd:20`
而不是 `ui/hud.gd:20`）或「行号超出文件行数」，重读多少次都不解决，而真正该改的
那一处没有任何人告诉模型。

`audit_evidence` 其实已经把明细放在 `checks` 与 `references` 里了，只是被丢掉。

## 说明

本轮那一轮的答案正文不在 trace 里，所以**无法断定**当时失败的是哪一项——不猜。
可确定且要修的是：纠偏文案不该对所有失败原因说同一句话。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
from agent_runtime.output_audit import audit_evidence  # noqa: E402

PROJECT = r"D:\WorkBuddy\godot_sample"
HAVE_PROJECT = os.path.isdir(PROJECT)


class DetailTranslationTests(unittest.TestCase):
    """每项失败都要产出对应的、且**不相同**的说明。"""

    def _detail(self, checks, refs=None):
        return agent_mod._evidence_failure_detail(
            {"checks": checks, "references": refs or []})

    def test_each_failing_check_gets_its_own_advice(self):
        for key, hint in agent_mod._EVIDENCE_CHECK_HINTS.items():
            detail, reflection = self._detail({key: False})
            self.assertIn(hint, detail,
                          "%s 失败时没有给出对应说明" % key)
            self.assertIn(hint, reflection,
                          "%s 失败时给用户的 reflection 也没说清" % key)

    def test_only_the_failing_ones_are_mentioned(self):
        detail, _ = self._detail({"read_file_present": False,
                                  "references_exist": True,
                                  "reference_lines_valid": True,
                                  "reference_present": True})
        self.assertIn("还没有成功读取过候选文件", detail)
        self.assertNotIn("行号超出", detail,
                         "把没失败的项也说了一遍——模型会去做无用改动")
        self.assertNotIn("路径在当前代码库里打不开", detail,
                         "把没失败的一项也说了一遍")

    def test_an_unopenable_reference_is_named_with_its_path(self):
        refs = [{"path": "hud.gd", "start_line": 20, "end_line": 20,
                 "exists": False, "in_root": True, "line_ok": False},
                {"path": "ui/hud.gd", "start_line": 16, "end_line": 30,
                 "exists": True, "in_root": True, "line_ok": True}]
        detail, _ = self._detail({"references_exist": False}, refs)
        self.assertIn("hud.gd:20", detail,
                      "必须点名打不开的那个引用——只说「有路径打不开」模型无从下手")
        self.assertNotIn("ui/hud.gd:16", detail,
                         "把正常的引用也列进问题清单了")

    def test_an_out_of_range_line_is_named_separately(self):
        refs = [{"path": "behaviors/enemy.gd", "start_line": 999, "end_line": 999,
                 "exists": True, "in_root": True, "line_ok": False}]
        detail, _ = self._detail({"reference_lines_valid": False}, refs)
        self.assertIn("enemy.gd:999", detail)
        self.assertIn("实际行数", detail,
                      "行号越界要说明得先确认文件实际行数，而不是让人再读一遍")

    def test_no_failures_still_says_something(self):
        detail, reflection = self._detail({"read_file_present": True})
        self.assertTrue(reflection.strip())
        self.assertFalse(detail.strip())

    def test_empty_audit_does_not_crash(self):
        detail, reflection = agent_mod._evidence_failure_detail(None)
        self.assertTrue(reflection.strip())


class NudgeTextIsNoLongerHardcodedTests(unittest.TestCase):
    """防回归：那句写死的"缺少 read_file 原文"不许再出现。"""

    def _nudge_src(self):
        import inspect
        return inspect.getsource(agent_mod.Agent._run)

    def test_the_nudge_quotes_the_failing_items(self):
        self.assertIn("_evidence_failure_detail(evidence_audit)", self._nudge_src(),
                      "纠偏文案没有使用失败项明细——所有失败原因仍然说同一句话")

    def test_the_old_hardcoded_wording_is_gone(self):
        self.assertNotIn(
            "但最终回答缺少可核验的 ", self._nudge_src(),
            "写死的「缺少可核验的 read_file 原文」还在——"
            "路径写错时会把模型导向重读文件，而重读解决不了路径写错")

    def test_the_reflection_also_comes_from_the_detail(self):
        self.assertIn("_reflection", self._nudge_src(),
                      "给用户看的 reflection 仍在用固定文案")


@unittest.skipUnless(HAVE_PROJECT, "需要真实的 godot_sample 项目")
class RealAuditBehaviourTests(unittest.TestCase):
    """用真实文件确认：完整读取算数，而路径写错会被明确判为 references_exist。"""

    def test_a_full_read_counts_as_verification(self):
        """实测第一轮已完整读了三个文件却仍被判失败——所以要确认审计不是这个原因。"""
        out = audit_evidence(
            {"text": "扣多少看 behaviors/enemy.gd:7，实际改血在 behaviors/player.gd:39。",
             "trace": {"steps": [{"action": "read_file", "ok": True, "input": "behaviors/enemy.gd"},
                                 {"action": "read_file", "ok": True, "input": "behaviors/player.gd"}]}},
            question="玩家受伤扣多少血在哪算的？", code_root=PROJECT)
        self.assertTrue(out["ok"],
                        "完整读取竟不算核验——那么每次定位类回答都要多花一轮区间读取")

    def test_a_bare_basename_is_caught_as_an_unopenable_reference(self):
        out = audit_evidence(
            {"text": "显示在 hud.gd:20。",
             "trace": {"steps": [{"action": "read_file", "ok": True, "input": "ui/hud.gd"}]}},
            question="HUD 在哪刷新？", code_root=PROJECT)
        self.assertFalse(out["checks"]["references_exist"],
                         "写成 hud.gd:20（少了 ui/ 前缀）竟被判为可打开——"
                         "那正是纠偏会误导模型的场景")
        detail, _ = agent_mod._evidence_failure_detail(out)
        self.assertIn("hud.gd:20", detail)


if __name__ == "__main__":
    unittest.main()