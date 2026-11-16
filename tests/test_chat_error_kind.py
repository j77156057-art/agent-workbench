"""聊天失败分类：配额/余额类不得误报成「疑似网络不稳定」。

2026-11-17 实机深测抓到：OpenCode 月配额 429（上游明说 12 天后重置）被归入
通用重试黄条，用户会无效重试 12 天。这里钉住分类与文案的可行动性。
"""
import unittest

import api
from llm import MissingAPIKey, is_quota_error

QUOTA_MSG = ("Error code: 429 - OpenCode Go usage limit reached (monthly, resets in 12d 9h). "
             "To keep going now, turn on Extra Usage or upgrade to Go Plus.")


class QuotaDetectTests(unittest.TestCase):
    def test_monthly_quota_is_quota(self):
        self.assertTrue(is_quota_error(RuntimeError(QUOTA_MSG)))

    def test_transient_429_is_not_quota(self):
        self.assertFalse(is_quota_error(
            RuntimeError("Error code: 429 - Rate limit reached for requests, retry after 2s")))

    def test_402_balance_is_quota(self):
        class BalanceError(RuntimeError):
            status_code = 402
        self.assertTrue(is_quota_error(BalanceError("Insufficient balance")))

    def test_network_error_is_not_quota(self):
        self.assertFalse(is_quota_error(ConnectionError("connection reset by peer")))


class ClassifyTests(unittest.TestCase):
    def test_quota_text_is_actionable_and_keeps_upstream_reason(self):
        kind, text = api._classify_chat_failure(RuntimeError(QUOTA_MSG))
        self.assertEqual(kind, "retried_failed")
        self.assertIn("配额", text)
        self.assertIn("切换", text)
        self.assertIn("resets in 12d 9h", text)
        self.assertNotIn("疑似网络", text)

    def test_network_error_keeps_generic_retry_text(self):
        kind, text = api._classify_chat_failure(ConnectionError("connection reset by peer"))
        self.assertEqual(kind, "retried_failed")
        self.assertIn("疑似网络", text)

    def test_missing_key_stays_permanent(self):
        kind, text = api._classify_chat_failure(MissingAPIKey("未配置 DASHSCOPE_API_KEY"))
        self.assertEqual(kind, "permanent")
        self.assertIn("DASHSCOPE_API_KEY", text)

    def test_system_exit_stays_permanent(self):
        kind, _ = api._classify_chat_failure(SystemExit(1))
        self.assertEqual(kind, "permanent")

    def test_non_retryable_client_error_permanent(self):
        class BadParam(RuntimeError):
            status_code = 400
        kind, _ = api._classify_chat_failure(BadParam("invalid parameter"))
        self.assertEqual(kind, "permanent")


if __name__ == "__main__":
    unittest.main()