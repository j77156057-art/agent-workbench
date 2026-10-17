# -*- coding: utf-8 -*-
"""③「来源可溯」审计门的窄触发语义。

背景（根因①那类翻车的度量补强）：`audit_evidence` 过去只验「路径能打开 + 行号
不越界 + 这轮读过该文件」，从不校验「结论声称『来自真实读取』的那段正文本轮是否
真送达」。实测模型能凭空写出第 757-768 行函数体、白纸黑字声明「不是推断」，因这些
行在磁盘上真实存在而判 PASS·2 处可核验。

本门的设计分寸（用户选定「只在有明确来源断言时验」）：
  · 只有结论**主动**写下「来自真实读取 / 不是推断」这类可证伪断言时才触发；
  · 数据源是 ①修好后如实的送达覆盖 `_READ_COVERAGE`（经 `read_served_ranges()`）；
  · **只对「有该文件送达记录、却没覆盖被引区间」判红**；查不到覆盖 = 不可判定 =
    不 FAIL（对齐本模块 `no_root` 的信条：没检查过就别断言失败）；
  · 不做断言、或不传 served_ranges 时，`provenance_traceable` 根本不进 checks——
    既有 PASS/FAIL 语义零回归。
"""
import os
import sys
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.output_audit import audit_evidence  # noqa: E402

_FULL_FILE_END = 1 << 30  # 与 tools 里表示「整文件已送达」的哨兵对齐


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docmind_prov_")
        # 造一本 1000 行的真实文件，使被引 757-768 落在实际行数内（line_ok=True），
        # 从而把「来源可溯」这一维从「行号越界」里干净地隔离出来。
        self.name = "big.gd"
        with open(os.path.join(self.root, self.name), "w", encoding="utf-8") as f:
            for i in range(1, 1001):
                f.write("func h%d() -> int:\n    return %d\n" % (i, i))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _cov_key(self):
        # 审计侧键 = normcase((root/rel).resolve())
        from pathlib import Path
        return os.path.normcase(str((Path(self.root) / self.name).resolve()))

    def _audit(self, text, served, question="敌人逻辑在哪个文件哪一行？"):
        return audit_evidence(
            {"text": text, "trace": {"steps": [{"action": "read_file(path: %s)" % self.name}]}},
            question=question, code_root=self.root, require_read=True,
            served_ranges=served)


class ClaimAndNotServedTests(_Root):
    def test_claimed_read_but_uncovered_range_fails_the_gate(self):
        """核心：模型声称读过，但送达覆盖明显没包含那段 -> FAIL + 点名。"""
        key = self._cov_key()
        text = ("实现见 %s:757-768；上面的行号与函数体来自那次真实读取，不是推断。" % self.name)
        out = self._audit(text, {key: [(1, 300)]})  # 只送达 1-300，尾段从没给
        self.assertIn("provenance_traceable", out["checks"])
        self.assertFalse(out["checks"]["provenance_traceable"])
        self.assertFalse(out["gate_ok"])
        self.assertEqual(out["verdict"], "FAIL")
        unv = out["unverified_provenance"]
        self.assertTrue(any(r["path"] == self.name and r["start_line"] == 757
                            for r in unv), unv)


class ClaimAndServedTests(_Root):
    def test_claimed_read_covered_by_served_range_passes(self):
        """送达覆盖确实包含被引区间 -> 通过，来源断言属实。"""
        key = self._cov_key()
        text = ("实现见 %s:757-768；这些行来自真实读取，不是推断。" % self.name)
        out = self._audit(text, {key: [(1, 800)]})
        self.assertTrue(out["checks"].get("provenance_traceable"))
        self.assertEqual(out["unverified_provenance"], [])
        self.assertTrue(out["gate_ok"])

    def test_whole_file_sentinel_covers_any_range(self):
        """(1, _FULL_FILE_END) 表示整文件送达 -> 任何区间都算已送达。"""
        key = self._cov_key()
        text = ("%s:999 的常量来自逐行读取，非推测。" % self.name)
        out = self._audit(text, {key: [(1, _FULL_FILE_END)]})
        self.assertTrue(out["checks"].get("provenance_traceable"))
        self.assertEqual(out["unverified_provenance"], [])


class NoClaimTests(_Root):
    def test_plain_citation_without_claim_is_not_judged(self):
        """没作来源断言的普通引用 -> 这一项根本不进 checks，回归零冲击。"""
        key = self._cov_key()
        text = "实现见 %s:757-768。" % self.name  # 同一条从未送达的区间，但没声称读过
        out = self._audit(text, {key: [(1, 300)]})
        self.assertNotIn("provenance_traceable", out["checks"])
        self.assertNotIn("provenance_traceable", out["gate_checks"])
        self.assertTrue(out["gate_ok"])  # 行号存在、路径可开 -> 照旧 PASS


class UndecidableTests(_Root):
    def test_no_served_data_skips_judgement(self):
        """served_ranges=None（调用方没覆盖数据）-> 不判定，绝不据此 FAIL。"""
        text = "%s:757-768 来自真实读取，不是推断。" % self.name
        out = self._audit(text, None)
        self.assertNotIn("provenance_traceable", out["checks"])
        self.assertTrue(out["gate_ok"])

    def test_no_coverage_entry_for_that_file_is_not_a_failure(self):
        """有覆盖数据、但没有**该文件**的条目 -> 不可判定，跳过而非判红。"""
        text = "%s:757-768 来自真实读取。" % self.name
        out = self._audit(text, {"some/other/path": [(1, 300)]})
        # 进入判定（claim+root+served 都在），但该引用无覆盖记录 -> 不记为未达 -> 通过
        self.assertTrue(out["checks"].get("provenance_traceable"))
        self.assertEqual(out["unverified_provenance"], [])
        self.assertTrue(out["gate_ok"])

    def test_no_code_root_never_judges(self):
        """没有代码根 -> 无从解析覆盖键 -> 不判定（对齐 no_root 信条）。"""
        text = "big.gd:757-768 来自真实读取。"
        out = audit_evidence(
            {"text": text, "trace": {"steps": [{"action": "read_file(path: big.gd)"}]}},
            question="在哪一？", code_root="", require_read=True,
            served_ranges={"x": [(1, 300)]})
        self.assertNotIn("provenance_traceable", out["checks"])


if __name__ == "__main__":
    unittest.main()
