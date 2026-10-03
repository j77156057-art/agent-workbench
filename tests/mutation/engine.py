"""变异电池的共用引擎：加载被变异的模块、跑对照组、逐条判定【有没有牙】。

每条电池只声明四件事：变异哪个模块、有哪些变异、每一层怎么跑、现场层是哪几条用例。
判定循环收在这里——同一套循环抄五遍，本身就是"某一份悄悄不跑了、悄悄全绿了"的起点
（本会话真的撞见过一次：判据用 `.is_file()` 查目录，8 条现场用例静默跳过而全量仍显示绿灯）。

三条不可退让的规则：
1. **对照组先跑**：未变异的代码必须在电池要用到的每一层都干净，否则整块电池不作数
   ——不然环境一红，每条变异都"被抓到"，分数好看而毫无意义。
2. **锚点失效就是错**：变异锚点找不到原文，说明电池与源码已经脱钩，报失败而不是跳过。
3. **现场跑不了 ≠ 抓住了**：没有浏览器/工具链时现场层会 skip，那些变异记成
   「本环境未验证」，既不算抓住也不算活下来，并且会被打印出来。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / ".tmp" / "mut"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LIVE = "live"


def read_source(battery: Any) -> str:
    path = REPO / str(battery.TARGET)
    if not path.is_file():
        raise FileNotFoundError("电池指向的源文件不存在：%s" % path)
    return path.read_text(encoding="utf-8")


def load_mutated(module_name: str, source: str, tag: str) -> Any:
    """把一份源码当成【那个模块】装进 sys.modules。

    写进 `.tmp/mut/`（gitignore 内）而不是就地改源文件：本仓有多个 AI 共用一个工作树，
    原地变异会撞别人正在编辑的文件（这条为它付过真金白银的代价）。
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    target = SCRATCH / ("%s_%d.py" % (module_name.rsplit(".", 1)[-1], abs(hash(tag))))
    target.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, str(target))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    package, _, leaf = module_name.rpartition(".")
    if package:
        setattr(importlib.import_module(package), leaf, module)
    return module


def _suite_from(test_module: str, bind_to: Any, classes: tuple[str, ...],
                module: Any, cases: tuple[str, ...] = ()) -> unittest.TestSuite:
    """把变异后的模块接到测试模块上。

    `bind_to` 可以是**一个名字**（`BIND`），也可以是**一组 (测试模块里的名字, 源模块里的属性名)**
    （`BINDS`）。两者的区别在导入形式：

    - `import agent` + `agent.foo()` —— 名字 `agent` 就是模块，接一个 `BIND='agent'` 就够。
    - `from agent_runtime.game_workflow import _MAX_TASK_DEPS, _raw_task_count` ——
      这些名字在**导入时**就把旧模块的符号绑死在测试模块上了，`setattr` 换掉模块对象
      也没用：测试读的还是那批旧符号，变异**永远打不进去**。
      本会话实测踩过：`battery_plan_dag` 10 条里 8 条报"活下来"，逐层量下来纯层与现场层
      全部照绿——根因就是这个，不是守卫没牙。

    所以from-import 的测试模块必须用 `BINDS` 把每个名字单独换掉。
    """
    imported = importlib.import_module(test_module)
    if isinstance(bind_to, str):
        setattr(imported, bind_to, module)
    else:
        for test_name, source_name in bind_to:
            # `'*'` 是「把整个模块绑上去」：测试里除了 from-import 的名字，
            # 还有 `agent.Agent(...)` 这种走**模块对象属性**的用法。
            # 只换from-import 的名字而漏掉模块本身，现场层真跑起来测的还是
            # 真代码——实测 4 条用例全绿，一条变异都没抓到。
            if source_name == "*":
                setattr(imported, test_name, module)
            else:
                setattr(imported, test_name, getattr(module, source_name))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for class_name in classes:
        klass = getattr(imported, class_name)
        if cases:
            for case in cases:
                suite.addTest(klass(case))
        else:
            suite.addTests(loader.loadTestsFromTestCase(klass))
    return suite


