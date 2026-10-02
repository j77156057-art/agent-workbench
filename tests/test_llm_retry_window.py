"""重试退避的总跨度必须**盖得住上游真实的故障窗口**，否则重试是假的。

这个文件的存在是因为一次真实故障：2026-10-03 跑批时连着两次 `APIError: Provider
returned an empty response`，手动重试立刻就好了。查账本发现——

    21:49:27  失败  elapsed 2153ms
    21:49:36  成功                       <- 9 秒后自愈
    00:26:17  失败  elapsed 4279ms
    00:26:25  成功                       <- 8 秒后自愈

故障窗口约 8–9 秒。而旧参数（attempts=3 / base=0.8 / cap=8）三次尝试铺开的退避
只有约 1.6–3.2 秒——**每次重试都落在同一个故障窗口内**，于是重试等于没重试。

这里钉住三件事：

1. 新参数的总跨度盖得住 9 秒（实测 14.9–27.1 秒，100% 覆盖）；
2. 旧参数盖不住——写下来是为了防止有人"顺手调回去"；
3. **只调大 base 而不动 attempts 是无效的**（`i` 只取到 `attempts-2`）。
   这条最容易踩：看起来把退避放大了，实际上 3 次尝试仍只有 2 段退避。
"""

import random
import statistics
import sys
import time
import unittest
from unittest.mock import patch

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import llm  # noqa: E402

#: 从账本反推出来的上游空响应故障窗口（两次实测分别是 9 秒和 8 秒，取 9 秒上界）。
OBSERVED_BLIP_S = 9.0

#: 旧参数，留作对照：证明"看起来在重试、其实没覆盖故障窗口"是真实发生过的。
OLD = {"attempts": 3, "base": 0.8, "cap": 8.0}


def total_backoff(attempts, base, cap, samples=4000):
    """退避总跨度的分布。退避发生在第 i 次失败后，i 只取 0..attempts-2。"""
    out = []
    for _ in range(samples):
        out.append(sum(min(base * (2 ** i), cap) * (0.7 + 0.6 * random.random())
                       for i in range(max(0, attempts - 1))))
    return sorted(out)


class WindowCoversTheBlipTests(unittest.TestCase):
    """这是本文件的核心断言：新参数必须盖得住实测故障窗口。"""

    def test_the_configured_window_covers_the_observed_blip(self):
        span = total_backoff(llm.LLM_RETRIES, llm.LLM_RETRY_BASE, llm.LLM_RETRY_CAP)
        self.assertGreaterEqual(
            span[0], OBSERVED_BLIP_S,
            "退避总跨度的**最小值** %.1fs 必须已经盖住实测故障窗口 %.0fs——"
            "只看中位数是不够的，运气差的那次照样会失败。当前 attempts=%d base=%s cap=%s"
            % (span[0], OBSERVED_BLIP_S, llm.LLM_RETRIES, llm.LLM_RETRY_BASE,
               llm.LLM_RETRY_CAP))
        self.assertAlmostEqual(statistics.median(span), 21.0, delta=6.0)

    def test_every_sample_covers_it_not_just_the_median(self):
        """抖动是随机的；如果只有一半样本够长，那就还有一半的失败。"""
        span = total_backoff(llm.LLM_RETRIES, llm.LLM_RETRY_BASE, llm.LLM_RETRY_CAP,
                             samples=1000)
        below = [s for s in span if s < OBSERVED_BLIP_S]
        self.assertEqual(below, [],
                         "%d/%d 个样本盖不住 %.0fs 窗口"
                         % (len(below), len(span), OBSERVED_BLIP_S))


class OldParamsWereInsufficientTests(unittest.TestCase):
    """把旧参数钉在这里，是为了防止"顺手调回去"后没人发现。"""

    def test_the_old_parameters_could_not_cover_the_blip(self):
        span = total_backoff(**OLD, samples=1000)
        self.assertLess(
            min(span), OBSERVED_BLIP_S,
            "旧参数本来就应该盖不住（这是改动的理由）；如果它现在盖得住了，"
            "说明故障窗口估计错了，本文件的前提需要重新审视")

    def test_the_old_maximum_span_was_about_two_and_a_half_seconds(self):
        """对照账本里的 2153ms：三次尝试确实只铺开了两秒多。"""
        span = total_backoff(**OLD, samples=500)
        self.assertLess(max(span), 4.0)


