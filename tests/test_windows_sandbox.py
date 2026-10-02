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


class JobCreationFailsClosedTests(unittest.TestCase):
    """起不出限位就必须是红——模块 docstring 就是这么承诺的（fail closed）。"""

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


class NestedProcessTreeTests(unittest.TestCase):
    """现场层：真作业对象里真拉一棵嵌套进程树。

    这是唯一能同时看见「常量」与「限位表」两处的地方——纯层只看得见常量。
    """

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
