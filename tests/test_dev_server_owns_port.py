"""端口归属：`通不通` 不足以证明「这个端口上跑的是我的项目」。

## 真事故

5173 上站着**另一个 worktree 项目**的 vite，于是 `preview_project`
拍到了**别人的画面**并报成功 —— 一个假绿灯。

而 `probe()` 只判「端口通不通、能不能应答」。通不通与归属是两件事。

端口漂移让这件事更容易发生：vite 撞了 5173 会自动挪到 5174，于是日志里有多行
地址、第一个是死的。模型若照着一个**写死**的端口去访问，撞上的可能是任何服务。

## 判据

`owns_port(port, markers=...)`：页面上有没有调用方给的项目指纹。

**没给标记时不做归属判断**，只返回「无法判归属」—— 不猜。宁可让调用方多传
一个标记，也不要静默把别人的服务认成自己的：那是**假绿灯**，比报错更难发现。

## 与漂移契约的关系

**这一族不动漂移契约。** `parse_port_hint` 的「取最后一个」、漂移用例
（5173 -> 5174）、变异电池里的 5173 fallback，全部原样保留 ——
那套机制的存在理由是「vite 会漂移」，与「谁在那个端口上」是两个问题。
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import dev_server as D  # noqa: E402


def _serve(body: str):
    """起一个只回固定 body 的本机服务，返回 (端口, 关停函数)。"""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv.server_address[1], srv.shutdown


def test_it_recognises_a_matching_marker():
    port, stop = _serve("<html><title>workbench</title></html>")
    try:
        ok, why = D.owns_port(port, markers=["workbench"])
    finally:
        stop()
    assert ok is True, why
    assert "workbench" in why


def test_it_rejects_a_page_without_the_marker():
    """反向守卫：别人的服务即使正常应答，也不认领。

    这是本条守卫存在的全部理由 —— 「通不通」被误当成「是我的」。
    """
    port, stop = _serve("<html><title>another-project</title></html>")
    try:
        ok, why = D.owns_port(port, markers=["workbench"])
    finally:
        stop()
    assert ok is False, (
        "页面上没有我的标记却认领了 —— 这正是那个假绿灯事故"
    )
    assert "another-project" in why, why


def test_without_markers_it_refuses_to_guess():
    """反向守卫：没给标记时**不猜**，如实说「无法判归属」。

    宁可返回「不知道」，也不要把别人的服务认成自己的。
    """
    port, stop = _serve("<html>hi</html>")
    try:
        ok, why = D.owns_port(port)
    finally:
        stop()
    assert ok is False, "没给标记却下了归属结论"
    assert "无法判归属" in why, why


def test_a_dead_port_is_not_owned():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    ok, why = D.owns_port(dead, markers=["workbench"])
    assert ok is False, why


def test_it_still_has_no_production_caller():
    """**记录一个未接线的状态**：本模块至今**没有生产调用点**。

    这不是「忘了接线」，是**接不了** —— `owns_port` 的判据是「页面上有没有
    markers 里的字串」，而实测三个候选指纹全部不可用：

    * `<title>` —— 三个入口都以 `Agent 工作台` 开头，而另一个 worktree 跑的是
      **同一份代码**，标题完全相同
    * 项目名注入页面 —— `git grep "APP_ID|PROJECT_NAME|VITE_APP" -- frontend`
      **零命中**
    * dev URL / 路径 —— `http://127.0.0.1:5173/` 不含项目路径

    而那个假绿灯事故恰恰是 worktree 撞端口。

    真正的修法是**前端在 dev 时注入项目身份**
    （`docs/frontend-task-project-identity.md`），注入落地后本模块直接接上即可。

    这条守卫的作用：一旦有人接线，它会红并提醒「记得同步更新
    `docs/known-defect-port-ownership-has-no-fingerprint.md` 的状态」——
    免得文档继续说「未接线」而代码已接。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    callers = []
    # 用 **AST** 而不是正则或逐行形态判断。
    #
    # 前两版都栽在同一个地方：
    #   1) 正则 `(?<![\w.])owns_port\s*\(` 的 `.` 否定断言把 `dev_server.owns_port(...)`
    #      整个排除 —— 它**从来没匹配过任何真实调用**，而我当时用**无效变异**
    #      （插在 docstring 里的语句，语法合法但永���执行）验出「7 passed」，
    #      误以为守卫有效。
    #   2) 放开前导 `模块.` 后逐行排除 docstring —— 结果又把
    #      「要判归属用 `owns_port()`」这句散文算成调用。
    #
    # AST 没有这些歧义：只有 `Call` 节点是调用，字符串里的散文根本不是。
    import ast as _ast

    for path in list(root.glob("*.py")) + list((root / "agent_runtime").glob("*.py")):
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "owns_port":
                callers.append("%s:%d  %s" % (path.name, node.lineno, name))
    assert not callers, (
        "owns_port 现在有生产调用点了：\n  " + "\n  ".join(callers) +
        "\n若这是有意的，请更新 docs/known-defect-port-ownership-has-no-fingerprint.md"
        " 的状态，并确认前端是否已注入项目身份")


def test_probe_still_only_answers_reachability():
    """**记录边界**：`probe()` 仍然只判「通不通」，它没有被改成判归属。

    有意如此：改它会动到既有契约（`dev_server` 的多处消费点按「通不通」用）。
    归属判断是**新增能力**，走 `owns_port()`。
    """
    import inspect

    doc = inspect.getdoc(D.probe) or ""
    assert "不判" in doc and "owns_port" in doc, (
        "probe 的文档应当明说它不判归属、判归属用 owns_port —— "
        "否则下一个人会以为 probe 已经足够"
    )


# --------------------------------------------------------------------------
# 漂移契约不许被这次改动碰到
# --------------------------------------------------------------------------

def test_the_drift_contract_is_untouched():
    """反向守卫：`parse_port_hint` 仍取**最后一个**匹配。

    那是另一族问题（「vite 会漂移」），与归属判断无关。这次不该顺手动它 ——
    它的用例（`test_it_takes_the_last_one_when_the_port_moved`）与变异电池
    都钉着这个行为。
    """
    assert D.parse_port_hint(
        "  ➜  Local:   http://127.0.0.1:5173/\n"
        "  ➜  Network: http://127.0.0.1:5174/\n") == 5174