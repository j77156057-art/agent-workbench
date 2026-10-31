# -*- coding: utf-8 -*-
"""`test/pass` 回执只认退出码 0。

为什么单独钉：这条回执是记忆晋升门唯一信任的"通过"凭证（`promote.evaluate` 的
`_has_pass_receipt`）。而它原先取自 `_tool_ok`，也就是 `_is_failure` 的反面 —— 那个
判据为了不把 `grep` 无匹配当失败（误判会喂 3 次熔断），带着一批"无匹配"白名单，
于是**根本没跑起来的 pytest** 也能被判成非失败：

    [exit code 4] ERROR: file or directory not found: tests/tes.py   -> _is_failure False
    [exit code 2] python: can't open the file ...                    -> _is_failure False
    [exit code 1] · 2.1s
    FAILED tests/test_a.py::x - assert 0 == 1                          -> _is_failure False

最后一条才是**主路径**：`_FAILURE_MARKERS` 只有一串中文工具错误文案，认不出任何
通用测试失败输出，所以一次真失败的 pytest 照样能拿到 `test/pass`。这里把口径拆开，
并且**反向钉住** `_is_failure` 不许被顺手改严 —— 它还得继续放无匹配合规。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402

PYTEST_CACHE_WARN = ("C:/x/_pytest/cacheprovider.py:475: PytestCacheWarning: "
                     "could not create cache path ... [WinError 5] 拒绝访问。")


class TestCommandPassedTests(unittest.TestCase):
    def test_only_exit_zero_is_a_credential(self):
        self.assertTrue(agent_mod._test_command_passed(
            "[exit code 0] · 1.6s\n" + PYTEST_CACHE_WARN + "\n8 passed in 1.6s"))

    def test_never_ran_pytest_is_not_a_credential(self):
        for name, obs in (
            ("exit 4 用法错", "[exit code 4] · 0.9s\nERROR: file or directory not found: tests/tes.py"),
            ("exit 2 打不开文件", "[exit code 2] · 1.1s\npython: can't open the file: No such file or directory"),
            ("exit 1 带 not found", "[exit code 1] · 3s\nTest summary: 2 failures, asset not found in list"),
            ("exit 1 常规失败", "[exit code 1] · 2.1s\nFAILED tests/test_a.py::x - assert 0 == 1"),
        ):
            with self.subTest(case=name):
                self.assertFalse(agent_mod._test_command_passed(obs),
                                 "非零退出码不得产出 test/pass 凭证")

    def test_missing_exit_code_is_no_credential(self):
        """后台化 / 超时 / 被截断 —— 拿不到退出码就按"没有凭证"处理，不猜。"""
        for obs in ("", "  ", "任务已转后台，用 dev_job_logs 取增量日志",
                    "[exit code 未提供]", "命令超时（300s）已终止"):
            with self.subTest(obs=obs[:18]):
                self.assertFalse(agent_mod._test_command_passed(obs))

    def test_failure_judge_cannot_be_the_gate(self):
        """**反向守卫**：`_is_failure` 根本认不出测试失败，所以它不能当凭证门。

        `_FAILURE_MARKERS` 是一串**中文工具错误文案**（"读取失败/拒绝访问/计算失败"…），
        没有任何通用英文失败标记。于是真正的 pytest 失败输出在它眼里是"非失败"：
        这才是那条假 `test/pass` 的**主路径**（不是边角）。也正因为如此，不许把
        这个洞用"往 markers 里加 FAILED/Traceback"的方式补上 —— 那会把 grep 无匹配
        与文件内容里出现的字样一律判成失败，喂给 3 次熔断。退出码才是权威。
        """
        real_pytest_failure = "[exit code 1] · 2.1s\nFAILED tests/test_a.py::x - assert 0 == 1\n1 failed in 2.1s"
        self.assertFalse(agent_mod._is_failure(real_pytest_failure),
                         "判据认不出真实测试失败 —— 正是必须改用退出码门的原因")
        self.assertFalse(agent_mod._test_command_passed(real_pytest_failure),
                         "新门必须拦住它")
        # grep 无匹配仍是合法答案，不能被顺手改严
        self.assertFalse(agent_mod._is_failure("[exit code 1] · 0.4s\n（无匹配）"))
        self.assertFalse(agent_mod._is_failure(
            "[exit code 0] · 1.65s\n" + PYTEST_CACHE_WARN + "\n8 passed"))


class WiringTests(unittest.TestCase):
    def test_both_dispatch_paths_use_the_exit_code_gate(self):
        """两条派发路径（顺序 + 并行批处理）都必须走新门。

        漏一条 = 那条路径上凭证依旧可伪造，而单测全绿。
        """
        source = open(os.path.join(os.path.dirname(os.path.abspath(agent_mod.__file__)),
                                   "agent.py"), encoding="utf-8").read()
        sites = re.findall(r"turn\.test_command_ok = (.+)", source)
        self.assertEqual(len(sites), 2, "测试命令凭证的赋值点应恰好两处")
        for expr in sites:
            with self.subTest(expr=expr.strip()[:40]):
                self.assertEqual(expr.strip(), "_test_command_passed(obs)",
                                 "凭证仍在用 _tool_ok / ok（= _is_failure 的反面）")

    def test_verified_still_requires_file_targets(self):
        turn = agent_mod._trace.Turn(session_id="s", provider="p", model="m", question="q")
        turn.test_command_ok = True
        turn.test_command_targets = {}
        self.assertFalse(agent_mod._test_command_verified(turn),
                         "没有待验证的文件改动，跑绿了也不算本回合有验证证据")


if __name__ == "__main__":
    unittest.main()
