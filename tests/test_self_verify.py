"""self_verify（写后自验证闭环 Phase 1）单元测试——全部离线、无模型、无真实 git。

覆盖：
  · 后端 .py 语法校验：坏文件→passed=False 且 failures 含 backend；好文件→passed=True
  · 命中对应单测：tests/test_<module>.py 存在且失败时→passed=False
  · scope=skip / 无改动 → skipped，不能声明验证通过
  · 入参解析 _parse_self_verify_arg（scope/files 多行/逗号）
  · Agent 收尾门格式化 _run_self_verify：把校验结果包成 Observation 文本 + passed 布尔
"""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import tools
import agent


class _TmpProject:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="sv_test_")
        self._patch = mock.patch.object(tools, "_get_code_root", lambda: self.dir)
        self._patch.start()

    def write(self, rel, content):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p) or self.dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def close(self):
        self._patch.stop()


class SelfVerifyBackendTest(unittest.TestCase):
    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def test_broken_py_fails(self):
        self.proj.write("badmod.py", "def f(:\n    pass\n")  # 语法错误
        raw = tools.self_verify("scope: auto\nfiles: badmod.py")
        data = json.loads(raw)
        self.assertFalse(data["passed"])
        self.assertTrue(any(fl["scope"] == "backend" for fl in data["failures"]))

    def test_valid_py_passes(self):
        self.proj.write("goodmod.py", "def f():\n    return 1\n")
        raw = tools.self_verify("scope: auto\nfiles: goodmod.py")
        data = json.loads(raw)
        self.assertTrue(data["passed"])
        self.assertEqual(data["failures"], [])

    def test_targeted_unittest_failure_caught(self):
        # 好语法、但有对应单测且该单测失败 → 应被捕获
        self.proj.write("buggy.py", "VALUE = 1\n")
        self.proj.write("tests/__init__.py", "")
        self.proj.write("tests/test_buggy.py", textwrap.dedent(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_it(self):\n"
            "        self.assertEqual(1, 2)\n"
        ))
        raw = tools.self_verify("scope: auto\nfiles: buggy.py")
        data = json.loads(raw)
        self.assertFalse(data["passed"])
        self.assertTrue(any("buggy" in (fl.get("error") or "") for fl in data["failures"]))

    def test_skip_scope_safe(self):
        raw = tools.self_verify("scope: skip")
        data = json.loads(raw)
        self.assertFalse(data["passed"])
        self.assertEqual(data["status"], "skipped")
        self.assertIn("skip", data["note"])

    def test_no_changes_safe(self):
        # 没有执行检查，不能标记为验证通过。
        raw = tools.self_verify("scope: auto")
        data = json.loads(raw)
        self.assertFalse(data["passed"])
        self.assertEqual(data["status"], "skipped")


class SelfVerifyParseTest(unittest.TestCase):
    def test_parse_scope_files_multiline(self):
        scope, files = tools._parse_self_verify_arg("scope: backend\nfiles: a.py\nfiles: b.py")
        self.assertEqual(scope, "backend")
        self.assertEqual(files, ["a.py", "b.py"])

    def test_parse_comma_files(self):
        scope, files = tools._parse_self_verify_arg("files: a.py, b.py")
        self.assertEqual(scope, "auto")
        self.assertEqual(files, ["a.py", "b.py"])

    def test_parse_plain_text_as_files(self):
        scope, files = tools._parse_self_verify_arg("a.py")
        self.assertEqual(scope, "auto")
        self.assertEqual(files, ["a.py"])

    def test_parse_empty(self):
        scope, files = tools._parse_self_verify_arg("")
        self.assertEqual(scope, "auto")
        self.assertIsNone(files)


class AgentGateFormatTest(unittest.TestCase):
    def test_passed_formats_obs(self):
        with mock.patch.object(agent, "self_verify", return_value=json.dumps(
                {"scope": "auto", "ran": ["py_compile:x.py"], "passed": True,
                 "failures": [], "note": ""})):
            obs, passed = agent._run_self_verify("x.py")
        self.assertTrue(passed)
        self.assertIn("自验证通过", obs)

    def test_failed_formats_obs_and_passed_false(self):
        with mock.patch.object(agent, "self_verify", return_value=json.dumps(
                {"scope": "auto", "ran": [], "passed": False,
                 "failures": [{"scope": "backend", "file": "x.py", "error": "boom"}],
                 "note": ""})):
            obs, passed = agent._run_self_verify("x.py")
        self.assertFalse(passed)
        self.assertIn("自验证未通过", obs)
        self.assertIn("boom", obs)

    def test_exception_is_unverified(self):
        with mock.patch.object(agent, "self_verify", side_effect=RuntimeError("kaboom")):
            obs, passed = agent._run_self_verify("x.py")
        self.assertFalse(passed)
        self.assertIn("跳过", obs)


