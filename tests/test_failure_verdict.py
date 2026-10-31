# -*- coding: utf-8 -*-
"""`run_command` 的失败判据改走结构（退出码 + 命令文本），不再靠字符串嗅探。

## 这条洞为什么比 `test/pass` 那条更大

`_FAILURE_MARKERS` 是一串**中文工具错误文案**（"读取失败/拒绝访问/计算失败"…），
里面没有任何通用英文失败标记。于是真跑失败的 pytest 在判据眼里是**非失败**：

    [exit code 1] · 2.1s
    FAILED tests/test_a.py::x - assert 0 == 1          -> _is_failure False

而 `_is_failure` 的反面就是 `_tool_ok`，`_tool_ok` 决定三件事：
1. `turn.failure_count += 1` / `last_failure` → 回合收尾写不写 **L4 error 钩子**；
2. `tool_fail_streak` / `fail_total` → **3 次熔断**与重试配额；
3. `turn.tool_step(ok=…)` → episode 里的 actions 与证据链。

也就是说：真失败的命令既不沉淀成"下次别再这么干"的记忆，也不触发换路重试，
模型看到的还是"这一步成功了"。回执那条（见 test_test_command_receipt_gate.py）
只是这个洞的一个出口。

## 修法的边界（反向守卫同样重要）

非零退出码在**一部分命令上就是答案**：`grep` 查无匹配返回 1、`diff` 有差异返回 1、
`test`/`[` 判假返回 1、`which`/`where`/`dir` 查无此项返回 1。这批必须继续放行 ——
原先"往 markers 加 FAILED/Traceback"之所以是错的修法，正是因为它会把这一批和
"文件内容里恰好含 FAILED 字样"一并判成失败，喂给熔断并给出误导性结论。

所以：**只有拿到工具上下文（`tool="run_command"` + 命令文本）时**才接管；
`_is_failure(obs)` 单参数调用、其它工具、其它调用点逐字节保持原判据。
"""
import inspect
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import agent_memory  # noqa: E402
from tests.test_agent_trace import _IsoBase  # noqa: E402

PYTEST_FAILURE = "[exit code 1] · 2.1s\nFAILED tests/test_a.py::x - assert 0 == 1\n1 failed in 2.1s"
PYTEST_CACHE_WARN = ("C:/x/_pytest/cacheprovider.py:475: PytestCacheWarning: "
                     "could not create cache path ... [WinError 5] 拒绝访问。")


def _run(command, observation):
    """派发点真实会怎么问判据（工具名 + 命令文本一起给）。"""
    return agent_mod._is_failure(observation, "run_command", command)


