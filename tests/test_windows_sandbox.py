"""Windows 作业对象的进程预算：这一档曾经把整条门禁压红（Wave22）。

**事故形状**（2026-10-02 实测，不是推演）：`_create_job` 给每个作业对象设
`ActiveProcessLimit = 4`。门禁整集那条子进程树是
`python -m dev_eval` → `python -m ruff` → `ruff.exe`——工具自己还会再开一层——而整集里
别的题同时还留着并发的后端/浏览器/git 进程。预算花光之后 `CreateProcess` 直接回
`WinError 1816`（配额不足），`diagnostics` 把它**误报成**「ruff 退出码 1 但输出没能解析
（可能被截断）」：退出码 1 的真实内容是一段 Python traceback，不是 lint 发现。
结果是门禁整集稳定红三题（`diagnostics-*` 两题 + `headcheck-catches-untracked-reference`），
而且它在检出和工作树里**都**红，于是"比失败题名单差集"把它当成公共噪声减掉了。

所以这里钉三件事：

1. 作业对象**必须**带 `KILL_ON_JOB_CLOSE`——那才是 99°C 漏浏览器真正的修法
   （`visual_acceptance._make_job` 至今只设这一个 flag）。抬高进程数上限不影响它。
2. 进程预算必须是**具名常量**且不低于实测峰值，不能再写回裸字面量 `4`。
3. 真在作业对象里起一棵嵌套进程树（Windows 现场），预算不够就会红。

**为什么第 3 条不能省**：纯层只看得到常量，于是"常量写 32、限位表里写 4"这种分叉它
一个都发现不了——本仓已经为同型错误付过一次账（`docs/HANDOFF-vibecoding.md` §9
「上限写两处，就等于写了一处旧的」）。现场层是唯一能同时看见两处的地方。

`ws` 这个别名是变异电池的接口：引擎把变异后的模块副本塞进本模块的 `ws`，
所以下面每一处都必须走 `ws.` 走属性，不能 `from ... import` 具体名字。
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_runtime.windows_sandbox as ws  # noqa: E402  电池按这个名字塞变异副本

#: 纯层与现场层共用的下限：**门禁整集的实测峰值是 7**（探针按作业对象
#: `QueryInformationJobObject` 采样，105 次采样 0 次失败）。所以"至少要能同时养住
#: 8 个"不是拍的，是"比整集实际用到过的最宽那一刻还多一个"。
REQUIRED_CONCURRENT = 8

#: `ACTIVE_PROCESS` 必须留着一个有限的刹车，所以预算也不能反过来写成无界。
#: 真正的资源上限是 `JOB_MEMORY`；这个数只是"别在触到内存上限之前先铺满进程表"。
CEILING = 256

#: 现场层那个孩子要并发拉起几个孙进程。
#:
#: 两个被实测纠正的形状：① `Popen` 在 `CreateProcess` 成功那一刻就返回，**不等进程真的
#: 起来**——所以"拉起 8 个"只证明系统肯创建，不证明 8 个**同时占着预算**。于是每个孙进程
#: 先写一个标记文件再睡，孩子等齐了标记才回报，中途 kill 一个还在加载 DLL 的 Python
#: 还会打出 `access violation`（第一版就这么吵）。② 只数 `Popen` 返回的个数，会把
#: 「创建成功但立刻被作业对象拒掉」和「真的活着」混成一个数。
_CHILD = r'''
import json, os, subprocess, sys, tempfile, time
want = int(sys.argv[1])
scratch = tempfile.mkdtemp(prefix="dm_process_budget_")
held, errors = [], []
for index in range(want):
    marker = os.path.join(scratch, "g%d" % index)
    code = "import os,sys,time;open(sys.argv[1],'w').close();time.sleep(120)"
    try:
        held.append(subprocess.Popen([sys.executable, "-c", code, marker]))
    except OSError as exc:
        errors.append("%d: %s: %s" % (index, type(exc).__name__, exc))
deadline = time.time() + 25
alive = 0
while time.time() < deadline:
    alive = sum(1 for i in range(want) if os.path.exists(os.path.join(scratch, "g%d" % i)))
    if alive >= want:
        break
    time.sleep(0.05)
print(json.dumps({"want": want, "created": len(held), "alive": alive, "errors": errors}))
for proc in held:
    proc.kill()
'''

#: 回收那条用的孩子：它**自己不会退出**（睡 300 秒），所以父进程消失只可能是因为
#: 作业对象句柄被关掉了。第一版的孩子会自己退出，于是"关句柄回收整棵树"这条断言
#: 什么都没验就绿了——这正是 §9 说的"判据可以结构上永远为真"。
#: `argv[1]` 是就绪标记：孙进程都起来之后才写，父进程据此确认"它真的活着"再关句柄。
_REAP_CHILD = r'''
import json, os, subprocess, sys, tempfile, time
ready = sys.argv[1]
scratch = tempfile.mkdtemp(prefix="dm_reap_")
markers = []
for index in range(2):
    marker = os.path.join(scratch, "r%d" % index)
    markers.append(marker)
    code = "import os,sys,time;open(sys.argv[1],'w').close();time.sleep(300)"
    subprocess.Popen([sys.executable, "-c", code, marker])
deadline = time.time() + 25
while time.time() < deadline and not all(os.path.exists(m) for m in markers):
    time.sleep(0.05)
with open(ready, "w", encoding="utf-8") as handle:
    json.dump({"grandchildren": [os.path.exists(m) for m in markers]}, handle)
time.sleep(300)
'''


#: 超时那条用的孩子：它自己**不写**标记，而是拉一个孙进程去写。
#:
#: 为什么必须多这一层（实测）：第一版的孩子直接写标记，于是「杀掉直接子进程」和
#: 「终止整个作业对象」两个机制**互为冗余**——只去掉其中一个，那条变异照样绿，
#: 电池就报两条"活下来"。而这两个机制的区别恰恰在于**孙进程**：作业对象能带走整棵树，
#: `process.kill()` 只能带走直接那一个。
_TIMEOUT_CHILD = r'''
import subprocess, sys, time
marker = sys.argv[1]
code = "import time, sys; time.sleep(6); open(sys.argv[1],'w').write('leaked')"
subprocess.Popen([sys.executable, "-c", code, marker])
time.sleep(30)
'''


class JobLimitDeclarationTests(unittest.TestCase):
    """限位表是怎么摆的。纯层——不碰内核，所以在任何平台都能跑，CI 上也有牙。"""

    def limits(self, memory_bytes: int = 1 << 20):
        return ws.job_limits(memory_bytes)

    def test_kill_on_job_close_is_always_requested(self):
        """99°C 漏浏览器的真正修法。别的 flag 都是资源刹车，只有这一个管"回收"。"""
        flags = self.limits().BasicLimitInformation.LimitFlags
        self.assertTrue(flags & ws.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                        "KILL_ON_JOB_CLOSE 掉了：作业对象一关，整棵树就没人收")

    def test_the_active_process_flag_is_still_requested(self):
        """它是第二道刹车，不能因为"内存上限够用"就顺手去掉。"""
        flags = self.limits().BasicLimitInformation.LimitFlags
        self.assertTrue(flags & ws.JOB_OBJECT_LIMIT_ACTIVE_PROCESS, "进程数刹车没了")

    def test_the_memory_caps_are_still_requested(self):
        """真正的资源上限是这两条；抬 ACTIVE_PROCESS 之后它们就是唯一的兜底。"""
        flags = self.limits().BasicLimitInformation.LimitFlags
        self.assertTrue(flags & ws.JOB_OBJECT_LIMIT_PROCESS_MEMORY, "单进程内存上限没了")
        self.assertTrue(flags & ws.JOB_OBJECT_LIMIT_JOB_MEMORY, "作业对象内存上限没了")

    def test_die_on_unhandled_exception_is_still_requested(self):
        """它今天在，删掉它会静默改变行为（孩子未捕获异常不再带走整棵树）。"""
        flags = self.limits().BasicLimitInformation.LimitFlags
        self.assertTrue(flags & ws.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
                        "孩子未捕获异常不再终止作业对象")

    def test_the_budget_is_above_the_measured_peak(self):
        """实测整集峰值 7。这里要求 8，并且**点名**事故值 4 是什么。"""
        self.assertGreaterEqual(
            ws.ACTIVE_PROCESS_LIMIT, REQUIRED_CONCURRENT,
            "进程预算 %s 低于门禁整集实测峰值 %s（2026-10-02 的事故值是 4）"
            % (ws.ACTIVE_PROCESS_LIMIT, REQUIRED_CONCURRENT))

    def test_the_budget_stays_finite(self):
        """抬一抬是为了合法进程树，不是为了放走失控的 fan-out。"""
        self.assertGreater(ws.ACTIVE_PROCESS_LIMIT, 0)
        self.assertLessEqual(
            ws.ACTIVE_PROCESS_LIMIT, CEILING,
            "进程预算没有上限了：坏循环能在触到内存上限之前先铺满进程表")

    def test_the_limit_table_and_the_constant_agree(self):
        """上限写两处就等于写了一处旧的（§9 那条教训）。

        这一条挡的是"常量写着 32、`job_limits` 里偷偷写 4"：纯层其余各条都只看常量，
        一个都发现不了，只有这条同时看两处——现场层则是从行为上发现。
        """
        self.assertEqual(self.limits().BasicLimitInformation.ActiveProcessLimit,
                         ws.ACTIVE_PROCESS_LIMIT)

    def test_the_memory_numbers_reach_the_table(self):
        """限位表里只有 flag、没有数，作业对象就等于没有内存上限。"""
        limits = self.limits(4096)
        self.assertEqual(limits.ProcessMemoryLimit, 4096)
        self.assertEqual(limits.JobMemoryLimit, 4096)

    def test_each_job_gets_its_own_table(self):
        """不能返回同一个模块级对象——两个作业对象会共用一张表，后一个覆盖前一个。"""
        first, second = self.limits(4096), self.limits(8192)
        self.assertIsNot(first, second)
        self.assertEqual(second.ProcessMemoryLimit, 8192)
        self.assertEqual(first.ProcessMemoryLimit, 4096)


def _run_isolated(code, **kwargs):
    """跑一条子进程命令。子进程只吐 ASCII：`run_isolated` 用 `text=True` 且没指定
    encoding，按本机区域编码解码；中文输出会让读线程抛异常、那一路变成 None
    （本轮报告里的编码缺口）。这里要验的是"输出接得回来、两路分得开"，不是编码策略。"""
    return ws.run_isolated([sys.executable, "-c", code], cwd=str(ROOT),
                           env=dict(os.environ), timeout=60, **kwargs)


class RunIsolatedOutputTests(unittest.TestCase):
    """`run_isolated` 跨平台可验的那半边：输出接得回来、两路分得开、退出码是自己的。

    此前 `run_isolated` 与 `spawn_isolated` 共用 `_create_job` 却只有后者有测试，于是
    「适配器进程的边界」这条承诺没人验证。它在非 Windows 上走 `subprocess.run`
    分支，所以这几条在任何平台都跑得到。
    """

    def test_the_child_output_comes_back(self):
        result = _run_isolated("print('adapter-output')")
        self.assertEqual(result.returncode, 0, result.stderr[:200])
        self.assertIn("adapter-output", result.stdout)

    def test_a_failing_child_reports_its_own_exit_code(self):
        result = _run_isolated("import sys; sys.stderr.write('adapter-stderr'); sys.exit(3)")
        self.assertEqual(result.returncode, 3, result.stdout[:200])
        self.assertIn("adapter-stderr", result.stderr)

    def test_stdout_and_stderr_stay_apart(self):
        """两个流不能被合并——上层要靠 stderr 认"这是失败不是输出"。"""
        result = _run_isolated("import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR')")
        self.assertIn("OUT", result.stdout)
        self.assertNotIn("ERR", result.stdout)
        self.assertIn("ERR", result.stderr)
        self.assertNotIn("OUT", result.stderr)

    # ---- Wave29（§6 缺口 13）：解码 ----

    def test_utf8_text_is_not_swallowed_by_the_console_encoding(self):
        """本机 cp936；GBK 解不了 UTF-8 中文——原来整条 stdout 变成 `None`。

        `text=True` 而不给 `encoding` 时 `Popen` 按**本机区域编码**解码，读线程抛
        `UnicodeDecodeError`，而 `communicate()` 把那一路返回成 `None`：实测中文、非 UTF-8
        字节、甚至"只写 stderr"全都是 `rc=0` + 空输出，唯一线索是一个打印到控制台、
        调用方永远看不见的后台线程异常。
        """
        result = _run_isolated("print('适配器输出')")
        self.assertIsNotNone(result.stdout, "stdout 是 None：读线程抛了解码异常")
        self.assertIn("适配器输出", result.stdout)

    def test_stderr_is_not_swallowed_either(self):
        """stdout 与 stderr 走两个独立的读线程，一条坏掉不该连累另一条。"""
        result = _run_isolated("import sys; sys.stderr.write('适配器 stderr')")
        self.assertIsNotNone(result.stderr, "stderr 是 None")
        self.assertIn("适配器 stderr", result.stderr)

    def test_undecodable_bytes_degrade_to_replacement_not_to_nothing(self):
        """非 UTF-8 字节变成 U+FFFD，但**长度与结构仍然可见**。

        这是这次取舍的核心：整条流消失（原来）比出现替换字符坏得多——上层据此至少能
        判断"有输出、里面有乱码"，而不是"没有输出"。
        """
        result = _run_isolated(r"import sys; sys.stdout.buffer.write(b'\xff\xfe bad')")
        self.assertIsNotNone(result.stdout)
        self.assertTrue(result.stdout.strip(), "输出整条消失了——这正是要修的形状")
        self.assertIn("�", result.stdout)
        self.assertIn("bad", result.stdout)

    def test_both_platform_branches_declare_the_same_decoding(self):
        """Windows 与非 Windows 两条分支必须**用同一套**，否则跨平台现象不一致。"""
        self.assertEqual(ws.SUBPROCESS_ENCODING, "utf-8")
        self.assertEqual(ws.SUBPROCESS_DECODE_ERRORS, "replace")
        source = Path(ROOT, "agent_runtime", "enterprise_sandbox.py").read_text(
            encoding="utf-8")
        self.assertIn("SUBPROCESS_ENCODING", source,
                      "enterprise_sandbox 的非 Windows 分支没共用那套解码")
        self.assertIn("SUBPROCESS_DECODE_ERRORS", source)


class WindowsOnlyLiveTests(unittest.TestCase):
    """**本电池的 LIVE 层：只有 Windows 才验得了的那一层**——注意它与别的电池的
    LIVE 含义不同：那边是"需要真浏览器"，这边是"作业对象是 Windows 机制"。

    为什么要这么分组（这不是洁癖，是 CI 会红）：`.github/workflows/harness.yml` 在
    **ubuntu-latest** 上跑 `run_all.py --no-live`。一个只被 `@skipUnless(os.name == "nt")`
    用例抓住的变异，在 ubuntu 上那类用例被 skip → 无人抓 → 记成**活下来** → 电池红、
    CI 步骤失败。声明成 LIVE 之后，它在 ubuntu 上记「本环境未验证」——既不算抓住，
    也不算活下来，这才是不谎报。
    """

    @staticmethod
    def _stub(**functions):
        """替身 kernel32。函数挂在**实例**上而不是类上：挂类上会变成绑定方法，
        而 `_create_job` 要往这些属性上写 `argtypes`/`restype`。"""
        stub = type("K32", (), {})()
        for name, fn in functions.items():
            setattr(stub, name, fn)
        return stub

    def _with_stub(self, stub):
        real = ctypes.WinDLL
        ctypes.WinDLL = lambda *_a, **_k: stub
        self.addCleanup(setattr, ctypes, "WinDLL", real)

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_a_job_that_refuses_our_limits_is_never_handed_back(self):
        closed = []
        # 限位设不上：必须抛，而且句柄要还回去，不能漏一个。
        self._with_stub(self._stub(
            CreateJobObjectW=lambda *_a: 4321,
            SetInformationJobObject=lambda *_a: 0,
            CloseHandle=lambda handle: closed.append(handle),
        ))
        with self.assertRaises(ws.SandboxUnavailable):
            ws._create_job(1 << 20)
        self.assertEqual(closed, [4321], "限位没设上的作业对象把句柄漏了")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_a_job_that_cannot_be_created_is_refused(self):
        # `SetInformationJobObject` 走不到，但 `_create_job` 会先往它身上写 argtypes，
        # 所以属性必须先在。
        self._with_stub(self._stub(
            CreateJobObjectW=lambda *_a: 0,
            SetInformationJobObject=lambda *_a: 1,
        ))
        with self.assertRaises(ws.SandboxUnavailable):
            ws._create_job(1 << 20)


    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_it_refuses_to_run_unbounded_when_the_job_cannot_be_created(self):
        """起不出作业对象必须是红——这个模块的承诺就是 fail closed，不是"降级跑一把"。"""
        self._with_stub(self._stub(
            CreateJobObjectW=lambda *_a: 0,
            SetInformationJobObject=lambda *_a: 1,
        ))
        with self.assertRaises(ws.SandboxUnavailable):
            _run_isolated("print('never-runs')")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_it_refuses_to_run_when_the_process_cannot_be_assigned(self):
        """加入作业对象失败也必须是红——否则那棵树从此没人回收，正是 99°C 的形状。

        `run_isolated` 与 `spawn_isolated` 的 `if ... AssignProcessToJobObject` 那一行
        **字面完全相同**，所以电池里对应那条变异的锚点必须带上下文才唯一。
        """
        self._with_stub(self._stub(
            CreateJobObjectW=lambda *_a: 4321,
            SetInformationJobObject=lambda *_a: 1,
            AssignProcessToJobObject=lambda *_a: 0,
            CloseHandle=lambda *_a: 0,
        ))
        with self.assertRaises(ws.SandboxUnavailable):
            _run_isolated("print('never-runs')")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_a_process_started_before_the_refusal_is_still_reaped(self):
        """拒绝时那个子进程**已经起来了**，必须被收掉——这才是"边界"的意思。

        `run_isolated` 先 `Popen` 再 `AssignProcessToJobObject`，所以"加入失败"这条路上
        有一个已经存在的进程；`finally` 里那层兜底就是为它准备的。判据仍是标记文件：
        孩子 5 秒后写标记，我们等 7 秒——它若还活着就写出来了。
        """
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "leaked.txt"
            code = ("import time, pathlib; time.sleep(5); "
                    "pathlib.Path(r'%s').write_text('leaked')" % marker)
            self._with_stub(self._stub(
                CreateJobObjectW=lambda *_a: 4321,
                SetInformationJobObject=lambda *_a: 1,
                AssignProcessToJobObject=lambda *_a: 0,
                CloseHandle=lambda *_a: 0,
            ))
            with self.assertRaises(ws.SandboxUnavailable):
                ws.run_isolated([sys.executable, "-c", code], cwd=root,
                                env=dict(os.environ), timeout=30)
            time.sleep(7)
            self.assertFalse(marker.exists(),
                             "已经拒绝执行了，那个子进程却还活着——每次这种失败都会漏一个")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_a_timeout_kills_the_tree_rather_than_merely_giving_up(self):
        """超时必须是**杀掉整棵树**，不是抛个异常就不管了。

        判据是那个标记文件：孩子拉一个孙进程，孙进程 6 秒后写标记。若只杀掉直接那个孩子
        （`process.kill()` 而不终止作业对象），孙进程会活下来把标记写出来——这正是
        `KILL_ON_JOB_CLOSE` / `TerminateJobObject` 存在的理由，也正是 99°C 事故的形状。
        """
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "late.txt"
            child = Path(root) / "timeout_child.py"
            child.write_text(_TIMEOUT_CHILD, encoding="utf-8")
            start = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                ws.run_isolated([sys.executable, str(child), str(marker)], cwd=root,
                                env=dict(os.environ), timeout=1)
            self.assertLess(time.time() - start, 3.5, "超时没有立刻生效")
            time.sleep(8)
            self.assertFalse(marker.exists(),
                             "超时之后孙进程还在跑并写出了标记——只收了直接那个孩子，"
                             "整棵树没收走")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_a_nested_tree_at_least_as_wide_as_the_gate_can_still_spawn(self):
        with tempfile.TemporaryDirectory() as root:
            child = Path(root) / "child.py"
            child.write_text(_CHILD, encoding="utf-8")
            popen, kernel32, job = ws.spawn_isolated(
                [sys.executable, str(child), str(REQUIRED_CONCURRENT)],
                cwd=root, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            try:
                out, _ = popen.communicate(timeout=180)
            finally:
                ws.terminate_isolated(kernel32, job, exit_code=0)
        lines = [line for line in (out or b"").decode("utf-8", "replace").splitlines() if line.strip()]
        self.assertTrue(lines, "孩子没有回报并发结果：%r" % (out,))
        payload = json.loads(lines[-1])
        self.assertEqual(payload["errors"], [],
                         "作业对象的进程预算不够，孩子拉进程时被拒了"
                         "（WinError 1816 = 配额不足）：%s" % payload["errors"])
        # `alive` 而不是 `created`：CreateProcess 成功不代表进程真的起来了。
        # 要证明的是"这么宽的一棵树能同时占住预算"，不是"系统肯创建这么多句柄"。
        self.assertEqual(payload["alive"], REQUIRED_CONCURRENT,
                         "只有 %s/%s 个孙进程真的同时活着（这一条就是 2026-10-02 "
                         "那次事故的形状：预算 4 撑不住这条树）" % (
                             payload["alive"], REQUIRED_CONCURRENT))

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_the_job_is_reaped_when_its_handle_closes(self):
        """预算抬宽之后，回收不能变松：句柄一关，整棵树必须跟着走。

        这条是 99°C 那条教训的对应项——`ACTIVE_PROCESS` 可以放宽，
        `KILL_ON_JOB_CLOSE` 不行。孩子自己会睡 300 秒，所以它消失只可能是被收走的。
        """
        with tempfile.TemporaryDirectory() as root:
            child = Path(root) / "child.py"
            ready = Path(root) / "ready.json"
            child.write_text(_REAP_CHILD, encoding="utf-8")
            popen, kernel32, job = ws.spawn_isolated(
                [sys.executable, str(child), str(ready)], cwd=root,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            # 先确认它真的起来了，否则"它不见了"可能只是它从没活过。
            deadline = time.time() + 90
            while not ready.exists() and popen.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(ready.exists(), "孩子没就绪就退出了，这条用例什么都没验")
            self.assertIsNone(popen.poll(), "孩子就绪之后不该自己退出——它睡 300 秒")
            # 不 terminate_isolated：只关句柄，模拟作业对象被丢弃。
            kernel32.CloseHandle(job)
            deadline = time.time() + 30
            while popen.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            leaked = popen.poll() is None
            if leaked:
                popen.kill()
            self.assertFalse(leaked,
                             "作业对象句柄关掉之后，子进程还活着——"
                             "KILL_ON_JOB_CLOSE 没起作用，会变成下一个 99°C")


if __name__ == "__main__":
    unittest.main()
