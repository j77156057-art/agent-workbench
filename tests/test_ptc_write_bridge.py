# -*- coding: utf-8 -*-
"""PTC 路径 B —— B2 写工具 bridge 回归（第54批 · 逐步扩，§6.2 步8 收口）。

本文件随 §6.2 各步增量增长。当前覆盖：
  步 1 · 独立写开关 DOCMIND_PTC_BRIDGE_WRITE（叠加主开关，默认关）：
    - flag 分层：主开关关时写开关再开也失效（逐字节回退到 B1/全关）。
    - 白名单按 flag 分读 / 读+写。
    - jail 前缀 stub：仅主开 → 只含只读包装、不含写包装；主+写开 → 含写包装；全关 → 无 stub。
    - 服务环：写开关关 → 写名仍被白名单闸拒（=B1，rejected、非 pending）；
      写开关开 → 写名转延后占位（pending、未执行、server 零调用、executed 不计），
      planned==handled 仍成立，诚实"未执行"标注出现。
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
import agent as agent_mod  # noqa: E402
from config import set_runtime  # noqa: E402


_READ_ONLY = frozenset({
    "calculate", "memory_search", "search_knowledge", "search_code",
    "grep", "read_file", "list_dir", "web_search", "web_fetch", "web_research",
})
_WRITE_SEED = frozenset({
    "apply_edit", "create_file", "generate_data_file",
    "create_external_file", "edit_external_file", "delete_external_file",
    "memory_save", "memory_forget",
    "dev_apply_edits", "dev_patch", "dev_move", "dev_refactor",
    "dev_region_edit", "dev_add_region", "dev_apply_regions", "init_regions",
    "dev_plan", "dev_lanes", "dev_propose", "dev_pricing", "dev_deps",
})


class FlagLayeringTests(unittest.TestCase):
    """写开关必须**叠加**在主开关之上：主关 → 写开关再开也失效。"""

    def _reload(self, main, write):
        env = dict(os.environ)
        env.pop("DOCMIND_PTC_BRIDGE", None)
        env.pop("DOCMIND_PTC_BRIDGE_WRITE", None)
        if main is not None:
            env["DOCMIND_PTC_BRIDGE"] = main
        if write is not None:
            env["DOCMIND_PTC_BRIDGE_WRITE"] = write
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(tools_mod)

    def tearDown(self):
        # 复原：清掉两个 env 再 reload，避免污染后续用例的全局常量。
        env = dict(os.environ)
        env.pop("DOCMIND_PTC_BRIDGE", None)
        env.pop("DOCMIND_PTC_BRIDGE_WRITE", None)
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(tools_mod)

    def test_default_off(self):
        self._reload(None, None)
        self.assertFalse(tools_mod._PTC_BRIDGE_ENABLED)
        self.assertFalse(tools_mod._PTC_BRIDGE_WRITE_ENABLED)
        self.assertEqual(tools_mod._ptc_bridge_tool_names(), frozenset())

    def test_main_on_write_off_readonly_only(self):
        self._reload("1", "0")
        self.assertTrue(tools_mod._PTC_BRIDGE_ENABLED)
        self.assertFalse(tools_mod._PTC_BRIDGE_WRITE_ENABLED)
        self.assertEqual(tools_mod._ptc_bridge_tool_names(), _READ_ONLY)
        self.assertEqual(tools_mod._ptc_bridge_write_tool_names(), frozenset())

    def test_main_on_write_on_union(self):
        self._reload("1", "1")
        self.assertTrue(tools_mod._PTC_BRIDGE_WRITE_ENABLED)
        self.assertEqual(tools_mod._ptc_bridge_tool_names(), _READ_ONLY | _WRITE_SEED)
        self.assertEqual(tools_mod._ptc_bridge_write_tool_names(), _WRITE_SEED)

    def test_main_off_write_on_still_disabled(self):
        # 分层关键：主开关关，写开关开也必须整体失效（回退到"关"的字节等价）。
        self._reload("0", "1")
        self.assertFalse(tools_mod._PTC_BRIDGE_ENABLED)
        self.assertFalse(tools_mod._PTC_BRIDGE_WRITE_ENABLED)
        self.assertEqual(tools_mod._ptc_bridge_tool_names(), frozenset())
        self.assertEqual(tools_mod._ptc_bridge_write_tool_names(), frozenset())


class StubSeamTests(unittest.TestCase):
    """jail 前缀 stub 按 flag 暴露写包装；全关逐字节无 stub。"""

    def _prefix(self, main, write):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", main), \
                patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", write):
            return tools_mod._python_exec_jail_prefix(1)

    def test_off_no_stub(self):
        p = self._prefix(False, False)
        self.assertNotIn("__BRIDGE_REQ__", p)
        self.assertNotIn("def apply_edit(", p)
        self.assertNotIn("def calculate(", p)

    def test_main_only_read_wrappers_no_write(self):
        p = self._prefix(True, False)
        self.assertIn("def calculate(", p)
        self.assertNotIn("def apply_edit(", p)   # 写开关关 → 不暴露写包装

    def test_main_and_write_exposes_write(self):
        p = self._prefix(True, True)
        self.assertIn("def calculate(", p)
        self.assertIn("def apply_edit(", p)      # 写开关开 → 暴露写包装（延后由服务环兜）


class _WriteLoopHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tools_mod.clear_bridge_writes()   # 隔离：延后写队列是模块全局，清掉跨用例残留/同参去重

    def tearDown(self):
        tools_mod.clear_bridge_writes()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, code, server, write_enabled, main_enabled=True, trace_path=None):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", main_enabled), \
                    patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", write_enabled), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", server), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", trace_path):
                return tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)


class ServiceLoopWriteDeferredTests(_WriteLoopHarness):
    def test_write_rejected_when_flag_off_equals_b1(self):
        log = []

        def server(name, arg):
            log.append(name)
            return (True, "X")

        code = ('ok, r = _bridge_call("apply_edit", "somefile")\n'
                'print("RES", ok)\nprint("END")')
        out = self._run(code, server, write_enabled=False)
        self.assertIn("END", out)
        self.assertEqual(log, [])                     # 写名未进 server
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 1)       # 白名单闸拒（=B1）
        self.assertEqual(st.get("pending", 0), 0)     # 关态不产生 pending
        self.assertEqual(st.get("executed"), 0)

    def test_write_deferred_when_flag_on(self):
        # 写开关开：apply_edit 转延后占位——server 零调用、executed 不计、pending 计、未执行。
        log = []

        def server(name, arg):
            log.append(name)
            return (True, "SHOULD_NOT_RUN")

        code = ('ok, r = apply_edit("path/to/x")\n'
                'print("OK" if ok else "DEFERRED", str(r))\nprint("END")')
        out = self._run(code, server, write_enabled=True)
        self.assertIn("END", out)
        self.assertIn("DEFERRED", out)                # 桥回 ok=False
        self.assertEqual(log, [])                     # 关键：写名绝不经 _serve/execute_tool
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("pending"), 1)
        self.assertEqual(st.get("executed"), 0)
        self.assertEqual(st.get("rejected"), 0)       # 不是白名单拒，是延后

    def test_mixed_read_and_write_reconciled(self):
        log = []

        def server(name, arg):
            log.append((name, arg))
            return (True, "ok:%s" % arg)

        code = ('a, ra = calculate("1+1")\n'
                'b, rb = apply_edit("x")\n'
                'c, rc = grep("needle")\n'
                'print("A", a, "B", b, "C", c)\nprint("END")')
        out = self._run(code, server, write_enabled=True)
        self.assertIn("END", out)
        self.assertEqual(log, [("calculate", "1+1"), ("grep", "needle")])  # 只读进 server
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("planned"), 3)
        self.assertEqual(st.get("handled"), 3)
        self.assertTrue(st.get("reconciled"))
        self.assertEqual(st.get("executed"), 2)       # 两条只读真跑
        self.assertEqual(st.get("pending"), 1)        # 一条写延后
        # 诚实标注：本段有写子调用一律延后·未执行
        self.assertIn("\u4e00\u5f8b\u5ef6\u540e", out)   # "一律延后"

    def test_honest_note_absent_when_no_writes(self):
        def server(name, arg):
            return (True, "ok")

        code = 'a, ra = calculate("1")\nprint("END")'
        out = self._run(code, server, write_enabled=True)
        self.assertNotIn("\u4e00\u5f8b\u5ef6\u540e", out)  # 无写 → 不该有延后标注


class TierGatePureTests(unittest.TestCase):
    """步 2：父侧 `_ptc_bridge_write_tier` 估档（镜像子 jail 钩）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        set_runtime("code_root", self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arg(self, path):
        return "path: %s\nnew_text: x" % path

    def test_path_inside_root_is_tier1(self):
        p = os.path.join(self.tmp, "sub", "x.py")
        tier, reason, path = tools_mod._ptc_bridge_write_tier("apply_edit", self._arg(p))
        self.assertEqual(tier, 1)
        self.assertEqual(path, p)

    def test_credential_path_is_hard_reject(self):
        tier, reason, _ = tools_mod._ptc_bridge_write_tier(
            "apply_edit", self._arg(os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa")))
        self.assertEqual(tier, -1)

    def test_system_dir_path_is_hard_reject(self):
        tier, reason, _ = tools_mod._ptc_bridge_write_tier(
            "create_file", self._arg("C:/Windows/System32/drivers/foo.sys"))
        self.assertEqual(tier, -1)

    def test_memory_save_no_path_conservative_tier2(self):
        tier, reason, path = tools_mod._ptc_bridge_write_tier(
            "memory_save", '{"kind":"fact","content":"x"}')
        self.assertEqual(tier, 2)
        self.assertIsNone(path)

    def test_unknown_tool_conservative_tier2(self):
        tier, reason, _ = tools_mod._ptc_bridge_write_tier("whatever_mutator", "x")
        self.assertEqual(tier, 2)


class ServiceLoopTierGateTests(_WriteLoopHarness):
    def _srv(self, log):
        def server(name, arg):
            log.append(name)
            return (True, "X")
        return server

    def test_credential_write_hard_rejected_no_pending(self):
        log = []
        cred = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: boom")\n'
                'print("OK" if ok else "REJECTED")\nprint("END")' % cred.replace("\\", "/"))
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertIn("REJECTED", out)
        self.assertEqual(log, [])                       # 绝不经 server/execute_tool
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("write_rejected"), 1)
        self.assertEqual(st.get("pending", 0), 0)       # 硬拒不登记待确认
        self.assertEqual(st.get("executed"), 0)
        self.assertIn("\u786c\u5e95\u7ebf", out)         # "硬底线"

    def test_root_internal_write_deferred_pending(self):
        log = []
        rp = os.path.join(self.tmp, "sub", "x.py").replace("\\", "/")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: hi")\n'
                'print("OK" if ok else "DEFERRED")\nprint("END")' % rp)
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertEqual(log, [])
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("pending"), 1)          # 根内写也一律延后（保守）
        self.assertEqual(st.get("write_rejected"), 0)
        # 关键：当场没落盘
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sub", "x.py")))

    def test_mixed_executed_pending_write_rejected(self):
        log = []
        rp = os.path.join(self.tmp, "a.py").replace("\\", "/")
        cred = os.path.join(os.path.expanduser("~"), ".aws", "credentials").replace("\\", "/")
        code = ('a, ra = calculate("1+1")\n'
                'b, rb = apply_edit("path: %s\\nnew_text: hi")\n'
                'c, rc = apply_edit("path: %s\\nnew_text: boom")\n'
                'print("END")' % (rp, cred))
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertEqual(log, ["calculate"])            # 只有一条只读进 server
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("executed"), 1)
        self.assertEqual(st.get("pending"), 1)          # 根内写延后
        self.assertEqual(st.get("write_rejected"), 1)   # 凭据写硬拒
        self.assertEqual(st.get("planned"), 3)
        self.assertEqual(st.get("handled"), 3)
        self.assertTrue(st.get("reconciled"))


