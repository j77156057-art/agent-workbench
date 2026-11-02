"""SIM115 的盲区：`return` 表达式里内联的 open() —— 用 AST 补上这道检查。

为什么需要单独一条守卫（都是实测，不是推测）：
- ruff 的 SIM115 **不判** `return open(p).read()` 与 `return json.load(open(p))`。
  我用形状探针实测过：`text = open(p).read()`、`if "x" in open(p).read():`、
  `handle = open(p); handle.close()` 都会被判，独独 `return <含 open 的表达式>` 不会。
- 这个方向最危险的是**假阴性**：把「SIM115 清零」当成「没有内联句柄」，而真正的泄漏
  恰好躲在 return 里。本仓清 SIM115 时就撞到这种站点（`game_workbench.read_memory`、
  `visual_acceptance._browser_stderr_target` 的调用点、`test_stall_probe` 的列表推导）。
- 所以这里用 AST 把「return 的表达式里有 open/io.open」全找出来，要求为 0。
  两种**带证据的例外**由人写清楚、代码里可见，不用豁免名单：
  ① 返回值就是一个**活句柄**、由调用方立刻关闭（`_browser_stderr_target` → Popen 之后
     close）。这种 `return open(...)` 本身是契约，判据按「return 的值是不是那次 open 调用
     本身」把它排除；泄漏风险仍在，但关闭点必须写在调用方并且看得见。
  ② 不是文件 open（`webbrowser.open`、`urllib` 的 `opener().open`）—— 这两类按
     「接收者是不是 io 模块」区分，属性调用一律不看。

扫描范围只含活代码（根级 .py、agent_runtime/、api_routes/、tools/、tests/）——
`dist/`、`_archived_builds/`、`.docmind/` 里的第三方副本不算本仓源码。
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_DIRS = ("agent_runtime", "api_routes", "tools", "tests")


def _return_open_sites(src: str):
    """返回 [(return 行号, open 行号)]：return 表达式里**消费掉**内联 open 的位置。

    只认 `open(...)` 与 `io.open(...)`；`webbrowser.open(...)` / `opener().open(...)`
    这类属性调用按接收者排除。`return open(...)`（值就是那次调用本身）不在此列 ——
    那是把活句柄交出去，关闭点应在调用方。
    """
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "open":
                continue
            if (isinstance(func, ast.Attribute) and func.attr == "open"
                    and isinstance(func.value, ast.Name) and func.value.id == "io"):
                continue
        for inner in ast.walk(node.value):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            is_open = isinstance(func, ast.Name) and func.id == "open"
            is_io_open = (isinstance(func, ast.Attribute) and func.attr == "open"
                          and isinstance(func.value, ast.Name) and func.value.id == "io")
            if is_open or is_io_open:
                hits.append((node.lineno, inner.lineno))
                break
    return hits


def _live_files():
    files = [p for p in ROOT.glob("*.py")]
    for name in LIVE_DIRS:
        files.extend((ROOT / name).rglob("*.py"))
    return sorted(p for p in files if "__pycache__" not in p.parts)


class NoInlineOpenInReturnTests(unittest.TestCase):
    def test_live_source_has_no_open_inlined_in_return(self):
        offenders = []
        for path in _live_files():
            try:
                hits = _return_open_sites(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - 语法错由别的门抓
                continue
            for ret_line, open_line in hits:
                offenders.append("%s:%d（return 在 %d）" % (
                    path.relative_to(ROOT).as_posix(), open_line, ret_line))
        self.assertEqual(offenders, [],
                         "return 里内联了 open：句柄只能靠 gc 关。改成 with 再把值返回：\n  "
                         + "\n  ".join(offenders))

    def test_the_detector_is_not_vacuous(self):
        """判据要能认出那个形状，也要能放过「不是文件 open」的调用。"""
        bad = "def f(p):\n    return open(p, encoding='utf-8').read()\n"
        bad_io = "import io\n\ndef f(p):\n    return io.open(p).read()\n"
        good_with = "def f(p):\n    with open(p) as h:\n        return h.read()\n"
        good_web = "import webbrowser\n\ndef f(u):\n    return webbrowser.open(u)\n"
        direct_handle = "def f(p):\n    return open(p, 'wb')\n"
        self.assertEqual(len(_return_open_sites(bad)), 1)
        self.assertEqual(len(_return_open_sites(bad_io)), 1)
        self.assertEqual(_return_open_sites(good_with), [])
        self.assertEqual(_return_open_sites(good_web), [], "把 webbrowser.open 当成文件句柄了")
        self.assertEqual(_return_open_sites(direct_handle), [],
                         "「把活句柄交给调用方」被当成泄漏了：这类要看调用方有没有关")

    def test_the_scan_covers_production_and_tests(self):
        """范围塌了（比如只扫根目录）这条会红：它必须同时看到生产代码与测试。"""
        names = {p.relative_to(ROOT).as_posix() for p in _live_files()}
        self.assertIn("tools.py", names)
        self.assertIn("agent_runtime/ci_status.py", names)
        self.assertIn("tests/test_launchers.py", names)
        self.assertFalse(any(n.startswith(("dist/", "_archived_builds/", ".docmind/")) for n in names),
                         "扫描范围混进了构建产物/归档副本")


if __name__ == "__main__":
    unittest.main()
