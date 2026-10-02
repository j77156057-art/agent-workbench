"""任务类端点的 handler 必须真的存在且可调用。

## 这份测试为什么存在

`task_branch_ep` 调用的 `task_branch` 定义在 `game_workbench.py`，但 `api.py` 顶部
那行巨型 `from game_workbench import ...` 里**漏了这个名字**。于是：

    POST /api/tasks/branch  →  任何请求都 NameError

它能长期存在而不被发现，靠的是三件事叠加：
  · `ruff` 之前不在检查清单里（F821 未定义名），纳入后第一时间抓到；
  · 端点抛的是 NameError 而不是 4xx，看起来像"接口坏了"而不是"少个 import"；
  · 没有测试覆盖这条路由——而同批的 task_revert / task_verify 大概率都有。

所以这里不测"分支逻辑对不对"（那是 game_workbench 的事，已有测试），只测**接线**：
注册表里列出来的每个端点，其 handler 真的存在、真的能被调用。

同样的手法也覆盖那批任务端点：漏任何一个都会在这里显形，而不是等到用户点按钮。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import api  # noqa: E402
import api_routes.engine as engine_routes  # noqa: E402


def _registered_task_routes():
    """真正调build_router() 建一遍路由，从产物里读出任务类端点。

    不要去读源码里的列表：那张表是**函数内的局部变量**，不是模块常量，
    任何"import 进来扫常量"的做法都会读到空——测试通过是因为拿到了空列表，
    恰恰是它自己文档里写的"假绿灯"。

    走 build_router 还有一个好处：`getattr(ctx, name)` 就在那里执行，
    少个名字会**当场抛出来**，所以这个函数本身就是一条接线检查。
    """
    router = engine_routes.build_router(api)
    found = []
    for route in getattr(router, "routes", []):
        path = str(getattr(route, "path", "") or "")
        if not path.startswith("/api/tasks"):
            continue
        methods = [m for m in (getattr(route, "methods", None) or []) if m != "HEAD"]
        found.append((path, ",".join(sorted(methods)), getattr(route, "name", "")))
    return found


class TaskRouteWiringTests(unittest.TestCase):

    def test_the_task_route_table_is_discoverable(self):
        """前置条件：路由真的建出来了。找不到的话下面所有断言都是空的。"""
        routes = _registered_task_routes()
        self.assertGreaterEqual(len(routes), 8,
                                "任务路由读不到或数量异常——测试本身失效（拿到空列表也算通过）")

    def test_every_task_handler_resolves(self):
        """注册时用了 getattr(ctx, name)，所以少个名字会在这里当场抛出来。"""
        routes = _registered_task_routes()
        for path, methods, name in routes:
            with self.subTest(path=path):
                self.assertTrue(callable(getattr(api, name, None)),
                                "%s %s 的 handler「%s」在 api 上不可取用" % (methods, path, name))

    def test_the_branch_endpoint_is_reachable(self):
        """本次修的那条：以前 POST /api/tasks/branch 任何请求都 NameError。"""
        paths = [p for p, _m, n in _registered_task_routes() if n == "task_branch_ep"]
        self.assertEqual(len(paths), 1,
                         "/api/tasks/branch 没被注册起来——先看路由表，再看 import")

    def test_task_branch_is_imported(self):
        """本次修的那一处：少个名字会让整条端点抛 NameError。"""
        import game_workbench
        self.assertTrue(hasattr(game_workbench, "task_branch"),
                        "game_workbench 里没有 task_branch——根因不是漏导入")
        self.assertTrue(hasattr(api, "task_branch"),
                        "api.py 没有导入 task_branch——/api/tasks/branch 会抛 NameError")

    def test_it_is_the_same_object(self):
        """必须引的是同一个函数，不是同名替身。"""
        import game_workbench
        self.assertIs(api.task_branch, game_workbench.task_branch)


class NoUndefinedNameTests(unittest.TestCase):
    """静态检查兜底：api.py 里不允许有未定义名。

    这条比单个端点测试更值钱——它对**未来**新增的漏导入同样生效，而不只是
    修好 `task_branch` 这一处。
    """

    def test_api_has_no_undefined_names(self):
        import ast
        import builtins
        source = open(os.path.join(ROOT, "api.py"), encoding="utf-8").read()
        tree = ast.parse(source)
        # 模块级绑定：import / def / class / 赋值
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
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - bound)
        self.assertEqual(missing, [],
                         "api.py 里这些名字没有定义（少个 import 的典型症状）：%s" % missing)


if __name__ == "__main__":
    unittest.main()