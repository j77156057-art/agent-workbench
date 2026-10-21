"""诊断工具回归：解析正确性 + 「绝不假干净」。

这里最要紧的不是能不能找出问题，而是【跑不成时不能报成没问题】：ruff 没装、输出被
截断、退出码说有发现却解析不出条目——这三种都必须响亮地报错。
真实执行 ruff 的用例在 ruff 不可用时 skip，其余全部本地可跑。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import diagnostics as dg  # noqa: E402


def ruff_available():
    try:
        proc = subprocess.run([sys.executable, "-m", "ruff", "--version"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


RUFF_OK = ruff_available()

#: 语料里的绝对路径必须用**本平台**的形状。早先这里写死 "D:\repo\pkg\a.py"：
#: 那在 Linux 上根本不是绝对路径（`os.path.isabs` 为 False），`_rel` 于是原样
#: 返回 "D:/repo/pkg/a.py" —— 这条用例在 ubuntu 上必红（CI 2026-10-21 实测）。
#: 根目录取**真正绝对**的那个：注意 `os.sep + "repo"` 在 Windows 上是
#: "\repo"（**没有盘符**），而 Python 3.13 起 `ntpath.isabs` 不认它 —— 得用
#: `tempfile.gettempdir()` 这种带根/带盘符的，否则两边又对不上。
RUFF_ROOT = os.path.join(tempfile.gettempdir(), "docmind-fixture-repo")

RUFF_JSON = json.dumps([
    {"filename": os.path.join(RUFF_ROOT, "pkg", "a.py"), "code": "F821",
     "message": "Undefined name `foo`", "location": {"row": 12, "column": 5},
     "end_location": {"row": 12, "column": 8}},
    {"filename": os.path.join(RUFF_ROOT, "pkg", "b.py"), "code": "E722",
     "message": "Do not use bare `except`", "location": {"row": 3, "column": 1},
     "end_location": {"row": 3, "column": 7}},
])

TSC_TEXT = """
> docmind-frontend@0.1 run typecheck