class PendingQueueTests(unittest.TestCase):
    """步 3：延后写台账纯单测——登记/同参去重/确认门槛/拒绝/执行器未注入 fail-closed。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        set_runtime("code_root", self.tmp)
        tools_mod.clear_bridge_writes()

    def tearDown(self):
        tools_mod.clear_bridge_writes()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stage_registers_without_touching_disk(self):
        pid, replayed = tools_mod.stage_bridge_write("apply_edit", "path: x\nnew_text: hi", 2, "t")
        self.assertFalse(replayed)
        self.assertTrue(pid.startswith("W"))
        items = tools_mod.list_bridge_writes()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], pid)
        self.assertEqual(items[0]["tool"], "apply_edit")
        self.assertGreaterEqual(items[0]["need"], 1)     # bridged 写至少需 1 次确认

    def test_same_arg_dedup_returns_same_pid_replayed(self):
        p1, r1 = tools_mod.stage_bridge_write("apply_edit", "path: y\nnew_text: hi", 2, "t")
        p2, r2 = tools_mod.stage_bridge_write("apply_edit", "path: y\nnew_text: hi", 2, "t")
        self.assertFalse(r1)
        self.assertTrue(r2)                              # 第二条同参 → replayed
        self.assertEqual(p1, p2)                         # 指向同一 pid
        self.assertEqual(len(tools_mod.list_bridge_writes()), 1)  # 不重复登记

    def test_different_arg_registers_separately(self):
        p1, _ = tools_mod.stage_bridge_write("apply_edit", "path: a\nnew_text: 1", 2, "t")
        p2, _ = tools_mod.stage_bridge_write("apply_edit", "path: b\nnew_text: 2", 2, "t")
        self.assertNotEqual(p1, p2)
        self.assertEqual(len(tools_mod.list_bridge_writes()), 2)

    def test_peek_reports_existing_and_none(self):
        self.assertIsNone(tools_mod._peek_bridge_write("apply_edit", "path: z\nnew_text: q"))
        pid, _ = tools_mod.stage_bridge_write("apply_edit", "path: z\nnew_text: q", 2, "t")
        self.assertEqual(tools_mod._peek_bridge_write("apply_edit", "path: z\nnew_text: q"), pid)

    def test_confirm_below_threshold_not_executed(self):
        pid, _ = tools_mod.stage_bridge_write("apply_edit", "path: c\nnew_text: hi", 3, "t")
        need = next(e["need"] for e in tools_mod.list_bridge_writes() if e["id"] == pid)
        self.assertGreater(need, 1)
        calls = []
        with patch.object(tools_mod, "_PTC_BRIDGE_WRITE_EXECUTOR",
                          lambda n, a: calls.append(n) or (True, "RAN")):
            for _ in range(need - 1):                     # 差一次到阈值
                ok, msg = tools_mod.confirm_bridge_write(pid)
                self.assertFalse(ok)
                self.assertIn("尚未执行", msg)
        self.assertEqual(calls, [])                       # 未达阈值绝不调用执行器
        self.assertTrue(any(e["id"] == pid for e in tools_mod.list_bridge_writes()))

    def test_confirm_at_threshold_executes_via_executor_once(self):
        arg = "path: d\nnew_text: hi"
        pid, _ = tools_mod.stage_bridge_write("apply_edit", arg, 2, "t")
        need = next(e["need"] for e in tools_mod.list_bridge_writes() if e["id"] == pid)
        calls = []

        def executor(name, a):
            calls.append((name, a))
            return (True, "已应用修改 d")

        with patch.object(tools_mod, "_PTC_BRIDGE_WRITE_EXECUTOR", executor):
            ok = None
            for _ in range(need):                         # 恰好确认到阈值
                ok, msg = tools_mod.confirm_bridge_write(pid)
        self.assertTrue(ok)
        self.assertEqual(calls, [("apply_edit", arg)])    # 执行器恰好调用一次、透传原参
        self.assertFalse(any(e["id"] == pid for e in tools_mod.list_bridge_writes()))  # 已移除

    def test_confirm_at_threshold_no_executor_fail_closed_keeps_pending(self):
        arg = "path: e\nnew_text: hi"
        pid, _ = tools_mod.stage_bridge_write("apply_edit", arg, 2, "t")
        need = next(e["need"] for e in tools_mod.list_bridge_writes() if e["id"] == pid)
        with patch.object(tools_mod, "_PTC_BRIDGE_WRITE_EXECUTOR", None):
            ok = None
            for _ in range(need):
                ok, msg = tools_mod.confirm_bridge_write(pid)
        self.assertFalse(ok)                              # 执行器未注入 → 不谎报成功
        self.assertIn("未就绪", msg)
        entry = next(e for e in tools_mod.list_bridge_writes() if e["id"] == pid)
        self.assertEqual(entry["confirms"], 0)            # 阈值那次无效确认已回退
        self.assertEqual(tools_mod._peek_bridge_write("apply_edit", arg), pid)  # 仍占同参位

    def test_reject_removes_and_frees_dedup_slot(self):
        arg = "path: f\nnew_text: hi"
        pid, _ = tools_mod.stage_bridge_write("apply_edit", arg, 2, "t")
        self.assertTrue(tools_mod.reject_bridge_write(pid))
        self.assertFalse(any(e["id"] == pid for e in tools_mod.list_bridge_writes()))
        self.assertIsNone(tools_mod._peek_bridge_write("apply_edit", arg))  # 拒绝后同参可再登记
        pid2, replayed2 = tools_mod.stage_bridge_write("apply_edit", arg, 2, "t")
        self.assertFalse(replayed2)                       # 槽位已释放 → 新登记非 replayed


class ServiceLoopDeferredWiringTests(_WriteLoopHarness):
    """步 3：服务环端到端——写转真登记 pending pid、当场绝不落盘、同参去重、异参分登。"""

    def _srv(self, log):
        def server(name, arg):
            log.append(name)
            return (True, "X")
        return server

    def test_write_receipt_carries_pid_and_no_disk_effect(self):
        log = []
        rp = os.path.join(self.tmp, "wired.py").replace("\\", "/")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: hello")\n'
                'print("DEFERRED" if not ok else "RAN", str(r))\nprint("END")' % rp)
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertIn("DEFERRED", out)
        self.assertEqual(log, [])                         # 写绝不经 server/execute_tool
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("pending"), 1)
        items = tools_mod.list_bridge_writes()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["id"].startswith("W"))   # 台账里有真 pid
        self.assertIn("#" + items[0]["id"], out)          # 回执文本带回该 pid
        self.assertIn("\u5f85\u786e\u8ba4", out)           # "待确认"：诚实说明登记未执行
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "wired.py")))  # 关键：未落盘

    def test_same_arg_twice_dedups_to_one_pending(self):
        log = []
        arg = "path: dup.py\\nnew_text: same"
        code = ('a, ra = apply_edit("%s")\n'
                'b, rb = apply_edit("%s")\n'
                'print("A", a, "B", b)\nprint("END")' % (arg, arg))
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("pending"), 1)            # 同参只登记一次
        self.assertEqual(st.get("replayed"), 1)           # 第二条记为 replayed
        self.assertEqual(len(tools_mod.list_bridge_writes()), 1)
        self.assertEqual(st.get("planned"), 2)
        self.assertEqual(st.get("handled"), 2)
        self.assertTrue(st.get("reconciled"))

    def test_distinct_writes_register_separately(self):
        log = []
        code = ('a, ra = apply_edit("path: p1.py\\nnew_text: x")\n'
                'b, rb = apply_edit("path: p2.py\\nnew_text: y")\n'
                'print("END")')
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("pending"), 2)
        self.assertEqual(st.get("replayed"), 0)
        self.assertEqual(len(tools_mod.list_bridge_writes()), 2)


class _FRes(object):
    def __init__(self, ok, text=""):
        self.ok = ok
        self.text = text


class _FTurn(object):
    def __init__(self):
        self.steps = []
        self.evidences = []

    def tool_step(self, name, arg="", latency_ms=0, obs="", ok=True):
        self.steps.append((name, obs, ok))

    def add_tool_evidence(self, **kw):
        self.evidences.append(kw)


def _read_objs(tpath):
    import io as _io
    with _io.open(tpath, "r", encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


class TraceWriteEventTests(_WriteLoopHarness):
    """步 4：延后/硬拒/去重的写 → trace.jsonl 落 ptc_write_pending，**不**落 ptc_call。"""

    def _srv(self, log):
        def server(name, arg):
            log.append(name)
            return (True, "X")
        return server

    def test_deferred_write_emits_pending_not_call(self):
        rp = os.path.join(self.tmp, "t.py").replace("\\", "/")
        tpath = os.path.join(self.tmp, "trace.jsonl")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: hi")\nprint("END")' % rp)
        self._run(code, self._srv([]), write_enabled=True, trace_path=tpath)
        objs = _read_objs(tpath)
        pend = [o for o in objs if o.get("event") == "ptc_write_pending"]
        calls = [o for o in objs if o.get("event") == "ptc_call"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["tool"], "apply_edit")
        self.assertFalse(pend[0]["ok"])                  # 未执行 → ok False
        self.assertTrue(str(pend[0]["pid"]).startswith("W"))
        self.assertFalse(pend[0]["replayed"])
        self.assertGreaterEqual(pend[0]["tier"], 1)
        self.assertEqual([c["tool"] for c in calls], [])  # 写绝不进 ptc_call（不可被当作执行证据）

    def test_credential_write_pending_event_pid_none(self):
        cred = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa").replace("\\", "/")
        tpath = os.path.join(self.tmp, "trace.jsonl")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: boom")\nprint("END")' % cred)
        self._run(code, self._srv([]), write_enabled=True, trace_path=tpath)
        objs = _read_objs(tpath)
        pend = [o for o in objs if o.get("event") == "ptc_write_pending"]
        self.assertEqual(len(pend), 1)
        self.assertIsNone(pend[0]["pid"])                # 硬拒 → 无待确认 pid
        self.assertFalse(pend[0]["ok"])

    def test_replayed_write_marked_in_event(self):
        arg = "path: r.py\\nnew_text: same"
        tpath = os.path.join(self.tmp, "trace.jsonl")
        code = ('a, ra = apply_edit("%s")\nb, rb = apply_edit("%s")\nprint("END")' % (arg, arg))
        self._run(code, self._srv([]), write_enabled=True, trace_path=tpath)
        objs = _read_objs(tpath)
        pend = [o for o in objs if o.get("event") == "ptc_write_pending"]
        self.assertEqual(len(pend), 2)                   # 两条都响应
        self.assertFalse(pend[0]["replayed"])
        self.assertTrue(pend[1]["replayed"])             # 第二条同参去重
        self.assertEqual(pend[0]["pid"], pend[1]["pid"])  # 指向同一 pid
        self.assertEqual(len(tools_mod.list_bridge_writes()), 1)

    def test_mixed_read_call_and_write_pending(self):
        rp = os.path.join(self.tmp, "m.py").replace("\\", "/")
        tpath = os.path.join(self.tmp, "trace.jsonl")
        code = ('a, ra = calculate("1+1")\n'
                'b, rb = apply_edit("path: %s\\nnew_text: hi")\nprint("END")' % rp)
        self._run(code, self._srv([]), write_enabled=True, trace_path=tpath)
        objs = _read_objs(tpath)
        calls = [o for o in objs if o.get("event") == "ptc_call"]
        pend = [o for o in objs if o.get("event") == "ptc_write_pending"]
        self.assertEqual([c["tool"] for c in calls], ["calculate"])
        self.assertTrue(calls[0]["ok"])                  # 只读真跑了
        self.assertEqual([p["tool"] for p in pend], ["apply_edit"])
        self.assertFalse(pend[0]["ok"])                  # 写延后
        summ = [o for o in objs if o.get("event") == "ptc_run_summary"][0]
        self.assertTrue(summ["reconciled"])
        self.assertEqual(summ["planned"], 2)
        self.assertEqual(summ["handled"], 2)


class FailClosedAttributionTests(_WriteLoopHarness):
    """步 4：延后写绕过 server → 绝不进 turn 归因 / numbers_strong（fail-closed）。"""

    def _mk_server(self, turn):
        return agent_mod._ptc_build_server(
            tool_lookup=lambda n: {"calculate": {"func": lambda a: "42"}}.get(n),
            spec_lookup=lambda n: None,
            execute_tool_fn=lambda fn, arg, f, tool_name="", side_effect=None: _FRes(True, fn(arg)),
            is_failure=lambda obs: False,
            evidence_fn=lambda name, obs, ok: ({"numbers": [obs], "strong": True} if name == "calculate" else None),
            turn=turn,
            side_effect_default="pure",
        )

    def test_deferred_write_not_attributed_read_is(self):
        turn = _FTurn()
        srv = self._mk_server(turn)
        rp = os.path.join(self.tmp, "att.py").replace("\\", "/")
        code = ('a, ra = calculate("21*2")\n'
                'b, rb = apply_edit("path: %s\\nnew_text: hi")\nprint("END")' % rp)
        out = self._run(code, srv, write_enabled=True)
        self.assertIn("END", out)
        # 归因只落到那条只读；延后写完全不进 tool_step（=不进 numbers_strong 的来源）
        self.assertEqual([s[0] for s in turn.steps], ["calculate"])
        self.assertTrue(all(name != "apply_edit" for name, _o, _ok in turn.steps))
        self.assertEqual(len(turn.evidences), 1)
        self.assertEqual(turn.evidences[0]["numbers"], ["42"])   # 仅 calculate 的数字
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("executed"), 1)                  # 只读真跑
        self.assertEqual(st.get("pending"), 1)                   # 写延后、未归因


class MembershipTests(unittest.TestCase):
    """步 5：写白名单成员落定 + 递归护栏（NEVER 集永不暴露，即便误加也被扣）。"""

    def _on(self):
        return (patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True),
                patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", True))

    def test_exact_membership_when_write_on(self):
        p1, p2 = self._on()
        with p1, p2:
            self.assertEqual(tools_mod._ptc_bridge_write_tool_names(), _WRITE_SEED)
            self.assertEqual(tools_mod._ptc_bridge_tool_names(), _READ_ONLY | _WRITE_SEED)

    def test_recursion_never_tools_absent(self):
        never = {"python_exec", "run_command", "dev_mcp_call", "game_playtest",
                 "start_workflow", "self_verify", "dev_commit"}
        p1, p2 = self._on()
        with p1, p2:
            wn = tools_mod._ptc_bridge_write_tool_names()
            an = tools_mod._ptc_bridge_tool_names()
            for n in never:
                self.assertNotIn(n, wn)
                self.assertNotIn(n, an)

    def test_never_disjoint_from_both_lists(self):
        self.assertFalse(tools_mod._PTC_BRIDGE_WRITE_NEVER & _READ_ONLY)
        self.assertFalse(tools_mod._PTC_BRIDGE_WRITE_NEVER &
                         tools_mod._PTC_BRIDGE_WRITE_WHITELIST)

    def test_never_subtracted_even_if_added_to_allowlist(self):
        # 纵深防御：把递归工具**误加**进写白名单，暴露面仍必须扣掉它们。
        p1, p2 = self._on()
        with p1, p2, patch.object(
                tools_mod, "_PTC_BRIDGE_WRITE_WHITELIST",
                tools_mod._PTC_BRIDGE_WRITE_WHITELIST | {"python_exec", "run_command"}):
            self.assertNotIn("python_exec", tools_mod._ptc_bridge_tool_names())
            self.assertNotIn("run_command", tools_mod._ptc_bridge_write_tool_names())


class RecursionGuardTests(_WriteLoopHarness):
    """步 5：服务环端到端——递归/命令/引擎/git 写走白名单拒（非延后登记、非执行）。"""

    def _srv(self, log):
        def server(name, arg):
            log.append(name)
            return (True, "X")
        return server

    def test_python_exec_run_command_rejected_not_deferred(self):
        log = []
        code = ('a, ra = _bridge_call("python_exec", "import os;os.system(\'rm -rf /\')")\n'
                'b, rb = _bridge_call("run_command", "del x")\n'
                'print("A", a, "B", b)\nprint("END")')
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertEqual(log, [])                         # 绝不经 server/execute_tool
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 2)          # 白名单闸拒
        self.assertEqual(st.get("pending", 0), 0)        # 绝不进延后登记
        self.assertEqual(st.get("executed"), 0)

    def test_engine_and_git_writes_rejected(self):
        log = []
        code = ('a, ra = _bridge_call("dev_mcp_call", "x")\n'
                'b, rb = _bridge_call("dev_commit", "q")\n'
                'print("END")')
        out = self._run(code, self._srv(log), write_enabled=True)
        self.assertIn("END", out)
        self.assertEqual(log, [])
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 2)
        self.assertEqual(st.get("pending", 0), 0)


class _Boom(object):
    """哨兵：一旦被调用即抛错，用来证明写代码路径在关态下从不进入。"""

    def __call__(self, *a, **k):
        raise AssertionError("写 bridge 路径在写开关关闭时不应被调用")


class OffPathInertTests(unittest.TestCase):
    """步 6：写开关未设 → 逐字节回退到 B1——写分发/登记路径永不进入，只读行为不变。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tools_mod.clear_bridge_writes()

    def tearDown(self):
        tools_mod.clear_bridge_writes()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_tool_names_empty_when_flag_off(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", False):
            self.assertEqual(tools_mod._ptc_bridge_write_tool_names(), frozenset())
            self.assertEqual(tools_mod._ptc_bridge_tool_names(), _READ_ONLY)

    def test_jail_prefix_no_write_wrappers_when_flag_off(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", False):
            p = tools_mod._python_exec_jail_prefix(1)
        self.assertIn("def calculate(", p)              # 只读包装照旧
        self.assertNotIn("def apply_edit(", p)          # 写开关关 → 不暴露写包装（=B1）

    def test_write_dispatch_and_stage_never_called_when_off(self):
        boom = _Boom()
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                    patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", False), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER",
                                 lambda n, a: (True, "X")), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", None), \
                    patch.object(tools_mod, "_ptc_bridge_write_dispatch", boom), \
                    patch.object(tools_mod, "stage_bridge_write", boom):
                code = ('ok, r = _bridge_call("apply_edit", "path: x/y\\nnew_text: hi")\n'
                        'print("REJECTED" if not ok else "SERVED")\nprint("END")')
                out = tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)
        self.assertIn("END", out)                       # 未抛 AssertionError → 路径没进
        self.assertIn("REJECTED", out)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 1)         # 走白名单拒（=B1）
        self.assertEqual(st.get("pending", 0), 0)


