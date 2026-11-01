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
        # 这些用例要**真跑项目命令**（跑目标单测 / 跑 engine 脚本 / node --check）。
        # 走的是 `enterprise_sandbox.run_project_command` —— 它需要一个可用的进程
        # 边界后端：Windows 天然有（host_compat），而 Linux CI 上默认是
        # `unavailable`（没有 Docker，也没开 DOCMIND_STAGE_HOST_COMMANDS），
        # 于是命令跑不了、判据一律 fail-closed —— 现象与「校验器坏了」一模一样。
        # 这里显式要求 host_compat：**命令在项目的一次性副本里跑**（试做区），
        # 与 Windows 本机口径一致，不涉及宿主机写入或凭据。
        self._env = mock.patch.dict(os.environ, {"DOCMIND_STAGE_HOST_COMMANDS": "1"})
        self._env.start()

    def write(self, rel, content):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p) or self.dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def close(self):
        self._env.stop()
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
            obs, state = agent._run_self_verify("x.py")
        self.assertIsNone(state, "校验器异常是「没能校验」，不是「校验失败」")
        self.assertIn("跳过", obs)

    def test_no_validator_is_unknown_not_failed(self):
        """`.md` 这类没有校验器的写入：不能记成失败，否则学习环拿到的全是假负面。"""
        with mock.patch.object(agent, "self_verify", return_value=json.dumps(
                {"scope": "auto", "ran": [], "passed": False, "failures": [],
                 "note": "无可执行的校验（该文件类型无对应校验器）"})):
            obs, state = agent._run_self_verify("docs/x.md")
        self.assertIsNone(state)
        self.assertIn("自验证未完成", obs)

    def test_partial_run_is_unknown(self):
        with mock.patch.object(agent, "self_verify", return_value=json.dumps(
                {"scope": "auto", "ran": ["py_compile:a.py"], "skipped": ["node --check"],
                 "passed": False, "failures": [], "note": ""})):
            _obs, state = agent._run_self_verify("a.py")
        self.assertIsNone(state, "跑了一部分不等于通过，也不等于失败")


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
    """静态产物 scope=web：单文件 HTML / CSS 的**第一级**写后校验（离线、零依赖）。

    为什么这一族存在：`.html` 原先不属于任何 scope → `ran` 为空 → status 判
    skipped → 收尾门把最终答复整段替换成「不能确认任务成功」。单文件 HTML 是
    vibecoding 最高频产物，这个洞正卡在主路上。

    与 SelfVerifyVisualAutoTest 的分工：这里验「产物本身有没有写坏」（结构/语法），
    那里验「跑起来画面有没有坏」（真浏览器截图）。合并两条并行分支时两套实现都
    留下了 —— 它们不是重复品，是两个不同失效面的门。
    """

    def setUp(self):
        self.proj = _TmpProject()
        # 本类只钉 web 门（结构/语法），所以把第二级浏览器门 mock 成「通过」：
        # 不 mock 就会真起浏览器截图，既慢又让「这条测的是哪一级」变模糊。
        # 为什么不用 DOCMIND_SELF_VERIFY_VISUAL=0 关掉它——关掉的门会进 `skipped`，
        # 而 finalize_verification 把「有 skip + 有执行」判成 partial（不通过），
        # 于是每条断言 passed 的用例都会红在跟本类无关的那一档上。
        patcher = mock.patch("agent_runtime.visual_acceptance.capture_project_preview",
                             lambda root, **kw: {"passed": True, "checks": {},
                                                 "screenshot": "x/preview.png"})
        patcher.start()
        self.addCleanup(patcher.stop)

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
class SelfVerifyVisualAutoTest(unittest.TestCase):
    """写 .html 静态网页 → scope:auto 自动挂真实浏览器视觉自审（离线：mock 掉截图）。

    这是 web 门之上的**第二级**：结构/语法过得去，不代表跑起来不是黑屏。
    """

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

    def test_auto_html_runs_both_gates(self):
        # 合并语义的核心判据：网页改动 auto 档一次挂两级门，不是二选一
        self.proj.write("index.html", "<!doctype html><title>t</title><p>hi</p>")
        report = {"passed": True, "checks": {"console_errors": 0, "failed_requests": 0},
                  "screenshot": ".docmind/visual-evidence/x/preview.png"}
        with self._capture_patch(ret=report):
            data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertTrue(any("html_structure" in r for r in data["ran"]),
                        "web 门没跑（单级实现回归了）：%s" % data)
        self.assertTrue(any("visual" in r for r in data["ran"]),
                        "visual 门没跑（单级实现回归了）：%s" % data)

    def test_css_does_not_trigger_visual(self):
        # 样式表没有「画面」可截，不该为改一行 CSS 起一次浏览器
        self.proj.write("style.css", "body { color: red; }\n")
        with mock.patch("agent_runtime.visual_acceptance.capture_project_preview") as m:
            m.return_value = {"passed": True, "checks": {}, "screenshot": ""}
            data = json.loads(tools.self_verify("scope: auto\nfiles: style.css"))
        self.assertTrue(any("css_balance" in r for r in data["ran"]))
        self.assertFalse(any("visual" in r for r in data["ran"]))
        m.assert_not_called()

    def test_auto_passes_changed_page_as_entry(self):
        """产物不叫 index.html 也必须截得到画面。

        visual 门原本只在项目里找 index.html，找不到就 fail-closed 判未通过。
        vibecoding 的产物叫 snake.html / game.html —— 那会让「文件已落盘却被判
        未验证」的收尾门 bug 换个形式放回来（web scope 那次修的就是它）。
        所以 auto 档必须把本次改动的网页当入口传进去。
        """
        self.proj.write("snake.html", "<!doctype html><title>snake</title><canvas></canvas>")
        seen = {}

        def _fake(root, **kw):
            seen["entry"] = kw.get("entry")
            return {"passed": True, "checks": {"console_errors": 0},
                    "screenshot": ".docmind/visual-evidence/x/preview.png"}

        with mock.patch("agent_runtime.visual_acceptance.capture_project_preview", _fake):
            data = json.loads(tools.self_verify("scope: auto\nfiles: snake.html"))
        self.assertEqual(seen.get("entry"), "snake.html")
        self.assertTrue(data["passed"])
        self.assertTrue(any("snake.html" in r for r in data["ran"]))

    def test_visual_optout_env_skips_not_passed(self):
        """关掉画面自审 = 那扇门没跑，于是整体只能到 partial（不是通过）。

        与 engine 门同判据：主动关掉的门会进 `skipped`，finalize_verification 把
        「有 skip + 有执行」判成 partial 而非 passed —— 关掉一扇门不等于它过了。
        合并时这条的判据**原样保留**（它不是该被合并抹掉的东西），只补两行把
        「web 门此时仍然出证据」钉住：opt-out 下不再是「无任何校验器」的未验证态，
        但画面结论确实缺失，所以 passed 仍为 False、note 必须写出来。
        """
        self.proj.write("index.html", "<!doctype html><title>t</title>")
        with mock.patch.dict(os.environ, {"DOCMIND_SELF_VERIFY_VISUAL": "0"}):
            with self._capture_patch(ret={"passed": True, "checks": {}, "screenshot": ""}):
                data = json.loads(tools.self_verify("scope: auto\nfiles: index.html"))
        self.assertFalse(any("visual" in r for r in data["ran"]),
                         "视觉门被关了却仍出现在 ran 里（假绿）：%s" % data)
        self.assertIn("DOCMIND_SELF_VERIFY_VISUAL", data.get("note", ""))
        self.assertTrue(any("visual:skip" in s for s in data.get("skipped", [])))
        # web 门仍在出证据：opt-out 不是「无任何校验器」，但画面结论确实缺失
        self.assertTrue(any("html_structure" in r for r in data["ran"]))
        self.assertEqual(data["failures"], [])
        self.assertFalse(data["passed"])  # 关掉的门没跑 → partial，不是通过



