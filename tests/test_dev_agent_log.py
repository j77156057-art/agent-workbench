"""dev_agent_log 工具测试：让 agent 自身能读自己结构化日志的契约验证。

设计目标（与 dev_plan / dev_lanes 同族工具一致）：
- 给 agent 一个**只读**入口读 `agent-log-v1.jsonl` —— agent 不应能写自己的日志
  （写由 `_agent_log_event` 在关键失败点自动发生，agent 改自己日志容易 noise flood）
- kind 过滤 + limit 上限 + root 为空安全
- 失败 / root 为空一律返回可读字符串（不抛异常），让 LLM 容易解析
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import agent_log  # noqa: E402
import tools as _tools_mod  # noqa: E402


class DevAgentLogToolTests(unittest.TestCase):

    def setUp(self):
        """每个测试用临时根目录 + 重定向 STATE_ROOT 隔离。"""
        self.tmp = tempfile.mkdtemp(prefix="dev-agent-log-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 把 STATE_ROOT 重定向到 tmp
        self._old_module_state_root = config.STATE_ROOT
        config.STATE_ROOT = self.tmp
        self.addCleanup(setattr, config, "STATE_ROOT", self._old_module_state_root)
        # patch tools._get_code_root 返回 tmp
        self._orig_get_code_root = _tools_mod._get_code_root
        _tools_mod._get_code_root = lambda: self.tmp
        self.addCleanup(setattr, _tools_mod, "_get_code_root", self._orig_get_code_root)

    def _write_events(self, *kinds):
        for k in kinds:
            agent_log.event(self.tmp, k, foo="bar", i=len(kinds))

    def test_read_returns_recent_events(self):
        """read action：返回最近的事件，kind 字段 + fields 字段都对。"""
        self._write_events("agent_init", "tool_call", "tool_call_failed")

        result = _tools_mod.dev_agent_log("action: read")
        # 解析返回（dev_agent_log 应返回 JSON 字符串）
        data = json.loads(result)
        self.assertIn("events", data)
        self.assertIn("count", data)
        self.assertEqual(data["count"], 3)
        # 反向 tail：最近的在最前
        kinds = [e["kind"] for e in data["events"]]
        self.assertEqual(kinds, ["tool_call_failed", "tool_call", "agent_init"])
        # 字段保留
        first = data["events"][0]
        self.assertIn("ts", first)
        self.assertEqual(first["fields"]["foo"], "bar")

    def test_kind_filter_works(self):
        """kind 过滤：只返回匹配 kind 的事件。"""
        self._write_events("agent_init", "tool_call", "tool_call", "tool_call_failed")

        result = _tools_mod.dev_agent_log("action: read\nkind: tool_call")
        data = json.loads(result)
        kinds = [e["kind"] for e in data["events"]]
        self.assertEqual(kinds, ["tool_call", "tool_call"])
        self.assertEqual(data["count"], 2)

    def test_limit_clamped_to_200(self):
        """limit 上限 200：传 limit=9999 实际只返回 200 条。"""
        for i in range(250):
            agent_log.event(self.tmp, "evt", i=i)

        result = _tools_mod.dev_agent_log("action: read\nlimit: 9999")
        data = json.loads(result)
        # 实际被 clamp 到 200
        self.assertLessEqual(len(data["events"]), 200)
        # truncated 字段告知
        self.assertTrue(data.get("truncated", False))

    def test_no_root_returns_safe_error(self):
        """root 为空时：返回可读错误，不抛异常。"""
        # 临时把 _get_code_root 改成返回空
        _tools_mod._get_code_root = lambda: ""
        result = _tools_mod.dev_agent_log("action: read")
        # 错误应包含"配置"或"代码库"等可读提示
        self.assertIn("代码库", result)

    def test_summary_action_compact(self):
        """summary action：返回紧凑字符串列表（不是 JSON events）——给 UI 调试面板用。"""
        self._write_events("agent_init", "tool_call", "tool_call_failed")

        result = _tools_mod.dev_agent_log("action: summary")
        # summary 返回 list[str] JSON
        data = json.loads(result)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 3)
        # 紧凑格式：[<ts>] <kind> <field>=<val>...
        first = data[0]
        self.assertIn("tool_call_failed", first)

    def test_unknown_action_falls_back_to_read(self):
        """未知 action：与 dev_plan 行为一致——fallback 到 read，不抛。"""
        self._write_events("agent_init")

        result = _tools_mod.dev_agent_log("action: nonexistent_xyz")
        # 不抛，返回 read 结果
        data = json.loads(result)
        self.assertIn("events", data)
        # 应提示有 fallback
        self.assertIn("fallback", result.lower() or data.get("note", "").lower() or "events" in data)

    def test_read_does_not_crash_on_corrupted_jsonl(self):
        """JSONL 单行损坏：read 不崩——agent_log 内部 try/except 已覆盖，工具透传。"""
        self._write_events("good_a", "good_b")
        # 注入一行损坏 JSON
        path = agent_log._log_path(self.tmp)
        self.assertIsNotNone(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ this is broken\n")
        self._write_events("good_c")

        result = _tools_mod.dev_agent_log("action: read")
        data = json.loads(result)
        # 3 条合法事件都应返回
        self.assertEqual(data["count"], 3)


if __name__ == "__main__":
    unittest.main()
