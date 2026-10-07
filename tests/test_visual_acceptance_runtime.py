# -*- coding: utf-8 -*-
"""正式开发舱视觉适配器与预览证据链的离线契约测试。"""
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_runtime.preview_adapters import build_preview_bundle
from agent_runtime.visual_acceptance import (VisualAcceptanceError, _entry_file,
                                             _pid_alive, _close_reaper,
                                             browser_session, spawn_browser_in_job,
                                             sweep_stale_profiles)


class VisualAcceptanceRuntimeTests(unittest.TestCase):
    def test_entry_file_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = os.path.abspath(root)
            with open(os.path.join(root_path, "index.html"), "w", encoding="utf-8") as handle:
                handle.write("<h1>preview</h1>")
            self.assertTrue(_entry_file(__import__("pathlib").Path(root_path)).name == "index.html")
            with self.assertRaises(VisualAcceptanceError):
                _entry_file(__import__("pathlib").Path(root_path), "../outside.html")

    def test_tool_report_registers_screenshot_artifact(self):
        import tools

        report = {
            "passed": True,
            "image": "aGVsbG8=",
            "screenshot": ".docmind/visual-evidence/run/preview.png",
            "checks": {"page_loaded": True},
            "artifacts": [{
                "id": "visual-preview", "kind": "image",
                "path": ".docmind/visual-evidence/run/preview.png",
                "label": "真实浏览器预览截图",
            }],
        }
        with patch.object(tools, "_get_code_root", return_value="C:/project"), \
             patch("agent_runtime.visual_acceptance.capture_project_preview", return_value=report):
            result = tools.preview_project("entry: index.html")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["images"], ["aGVsbG8="])
        self.assertEqual(result.artifacts[0]["kind"], "image")

    def test_failed_report_without_image_stays_a_tool_failure(self):
        import tools

        report = {
            "passed": False,
            "checks": {"page_loaded": False},
            "runtime_errors": ["页面脚本异常"],
            "artifacts": [],
        }
        with (
            patch.object(tools, "_get_code_root", return_value="C:/project"),
            patch("agent_runtime.visual_acceptance.capture_project_preview", return_value=report),
        ):
            result = tools.preview_project("entry: index.html")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "visual_acceptance_failed")
        self.assertEqual(result.data["images"], [])
        self.assertIn("未通过", result.text)

    def test_workflow_preview_keeps_visual_artifact(self):
        bundle = build_preview_bundle({
            "workflow_id": "wf-visual", "status": "completed",
            "review": {"ok": True}, "results": {"results": {
                "tester": {"status": "ok", "artifacts": [{
                    "id": "visual-preview", "kind": "image",
                    "path": ".docmind/visual-evidence/run/preview.png",
                    "label": "真实浏览器预览截图",
                }]},
            }},
        })
        self.assertEqual(bundle["counts"]["by_kind"]["image"], 1)
        self.assertEqual(bundle["artifacts"][0]["adapter"], "generic")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_reaper_reaps_the_whole_tree_when_the_owner_dies(self):
        """父进程被硬杀时，整棵进程树必须由操作系统回收。

        这条断的是 462 个 headless Edge 的真实成因：Agent 会话被杀，`finally` 跑不到，
        而 taskkill 恰好就写在 `finally` 里。测的是机制本身，所以不起真浏览器 —— 用一层
        python 父子模拟「浏览器 root + 它派生的 renderer/gpu」，孙进程在出生之后才出现，
        正好验证「出生即在内」的作业对象会把后来派生的进程一并纳管。
        """
        sleeper = "import time;time.sleep(%d)" % 120
        with tempfile.TemporaryDirectory() as mailbox:
            pidfile = os.path.join(mailbox, "child.txt")
            parent, job, error = spawn_browser_in_job(
                [sys.executable, "-c",
                 "import subprocess,sys,time;"
                 "gp=subprocess.Popen([sys.executable,'-c',%r]);"
                 # 末尾那个 chr(10) 是**写入完成的标记**：读的人只在看见换行时
                 # 才认这个 pid，于是「只读到一半」被排除掉了。
                 "open(%r,'w').write(str(gp.pid)+chr(10));"
                 "time.sleep(120)" % (sleeper, pidfile)])
            self.assertIsNotNone(parent, "进程没能创建在作业对象里：%s" % error)
            grandchild = None
            try:
                # 等 pidfile 里出现**带结束标记的正 pid**。
                #
                # 原来只等 `os.path.exists()`，而父进程做的是 `open(f,'w').write(...)`：
                # `exists()` 在 `open()` 那一刻就为真，写还在后面。撞进那个窗口时
                # `read_text()` 返回空，`or "0"` 兜成 0 —— 而 `pid_alive(0)` 恒为 True
                # （它刻意的失败关闭：宁可不删，绝不误删）。于是最后那句 assertFalse
                # **恒失败**，而作业对象回收机制一点问题都没有：一条恒红的测试。
                #
                # 实测这个窗口在段 6 的负载下真会被撞上（3 轮红 1 轮），而失败消息与
                # 「孙进程真的没死」**完全无法区分** —— 这才是必须先堵它的原因。
                deadline = time.time() + 30
                raw = ""
                while time.time() < deadline:
                    try:
                        raw = pathlib.Path(pidfile).read_text()
                    except OSError:
                        raw = ""
                    if raw.endswith("\n") and raw.strip().isdigit():
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    raw.endswith("\n") and raw.strip().isdigit(),
                    "没读到孙进程 pid（读到 %r）—— 下面每一条关于孙进程的断言都是空的，"
                    "这条测试什么也没断" % (raw,))
                grandchild = int(raw.strip())
                self.assertNotEqual(grandchild, parent.pid,
                                    "读到的是父进程自己的 pid，pidfile 没写对：%d"
                                    % grandchild)
                self.assertIsNone(parent.poll(), "子进程应当还活着，否则这条测试什么都没断")
                # 正向对照：探针必须先证明「活着」，后面那句 assertFalse 才有意义。
                # 少了这一步，一个恒返回 False 的探针会让整条测试永远为真；反过来，
                # 一个恒返回 True 的探针会让它永远为假。所以这里把**实际读到的 pid**
                # 写进消息 —— 红的时候不必再回头猜读到了什么。
                self.assertTrue(_pid_alive(grandchild),
                                "存活探针失效（孙进程 %d 明明在跑却报没有），"
                                "后面的断言是空的" % grandchild)
                self.assertTrue(_pid_alive(parent.pid), "探针要能认出活进程，两个都要成立")
                _close_reaper(job)
                job = None
                deadline = time.time() + 15
                while parent.poll() is None and time.time() < deadline:
                    time.sleep(0.1)
                self.assertIsNotNone(parent.poll(), "关闭作业句柄后浏览器进程没有跟着退出")
                deadline = time.time() + 15
                while _pid_alive(grandchild) and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(_pid_alive(grandchild),
                                 "孙进程 %d 不在作业里，Chromium 的 renderer/gpu 子进程"
                                 "就是这么漏掉的（父进程 pid=%d，作业句柄已关）"
                                 % (grandchild, parent.pid))
            finally:
                if job is not None:
                    _close_reaper(job)
                for proc in (parent,):
                    if proc.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10)

    @unittest.skipUnless(os.name == "nt", "复现的是 Edge 在 Windows 上的退出文件锁")
    def test_profile_release_survives_the_lock_edge_leaves_on_the_way_out(self):
        """`finally` 跑到了也可能没删干净，所以删 profile 必须自己重试。

        正常退出实测 10/10 次留下目录：Edge 还锁着自己的文件我们就开始 rmtree，而
        `TemporaryDirectory(ignore_cleanup_errors=True)` 只是不抛异常，不等于删掉了。
        """
        from agent_runtime.visual_acceptance import _release_profile
        with tempfile.TemporaryDirectory() as outer:
            profile = tempfile.TemporaryDirectory(dir=outer, ignore_cleanup_errors=True)
            lockfile = os.path.join(profile.name, "leveldb.lock")
            pathlib.Path(lockfile).write_text("x")
            handle = open(lockfile, "rb")
            try:
                self.assertFalse(_release_profile(profile), "文件被占用时应当如实报告没删净")
                self.assertTrue(os.path.isdir(profile.name))
            finally:
                handle.close()
            self.assertTrue(_release_profile(profile))
            self.assertFalse(os.path.isdir(profile.name))

    def test_profile_sweep_needs_positive_evidence_that_the_browser_is_gone(self):
        """两条例据都要成立才删：归属 PID 没了，且 CDP 端口连不上。"""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        # 一个确定没人监听的端口
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        # 一个确定在监听的端口
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        live_port = listener.getsockname()[1]
        now = time.time()
        try:
            with tempfile.TemporaryDirectory() as root:
                def make(name: str, owner: str | None, port: int | None) -> str:
                    path = os.path.join(root, name)
                    os.mkdir(path)
                    if owner is not None:
                        with open(os.path.join(path, ".docmind-owner"), "w", encoding="ascii") as fh:
                            fh.write(owner)
                    if port is not None:
                        with open(os.path.join(path, "DevToolsActivePort"), "w",
                                  encoding="ascii") as fh:
                            fh.write("%d\n/dev/page\n" % port)
                    os.utime(path, (now - 24 * 3600, now - 24 * 3600))
                    return path

                abandoned = make("docmind-visual-preview-aaaaaaaa", str(dead.pid), dead_port)
                owner_alive = make("docmind-visual-preview-bbbbbbbb", str(os.getpid()), dead_port)
                browser_alive = make("docmind-visual-preview-cccccccc", str(dead.pid), live_port)
                no_port_file = make("docmind-visual-preview-dddddddd", str(dead.pid), None)
                unclaimed = make("docmind-visual-preview-eeeeeeee", None, dead_port)
                foreign = make("someone-else-preview-ffffffff", str(dead.pid), dead_port)

                removed = [os.path.basename(p) for p in sweep_stale_profiles(root)]

                # eeeeeeee 没有归属标记 = 改动前启动的会话留下的孤儿。端口探针就是为了
                # 能回收这一类：光靠 PID 判据的话，它们永远留在盘上。
                self.assertEqual(sorted(removed), ["docmind-visual-preview-aaaaaaaa",
                                                   "docmind-visual-preview-eeeeeeee"])
                self.assertTrue(os.path.isdir(owner_alive), "主人还活着的 profile 绝不能删")
                self.assertTrue(os.path.isdir(browser_alive),
                                "CDP 端口还在接受连接 = 浏览器活着，归属 PID 说了不算")
                self.assertTrue(os.path.isdir(no_port_file),
                                "读不到端口就当作还在用：宁可漏删，不可误删")
                self.assertTrue(os.path.isdir(foreign), "不是我们的前缀一律不碰")
                self.assertEqual(sweep_stale_profiles(os.path.join(root, "nope")), [])
        finally:
            listener.close()

    @unittest.skipUnless(os.name == "nt", "复现的是 Windows 上目录 mtime 不随子目录写入推进")
    def test_sweep_cannot_corrupt_a_live_browser_whose_mtime_looks_stale(self):
        """这条钉死我踩过的坑：mtime 不是「多久没用」的可靠代理。

        Windows 上往已存在的子目录反复写文件不会推进父目录 mtime，而 Edge 平时正是往
        Default\\Cache\\ 里写。旧实现按「mtime 超过 6 小时」判定可删，实测把一个正在用
        的 profile 从 222 个文件删到 95 个，同时返回空列表。
        判据用我们自己放的标记文件，不用文件总数：活浏览器一直在写自己的缓存，总数天然
        会涨（实测静置 1.5 秒 +26），拿相等断言永远不成立。标记文件 Edge 不会删，rmtree 会。
        """
        temp_root = pathlib.Path(tempfile.gettempdir())
        before = {p.name for p in temp_root.glob("docmind-visual-preview-*")}
        with browser_session(20.0) as devtools:
            devtools.call("Page.navigate", {"url": "about:blank"})
            devtools.drain(0.6)
            mine = next(p for p in temp_root.glob("docmind-visual-preview-*")
                        if p.name not in before)
            self.assertTrue((mine / "DevToolsActivePort").is_file(),
                            "没有端口文件就判断不了存活，这条测试会失去意义")
            markers = []
            for rel in (".docmind-probe-root", "Default/Cache/.docmind-probe-nested"):
                marker = mine / rel
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("keep me", encoding="utf-8")
                markers.append(marker)
            stale = time.time() - 7 * 3600
            os.utime(mine, (stale, stale))
            sweep_stale_profiles(temp_root)
            for marker in markers:
                self.assertTrue(marker.is_file(),
                                "活浏览器的 profile 被 sweep 删掉了：%s 不在了" % marker.name)
            self.assertTrue(mine.is_dir())


    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_browser_is_born_inside_the_job_not_attached_afterwards(self):
        """出生即在内，没有事后补绑的窗口。

        漏掉 EXTENDED_STARTUPINFO_PRESENT 时 CreateProcessW 会「成功」但属性列表被整个
        忽略，IsProcessInJob 为假——那会让我们无声地退回 150ms 竞态。这条断言就是防那个。
        """
        from agent_runtime.visual_acceptance import (_close_reaper, _in_job,
                                                     spawn_browser_in_job)
        process, job, error = spawn_browser_in_job(
            [sys.executable, "-c", "import time;time.sleep(30)"])
        self.assertIsNotNone(process, error)
        try:
            self.assertTrue(_in_job(process._handle, job),
                            "进程没有出生在作业对象里：属性列表被忽略了，竞态又回来了")
        finally:
            process.kill()
            process.wait(timeout=10)
            _close_reaper(job)

    def test_cleanup_deviations_are_reported_and_clean_runs_stay_silent(self):
        from agent_runtime.visual_acceptance import browser_session
        with patch("agent_runtime.visual_acceptance._release_profile", return_value=False):
            cleanup = {}
            with browser_session(20.0, cleanup=cleanup):
                pass
        self.assertFalse(cleanup["profile_released"], "偏离必须被如实记录")
        self.assertTrue(cleanup["spawned"])
        self.assertTrue(cleanup["job_closed"])
        clean = {}
        with browser_session(20.0, cleanup=clean):
            pass
        self.assertTrue(all(clean.values()), "正常收尾不该报偏离：%r" % clean)

    def test_preview_report_carries_cleanup_only_when_it_deviates(self):
        import base64 as b64mod
        import contextlib
        from agent_runtime import visual_acceptance as visual

        class StubDevtools:
            console, runtime_errors, failed_requests = [], [], []

            def call(self, method, params=None):
                if method == "Page.captureScreenshot":
                    return {"data": b64mod.b64encode(b"png").decode()}
                return {}

            def evaluate(self, expression):
                return ""

            def drain(self, seconds=0.0):
                pass

        @contextlib.contextmanager
        def fake_session(timeout=25.0, extra_flags=(), cleanup=None):
            if cleanup is not None:
                cleanup.update(fake_session.outcome)
            yield StubDevtools()

        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "index.html").write_text("<h1>x</h1>", encoding="utf-8")
            for outcome, expect_key in (({"spawned": True, "job_closed": True,
                                          "profile_released": True}, False),
                                        ({"spawned": True, "job_closed": True,
                                          "profile_released": False}, True)):
                fake_session.outcome = outcome
                with patch.object(visual, "browser_session", fake_session):
                    report = visual.capture_project_preview(root)
                self.assertEqual("cleanup" in report, expect_key,
                                 "正常收尾不该带 cleanup 键，偏离时必须带：%r" % outcome)
                if expect_key:
                    self.assertFalse(report["cleanup"]["profile_released"])
                    self.assertTrue(report["ok"], "收尾偏离不得伪装成页面验收失败")


