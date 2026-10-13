"""agent_runtime/agent_log 结构化事件流测试。

验证 6 项契约：
1. event() 写入 JSONL，read() 反向 tail 能读到
2. 敏感字段脱敏（api_key / password / secret 前缀字段值替换为 [REDACTED]）
3. root 为空时 event()/read() 不抛异常，返回 False / []
4. 文件不存在时 read() 返回 []
5. 截断：写满 MAX_BYTES 后老内容被丢弃（验证日志不会无限增长）
6. JSON 损坏的单行不导致 read() 整体失败
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import agent_log  # noqa: E402


class AgentLogTests(unittest.TestCase):

    def setUp(self):
        """每个测试用临时根目录 + 临时 STATE_ROOT 隔离。"""
        self.tmp = tempfile.mkdtemp(prefix="agent-log-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 把 STATE_ROOT 重定向到临时目录，避免污染实际状态目录
        self._old_state_root = os.environ.get("DOCMIND_STATE_ROOT", "")
        # config.STATE_ROOT 在 import 时已固化，必须用 monkey-patch
        self._old_module_state_root = config.STATE_ROOT
        config.STATE_ROOT = self.tmp
        self.addCleanup(setattr, config, "STATE_ROOT", self._old_module_state_root)

    def test_write_then_read_round_trip(self):
        """基本契约：写 3 条 → 反向 tail 读 3 条 → 第一条是最后写的。"""
        ok1 = agent_log.event(self.tmp, "agent_init", session_id="s1", model="x")
        ok2 = agent_log.event(self.tmp, "tool_call", name="dev_apply_edits", args={"k": "v"})
        ok3 = agent_log.event(self.tmp, "tool_call_failed", name="dev_apply_edits", error="IOError")
        self.assertTrue(ok1 and ok2 and ok3)

        events = agent_log.read(self.tmp)
        self.assertEqual(len(events), 3)
        # 反向 tail：最近的在最前
        self.assertEqual(events[0]["kind"], "tool_call_failed")
        self.assertEqual(events[1]["kind"], "tool_call")
        self.assertEqual(events[2]["kind"], "agent_init")
        # 字段保留
        self.assertEqual(events[0]["fields"]["error"], "IOError")
        self.assertEqual(events[1]["fields"]["args"], {"k": "v"})

    def test_sensitive_fields_redacted(self):
        """敏感前缀字段值替换为 [REDACTED]；非敏感字段保持原样。"""
        agent_log.event(
            self.tmp, "tool_call",
            api_key="sk-1234567890abcdef",
            access_token="ghp_abcdefghijklmnop",
            password="hunter2",
            public_field="公开信息",
            nested={"private_key": "BEGIN RSA...", "ok": "stays"},
        )

        events = agent_log.read(self.tmp)
        self.assertEqual(len(events), 1)
        fields = events[0]["fields"]
        self.assertEqual(fields["api_key"], "[REDACTED]")
        self.assertEqual(fields["access_token"], "[REDACTED]")
        self.assertEqual(fields["password"], "[REDACTED]")
        self.assertEqual(fields["public_field"], "公开信息")  # 非敏感，原样
        self.assertEqual(fields["nested"]["private_key"], "[REDACTED]")  # 嵌套也脱敏
        self.assertEqual(fields["nested"]["ok"], "stays")

    def test_empty_root_returns_safely(self):
        """root 为空时 event() / read() / tail_summary() 全部静默返回。"""
        # event 不抛
        self.assertFalse(agent_log.event("", "test_kind", x=1))
        # read 不抛，返回 []
        self.assertEqual(agent_log.read(""), [])
        # tail_summary 不抛
        self.assertEqual(agent_log.tail_summary(""), [])

    def test_read_missing_file_returns_empty(self):
        """文件不存在时 read() 返回 []（不是抛 FileNotFoundError）。"""
        # root 合法但日志文件还没创建过
        events = agent_log.read(self.tmp)
        self.assertEqual(events, [])

    def test_kind_filter(self):
        """kind 过滤：只返回匹配 kind 的事件。"""
        agent_log.event(self.tmp, "agent_init", x=1)
        agent_log.event(self.tmp, "tool_call", x=2)
        agent_log.event(self.tmp, "tool_call", x=3)

        only_tool = agent_log.read(self.tmp, kind="tool_call")
        self.assertEqual(len(only_tool), 2)
        self.assertTrue(all(e["kind"] == "tool_call" for e in only_tool))

    def test_corrupted_line_does_not_kill_read(self):
        """JSON 损坏的单行不导致 read() 整体失败——损坏行被跳过，其他事件正常返回。"""
        path = agent_log._log_path(self.tmp)
        self.assertIsNotNone(path)
        # 写 2 条合法 + 1 条损坏
        agent_log.event(self.tmp, "good_a", x=1)
        agent_log.event(self.tmp, "good_b", x=2)
        # 手工注入坏行
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ this is not valid json\n")
        agent_log.event(self.tmp, "good_c", x=3)

        events = agent_log.read(self.tmp)
        # 损坏行被静默跳过；3 条合法事件都应返回
        self.assertEqual(len(events), 3)
        kinds = [e["kind"] for e in events]
        self.assertIn("good_a", kinds)
        self.assertIn("good_b", kinds)
        self.assertIn("good_c", kinds)

    def test_truncation_keeps_recent_events(self):
        """超过 MAX_BYTES 时截断，保留最近的事件（不保留全部历史）。"""
        # 临时把阈值改小便于测试
        import agent_runtime.agent_log as _mod
        original = _mod.MAX_BYTES
        _mod.MAX_BYTES = 1024  # 1 KiB
        try:
            for i in range(200):
                agent_log.event(self.tmp, "evt", i=i, payload="x" * 50)
            # 文件大小应远小于 200 * (单条事件字节) ≈ 200*80=16K
            path = agent_log._log_path(self.tmp)
            self.assertIsNotNone(path)
            size = os.path.getsize(path)
            self.assertLess(size, 2048, "截断机制未生效：文件 %d 字节" % size)
        finally:
            _mod.MAX_BYTES = original

    def test_tail_summary_compact_view(self):
        """tail_summary 返回紧凑字符串列表，给 UI 调试面板用。"""
        agent_log.event(self.tmp, "agent_init", session_id="s1")
        agent_log.event(self.tmp, "tool_call", name="dev_apply_edits")
        summary = agent_log.tail_summary(self.tmp, limit=5)
        self.assertEqual(len(summary), 2)
        # 紧凑格式：[<ts>] <kind> <field1>=<val1>, ...
        self.assertIn("agent_init", summary[1])  # 反向：旧的在下
        self.assertIn("tool_call", summary[0])  # 新的在上
        self.assertIn("session_id=s1", summary[1])
        self.assertIn("name=dev_apply_edits", summary[0])


if __name__ == "__main__":
    unittest.main()