class ClassifyTest(unittest.TestCase):
    def test_classify_scene(self):
        _py, _fe, scene, engine, _web, _data, _docs, _sql, _godot, _regions = tools._sv_classify(["x/scenes/Main.tscn", "scene_runtime.py"])
        self.assertTrue(scene)
        self.assertFalse(engine)

    def test_classify_engine(self):
        _py, _fe, scene, engine, _web, _data, _docs, _sql, _godot, _regions = tools._sv_classify(["desktop_bridge.py", "engine_embed.py"])
        self.assertTrue(engine)

    def test_classify_web(self):
        _py, _fe, _scene, _engine, web, _data, _docs, _sql, _g, _r = tools._sv_classify(
            ["index.html", "game.HTM", "style.css", "main.py"])
        # 合并后 web 桶统一取 `_WEB_EXTS`（.html/.htm/.css）：.css 也收，因为 web 门
        # 有 CSS 花括号平衡校验器；.py 仍不进桶。两个并行分支曾各自定义一套 web
        # 集合（一边含 css 一边不含），合并时收敛成一个真源，避免 auto 档判定分叉。
        self.assertEqual(len(web), 3)
        self.assertTrue(any(p.endswith("index.html") for p in web))
        self.assertFalse(any(p.endswith("main.py") for p in web))


