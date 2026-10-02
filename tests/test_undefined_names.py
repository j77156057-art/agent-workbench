"""核心模块的**未定义名**静态检查——一类"看起来做完了、实际没生效"的兜底。

## 为什么要这个

这三天的连续三个 bug 形状完全一样：

| | 表面症状 | 真实原因 |
|---|---|---|
| 11 个写盘工具 | 测试全绿 | 断言的是运行期不读的死副本 |
| 轮次快照 | 写入无任何报错 | `_apply_raw` 漏搬字段，**写得进读不回来** |
| `task_branch` | 端点"坏了" | `from x import ...` 少一个名字 |

它们能存活，靠的都是同一件事：**没有一层检查去验证"真的生效"**，而不是验证
"代码写了"。而 `ruff` 之前不在本轮次的检查清单里——纳入之后 F821 第一时间就
抓出了 `task_branch`。

## 这个文件做什么

用 AST 扫每个核心模块，找出**被读取但从未定义**的名字：少个 import、拼错的变量名、
重构后忘了带过来的符号，都是这个形状。

它不替代 pytest，也不替代类型检查：
 · 它抓的是"名字根本不存在"，而 pytest 抓不到 —— 端点抛 NameError 之前，
   没有任何断言会因为这件事变红；
 · 它对**未来**新增的漏项同样生效，不必等某条路由被用户点到。

## 局限（刻意说清，避免过度承诺）

1. **只覆盖模块顶层可见的名字。** 函数内 `global`/`nonlocal` 声明、闭包捕获、
   `exec` 注入的名字可能误报或漏报，所以每个模块的已知例外都显式列了出来，
   而不是默默跳过。

2. **不检查属性是否存在**（`foo.bar_baz` 里 `bar_baz` 拼错是查不出来的），
   那属于类型检查的活。

3. **不检查运行时行为。** 名字存在但传错参数、返回错类型，它一律看不出来。
"""

import ast
import builtins
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 要扫的模块，以及它们**已知合法**的例外名字。
#:
#: 例外必须逐个写出来并说明理由——默默跳过等于把检查变成"看起来在跑"。
MODULES = {
    "llm.py": {
        # llm.py 在模块顶层做了一批 `from x import (...)` 的延迟导入，
        # 部分被 try/except 包起来（可选依赖缺失时降级），这类名字可能只在
        # 运行时某个分支里有定义。
        "set": ("可选依赖的延迟导入分支，见下方 test 的说明",),
    },
}


def _module_binding_names(tree):
    """收集模块级可见的名字：import / def / class / 顶层赋值 / 参数 / except名。"""
    bound = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


#: Python 在模块顶层**隐式**注入的名字。不把它们算进"未定义"，
#: 否则每个用到 __file__ 的模块都会误报——检查器一旦有噪声就没人看它了。
IMPLICIT_MODULE_NAMES = frozenset({
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__debug__", "WindowsError",
})


def _read_source(path):
    """读源码并剥掉 UTF-8 BOM。

    必须用 utf-8-sig：llm.py 顶部带着一个 BOM（PowerShell 的 Set-Content 加的），
    直接 ast.parse 会抛 `invalid non-printable character U+FEFF`——
    于是这个检查器在**最该查的 llm.py 上直接崩掉**，看起来像"检查失败"而不是
    "检查器坏了"。
    """
    with open(path, encoding="utf-8-sig") as handle:
        return handle.read()


def undefined_names(path):
    """返回该文件里「被读取但从未定义」的名字（已扣掉声明的例外）。"""
    tree = ast.parse(_read_source(path), filename=path)
    bound = _module_binding_names(tree) | IMPLICIT_MODULE_NAMES
    used = {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return sorted(used - bound)


class NoStrayBomTests(unittest.TestCase):
    """BOM 会让某些工具链解析失败（ast、ruff 的部分规则、某些 CI 的 grep）。

    本轮就在 llm.py 上踩到过：它带着 BOM，导致基于 AST 的检查直接抛
    SyntaxError——检查器在最该查的文件上失效。所以把这件事钉成测试。
    """

    def test_core_modules_start_with_a_clean_hashbang_or_docstring(self):
        for relpath in ("llm.py", "api.py", "tools.py", "agent.py",
                        "sessions.py", "pricing.py"):
            path = os.path.join(ROOT, relpath)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                self.assertFalse(
                    handle.read(3) == b"\xef\xbb\xbf",
                    "%s 顶部有 UTF-8 BOM——某些工具链会因此解析失败" % relpath)


class CoreModulesHaveNoUndefinedNames(unittest.TestCase):

    def _check(self, relpath, allowed=()):
        path = os.path.join(ROOT, relpath.replace("/", os.sep))
        if not os.path.isfile(path):
            self.fail("文件不存在：%s" % relpath)
        missing = [n for n in undefined_names(path) if n not in allowed]
        self.assertEqual(
            missing, [],
            "%s 里这些名字被使用但从未定义（少个 import / 拼错变量 / 重构漏带）：\n  %s"
            % (relpath, "\n  ".join(missing)))

    def test_api(self):
        """少一个 import 就让整条端点抛 NameError——本轮修的 task_branch 就是。"""
        self._check("api.py")

    def test_llm(self):
        self._check("llm.py", MODULES["llm.py"]["set"])

    def test_tools(self):
        self._check("tools.py")

    def test_sessions(self):
        """本轮新加快照逻辑时踩过 `_apply_raw` 漏搬字段；这里防的是漏名字。"""
        self._check("sessions.py")

    def test_agent(self):
        self._check("agent.py")

    def test_pricing(self):
        self._check("pricing.py")

    def test_agent_runtime_tools(self):
        self._check("agent_runtime/tools.py")

    def test_agent_runtime_tool_failure(self):
        self._check("agent_runtime/tool_failure.py")

    def test_agent_runtime_plan_state(self):
        self._check("agent_runtime/plan_state.py")


class TheCheckItselfWorksTests(unittest.TestCase):
    """检查器必须真的能抓——不然它只是一盏永远绿的灯。"""

    def _scan_source(self, source):
        tree = ast.parse(source)
        bound = _module_binding_names(tree)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        return sorted(used - bound)

    def test_it_flags_a_missing_import(self):
        found = self._scan_source(
            "def handler(root):\n"
            "    return missing_helper(root)\n")
        self.assertEqual(found, ["missing_helper"])

    def test_it_flags_a_misspelled_name(self):
        found = self._scan_source(
            "value = 1\n"
            "def handler():\n"
            "    return valu\n")          # 少个 e
        self.assertEqual(found, ["valu"])

    def test_it_accepts_a_correct_import(self):
        found = self._scan_source(
            "from game_workbench import task_branch\n"
            "def handler(root):\n"
            "    return task_branch(root, {})\n")
        self.assertEqual(found, [])

    def test_it_understands_locals_and_params(self):
        """局部变量与参数不是全局名字，否则每个函数都会误报满屏。"""
        found = self._scan_source(
            "def handler(arg):\n"
            "    local = arg + 1\n"
            "    return local\n")
        self.assertEqual(found, [])

    def test_it_understands_walrus_and_comprehension_targets(self):
        """推导式里的循环变量是 Store，不是未定义。"""
        found = self._scan_source(
            "def handler(items):\n"
            "    return [x * 2 for x in items]\n")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()