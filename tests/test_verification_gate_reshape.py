# -*- coding: utf-8 -*-
"""按 Claude Code 的形状重做核验：事实项当闸门，过程项不当，结论三值化。

## 依据（来自 Claude Code 泄露源码的 verificationAgent.ts）

它的验证 prompt 把"读过"**明确排除**在证据之外：

    - "The code looks correct based on my reading" — reading is not verification. Run it.
    - "The implementer's tests already pass" — the implementer is an LLM. Verify independently.
    Test suite results are context, not evidence.

它还把一种失败模式**命名**了：

    verification avoidance: when faced with a check, you find reasons not to run it —
    you read code, narrate what you would test, write "PASS," and move on.

而它的三值判定带一条明确的防逃逸约束：

    PARTIAL is for environmental limitations only ... not for "I'm unsure whether this is
    a bug." If you can run the check, you must decide PASS or FAIL.

## DocMind 这边的事实

Q2（"这一轮有没有调 read_file"）作为闸门已连续**四次**误报：并行读取看不见、
完整读取看不见、连上了还被路径切分错误连累。而 Q1（路径能否打开、行号是否越界）
是纯文件系统判定，至今零误报，且拦的是真错误。

所以：**闸门只看 Q1；Q2 降级为参考；结论三值化；再把"验证躲避"写进提示。**
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
from agent_runtime.output_audit import audit_evidence  # noqa: E402

PROJECT = r"D:\WorkBuddy\godot_sample"
HAVE_PROJECT = os.path.isdir(PROJECT)

READ = [{"action": "read_file", "ok": True, "input": "behaviors/enemy.gd"}]
ANSWER = "扣多少看 behaviors/enemy.gd:7，实际改血在 behaviors/player.gd:39。"
QUESTION = "玩家受伤扣多少血在哪算的？"


class GateIgnoresTheProcessCheckTests(unittest.TestCase):
    """Q2 不得再驱动闸门。"""

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_no_read_record_but_valid_refs_passes_the_gate(self):
        """没有 read_file 记录、但引用全部可核验 -> 门禁必须放行。

        这正是本次会话反复误报的场景：模型读了文件、harness 没看见。
        """
        out = audit_evidence({"text": ANSWER, "trace": {"steps": []}},
                             question=QUESTION, code_root=PROJECT)
        self.assertTrue(out["gate_ok"],
                        "仅因为没有 read_file 记录就拦住答案——这是过程指标当闸门，"
                        "本次会话已因此误报四次")
        self.assertFalse(out["checks"]["read_file_present"],
                         "用例前提不成立：read_file_present 本应是 False")

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_verdict_is_partial_not_fail_in_that_case(self):
        out = audit_evidence({"text": ANSWER, "trace": {"steps": []}},
                             question=QUESTION, code_root=PROJECT)
        self.assertEqual(out["verdict"], "PARTIAL",
                         "引用可核验、只是缺旁证 -> PARTIAL，不是 FAIL")

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_ok_still_requires_the_process_check_for_legacy_callers(self):
        """`ok` 保留旧语义，避免悄悄改变其它调用方的行为。"""
        out = audit_evidence({"text": ANSWER, "trace": {"steps": []}},
                             question=QUESTION, code_root=PROJECT)
        self.assertFalse(out["ok"],
                         "`ok` 应保持旧语义（含过程项），新逻辑一律走 gate_ok")


class GateStillBlocksRealErrorsTests(unittest.TestCase):
    """Q1 必须仍然是硬门禁——放松过程项不等于放松事实项。"""

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_an_unopenable_reference_fails_the_gate(self):
        out = audit_evidence({"text": "显示在 hud.gd:20 处理。", "trace": {"steps": READ}},
                             question="HUD 在哪刷新？", code_root=PROJECT)
        self.assertFalse(out["gate_ok"], "打不开的引用竟放行了——那是幻觉路径")
        self.assertEqual(out["verdict"], "FAIL",
                         "能查且查出了问题 -> FAIL。PARTIAL 只给环境限制，"
                         "不得用作「我不确定」的逃逸口")

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_an_out_of_range_line_fails_the_gate(self):
        out = audit_evidence({"text": "数值在 behaviors/enemy.gd:999。", "trace": {"steps": READ}},
                             question="扣血在哪算？", code_root=PROJECT)
        self.assertFalse(out["gate_ok"], "越界行号放行了——那是幻觉行号")
        self.assertEqual(out["verdict"], "FAIL")

    @unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
    def test_no_reference_at_all_fails_the_gate(self):
        out = audit_evidence({"text": "扣血逻辑在角色控制器里。", "trace": {"steps": READ}},
                             question=QUESTION, code_root=PROJECT)
        self.assertFalse(out["gate_ok"], "定位类问题却没有任何文件引用——必须拦住")


class VerdictRenderingTests(unittest.TestCase):
    """用户可见结论：给事实，不给恐吓。"""

    def _audit(self, **kw):
        base = {"checks": {}, "references": [], "verdict": "PASS",
                "required": True, "gate_ok": True, "gate_checks": {}}
        base.update(kw)
        return agent_mod._evidence_verdict_note(base)

    def test_pass_names_the_count(self):
        refs = [{"path": "a.gd", "start_line": 7, "exists": True, "in_root": True, "line_ok": True},
                {"path": "b.gd", "start_line": 9, "exists": True, "in_root": True, "line_ok": True}]
        note = self._audit(verdict="PASS", references=refs)
        self.assertIn("PASS", note)
        self.assertIn("2 处引用可核验", note)

    def test_fail_names_the_bad_references(self):
        refs = [{"path": "a.gd", "start_line": 7, "exists": True, "in_root": True, "line_ok": True},
                {"path": "hud.gd", "start_line": 20, "exists": False, "in_root": True, "line_ok": False}]
        note = self._audit(verdict="FAIL", gate_ok=False, references=refs)
        self.assertIn("FAIL", note)
        self.assertIn("hud.gd:20", note, "必须点名未通过的引用")
        self.assertIn("路径打不开", note, "要说清是哪一类问题")

    def test_partial_is_labelled_as_an_environment_limit(self):
        note = self._audit(verdict="PARTIAL", references=[])
        self.assertIn("环境限制", note,
                      "PARTIAL 必须标明是环境限制，否则它会退化成「我不确定」")

    def test_the_old_scary_wording_is_gone(self):
        """旧文案把原因说错（是没读，不是路径/行号问题），且已被证明会误导。

        断言只针对**真正被发出的那句**——``agent.py`` 里还留着解释这次改动的注释，
        它们提到旧文案是合理的，不该被这条断言算成回归。
        """
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertNotIn("（证据复核：以上文件/行号", src,
                         "旧的恐吓文案还在被发出——它断言的原因（没读过）常常不成立")
        self.assertNotIn("请勿据此修改代码。）", src,
                         "旧的恐吓文案还在被发出")


class VerificationAvoidanceIsNamedTests(unittest.TestCase):
    """把"验证躲避"这个已命名的失败模式写进系统提示。"""

    def test_the_system_prompt_names_it(self):
        import agent as A
        self.assertIn("验证躲避", A.SYSTEM_PROMPT,
                      "提示里必须有这条：本次会话的失败全部是「用叙述代替执行」")
        self.assertIn("叙述不算", A.SYSTEM_PROMPT,
                      "必须点明「叙述计划」不等于「执行」——这正是模型做的事")

    def test_the_criteria_is_positive(self):
        """给出可判定的判据，而不只是禁令。"""
        import agent as A
        self.assertIn("你手上有没有那次执行/读取的真实输出", A.SYSTEM_PROMPT,
                      "要有正向判据，否则禁令无法自检")

    def test_it_survives_the_channel_switch(self):
        """两条通道的提示都要有——它不在被替换的契约块里。"""
        from agent import _prompt_for_channel
        for native in (True, False):
            self.assertIn("验证躲避",
                          _prompt_for_channel(agent_mod.SYSTEM_PROMPT, native),
                          "native=%s 时这条丢了" % native)

    def test_telling_the_user_the_truth_is_allowed(self):
        """必须留"拿不到就如实说"的口子，否则会逼出编造。"""
        import agent as A
        self.assertIn("我没核实", A.SYSTEM_PROMPT,
                      "必须允许如实声明未核实——否则模型只能假装确认过")


if __name__ == "__main__":
    unittest.main()