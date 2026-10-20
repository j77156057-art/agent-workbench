# -*- coding: utf-8 -*-
"""PTC 路径 B —— 步 6「trace 侧信道 + fail-closed 归因」回归（第51批）。

三块：
  A. 纯函数 _ptc_parse_plans / _ptc_reconcile：子侧 planned 台账解析 + planned==ran 硬不变式。
  B. 服务环端到端：2 白名单 + 1 越权 → planned==handled==3、executed=2、rejected=1、reconciled True；
     _PTC_TRACE_PATH 落盘 jsonl（summary + 每调用一条 ptc_call），best-effort 全量留痕。
  C. agent 侧 _ptc_build_server：纯依赖注入归因流 —— 未知/无实现/异常一律 fail-closed，
     成功走完整 execute_tool 并把 tool_step + add_tool_evidence 归因进 turn；provenance 不丢。
     _ptc_server_scope：开模式 python_exec 才装 tools._PTC_BRIDGE_SERVER，退出（含异常）必恢复；
     关模式 / 非 python_exec 严格空操作（连全局都不碰 → 默认路径逐字节不变）。
"""

import os
import sys
import json
import io
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
import agent as agent_mod  # noqa: E402
from config import set_runtime  # noqa: E402


# ---------------------------------------------------------------- A. 纯函数
class ReconcilePureTests(unittest.TestCase):
    def test_parse_plans_extracts_dicts_ignores_noise(self):
        lines = [
            "some unrelated stderr line\n",
            "__BRIDGE_PLAN__" + json.dumps({"seq": 1, "tool": "calculate", "arg_hash": "a"}) + "\n",
            "Traceback ... \n",
            "__BRIDGE_PLAN__" + json.dumps({"seq": 2, "tool": "grep", "arg_hash": "b"}) + "\n",
            "__BRIDGE_PLAN__not-valid-json\n",           # 坏 JSON 跳过
        ]
        plans = tools_mod._ptc_parse_plans(lines)
        self.assertEqual([p["seq"] for p in plans], [1, 2])
        self.assertEqual([p["tool"] for p in plans], ["calculate", "grep"])

    def test_parse_plans_handles_none(self):
        self.assertEqual(tools_mod._ptc_parse_plans(None), [])

    def test_reconcile_equal_true(self):
        lines = [
            "__BRIDGE_PLAN__" + json.dumps({"seq": 1, "tool": "calculate"}) + "\n",
            "__BRIDGE_PLAN__" + json.dumps({"seq": 2, "tool": "grep"}) + "\n",
        ]
        r = tools_mod._ptc_reconcile(lines, handled=2)
        self.assertEqual(r["planned"], 2)
        self.assertEqual(r["handled"], 2)
        self.assertTrue(r["reconciled"])

    def test_reconcile_unequal_false(self):
        lines = [
            "__BRIDGE_PLAN__" + json.dumps({"seq": 1, "tool": "calculate"}) + "\n",
            "__BRIDGE_PLAN__" + json.dumps({"seq": 2, "tool": "grep"}) + "\n",
        ]
        r = tools_mod._ptc_reconcile(lines, handled=1)  # 父侧少响应一条
        self.assertEqual(r["planned"], 2)
        self.assertFalse(r["reconciled"])


# ---------------------------------------------------------------- B. 端到端 trace
class _LoopHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, code, server, trace_path=None, enabled=True):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", enabled), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", server), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", trace_path):
                return tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)


