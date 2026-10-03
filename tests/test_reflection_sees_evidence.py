# -*- coding: utf-8 -*-
"""反思判据必须**看得见工具观察**，而不是只数结论复述了多少。

## 实测故障（真实工作流 trace，`wf-f3da9d3ffb32`）

三次任务都做出了正确的字节级写入。`python_exec` 的观察里全是硬凭据：

    WRITE_OK: True
    actual == after: True
    bom_preserved: True
    LF_offsets_identical: True
    all_diff_offsets_vs_before: [121] | bytes: [(121, b'2', b'5')]

而判据给回的是：

    子代理反思未通过：…但**结论被截断**，未展示写后重新读取 actual、
    actual == after、BOM 与全部换行位置和类型…，无法确认已实际写回

**同一件事连错三次**，逼出两轮 replan 把同一件事重做三遍。

## 根因（不是模型的问题，是判据看不见）

`_reflection_result` 把 `traj` 传进来了，但prompt 里只有：

    步骤数：{len(traj)}；失败步骤：{failed_steps}

**工具观察一个字都没进prompt。** 于是判据在结构上只能问
「结论有没有把这些复述一遍」——它压根不知道观察里有什么。

这与 §9 记的「判据可以结构上永远为假，比写反更难发现」同族：
不是判据写错了，是判据**拿不到它要判的东西**。

## 修法

把观察压成有界证据块喂进去，并把判据的职责从「复述了吗」改成
「**结论有没有声称观察里不存在的东西**」。

§9 另一条教训也照做：证据块取**尾部**（子代理的最终写盘验证总在最后几步），
且截断时**如实标注**，不假装给全了——否则判据又会在这段看不到的地方上
做出「没有证据」的结论。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402
from agent_runtime import tools as agent_runtime_tools  # noqa: E402

#: 一次真实成功写入的观察（形状照抄 wf-f3da9d3ffb32 的 trace，不是编的）
REAL_TRAJ = [
    {"action": "list_dir(path: .)", "obs": "behaviors  scripts  ui  values", "ok": True},
    {"action": "grep(pattern: HP_GROWTH)",
     "obs": "values\\level.gd:5: const HP_GROWTH: float = 0.12", "ok": True},
    {"action": "python_exec(...)",
     "obs": "WRITE_OK: True\nactual == after: True\nbom_preserved: True\n"
            "LF_offsets_identical: True\nall_diff_offsets_vs_before: [121] | "
            "bytes: [(121, b'2', b'5')]", "ok": True},
]


class StageCwdReportTests(unittest.TestCase):
    """诊断那一行必须**判得对**——第一版把试做区里的路径判成了"不在"。

    写死 `config.STATE_ROOT` 是错的：生产的 state_root 是 `<repo>/.docmind`，
    测试里是临时目录，而 `project_stage.create_stage` 用的是
    `Path(state_root)/"project_stages"/<wid>`。所以按**路径分段**判。
    """

    def test_a_stage_workspace_path_counts_as_inside(self):
        text = agent_runtime_tools.stage_cwd_report(
            r"D:\somewhere\.docmind\project_stages\wf-1\workspace")
        self.assertNotIn("不在试做区内", text)

    def test_the_real_project_counts_as_outside(self):
        text = agent_runtime_tools.stage_cwd_report(r"D:\WorkBuddy\godot_sample")
        self.assertIn("不在试做区内", text)

    def test_it_does_not_depend_on_where_state_root_is(self):
        """同一个 stage 路径换个基址也得判成在内。"""
        for base in (r"C:\tmp\xyz", r"D:\WorkBuddy\rag-agent\.docmind",
                     r"/home/u/.docmind"):
            text = agent_runtime_tools.stage_cwd_report(
                os.path.join(base, "project_stages", "wf-9", "workspace"))
            self.assertNotIn("不在试做区内", text,
                             "基址是 %s 时判错了" % base)


class TrajEvidenceBlockTests(unittest.TestCase):
    """证据块本身：给得够、说得清边界。"""

    def test_it_carries_the_actual_observations(self):
        block = agent._traj_evidence_block(REAL_TRAJ)
        for needle in ("WRITE_OK: True", "bom_preserved: True",
                       "all_diff_offsets_vs_before: [121]",
                       "grep(pattern: HP_GROWTH)"):
            self.assertIn(needle, block,
                          "证据块里没有 %r——判据还是看不见它" % needle)

    def test_an_empty_traj_says_so_instead_of_looking_like_missing_evidence(self):
        block = agent._traj_evidence_block([])
        self.assertIn("无", block)
        self.assertNotIn("未提供", block,
                         "空轨迹不该说成「截断」，那会让判据误以为还有没给的证据")

    def test_truncation_is_declared_not_hidden(self):
        """§9：没给全就必须说没给全，否则判据会在看不见的那段上下错结论。"""
        long = [{"action": "s%d" % i, "obs": "x" * 300, "ok": True} for i in range(30)]
        block = agent._traj_evidence_block(long)
        self.assertIn("未提供", block)
        self.assertIn("最后", block)

    def test_it_takes_the_tail_where_the_write_verification_lives(self):
        long = [{"action": "s%d" % i, "obs": "obs%d" % i, "ok": True}
                for i in range(30)]
        block = agent._traj_evidence_block(long)
        self.assertIn("obs29", block, "尾部没被保留——最终写盘验证就在那里")
        self.assertNotIn("obs0 ", block)

    def test_it_is_bounded(self):
        long = [{"action": "s%d" % i, "obs": "y" * 500, "ok": True} for i in range(60)]
        self.assertLessEqual(len(agent._traj_evidence_block(long)), 2000,
                             "证据块无界——它会被塞进每次反思的 prompt")

    def test_failed_steps_are_marked(self):
        block = agent._traj_evidence_block([
            {"action": "grep(pattern: x)", "obs": "文件不存在", "ok": False}])
        self.assertIn("失败", block)


class ReflectionSeesEvidenceTests(unittest.TestCase):
    """判据的 prompt 必须真的带上观察。"""

    def _prompt(self, traj, conclusion="已完成。"):
        captured = {}

        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, messages, stream=True, **kw):
                captured["prompt"] = messages[-1]["content"]
                return '{"ok": true, "issues": [], "next_step": ""}'

            def count_tokens(self, text):
                return 1

        agent._reflection_result(_LLM(), role="coder", task="改一个常量",
                                 conclusion=conclusion, traj=traj, context={})
        return captured["prompt"]

    def test_the_observations_reach_the_prompt(self):
        prompt = self._prompt(REAL_TRAJ)
        self.assertIn("WRITE_OK: True", prompt,
                      "工具观察没进prompt——判据只能判「结论复述了吗」")
        self.assertIn("bom_preserved: True", prompt)

    def test_the_criterion_is_restated_as_against_observations(self):
        prompt = self._prompt(REAL_TRAJ)
        self.assertIn("工具观察", prompt)
        self.assertIn("不得声称观察里没有的东西", prompt,
                      "判据的职责必须从「复述了吗」改成「有没有超出观察」")

    def test_it_does_not_ask_for_verbatim_restatement_anymore(self):
        """旧判据的实际判据就是「结论有没有逐条复述」，必须明确取消。"""
        prompt = self._prompt(REAL_TRAJ)
        self.assertNotIn("结论是否有证据：", prompt)
        self.assertIn("不必逐字复述", prompt)


class DeterministicFallbackTests(unittest.TestCase):
    """LLM 判据不可用时的确定性兜底也不能变松。"""

    def test_no_conclusion_is_still_not_ok(self):
        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, *a, **kw):
                raise RuntimeError("不可用")

            def count_tokens(self, text):
                return 1

        out = agent._reflection_result(_LLM(), role="coder", task="t",
                                       conclusion="   ", traj=REAL_TRAJ, context={})
        self.assertFalse(out["ok"])
        self.assertIn("没有最终结论", out["issues"])

    def test_failed_tool_steps_still_block_ok(self):
        class _LLM:
            provider = "fake"
            model = "reflector"
            last_usage = {}

            def chat(self, *a, **kw):
                raise RuntimeError("不可用")

            def count_tokens(self, text):
                return 1

        out = agent._reflection_result(
            _LLM(), role="coder", task="t", conclusion="做完了",
            traj=[{"action": "grep(pattern: x)", "obs": "文件不存在", "ok": False}],
            context={})
        self.assertFalse(out["ok"], "有失败工具步骤却判通过——兜底被放松了")


if __name__ == "__main__":
    unittest.main()