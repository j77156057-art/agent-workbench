"""变异电池的【元测试】——不跑变异，只查电池有没有和源码脱钩，以及引擎自己有没有牙。

为什么需要这一份：电池的失效方式不是"红"，而是**悄悄不跑了**——锚点被重构掉、测试类改名、
现场类整类 skip。这些都会让"14/14 抓住"变成一句空话而没有任何东西变红。
本会话真撞见过两次同型的：判据用 `.is_file()` 查目录导致 8 条现场用例静默跳过而全量仍显示绿灯。
"""

import importlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.mutation import engine  # noqa: E402
from tests.mutation import run_all  # noqa: E402

#: 我这块程序该有的五块电池（别的 lane 加新电池只要自己在文件里声明
#: `MINIMUM_MUTATIONS`，不需要来这里登记——共享工作树里不该有一张中央登记表）
MINE = ("voice_loop", "head_check", "test_run", "vue", "app_test",
        "bat_guard")


def fake_battery(mutations, live=()):
    return SimpleNamespace(BATTERY_NAME="fake", MODULE="docmind_fake_mutated_module",
                           TARGET="agent_runtime/capacity.py", CASE_ID="unused",
                           TEST_MODULE="tests.test_mutation_batteries", BIND="at",
                           UNIT_CLASSES=(), LIVE=live, MUTATIONS=mutations)


