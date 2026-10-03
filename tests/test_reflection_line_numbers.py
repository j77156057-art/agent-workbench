# -*- coding: utf-8 -*-
"""反思判据不得去核验**材料里根本没有的东西**——行号。

## 实测（`wf-2c5612c6ee53`，连续三轮同一句判词）

    「所有 read_file 观察均被截断，未获得完整行号证据」
    「结论中不得声称观察里没有的内容，目前无任何带文件:行号的证据」

而 `read_file` **整文件读取的观察里一个行号都没有**：

    read_file(path: project.godot)                   → === project.godot === + 裸正文
    read_file(path: project.godot, start: 6, end: 8) → 6: config/name="StarVoyager"
    grep(pattern: GRAVITY)                           →     20| const GRAVITY: float = 24.0

任务文本常写「每条结论带 `文件:行号`」，判据据此判「没有行号 → 无证据」——
**可它手里只有裸正文**。

## 后果的结构性

子代理反复重读同一批文件，每轮都败在同一句「无行号证据」，直到耗尽重规划次数。
**那是一条结构上不可能成功的路**——和「任务要写文件却派给只读角色」同一族：
一个注定完成不了的任务被呈现成「模型没做好」。

## 本条判据的取舍（如实说明）

这是**弱化**：原来「缺行号」也能判失败，现在不行。

换来的：判据不再核验材料里不存在的东西，而**防幻觉的那一条原封不动**——
「结论不得声称观察里没有的东西」照样拦得住编造的内容。

所以判据从「形式合规（带行号）」改成「**实质合规（内容确实在观察里出现过）**」。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402

WHOLE_FILE_OBS = (
    "=== project.godot ===\n"
    "; Engine configuration file.\n"
    "config_version=5\n"
    "\n"
    "[application]\n"
    'config/name="StarVoyager"\n'
    'run/main_scene="res://scenes/Main.tscn"\n'
)
RANGE_OBS = (
    "=== project.godot（第 6-8 行，共 42 行）===\n"
    "6: config/name=\"StarVoyager\"\n"
    "7: run/main_scene=\"res://scenes/Main.tscn\"\n"
    "8: config/features=PackedStringArray(\"4.2\")\n"
)
GREP_OBS = "[behaviors/player.gd:L20-L21]\n    20| const GRAVITY: float = 24.0\n"


class LineNumberDetectionTests(unittest.TestCase):
    """判据只认**工具自己写的两种框**，不猜内容。"""

    def test_whole_file_read_is_recognised_as_unnumbered(self):
        self.assertFalse(agent._obs_has_line_numbers(WHOLE_FILE_OBS),
                         "裸正文被判成带行号了")

    def test_range_read_is_recognised_as_numbered(self):
        self.assertTrue(agent._obs_has_line_numbers(RANGE_OBS))

    def test_grep_is_recognised_as_numbered(self):
        self.assertTrue(agent._obs_has_line_numbers(GREP_OBS))

    def test_empty_observation_is_not_numbered(self):
        self.assertFalse(agent._obs_has_line_numbers(""))

    def test_a_start_only_range_is_still_numbered(self):
        """只给 `start:` 时工具会自己补 `end:` 并打抬头（实测 `start: 25` → 第 25-29 行）。"""
        self.assertTrue(agent._obs_has_line_numbers(
            "=== a.gd（第 25-29 行，共 29 行）===\n25: l25\n26: l26\n"))

    # ---- 裸正文上的假阳性：第一版就是栽在这里 ---------------------------

    def test_a_python_dict_literal_is_not_numbered(self):
        self.assertFalse(agent._obs_has_line_numbers(
            '=== t.py ===\nCFG = {\n    1: "one",\n    2: "two",\n}\n'),
            "dict 字面量被判成带行号——判据会回去核验行号")

    def test_a_markdown_year_heading_is_not_numbered(self):
        self.assertFalse(agent._obs_has_line_numbers(
            "=== notes.md ===\n# 项目\n2024: 年度回顾\n2025: 计划\n"),
            "md 的「2024: 年度回顾」被判成带行号")

    def test_yaml_numeric_keys_are_not_numbered(self):
        self.assertFalse(agent._obs_has_line_numbers(
            "=== c.yaml ===\n开关:\n  0: 关\n  1: 开\n"),
            "YAML 数字键被判成带行号——而 YAML/JSON/日志恰恰是常读的")

    def test_a_log_line_is_not_numbered(self):
        self.assertFalse(agent._obs_has_line_numbers(
            "=== run.log ===\n12:34:56 INFO 启动\n12:35:01 INFO 加载完成\n"))

    def test_there_is_no_content_guessing_fallback(self):
        """留一个「整文件读取时逐行判断」的兜底就会把上面三例全部误判。

        所以这里钉死：判定只由两种工具框决定。
        """
        import inspect

        src = inspect.getsource(agent._obs_has_line_numbers)
        self.assertNotIn("_RE_OBS_RANGE_LINE", src,
                         "又加回了逐行猜测的兜底——裸正文会被误判成带行号")


class EvidenceBlockDeclaresItsShapeTests(unittest.TestCase):
    """证据块必须**如实声明**哪些观察带行号——把事实摆在判据眼前。"""

    def test_unnumbered_observations_are_declared(self):
        block = agent._traj_evidence_block([
            {"action": "read_file(path: project.godot)", "obs": WHOLE_FILE_OBS},
            {"action": "read_file(path: behaviors/player.gd)", "obs": WHOLE_FILE_OBS},
        ])
        self.assertIn("都不带行号", block,
                      "没有声明「这几条不带行号」——判据会照着任务里的"
                      "「每条带行号」去核验一份裸正文")
        self.assertIn("裸正文", block)

    def test_numbered_observations_are_declared(self):
        block = agent._traj_evidence_block([
            {"action": "read_file(path: project.godot, start: 6, end: 8)",
             "obs": RANGE_OBS},
            {"action": "grep(pattern: GRAVITY)", "obs": GREP_OBS},
        ])
        self.assertIn("都带行号", block)

    def test_mixed_observations_report_both(self):
        block = agent._traj_evidence_block([
            {"action": "read_file(path: project.godot)", "obs": WHOLE_FILE_OBS},
            {"action": "grep(pattern: GRAVITY)", "obs": GREP_OBS},
        ])
        self.assertIn("只有 1 条带行号", block)

    def test_the_content_is_still_there(self):
        """声明形状不能挤掉内容——那才是判据要的证据。"""
        block = agent._traj_evidence_block([
            {"action": "read_file(path: project.godot)", "obs": WHOLE_FILE_OBS},
        ])
        self.assertIn('config/name="StarVoyager"', block)
        self.assertIn("read_file(path: project.godot)", block)

    def test_the_declaration_survives_the_budget_cut(self):
        """块被 1600 预算截断时，表头可能被切掉——那正是最需要它的时候。

        截断取的是**尾部**，所以表头必须单独处理，不能混在会被切掉的正文里。
        """
        long_steps = [{"action": "read_file(path: f%d.gd)" % i,
                      "obs": "=== f%d.gd ===\n" % i + ("x" * 400)}
                     for i in range(6)]
        block = agent._traj_evidence_block(long_steps, limit=300)
        self.assertIn("不带行号", block,
                      "预算把「本块不带行号」这句切掉了——判据又只能靠猜")
        self.assertIn("f5.gd", block, "正文也得保住——那才是判据要的证据")


class ReflectionPromptSaysLineNumbersAreNotTheCriterionTests(unittest.TestCase):
    """反思提示必须讲清行号不是默认判据。"""

    def setUp(self):
        import inspect
        src = inspect.getsource(agent._reflection_result)
        self.src = src

    def test_it_tells_the_judge_line_numbers_are_conditional(self):
        self.assertIn("行号不是默认判据", self.src)

    def test_it_explains_the_read_file_shape(self):
        """判据得知道整文件读取是裸正文——否则它凭什么不核验行号。"""
        self.assertIn("裸正文", self.src)

    def test_the_exemption_sentence_is_written_only_once(self):
        """那句话写在两处就会漂——抽成常量，两处共用，测试断言这个常量。"""
        self.assertIn("_LINE_NUMBER_EXEMPTION", self.src)
        import inspect
        from agent import _LINE_NUMBER_EXEMPTION
        block_src = inspect.getsource(agent._traj_evidence_block)
        self.assertIn("_LINE_NUMBER_EXEMPTION", block_src,
                      "证据块没用那个常量，两处会各自漂")
        self.assertTrue(_LINE_NUMBER_EXEMPTION.strip().endswith("。"))

    def test_the_anti_hallucination_clause_is_kept(self):
        """这是本次**唯一不能动**的那一条：声称观察里没有的东西照样是缺陷。"""
        self.assertIn("不得声称观察里没有的东西", self.src)

    def test_the_evidence_still_governs(self):
        self.assertIn("证据以「工具观察」为准", self.src)


if __name__ == "__main__":
    unittest.main()