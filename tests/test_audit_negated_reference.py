# -*- coding: utf-8 -*-
"""核验必须识别否定语境：模型如实报告试错，不该被门禁拒掉。

## 实测事故（用户截图 + 三轮 research 全失败）

自主开发舱一个 `wf` 任务，5 个子任务 **0/5 完成**，重规划 2 次仍全败。
原因是 `research` 子代理连续三次被「Harness 复核」拒掉：

    第 1 轮  核验：FAIL · 28 处引用可核验 · 2 处未通过（路径打不开）：player.gd:?、enemy.gd:?
    第 2 轮  核验：FAIL · 44 处引用可核验 · 5 处未通过（路径打不开）：…
    第 3 轮  核验：FAIL · 33 处引用可核验 · 3 处未通过（路径打不开）：
            addons/godot_ai/port_resolver.gd:?、addons/godot_ai/update_installer.gd:?…

**第 2 轮的结论已经把路径全部改对了**（正文里写的是
`addons/godot_ai/utils/port_resolver.gd`），却仍被拒 —— 因为它同时**如实写了**
哪些路径是错的：

    路径更正：`addons/godot_ai/port_resolver.gd` 不存在；
    真实路径 `addons/godot_ai/utils/port_resolver.gd`。

审计把否定句里的路径当成**引用**，去代码根找，找不到 → 「路径打不开」→ FAIL。

> **方向完全反了：模型越如实报告自己的试错，越过不了门禁。**
> 而 `research` 失败连累 `implement`/`verify` 全部 blocked，最终 0/5。

## 判据：按**子句**判否定，不是按字符窗口

⚠ 第一版用「路径前后 24 字窗口」找否定词，**实测会误伤正面引用** ——
否定词与下一条路径只隔 14 字符：

    不存在 `a/ghost.gd` 这个文件；真实实现见 `b/real.gd`。
                                         ^^^^^^^^ 被当成否定的

按「。；！？」切成子句后，前者在子句 0、后者在子句 1，互不干扰。

## ⚠ 三条不能被这条修复换掉的东西

1. **正面断言不存在的文件必须仍被拦**（真幻觉）
2. **行号越界必须仍被拦**
3. **否定句里的路径不核验，但要在结果里记数**（`skipped_negated_refs`），
   否则这类跳过会变成看不见的静默
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.output_audit import (  # noqa: E402
    _ref_is_negated, audit_evidence,
)

GOOD = "addons/godot_ai/utils/port_resolver.gd"
OTHER = "addons/godot_ai/utils/update_installer.gd"
BAD = "addons/godot_ai/port_resolver.gd"       # 少了 utils/ —— 真实错路径
GHOST = "addons/godot_ai/does_not_exist.gd"


class _Root:
    """一次搭好代码根，让存在性判定是真实的而不是空目录的巧合。"""

    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="audit_neg_"))
        for name in ("port_resolver.gd", "update_installer.gd"):
            target = self.path / "addons" / "godot_ai" / "utils" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join("# %d" % i for i in range(1, 320)),
                              encoding="utf-8")

    def audit(self, conclusion):
        return audit_evidence(
            {"conclusion": conclusion,
             "steps": [{"action": "read_file(path: %s)" % GOOD}]},
            question="这个 addon 有什么功能？",
            code_root=str(self.path))


class NegatedReferenceIsNotAReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = _Root()

    # ---------- 事故形态：必须放行 ----------

    def test_reporting_a_wrong_path_does_not_fail_the_gate(self):
        """截图原句：如实点名错路径并给出正确路径，必须放行。"""
        text = ("路径更正：`%s` 不存在；真实路径 `%s:303`。"
                "另 `addons/godot_ai/update_installer.gd` 读不到，"
                "实际在 `%s:176`。" % (BAD, GOOD, OTHER))
        res = self.root.audit(text)
        self.assertTrue(res["gate_ok"],
                        "如实报告试错的路径被判 FAIL —— 这就是那三轮 research "
                        "全败的原因。核验: %s" % (res.get("verdict")))
        self.assertEqual(res["skipped_negated_refs"], 4,
                         "跳过的应该是 4 个错路径（两处否定各带路径与其复述）")

    def test_the_path_trap_note_also_passes(self):
        """「路径陷阱：xxx 是错的（读不到），实际在 yyy」也要放行。"""
        text = ("路径陷阱：`%s` 是错的（读不到），"
                "实际在 `%s`。" % (BAD, GOOD))
        self.assertTrue(self.root.audit(text)["gate_ok"],
                        "「路径陷阱」句式被误判成引用")

    # ---------- 闸门不能被修没 ----------

    def test_a_plain_hallucinated_reference_is_still_blocked(self):
        """真幻觉：正面断言一个不存在的文件，必须仍被拦。"""
        res = self.root.audit("实现见 `%s:100`。" % GHOST)
        self.assertFalse(res["gate_ok"],
                         "否定句判据把**正面幻觉**也放过了 —— 闸门被修没了")
        self.assertEqual([c["path"] for c in res["references"]], [GHOST])

    def test_out_of_range_line_numbers_are_still_blocked(self):
        res = self.root.audit("实现见 `%s:9999`。" % GOOD)
        self.assertFalse(res["gate_ok"], "行号闸门被修没了")

    def test_the_positive_ref_next_to_a_negation_is_still_checked(self):
        """否定一个 + 正面断言另一个不存在的 -> 后面那个必须被拦。

        这条是**子句切分**的判据：两个子句各判各的。
        第一版用 24 字窗口时，这条会因「不存在」落在窗口内而被放过。
        """
        res = self.root.audit(
            "不存在 `%s`；实现见 `addons/godot_ai/b.gd:1`。" % BAD)
        self.assertFalse(res["gate_ok"],
                         "否定语境吃掉了相邻子句的正面引用 —— "
                         "子句切分没生效，或退化成了固定窗口")
        self.assertEqual([c["path"] for c in res["references"]],
                         ["addons/godot_ai/b.gd"],
                         "被核验的应当只有正面那条")
        self.assertEqual(res["skipped_negated_refs"], 1)

    def test_a_positive_reference_is_still_verified(self):
        """对照组：纯正面引用仍要走完整核验（不能被跳过）。"""
        res = self.root.audit("实现见 `%s:303`。" % GOOD)
        self.assertEqual([c["path"] for c in res["references"]], [GOOD])
        self.assertEqual(res["skipped_negated_refs"], 0,
                         "正面引用被当成否定的跳过了 —— 判据反了")


class NegationDetectionUnitTests(unittest.TestCase):
    """`_ref_is_negated` 的单元判据：子句边界是真的边界。"""

    def _is_negated(self, text, path):
        import re
        m = re.search(re.escape(path), text)
        self.assertIsNotNone(m, "路径不在文本里")
        return _ref_is_negated(text, m.start(), m.end())

    def test_negation_before_the_path(self):
        self.assertTrue(self._is_negated("不存在 `a.gd` 这个文件", "a.gd"))

    def test_negation_after_the_path(self):
        self.assertTrue(self._is_negated("`a.gd` 不存在；", "a.gd"))

    def test_semicolon_separates_clauses(self):
        """核心：否定在上一子句，不影响下一子句。"""
        text = "不存在 `a.gd` 这个文件；实现见 `b.gd:1`。"
        self.assertTrue(self._is_negated(text, "a.gd"))
        self.assertFalse(self._is_negated(text, "b.gd"),
                         "子句边界没生效 —— 「不存在」跨句污染了下一条")

    def test_period_also_separates(self):
        text = "`a.gd` 读不到。实现见 `b.gd:1`。"
        self.assertTrue(self._is_negated(text, "a.gd"))
        self.assertFalse(self._is_negated(text, "b.gd"))

    def test_plain_reference_is_not_negated(self):
        self.assertFalse(self._is_negated("实现见 `b.gd:1`。", "b.gd"))


if __name__ == "__main__":
    unittest.main()