class SelfVerifyScopeAutoTest(unittest.TestCase):
    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def test_auto_includes_scene(self):
        # scene 子系统文件在 auto 模式应自动触发 verify_scene_canvas.py（用 stub 代替重型真脚本）
        self.proj.write("verify_scene_canvas.py", "import sys\nsys.exit(0)\n")
        self.proj.write("scenes/Main.tscn", "[gd_scene format=3]\n")
        data = json.loads(tools.self_verify("scope: auto\nfiles: scenes/Main.tscn"))
        self.assertTrue(data["passed"])
        self.assertTrue(any("scene" in r for r in data["ran"]))

    def test_auto_engine_skipped_without_flag(self):
        # engine 默认不自动跑（需真 Godot），应降级 skip 并在 note 说明
        self.proj.write("verify_engine_embed.py", "import sys\nsys.exit(0)\n")
        self.proj.write("desktop_bridge.py", "X = 1\n")
        with mock.patch.dict(os.environ, {"DOCMIND_SELF_VERIFY_ENGINE": "0"}):
            data = json.loads(tools.self_verify("scope: auto\nfiles: desktop_bridge.py"))
        self.assertFalse(data["passed"])
        self.assertEqual(data["status"], "partial")
        self.assertFalse(any("engine" in r for r in data["ran"]))
        self.assertIn("engine", data.get("note", ""))

    def test_auto_engine_runs_with_flag(self):
        self.proj.write("verify_engine_embed.py", "import sys\nsys.exit(0)\n")
        self.proj.write("desktop_bridge.py", "X = 1\n")
        with mock.patch.dict(os.environ, {"DOCMIND_SELF_VERIFY_ENGINE": "1"}):
            data = json.loads(tools.self_verify("scope: auto\nfiles: desktop_bridge.py"))
        self.assertTrue(data["passed"])
        self.assertTrue(any("engine" in r for r in data["ran"]))

    def test_scope_all_runs_engine_without_flag(self):
        # scope:all 显式触发，不依赖 flag
        self.proj.write("verify_engine_embed.py", "import sys\nsys.exit(0)\n")
        self.proj.write("desktop_bridge.py", "X = 1\n")
        with mock.patch.dict(os.environ, {"DOCMIND_SELF_VERIFY_ENGINE": "0"}):
            data = json.loads(tools.self_verify("scope: all\nfiles: desktop_bridge.py"))
        self.assertTrue(any("engine" in r for r in data["ran"]))


class SelfVerifyVisualAutoTest(unittest.TestCase):
    """写 .html 静态网页 → scope:auto 自动挂真实浏览器视觉自审（离线：mock 掉截图）。"""

    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def _capture_patch(self, ret=None, exc=None):
        def _fake(root, **kw):
            if exc is not None:
                raise exc
            return ret
        return mock.patch("agent_runtime.visual_acceptance.capture_project_preview", _fake)

    def test_auto_html_runs_visual_and_passes(self):
        self.proj.write("index.html", "<!doctype html><title>t</title>")
        report = {"passed": True, "checks": {"console_errors": 0, "failed_requests": 0},
                  "screenshot": ".docmind/visual-evidence/x/preview.png"}
        with self._capture_patch(ret=report):
            data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertTrue(data["passed"])
        self.assertTrue(any("visual" in r for r in data["ran"]))

    def test_auto_html_console_error_fails(self):
        # 页面黑屏/脚本报错 → capture 报 passed=False → 整体未通过（这正是能抓到 localStorage 崩溃的路径）
        self.proj.write("index.html", "<!doctype html><title>t</title>")
        report = {"passed": False,
                  "checks": {"console_errors": 1, "errors": ["SecurityError: storage blocked"]},
                  "screenshot": ""}
        with self._capture_patch(ret=report):
            data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertFalse(data["passed"])
        self.assertTrue(any(fl["scope"] == "visual" for fl in data["failures"]))

    def test_auto_html_capture_exception_fail_closed(self):
        # 起不了浏览器/截图抛异常 → 记 failure，绝不假绿（无证据 ≠ 通过）
        self.proj.write("index.html", "<!doctype html><title>t</title>")
        with self._capture_patch(exc=RuntimeError("no browser")):
            data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertFalse(data["passed"])
        self.assertTrue(any(fl["scope"] == "visual" for fl in data["failures"]))

    def test_visual_optout_env_skips_not_passed(self):
        self.proj.write("index.html", "<!doctype html><title>t</title>")
        with mock.patch.dict(os.environ, {"DOCMIND_SELF_VERIFY_VISUAL": "0"}):
            with self._capture_patch(ret={"passed": True, "checks": {}, "screenshot": ""}):
                data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertFalse(data["passed"])
        self.assertFalse(any("visual" in r for r in data["ran"]))
        self.assertIn("DOCMIND_SELF_VERIFY_VISUAL", data.get("note", ""))


class ClassifyTest(unittest.TestCase):
    def test_classify_scene(self):
        _py, _fe, scene, engine, _web = tools._sv_classify(["x/scenes/Main.tscn", "scene_runtime.py"])
        self.assertTrue(scene)
        self.assertFalse(engine)

    def test_classify_engine(self):
        _py, _fe, scene, engine, _web = tools._sv_classify(["desktop_bridge.py", "engine_embed.py"])
        self.assertTrue(engine)

    def test_classify_web(self):
        _py, _fe, _scene, _engine, web = tools._sv_classify(
            ["index.html", "game.HTM", "style.css", "main.py"])
        self.assertEqual(len(web), 2)  # .html + 大写 .HTM 都收，css/py 不进
        self.assertTrue(any(p.endswith("index.html") for p in web))


if __name__ == "__main__":
    unittest.main(verbosity=2)
