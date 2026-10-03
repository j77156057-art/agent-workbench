"""缺口 16（Wave28）：Ollama 探活的缓存的 TTL 必须**明显大于**一次探活的耗时。

背景（实测，不是推演）：本机对关闭的回环端口 `socket.connect(("127.0.0.1", 11434))`
要 **2035ms**（正常应 <1ms；这台机器上有东西在拖回环连接），`urllib` 走同一条路 2071ms。
`GET /api/config` 冷启动因此是 **6.1s**：`_live_capability`（首次构造 `LLMClient` 时的
实时探活）4081ms + `check_ollama` 2030ms + 其它 11ms。

原 TTL 是 2.0 秒——与探活本身同量级，而 `ts` 又是在探活**结束**后才写，于是隔两三秒
重开面板就必然重付一次。这里钉住"TTL 要比探活预算大出一个量级"这条不变量。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api  # noqa: E402

#: 探活的 HTTP 超时是 3 秒（`check_ollama` 里写死的 `timeout=3`）。TTL 至少要大于它，
#: 否则"探测还没跑完，缓存已经过期"，缓存等于不存在。
PROBE_BUDGET_S = 3.0


class ProbeCacheTtlTests(unittest.TestCase):
    def setUp(self):
        self._prev = dict(api._ollama_check_cache)
        self.addCleanup(lambda: api._ollama_check_cache.update(self._prev))

    def test_the_ttl_outlives_a_single_probe(self):
        """TTL 必须比一次探活（含 3s HTTP 超时）长——这是原值 2.0 的病根。"""
        self.assertGreater(
            api._OLLAMA_CHECK_TTL, PROBE_BUDGET_S,
            "TTL %ss 不大于一次探活的预算 %ss，缓存几乎必然过期（原值 2.0 就是这个错）"
            % (api._OLLAMA_CHECK_TTL, PROBE_BUDGET_S))

    def test_a_second_call_within_the_window_does_not_probe_again(self):
        """**行为**判据：窗口内第二次调用不能再打一次探活。

        只断言常量是不够的——`ts` 写在哪、`force` 怎么绕，都能让一个"看着够大"的 TTL
        形同虚设。所以这里数**探活调用次数**。
        """
        api._ollama_check_cache.update({"ts": 0.0, "value": None})
        calls = []

        class _Opener:
            def open(self, *_a, **_k):
                calls.append(1)
                raise OSError("refused")            # 模拟 Ollama 不在
        with patch.object(api, "_ollama_no_proxy", _Opener()):
            first = api.check_ollama()
            second = api.check_ollama()
        self.assertEqual(len(calls), 1, "窗口内第二次调用又探了一次（缓存没生效）")
        self.assertIs(first, second, "窗口内应复用同一个结果对象")
        self.assertIs(second["reachable"], False)

    def test_force_bypasses_the_cache(self):
        """`force=True` 必须能拿到即时结果——TTL 变大之后这是"要新鲜"的唯一出口。"""
        api._ollama_check_cache.update({"ts": time.time(), "value": {"stale": True}})
        calls = []

        class _Opener:
            def open(self, *_a, **_k):
                calls.append(1)
                raise OSError("refused")
        with patch.object(api, "_ollama_no_proxy", _Opener()):
            got = api.check_ollama(force=True)
        self.assertEqual(len(calls), 1, "force=True 没有绕过缓存")
        self.assertNotIn("stale", got)

    def test_an_expired_entry_is_recomputed(self):
        api._ollama_check_cache.update({"ts": time.time() - api._OLLAMA_CHECK_TTL - 1,
                                        "value": {"stale": True}})
        calls = []

        class _Opener:
            def open(self, *_a, **_k):
                calls.append(1)
                raise OSError("refused")
        with patch.object(api, "_ollama_no_proxy", _Opener()):
            got = api.check_ollama()
        self.assertEqual(len(calls), 1, "过期的缓存没有被重算")
        self.assertNotIn("stale", got)


if __name__ == "__main__":
    unittest.main()