class EngineJudgementTests(unittest.TestCase):
    """引擎的判定逻辑本身要被钉住——不然"全部抓住"这个结论没有可信度。"""

    SOURCE = "VALUE = 1\nOTHER = 2\n"

    def run_it(self, battery, layers, allow_live=True):
        return engine.run_battery(battery, layers=layers, source=self.SOURCE,
                                  allow_live=allow_live, verbose=False)

    def layer(self, pristine=([], 1), mutated=([], 1)):
        """按模块分身的假层：对照组跑的是未变异的模块（`VALUE == 1`）。
        不分开写就会踩一个真陷阱——对照组红的时候引擎**整块不作数**，
        那两条测试就成了"什么都没验"。"""
        def run(module):
            return tuple(mutated if getattr(module, "VALUE", 1) != 1 else pristine)
        return run

    def test_a_mutant_no_layer_catches_is_reported_as_survivor(self):
        battery = fake_battery([("没人查", "VALUE = 1", "VALUE = 9")])
        clean = lambda _m: ([], 1)
        report = self.run_it(battery, {"纯层": clean, "门禁": clean})
        self.assertEqual(report["survivors"], ["没人查"])
        self.assertEqual(report["caught"], 0)
        self.assertFalse(engine.is_green(report))

    def test_a_mutant_that_does_not_compile_is_a_problem_not_a_catch(self):
        """变异把模块改到装不进 = **这条变异自己写歪了**，不能当成"守卫抓住了它"。

        本会话撞上两次：① 一条只替换了半句的变异让整块电池崩在 IndentationError 上；
        ② 更糟的是我第一版修法把它记成"抓到"——那等于给分数白送一分，
        而分数正是这块电池唯一在产出的东西。事前检查 + 不作数，才是对的。
        """
        battery = fake_battery([("改到装不进", "VALUE = 1", "VALUE = (")])
        clean = lambda _m: ([], 1)
        report = self.run_it(battery, {"纯层": clean, "门禁": clean})
        self.assertEqual(report["caught"], 0, report)
        self.assertEqual(report["survivors"], [], "崩掉的变异不该被算成活下来，也不该算抓住")
        self.assertTrue([row for row in report["problems"] if "编译不过" in row], report["problems"])
        self.assertFalse(engine.is_green(report))

    def test_a_control_group_that_is_red_voids_the_whole_battery(self):
        """环境在红的时候，每条变异都会"被抓到"——那必须是【不作数】而不是满分。"""
        battery = fake_battery([("任何一条", "VALUE = 1", "VALUE = 9"),
                                ("另一条", "OTHER = 2", "OTHER = 9")])
        always_fails = lambda _m: (["某条测试本来就红"], 3)
        report = self.run_it(battery, {"纯层": always_fails, "门禁": always_fails})
        self.assertEqual(report["caught"], 0)
        self.assertEqual(report["survivors"], [], "对照组红的时候不该逐条判定")
        self.assertTrue([p for p in report["problems"] if "对照组" in p], report["problems"])
        self.assertFalse(engine.is_green(report))

    def test_a_live_only_mutant_without_a_browser_is_unverified_not_caught(self):
        battery = fake_battery([("只有现场能验", "VALUE = 1", "VALUE = 9", ("live",))],
                               live=("LiveX", ("test_a",)))
        clean = lambda _m: ([], 1)
        offline = self.run_it(battery, {"纯层": clean, "门禁": clean}, allow_live=False)
        self.assertEqual(offline["caught"], 0)
        self.assertEqual(offline["unverified"], ["只有现场能验"])
        self.assertTrue(engine.is_green(offline), "没验到不是失败，但也不许算抓住")
        skipped = self.run_it(battery, {"纯层": self.layer(), "门禁": self.layer(),
                                        "现场": self.layer(mutated=([], 0))})
        self.assertEqual(skipped["unverified"], ["只有现场能验"],
                         "现场层全类 skip（跑了 0 条）不能算抓住了")

    def test_a_live_layer_that_goes_red_does_catch_it(self):
        battery = fake_battery([("只有现场能验", "VALUE = 1", "VALUE = 9", ("live",))],
                               live=("LiveX", ("test_a",)))
        report = self.run_it(battery, {"纯层": self.layer(), "门禁": self.layer(),
                                       "现场": self.layer(
                                           mutated=(["LiveX.test_a"], 1))})
        self.assertEqual(report["caught"], 1)
        self.assertEqual(report["unverified"], [])

    def test_stale_or_noop_mutations_are_problems_not_skips(self):
        battery = fake_battery([("锚点没了", "THIS_IS_NOT_IN_THE_SOURCE = 1", "x = 2"),
                                ("等于没变异", "VALUE = 1", "VALUE = 1")])
        report = self.run_it(battery, {"纯层": lambda _m: ([], 1)})
        self.assertEqual(len(report["problems"]), 2, report["problems"])
        self.assertEqual(report["caught"], 0)
        self.assertFalse(engine.is_green(report))

    def test_marking_a_mutant_live_requires_a_declared_live_layer(self):
        battery = fake_battery([("标了现场", "VALUE = 1", "VALUE = 9", ("live",))], live=())
        problems = engine.anchors_valid(battery, self.SOURCE)
        self.assertTrue([p for p in problems if "没声明 LIVE" in p], problems)

    def test_empty_battery_is_a_problem(self):
        self.assertTrue(engine.anchors_valid(fake_battery([]), self.SOURCE))

    def test_a_control_layer_that_never_ran_voids_the_battery(self):
        """对照组"跑了 0 条"与"跑红了"一样致命：整类 skip 的环境里没有任何东西被验证。"""
        battery = fake_battery([("任何一条", "VALUE = 1", "VALUE = 9")])
        report = self.run_it(battery, {"纯层": self.layer(pristine=([], 0)),
                                       "门禁": self.layer()})
        self.assertEqual(report["caught"], 0)
        self.assertTrue([p for p in report["problems"] if "没跑" in p], report["problems"])
        self.assertFalse(engine.is_green(report))

    def test_mutants_that_silently_vanish_are_not_green(self):
        """总数对不上就是"有变异悄悄没被判定"——这比红更糟，因为它看起来像满分。"""
        report = {"name": "x", "total": 3, "caught": 1, "survivors": [], "unverified": [],
                  "problems": [], "control": {}, "wall": 1.0, "skipped_live": 0}
        self.assertFalse(engine.is_green(report), "3 条里只判了 1 条却没有任何红")