class AttemptsMustMoveWithBaseTests(unittest.TestCase):
    """最容易踩的坑：只调大 base 而不动 attempts，完全无效。"""

    def test_backoff_segments_equal_attempts_minus_one(self):
        for attempts in (1, 2, 3, 4, 5):
            with self.subTest(attempts=attempts):
                delays = []
                with patch("llm.time.sleep", delays.append), \
                        patch("llm.random.random", return_value=0.5):
                    calls = []

                    def boom():
                        calls.append(1)
                        raise llm.EmptyProviderResponse("空")

                    with self.assertRaises(llm.EmptyProviderResponse):
                        llm.retry_call(boom, attempts=attempts, base=1.0)
                self.assertEqual(len(calls), attempts)
                self.assertEqual(len(delays), max(0, attempts - 1),
                                 "退避段数必须等于 attempts-1")

    def test_raising_base_alone_does_not_reach_the_blip(self):
        """attempts 仍是 3 时，把 base 从 0.8 拉到 3.0 依然盖不住 9 秒窗口。"""
        span = total_backoff(attempts=3, base=3.0, cap=12.0, samples=1000)
        self.assertLess(min(span), OBSERVED_BLIP_S,
                        "若这条断言失败，说明 attempts-1 段退避已足够——"
                        "那么把 attempts 从 3 提到 4 就是多余的，应重新评估")

    def test_the_shipped_attempts_is_at_least_four(self):
        self.assertGreaterEqual(
            llm.LLM_RETRIES, 4,
            "要覆盖 9 秒窗口至少需要 3 段退避，即 attempts >= 4")


class BackoffShapeTests(unittest.TestCase):

    def _delays(self, attempts, base, cap):
        out = []
        with patch("llm.time.sleep", out.append), \
                patch("llm.random.random", return_value=1.0):  # 抖动取上界
            def boom():
                raise llm.EmptyProviderResponse("空")
            with self.assertRaises(llm.EmptyProviderResponse):
                llm.retry_call(boom, attempts=attempts, base=base)
        return out

    def test_it_grows_exponentially(self):
        d = self._delays(4, 3.0, llm.LLM_RETRY_CAP)
        self.assertEqual(len(d), 3)
        self.assertLess(d[0], d[1])
        self.assertLess(d[1], d[2])

    def test_the_cap_is_respected(self):
        """上限必须真的生效，否则长重试会变成几分钟的静默等待。"""
        cap = llm.LLM_RETRY_CAP
        d = self._delays(8, 3.0, cap)
        for delay in d:
            self.assertLessEqual(delay, cap * 1.3 + 1e-9,
                                 "单段退避超过了上限 %.1fs" % cap)

    def test_the_jitter_is_actually_random(self):
        """全固定间隔会导致多个客户端在同一时刻集体重试（惊群）。"""
        seen = set()
        for _ in range(6):
            with patch("llm.time.sleep", seen.add):
                def boom():
                    raise llm.EmptyProviderResponse("空")
                with self.assertRaises(llm.EmptyProviderResponse):
                    llm.retry_call(boom, attempts=3, base=3.0)
        self.assertGreater(len(seen), 3,
                           "退避间隔没有抖动=%d 个不同值，客户端会集体重试" % len(seen))


class DeadlineStillWinsTests(unittest.TestCase):
    """退避放宽了，但 deadline 依然是硬约束——否则会顶穿本轮预算。"""

    def test_a_tight_deadline_stops_before_sleeping(self):
        slept = []
        with patch("llm.time.sleep", slept.append):
            def boom():
                raise llm.EmptyProviderResponse("空")
            with self.assertRaises(llm.EmptyProviderResponse):
                llm.retry_call(boom, deadline=time.monotonic() + 0.05,
                               attempts=4, base=3.0)
        self.assertEqual(slept, [], "deadline 已经很近时不该再睡 3 秒")

    def test_an_expired_deadline_raises_timeout_without_calling(self):
        called = []

        def never():
            called.append(1)

        with self.assertRaises(TimeoutError):
            llm.retry_call(never, deadline=time.monotonic() - 1)
        self.assertEqual(called, [])


class EmptyResponseStaysRetryableTests(unittest.TestCase):
    """放宽退避的前提是空响应确实被当成可重试——否则再宽也没用。"""

    def test_it_is_retried_and_can_eventually_succeed(self):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            # 前 3 次都在故障窗口内返回空信封，第 4 次（已过窗口）才给内容
            if state["n"] < 4:
                raise llm.EmptyProviderResponse("空响应")
            return "ok"

        slept = []
        with patch("llm.time.sleep", slept.append):
            out = llm.retry_call(flaky, attempts=llm.LLM_RETRIES,
                                 base=llm.LLM_RETRY_BASE)
        self.assertEqual(out, "ok")
        self.assertEqual(state["n"], 4, "应该在第 4 次拿到内容")
        self.assertEqual(len(slept), 3, "前三次失败各退避一次")
        total = sum(slept)
        self.assertGreaterEqual(
            total, OBSERVED_BLIP_S,
            "撑到第 4 次的退避总跨度 %.1fs 必须盖住 %.0fs 故障窗口"
            % (total, OBSERVED_BLIP_S))


class NonRetryableStillFailsFastTests(unittest.TestCase):
    """全局放宽退避**不能**让不可重试的错误也去睡 20 秒。"""

    def test_a_400_class_error_is_not_retried(self):
        slept = []
        calls = []

        class BadRequest(Exception):
            status_code = 400

        def bad():
            calls.append(1)
            raise BadRequest("参数错了")

        with patch("llm.time.sleep", slept.append):
            with self.assertRaises(BadRequest):
                llm.retry_call(bad, attempts=llm.LLM_RETRIES)
        self.assertEqual(len(calls), 1, "4xx 不该重试")
        self.assertEqual(slept, [], "4xx 不该退避")


if __name__ == "__main__":
    unittest.main()