# -*- coding: utf-8 -*-
"""② 工具观察豁免：自带结构化判定的权威工具（eda_report_gate）顶替「文件:行号」。

背景（docmind-drill-bugs.md A2）：`output_audit.audit_evidence` 的定位门只要问题里
蹭到定位触发词就要求正文给出可核验的仓库引用。但 EDA 视觉/报告这类终答的**权威证据是
工具算出来的那条观察本身**（met/unmet/rollback，来自真取像/报告解析），正文合理地不含
任何 `文件:行号`。旧逻辑据此把如实的结论倒盖成 FAIL，还 nudging 模型去「补引用」（可能
诱导编造行号）。

本测试钉住豁免的**边界**，确保它只在正确的情形生效：
  - 豁免成立：正文无引用 + 本轮有一次**成功**的权威工具观察 -> 不再 FAIL。
  - 不越界：正文一旦写了引用，照旧逐一核验（幻觉路径 / 越界行号仍 FAIL）。
  - 不空转：没有权威工具观察（只有 read_file）时，定位门照旧拦住（保住
    `test_no_reference_at_all_fails_the_gate` 等锁测的语义）。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402,F401  （与既有测试一致，确保 import 链不炸）
from agent_runtime.output_audit import audit_evidence  # noqa: E402
from tools import evidence_authority_tool_names  # noqa: E402

# 一个会命中定位门的中文问题（含「在哪」「逻辑」两个触发词）。
QUESTION = "这份 EDA 报告的时序逻辑在哪算的？"
# 权威工具**成功返回**、正文不含任何 file:line 引用的终答。
AUTHORITY_STEP = [{"action": 'eda_report_gate({"tool": "vivado", "kind": "timing"})',
                   "ok": True}]
NO_REF_ANSWER = "时序收敛未达成（unmet），建议回滚到上一版约束。"
# 传给 audit_evidence 的权威集合（单测里用字面量，不依赖真实注册表内容）。
ED = {"eda_report_gate"}


class AuthorityExemptionTests(unittest.TestCase):
    """豁免只在「成功权威观察 + 正文无引用」时生效。"""

    def test_authority_step_without_refs_is_exempt(self):
        out = audit_evidence({"text": NO_REF_ANSWER, "trace": {"steps": AUTHORITY_STEP}},
                             question=QUESTION, authority_tools=ED)
        self.assertTrue(out["authority_exempt"],
                        "本轮有成功的权威工具观察、正文无引用 -> 应豁免")
        self.assertFalse(out["required"], "豁免必须把 required 拉下来")
        self.assertTrue(out["gate_ok"], "如实的 unmet 结论不该被定位门倒盖成拦住")
        self.assertNotEqual(out["verdict"], "FAIL",
                            "PARTIAL/FAIL 都只该给真问题或环境限制，不该给如实结论")
        self.assertTrue(out["ok"], "ok 也该随 required 一起放行")

    def test_no_authority_step_still_fails(self):
        """没有权威观察（只有 read_file）时不得豁免——保住锁测语义。"""
        out = audit_evidence(
            {"text": NO_REF_ANSWER,
             "trace": {"steps": [{"action": "read_file(behaviors/enemy.gd)", "ok": True}]}},
            question=QUESTION, authority_tools=ED)
        self.assertFalse(out["authority_exempt"], "read_file 不是权威工具，不该触发豁免")
        self.assertTrue(out["required"], "无豁免时定位门仍要求引用")
        self.assertFalse(out["gate_ok"], "定位类问题、无引用、无权威观察 -> 必须拦住")
        self.assertEqual(out["verdict"], "FAIL")

    def test_failed_authority_step_is_not_exempt(self):
        """权威工具**失败**的观察不是证据 -> 不得豁免（否则给幻觉开了后门）。"""
        out = audit_evidence(
            {"text": NO_REF_ANSWER,
             "trace": {"steps": [{"action": 'eda_report_gate({"tool":"vivado"})', "ok": False}]}},
            question=QUESTION, authority_tools=ED)
        self.assertFalse(out["authority_exempt"], "失败的权威观察不能顶替证据")
        self.assertFalse(out["gate_ok"])

    def test_non_authority_success_is_not_exempt(self):
        """普通工具（web_search）即使成功也不豁免——只有带标记的权威工具才算。"""
        out = audit_evidence(
            {"text": NO_REF_ANSWER,
             "trace": {"steps": [{"action": "web_search(timing report)", "ok": True}]}},
            question=QUESTION, authority_tools=ED)
        self.assertFalse(out["authority_exempt"], "web_search 不在权威集合里")
        self.assertFalse(out["gate_ok"])

    def test_authority_with_hallucinated_ref_is_not_exempt(self):
        """正文一旦写了引用，豁免不触发——幻觉路径仍被逐一核验并 FAIL。"""
        with tempfile.TemporaryDirectory() as root:
            out = audit_evidence(
                {"text": "时序在 ghost.gd:12 处理。", "trace": {"steps": AUTHORITY_STEP}},
                question=QUESTION, code_root=root, authority_tools=ED)
        self.assertFalse(out["authority_exempt"],
                         "有引用就不该走豁免分支（not refs 守卫）")
        self.assertFalse(out["gate_ok"], "打不开的引用必须仍被拦住")
        self.assertEqual(out["verdict"], "FAIL", "能查且查出问题 -> FAIL")

    def test_no_authority_tools_passed_behaves_as_before(self):
        """不传 authority_tools（默认 None）-> 行为与改动前完全一致（向后兼容）。"""
        out = audit_evidence({"text": NO_REF_ANSWER, "trace": {"steps": AUTHORITY_STEP}},
                             question=QUESTION)
        self.assertFalse(out["authority_exempt"], "没传权威集合时不应凭空豁免")
        self.assertFalse(out["gate_ok"])


class AuthorityRegistryFlagTests(unittest.TestCase):
    """注册表侧：eda_report_gate 被标为权威，普通工具不被误标。"""

    def test_eda_report_gate_is_flagged(self):
        names = evidence_authority_tool_names()
        self.assertIn("eda_report_gate", names,
                      "eda_report_gate 的 verdict 就是终答权威证据，必须打标")

    def test_ordinary_tools_are_not_flagged(self):
        names = evidence_authority_tool_names()
        for tool in ("web_search", "read_file", "grep"):
            self.assertNotIn(tool, names, f"{tool} 的观察不是结构化判定，不该顶替引用")


if __name__ == "__main__":
    unittest.main(verbosity=2)