src/workbench/liveAudioControl.ts(19,14): error TS2304: Cannot find name `OUTPUT_SAMPLE_RATE`.
src/workbench/components/ChatDock.vue(120,3): warning TS1380: An import path cannot end with a '.ts' extension.
npm ERR! process exited
"""


class ParseCases(unittest.TestCase):
    def test_ruff_json_normalises_paths_and_severity(self):
        issues, fmt = dg.parse_ruff(RUFF_JSON, RUFF_ROOT)
        self.assertEqual(fmt, "json")
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]["file"], "pkg/a.py")
        self.assertEqual(issues[0]["line"], 12)
        self.assertEqual(issues[0]["rule"], "F821")
        self.assertEqual(issues[0]["severity"], "error")    # F821 未定义名字
        self.assertEqual(issues[1]["severity"], "warning")  # E722 裸 except 属卫生问题

    def test_ruff_concise_fallback(self):
        payload = "pkg/a.py:12:5: F821 Undefined name `foo`\nnote: 1 error\n"
        issues, fmt = dg.parse_ruff(payload, "/repo")
        self.assertEqual(fmt, "concise")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["rule"], "F821")

    def test_truncated_json_is_not_read_as_clean(self):
        issues, fmt = dg.parse_ruff(RUFF_JSON[:60], "/repo")   # 截断成非法 JSON
        self.assertEqual(fmt, "concise")
        self.assertEqual(issues, [])

    def test_empty_output(self):
        self.assertEqual(dg.parse_ruff("", "/repo"), ([], ""))

    def test_tsc_lines_and_noise(self):
        issues = dg.parse_tsc(TSC_TEXT, "frontend")
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]["file"], "frontend/src/workbench/liveAudioControl.ts")
        self.assertEqual(issues[0]["rule"], "TS2304")
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[1]["severity"], "warning")
        self.assertTrue(all(row["engine"] == "vue-tsc" for row in issues))

    def test_severity_mapping(self):
        self.assertEqual(dg._severity("E902"), "error")
        self.assertEqual(dg._severity("W291"), "warning")
        self.assertEqual(dg._severity(""), "info")

    def test_rules_validation(self):
        select, err = dg._select_rules("")
        self.assertEqual(select, dg.DEFAULT_SELECT)
        self.assertEqual(err, "")
        self.assertEqual(dg._select_rules("ALL")[0], "ALL")
        self.assertEqual(dg._select_rules("E, W, F")[0], "E,W,F")
        self.assertFalse(dg._select_rules("--fix")[0])
        self.assertIn("rules", dg._select_rules("--fix")[1])


class _TmpRoot(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_diag_")
        config.set_runtime("code_root", self.tmp)

    def tearDown(self):
        config.set_runtime("code_root", self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, content):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full


class PythonGuardCases(_TmpRoot):
    def test_target_escape_rejected(self):
        res = dg.python_diagnostics(self.tmp, ["../outside.py"])
        self.assertFalse(res["ok"])
        self.assertIn("越界", res["error"])

    def test_too_many_targets_rejected(self):
        targets = [f"a{i}.py" for i in range(dg.MAX_TARGETS + 1)]
        res = dg.python_diagnostics(self.tmp, targets)
        self.assertFalse(res["ok"])
        self.assertIn("最多诊断", res["error"])

    def test_bad_rules_rejected(self):
        res = dg.python_diagnostics(self.tmp, ["a.py"], rules="; rm -rf")
        self.assertFalse(res["ok"])

    def test_missing_ruff_is_reported_not_clean(self):
        fake = {"error": "", "timed_out": False, "exit_code": 1,
                "output": "No module named ruff", "truncated": False}
        with mock.patch.object(dg.process_runner, "run_bounded", return_value=fake):
            res = dg.python_diagnostics(self.tmp, ["a.py"])
        self.assertFalse(res["ok"])
        self.assertIn("未安装", res["error"])

    def test_unparsable_findings_are_reported_not_clean(self):
        # 关键回归：曾经 8786 字符的 JSON 被默认窗口截断，工具静默回报「0 条」
        fake = {"error": "", "timed_out": False, "exit_code": 1,
                "output": "[{\"filename\": \"a.py\", \"loc", "truncated": True}
        with mock.patch.object(dg.process_runner, "run_bounded", return_value=fake):
            res = dg.python_diagnostics(self.tmp, ["a.py"])
        self.assertFalse(res["ok"])
        self.assertIn("没能解析", res["error"])

    def test_real_ruff_finds_undefined_name(self):
        if not RUFF_OK:
            self.skipTest("ruff 不可用")
        self.write("pkg/bad.py", "def run():\n    return foo(1)\n")
        res = dg.python_diagnostics(self.tmp, ["pkg/bad.py"])
        self.assertTrue(res["ok"], res["error"])
        rules = {row["rule"] for row in res["issues"]}
        self.assertIn("F821", rules)
        hit = [row for row in res["issues"] if row["rule"] == "F821"][0]
        self.assertEqual(hit["file"], "pkg/bad.py")
        self.assertEqual(hit["line"], 2)

    def test_real_ruff_clean_file_reports_clean(self):
        if not RUFF_OK:
            self.skipTest("ruff 不可用")
        self.write("pkg/good.py", "VALUE = 1\nprint(VALUE)\n")
        res = dg.python_diagnostics(self.tmp, ["pkg/good.py"])
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["issues"], [])
        text = dg.render(dg.run(self.tmp, "pkg/good.py", scope="py"))
        self.assertIn("没有发现问题", text)


class FrontendGuardCases(_TmpRoot):
    def test_no_frontend_dir(self):
        res = dg.frontend_diagnostics(self.tmp)
        self.assertFalse(res["ok"])
        self.assertIn("package.json", res["error"])

    def test_no_typecheck_script(self):
        self.write("frontend/package.json", json.dumps({"scripts": {"dev": "vite"}}))
        res = dg.frontend_diagnostics(self.tmp)
        self.assertFalse(res["ok"])
        self.assertIn("typecheck", res["error"])

    def test_missing_npm_reported(self):
        self.write("frontend/package.json",
                   json.dumps({"scripts": {"typecheck": "vue-tsc --noEmit"}}))
        with mock.patch.object(dg.shutil, "which", return_value=None):
            env = dict(os.environ)
            env.pop("DOCMIND_NPM_BIN", None)
            with mock.patch.dict(os.environ, env, clear=True):
                res = dg.frontend_diagnostics(self.tmp)
        self.assertFalse(res["ok"])
        self.assertIn("npm", res["error"])

    def test_failure_without_parsable_errors_is_disclosed(self):
        self.write("frontend/package.json",
                   json.dumps({"scripts": {"typecheck": "vue-tsc --noEmit"}}))
        fake = {"error": "", "timed_out": False, "exit_code": 2,
                "output": "something exploded entirely", "truncated": False}
        with mock.patch.object(dg.process_runner, "run_bounded", return_value=fake):
            with mock.patch.object(dg.shutil, "which", return_value="npm"):
                res = dg.frontend_diagnostics(self.tmp)
        self.assertTrue(res["ok"])
        self.assertEqual(res["issues"], [])
        self.assertTrue(any("没解析出类型错误" in n for n in res["notes"]))


class DispatchCases(_TmpRoot):
    def test_auto_scope_picks_python(self):
        self.write("pkg/a.py", "x = 1\n")
        plain = {"engine": "ruff", "ok": True, "issues": [], "notes": [], "error": ""}
        with mock.patch.object(dg, "python_diagnostics", return_value=plain) as py:
            with mock.patch.object(dg, "frontend_diagnostics", return_value=plain) as fe:
                res = dg.run(self.tmp, "pkg/a.py")
        py.assert_called_once()
        fe.assert_not_called()
        self.assertEqual(res["scope"], "py")

    def test_auto_scope_picks_frontend_for_vue(self):
        self.write("frontend/src/a.vue", "<template><p>x</p></template>\n")
        plain = {"engine": "vue-tsc", "ok": True, "issues": [], "notes": [], "error": ""}
        with mock.patch.object(dg, "python_diagnostics") as py:
            with mock.patch.object(dg, "frontend_diagnostics", return_value=plain):
                res = dg.run(self.tmp, "frontend/src/a.vue")
        py.assert_not_called()
        self.assertEqual(res["scope"], "frontend")

    def test_comma_separated_targets_are_split(self):
        plain = {"engine": "ruff", "ok": True, "issues": [], "notes": [], "error": ""}
        with mock.patch.object(dg, "python_diagnostics", return_value=plain) as py:
            dg.run(self.tmp, "a.py, b.py", scope="py")
        self.assertEqual(py.call_args[0][1], ["a.py", "b.py"])

    def test_issue_cap_marks_truncated(self):
        issues = [{"engine": "ruff", "file": f"a{i}.py", "line": i, "column": 1,
                   "rule": "F821", "message": "m", "severity": "error"}
                  for i in range(dg.MAX_ISSUES + 5)]
        with mock.patch.object(dg, "python_diagnostics",
                               return_value={"engine": "ruff", "ok": True, "issues": issues,
                                             "notes": [], "error": ""}):
            res = dg.run(self.tmp, "a.py", scope="py")
        self.assertTrue(res["truncated"])
        self.assertEqual(res["count"], dg.MAX_ISSUES)

    def test_render_never_claims_clean_without_a_run(self):
        text = dg.render({"ok": False, "engines": {}, "issues": [], "notes": [],
                          "error": "ruff 未安装", "count": 0})
        self.assertIn("没有可用的诊断结果", text)
        self.assertIn("未安装", text)


class ToolLayerCases(_TmpRoot):
    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        self.assertIn("dev_diagnostics", names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        self.assertIn("- dev_diagnostics(target?", prompt)

    def test_missing_code_root(self):
        config.set_runtime("code_root", "")
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_diagnostics("target: a.py"))

    def test_bare_target_input_works(self):
        self.write("pkg/a.py", "def run():\n    return foo(1)\n")
        if not RUFF_OK:
            self.skipTest("ruff 不可用")
        out = tools.dev_diagnostics("pkg/a.py")
        self.assertIn("pkg/a.py", out)
        self.assertIn("F821", out)

    def test_scope_and_rules_passed_through(self):
        self.write("pkg/a.py", "def run():\n    return foo(1)\n")
        if not RUFF_OK:
            self.skipTest("ruff 不可用")
        out = tools.dev_diagnostics("target: pkg/a.py\nscope: py\nrules: ALL")
        self.assertIn("F821", out)


if __name__ == "__main__":
    unittest.main()
