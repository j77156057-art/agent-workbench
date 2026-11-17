# -*- coding: utf-8 -*-
"""原生 function-calling 通道：schema 导出、arguments 归一、tool_calls 派发（离线）。"""
import json
import os
import tempfile
import unittest

import agent as agent_mod
import agent_trace
import sessions
from llm import args_to_input, normalize_tool_calls
import tools  # TOOLS 动态取：reload(tools) 会换掉这张 dict（见 test_parallel 注释）
from tools import tool_schemas


class _NativeLLM:
    """假 LLM：按脚本返回原生 tool_calls 或纯文本。provider 设为支持原生 FC 的云 provider。"""
    provider = "deepseek"
    model = "fake-native"

    def __init__(self, steps):
        self.steps = steps
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, **kw):
        spec = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 2}
        self.last_tool_calls = []
        content = ""
        if spec[0] == "tools":
            calls = spec[1]
            self.last_tool_calls = [
                {"id": f"c{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(calls)
            ]
        else:
            content = spec[1]
        if stream:
            def g():
                for ch in content:
                    yield ch
            return g()
        return content

    def count_tokens(self, text):
        return max(1, len(text) // 3)


class SchemaTests(unittest.TestCase):
    def test_schema_shape_and_subset(self):
        schemas = tool_schemas(["search_code", "read_file"])
        self.assertEqual(len(schemas), 2)
        for s in schemas:
            self.assertEqual(s["type"], "function")
            self.assertIn("input", s["function"]["parameters"]["properties"])
            self.assertTrue(s["function"]["description"])

    def test_schema_covers_registry(self):
        self.assertEqual(len(tool_schemas()), len(tools.TOOLS))

    def test_args_to_input_extracts_single_input(self):
        self.assertEqual(args_to_input('{"input": "path: a.py"}'), "path: a.py")

    def test_args_to_input_multi_param_to_multiline(self):
        out = args_to_input('{"path": "a.py", "old_text": "x"}')
        self.assertIn("path: a.py", out)
        self.assertIn("old_text: x", out)

    def test_args_to_input_passthrough_on_bad_json(self):
        self.assertEqual(args_to_input("not json"), "not json")

    def test_normalize_tool_calls_dict_and_object(self):
        class _Fn:
            name = "read_file"
            arguments = '{"input": "x"}'

        class _Call:
            id = "c1"
            function = _Fn()

        out = normalize_tool_calls([_Call()])
        self.assertEqual(out[0]["name"], "read_file")
        self.assertEqual(out[0]["arguments"], '{"input": "x"}')


class NativeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_native_")
        self._old_trace = agent_trace.TRACE_FILE
        self._old_dir = sessions.SESSIONS_DIR
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "t.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "s")

    def tearDown(self):
        agent_trace.TRACE_FILE = self._old_trace
        sessions.SESSIONS_DIR = self._old_dir

    def test_native_tool_call_is_dispatched(self):
        llm = _NativeLLM([
            ("tools", [("calculate", {"input": "1+1"})]),
            ("text", "Final Answer: 结果是 2"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("算一下 1+1", stream=True))
        kinds = [e["type"] for e in events]
        self.assertIn("action", kinds)
        action = next(e for e in events if e["type"] == "action")
        self.assertTrue(action["text"].startswith("calculate"))
        obs = next(e for e in events if e["type"] == "observation")
        self.assertIn("2", obs["text"])
        self.assertEqual(events[-1]["type"], "final")
        self.assertIn("2", events[-1]["text"])
        # 账本记到一次工具步
        rec = agent_trace.recent(1)[0]
        self.assertEqual(rec["n_steps"], 1)

    def test_multiple_tool_calls_execute_sequentially(self):
        # 注意：不能用 calculate —— 它成功后会回填观察、要求模型再走一轮解读，
        # 这里用 search_code 验证一次返回多个 tool_call 会被逐条执行。
        llm = _NativeLLM([
            ("tools", [("search_code", {"input": "query: alpha"}),
                       ("search_code", {"input": "query: beta"})]),
            ("text", "Final Answer: 都查完了"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("查两下", stream=True))
        actions = [e for e in events if e["type"] == "action"]
        self.assertEqual(len(actions), 2, [e.get("text") for e in actions])
        self.assertEqual(agent_trace.recent(1)[0]["n_steps"], 2)

    def test_react_mode_ignores_tool_calls(self):
        llm = _NativeLLM([
            ("tools", [("calculate", {"input": "1+1"})]),
            ("text", "Final Answer: 兜底答案"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="react")
        self.assertFalse(a._native_enabled())
        events = list(a.run("q", stream=True))
        self.assertNotIn("action", [e["type"] for e in events])

    def test_auto_mode_follows_provider(self):
        self.assertTrue(agent_mod.Agent(llm=_NativeLLM([]), tool_mode="auto")._native_enabled())
        llm = _NativeLLM([])
        llm.provider = "mock"
        self.assertFalse(agent_mod.Agent(llm=llm, tool_mode="auto")._native_enabled())


class DedupDocsTests(unittest.TestCase):
    """回归：向量检索非空结果去重路径不得 NameError（review B-1）。"""

    def test_non_empty_results_deduped(self):
        import tools
        docs = ["同一片段", "同一片段", "另一片段"]
        metas = [{"source": "a.py"}, {"source": "a.py"}, {"source": "b.py"}]
        out_d, out_m = tools._dedup_docs(docs, metas)
        self.assertEqual(out_d, ["同一片段", "另一片段"])
        self.assertEqual([m["source"] for m in out_m], ["a.py", "b.py"])

    def test_empty_is_safe(self):
        import tools
        self.assertEqual(tools._dedup_docs([], []), ([], []))


if __name__ == "__main__":
    unittest.main()
