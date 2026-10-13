# -*- coding: utf-8 -*-
"""决策 7 第二层写闸 + 决策 17 写账本：可证伪验证。

## 为什么

`lanes.py` 原本是「君子协定」——只有 `dev_lanes` 工具被 agent 主动调用，没有任何
写路径落盘前反查归属。本测试证明：
  · 一个绑定了 lane **B** 的小对话，试图写 lane **A** 已认领的文件 → 被拦截且不落盘
    （判据 7）；
  · 绑定了正确 lane **A** 的小对话 → 放行且真实写入；
  · 无 lane 上下文（单用户 / 编排主代理）→ 零开销透传，即便文件被认领也放行；
  · 真实写工具 `apply_edit` 在越界时被拒绝、**磁盘文件字节不变**；
  · 写账本（决策 17）只记「有 lane 上下文」的写，单用户模式零 I/O。

## 零开销透传的反向守卫

单用户模式是主使用场景：绝不因这道附加安全层而打断正常写。所以「无 lane 上下文
时一律放行」是本测试必须守住的底线。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime import lanes
import tools as tools_mod


class LanesWriteGateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lanes_gate_")
        # 让 tools._get_code_root() 指向临时根
        self._patchers = [
            patch.object(tools_mod, "get_runtime",
                         lambda key: self.root if key == "code_root" else ""),
        ]
        for p in self._patchers:
            p.start()
        # 预置一个被认领的文件
        self.target = os.path.join(self.root, "mod.py")
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("X = 1\n")

    def tearDown(self):
        lanes.set_current_lane(None)
        try:
            lanes.release(self.root, "AI-A")
        except Exception:  # noqa: BLE001
            pass
        try:
            lanes.release(self.root, "AI-B")
        except Exception:  # noqa: BLE001
            pass
        for p in self._patchers:
            p.stop()

    def _edit(self, new_text, old_text="X = 1"):
        return tools_mod.apply_edit(
            "path: mod.py\nold_text: %s\nnew_text: %s" % (old_text, new_text))

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_same_lane_write_is_allowed(self):
        lanes.claim(self.root, "AI-A", ["mod.py"])
        lanes.set_current_lane("AI-A")
        out = self._edit("X = 2")
        self.assertIn("已写入", out, "认领方写自己的文件必须放行")
        self.assertEqual(self._read(self.target), "X = 2\n")

    def test_cross_lane_write_is_blocked_and_not_written(self):
        lanes.claim(self.root, "AI-A", ["mod.py"])
        lanes.set_current_lane("AI-B")  # 越界的小对话
        out = self._edit("X = 99")
        self.assertIn("拒绝写入", out, "越界写必须被闸拦截")
        self.assertIn("AI-A", out)
        # 关键：磁盘文件字节不变
        self.assertEqual(self._read(self.target), "X = 1\n",
                         "被拦截的写绝不能落盘")

    def test_no_lane_context_is_pass_through_even_when_claimed(self):
        lanes.claim(self.root, "AI-A", ["mod.py"])
        lanes.set_current_lane(None)  # 单用户 / 编排主代理
        out = self._edit("X = 7")
        self.assertIn("已写入", out, "无 lane 上下文必须零开销透传")
        self.assertEqual(self._read(self.target), "X = 7\n")

    def test_gate_core_logic(self):
        lanes.claim(self.root, "AI-A", ["mod.py"])
        lanes.set_current_lane("AI-B")
        self.assertIsNotNone(lanes.write_gate(self.root, "mod.py"),
                              "越界：gate 必须返回阻塞原因")
        lanes.set_current_lane("AI-A")
        self.assertIsNone(lanes.write_gate(self.root, "mod.py"),
                          "认领方：gate 必须放行")
        lanes.set_current_lane(None)
        self.assertIsNone(lanes.write_gate(self.root, "mod.py"),
                          "无 lane 上下文：gate 必须透传")
        # 未被认领的文件：任何 lane 都放行
        lanes.set_current_lane("AI-Z")
        self.assertIsNone(lanes.write_gate(self.root, "other.py"),
                          "无人认领的文件：gate 必须放行")

    def test_ledger_records_only_lane_writes(self):
        lanes.claim(self.root, "AI-A", ["mod.py"])
        # 无 lane 上下文：不应记任何账
        lanes.set_current_lane(None)
        self._edit("X = 2")
        self.assertEqual(lanes.read_write_ledger(self.root)["count"], 0,
                         "单用户模式写账本必须零 I/O")
        # 有 lane：应记账（先恢复文件为 X = 1 以匹配 old_text）
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        lanes.set_current_lane("AI-A")
        self._edit("X = 3")
        ledger = lanes.read_write_ledger(self.root, lane="AI-A")
        self.assertGreaterEqual(ledger["count"], 1, "lane 写必须入账")
        entry = ledger["writes"][-1]
        self.assertEqual(entry["op"], "modify")
        self.assertEqual(entry["path"], "mod.py")


if __name__ == "__main__":
    unittest.main()
