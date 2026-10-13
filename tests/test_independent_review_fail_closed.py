# -*- coding: utf-8 -*-
"""决策 9（fail-closed）：`independent_review` 在非 enterprise 环境下不再静默放行。

## 为什么

`independent_review.py` 原自杀式默认值：`execution_mode() != "enterprise"` 时直接
返回 `{"ok": True, "status": "local_mode", "independent": False}` —— 在无人值守的
自主舱里等于静默通过，与 `visual_acceptance` 的 fail-closed 立场互相打架
（`docs/cockpit-v2-redesign.md` §4.5）。

改为：非企业环境无法提供容器级隔离的独立复核证据，必须**显式失败**
（`ok: False, status: "unverified", independent: True`），由末尾验收守卫拦下。

## 可证伪

本文件只依赖 `agent_runtime.independent_review`（v2 重构里**保留迁移**的模块），
不碰 `game_workflow` / `workflow_reflection`（删除清单），所以能活过后端瘦身。
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.independent_review import review_project  # noqa: E402


class IndependentReviewFailClosedTests(unittest.TestCase):
    def test_non_enterprise_mode_is_fail_closed(self):
        """非 enterprise 环境必须 fail-closed，不得 `ok: True`。"""
        with patch("agent_runtime.independent_review.execution_mode", return_value="local"):
            report = review_project("/tmp/irrelevant", {}, "/tmp/state")
        self.assertFalse(report["ok"], "非企业环境必须 fail-closed，不能静默通过")
        self.assertEqual(report["status"], "unverified")
        self.assertTrue(report["independent"], "必须标记为独立复核产物")
        self.assertIn("fail-closed", report["message"])

    def test_enterprise_mode_never_takes_fail_closed_shortcut(self):
        """对照守卫：enterprise 模式绝不走 fail-closed 早返回分支。

        `fail-closed` 文案是「非企业环境」短路分支的专属标记；enterprise 模式
        即便因 checkpoint 缺失而返回 `unverified`，也绝不能带上该文案——那是
        两条不同的判定链（后者会进入完整 sha256 比对 + 真跑测试）。
        """
        with patch("agent_runtime.independent_review.execution_mode", return_value="enterprise"):
            report = review_project("/tmp/irrelevant", {}, "/tmp/state")
        self.assertNotIn("fail-closed", report["message"],
                         "enterprise 模式不允许出现 fail-closed 短路文案")


if __name__ == "__main__":
    unittest.main()
