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

## Agent 工作台 这边的事实

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
                "verdict_reason": "", "required": True,
                "gate_ok": True, "gate_checks": {}}
        base.update(kw)
        return agent_mod._evidence_verdict_note(base)

    def test_pass_names_the_count(self):
        refs = [{"path": "a.gd", "start_line": 7, "exists": True, "in_root": True, "line_ok": True},
                {"path": "b.gd", "start_line": 9, "exists": True, "in_root": True, "line_ok": True}]
        note = self._audit(verdict="PASS", references=refs)
        self.assertIn("PASS", note)
        self.assertIn("2 处引用可核验", note)

    def test_pass_must_not_tell_the_user_to_double_check(self):
        """全部核实通过却仍让用户逐个复查，正好抵消了给出正向信号的意义。

        之前这句是无条件拼在后面的：PASS 的输出是
        「（核验：PASS · 3 处引用可核验）未核实的引用请自行打开对应文件确认…」。
        """
        refs = [{"path": "a.gd", "start_line": 7, "exists": True, "in_root": True, "line_ok": True}]
        note = self._audit(verdict="PASS", references=refs)
        self.assertNotIn("未核实的引用", note,
                         "PASS 时仍要求用户自行复查——那等于没给出任何正向信号")

    def test_agent_loop_keeps_the_nudge_but_does_not_stamp_a_verdict(self):
        """「留地板、拆其余」：核验通过/失败都不再往用户答案上盖横幅。

        判据函数 `_evidence_verdict_note` 本身保留、由本类其它用例直测；但 `_run` 已
        刻意不再调用它去追加「（核验：PASS·N 处…）／FAIL…」横幅——那是被拆掉的
        "事后盖章"。这里钉的是**当前契约**：一次性证据纠偏闸门仍在，且 PASS/FAIL
        横幅不由 `_run` 追加，防止"处处受限"式盖章悄悄长回来。
        """
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn("if evidence_nudges < 1:", src,
                      "一次性证据纠偏闸门必须仍在（内部 re-ask，不盖章）")
        self.assertNotIn(
            'if evidence_audit.get("required") and evidence_audit.get("references"):', src,
            "逐次盖章分支应已随「留地板、拆其余」拆除")
        self.assertEqual(
            src.count("_evidence_verdict_note(evidence_audit)"), 0,
            "PASS/FAIL 横幅已不再由 _run 追加；>0 说明「事后盖章」又回来了")

    def test_fail_names_the_bad_references(self):
        refs = [{"path": "a.gd", "start_line": 7, "exists": True, "in_root": True, "line_ok": True},
                {"path": "hud.gd", "start_line": 20, "exists": False, "in_root": True, "line_ok": False}]
        note = self._audit(verdict="FAIL", gate_ok=False, references=refs)
        self.assertIn("FAIL", note)
        self.assertIn("hud.gd:20", note, "必须点名未通过的引用")
        self.assertIn("路径打不开", note, "要说清是哪一类问题")

    def test_partial_is_labelled_by_its_reason_not_by_a_single_phrase(self):
        """PARTIAL 有两种含义，不能共用一句话。

        早先的断言写的是「PARTIAL 必须标明是环境限制」——那把 bug 编码进去了：
        "引用已核实通过、只是缺读取记录"根本不是环境限制。
        """
        no_root = self._audit(verdict="PARTIAL", verdict_reason="no_root", references=[])
        self.assertIn("未检查", no_root,
                      "无法核验时必须说「未检查」，而不是笼统一句 PARTIAL")

        no_read = self._audit(verdict="PARTIAL", verdict_reason="no_read_record", references=[])
        self.assertIn("引用已逐一核实通过", no_read,
                      "引用已核实通过时要说清这一层，否则用户以为自己没被检查")
        self.assertNotIn("无法核验", no_read)

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


@unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
class PartialMeansTwoDifferentThingsTests(unittest.TestCase):
    """两种 PARTIAL 的含义完全不同，措辞必须分开。

    早先两者共用「环境限制，无法核验」一句话，于是第二种明明核实过了却被说成
    「无法核验」；更糟的是第一种会把每条引用都列成「未通过（路径打不开）」——
    我们压根没检查过，**凭什么断言路径有问题**。
    """

    def test_no_read_record_says_the_refs_were_checked(self):
        out = audit_evidence({"text": ANSWER, "trace": {"steps": []}},
                             question=QUESTION, code_root=PROJECT)
        self.assertEqual(out["verdict_reason"], "no_read_record")
        note = agent_mod._evidence_verdict_note(out)
        self.assertIn("2 处引用可核验", note)
        self.assertNotIn("无法核验", note,
                         "引用已核实通过，却报成「无法核验」——两回事")

    def test_no_code_root_does_not_claim_the_path_is_bad(self):
        out = audit_evidence({"text": ANSWER, "trace": {"steps": []}},
                             question=QUESTION, code_root="")
        self.assertEqual(out["verdict_reason"], "no_root")
        note = agent_mod._evidence_verdict_note(out)
        self.assertIn("未检查", note)
        self.assertNotIn("路径打不开", note,
                         "没有代码根时我们从未检查过路径，断言它打不开是在说不知道的事")
        self.assertNotIn("未核实的引用请自行打开", note,
                         "无法核验 ≠ 引用有问题，不该让用户去逐个自查")

    def test_every_verdict_reason_is_carried_through(self):
        """新增分支时若忘了带 reason，措辞会退回默认那套。"""
        for steps, root, expected in (
            (READ, PROJECT, ""),          # PASS
            ([], PROJECT, "no_read_record"),
            (READ, "", "no_root"),
        ):
            out = audit_evidence({"text": ANSWER, "trace": {"steps": steps}},
                                 question=QUESTION, code_root=root)
            self.assertEqual(out.get("verdict_reason"), expected,
                             "root=%r steps=%r 的 reason 不对" % (root, len(steps)))


