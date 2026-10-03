"""`agent_runtime/dev_server.py`：本地 dev server 的生命周期与**端口归属**。

这个模块此前**一条测试都没有**（§6 缺口 4 是从"没人验"发现的）。这一份里最要紧的不是
"服务起来了吗"，而是 **"应答的那个服务是谁起的"**——下面 `OwnershipTests` 那组就是从
一次确定性的假绿里来的。
"""
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import dev_server  # noqa: E402

BODY = b"<!doctype html><title>STUB-SERVICE</title>"


def _serve() -> ThreadingHTTPServer:
    """在随机回环端口上起一个**不属于本项目**的服务，并返回它。"""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def argv_or_sleeper():
    """一个既不打印端口、也不绑定任何指定端口的无关命令（只活着）。"""
    return [sys.executable, "-c", "import time; time.sleep(30)"]


class PortHintTests(unittest.TestCase):
    """从日志里认端口：撞了端口会往后挪，所以有**多行**。"""

    def test_it_reads_the_port_out_of_a_dev_server_banner(self):
        self.assertEqual(
            dev_server.parse_port_hint("  ➜  Local:   http://127.0.0.1:5173/\n"), 5173)
        self.assertEqual(dev_server.parse_port_hint("ready - Listening on localhost:3000"), 3000)

    def test_it_takes_the_last_one_when_the_port_moved(self):
        """vite 撞了 5173 会自动挪到 5174，两行地址都在日志里。

        取第一个认到的会拿到**没起来那个**（死掉的地址），于是探测永远失败，
        报告却说"等了 20 秒没认到端口"——指向一个错的地址。
        """
        log = ("  Port 5173 is in use, trying another one...\n"
               "  ➜  Local:   http://127.0.0.1:5173/\n"
               "  ➜  Network: http://127.0.0.1:5174/\n")
        self.assertEqual(dev_server.parse_port_hint(log), 5174)

    def test_no_banner_yields_zero(self):
        self.assertEqual(dev_server.parse_port_hint("编译中……\n没有任何地址"), 0)
        self.assertEqual(dev_server.parse_port_hint(""), 0)

    def test_port_taken_sees_a_live_listener(self):
        server = _serve()
        try:
            self.assertTrue(dev_server.port_taken(server.server_address[1]))
        finally:
            _stop(server)
        self.assertFalse(dev_server.port_taken(dev_server.free_port()))

    def test_port_taken_ignores_out_of_range(self):
        for bad in (0, 80, 70000, -1):
            self.assertFalse(dev_server.port_taken(bad), bad)