class SelfVerifyDataDocsTest(unittest.TestCase):
    """数据文件（json/jsonl/toml）与文档（md）的写后校验。

    2026-11-02 之前这两类一个桶都不进：`.json` 1399 个、`.md` 215 个，纯数据/文档
    写入永远拿不到验证结论（要么「无对应校验器」，要么被前一张网当成校验失败）。
    """

    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def _run(self, rel):
        return json.loads(tools.self_verify("scope: auto\nfiles: " + rel))

    def test_valid_json_passes(self):
        self.proj.write("data/ok.json", '{"a": 1, "b": [1, 2]}\n')
        data = self._run("data/ok.json")
        self.assertTrue(data["passed"], data)
        self.assertIn("data_json:ok.json", data["ran"])

    def test_truncated_json_is_a_failure(self):
        self.proj.write("data/bad.json", '{"a": 1')
        data = self._run("data/bad.json")
        self.assertFalse(data["passed"])
        self.assertIn("data", [f["scope"] for f in data["failures"]])

    def test_jsonl_bad_line_names_the_line(self):
        self.proj.write("data/bad.jsonl", '{"a": 1}\nnot json\n')
        data = self._run("data/bad.jsonl")
        self.assertFalse(data["passed"])
        self.assertIn("第 2 行", json.dumps(data["failures"], ensure_ascii=False))

    def test_markdown_with_balanced_fences_passes(self):
        self.proj.write("docs/ok.md", "# 标题\n\n```py\nx = 1\n```\n")
        data = self._run("docs/ok.md")
        self.assertTrue(data["passed"], data)
        self.assertIn("docs_markdown:ok.md", data["ran"])

    def test_markdown_with_odd_fences_fails(self):
        self.proj.write("docs/bad.md", "# 标题\n\n```py\nx = 1\n")
        data = self._run("docs/bad.md")
        self.assertFalse(data["passed"])
        self.assertIn("围栏", json.dumps(data["failures"], ensure_ascii=False))

    def test_markdown_with_nul_bytes_fails(self):
        path = os.path.join(self.proj.dir, "docs", "nul.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"# t\x00\n")
        data = self._run("docs/nul.md")
        self.assertFalse(data["passed"])
        self.assertIn("NUL", json.dumps(data["failures"], ensure_ascii=False))

    def test_mjs_counts_as_frontend(self):
        target = os.path.join(self.proj.dir, "frontend", "tests", "x.browser.mjs")
        py, fe, _scene, _engine, _web, _data, _docs, _sql, _g, _r = tools._sv_classify([target])
        self.assertEqual(fe, [target], "浏览器回归（.mjs）不该落在「无校验器」里")

    def test_json_and_md_no_longer_report_the_no_validator_gap(self):
        self.proj.write("data/ok.json", "{}\n")
        self.proj.write("docs/ok.md", "# t\n")
        for rel in ("data/ok.json", "docs/ok.md"):
            note = self._run(rel).get("note") or ""
            self.assertNotIn("无对应校验器", note, rel)


class SelfVerifySqlTest(unittest.TestCase):
    """SQL 的语句完整性校验：切句要认字符串里的分号与 TRIGGER 的内部分号，不连库执行。"""

    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def _run(self, rel):
        return json.loads(tools.self_verify("scope: auto\nfiles: " + rel))

    def test_valid_sql_counts_statements(self):
        self.proj.write("db/ok.sql",
                        "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
                        "INSERT INTO t VALUES (1, 'a;b');\n")
        data = self._run("db/ok.sql")
        self.assertTrue(data["passed"], data)
        self.assertIn("（2 条）", data["ran"][0], "字符串里的分号不该被切句")

    def test_semicolon_at_end_of_a_string_line_does_not_split(self):
        """按行尾分号切句会把它切成两条 —— 必须按 SQL 字面量规则切。"""
        self.proj.write("db/str.sql",
                        "INSERT INTO t VALUES ('a;\nb');\n"
                        "SELECT 1;\n")
        data = self._run("db/str.sql")
        self.assertTrue(data["passed"], data)
        self.assertIn("（2 条）", data["ran"][0])

    def test_truncated_sql_fails(self):
        self.proj.write("db/bad.sql", "CREATE TABLE t (\n  id INTEGER\n")
        data = self._run("db/bad.sql")
        self.assertFalse(data["passed"])
        self.assertIn("没有以分号结束", json.dumps(data["failures"], ensure_ascii=False))

    def test_comment_only_file_is_not_an_error(self):
        self.proj.write("db/note.sql", "-- 只有注释\n-- 没有语句\n")
        data = self._run("db/note.sql")
        self.assertTrue(data["passed"], data)
        self.assertIn("（0 条）", data["ran"][0])

    def test_trigger_body_semicolons_are_not_split(self):
        self.proj.write("db/trigger.sql",
                        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN\n"
                        "  UPDATE t SET name = 'x';\nEND;\n")
        data = self._run("db/trigger.sql")
        self.assertTrue(data["passed"], data)
        self.assertIn("（1 条）", data["ran"][0])


class WriteObservationParseTest(unittest.TestCase):
    """写后收尾门的入口：从 Observation 里取「刚写了哪些文件」。"""

    PATCH = ("补丁已应用：2 个文件，+10 -3\n"
             "  src/a.py  +4 -1\n"
             "  docs/b.md  +6 -2（新建）\n"
             "提示: 行号已自动对齐")

    def test_patch_observation_yields_every_file(self):
        self.assertEqual(agent._parse_written_rel(self.PATCH), "src/a.py,docs/b.md")

    def test_dry_run_patch_writes_nothing(self):
        self.assertIsNone(agent._parse_written_rel(
            "校验通过（dry_run，未写盘）：1 个文件，+2 -0\n  src/c.py  +2 -0\n"))

    def test_legacy_write_observation_still_parses(self):
        self.assertEqual(
            agent._parse_written_rel("已创建 docs/x.md（513 字节，路径沙箱校验通过）"),
            "docs/x.md")

    def test_dev_patch_is_covered_by_the_post_write_gate(self):
        """它能一次改多个文件；不在门内时整轮既没有 pass 也没有 fail（没有证据）。"""
        self.assertIn("dev_patch", agent._VERIFY_AFTER_WRITE_TOOLS)

    def test_real_dev_patch_output_round_trips_into_verification(self):
        """真打一次补丁，用**真实渲染输出**喂解析器，再拿解析出的路径去校验。

        `patch_apply.render_patch` 的格式一变，解析器就会静默取不到文件（收尾门
        看起来还在、实际什么都没验）。这条用例把两边的格式钉在一起。
        """
        proj = _TmpProject()
        try:
            proj.write("a.py", "x = 1\n")
            obs = tools.dev_patch("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n")
            self.assertEqual(agent._parse_written_rel(obs), "a.py")
            data = json.loads(tools.self_verify("scope: auto\nfiles: a.py"))
            self.assertTrue(data["passed"], data)
        finally:
            proj.close()


class SelfVerifyGodotRegionsTest(unittest.TestCase):
    """Godot 文本资源的结构校验 + 分区配置的落盘状态校验。"""

    def setUp(self):
        self.proj = _TmpProject()

    def tearDown(self):
        self.proj.close()

    def _run(self, rel):
        return json.loads(tools.self_verify("scope: auto\nfiles: " + rel))

    def test_gd_with_braces_inside_strings_is_fine(self):
        self.proj.write("scripts/player.gd",
                        'extends Node\n\nfunc _ready():\n\tprint("{ok}")\n')
        data = self._run("scripts/player.gd")
        self.assertTrue(data["passed"], data)

    def test_truncated_gd_fails(self):
        self.proj.write("scripts/broken.gd", "extends Node\n\nfunc _ready():\n\tvar d = {\n")
        data = self._run("scripts/broken.gd")
        self.assertFalse(data["passed"])
        self.assertIn("不配平", json.dumps(data["failures"], ensure_ascii=False))

    def test_tscn_without_header_fails(self):
        self.proj.write("levels/bad.tscn", '[node name="Root" type="Node"]\n')
        data = self._run("levels/bad.tscn")
        self.assertFalse(data["passed"])
        self.assertIn("gd_scene", json.dumps(data["failures"], ensure_ascii=False))

    def test_tscn_with_header_passes(self):
        self.proj.write("levels/level1.tscn",
                        "[gd_scene load_steps=2 format=3]\n\n[node name=\"Root\" type=\"Node\"]\n")
        self.assertTrue(self._run("levels/level1.tscn")["passed"])

    def test_gdshader_balanced_passes(self):
        self.proj.write("shaders/s.gdshader",
                        "shader_type canvas_item;\nvoid fragment() {\n\tCOLOR = vec4(1.0);\n}\n")
        self.assertTrue(self._run("shaders/s.gdshader")["passed"])

    def test_regions_with_missing_dir_fails(self):
        os.makedirs(os.path.join(self.proj.dir, "scripts"), exist_ok=True)
        self.proj.write("regions.json", json.dumps(
            [{"key": "scripts", "dir": "scripts"}, {"key": "ghost", "dir": "nope"}]))
        data = self._run("regions.json")
        self.assertFalse(data["passed"])
        self.assertIn("分区目录不存在", json.dumps(data["failures"], ensure_ascii=False))

    def test_regions_scheme_passes_when_dirs_exist(self):
        os.makedirs(os.path.join(self.proj.dir, "scripts"), exist_ok=True)
        self.proj.write("regions.json", json.dumps([{"key": "scripts", "dir": "scripts"}]))
        data = self._run("regions.json")
        self.assertTrue(data["passed"], data)
        self.assertIn("regions_scheme", data["ran"][0])

    def test_apply_regions_message_targets_regions_json(self):
        self.assertEqual(
            agent._parse_written_rel("已应用 Agent 研判的分区方案。分区目录已初始化"),
            "regions.json")

    def test_dev_apply_regions_is_in_the_gate(self):
        self.assertIn("dev_apply_regions", agent._VERIFY_AFTER_WRITE_TOOLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