class RemedyMatchesTheActualProblemTests(unittest.TestCase):
    """补救方式必须对准**真正的问题**，不能一律叫它"再读一遍"。

    实测：模型把引用写成裸文件名 `enemy.gd` / `player.gd` / `hud.gd`
    （真实路径在 `behaviors/`、`ui/` 下），闸门判 FAIL 是对的。但纠偏文案一律说
    "调用 read_file 读取候选文件的相关行"，于是它把**已经完整读过的三个文件又读
    了一遍**——7 次额外调用、44 秒。问题在引用写法，不在读得不够。
    """

    def _audit(self, failed, read_present=True):
        return {"gate_checks": dict(failed, location_question=True),
                "checks": {"read_file_present": read_present,
                           "location_question": True},
                "references": [], "verdict": "FAIL"}

    def test_a_bad_path_does_not_ask_for_a_reread(self):
        remedy = agent_mod._evidence_remedy(
            self._audit({"references_exist": False}, read_present=True), native_mode=True)
        self.assertIn("相对代码根目录的完整相对路径", remedy)
        self.assertIn("不需要重新读文件", remedy,
                      "引用写法错了却让它重新读——它手上已经有原文了")
        self.assertNotIn("现在直接调用 read_file", remedy)

    def test_only_when_nothing_was_read_does_it_suggest_reading(self):
        remedy = agent_mod._evidence_remedy(
            self._audit({"references_exist": False}, read_present=False), native_mode=True)
        self.assertIn("现在直接调用 read_file", remedy,
                      "本轮确实没读过原文时，必须让它去读")

    def test_an_out_of_range_line_says_fix_the_number(self):
        remedy = agent_mod._evidence_remedy(
            self._audit({"reference_lines_valid": False}, read_present=True), native_mode=True)
        self.assertIn("修正行号", remedy)
        self.assertNotIn("现在直接调用 read_file", remedy)

    def test_a_missing_reference_says_give_one(self):
        remedy = agent_mod._evidence_remedy(
            self._audit({"reference_present": False}, read_present=True), native_mode=True)
        self.assertIn("文件:行号", remedy)

    def test_the_text_protocol_tail_is_kept(self):
        remedy = agent_mod._evidence_remedy(
            self._audit({"references_exist": False}, read_present=True), native_mode=False)
        self.assertIn("Final Answer", remedy,
                      "文本协议下仍需说明用标记收尾——两套通道文案必须都可用")

    def test_an_empty_audit_does_not_produce_an_empty_remedy(self):
        remedy = agent_mod._evidence_remedy({}, native_mode=True)
        self.assertTrue(remedy.strip())
        self.assertIn("怎么改", remedy)


@unittest.skipUnless(HAVE_PROJECT, "需要真实项目")
class UnopenableFileMustNotAlsoBeReportedAsBadLinesTests(unittest.TestCase):
    """打不开的文件**不能**同时被报成「行号越界」。

    我们压根没打开那个文件、没数过行数，所以「行号超出了文件实际行数」是一句
    我们不知道真不真的断言，而它给出的建议（按原文修正行号）模型也无法执行。
    实测就是踩到这条：模型写裸文件名 `enemy.gd` 时，两条失败同时出现。
    """

    def test_only_the_path_problem_is_reported(self):
        out = audit_evidence({"text": "数值在 enemy.gd:7。", "trace": {"steps": READ}},
                             question=QUESTION, code_root=PROJECT)
        self.assertFalse(out["gate_checks"]["references_exist"],
                         "用例前提不成立：裸文件名本应打不开")
        self.assertTrue(
            out["gate_checks"]["reference_lines_valid"],
            "打不开的文件被同时报成「行号越界」——我们从未数过行数，"
            "这条建议模型也无法执行")

    def test_the_remedy_does_not_talk_about_line_numbers(self):
        out = audit_evidence({"text": "数值在 enemy.gd:7。", "trace": {"steps": READ}},
                             question=QUESTION, code_root=PROJECT)
        detail, _ = agent_mod._evidence_failure_detail(out)
        self.assertNotIn("行号超出", detail,
                         "纠偏文案里出现了从未验证过的行号问题")
        self.assertIn("打不开", detail)

    def test_a_real_out_of_range_line_is_still_caught(self):
        """打不开 vs 真的越界，两者必须仍能区分开。"""
        out = audit_evidence({"text": "数值在 behaviors/enemy.gd:999。",
                              "trace": {"steps": READ}},
                             question=QUESTION, code_root=PROJECT)
        self.assertTrue(out["gate_checks"]["references_exist"])
        self.assertFalse(out["gate_checks"]["reference_lines_valid"],
                         "文件能打开、行号 999 越界——必须照常报出来")


if __name__ == "__main__":
    unittest.main()