class BatteryDeclarationTests(unittest.TestCase):
    """五份真电池的体检：源码、测试类、门禁题号、现场用例名——任何一项改名都会红在这里。"""

    def batteries(self):
        return run_all.discover()

    def test_discovery_finds_every_battery_i_own(self):
        names = sorted(b.BATTERY_NAME for b in self.batteries())
        for mine in MINE:
            self.assertIn(mine, names, "我的电池不见了：%s（现有 %s）" % (mine, names))

    def test_every_battery_declares_its_own_floor(self):
        """删一条变异必须是显式的：电池自己声明 `MINIMUM_MUTATIONS`。

        别的 lane 新加的电池也走同一条规则——不需要谁去改中央清单，
        所以共享工作树里不会因为"多了一块电池"而互相挡路。
        """
        for battery in self.batteries():
            with self.subTest(battery=battery.BATTERY_NAME):
                floor = getattr(battery, "MINIMUM_MUTATIONS", None)
                self.assertTrue(isinstance(floor, int) and floor > 0,
                                "在这个电池文件里加一行 MINIMUM_MUTATIONS = 现在的条数")
                self.assertGreaterEqual(len(battery.MUTATIONS), floor)

    def test_every_test_class_is_reachable_from_a_layer(self):
        """新写的测试类必须落在电池的某一层里，否则**永远不会被拿来判变异**。

        这条是我自己撞出来的：给 `dev_apptest` 加完流式判定与测试之后跑电池，
        10 条流式变异全活下来——不是守卫没牙，是电池根本不知道有新类。
        不想让它成为层的，必须显式写进 `LAYER_EXEMPT` 并说清原因。
        """
        for battery in self.batteries():
            module = importlib.import_module(battery.TEST_MODULE)
            covered = set(battery.UNIT_CLASSES)
            if getattr(battery, "LIVE", ()):
                covered.add(battery.LIVE[0])
            exempt = set(getattr(battery, "LAYER_EXEMPT", {}) or {})
            with self.subTest(battery=battery.BATTERY_NAME):
                for name, klass in vars(module).items():
                    if not (isinstance(klass, type) and issubclass(klass, unittest.TestCase)
                            and getattr(klass, "__module__", "") == battery.TEST_MODULE):
                        continue
                    if not [m for m in dir(klass) if m.startswith("test_")]:
                        continue                     # 只是公共基类
                    self.assertIn(name, covered | exempt,
                                  "%s 里的 %s 既不在 UNIT_CLASSES/LIVE，也没写 LAYER_EXEMPT" % (
                                      battery.TEST_MODULE, name))

    def test_targets_and_gate_cases_exist(self):
        from agent_runtime import dev_eval
        known = {row["id"] for row in dev_eval.DEV_DATASET}
        for battery in self.batteries():
            with self.subTest(battery=battery.BATTERY_NAME):
                self.assertTrue((engine.REPO / battery.TARGET).is_file(), battery.TARGET)
                self.assertIn(battery.CASE_ID, known,
                              "门禁题被改名/删掉了，这层守卫就成了装饰")

    def test_declared_test_classes_and_bindings_still_resolve(self):
        for battery in self.batteries():
            with self.subTest(battery=battery.BATTERY_NAME):
                module = importlib.import_module(battery.TEST_MODULE)
                self.assertTrue(hasattr(module, battery.BIND),
                                "测试模块里换了别名，电池塞不进被变异的模块")
                self.assertTrue(battery.UNIT_CLASSES, "纯层一个类都没有")
                for name in battery.UNIT_CLASSES:
                    klass = getattr(module, name, None)
                    self.assertIsNotNone(klass, "测试类 %s 不在了" % name)
                    self.assertTrue(issubclass(klass, unittest.TestCase))
                if battery.LIVE:
                    class_name, cases = battery.LIVE
                    klass = getattr(module, class_name, None)
                    self.assertIsNotNone(klass, "现场类 %s 不在了" % class_name)
                    for case in cases:
                        self.assertTrue(hasattr(klass, case), "现场用例 %s 改名了" % case)

    def test_every_anchor_still_matches_the_current_source(self):
        """毫秒级，所以每次 CI 都能真跑到：重构把某行改掉，对应电池当场失效可见。"""
        for battery in self.batteries():
            source = engine.read_source(battery)
            with self.subTest(battery=battery.BATTERY_NAME):
                self.assertEqual(engine.anchors_valid(battery, source), [])

    def test_live_marks_and_live_layer_agree_both_ways(self):
        """标了 `("live",)` 就必须声明现场用例；声明了现场就必须至少有一条用得上它。

        两个方向都查，才不会出现"现场层形同虚设"或"有变异没人能验"这两种静默失效。
        """
        for battery in self.batteries():
            marked = [row[0] for row in battery.MUTATIONS if len(row) > 3]
            with self.subTest(battery=battery.BATTERY_NAME):
                if marked:
                    self.assertTrue(getattr(battery, "LIVE", ()),
                                    "有现场级变异却没声明 LIVE：%s" % marked[:3])
                else:
                    self.assertFalse(getattr(battery, "LIVE", ()),
                                     "声明了 LIVE 却没有任何变异需要它——现场层是装饰")