class ServiceLoopReconcileTests(_LoopHarness):
    def _server(self, log):
        def server(name, arg):
            log.append((name, arg))
            return (True, "ok:%s" % arg)
        return server

    def test_planned_equals_handled_reconciled(self):
        log = []
        code = ('a, ra = calculate("1+1")\n'
                'b, rb = grep("needle")\n'
                'c, rc = _bridge_call("apply_edit", "x")\n'
                'print("A", a, "B", b, "C", c)\n'
                'print("END")')
        out = self._run(code, self._server(log))
        self.assertIn("END", out)
        self.assertEqual(log, [("calculate", "1+1"), ("grep", "needle")])  # 仅白名单进 server
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("planned"), 3)   # 三条 bridge 调用全部在子侧落计划
        self.assertEqual(st.get("handled"), 3)   # 父侧对三条全部响应（2 执行 + 1 拒绝）
        self.assertTrue(st.get("reconciled"))
        self.assertEqual(st.get("executed"), 2)
        self.assertEqual(st.get("rejected"), 1)
        # 全对账 → 不该出现 planned≠handled 的诚实告警
        self.assertNotIn("planned\u2260handled", out)

    def test_trace_jsonl_written_when_path_set(self):
        log = []
        tpath = os.path.join(self.tmp, "trace.jsonl")
        code = ('a, ra = calculate("2+3")\n'
                'b, rb = _bridge_call("apply_edit", "y")\n'
                'print("DONE")')
        self._run(code, self._server(log), trace_path=tpath)
        self.assertTrue(os.path.exists(tpath), "trace.jsonl 应落盘")
        with io.open(tpath, "r", encoding="utf-8") as f:
            objs = [json.loads(ln) for ln in f if ln.strip()]
        summaries = [o for o in objs if o.get("event") == "ptc_run_summary"]
        calls = [o for o in objs if o.get("event") == "ptc_call"]
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        self.assertEqual(s["planned"], 2)
        self.assertEqual(s["handled"], 2)
        self.assertTrue(s["reconciled"])
        self.assertEqual(len(calls), 2)          # 每条响应一行 ptc_call
        tools_in_calls = sorted(c["tool"] for c in calls)
        self.assertEqual(tools_in_calls, ["apply_edit", "calculate"])

    def test_no_trace_file_when_path_none(self):
        log = []
        code = 'a, ra = calculate("1")\nprint("END")'
        self._run(code, self._server(log), trace_path=None)
        # 未设路径 → 不落文件，仅 _PTC_LAST_STATS 供读取
        st = tools_mod._PTC_LAST_STATS
        self.assertTrue(st.get("reconciled"))


# ---------------------------------------------------------------- C. build_server 归因流
class _Res(object):
    def __init__(self, ok, text=""):
        self.ok = ok
        self.text = text


class _Spec(object):
    def __init__(self, se):
        self.side_effect = se


class _FakeTurn(object):
    def __init__(self):
        self.steps = []
        self.evidences = []

    def tool_step(self, name, arg="", latency_ms=0, obs="", ok=True):
        self.steps.append((name, arg, obs, ok))

    def add_tool_evidence(self, **kw):
        self.evidences.append(kw)


class BuildServerTests(unittest.TestCase):
    def _mk(self, tools=None, execute=None, evidence=None, turn=None,
            is_failure=lambda obs: False, spec=None):
        tools = tools or {}
        return agent_mod._ptc_build_server(
            tool_lookup=lambda n: tools.get(n),
            spec_lookup=lambda n: spec,
            execute_tool_fn=execute or (lambda fn, arg, f, tool_name="", side_effect=None: _Res(True, "R")),
            is_failure=is_failure,
            evidence_fn=evidence or (lambda name, obs, ok: None),
            turn=turn,
            side_effect_default="default_se",
        )

    def test_unknown_tool_fail_closed_no_attribution(self):
        turn = _FakeTurn()
        srv = self._mk(tools={}, turn=turn)
        ok, msg = srv("nope", "x")
        self.assertFalse(ok)
        self.assertIn("\u672a\u77e5\u5de5\u5177", msg)  # 未知工具
        self.assertEqual(turn.steps, [])               # 未执行 → 不归因

    def test_entry_without_func_fail_closed(self):
        srv = self._mk(tools={"calculate": {"description": "no func here"}})
        ok, msg = srv("calculate", "x")
        self.assertFalse(ok)
        self.assertIn("\u65e0\u53ef\u8c03\u7528\u5b9e\u73b0", msg)  # 无可调用实现

    def test_happy_path_attributes_turn(self):
        seen = {}
        turn = _FakeTurn()

        def execute(fn, arg, f, tool_name="", side_effect=None):
            seen["tool_name"] = tool_name
            seen["side_effect"] = side_effect
            seen["fn"] = fn
            return _Res(True, "42")

        fncalled = {"hit": False}

        def real_fn(arg):
            fncalled["hit"] = True
            return "42"

        srv = self._mk(
            tools={"calculate": {"func": real_fn}},
            execute=execute,
            evidence=lambda name, obs, ok: {"numbers": [42], "strong": True},
            turn=turn,
            spec=_Spec("pure"),
        )
        ok, obs = srv("calculate", "6*7")
        self.assertTrue(ok)
        self.assertEqual(obs, "42")
        self.assertEqual(seen["tool_name"], "calculate")
        self.assertEqual(seen["side_effect"], "pure")     # spec 优先于 default
        self.assertIs(seen["fn"], real_fn)
        self.assertEqual(len(turn.steps), 1)
        self.assertEqual(turn.steps[0][0], "calculate")
        self.assertTrue(turn.steps[0][3])                 # ok=True
        self.assertEqual(turn.evidences, [{"numbers": [42], "strong": True}])

    def test_default_side_effect_when_no_spec(self):
        seen = {}

        def execute(fn, arg, f, tool_name="", side_effect=None):
            seen["side_effect"] = side_effect
            return _Res(True, "ok")

        srv = self._mk(
            tools={"grep": {"func": lambda a: "ok"}},
            execute=execute,
            spec=None,                                        # _tool_spec 返回 None
        )
        ok, obs = srv("grep", "needle")
        self.assertTrue(ok)
        self.assertEqual(seen["side_effect"], "default_se")   # 落 side_effect_default

    def test_is_failure_marks_not_ok_still_attributed(self):
        turn = _FakeTurn()
        srv = self._mk(
            tools={"read_file": {"func": lambda a: "BAD"}},
            execute=lambda fn, arg, f, tool_name="", side_effect=None: _Res(True, "BAD"),
            is_failure=lambda obs: obs == "BAD",
            turn=turn,
        )
        ok, obs = srv("read_file", "x")
        self.assertFalse(ok)                                  # 治理判失败
        self.assertEqual(len(turn.steps), 1)
        self.assertFalse(turn.steps[0][3])                    # 归因带 ok=False

    def test_execute_exception_fail_closed_no_attribution(self):
        turn = _FakeTurn()

        def boom(fn, arg, f, tool_name="", side_effect=None):
            raise RuntimeError("underlying")

        srv = self._mk(tools={"memory_search": {"func": lambda a: "x"}},
                       execute=boom, turn=turn)
        ok, msg = srv("memory_search", "kw")
        self.assertFalse(ok)
        self.assertIn("\u670d\u52a1\u5f02\u5e38", msg)          # 服务异常
        self.assertEqual(turn.steps, [])                       # 异常未确认 → 不归因


