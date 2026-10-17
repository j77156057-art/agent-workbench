# -*- coding: utf-8 -*-
"""④之②「确定性计数自检」审计门的窄触发语义（候选修法甲：内部矛盾，硬 FAIL）。

背景（grep 40 截断那轮"准确总数"翻车的度量补强）：模型曾给 godot_sample 的函数数
报"准确总数 194"（真值 196），且分项自相矛盾。工具层的静默截断已由 grep/ingest 如实
交代堵住；这里补的是**度量层**——纯文本里能**无外部真值**判定的一类过度声称：模型给一张
`名称: 数字` 分项表 + 一个总数，两者加总对不上。

设计分寸（与 ③ 同源，用户选「内部矛盾」口径）：
  · **判不了就不进 checks**——抽取器只在结构无歧义时下结论：恰有一个总数、≥3 个独立成行的
    `名称: 数字` 分项、label 去重、且不是"总数词"本身；任一含糊返回 None、门禁不碰它；
  · **跨口径防守**：分项里只要出现像文件路径/带代码扩展名的 label（`enemy.gd: 30` 这类
    逐文件行数表），整体不判定——它们彼此不求和、与总数不同口径，硬对就是误伤；
  · **覆盖边界如实说明**：若分项本身就加总等于它写的总数（现场 194=自洽那种），内部无矛盾，
    判 consistent=True 放行——"每项各自漏计但总数自洽"是外部真值问题，审计侧拿不到全仓，
    硬 FAIL 就是误伤（本模块信条：越诚实越 FAIL 是事故）；
  · 一条"总数≠分项加总"是内容错误，绝不被"无代码根"降级成 PARTIAL。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.output_audit import audit_evidence  # noqa: E402


def _audit(text, **kw):
    """隔离出计数门：required 关掉（require_read=False），正文里无 file:line 引用，
    于是 gate 里唯一可能变红的就是 count_consistent。"""
    kw.setdefault("require_read", False)
    return audit_evidence({"text": text}, **kw)


class CountConsistencyTests(unittest.TestCase):
    # ---- 现场形态：声称总数 ≠ 自己列出的分项加总 -> 判红 --------------------
    def test_internal_mismatch_fails_the_gate(self):
        text = (
            "下面按模块统计函数数量。\n"
            "migration_fallback: 9\n"
            "release_verifier: 29\n"
            "game_logic: 12\n"
            "准确总数: 194\n")
        r = _audit(text)
        self.assertIn("count_consistent", r["checks"])
        self.assertFalse(r["checks"]["count_consistent"])
        self.assertFalse(r["gate_checks"]["count_consistent"])
        self.assertEqual(r["verdict"], "FAIL")
        cc = r["count_conflict"]
        self.assertEqual(cc["total"], 194)
        self.assertEqual(cc["sum"], 50)          # 9+29+12
        self.assertEqual(len(cc["items"]), 3)
        self.assertFalse(cc["consistent"])

    # ---- 分项与总数自洽 -> consistent=True，门禁放行（不是 FAIL）----------
    def test_consistent_breakdown_passes(self):
        text = (
            "combat: 12\n"
            "ui: 30\n"
            "audio: 8\n"
            "总数: 50\n")
        r = _audit(text)
        self.assertIn("count_consistent", r["checks"])
        self.assertTrue(r["checks"]["count_consistent"])
        self.assertEqual(r["verdict"], "PASS")

    # ---- 只有现场 194 那种"分项各自漏计但加总=声称总数"：内部无矛盾，放行 --
    def test_self_consistent_but_wrong_is_not_judged_bad(self):
        # 12+31+... 故意让它加总等于总数：审计点无法知道每项是否真的漏计（外部真值），
        # 因此绝不能 FAIL——否则就是误伤如实的自洽陈述。
        text = (
            "A组: 12\n"
            "B组: 31\n"
            "C组: 151\n"
            "准确总数: 194\n")
        r = _audit(text)
        self.assertIn("count_consistent", r["checks"])
        self.assertTrue(r["checks"]["count_consistent"])
        self.assertEqual(r["verdict"], "PASS")

    # ---- 无总数 -> 不判定，键根本不进 checks（零回归面）--------------------
    def test_no_total_is_not_judged(self):
        text = "combat: 12\nui: 30\naudio: 8\n"
        r = _audit(text)
        self.assertNotIn("count_consistent", r["checks"])
        self.assertIsNone(r["count_conflict"])

    # ---- 分项不足 3 条 -> 不构成"一张表"，不判定 ----------------------------
    def test_fewer_than_three_items_is_not_judged(self):
        text = "combat: 12\nui: 30\n总数: 42\n"
        r = _audit(text)
        self.assertNotIn("count_consistent", r["checks"])

    # ---- 出现两个不同总数 -> 分不清谁是总计，不判定 ------------------------
    def test_ambiguous_totals_are_not_judged(self):
        text = ("combat: 12\nui: 30\naudio: 8\n"
                "总数: 50\n备注里的另一个总数: 44\n")
        r = _audit(text)
        self.assertNotIn("count_consistent", r["checks"])

    # ---- 跨口径防守：总数配的是逐文件行数表 -> 整体不判定 ------------------
    def test_file_attribute_table_is_not_judged(self):
        text = ("函数总数: 194\n"
                "enemy.gd: 30\n"
                "player.gd: 45\n"
                "ui/hud.gd: 20\n")
        r = _audit(text)
        self.assertNotIn("count_consistent", r["checks"])
        self.assertIsNone(r["count_conflict"],
                          "逐文件属性表（label 像路径/带扩展名）不得拿去核对总数")

    # ---- 散文里的零散数字 -> 不判定 ----------------------------------------
    def test_prose_numbers_are_not_judged(self):
        text = "项目里大概有 40 个函数，主要集中在战斗与 UI 两块。"
        r = _audit(text)
        self.assertNotIn("count_consistent", r["checks"])

    # ---- 普通定位答案完全不受影响（既有 PASS/FAIL 语义零回归）-------------
    def test_plain_answer_untouched(self):
        r = _audit("可以修改，但请先说明目标文件。")
        self.assertNotIn("count_consistent", r["checks"])
        self.assertTrue(r["ok"])

    # ---- 保守边界：分项里混进 file:line 引用行 -> 口径不可信，整体不判定 --
    def test_breakdown_mixed_with_fileref_is_not_judged(self):
        # 这是有意为之：带代码引用（pathish label）时，"名称: 数字" 既可能是求和分项、
        # 也可能是行号/属性，无法无歧义区分——按「判不了就不判」的信条，宁可不碰门禁。
        text = ("combat: 12\nui: 30\naudio: 8\n总数: 55\n"
                "参见 behaviors/enemy.gd:1")
        r = _audit(text, require_read=True,
                   question="敌人的巡逻逻辑在哪个文件？", code_root="")
        self.assertNotIn("count_consistent", r["checks"])
        self.assertIsNone(r["count_conflict"])

    # ---- 纯类别分项的矛盾仍判红（不依赖 root、与定位门正交）--------------
    def test_pure_category_mismatch_fails_without_location(self):
        text = ("combat: 12\nui: 30\naudio: 8\n总数: 55\n")
        r = _audit(text)
        self.assertFalse(r["gate_checks"]["count_consistent"])
        self.assertEqual(r["verdict"], "FAIL")


class AgentNamesTheConflictTests(unittest.TestCase):
    """agent 侧纠偏必须点名到具体数字，否则模型不知道是哪一处矛盾。"""

    def test_failure_detail_names_total_and_sum(self):
        import agent
        text = ("migration_fallback: 9\nrelease_verifier: 29\n"
                "game_logic: 12\n准确总数: 194\n")
        audit = _audit(text)
        detail, reflection = agent._evidence_failure_detail(audit)
        self.assertIn("194", detail)
        self.assertIn("50", detail)               # 9+29+12 的加总
        self.assertIn("194", reflection)
        self.assertIn("50", reflection)


if __name__ == "__main__":
    unittest.main()