class RunnerSurfaceTests(unittest.TestCase):
    def test_unknown_battery_name_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            run_all.discover(["不存在的一块"])
        self.assertIn("没有这些电池", str(caught.exception))

    def test_summary_names_the_unverified_count_rather_than_hiding_it(self):
        report = {"name": "x", "total": 3, "caught": 2, "survivors": [],
                  "unverified": ["一条"], "problems": [], "control": {}, "wall": 1.0,
                  "skipped_live": 1}
        text = engine.summarize(report)
        self.assertIn("2/3", text)
        self.assertIn("本环境未验证 1 条", text)
        self.assertTrue(engine.is_green(report))

    def test_a_live_mutant_is_never_counted_twice(self):
        """「本环境验不了」与「活下来」是两件事：混在一起会让 CI 的 --no-live 自己卡死。"""
        battery = fake_battery([("只有现场能验", "VALUE = 1", "VALUE = 9", ("live",))],
                               live=("LiveX", ("test_a",)))
        clean = lambda _m: ([], 1)
        report = engine.run_battery(battery, layers={"纯层": clean, "门禁": clean},
                                    source="VALUE = 1\n", allow_live=False, verbose=False)
        self.assertEqual(report["survivors"], [], report["survivors"])
        self.assertEqual(report["unverified"], ["只有现场能验"])
        self.assertEqual(report["caught"] + len(report["unverified"]), report["total"])

    def test_modules_are_handed_back_untouched(self):
        """电池把【副本】装进 sys.modules；不恢复的话，同一进程里后面跑的测试测的就是副本。
        本会话真为这类污染付过账（判据查了个目录 → 8 条现场用例静默跳过而全量仍显示绿灯）。"""
        import agent_runtime
        import agent_runtime.capacity as real
        original = sys.modules["agent_runtime.capacity"]
        battery = SimpleNamespace(
            BATTERY_NAME="fake", MODULE="agent_runtime.capacity",
            TARGET="agent_runtime/capacity.py", CASE_ID="unused",
            TEST_MODULE="tests.test_mutation_batteries", BIND="at", UNIT_CLASSES=(),
            LIVE=(), MUTATIONS=[("改个数", "VALUE = 1", "VALUE = 9")])
        layers = {"纯层": lambda _m: ([], 1), "门禁": lambda _m: ([], 1)}
        engine.run_battery(battery, layers=layers, source="VALUE = 1\n", verbose=False)
        self.assertIs(sys.modules["agent_runtime.capacity"], original)
        self.assertIs(agent_runtime.capacity, original)
        self.assertTrue(callable(real.normalize_settings), "恢复之后还得是真模块，不是副本")


if __name__ == "__main__":
    unittest.main()
