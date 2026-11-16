# -*- coding: utf-8 -*-
"""常开真实网络验收 lane（2026-11-17 挂 CI）。

此前真实网络验收全是 opt-in vars（本地校园网把出口墙掉，vars 一直没设），
等于没有门。CI runner 出口干净，可以常开；但外部站点仍有抖动/验证码风险，
策略是：

* **重试**：每个探针最多 3 次、指数退避；全败才红。
* **可见豁免**：搜索类探针若全败且失败签名是「封禁/验证码/空结果墙」，
  记 skip 并带原因（CI summary 的 skipped 计数可见），不静默绿；
  抓取类探针（example.com，IANA 保留域）**不允许豁免**——它失败就是真红。
* 判据扣机制：抓取要回页面特有内容；搜索要回真实链接，不扣措辞。
"""
import os
import re
import time
import unittest

import tools

BLOCK_SIGNATURES = ("captcha", "verify", "blocked", "forbidden", "403",
                    "验证码", "封禁", "人机", "unusual traffic",
                    "unavailable", "不可用", "无结果", "no results", "未配置")


def _retry(fn, attempts=3):
    last = None
    for i in range(attempts):
        try:
            out = fn()
            if out:
                return out, i + 1
            last = "empty result"
        except Exception as exc:  # noqa: BLE001 —— 探针要记录每次失败签名
            last = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(2 ** i)
    return None, last


class RealNetworkLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 本地（校园网）出口已知被墙，常开门挂在 CI runner（出口干净）。
        # 本地显式 DOCMIND_REAL_NETWORK=1 可强跑；skip 在计数里可见，不静默。
        if not os.environ.get("CI") and not os.environ.get("DOCMIND_REAL_NETWORK"):
            raise unittest.SkipTest("真实网络 lane 以 CI 为门；本地强跑设 DOCMIND_REAL_NETWORK=1")
    def test_web_fetch_returns_page_content(self):
        out, info = _retry(lambda: tools.web_fetch("https://example.com"))
        self.assertIsNotNone(out, "example.com 抓取三次全败：%s" % info)
        self.assertIn("Example Domain", out,
                      "抓回了但不是 example.com 的内容：%s" % str(out)[:120])

    def test_web_search_returns_real_links(self):
        # godot 类查询能命中辅助源（GitHub 等），比通用词在数据中心 IP 下更稳。
        out, info = _retry(lambda: tools.web_search("godot engine github repository"))
        if out and re.search(r"https?://", out):
            return
        signature = str(info or "").lower() + " " + str(out or "").lower()
        if any(s in signature for s in BLOCK_SIGNATURES):
            self.skipTest("搜索引擎封禁/验证码/不可用墙（可见豁免，非静默绿）：%s"
                          % str(out or info)[:160])
        self.fail("搜索三次全败且非封禁签名：info=%s out=%s"
                  % (str(info)[:120], str(out)[:160]))


if __name__ == "__main__":
    unittest.main(verbosity=2)