class OwnershipTests(unittest.TestCase):
    """**那个应答的服务是谁起的**——这是 §6 缺口 4 挖出来的确定性假绿。

    `dev_serve` 传的 argv 里**没有端口参数**（`tools.py` 的 `_shell_argv(command)`），
    所以 `port=` 只是"优先去探这里"。于是：用户给 `port: X`，而 X 上已经有别人的服务时，
    `probe` 会成功，`start()` 于是**把别人的服务当成自己的**报就绪，并把那个 URL 交回去
    ——后面所有整页断言都打在别人的服务上。
    """

    def test_a_taken_port_is_not_reported_as_our_ready_service(self):
        squatter = _serve()
        taken = squatter.server_address[1]
        job = {}
        try:
            # 起一个完全无关的服务：它既不打印端口、也不绑那个端口。
            argv = [sys.executable, "-c", "import time; time.sleep(30)"]
            with patch.object(dev_server.process_runner, "start_job",
                              side_effect=lambda *a, **k: job.setdefault("rep", {
                                  "ok": True, "job_id": "probe-job"}) or job["rep"]), \
                    patch.object(dev_server, "_view",
                                 return_value={"logs": "", "state": "running"}), \
                    patch.object(dev_server, "probe",
                                 return_value=(True, "HTTP 200")) as probe_spy:
                got = dev_server.start(ROOT, "无关的服务", argv=argv, port=taken, wait=2.0)
        finally:
            _stop(squatter)
        self.assertFalse(got["ready"], "端口启动前就被占，却报成了自己的服务：%r" % got)
        self.assertIn("不是", got["error"])
        self.assertTrue(probe_spy.called, "根本没探过就没下结论，那是另一种形状")

    def test_a_free_port_still_reaches_ready(self):
        """反证：上面那条守卫不能把正常路径一起误伤（这是第一版真的发生过的事）。

        第一版把"启动前是否有人听"算在 `start_job` **之后** —— 那时端口上坐着的正是
        我们自己的进程，于是 `port_taken` 恒为 True，正向路径当场被误伤成
        `ready=False` + "等了 8s 没探测到就绪（HTTP 200）"这种自相矛盾的报告。
        """
        free = dev_server.free_port()
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        job = {}
        try:
            with patch.object(dev_server.process_runner, "start_job",
                              side_effect=lambda *a, **k: job.setdefault("rep", {
                                  "ok": True, "job_id": "probe-job"}) or job["rep"]), \
                    patch.object(dev_server, "_view",
                                 return_value={"logs": "", "state": "running"}), \
                    patch.object(dev_server, "probe", return_value=(True, "HTTP 200")):
                got = dev_server.start(ROOT, "绑到空闲端口的服务", argv=argv,
                                       port=free, wait=2.0)
        finally:
            if job.get("rep"):
                dev_server.process_runner.job_cancel("probe-job")
        self.assertTrue(got["ready"], got.get("error"))
        self.assertEqual(got["port"], free)

    def test_busy_before_must_be_asked_before_the_process_starts(self):
        """盯住顺序：`busy_before` 要在 `start_job` **之前**算。

        断言的是**顺序**而不是结果：把 `port_taken` 换成一个会记账的假实现，
        记录它被调用时子进程是否已经启动过。
        """
        order = []
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        free = dev_server.free_port()

        def _fake_start_job(*_a, **_k):
            order.append("start_job")
            return {"ok": True, "job_id": "order-probe"}

        def _fake_port_taken(_port, _host="127.0.0.1"):
            order.append("port_taken")
            return False

        try:
            with patch.object(dev_server.process_runner, "start_job", _fake_start_job), \
                    patch.object(dev_server, "port_taken", _fake_port_taken), \
                    patch.object(dev_server, "_view",
                                 return_value={"logs": "", "state": "running"}), \
                    patch.object(dev_server, "probe", return_value=(True, "HTTP 200")):
                dev_server.start(ROOT, "顺序探针", argv=argv, port=free, wait=1.5)
        finally:
            dev_server.process_runner.job_cancel("order-probe")
        self.assertEqual(order[:2], ["port_taken", "start_job"],
                         "必须先问端口是否已被占用，再启动子进程：%r" % order)

    def test_a_announced_port_is_treated_as_proof_of_ownership(self):
        """服务自己打印的端口是可证的归属——即使那个端口启动前就有人听（换了端口的 dev
        server 会先失败再挪，日志里两个地址都在）。这时不能因为 `busy_before` 就否掉。"""
        server = _serve()
        taken = server.server_address[1]
        job = {}
        try:
            with patch.object(dev_server.process_runner, "start_job",
                              side_effect=lambda *a, **k: job.setdefault("rep", {
                                  "ok": True, "job_id": "own-probe"}) or job["rep"]), \
                    patch.object(dev_server, "_view", return_value={
                        "logs": "Local: http://127.0.0.1:%d/\n" % taken,
                        "state": "running"}), \
                    patch.object(dev_server, "probe", return_value=(True, "HTTP 200")):
                got = dev_server.start(ROOT, "自报端口的服务", argv=argv_or_sleeper(),
                                       port=taken, wait=2.0)
        finally:
            _stop(server)
            if job.get("rep"):
                dev_server.process_runner.job_cancel("own-probe")
        self.assertTrue(got["ready"], got.get("error"))
        self.assertIn("认领", " ".join(got.get("notes") or []))


