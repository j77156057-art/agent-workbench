"""④之②·乙：截断证据 × 确定性计数口吻 对撞。

现场："准确总数 194"（真值 196）——模型的分项加总恰好自洽（②甲抓不到），但那个数
是拿**被截断的观察**拼出来的。乙把"答案自称准确"与"本轮代码检索证据自报少计型截断"
两件事对撞：两条**同时**成立才判红，缺一不碰（误伤面极小）。

触发口径（用户拍板·严）：
  * 只有答案带确定性口吻词（准确/精确/确切/实际/最终）紧邻数字，才算"声称准确"；
  * 只有 grep / search_code / dev_glob 的**少计型**截断算证据不完整（read_file 单文件
    截断、web 正文截断、grep 跳过 .docmind 影子副本都不算——非少计/不同口径）。

全程离线打桩，喂合成 dict + 合成 obs 文本，不触真实 FS（引用留空即可）。
"""

import unittest

from agent_runtime.output_audit import (
    audit_evidence, obs_reports_code_undercount)


def _audit(text, steps, question="整个项目一共定义了多少个函数？给准确总数"):
    return audit_evidence({"text": text, "trace": {"steps": steps}},
                          question=question, require_read=False)


class ObsUndercountMarkerTests(unittest.TestCase):
    """helper 只认代码检索类工具的"少计型"截断，其它一律 False。"""

    def test_grep_hit_cap_counts(self):
        self.assertTrue(obs_reports_code_undercount(
            "grep(pattern: func)",
            "…\n（提示：已达 40 条上限并**截断**，实际匹配可能更多——不要把这 40 条当全部）"))

    def test_grep_big_file_skip_counts(self):
        self.assertTrue(obs_reports_code_undercount(
            "grep(pattern: func)", "命中 3 行\n另有 2 个 >2MB 的文件未搜索。"))

    def test_search_code_topk_counts(self):
        self.assertTrue(obs_reports_code_undercount(
            "search_code(hero)",
            "[a.py:L1-L2]\n…\n（提示：本次为语义检索，按相关度最多返回前 4 条，已取满；）"))

    def test_glob_narrow_counts(self):
        self.assertTrue(obs_reports_code_undercount(
            "dev_glob(pattern: *.gd)", "找文件：命中 500 个，只显示前 80 个（浅层优先）"))

    def test_glob_scan_cap_counts(self):
        self.assertTrue(obs_reports_code_undercount(
            "dev_glob(pattern: *.gd)", "目录条目超过 5000 上限，结果可能不完整；请用 scope: 缩小范围。"))

    def test_docmind_state_skip_is_NOT_undercount(self):
        # grep 跳过 .docmind 影子副本 ≠ 少计（产物仍搜）——绝不能被当截断误伤
        self.assertFalse(obs_reports_code_undercount(
            "grep(pattern: func)", "命中 2 行\n已跳过 DocMind 状态目录（产物目录仍会搜索）：.docmind/projects。"))

    def test_read_file_truncation_is_excluded(self):
        # read_file 单文件正文截断与"跨全仓计数"不同口径，不算
        self.assertFalse(obs_reports_code_undercount(
            "read_file(x.gd)", "…\n…（已截断，仅显示前 12000 字，共 900 行）"))

    def test_clean_output_not_truncated(self):
        self.assertFalse(obs_reports_code_undercount("grep(pattern: func)", "enemy.gd:2: func a"))

    def test_non_code_tool_never_counts(self):
        self.assertFalse(obs_reports_code_undercount("web_search(x)", "已达 40 条上限并截断 已取满"))


class AccuracyClaimTests(unittest.TestCase):
    def test_accuracy_word_plus_number_present(self):
        r = _audit("我扫描完了。准确总数 194 个函数。", [])
        self.assertIn("count_evidence_complete", r["checks"])
        self.assertEqual(r["accuracy_count"], 194)

    def test_plain_total_without_accuracy_is_not_judged(self):
        # 只写"总计 50"无确定性口吻 -> 乙不判定（那是②甲的活），key 缺席
        r = _audit("总计 50 个函数", [])
        self.assertNotIn("count_evidence_complete", r["checks"])
        self.assertIsNone(r["accuracy_count"])


class CollisionTests(unittest.TestCase):
    _TRUNC_STEP = [{"action": "grep(pattern: func path: .)", "ok": True, "truncated": True}]
    _CLEAN_STEP = [{"action": "grep(pattern: func path: .)", "ok": True, "truncated": False}]

    def test_accuracy_claim_over_truncated_evidence_fails(self):
        r = _audit("准确总数 194 个函数", self._TRUNC_STEP)
        self.assertFalse(r["gate_ok"])
        self.assertFalse(r["checks"]["count_evidence_complete"])
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue(r["evidence_truncated"])
        self.assertEqual(r["truncated_evidence_tools"], ["grep"])

    def test_accuracy_claim_over_complete_evidence_passes(self):
        r = _audit("准确总数 196 个函数", self._CLEAN_STEP)
        self.assertTrue(r["checks"]["count_evidence_complete"])
        self.assertEqual(r["verdict"], "PASS")

    def test_truncated_evidence_without_accuracy_claim_is_left_alone(self):
        # 证据截断但答案没自称准确（也无分项矛盾）-> 不判（保守，绝不误伤如实的估计）
        r = _audit("大概有几十处，分布在各文件", self._TRUNC_STEP)
        self.assertNotIn("count_evidence_complete", r["checks"])
        self.assertEqual(r["verdict"], "PASS")

    def test_no_root_cannot_soften_the_collision(self):
        # 乙是内容/口吻错误：即便没有代码根，也不能被降级成 PARTIAL 原样放出
        # （引用留空 -> 走无 root 路径仍应 FAIL）
        r = audit_evidence({"text": "准确总数 194 个函数",
                            "trace": {"steps": self._TRUNC_STEP}},
                           question="一共多少个函数", require_read=True)
        self.assertEqual(r["verdict"], "FAIL")


class AgentReflectionWiringTests(unittest.TestCase):
    """agent._evidence_failure_detail 要把"哪个数字自称准确、哪几个工具截断"说到具体。"""

    def test_names_number_and_tool_in_detail_and_reflection(self):
        import agent
        r = _audit("准确总数 194 个函数",
                   [{"action": "grep(pattern: func)", "ok": True, "truncated": True}])
        detail, reflection = agent._evidence_failure_detail(r)
        self.assertIn("194", detail)
        self.assertIn("grep", detail)
        self.assertIn("194", reflection)

    def test_complete_evidence_produces_no_collision_label(self):
        import agent
        r = _audit("准确总数 196 个函数",
                   [{"action": "grep(pattern: func)", "ok": True, "truncated": False}])
        detail, reflection = agent._evidence_failure_detail(r)
        self.assertNotIn("196", detail)


if __name__ == "__main__":
    unittest.main()