# ---------------------------------------------------------------- C. server_scope 接线
class ServerScopeTests(unittest.TestCase):
    def _agent(self):
        return agent_mod.Agent(llm=None, tool_registry={})

    def test_off_mode_noop_does_not_touch_global(self):
        a = self._agent()
        sentinel = object()
        with patch.object(agent_mod, "_PTC_BRIDGE_ENABLED", False):
            with patch.object(tools_mod, "_PTC_BRIDGE_SERVER", sentinel):
                with a._ptc_server_scope("python_exec", None):
                    self.assertIs(tools_mod._PTC_BRIDGE_SERVER, sentinel)  # 根本没被替换
                self.assertIs(tools_mod._PTC_BRIDGE_SERVER, sentinel)

    def test_non_python_exec_noop(self):
        a = self._agent()
        sentinel = object()
        with patch.object(agent_mod, "_PTC_BRIDGE_ENABLED", True):
            with patch.object(tools_mod, "_PTC_BRIDGE_SERVER", sentinel):
                with a._ptc_server_scope("read_file", None):               # 非 python_exec
                    self.assertIs(tools_mod._PTC_BRIDGE_SERVER, sentinel)

    def test_on_python_exec_installs_then_restores(self):
        a = self._agent()
        sentinel = object()
        with patch.object(agent_mod, "_PTC_BRIDGE_ENABLED", True):
            with patch.object(tools_mod, "_PTC_BRIDGE_SERVER", sentinel):
                with a._ptc_server_scope("python_exec", None):
                    self.assertIsNot(tools_mod._PTC_BRIDGE_SERVER, sentinel)
                    self.assertTrue(callable(tools_mod._PTC_BRIDGE_SERVER))
                self.assertIs(tools_mod._PTC_BRIDGE_SERVER, sentinel)      # 恢复旧值

    def test_restores_on_exception(self):
        a = self._agent()
        sentinel = object()
        with patch.object(agent_mod, "_PTC_BRIDGE_ENABLED", True):
            with patch.object(tools_mod, "_PTC_BRIDGE_SERVER", sentinel):
                with self.assertRaises(RuntimeError):
                    with a._ptc_server_scope("python_exec", None):
                        self.assertTrue(callable(tools_mod._PTC_BRIDGE_SERVER))
                        raise RuntimeError("body boom")
                self.assertIs(tools_mod._PTC_BRIDGE_SERVER, sentinel)      # 异常也必恢复


if __name__ == "__main__":
    unittest.main()
