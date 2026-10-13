"""`dev_mcp_call` 在 before/after hook 异常时不再静默 fail-open。

之前：
- `import hooks` 或 `before_mcp` 抛异常 → 把 `_workflow_hooks` 置 None，
  继续执行实际 MCP 调用，等于绕过审计/审批边界；
- `after_mcp` 抛异常 → `pass`，调用已完成，影响小但缺可观测。

之后：
- before hook 异常 → 拒绝执行，返回明确错误；
- after hook 异常 → 不影响 MCP 结果，但记 warning 日志。
"""
import logging
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools


class _Boom(Exception):
    pass


class DevMcpCallHooksFailClosedTests(unittest.TestCase):
    def test_before_mcp_hook_raises_blocks_call(self):
        """before_mcp 抛异常 → 拒绝执行，不调 _dev_mcp_call_impl。"""
        called_impl = mock.Mock(return_value="OK")
        with mock.patch.object(tools, "_dev_mcp_call_impl", called_impl), \
             mock.patch("hooks.run_workflow", side_effect=_Boom("hooks broken")):
            out = tools.dev_mcp_call("key: x")
        called_impl.assert_not_called()
        self.assertIn("钩子异常", out)
        self.assertIn("拒绝执行", out)

    def test_before_mcp_normal_path_still_runs(self):
        """before_mcp 正常 → 实际 MCP 调用继续。"""
        called_impl = mock.Mock(return_value="OK")
        with mock.patch.object(tools, "_dev_mcp_call_impl", called_impl), \
             mock.patch("hooks.run_workflow", return_value={"blocked": False}):
            out = tools.dev_mcp_call("key: x")
        called_impl.assert_called_once()
        self.assertEqual(out, "OK")

    def test_before_mcp_blocked_returns_without_calling_impl(self):
        """before_mcp blocked → 返回拦截说明，不调 _dev_mcp_call_impl。"""
        called_impl = mock.Mock()
        with mock.patch.object(tools, "_dev_mcp_call_impl", called_impl), \
             mock.patch("hooks.run_workflow",
                        return_value={"blocked": True, "reason": "需审核"}):
            out = tools.dev_mcp_call("key: x")
        called_impl.assert_not_called()
        self.assertIn("已被钩子拦截", out)
        self.assertIn("需审核", out)

    def test_after_mcp_hook_raises_does_not_break_result_but_logs(self):
        """after_mcp 抛异常 → 不影响 MCP 结果，但写 warning 日志。"""
        def hooks_run(kind, *_args, **_kwargs):
            if kind == "before_mcp":
                return {"blocked": False}
            if kind == "after_mcp":
                raise _Boom("after hook broken")
            return {}

        with mock.patch.object(tools, "_dev_mcp_call_impl",
                                return_value="OK"), \
             mock.patch("hooks.run_workflow", side_effect=hooks_run), \
             self.assertLogs("tools", level="WARNING") as cm:
            out = tools.dev_mcp_call("key: x")
        self.assertEqual(out, "OK")
        self.assertTrue(any("MCP 调用后钩子异常" in m for m in cm.output),
                          f"未记录 after_mcp 异常：{cm.output}")


if __name__ == "__main__":
    unittest.main()
