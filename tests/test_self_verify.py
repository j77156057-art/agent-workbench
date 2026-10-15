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


class SelfVerifyWebTest(unittest.TestCase):
    """静态产物 scope=web：单文件 HTML / CSS 的写后校验。

    为什么这一族存在：`.html` 原先不属于任何 scope → `ran` 为空 → status 判
    skipped → 收尾门把最终答复整段替换成「不能确认任务成功」。单文件 HTML 是
    vibecoding 最高频产物，这个洞正卡在主路上。
    """
    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def _node(self):
        import shutil
        return os.getenv("DOCMIND_NODE_BIN") or shutil.which("node")

    def test_html_no_longer_falls_into_the_no_validator_gap(self):
        # 回归：改动文件只有 .html 时，不能再落到「无对应校验器」
        self.proj.write("game.html", "<!DOCTYPE html>\n<html><body><p>hi</p></body></html>\n")
        data = json.loads(tools.self_verify("scope: auto\nfiles: game.html"))
        self.assertTrue(data["ran"], "html 仍然没有任何检查被执行：%s" % data)
        self.assertNotIn("无对应校验器", data.get("note") or "")
        self.assertTrue(any("html_structure" in r for r in data["ran"]))

    def test_valid_html_with_valid_script_passes(self):
        self.proj.write("game.html", textwrap.dedent(
            "<!DOCTYPE html>\n"
            "<html><head><title>t</title></head><body>\n"
            "<canvas id='c'></canvas>\n"
            "<script>\n"
            "  var a = 1;\n"
            "  function go() { return a + 1; }\n"
            "  go();\n"
            "</script>\n"
            "</body></html>\n"
        ))
        data = json.loads(tools.self_verify("scope: auto\nfiles: game.html"))
        self.assertEqual(data["failures"], [])
        self.assertTrue(any("html_structure" in r for r in data["ran"]))
        if self._node():
            self.assertTrue(data["passed"])
            self.assertTrue(any("node_check" in r for r in data["ran"]))
        else:
            self.skipTest("本机无 node，只能验证 HTML 结构检查已执行")

    def test_broken_inline_script_fails(self):
        if not self._node():
            self.skipTest("本机无 node，无法做脚本语法校验")
        self.proj.write("game.html", textwrap.dedent(
            "<html><body>\n"
            "<script>\n"
            "  function go( { return 1; }\n"     # 括号不成对 → 语法错误
            "</script>\n"
            "</body></html>\n"
        ))
        data = json.loads(tools.self_verify("scope: auto\nfiles: game.html"))
        self.assertFalse(data["passed"])
        self.assertTrue(any(fl["scope"] == "web" for fl in data["failures"]),
                        "内联脚本语法错误没有被 web scope 抓到：%s" % data)

    def test_mismatched_tag_fails(self):
        self.proj.write("game.html", "<html><body><div><span>x</div></body></html>\n")
        data = json.loads(tools.self_verify("scope: web\nfiles: game.html"))
        self.assertFalse(data["passed"])
        self.assertTrue(any("web" == fl["scope"] for fl in data["failures"]))

    def test_optional_end_tags_do_not_false_fail(self):
        # `<p>a<p>b` 是合法 HTML5（结束标签可省略）。收尾门一旦在这里假红，
        # 模型会去改本来正确的文件 —— 比漏检更糟，所以单独钉住。
        self.proj.write("index.html", "<!DOCTYPE html>\n<html><body><p>a<p>b</body></html>\n")
        data = json.loads(tools.self_verify("scope: web\nfiles: index.html"))
        self.assertEqual(data["failures"], [], "合法的可省略结束标签被误判：%s" % data)

    def test_unbalanced_css_fails(self):
        self.proj.write("style.css", "body { color: red;\n")
        data = json.loads(tools.self_verify("scope: web\nfiles: style.css"))
        self.assertFalse(data["passed"])
        self.assertTrue(any(fl["scope"] == "web" for fl in data["failures"]))

    def test_empty_html_fails(self):
        self.proj.write("empty.html", "   \n")
        data = json.loads(tools.self_verify("scope: web\nfiles: empty.html"))
        self.assertFalse(data["passed"])

    def test_missing_file_is_failure_not_skip(self):
        # 与 backend scope 同判据：校验一个不存在的文件 = 路径写错，不能假通过
        data = json.loads(tools.self_verify("scope: web\nfiles: nope.html"))
        self.assertFalse(data["passed"])
        self.assertTrue(any("不存在" in str(fl.get("error")) for fl in data["failures"]))


class ClassifyTest(unittest.TestCase):
    def test_classify_scene(self):
        _py, _fe, scene, engine = tools._sv_classify(["x/scenes/Main.tscn", "scene_runtime.py"])
        self.assertTrue(scene)
        self.assertFalse(engine)

    def test_classify_engine(self):
        _py, _fe, scene, engine = tools._sv_classify(["desktop_bridge.py", "engine_embed.py"])
        self.assertTrue(engine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
