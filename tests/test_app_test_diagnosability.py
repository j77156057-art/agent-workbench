# -*- coding: utf-8 -*-
"""整页守卫的三个缺陷：诊断信息误导 + 就绪预算写死 + 端口文件读竞争。

三者都让 `tests/test_app_test.py` 的深链那条**红得毫无信息量**，而且时红时不红。

## A `DevToolsActivePort` 读竞争（`visual_acceptance.py`）

```python
while not active.exists() and time.monotonic() < deadline:   # 只等「文件出现」
    time.sleep(0.05)
if not active.exists():
    raise VisualAcceptanceError("浏览器调试端口未就绪。")
port = active.read_text(encoding="utf-8").splitlines()[0].strip()   # 读一次，不重试
```

就绪判据是 `exists()`，但真正需要的条件是**可读**。Windows 上 Edge 创建
`DevToolsActivePort` 后会短暂独占该文件，此时 `exists()` 已为 True 而
`read_text()` 抛 `PermissionError`（Errno 13）。实测 4 次里撞到 1 次：

    [Errno 13] Permission denied: 'D:\\Temp\\docmind-visual-preview-xxx\\DevToolsActivePort'

## B 就绪预算写死 6 秒（`page_action.py`）

```python
devtools.evaluate(
    "(async()=>{for(let n=0;n<120;n++){if(document.readyState==='complete')return true;"
    "await new Promise(r=>setTimeout(r,50))}throw Error('页面加载超时')})()")
```

`120 × 50ms` = 固定 6 秒，**写死在浏览器侧 JS 里**；调用方传的 `timeout=180.0`
一个字都没进去。被测页面要做：加载 bundle + vendor chunk -> 拉 `GET /api/config`
-> 等 Ollama 探活（该函数注释自述**首次 6108 毫秒、之后 15 毫秒**）-> 挂载 Vue。
冷启动 6 秒装不下。

`app_test.py` 的 `_open_with_retry` 已在绕它（重试 3 次 + 退避 2s/4s），而它的
注释自己写着这招「把一次失败放大成三次连续抢资源」。

## C 诊断字段被截断（`tests/test_app_test.py`，4 处）

```python
detail = json.dumps({"failed": ..., "deep": ..., "console": ..., "error": got.get("error")})[:1200]
```

`error` 排在**最后**，而 `failed` 一长（实测 20 条）就把 1200 字用完 ——
**唯一有信息量的字段被截掉**。这就是「红得毫无信息量」的直接原因：
探针前三次排查只看到一串判据全红，看不出原因。

## 反向守卫

三条都守着「不许把诊断信息弄丢」这个方向：
- A: 文件被独占时必须**等**，不许直接抛
- B: 预算必须**跟着传参走**，不许再写死 6 秒；也不许无上限（真挂死时要能退出）
- C: `error` 必须在 `failed` **之前**出现，且 `[:1200]` 截断不得吃掉它
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime import app_test as at  # noqa: E402
from agent_runtime import visual_acceptance as va  # noqa: E402


def _code_lines(module_or_path):
    """模块（或 .py 路径）的**代码**文本：注释与文档字符串全部剥掉。

    第一版守卫直接扫源码文本，结果命中的是我自己写的注释 —— 那段注释里引用了
    旧形态 `for(let n=0;n<120;n++)` 来说明「此前写死」，于是**改完代码守卫反而红**。

    第二版用 `tokenize` 手工剥，结果 docstring 的 skip 状态与 with 块叠在一起时
    会把后面的真代码一起吞掉（`_read_active_port` 就这么消失的）。
    改用 `ast`：注释在 AST 里根本不存在，docstring 是 `Expr(Constant)`，
    剔掉它们就干净了。
    """
    import ast
    path = getattr(module_or_path, "__file__", module_or_path)
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)          # 剔除 docstring
    return ast.unparse(tree)


class ActivePortReadTests(unittest.TestCase):
    """A：端口文件要**等可读**，而不是等「存在」。"""

    def _write_locked_style(self, folder, *, readable_after):
        """造一个「先存在、后可读」的文件，模拟 Edge 的独占窗口。"""
        path = Path(folder) / "DevToolsActivePort"
        path.write_text("51234\n/devtools/browser/abc\n", encoding="utf-8")
        if not readable_after:
            path.unlink()

    def test_the_browser_session_uses_the_retrying_reader(self):
        """生产代码必须走那个带重试的读取，而不是裸 `read_text`。

        关键是要断言**调用点真的换了**，而不只是「那个函数还在」——
        变异 A 把调用点退回裸读而函数仍留着时，只查函数存在的守卫会 0 红放过去
        （第一版守卫就这样漏了）。所以这里逐行看有没有裸读那一支。
        """
        import io
        import ast
        src = io.open(va.__file__, encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "browser_session")
        body = ast.unparse(fn)
        self.assertIn("_read_active_port", body,
                      "browser_session 必须走带重试的读取")
        # 只看这个函数：`_browser_still_listening` 读不到端口时**要**当「还活着」
        # （改动前的注释解释过为什么），那里裸读是对的，不该被这条守卫误伤。
        self.assertNotIn("active.read_text(", body,
                         "调用点退回裸读了 —— Windows 上 Edge 独占该文件时会 PermissionError")
        self.assertNotIn(".splitlines()[0]", body,
                         "裸读那一支（含空文件 IndexError）必须消失")
        self.assertGreaterEqual(src.count("_read_active_port("), 2,
                                "定义 + 至少一个调用点")

    def test_it_retries_while_the_file_is_not_readable(self):
        """**反向守卫**：文件存在但读不了时，必须继续等而不是立刻抛。"""
        import time as _time
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "DevToolsActivePort"

            attempts = {"n": 0}
            real_read = Path.read_text

            def flaky_read(self, *a, **kw):
                # 前两次读抛 PermissionError（Edge 独占），第三次才放行
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise PermissionError(13, "被另一个进程占用")
                return "51234\n/devtools/browser/abc\n"

            path.write_text("x", encoding="utf-8")
            Path.read_text = flaky_read
            try:
                port, _ws = va._read_active_port(path, timeout=3.0, interval=0.01)
            finally:
                Path.read_text = real_read
            self.assertEqual(port, "51234")
            self.assertGreaterEqual(attempts["n"], 3,
                                    "读取被独占时必须重试，不能一次就放弃")

    def test_it_gives_up_with_a_readable_message(self):
        """**反向守卫**：等不到就说清在等什么，不许裸 FileNotFoundError。"""
        import time as _time
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "DevToolsActivePort"
            t0 = _time.monotonic()
            with self.assertRaises(va.VisualAcceptanceError) as caught:
                va._read_active_port(missing, timeout=0.2, interval=0.01)
            self.assertIn("端口", str(caught.exception))
            self.assertLess(_time.monotonic() - t0, 2.0, "必须真的等到预算用完再放弃")

    def test_an_empty_file_is_not_taken_as_a_port(self):
        """**反向守卫**：空文件/无首行都不能被当成端口（`splitlines()[0]` 会 IndexError）。"""
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / "DevToolsActivePort"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(va.VisualAcceptanceError):
                va._read_active_port(empty, timeout=0.15, interval=0.01)


class ReadyBudgetTests(unittest.TestCase):
    """B：就绪预算必须跟着 `timeout` 走，且有上限。"""

    def _budget_js(self, timeout):
        return va.PAGE_READY_JS % {"budget_ms": int(timeout * 1000), "step_ms": 50}

    def test_the_ready_budget_is_not_a_hardcoded_6_seconds(self):
        """**反向守卫**：预算必须可由参数派生，代码里不许再出现字面量 120 次。"""
        code = _code_lines(va)
        self.assertNotIn("n<120", code, "visual_acceptance 里不许再写死 120 次")

    def test_all_three_call_sites_share_one_budget(self):
        """三处曾各抄一份写死的 6 秒。现在必须都走 `visual.page_ready_js`。

        `app_test._open_with_retry` 的注释自己写过：
        「同一个逻辑写两处就是"其中一处迟早变陈旧"」——实测真的是三处。
        """
        for name in ("page_action.py", "media_fixture.py", "visual_acceptance.py"):
            path = Path(va.__file__).parent / name
            src = io.open(path, encoding="utf-8").read()
            self.assertIn("page_ready_js(", src,
                          "%s 应改用共用的 page_ready_js" % name)
            self.assertNotIn("n<120", _code_lines(path),
                             "%s 里还留着写死的 120 次预算" % name)

    def test_a_bigger_timeout_gives_a_bigger_budget_but_capped(self):
        """**反向守卫**：预算跟 `timeout` 走，但必须有上限。"""
        self.assertEqual(va.page_ready_budget(6.0), 6.0)
        self.assertEqual(va.page_ready_budget(180.0), va.PAGE_READY_BUDGET_CAP,
                         "timeout=180 时必须被上限压住，否则单页能独占三分钟")
        self.assertLessEqual(va.PAGE_READY_BUDGET_CAP, 60.0)

    def test_the_script_actually_contains_the_derived_budget(self):
        js = va.page_ready_js(180.0)
        self.assertIn(str(int(va.PAGE_READY_BUDGET_CAP * 1000)), js)
        self.assertIn("页面加载超时", js)
        self.assertNotIn("<120", js)


class DetailNotTruncatedTests(unittest.TestCase):
    """C：诊断信息里 `error` 不能被 `[:1200]` 挤掉。"""

    def test_the_error_key_is_inserted_first_in_the_dict(self):
        """**反向守卫**：`error` 必须在 dict 构造时**第一个**插入。

        只验输出顺序不够 —— 变异 C 是把收尾换回一刀切，输出层也少了 error；
        但真正的病灶是「构造 dict 时 error 排在 failed 后面」，那样任何
        基于「先算长度再决定」的补救都晚了。
        """
        import ast
        import io
        tree = ast.parse(io.open(at.__file__, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "detail_text")
        first_assign = None
        for node in ast.walk(fn):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                first_assign = node
                break
        self.assertIsNotNone(first_assign, "detail_text 里应有 parts 的类型标注赋值")
        keys = [k.value for k in first_assign.value.keys]
        self.assertEqual(keys[0], "error",
                         "parts 的第一个键必须是 error，实得 %r" % (keys[:3],))

    def test_detail_puts_error_before_failed(self):
        got = {
            "failed": ["deeplink_%s_%s" % (n, t)
                       for n in ("root", "workbench", "materials", "voice",
                                 "settings", "graph", "timeline", "guard",
                                 "lane", "eval", "e2e", "history")
                       for t in ("renders_its_own_page", "serves_no_other_page")],
            "observed": {"deep_links": [], "deep_console_errors": []},
            "error": "VisualAcceptanceError: 浏览器调试端口未就绪。",
        }
        detail = at.detail_text(got)
        self.assertLess(detail.index("error"), detail.index("failed"),
                        "error 必须排在 failed 之前 —— failed 一长就会把它挤出截断范围")
        self.assertIn("浏览器调试端口未就绪", detail)

    def test_detail_keeps_the_error_even_when_failed_is_huge(self):
        """**反向守卫**：`failed` 挤不进预算时，该丢的是 `failed`，不是 `error`。

        第一版守卫的期望本身是错的（要求 3000 字的 error 完整出现在 1200 字的
        诊断里）—— 那是自相矛盾的。真正要守的是**优先级**：
        error 在、failed 被压缩，而不是反过来。
        """
        got = {"failed": ["deeplink_%s_%s" % (name, tail)
                          for name in ("root", "workbench", "materials", "voice",
                                       "settings", "graph", "timeline", "guard",
                                       "lane", "eval", "e2e", "history")
                          for tail in ("renders_its_own_page",
                                       "serves_no_other_page")],
               "observed": {"deep_links": [], "deep_console_errors": []},
               # 真实那次 error 是一整段带重试次数与退避秒数的说明（探针实测 160+ 字）
               "error": "整页守卫没能走完：深链路由 /workbench：开了 3 次都顶破 "
                        "open_page 的就绪预算（每次都已回收上一个会话并退避 2/4 秒）："
                        "{'exceptionId': 1, 'text': 'Uncaught (in promise) Error: "
                        "页面加载超时'}"}
        detail = at.detail_text(got)
        # 只守真正的需求：**error 不被挤掉**。
        # 不要求「一定出现『另有 N 条』」—— 实测这组输入是 1177 字、没超预算，
        # 本来就不该压缩（第一版守卫把它当成需求，是过严）。
        self.assertIn("顶破 open_page 的就绪预算", detail, "error 必须完整保留")
        self.assertLessEqual(len(detail), at.DETAIL_LIMIT + 200)

    def test_when_the_budget_is_tight_failed_gets_shrunk_not_error(self):
        """同一需求，换个真能撑爆预算的输入：error 仍须完整，failed 让位。"""
        names = ("root", "workbench", "materials", "voice", "settings", "graph",
                 "timeline", "guard", "lane", "eval", "e2e", "history")
        err = ("顶破 open_page 的就绪预算（退避 2/4 秒）："
               "{'exceptionId': 1, 'text': 'Uncaught (in promise) Error: 页面加载超时'}")
        got = {"failed": ["deeplink_%s_renders_its_own_page_and_serves_no_other_page" % n
                          for n in names] * 8,
               "observed": {"deep_links": ["x" * 40] * 6, "deep_console_errors": ["y" * 40] * 6},
               "error": err}
        detail = at.detail_text(got)
        self.assertIn(err, detail, "error 必须完整保留")
        self.assertLessEqual(len(detail), at.DETAIL_LIMIT + 200)
        self.assertIn("另有", detail, "撑爆预算时该压缩的是 failed")

    def test_detail_truncates_an_over_long_error_but_says_so(self):
        """error 自己超长时允许截断，但**必须留痕**（不许静默削掉）。"""
        long_error = "错误细节" * 2000
        got = {"failed": ["k1"], "observed": {}, "error": long_error}
        detail = at.detail_text(got)
        self.assertLessEqual(len(detail), at.DETAIL_LIMIT + 200)
        self.assertIn("已截", detail, "截断必须留痕，否则读者以为看全了")


if __name__ == "__main__":
    unittest.main()