class CommandVerdictTests(unittest.TestCase):
    def test_real_test_failure_is_now_a_failure(self):
        """四种"命令跑了但没通过"的形状必须全部判失败 —— 这是原洞的主路径。"""
        cases = {
            "pytest 断言失败": ("pytest -q", PYTEST_FAILURE),
            "pytest 用法错（目标文件不存在）": (
                "python -m pytest tests/tes.py -q",
                "[exit code 4] · 0.9s\nERROR: file or directory not found: tests/tes.py"),
            "解释器打不开文件": (
                "python run.py",
                "[exit code 2] · 1.1s\npython: can't open the file: No such file or directory"),
            "构建失败": (
                "npm run build",
                "[exit code 1] · 8.4s\nERROR in ./src/App.tsx\n  Module not found: Error: Can't resolve 'x'"),
        }
        for name, (command, obs) in cases.items():
            with self.subTest(case=name):
                self.assertTrue(_run(command, obs), "真失败仍被判成成功")
                # 判据独立于凭证门：两边现在口径一致
                self.assertFalse(agent_mod._test_command_passed(obs))

    def test_exit_zero_never_a_failure_even_if_output_looks_bad(self):
        """退出码 0 时不许反判失败：pytest 缓存警告、被测程序自己打印的"拒绝访问"
        都是正常输出，判成失败会喂熔断（这条是既有守卫，接管后必须继续成立）。"""
        obs = "[exit code 0] · 1.65s\n" + PYTEST_CACHE_WARN + "\n8 passed in 1.65s"
        self.assertFalse(_run("pytest -q", obs))
        self.assertFalse(_run("python demo.py", "[exit code 0]\n拒绝访问: secret.txt"))

    def test_nonzero_that_is_the_answer_stays_ok(self):
        """非零但「这就是答案」的命令族：1 号码继续放行，且不许计入失败配额。"""
        cases = {
            "grep 无匹配": ("grep -rn TODO src/", "[exit code 1] · 0.4s\n（无匹配）"),
            "rg 无匹配": ("rg -n FIXME .", "[exit code 1] · 0.2s\n"),
            "findstr 无匹配": ("findstr /s TODO *.py", "[exit code 1] · 0.3s\n（命令已执行，无输出）"),
            "diff 有差异": ("diff a.txt b.txt", "[exit code 1] · 0.1s\n2c2\n< old\n> new"),
            "test 判假": ("test -f build/app.exe", "[exit code 1] · 0.1s\n"),
            "which 查无": ("which godot", "[exit code 1] · 0.2s\n"),
            "dir 查无此项": ("dir no_such_dir", "[exit code 1] · 0.2s\nFile Not Found"),
            "git diff --quiet 有改动": ("git diff --quiet", "[exit code 1] · 0.5s\n"),
            "pgrep 查无进程": ("pgrep -f msedge", "[exit code 1] · 0.3s\n（命令已执行，无输出）"),
            "pkill 没杀到东西": ("pkill -f chrome", "[exit code 1] · 0.2s\n"),
            "cd 前缀后的 grep": ("cd backend && grep -c error log.txt",
                                 "[exit code 1] · 0.4s\n0"),
        }
        for name, (command, obs) in cases.items():
            with self.subTest(case=name):
                self.assertFalse(_run(command, obs), "合法答案被当成失败会喂 3 次熔断")

    def test_same_family_code_two_is_a_real_failure(self):
        """只豁免 1 号码：grep 的正则语法错、文件读不了（2）仍是失败。"""
        self.assertTrue(_run("grep -rn '[unclosed' src/",
                             "[exit code 2] · 0.2s\ngrep: Unmatched [, [^"))
        self.assertTrue(_run("diff a.txt missing.txt",
                             "[exit code 2] · 0.1s\ndiff: missing.txt: No such file or directory"))

    def test_command_that_never_started_is_a_failure(self):
        """`exit_code=None` 只有两个来源（命令不存在 / 命令无法启动）—— 都是失败。
        原先 `[exit code None]` 不是整数，markers 又不认"命令不存在"，于是判成功。"""
        for obs in ("[exit code None] · 0.0s\n\n错误：命令不存在：[WinError 2] 系统找不到指定的文件。",
                    "[exit code None] · 0.0s\n\n错误：命令无法启动：[Errno 13] Permission denied"):
            with self.subTest(obs=obs[:28]):
                self.assertTrue(_run("godot --headless -s t.gd", obs))

    def test_structured_signal_wins_over_the_marker_table(self):
        """接管后不许再走 markers：否则「输出里有没有失败字样」又变成第二权威。"""
        # 0 号码 + 满屏 markers → 非失败
        self.assertFalse(_run("pytest -q", "[exit code 0] · 1s\n读取失败 拒绝访问 文件不存在 计算失败"))
        # 非零 + 一个 marker 都没有 → 失败（这正是原洞）
        self.assertTrue(_run("pytest -q", "[exit code 1] · 1s\n1 failed, 7 passed"))

    def test_background_job_json_is_untouched(self):
        """后台任务回的是 `{"ok": true, "job_id": …}`，没有退出码头 → 交回原判据。"""
        obs = json.dumps({"ok": True, "job_id": "job-1", "state": "running"}, ensure_ascii=False)
        self.assertFalse(_run("uvicorn main:app\nbackground: true", obs))


class NoContextBehaviorUnchangedTests(unittest.TestCase):
    """**反向守卫**：没给上下文就等于今天。

    漏掉这条守卫的后果：某个调用点忘了传 tool，于是那条路径上的判据悄悄换了口径，
    而单测全绿。
    """

    def test_single_argument_calls_keep_the_string_judge(self):
        self.assertFalse(agent_mod._is_failure(PYTEST_FAILURE))
        self.assertFalse(agent_mod._is_failure("[exit code 2] grep: Unmatched ["))
        self.assertFalse(agent_mod._is_failure("[exit code None]\n错误：命令不存在"))
        self.assertEqual(agent_mod._is_failure(PYTEST_FAILURE, "", ""),
                         agent_mod._is_failure(PYTEST_FAILURE))

    def test_other_tools_keep_the_string_judge(self):
        """只有 `run_command` 接管：read_file 的观察里带 `[exit code …]` 字样时
        （日志文件、被读的脚本）不许改判。"""
        self.assertTrue(agent_mod._is_failure("读取失败：文件不存在", "read_file", "x"))
        self.assertFalse(agent_mod._is_failure(PYTEST_FAILURE, "read_file", "logs/run.log"))
        # 无匹配仍是合法答案（这条不属于 run_command，走原判据）
        self.assertFalse(agent_mod._is_failure("未找到相关内容", "search_code", "query: x"))

    def test_empty_observation_still_failure(self):
        self.assertTrue(agent_mod._is_failure("", "run_command", "pytest -q"))
        self.assertTrue(agent_mod._is_failure(None, "run_command", "pytest -q"))