# 静态门：不许在唯一的实现之外自己裸起浏览器。
#
# 旧版是「按守卫 token 逐文件豁免」，实测没有牙齿：往 cockpit 里注入一段旧式裸启动，
# 只要该文件别处还有一次合规调用，整个文件就被放行（已用变异验证过，门当时 1 passed）。
# 现在改成按文件白名单——谁都不许直接创建浏览器进程，只有实现 spawn helper 的那一个
# 文件例外。于是「文件里有别的合规调用」根本不构成豁免理由。
_SPAWN_HEAD = re.compile(
    r"\b(?:Popen|run|call|check_call|check_output|create_subprocess_exec|create_subprocess_shell"
    r"|execv|execve|execl|execvp|execvpe)\s*\(\s*\[?\s*"
    r"(?:(?:str\(\s*)?(?:[A-Za-z_]\w*\.)*(?:msedge|_edge_binary|\bedge\b|chrome\.exe|chromium)\b"
    r"|(?:r?[\"'])[^\"']*(?:msedge|chrome\.exe|chromium))",
    re.IGNORECASE)
_WIDE_SPAWN = re.compile(r"\bCreateProcessW\s*\(")
# argv 被提到变量里再传，上面两条都看不见。这里先收集「值最终来自浏览器二进制」的变量名
# （做到传递闭包：实测两级提升 `binpath = _edge_binary()` → `argv = [binpath]` → Popen(argv)
# 就会溜过单层判定），再看启动调用首项是不是这些名字。
# 刻意不做 AST 数据流：代价是 `x = 1  # chromium` 这类注释会误报——宁可多问一句，
# 也不要漏掉一整棵进程树。
_BROWSER_TOKEN = re.compile(r"msedge|_edge_binary|\bedge\b|chrome\.exe|chromium", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", re.M)
_SPAWN_ANY_HEAD = re.compile(
    r"\b(?:Popen|run|call|check_call|check_output|create_subprocess_exec|create_subprocess_shell"
    r"|execv|execve|execl|execvp|execvpe)\s*\(\s*\[?\s*(?:str\(\s*)?([A-Za-z_]\w*)\b")


def _browser_named_assignments(text: str) -> set:
    """收集「值最终来自浏览器二进制」的变量名，含多级提升。

    只走一层是不够的：实测 `binpath = _edge_binary()` → `argv = [binpath, ...]` →
    `Popen(argv)` 就溜过去了。这里做到不动点，并设一个上限防止自引用赋值死循环。
    """
    assignments = _ASSIGNMENT.findall(text)
    names = {name for name, rhs in assignments if _BROWSER_TOKEN.search(rhs)}
    for _ in range(8):
        grown = set(names)
        for name, rhs in assignments:
            if name in grown:
                continue
            if any(re.search(r"\b%s\b" % re.escape(known), rhs) for known in names):
                grown.add(name)
        if grown == names:
            break
        names = grown
    return names


def _raw_browser_spawns(text: str) -> list[str]:
    """返回文本里「把浏览器二进制直接当 argv 首项启动」的位置；命中不到回空列表。

    只认 argv 首项，不认「附近出现过 msedge」——否则 taskkill 那类调用会因为几百字符内
    有个 _msedge_pids_using 就误报，假阳性会最先毁掉一道门的公信力。
    """
    hits = [m.group(0).strip() for m in _SPAWN_HEAD.finditer(text)]
    hits += [m.group(0).strip() for m in _WIDE_SPAWN.finditer(text)]
    browser_names = _browser_named_assignments(text)
    for call in _SPAWN_ANY_HEAD.finditer(text):
        if call.group(1) in browser_names:
            hits.append("hoisted " + call.group(0).strip())
    return hits


class BrowserLaunchDisciplineTests(unittest.TestCase):
    """462 个 headless 进程把 CPU 顶到 99°C 之后立下的规矩：只有一条启动路径。"""

    SANCTIONED_FILES = {"agent_runtime/visual_acceptance.py"}
    # 探针字面量一律拼接而来：直接写在源码里会被上面那两条正则命中，
    # 于是门会因为自己的样本而红，而那看起来像是「有人新写了启动路径」。
    P = "Popen"
    RUN = "create_subprocess_exec"
    EXE = "execv"
    W = "W"

    def _sites(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        found = []
        for root in (repo / "tests", repo / "agent_runtime"):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                hits = _raw_browser_spawns(path.read_text(encoding="utf-8", errors="replace"))
                if hits:
                    found.append((path, hits))
        return found

    def _rel(self, repo, path) -> str:
        return str(path.relative_to(repo)).replace("\\", "/")

    def test_gate_own_source_is_not_flagged(self):
        """守门文件必须扫不出自己的探针，否则它红得没有意义。"""
        repo = pathlib.Path(__file__).resolve().parents[1]
        here = pathlib.Path(__file__).resolve()
        self.assertEqual(_raw_browser_spawns(here.read_text(encoding="utf-8")), [],
                         "本文件里的探针字面量必须靠拼接构造，不能直接写出启动形状")
        self.assertNotIn(self._rel(repo, here), {rel for rel, _ in
                                                 [(p, h) for p, h in self._sites()]})

    def test_detector_still_finds_the_sanctioned_implementation(self):
        """探测器连唯一的实现都扫不到，就说明它已经失明，而不是大家都干净。"""
        repo = pathlib.Path(__file__).resolve().parents[1]
        rels = {self._rel(repo, path) for path, _ in self._sites()}
        self.assertTrue(self.SANCTIONED_FILES <= rels,
                        "探测器没命中 %r：它失效了" % sorted(self.SANCTIONED_FILES - rels))

    def test_only_the_sanctioned_file_may_spawn_a_browser(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        for path, hits in self._sites():
            rel = self._rel(repo, path)
            with self.subTest(path=rel):
                self.assertIn(rel, self.SANCTIONED_FILES,
                              "在 %s 之外直接创建浏览器进程（%s）：这条路径没有回收保证，"
                              "请改走 browser_session / spawn_browser_in_job"
                              % (sorted(self.SANCTIONED_FILES), hits[:3]))

    def test_gate_does_not_flag_ordinary_prose(self):
        for text in (
            f"subprocess.{self.P}([sys.executable, '-c', 'handle a, edge case'])",
            f"subprocess.{self.P}([python, 'x.py'])  # knowledge: cutting-edge",
            f"subprocess.{self.P}(['node', 'build.js'], env={{'EDGE_TOKEN': '1'}})",
            f"subprocess.{self.P}([encoder, '--input', path])",
            f"subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'])  # 见 _msedge_pids_using",
        ):
            with self.subTest(text=text[:46]):
                self.assertEqual(_raw_browser_spawns(text), [])

    def test_gate_catches_every_raw_spawn_shape(self):
        for text in (
            f"subprocess.{self.P}([edge, *FLAGS, 'about:blank'])",
            f"subprocess.{self.P}([str(EDGE), '--headless=new'])",
            f"subprocess.{self.P}([visual._edge_binary(), *FLAGS])",
            f"subprocess.{self.P}([r'C:\\Program Files\\Microsoft\\Edge\\msedge.exe'])",
            f"asyncio.{self.RUN}('msedge.exe', '--headless=new')",
            f"os.{self.EXE}p('chrome.exe', argv)",
            f"kernel32.CreateProcess{self.W}(None, cmdline, None, None)",
            # argv 被提到变量里：靠「赋值右侧出现过浏览器 token」的名字回溯抓住
            f"BIN = visual._edge_binary()\nsubprocess.{self.P}([BIN, '--headless=new'])",
            f"cmd = [edge, '--headless=new']\nsubprocess.{self.P}(cmd)",
            # 两级提升：单层名字回溯漏过它，所以做了传递闭包
            ("BIN = visual._edge_binary()\nARGV = [BIN, '--headless=new']\n"
             f"subprocess.{self.P}(ARGV)"),
        ):
            with self.subTest(text=text[:46]):
                self.assertNotEqual(_raw_browser_spawns(text), [],
                                    "这一形漏了，门就是装饰：%s" % text)

    def test_hoisted_name_heuristic_does_not_flag_unrelated_names(self):
        """名字回溯不能变成「任何 Popen([某名字]) 都算」。"""
        for text in (
            f"count = len(items)\nsubprocess.{self.P}([count, '--fast'])",
            f"encoder = build_encoder(cfg)\nsubprocess.{self.P}([encoder, '--input', path])",
        ):
            with self.subTest(text=text[:46]):
                self.assertEqual(_raw_browser_spawns(text), [])


if __name__ == "__main__":
    unittest.main()
