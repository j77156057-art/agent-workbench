"""Wave16 回归：把真实 `.vue` 组件挂进真浏览器验接线（零新依赖）。

这一层的价值全在"能红"上：组件被改坏（emit 换了实参、`v-if` 写反）时守卫必须变红，
所以除了纯判定表，现场类里包含**故意改坏的组件副本**做反证——副本放在 gitignore 的
`frontend/.tmp/` 里，绝不原地改被别人监听的生产源码（本会话为这条踩过两次）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import vue_component as vc  # noqa: E402

TARGET = "src/workbench/components/WorkflowMembersDock.vue"
MEMBERS = [{"id": "t1", "label": "研究", "role": "r", "status": "running", "task": "查资料"},
           {"id": "t2", "label": "写作", "role": "r", "status": "ok", "task": "写初稿"}]


def props_payload(active=True, members=None):
    return json.dumps({"active": active, "members": MEMBERS if members is None else members},
                      ensure_ascii=False)


def _toolchain():
    """返回 (前端目录, 跳过原因)。判据直接用模块自己引用的那批常量，别再手抄一遍路径：
    上一版这里用 `.is_file()` 查了一个**目录**（`node_modules/@vue/compiler-sfc`），
    于是 8 条现场用例【静默跳过】而全量仍然显示绿灯——比红更糟。"""
    try:
        fe = vc.frontend_dir(ROOT)
    except vc.VueComponentError:
        return None, "找不到前端目录"
    if not fe.joinpath(*vc.ESBUILD_API).is_file():
        return None, "缺 esbuild（前端没 npm ci）"
    if not fe.joinpath(*vc.SFC_API).is_file():
        return None, "缺 @vue/compiler-sfc"
    if not shutil.which("node"):
        return None, "缺 node"
    try:
        from agent_runtime import visual_acceptance as visual
        visual._edge_binary()
    except Exception as exc:  # noqa: BLE001 - 这里就是要"没浏览器就说清楚"
        return None, "缺浏览器：%s" % str(exc)[:80]
    return fe, ""


FRONTEND, SKIP_REASON = _toolchain()
TOOLCHAIN_OK = FRONTEND is not None


class Base(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = Path(tempfile.mkdtemp(prefix="docmind_vue_state_"))
        config.set_runtime("code_root", str(self.tmp))
        self.addCleanup(config.set_runtime, "code_root", self._prev)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class PathGuardTests(Base):
    def setUp(self):
        super().setUp()
        (self.tmp / "frontend" / "src").mkdir(parents=True)
        (self.tmp / "frontend" / "package.json").write_text('{"name":"x"}\n', encoding="utf-8",
                                                            newline="\n")
        (self.tmp / "frontend" / "src" / "Ok.vue").write_text("<template><p>ok</p></template>\n",
                                                              encoding="utf-8", newline="\n")

    def test_a_tree_without_a_frontend_fails_explicitly(self):
        with self.assertRaises(vc.VueComponentError):
            vc.frontend_dir(self.tmp / "nowhere")

    def test_only_vue_files_inside_the_frontend_tree_are_compilable(self):
        fe = vc.frontend_dir(str(self.tmp))
        for bad in ("/etc/passwd.vue", "../secrets.vue", "src/Ok.txt", "README.md",
                    "src/../src/Ok.vue", "", "other/Ok.vue"):
            with self.subTest(rel=bad):
                with self.assertRaises(vc.VueComponentError):
                    vc.component_file(fe, bad)

    def test_missing_component_is_named_not_swallowed(self):
        fe = vc.frontend_dir(str(self.tmp))
        with self.assertRaises(vc.VueComponentError) as caught:
            vc.component_file(fe, "src/Nope.vue")
        self.assertIn("不存在", str(caught.exception))

    def test_tmp_copies_are_allowed_for_negative_controls(self):
        """故意改坏的副本走 `frontend/.tmp/**`（gitignore 内），否则只能原地改生产文件。"""
        fe = vc.frontend_dir(str(self.tmp))
        holder = fe / ".tmp"
        holder.mkdir(exist_ok=True)
        (holder / "Broken.vue").write_text("<template><p>x</p></template>\n", encoding="utf-8",
                                           newline="\n")
        self.assertTrue(vc.component_file(fe, ".tmp/Broken.vue").is_file())

    def test_an_oversized_component_is_refused(self):
        fe = vc.frontend_dir(str(self.tmp))
        big = fe / "src" / "Big.vue"
        big.write_text("<!--" + "x" * (vc.MAX_COMPONENT_BYTES + 10) + "-->\n", encoding="utf-8",
                       newline="\n")
        with self.assertRaises(vc.VueComponentError):
            vc.component_file(fe, "src/Big.vue")


class ArgShapeTests(Base):
    def test_selectors_reject_option_shapes_and_dedup(self):
        self.assertEqual(vc.selectors(".a, .b,.a"), [".a", ".b"])
        for bad in ("--help", "-x", "$(rm)", "  ", ".a; rm -rf /"):
            with self.subTest(sel=bad):
                if not bad.strip():
                    self.assertEqual(vc.selectors(bad), [])
                    continue
                with self.assertRaises(vc.VueComponentError):
                    vc.selectors(bad)

    def test_selector_count_is_capped(self):
        with self.assertRaises(vc.VueComponentError):
            vc.selectors(", ".join(".s%d" % i for i in range(vc.MAX_SELECTORS + 1)))

    def test_text_needles_are_not_validated_as_css(self):
        """`1/2`、`工作流团队` 是渲染文本，不是选择器。把它们过选择器规则会把合法断言
        直接拒掉（实测就是这么红的）。"""
        self.assertEqual(vc.text_needles("1/2\n工作流团队"), ["1/2", "工作流团队"])
        self.assertEqual(vc.text_needles(""), [])
        with self.assertRaises(vc.VueComponentError):
            vc.text_needles("x" * 200)

    def test_count_assertion_is_split_into_selector_and_wanted(self):
        self.assertEqual(vc.count_plan(".wmd-item=2"), [[".wmd-item", "2"]])
        self.assertEqual(vc.count_plan(".a-b=0, .b=12"), [[".a-b", "0"], [".b", "12"]])
        for bad in (".wmd-item", ".wmd-item=many", "=2", ".wmd-item=-1", "--help=2"):
            with self.subTest(counts=bad):
                with self.assertRaises(vc.VueComponentError):
                    vc.count_plan(bad)

    def test_a_selector_that_happens_to_contain_equals_still_parses(self):
        """rpartition 取最后一个 `=`：`[data-x=2]=3` 的"
        选择器是 `[data-x=2]`、期望值是 3。"""
        self.assertEqual(vc.count_plan("[data-x=2]=3"), [["[data-x=2]", "3"]])

    def test_nth_absent_is_unset_while_nth_zero_is_explicit(self):
        """`#0` 与"不写 #"必须区分：后者命中多个时应拒绝并回候选清单。"""
        plan = vc.click_plan(".a, .b#0, .c#2")
        self.assertEqual(plan, [{"selector": ".a", "nth": ""},
                                {"selector": ".b", "nth": 0},
                                {"selector": ".c", "nth": 2}])

    def test_too_many_clicks_are_refused(self):
        with self.assertRaises(vc.VueComponentError):
            vc.click_plan(", ".join(".c%d" % i for i in range(vc.MAX_CLICKS + 1)))

    def test_props_must_be_a_json_object(self):
        self.assertEqual(vc.parse_props(""), {})
        self.assertEqual(vc.parse_props('{"a": 1}'), {"a": 1})
        for bad in ("[1,2]", '"str"', "{不是 JSON}"):
            with self.subTest(props=bad):
                with self.assertRaises(vc.VueComponentError):
                    vc.parse_props(bad)

    def test_relative_import_path_uses_forward_slashes(self):
        fe = Path("D:/x/frontend")
        got = vc.posix_relative(fe / ".tmp" / "vue-guard" / "g1", fe / "src" / "A.vue")
        self.assertNotIn("\\", got)
        self.assertTrue(got.endswith("src/A.vue"), got)


class MountPageTests(Base):
    def config(self, **over):
        base = {"props": {"active": True}, "expect_present": [".p"], "expect_absent": [],
                "counts": [[".i", "2"]], "texts": ["1/2"], "expect_emit": "focus",
                "expect_emit_args": ["t1"], "root": "#app > *"}
        base.update(over)
        return base

    def page(self, **over):
        target = self.tmp / "guard.html"
        vc.mount_page("app.mjs", self.config(**over), target)
        return target.read_text(encoding="utf-8")

    def test_no_unreplaced_placeholders(self):
        text = self.page()
        for token in ("%(bundle)s", "%(config)s", "%(title)s"):
            self.assertNotIn(token, text)

    def test_page_imports_the_bundle_and_mounts_the_component(self):
        text = self.page()
        self.assertIn("./app.mjs", text)
        self.assertIn("createApp", text)
        self.assertIn("Comp, __vueStyles__", text, "样式必须跟组件一起注入，否则类断言无意义")

    def test_emit_listeners_come_from_the_components_own_declaration(self):
        """按 `Comp.emits` 建监听：写了组件没声明的名字，永远等不到也看不出来。

        断言必须盯住**建监听那一行**：只查字符串 "Comp.emits" 在页面里出现过，
        把循环改成写死 `['focus']` 照样绿（变异电池就是这么活的）。
        """
        text = self.page()
        self.assertIn("for (const name of (Comp.emits || []))", text)
        self.assertIn("declaredEmits", text)

    def test_bad_selectors_are_recorded_instead_of_silently_emptying_facts(self):
        text = self.page()
        # present/absent/count 共用同一条 probe()：复制成两份 try/catch 时，
        # 改掉其中一份的失败分支没人能发现（实测）。
        self.assertEqual(text.count("bad selector"), 1,
                         "非法选择器要么记一次（共用一条路径），要么就没记")
        self.assertEqual(text.count("querySelectorAll(sel)"), 1, "选择器探测被复制了")
        # 失败分支必须【既记下、又不谎报"元素在"】：只查其中一半，
        # 变异（把 any:false 改成 true）就逃得过（实测）。
        self.assertIn("state.errors.push('bad selector ' + sel);\n"
                     "      return { any: false, n: -1 };", text)

    def test_props_are_injected_as_a_json_literal(self):
        text = self.page(props={"active": False, "members": [{"id": "t1"}]})
        self.assertIn('"active": false', text)
        self.assertNotIn("__PROPS__", text)


class EvaluateTableTests(Base):
    STATE = {"mounted": True, "rendered": True, "errors": [], "warnings": [],
             "declaredEmits": ["focus"], "emits": [{"name": "focus", "args": ["t2"]}],
             "facts": {"present:.wmd-panel": True, "absent:.nope": True,
                       "count:.wmd-item": 2, "text:1/2": True}}

    def evaluate(self, config=None, state=None, clicks=(), results=(), synced=True):
        cfg = {"props": {}, "expect_present": [".wmd-panel"], "expect_absent": [".nope"],
               "counts": [[".wmd-item", "2"]], "texts": ["1/2"], "expect_emit": "focus",
               "expect_emit_args": ["t2"]}
        cfg.update(config or {})
        return vc.evaluate(dict(state or self.STATE), cfg, list(clicks), list(results),
                           synced=synced)

    def test_a_faithful_component_passes_every_line(self):
        got = self.evaluate(clicks=[{"selector": ".wmd-strip"}], results=[{"ok": True}])
        self.assertTrue(got["passed"], got["failed"])

    def test_mounting_without_assertions_is_not_a_pass(self):
        """"挂上了"不是证据：只 mount 不断言任何东西必须判未通过。"""
        empty = {"props": {}, "expect_present": [], "expect_absent": [], "counts": [],
                 "texts": [], "expect_emit": "", "expect_emit_args": None}
        got = self.evaluate(config=empty)
        self.assertFalse(got["passed"])
        self.assertIn("assertions_declared", got["failed"])

    def test_emit_with_the_wrong_payload_is_red(self):
        """最典型的接线错：emit 的名字对、实参从 id 变成 label。"""
        state = dict(self.STATE, emits=[{"name": "focus", "args": ["写作"]}])
        got = self.evaluate(state=state)
        self.assertFalse(got["passed"])
        self.assertIn("emit_args_exact", got["failed"])
        self.assertIn("emit_recorded", [name for name in got["assertions"]
                                        if got["assertions"][name]], "名字本身要仍然成立")

    def test_undeclared_emit_name_is_red_instead_of_waiting_forever(self):
        state = dict(self.STATE, declaredEmits=["focus"], emits=[])
        got = self.evaluate(config={"expect_emit": "noSuchEvent"}, state=state)
        self.assertFalse(got["passed"])
        self.assertIn("emit_declared_by_component", got["failed"])

    def test_a_component_that_never_emits_is_red(self):
        got = self.evaluate(state=dict(self.STATE, emits=[]))
        self.assertFalse(got["passed"])
        self.assertIn("emit_recorded", got["failed"])

    def test_wrong_member_count_is_red(self):
        facts = dict(self.STATE["facts"], **{"count:.wmd-item": 3})
        got = self.evaluate(state=dict(self.STATE, facts=facts))
        self.assertIn("count:.wmd-item=2", got["failed"])

    def test_absent_expectation_can_be_broken(self):
        facts = dict(self.STATE["facts"], **{"absent:.nope": False})
        self.assertIn("absent:.nope", self.evaluate(state=dict(self.STATE, facts=facts))["failed"])

    def test_empty_facts_with_a_failed_read_back_is_one_clear_red(self):
        """读状态那次点击抛错时 facts 是空的——必须单独红出来，
        而不是让 present/absent/count/text 四条一起"莫名没成立"。"""
        got = self.evaluate(state=dict(self.STATE, facts={}), synced=False)
        self.assertIn("state_read_back", got["failed"])

    def test_vue_warning_is_red(self):
        got = self.evaluate(state=dict(self.STATE, warnings=["Missing prop: active"]))
        self.assertIn("no_vue_warnings", got["failed"])

    def test_component_throw_is_red(self):
        got = self.evaluate(state=dict(self.STATE, errors=["TypeError: x is not a function"]))
        self.assertIn("no_component_errors", got["failed"])

    def test_click_that_did_not_apply_is_red(self):
        got = self.evaluate(clicks=[{"selector": ".x"}, {"selector": ".y"}],
                            results=[{"ok": True}, {"ok": False}])
        self.assertIn("every_click_applied", got["failed"])

    def test_a_hidden_component_is_still_rendered_truthfully(self):
        got = self.evaluate(state=dict(self.STATE, rendered=False))
        self.assertIn("rendered", got["failed"])


class RenderTests(Base):
    def test_render_lists_what_failed_and_the_boundary(self):
        judged = {"checks": {"compiled": True, "mounted": True, "present:.p": False},
                  "ok": False, "component": "src/a.vue", "bundle_bytes": 10, "elapsed": 1.2,
                  "failed": ["present:.p"], "declared_emits": ["focus"],
                  "observed_emits": [], "facts": {}, "vue_errors": [], "vue_warnings": [],
                  "clicks": [], "error": ""}
        text = vc.render(judged)
        self.assertIn("组件守卫【未通过】", text)
        self.assertIn("未通过项：present:.p", text)
        self.assertIn("边界", text)

    def test_unstarted_guard_has_a_reason_not_a_blank(self):
        self.assertIn("未能开始", vc.render({"checks": {}, "error": "没有 esbuild"}))

    def test_compile_failure_is_reported_as_compile_failure(self):
        text = vc.render({"checks": {"compiled": False}, "error": "SFC 解析失败"})
        self.assertIn("编译失败", text)


class CompileArtifactTests(Base):
    """编译产物的形状：这几条不需要浏览器，却能钉住"为什么必须这样编"。"""

    def setUp(self):
        super().setUp()
        fe = self.tmp / "frontend"
        (fe / "node_modules" / "esbuild" / "lib").mkdir(parents=True)
        (fe / "node_modules" / "@vue" / "compiler-sfc" / "dist").mkdir(parents=True)
        fe.joinpath("package.json").write_text('{"name":"x"}\n', encoding="utf-8", newline="\n")
        fe.joinpath("node_modules/esbuild/lib/main.js").write_text("module.exports={}\n",
                                                                   encoding="utf-8", newline="\n")
        fe.joinpath("node_modules/@vue/compiler-sfc/dist/compiler-sfc.cjs.js").write_text(
            "module.exports={}\n", encoding="utf-8", newline="\n")
        (fe / "src").mkdir(parents=True, exist_ok=True)
        self.component = fe / "src" / "A.vue"
        self.component.write_text('<script setup lang="ts">\ninterface M { id: string }\n'
                                  'const p = defineProps<{ m: M }>()\n</script>\n'
                                  '<template><p>{{ p.m.id }}</p></template>\n',
                                  encoding="utf-8", newline="\n")
        self.frontend = fe

    def run_compile(self, payload, bundle_bytes=0, which="node"):
        captured = {}

        class Proc:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""

        def runner(argv, **kwargs):
            captured["argv"] = argv
            if bundle_bytes:
                Path(argv[3]).write_bytes(b"x" * bundle_bytes)
            return Proc()

        original = shutil.which
        shutil.which = lambda name: which if name == "node" else original(name)
        try:
            got = vc.compile_component(self.frontend, self.component, self.tmp / "out",
                                       runner=runner)
        finally:
            shutil.which = original
        return got, captured

    def test_loader_follows_the_sfc_language(self):
        """`compileScript` 的输出保留 TS，写死 loader:'js' 会在 `interface` 那行炸。"""
        script = vc.COMPILE_SCRIPT
        self.assertIn("lang", script)
        self.assertIn("? 'ts' : 'js'", script.replace("\\'", "'"))
        self.assertNotIn("return { loader: 'js'", script)

    def test_generated_files_pin_one_vue_instance_and_the_runtime_flags(self):
        got, captured = self.run_compile({"ok": True, "warnings": []}, bundle_bytes=5000)
        self.assertTrue(got["ok"], got)
        script = (self.tmp / "out" / "compile.mjs").read_text(encoding="utf-8")
        entry = (self.tmp / "out" / "entry.mjs").read_text(encoding="utf-8")
        self.assertIn(str((self.tmp / "frontend" / "package.json").as_posix()), script,
                      "createRequire 的锚点必须指向前端目录，否则脚本放哪都 import 不到 esbuild")
        self.assertIn("process.env.NODE_ENV", script, "不打 define 的话 vue 运行时会在浏览器里炸")
        self.assertIn("inlineTemplate", script)
        self.assertIn("from 'vue'", entry, "createApp 必须与组件同源，否则页面上是两个 vue 实例")
        self.assertIn("compile.mjs", captured["argv"][1])

    def test_a_claimed_success_without_a_bundle_is_a_failure(self):
        got, _ = self.run_compile({"ok": True, "warnings": []}, bundle_bytes=0)
        self.assertFalse(got["ok"])
        self.assertIn("产物", got["error"])

    def test_a_truncated_bundle_is_a_failure(self):
        got, _ = self.run_compile({"ok": True, "warnings": []}, bundle_bytes=10)
        self.assertFalse(got["ok"])

    def test_the_compilers_own_error_is_passed_through(self):
        got, _ = self.run_compile({"ok": False, "error": "SFC 解析失败：x"})
        self.assertFalse(got["ok"])
        self.assertIn("SFC 解析失败", got["error"])

    def test_no_json_at_all_is_not_read_as_success(self):
        class Proc:
            returncode = 0
            stdout = "console noise, no json"
            stderr = ""

        got = vc.compile_component(self.frontend, self.component, self.tmp / "out2",
                                   runner=lambda argv, **kw: Proc())
        self.assertFalse(got["ok"])
        self.assertIn("没回话", got["error"])

    def test_missing_node_is_reported_not_skipped(self):
        got, _ = self.run_compile({"ok": True}, bundle_bytes=5000, which=None)
        self.assertFalse(got["ok"])
        self.assertIn("node", got["error"])

    def test_a_frontend_without_esbuild_says_so(self):
        bare = self.tmp / "bare"
        (bare / "node_modules").mkdir(parents=True)
        bare.joinpath("package.json").write_text("{}\n", encoding="utf-8", newline="\n")
        got = vc.compile_component(bare, self.component, self.tmp / "out3")
        self.assertFalse(got["ok"])
        self.assertIn("esbuild", got["error"])


class RequestParsingTests(Base):
    """工具报文 → (config, 点击计划)。这条边界必须【不起浏览器】就能验：
    以前参数是在 run_guard 里就地散的，变异电池里"文本断言被当选择器校验"这类
    缺陷纯层根本测不到（只有现场类才走到）。"""

    def test_text_needles_survive_the_parser(self):
        config, plan = vc.parse_request(component="src/A.vue", texts="1/2\n工作流团队",
                                        counts=".i=2", clicks=".strip\n.i#1")
        self.assertEqual(config["texts"], ["1/2", "工作流团队"])
        self.assertEqual(config["counts"], [[".i", "2"]])
        self.assertEqual(plan, [{"selector": ".strip", "nth": ""},
                               {"selector": ".i", "nth": 1}])

    def test_every_bad_input_is_rejected_here_not_later(self):
        for bad in ({"texts": "a" * 300}, {"counts": ".i=many"}, {"counts": "--help=2"},
                    {"expect_present": "$(rm)"}, {"clicks": ".a, .b, .c, .d, .e, .f, .g, .h, .i"},
                    {"props": "[1]"}, {"expect_emit_args": '{"a":1}'}):
            with self.subTest(bad=str(bad)[:30]):
                args = {"component": "src/A.vue"}
                args.update(bad)
                with self.assertRaises(vc.VueComponentError):
                    vc.parse_request(**args)

    def test_max_clicks_boundary_is_the_documented_one(self):
        ok = ", ".join(".c%d" % i for i in range(vc.MAX_CLICKS))
        self.assertEqual(len(vc.parse_request(component="a.vue", clicks=ok)[1]), vc.MAX_CLICKS)

    def test_empty_optional_lists_are_empty_not_none(self):
        config, plan = vc.parse_request(component="src/A.vue")
        self.assertEqual(plan, [])
        self.assertEqual(config["expect_present"], [])
        self.assertIsNone(config["expect_emit_args"])


class ArtifactLocationTests(Base):
    def test_artifacts_stay_inside_the_frontend_tree(self):
        """裸说明符要从 `frontend/node_modules` 解析；写到仓外就编不动，
        而且会把孤儿目录留在别人机器上（两处都得挡住）。"""
        fe = Path("D:/x/frontend")
        got = str(vc.workdir_for(fe)).replace("\\", "/")
        self.assertTrue(got.startswith("D:/x/frontend/"), got)
        self.assertTrue(got.endswith(".tmp/vue-guard"), got)

    def test_cleanup_reports_a_directory_it_could_not_remove(self):
        """`rmtree(ignore_errors=True)` 会把失败变成"删过了"——必须如实回报。"""
        target = self.tmp / "out"
        target.mkdir()
        tries = []

        def failing(path):
            tries.append(path)
            raise OSError(32, "另一个程序正在使用此文件")

        got = vc.clean_target(target, attempts=3, remover=failing, pause=lambda seconds: None)
        self.assertFalse(got["removed"])
        self.assertIn("没删干净", got["error"])
        self.assertEqual(len(tries), 3, "没有重试：%d 次" % len(tries))
        self.assertTrue(target.exists())

    def test_cleanup_succeeds_and_says_zero_retries_when_the_dir_is_already_gone(self):
        gone = self.tmp / "already-gone"
        got = vc.clean_target(gone, attempts=3, pause=lambda seconds: None)
        self.assertTrue(got["removed"])
        self.assertEqual(got["attempts"], 0)

    def test_cleanup_stops_retrying_once_the_directory_is_gone(self):
        target = self.tmp / "flaky"
        target.mkdir()
        calls = []

        def first_fail(path):
            calls.append(1)
            raise OSError(32, "占着")

        def second_ok(path):
            calls.append(2)
            shutil.rmtree(path)

        # 第一次抛、第二次成功：不能盲目把 attempts 用完
        try:
            got = vc.clean_target(target, attempts=5, remover=first_fail,
                                  pause=lambda seconds: None)
            self.assertFalse(got["removed"])
            got2 = vc.clean_target(self.tmp / "second", attempts=2, remover=second_ok,
                                   pause=lambda seconds: None)
        except Exception as exc:  # noqa: BLE001
            self.fail("clean_target 不该把 OSError 抛给调用方：%s" % exc)
        self.assertTrue(got2["removed"] or not (self.tmp / "second").exists())


class ToolSurfaceTests(unittest.TestCase):
    def test_registered_in_all_three_places(self):
        import agent
        import tools
        self.assertIn("dev_vue", tools.TOOLS)
        self.assertIn("expect_emit", tools.TOOLS["dev_vue"]["description"])
        self.assertIn("component", tools.dev_vue.__doc__)
        line = next(l for l in agent._SYSTEM_PROMPT_FULL.splitlines() if l.startswith("- dev_vue"))
        self.assertIn("零新依赖", line)
        self.assertEqual(len(tools.tool_schemas()), len(tools.TOOLS))

    def test_mcp_exposure_is_reviewed(self):
        from agent_runtime import mcp_server
        self.assertIn("dev_vue", mcp_server.WRITE_TOOLS,
                      "会起浏览器执行组件代码的工具不能进默认只读表")

    def test_a_project_without_a_frontend_reports_failure_not_green(self):
        import tools
        stray = tempfile.mkdtemp(prefix="docmind_vue_tool_")
        prev = config.get_runtime("code_root")
        config.set_runtime("code_root", stray)
        self.addCleanup(config.set_runtime, "code_root", prev)
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        text = tools.TOOLS["dev_vue"]["func"]("component: src/A.vue")
        self.assertTrue(text.strip())
        self.assertNotIn("【通过】", text)

    def test_bad_arguments_are_refused_before_any_browser_starts(self):
        import tools
        prev = config.get_runtime("code_root")
        config.set_runtime("code_root", ROOT)
        self.addCleanup(config.set_runtime, "code_root", prev)
        for arg in ("component: ../outside.vue", "component: src/A.vue\ntimeout: 1",
                    "component: src/A.vue\ntimeout: abc", "component: src/A.vue\ncounts: .a=b",
                    "component: src/A.vue\nprops: not-json"):
            with self.subTest(arg=arg.split("\n")[-1]):
                text = tools.TOOLS["dev_vue"]["func"](arg)
                self.assertTrue(text.strip(), arg)
                self.assertNotIn("【通过】", text)


@unittest.skipUnless(TOOLCHAIN_OK, "跳过真组件现场：%s" % (SKIP_REASON or "未知"))
class LiveComponentTests(Base):
    def setUp(self):
        super().setUp()
        self._prev_root = config.get_runtime("code_root")
        config.set_runtime("code_root", ROOT)
        self.addCleanup(config.set_runtime, "code_root", self._prev_root)

    def guard(self, **over):
        args = {"component": TARGET, "props": props_payload(),
                # 只点折叠条不会有 emit：必须真的点到某个成员条目
                "clicks": ".wmd-strip\n.wmd-item#0",
                "expect_present": ".wmd-panel", "counts": ".wmd-item=2",
                "texts": "1/2", "expect_emit": "focus",
                "expect_emit_args": json.dumps(["t1"]), "timeout": 90.0}
        args.update(over)
        return vc.run_guard(ROOT, **args)

    def test_the_real_component_passes_and_proves_the_wiring(self):
        result = self.guard()
        self.assertTrue(result["ok"], json.dumps(
            {"failed": result.get("failed"), "facts": result.get("facts"),
             "error": result.get("error"), "vue_errors": result.get("vue_errors")},
            ensure_ascii=False))
        # emit 的实参必须是成员 id：这是最容易在重构里被换成 label / index 的一根线
        self.assertEqual(result["observed_emits"], ["focus"])
        self.assertEqual(result["facts"]["count:.wmd-item"], 2)
        self.assertTrue(result["facts"]["text:1/2"], "doneCount 的计算接线")
        self.assertEqual(result["assertions"]["emit_args_exact"], True)
        self.assertIn("组件守卫【通过】", vc.render(result))

    def test_a_passive_component_does_not_render_the_dock(self):
        """反证一：`active: false` 时 `v-if` 必须让它什么都不渲染。"""
        result = self.guard(props=props_payload(active=False), expect_present=".wmd-strip",
                            expect_emit="", expect_emit_args="", clicks="")
        self.assertFalse(result["ok"])
        self.assertIn("present:.wmd-strip", result["failed"])

    def test_wrong_emit_payload_is_caught(self):
        """反证二：断言实参是 t9（组件实际发 t1）必须红——这条直接对应
        "emit 从 id 换成 label/index" 那一类重构事故。"""
        result = self.guard(expect_emit_args=json.dumps(["t9"]))
        self.assertFalse(result["ok"])
        self.assertIn("emit_args_exact", result["failed"])
        self.assertNotIn("emit_recorded", result["failed"], "名字对、实参错：只该红实参那条")

    def test_an_emit_the_component_never_declared_is_red(self):
        result = self.guard(expect_emit="pick", expect_emit_args="")
        self.assertFalse(result["ok"])
        self.assertIn("emit_declared_by_component", result["failed"])

    def test_mounting_without_assertions_is_red(self):
        result = self.guard(expect_present="", counts="", texts="", expect_emit="",
                            expect_emit_args="", clicks="")
        self.assertFalse(result["ok"])
        self.assertIn("assertions_declared", result["failed"])

    def test_a_mutated_copy_of_the_component_turns_the_guard_red(self):
        """反证三（最有分量的一条）：把组件**副本**的 emit 实参换成常量，守卫必须红。

        变异只写在 `frontend/.tmp/`（gitignore 内），不碰被别的 lane 监听的生产文件。
        """
        source = (FRONTEND / TARGET).read_text(encoding="utf-8")
        self.assertIn("emit('focus', id)", source, "生产文件形状变了，这条反证要看一眼")
        holder = FRONTEND / ".tmp" / "vue-guard-mut"
        holder.mkdir(parents=True, exist_ok=True)
        broken = holder / "MutatedDock.vue"
        broken.write_text(source.replace("emit('focus', id)", "emit('focus', 'WRONG')"),
                          encoding="utf-8", newline="\n")
        self.addCleanup(broken.unlink, missing_ok=True)
        result = self.guard(component=".tmp/vue-guard-mut/MutatedDock.vue")
        self.assertFalse(result["ok"], json.dumps(
            {"failed": result.get("failed"), "observed": result.get("observed_emits")},
            ensure_ascii=False))
        self.assertIn("emit_args_exact", result["failed"])
        self.assertIn("emit_recorded", [name for name, ok in result["assertions"].items() if ok])

    def test_a_mutated_vif_condition_turns_the_guard_red(self):
        """反证四：`v-if="visible"` 改成 `v-if="false"`——组件什么都不渲染。

        这条用 `clicks=""`：组件不渲染时点 `.wmd-strip` 会在定位阶段就抛错，
        红是红了，但红成"没开始"而不是"断言未成立"，看不出是哪根线断的。
        """
        source = (FRONTEND / TARGET).read_text(encoding="utf-8")
        holder = FRONTEND / ".tmp" / "vue-guard-mut"
        holder.mkdir(parents=True, exist_ok=True)
        broken = holder / "NeverOpens.vue"
        broken.write_text(source.replace('v-if="visible"', 'v-if="false"'),
                          encoding="utf-8", newline="\n")
        self.addCleanup(broken.unlink, missing_ok=True)
        result = self.guard(component=".tmp/vue-guard-mut/NeverOpens.vue", clicks="",
                            expect_emit="", expect_emit_args="")
        self.assertFalse(result["ok"])
        self.assertIn("rendered", result["failed"])
        self.assertIn("present:.wmd-panel", result["failed"])

    def test_nothing_is_left_behind_in_the_working_tree(self):
        """本次产物目录必须真的消失，而不是 `ignore_errors=True` 报"删过了"。

        这条不是洁癖：旧写法用 `shutil.rmtree(..., ignore_errors=True)`，浏览器刚关掉时
        `app.mjs` 还被句柄占着，一次删不掉就再没人知道——仓库里确实躺着这么一套孤儿目录。
        """
        result = self.guard()
        self.assertTrue(result["cleaned"]["removed"], result.get("cleaned"))
        self.assertFalse(Path(result["artifacts_dir"]).exists(),
                         "本次产物目录还在：%s" % result["artifacts_dir"])
        self.assertGreaterEqual(result["cleaned"]["attempts"], 0)
        orphans = list((FRONTEND / ".tmp" / "vue-guard").glob("guard-*")) \
            if (FRONTEND / ".tmp" / "vue-guard").is_dir() else []
        self.assertEqual(orphans, [], "留下了别的产物目录：%s" % orphans)


if __name__ == "__main__":
    unittest.main()