class BoundJudgeTests(unittest.TestCase):
    def test_failure_for_is_a_one_argument_callable(self):
        """`execute_tool(function, argument, legacy_failure)` 的契约只喂文本；
        绑定点必须产出一个 1 参数可调用，否则 PTC 那类测试替身当场 TypeError。"""
        judge = agent_mod._failure_for("run_command", "pytest -q")
        params = [p for p in inspect.signature(judge).parameters.values()]
        self.assertEqual(len(params), 1, "绑定后的判据必须恰好 1 个参数")
        self.assertTrue(judge(PYTEST_FAILURE))
        self.assertFalse(judge("[exit code 0] · 1s\n8 passed"))

    def test_execute_tool_marks_command_failure_via_bound_judge(self):
        """这就是 `_tool_ok` 的真实来源：接管前这里 ok=True（洞），接管后必须 False。"""
        from agent_runtime.tools import execute_tool
        result = execute_tool(lambda arg: PYTEST_FAILURE, "pytest -q",
                              agent_mod._failure_for("run_command", "pytest -q"),
                              tool_name="run_command")
        self.assertFalse(result.ok, "真失败的命令被 execute_tool 记成成功")
        self.assertEqual(result.error_kind, "legacy_failure")
        ok_result = execute_tool(lambda arg: "[exit code 0] · 1s\n8 passed\n" + PYTEST_CACHE_WARN,
                                 "pytest -q", agent_mod._failure_for("run_command", "pytest -q"),
                                 tool_name="run_command")
        self.assertTrue(ok_result.ok)

    def test_execute_tool_for_a_non_command_tool_is_unchanged(self):
        from agent_runtime.tools import execute_tool
        result = execute_tool(lambda arg: "未找到相关内容", "query: x",
                              agent_mod._failure_for("search_code", "query: x"),
                              tool_name="search_code")
        self.assertTrue(result.ok, "检索无匹配是合法答案，不许进失败配额")


class PtcBridgeTests(unittest.TestCase):
    """PTC（python_exec 桥）也是真派发路径，漏一条 = 那条路上判据仍旧瞎。"""

    def test_ptc_path_honours_the_exit_code_verdict(self):
        def fake_execute(function, arg, judge, *, tool_name="", side_effect=None):
            return type("R", (), {"ok": not judge(PYTEST_FAILURE), "text": PYTEST_FAILURE})()

        server = agent_mod._ptc_build_server(
            tool_lookup=lambda name: {"func": fake_execute},
            spec_lookup=lambda name: None,
            execute_tool_fn=fake_execute,
            is_failure=agent_mod._is_failure,
            failure_for=agent_mod._failure_for,
            evidence_fn=lambda name, obs, ok: None,
            turn=None,
            side_effect_default=None,
        )
        ok, obs = server("run_command", "pytest -q")
        self.assertFalse(ok, "PTC 路径把真失败记成成功")
        self.assertEqual(obs, PYTEST_FAILURE)

    def test_ptc_without_failure_for_keeps_the_one_argument_contract(self):
        """测试替身传的就是 `lambda obs: …`；没给 failure_for 时不许多传参数。"""
        seen = []

        def fake_execute(function, arg, judge, *, tool_name="", side_effect=None):
            seen.append(len(inspect.signature(judge).parameters))
            return type("R", (), {"ok": not judge("BAD"), "text": "BAD"})()

        server = agent_mod._ptc_build_server(
            tool_lookup=lambda name: {"func": fake_execute},
            spec_lookup=lambda name: None,
            execute_tool_fn=fake_execute,
            is_failure=lambda obs: obs == "BAD",
            evidence_fn=lambda name, obs, ok: None,
            turn=None,
            side_effect_default=None,
        )
        ok, _ = server("some_tool", "arg")
        self.assertEqual(seen, [1], "缺省注入路径必须仍按 1 参数调用判据")
        self.assertFalse(ok)