class WriteAbnormalTerminationTests(unittest.TestCase):
    """步 6：延后写即便在超时/断连等异常收场下也**绝不当场落盘**（fail-closed）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tools_mod.clear_bridge_writes()

    def tearDown(self):
        tools_mod.clear_bridge_writes()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_capture(self, code, exec_timeout=None, close_stdin=False):
        captured = {}
        real_popen = tools_mod.subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            captured["proc"] = p
            if close_stdin:
                try:
                    p.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            return p

        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                    patch.object(tools_mod, "_PTC_BRIDGE_WRITE_ENABLED", True), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", lambda n, a: (True, "X")), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", None), \
                    patch.object(tools_mod.subprocess, "Popen", spy), \
                    patch.object(tools_mod, "_PYTHON_EXEC_TIMEOUT",
                                 exec_timeout if exec_timeout is not None
                                 else tools_mod._PYTHON_EXEC_TIMEOUT):
                out = tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)
        return out, captured.get("proc")

    def test_deferred_write_not_written_on_timeout(self):
        rp = os.path.join(self.tmp, "to.py").replace("\\", "/")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: hi")\n'
                'import time\n'
                'while True:\n'
                '    time.sleep(0.05)' % rp)
        out, proc = self._run_capture(code, exec_timeout=2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "to.py")),
                         "延后写在超时收场下也绝不该落盘")
        self.assertIn("\u8d85\u65f6", out)                     # "超时"：诚实报未跑完
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll(), "超时后子进程未被收尸 → 孤儿")

    def test_deferred_write_not_written_on_disconnect(self):
        rp = os.path.join(self.tmp, "dc.py").replace("\\", "/")
        code = ('ok, r = apply_edit("path: %s\\nnew_text: hi")\n'
                'print("AFTER", ok)' % rp)
        out, proc = self._run_capture(code, close_stdin=True)
        # 回执握手无法完成 → 子侧 EOF 抛 bridge disconnected，AFTER 不会打印、更不落盘。
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dc.py")),
                         "断连下延后写绝不该落盘")
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll(), "断连后子进程未被收尸 → 孤儿")


if __name__ == "__main__":
    unittest.main()