class ArgumentGuardTests(unittest.TestCase):
    """启动前的围栏：不在代码根目录内、不存在的目录都要拒。"""

    def test_it_refuses_a_cwd_outside_the_code_root(self):
        got = dev_server.start(ROOT, "越界", argv=["x"], cwd="../..")
        self.assertFalse(got["ok"])
        self.assertIn("代码根目录外", got["error"])

    def test_it_refuses_a_cwd_that_does_not_exist(self):
        """**在根目录之内**但本身不存在的目录。

        第一版用 `cwd="no-such-dir"`（相对 ROOT），可它先被"越界"检查拦下，
        于是断言的 `"工作目录不存在"` 那条**根本没被执行到**——测试看着在覆盖，
        实际覆盖的是另一条分支（电池里那条变异因此活了下来）。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            got = dev_server.start(root, "不存在的工作目录", argv=["x"],
                                  cwd="no-such-dir")
        self.assertFalse(got["ok"])
        self.assertIn("工作目录不存在", got["error"])
        # **要说是哪个目录**：只留一句"工作目录不存在"的话，调用方得自己猜是 cwd 还是根，
        # 而这条报错在整页守卫里常常是唯一线索。
        self.assertIn("no-such-dir", got["error"],
                      "报错没指出是哪个目录：%r" % got["error"])

    def test_it_refuses_a_missing_code_root(self):
        for bad in ("", str(ROOT / "no-such-root")):
            with self.subTest(root=bad):
                got = dev_server.start(bad, "根目录不存在", argv=["x"])
                self.assertFalse(got["ok"])
                self.assertIn("代码根目录不存在", got["error"])

    def test_it_refuses_an_empty_argv(self):
        got = dev_server.start(ROOT, "空命令", argv=[])
        self.assertFalse(got["ok"])
        self.assertIn("缺少要执行的命令", got["error"])


class LoopbackGuardTests(unittest.TestCase):
    def test_it_refuses_a_non_loopback_url(self):
        url, err = dev_server.loopback_url("http://10.0.0.5:8080/")
        self.assertEqual(url, "")
        self.assertIn("只允许本机回环地址", err)

    def test_it_accepts_loopback_forms(self):
        for raw in ("http://127.0.0.1:5173/", "localhost:8080", "http://[::1]:9000/x"):
            url, err = dev_server.loopback_url(raw)
            self.assertEqual(err, "", raw)
            self.assertTrue(url.startswith("http://"), url)

    def test_a_url_without_a_port_is_refused_unless_one_is_supplied(self):
        """缺端口不能默默补 80——那是把"没写端口"变成"去访问 80 端口"。

        `preview_project` 之类会把补出来的 URL 真的打开，所以默认补一个端口等于
        让调用方在没意识到的情况下访问另一个服务。
        """
        url, err = dev_server.loopback_url("http://127.0.0.1/", default_port=0)
        self.assertEqual(url, "")
        self.assertIn("缺少端口", err)
        # 显式给了 default_port 才补
        url2, err2 = dev_server.loopback_url("http://127.0.0.1/", default_port=5173)
        self.assertEqual(err2, "")
        self.assertEqual(url2, "http://127.0.0.1:5173/")

    def test_it_refuses_a_non_http_scheme(self):
        """只支持 http/https——`file://`、`ftp://` 之类不能被这个工具拿去读。"""
        for raw in ("ftp://127.0.0.1:21/", "file:///etc/passwd", "ws://127.0.0.1:5173/"):
            url, err = dev_server.loopback_url(raw)
            self.assertEqual(url, "", raw)
            self.assertIn("只支持 http/https", err, raw)


if __name__ == "__main__":
    unittest.main()