class KnownLimitsTests(unittest.TestCase):
    """把「这次没修到」钉成断言，而不是留在注释里等人发现。

    钉住它们的理由：这三条都是**结构判定天然覆盖不到**的形状。不写出来的话，
    下次读到 `_is_failure` 的人会以为退出码已经是全知权威。
    """

    def test_pipeline_takes_the_last_exit_code(self):
        """`pytest | tail` 的退出码是 tail 的（0）—— 管道会吞掉失败。

        这是 shell 语义，不是判据的 bug：要补只能在工具侧加 `pipefail` 或逐段取码，
        那属于改 `run_command` 的输出协议，不该由判据兜。模型侧的规矩是「别把测试
        输出接进管道再判通过」。
        """
        obs = "[exit code 0] · 2.4s\nFAILED tests/test_a.py::x - assert 0 == 1\n1 failed in 2.1s"
        self.assertFalse(_run("pytest -q | tail -6", obs),
                         "已知限制：管道末位命令的退出码会盖掉测试失败")

    def test_region_verify_header_is_not_at_line_start(self):
        """`dev_region_verify` 把退出码头印在第二行（`运行 X 的 verify 命令：…\\n[exit code N]`），
        结构判定按契约只看首行，所以这条路仍走字符串判据。要修得先修 tools 侧的渲染。"""
        obs = "运行 backend 的 verify 命令：`pytest -q`\n" + PYTEST_FAILURE
        self.assertFalse(agent_mod._is_failure(obs, "dev_region_verify", "backend"))

    def test_command_text_absent_means_conservative(self):
        """拿不到命令文本时（如审计回放），非零一律按失败 —— 宁可多计一次失败，
        也不放过真失败；代价是最多多一次重试。"""
        self.assertTrue(agent_mod._is_failure(PYTEST_FAILURE, "run_command", ""))


class WiringTests(unittest.TestCase):
    """源码级钉死：接线漏一条，行为级单测照样绿。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(agent_mod.__file__)), "agent.py")
        with open(path, encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_dispatch_sites_pass_the_tool_context(self):
        """顺序 + 并行两条派发路径都得把 (工具名, 命令文本) 交给判据。"""
        sites = re.findall(r"execute_tool\(\s*([^,]+),\s*([a-z_0-9]+),\s*([A-Za-z_]+)\(",
                           self.source)
        self.assertEqual(len(sites), 2, "agent.py 里的 execute_tool 派发点应恰好两处")
        bound = [s for s in sites if s[2] == "_failure_for"]
        self.assertEqual(len(bound), 2,
                         "两处派发点的 execute_tool 都必须绑定工具上下文（顺序 + 并行）")
        judges = re.findall(r"not _is_failure\(obs,\s*([a-z_0-9]+),\s*([a-z_0-9]+)\)", self.source)
        self.assertEqual(len(judges), 2, "_tool_ok / ok 两处计算都要带上上下文")

    def test_ptc_server_scope_binds_the_judge(self):
        self.assertIn("failure_for=_failure_for", self.source,
                      "PTC 服务环没接上下文判据")

    def test_marker_table_still_has_no_generic_failure_words(self):
        """反向守卫：不许用"往 markers 里加 FAILED/Traceback"补这个洞。

        加了就会把 grep 无匹配、以及文件内容里恰好含这些字样的合法读取一并判成失败，
        喂给 `_TOOL_FAIL_LIMIT=3` 熔断并给出误导性结论 —— 这正是当年加无匹配白名单
        要救的坑。权威只允许是退出码。
        """
        for word in ("FAILED", "Traceback", "ERROR in", "failed", "No such file"):
            with self.subTest(word=word):
                self.assertNotIn(word, agent_mod._FAILURE_MARKERS)


class ProfileCaptureIsolationTests(_IsoBase):
    """L1 画像表按 user_id 全局键，不按 scope 分行 —— 所以隔离流量必须拒写。

    评测题只要出现一句「我叫…/我在…」，原先会永久写进真实用户的常驻画像，
    并注入之后每一个窗口（`profile_context()` 读的就是那一行）。
    """

    def test_isolated_turn_refuses_to_learn_the_profile(self):
        before = agent_memory.get_profile()
        profile = agent_memory.capture_explicit_profile("我叫评测机，我在广州",
                                                        write_project="eval-golden")
        self.assertEqual(profile["display_name"], before["display_name"])
        self.assertEqual(agent_memory.get_profile()["display_name"], "",
                         "评测流量写进了真实用户画像")
        self.assertEqual(agent_memory.get_profile()["location"], "")

    def test_real_turn_still_learns(self):
        """反向守卫：拒写只针对隔离 scope，正常回合的画像能力不许被顺手关掉。"""
        profile = agent_memory.capture_explicit_profile("我叫小李，我在南宁", write_project="")
        self.assertEqual(profile["display_name"], "小李")
        self.assertEqual(profile["location"], "南宁")

    def test_default_argument_keeps_learning(self):
        """不传参数（其它调用方）逐字节等于今天。"""
        profile = agent_memory.capture_explicit_profile("我的语言是英文")
        self.assertEqual(profile["language"], "英文")


if __name__ == "__main__":
    unittest.main()
