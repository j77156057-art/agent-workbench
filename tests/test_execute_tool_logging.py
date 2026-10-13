"""`execute_tool` 异常路径的日志记录。

之前：
- 工具抛异常时只返回 `ToolResult(False, "工具执行失败（TypeError）。")`，
  traceback 完全被吞；
- `_idempotency_complete` 失败时 `pass`，长期 `idempotency_in_doubt`
  没有线索。

现在：两边都通过 `logging.warning` 记录 `exc_info=True` 完整 traceback
+ 关键参数（tool_name / key / db_path），对外仍只暴露类型名（不泄漏
消息可能包含的凭据/外部数据）。
"""
import logging
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime import tools
from agent_runtime.tools import SideEffect, execute_tool


class _Boom(Exception):
    pass


def _legacy_failure(_text):
    return False


class ExecuteToolLoggingTests(unittest.TestCase):
    def test_tool_exception_logs_traceback(self):
        def boom(_a):
            raise _Boom("内部实现细节不应被外发")

        with self.assertLogs("agent_runtime.tools", level="WARNING") as cm:
            result = execute_tool(boom, "x", _legacy_failure,
                                   tool_name="my_tool",
                                   side_effect=SideEffect.PURE)
        self.assertFalse(result.ok)
        self.assertIn("工具执行失败", result.text)
        self.assertIn("Boom", result.text)
        # 日志含工具名 + 异常类型 + traceback
        joined = "\n".join(cm.output)
        self.assertIn("my_tool", joined)
        self.assertIn("Boom", joined)
        self.assertIn("Traceback", joined,
                       "未记录完整 traceback：%s" % joined[:400])

    def test_idempotency_complete_failure_logs(self):
        """_idempotency_complete 抛异常 → 写 warning 日志，result.ok 不变。"""
        fake_key = "key-123"
        fake_db = "D:/tmp/db.sqlite"

        with mock.patch.object(tools, "_idempotency_claim",
                                return_value=(fake_key, fake_db, None)), \
             mock.patch.object(tools, "_idempotency_complete",
                                side_effect=_Boom("db down")), \
             self.assertLogs("agent_runtime.tools", level="WARNING") as cm:
            result = execute_tool(lambda a: "ok-output", "x", _legacy_failure,
                                   tool_name="my_tool",
                                   side_effect=SideEffect.MUTATING)
        self.assertTrue(result.ok, "MCP 已完成，不应被 complete 失败覆盖")
        self.assertEqual(result.text, "ok-output")
        joined = "\n".join(cm.output)
        self.assertIn(fake_key, joined)
        self.assertIn(fake_db, joined)
        self.assertIn("my_tool", joined)
        self.assertIn("Traceback", joined)

    def test_idempotency_complete_success_no_warning(self):
        """complete 成功时不记 warning。"""
        with mock.patch.object(tools, "_idempotency_claim",
                                return_value=("k", "D:/tmp/db.sqlite", None)), \
             mock.patch.object(tools, "_idempotency_complete"), \
             self.assertNoLogs("agent_runtime.tools", level="WARNING"):
            execute_tool(lambda a: "ok", "x", _legacy_failure,
                          tool_name="my_tool",
                          side_effect=SideEffect.MUTATING)

    def test_idempotency_claim_failure_blocks_call_and_logs(self):
        """claim 失败 → 拒绝执行 + warning 日志。"""
        with mock.patch.object(tools, "_idempotency_claim",
                                side_effect=_Boom("db disk full")), \
             self.assertLogs("agent_runtime.tools", level="WARNING") as cm:
            result = execute_tool(lambda a: "should-not-run", "x", _legacy_failure,
                                   tool_name="my_tool",
                                   side_effect=SideEffect.MUTATING)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "idempotency_unavailable")
        joined = "\n".join(cm.output)
        self.assertIn("幂等 claim 失败", joined)
        self.assertIn("my_tool", joined)
        self.assertIn("Traceback", joined)


if __name__ == "__main__":
    unittest.main()