def run_suite(suite: unittest.TestSuite) -> tuple[list[str], int]:
    """跑一套用例，回（失败项名字，真正执行掉的条数）。

    `ran` 必须是【执行数】而不是 `testsRun`：全类 skip 时 `testsRun` 照样 >0，
    只看它就把"没跑"当成"跑了且没红"。
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = unittest.TextTestRunner(stream=buffer, verbosity=0).run(suite)
    failed = ["%s.%s" % (case[0].__class__.__name__, case[0]._testMethodName)
              for case in result.failures + result.errors]
    return failed, result.testsRun - len(result.skipped)


def gate_failures(case_id: str) -> Callable[[Any], tuple[list[str], int]]:
    def layer(_module: Any) -> tuple[list[str], int]:
        import agent_runtime.dev_eval as de
        case = next((dict(row) for row in de.DEV_DATASET if row["id"] == case_id), None)
        if case is None:
            return ["门禁题不存在：%s" % case_id], 1
        row = de.evaluate_case(case)
        bad = [item["name"] for item in row.get("checks", []) if not item["ok"]]
        return bad, 1
    return layer


def unit_layer(battery: Any) -> Callable[[Any], tuple[list[str], int]]:
    def layer(module: Any) -> tuple[list[str], int]:
        return run_suite(_suite_from(battery.TEST_MODULE, _bind_spec(battery),
                                     battery.UNIT_CLASSES, module))
    return layer


def _bind_spec(battery: Any) -> Any:
    """这块电池要把变异模块接到测试模块的哪些名字上。"""
    binds = getattr(battery, "BINDS", None)
    if binds:
        return tuple(tuple(row) for row in binds)
    return battery.BIND


def live_layer(battery: Any) -> Callable[[Any], tuple[list[str], int]] | None:
    live = getattr(battery, "LIVE", ())
    if not live:
        return None

    def layer(module: Any) -> tuple[list[str], int]:
        class_name, cases = live
        return run_suite(_suite_from(battery.TEST_MODULE, _bind_spec(battery),
                                     (class_name,), module, tuple(cases)))
    return layer


def anchors_valid(battery: Any, source: str) -> list[str]:
    """锚点与形状检查：这些不需要跑测试，毫秒级，所以每次 CI 都能真正跑到。"""
    problems = []
    seen = set()
    for index, entry in enumerate(getattr(battery, "MUTATIONS", ())):
        name = str(entry[0]) if len(entry) > 3 or entry else "第 %d 条" % index
        if len(entry) not in (3, 4):
            problems.append("%s：每条变异要是 (名字, 原文, 变异后[, (\"live\",)])" % name)
            continue
        old, new = str(entry[1]), str(entry[2])
        if not old:
            problems.append("%s：锚点是空串" % name)
        elif old not in source:
            problems.append("%s：锚点在当前源码里找不到（电池已与源码脱钩）" % name)
        elif source.count(old) > 1:
            # 只查"锚点在不在"不够：引擎做的是 replace(old, new, 1)，锚点出现两次时
            # 变异只落在**第一处**。本会话实测踩过：`run_isolated` 与 `spawn_isolated`
            # 里 `if ... AssignProcessToJobObject` 那一行字面完全相同，于是那条变异
            # 被打进没人调用的那个函数——它"活下来"看起来像守卫没牙，其实是电池写歪了。
            problems.append(
                "%s：锚点在源码里出现 %d 次，只替换第一处，变异可能打进另一处近似代码；"
                "请多带一行上下文把它变成唯一" % (name, source.count(old)))
        if old == new:
            problems.append("%s：变异前后一模一样，等于没变异" % name)
        elif old in source:
            # 变异后的那份代码必须**编译得过**：只替换半句的变异会当场崩掉解释器，
            # 那种"红"证明不了任何判据有牙，把它记成抓住等于给自己加分。
            try:
                ast.parse(source.replace(old, new, 1))
            except SyntaxError as exc:
                problems.append("%s：变异后源码编译不过（这条变异自己写歪了）：%s" % (
                    name, str(exc)[:90]))
        if name in seen:
            problems.append("变异名字重复：%s" % name)
        seen.add(name)
        marked = len(entry) > 3
        if marked and not getattr(battery, "LIVE", ()):
            problems.append("%s：标了现场却没声明 LIVE" % name)
    if not getattr(battery, "MUTATIONS", ()):
        problems.append("这块电池一条变异都没有")
    return problems


def build_layers(battery: Any, allow_live: bool = True
                 ) -> dict[str, Callable[[Any], tuple[list[str], int]]]:
    layers = {"纯层": unit_layer(battery), "门禁": gate_failures(str(battery.CASE_ID))}
    live = live_layer(battery) if allow_live else None
    if live:
        layers["现场"] = live
    return layers


def run_battery(battery: Any, *, allow_live: bool = True, verbose: bool = True,
                layers: dict[str, Callable[[Any], tuple[list[str], int]]] | None = None,
                source: str | None = None) -> dict[str, Any]:
    """跑一块电池，回一份结构化报告（不抛异常：环境不满足要如实说，不能报"全绿"）。

    `layers` / `source` 可注入，是为了让引擎自己的判定逻辑被单测覆盖——不然
    "对照组红了就不作数""现场 skip 了不算抓住"这两条只能靠真跑一遍环境再祈祷。
    """
    name = str(battery.BATTERY_NAME)
    report: dict[str, Any] = {"name": name, "total": 0, "caught": 0, "survivors": [],
                              "unverified": [], "problems": [], "control": {},
                              "wall": 0.0, "skipped_live": 0}
    if source is None:
        try:
            source = read_source(battery)
        except OSError as exc:
            report["problems"].append("读不到源码：%s" % exc)
            return report
    report["problems"].extend(anchors_valid(battery, source))
    report["total"] = len(getattr(battery, "MUTATIONS", ()))
    if report["problems"]:
        return report

    layers = dict(layers or build_layers(battery, allow_live))
    module_name = str(battery.MODULE)
    package, _, leaf = module_name.rpartition(".")
    # 电池结束后要把 sys.modules 与包属性**换回原来那个真模块**：留着副本，后面在同一进程
    # 里跑的测试测的就是副本（本仓为这类污染付过代价：8 条现场用例静默跳过而全量仍显示绿灯）。
    saved = sys.modules.get(module_name)
    try:
        _run_mutations(battery, source, layers, report, module_name, allow_live, verbose,
                       pristine_loader=lambda: load_mutated(module_name, source, "pristine"))
    finally:
        if saved is not None:
            sys.modules[module_name] = saved
            if package:
                setattr(importlib.import_module(package), leaf, saved)
        else:
            sys.modules.pop(module_name, None)
            if package and hasattr(importlib.import_module(package), leaf):
                delattr(importlib.import_module(package), leaf)
    return report


def _run_mutations(battery, source, layers, report, module_name, allow_live, verbose,
                   pristine_loader) -> None:
    """对照组 + 逐条判定。`report` 就地填。"""
    live = layers.get("现场")
    # 对照组：未变异的代码必须在每一层都干净
    pristine = pristine_loader()
    for label, layer in layers.items():
        failed, ran = layer(pristine)
        report["control"][label] = {"failed": failed[:4], "ran": ran}
        if failed or ran == 0:
            report["problems"].append("对照组（未变异）在「%s」层就红/没跑：failed=%s ran=%s"
                                      % (label, failed[:3], ran))
    if report["problems"]:
        return

    started = time.monotonic()
    for entry in battery.MUTATIONS:
        label, old, new = str(entry[0]), str(entry[1]), str(entry[2])
        wants_live = len(entry) > 3
        # 兜底：变异后的模块装不进。`anchors_valid` 已经在事前把它报成问题（这条变异写歪了，
        # 不给它记分）；这里只是保证一块电池不会因一条坏变异崩掉整轮。
        try:
            module = load_mutated(module_name, source.replace(old, new, 1), label)
        except BaseException as exc:  # noqa: BLE001 - 装不进就是这条电池写歪了
            report["problems"].append("%s：变异后模块装不进（%s: %s）" % (
                label, type(exc).__name__, str(exc)[:90]))
            if verbose:
                print("  ! %s（变异后模块装不进，不作数）" % label)
            pristine_loader()
            continue
        caught_by = []
        for tag in ("纯层", "门禁"):
            failed, _ran = layers[tag](module)
            if failed:
                caught_by.append("%s:%s" % (tag, failed[0]))
                break
        unverified = False
        if not caught_by and wants_live:
            if live is None or not allow_live:
                unverified = True
            else:
                failed, ran = live(module)
                if failed:
                    caught_by.append("现场:%s" % failed[0])
                elif ran == 0:
                    unverified = True
            report["skipped_live"] += 1
            if unverified:
                report["unverified"].append(label)
                if verbose:
                    print("  [-] %s（本环境验不了，不算抓住也不算活下来）" % label)
        if caught_by:
            report["caught"] += 1
            if verbose:
                print("  [x] %s（%s）" % (label, "；".join(caught_by)))
        elif not unverified:
            report["survivors"].append(label)
            if verbose:
                print("  [ ] %s 活下来（守卫没牙）" % label)
        pristine = pristine_loader()
    report["wall"] = round(time.monotonic() - started, 1)


def summarize(report: dict[str, Any]) -> str:
    tail = []
    if report["unverified"]:
        tail.append("本环境未验证 %d 条" % len(report["unverified"]))
    if report["survivors"]:
        tail.append("活下来 %d 条" % len(report["survivors"]))
    if report["problems"]:
        tail.append("有问题：%s" % "；".join(report["problems"][:3]))
    return "%s：%d/%d 抓到%s" % (
        report["name"], report["caught"], report["total"],
        ("（%s）" % "，".join(tail)) if tail else "")


def is_green(report: dict[str, Any]) -> bool:
    """抓到全部【本环境能验的】变异，且没有锚点/对照组问题，才算这块电池绿。"""
    return (not report["problems"] and not report["survivors"]
            and report["caught"] + len(report["unverified"]) == report["total"])
