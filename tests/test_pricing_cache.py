"""缓存计价 + 阶梯计价的回归。

**要防的是两种"看着很对其实错"的算法**：

1. **缓存命中被按全价重复计**。实测本机 `custom/stealth/space-bunny-alpha` 一次
   调用 `prompt_tokens=21819` 里 `cache_read_tokens=18108`（83% 命中）——按全价算
   会把实际开销高估近 5 倍。反过来，**只按"消耗 token 量"算而完全不看缓存**，也是
   同一类错。所以四条都要钉：纯输入、纯缓存读、混合、缓存写。

2. **阶梯定价被当成平铺单价**。OpenRouter 的模型目录普遍带 `overrides`，实测
   `openai/gpt-6.1-sol-pro` 在 `min_prompt_tokens=272000` 处 prompt 单价翻倍
   （0.000002 → 0.000004），缓存读写也各自跳档。用平铺价算长对话会**系统性低估**
   ——而且低估得很平滑，看数字根本发现不了。

   这里还有一个我自己写出来的 bug，由本文件钉死：只配**一档**且门槛是 272k 时，
   如果把 `ordered[0]` 当基线，**所有低于门槛的调用**（绝大多数）都会被按那一档的
   高价计费，一次 200k 的对话凭空翻倍。所以低于最低门槛必须回落到平铺价。

3. **"免费"与"没查到"必须可区分**。实测 `stealth/space-bunny-alpha` 在
   OpenRouter 上的 `pricing` 就是 `{"prompt":"0","completion":"0"}`——它**真的免费**。
   而"没查到这个模型"是另一回事。两者都报 0 就是假干净，所以 `openrouter_pricing`
   用 `free=True` / `error=...` 分开表达。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pricing  # noqa: E402


class PricingFileCase(unittest.TestCase):
    """把 PRICING_FILE 指到临时文件，避免污染真实状态目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pricing-")
        self.path = os.path.join(self.tmp, "pricing.json")
        self._old_file = pricing.PRICING_FILE
        self._old_env = pricing.os.getenv("DOCMIND_PRICING_FILE")
        pricing.PRICING_FILE = self.path

    def tearDown(self):
        pricing.PRICING_FILE = self._old_file
        if self._old_env is None:
            pricing.os.environ.pop("DOCMIND_PRICING_FILE", None)
        else:
            pricing.os.environ["DOCMIND_PRICING_FILE"] = self._old_env
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, table):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(table, handle)


class CachePricingTests(PricingFileCase):
    """缓存命中必须按折扣价算，且只算一次。"""

    def setUp(self):
        super().setUp()
        self.write({"qwen": {"models": {"qwen-plus": {
            "in": 0.8, "out": 2.0}}}})

    def test_no_cache_matches_the_plain_price(self):
        self.assertAlmostEqual(pricing.cost_cny("qwen", "qwen-plus", 1_000_000, 1_000_000), 2.8)

    def test_a_full_cache_hit_is_ten_times_cheaper(self):
        """全命中：应按输入价的 1/10 计，而不是全价。"""
        self.assertAlmostEqual(
            pricing.cost_cny("qwen", "qwen-plus", 1_000_000, 0, cache_read_tokens=1_000_000),
            0.08)

    def test_cache_write_is_priced_above_the_input_rate(self):
        self.assertAlmostEqual(
            pricing.cost_cny("qwen", "qwen-plus", 1_000_000, 0, cache_creation_tokens=1_000_000),
            1.0)

    def test_a_mixed_call_splits_correctly(self):
        """实测形状：prompt 21819 里 18108 命中缓存。混合必须精确拆分。"""
        total_in, cached = 1_000_000, 900_000
        got = pricing.cost_cny("qwen", "qwen-plus", total_in, 0, cache_read_tokens=cached)
        want = (100_000 * 0.8 + 900_000 * 0.08) / 1_000_000
        self.assertAlmostEqual(got, want)

    def test_cache_tokens_never_make_it_cheaper_than_zero(self):
        """provider 口径差异可能让 cache > in；不能算出负数或负单价。"""
        got = pricing.cost_cny("qwen", "qwen-plus", 100, 0, cache_read_tokens=999_999)
        self.assertGreaterEqual(got, 0.0)

    def test_local_providers_stay_free_even_with_cache(self):
        self.assertEqual(
            pricing.cost_cny("ollama", "x", 1_000_000, 1_000_000, cache_read_tokens=1_000_000),
            0.0)


class TieredPricingTests(PricingFileCase):
    """阶梯定价：按当次输入量选档，低于门槛回落平铺价。"""

    SOL = {"models": {"gpt-6.1-sol-pro": {
        "in": 14.2, "out": 71.0, "cache_read": 0.71, "cache_creation": 17.75,
        "overrides": [{"min_prompt_tokens": 272000,
                       "in": 28.4, "out": 106.5,
                       "cache_read": 1.42, "cache_creation": 35.5}]}}}

    def setUp(self):
        super().setUp()
        self.write({"custom": self.SOL})

    def test_under_the_threshold_uses_the_flat_rate(self):
        """这是我写错过的地方：只配一档且门槛 272k 时，低于门槛必须用平铺价，
        而不是把那一档当基线（那会让 200k 的对话凭空翻倍）。"""
        got = pricing.cost_cny("custom", "gpt-6.1-sol-pro", 200_000, 1_000)
        want = (200_000 * 14.2 + 1_000 * 71.0) / 1_000_000
        self.assertAlmostEqual(got, want)

    def test_over_the_threshold_switches_rate(self):
        got = pricing.cost_cny("custom", "gpt-6.1-sol-pro", 300_000, 1_000)
        want = (300_000 * 28.4 + 1_000 * 106.5) / 1_000_000
        self.assertAlmostEqual(got, want)

    def test_using_the_flat_rate_would_halve_the_bill(self):
        """量化"漏掉阶梯"的后果——说明这条守卫不是装饰。"""
        tiered = pricing.cost_cny("custom", "gpt-6.1-sol-pro", 300_000, 1_000)
        flat = (300_000 * 14.2 + 1_000 * 71.0) / 1_000_000
        self.assertAlmostEqual(flat / tiered, 0.5, places=2)

    def test_cache_prices_also_switch_tier(self):
        got = pricing.cost_cny("custom", "gpt-6.1-sol-pro", 300_000, 0,
                               cache_read_tokens=300_000)
        self.assertAlmostEqual(got, 300_000 * 1.42 / 1_000_000)

    def test_two_tiers_pick_the_matching_one(self):
        self.write({"custom": {"models": {"y": {
            "in": 14.2, "out": 71.0,
            "overrides": [{"min_prompt_tokens": 0, "in": 14.2, "out": 71.0},
                          {"min_prompt_tokens": 272000, "in": 28.4, "out": 106.5}]}}}})
        low = pricing.cost_cny("custom", "y", 100_000, 1_000)
        high = pricing.cost_cny("custom", "y", 400_000, 1_000)
        self.assertAlmostEqual(low, (100_000 * 14.2 + 1_000 * 71.0) / 1_000_000)
        self.assertAlmostEqual(high, (400_000 * 28.4 + 1_000 * 106.5) / 1_000_000)

    def test_a_partial_tier_does_not_zero_the_missing_fields(self):
        """某档只调了 prompt 时，输出价必须沿用平铺档，不能被清零。"""
        self.write({"custom": {"models": {"z": {
            "in": 10.0, "out": 50.0,
            "overrides": [{"min_prompt_tokens": 1000, "in": 20.0}]}}}})
        got = pricing.cost_cny("custom", "z", 2000, 1000)
        want = (2000 * 20.0 + 1000 * 50.0) / 1_000_000
        self.assertAlmostEqual(got, want)


class TierSelectionUnitTests(unittest.TestCase):

    def test_no_tiers_returns_empty(self):
        self.assertEqual(pricing._tier_for(1000, []), {})
        self.assertEqual(pricing._tier_for(1000, None), {})

    def test_below_the_lowest_threshold_returns_empty(self):
        self.assertEqual(pricing._tier_for(10, [{"min_prompt_tokens": 100, "in": 1}]), {})

    def test_it_picks_the_last_matching_tier(self):
        tiers = [{"min_prompt_tokens": 0, "in": 1},
                 {"min_prompt_tokens": 100, "in": 2},
                 {"min_prompt_tokens": 200, "in": 3}]
        self.assertEqual(pricing._tier_for(50, tiers)["in"], 1)
        self.assertEqual(pricing._tier_for(150, tiers)["in"], 2)
        self.assertEqual(pricing._tier_for(9999, tiers)["in"], 3)

    def test_unsorted_input_is_handled(self):
        tiers = [{"min_prompt_tokens": 200, "in": 3}, {"min_prompt_tokens": 0, "in": 1}]
        self.assertEqual(pricing._tier_for(500, tiers)["in"], 3)


class OpenRouterLookupTests(unittest.TestCase):
    """联网检索。默认 skip（要联网），但**结构与失败路径**无条件可测。"""

    def setUp(self):
        self._old = pricing.os.environ.get("DOCMIND_USD_CNY")
        pricing.os.environ["DOCMIND_USD_CNY"] = "7.0"

    def tearDown(self):
        if self._old is None:
            pricing.os.environ.pop("DOCMIND_USD_CNY", None)
        else:
            pricing.os.environ["DOCMIND_USD_CNY"] = self._old

    def test_usd_to_cny_uses_the_env_override(self):
        self.assertEqual(pricing._usd_to_cny_rate(), 7.0)

    def test_a_bad_rate_falls_back_instead_of_crashing(self):
        pricing.os.environ["DOCMIND_USD_CNY"] = "not-a-number"
        self.assertGreater(pricing._usd_to_cny_rate(), 0)
        pricing.os.environ["DOCMIND_USD_CNY"] = "-1"
        self.assertGreater(pricing._usd_to_cny_rate(), 0)

    def test_an_empty_model_name_is_refused(self):
        result = pricing.openrouter_pricing("")
        self.assertIn("error", result)
        self.assertNotIn("in", result)

    def test_a_network_failure_says_so_instead_of_reporting_free(self):
        """这条最重要：检索失败必须报 error，**不能**返回 0 冒充「免费」——
        两者含义完全不同，混起来就是本仓最贵的那种假干净。"""
        import urllib.error
        import urllib.request as request

        def boom(*args, **kwargs):
            raise urllib.error.URLError("no network")

        saved = request.urlopen
        request.urlopen = boom
        try:
            result = pricing.openrouter_pricing("some/model", timeout=1)
        finally:
            request.urlopen = saved
        self.assertIn("error", result)
        self.assertNotIn("free", result)

    @unittest.skipUnless(os.getenv("DOCMIND_TEST_NETWORK") == "1",
                         "需要联网；设 DOCMIND_TEST_NETWORK=1 才跑")
    def test_a_free_model_is_reported_as_free_not_as_missing(self):
        """实测 stealth/space-bunny-alpha 的 pricing 就是 0——它**真的免费**。"""
        result = pricing.openrouter_pricing("stealth/space-bunny-alpha")
        self.assertNotIn("error", result)
        self.assertTrue(result["free"])
        self.assertEqual(result["in"], 0.0)

    @unittest.skipUnless(os.getenv("DOCMIND_TEST_NETWORK") == "1",
                         "需要联网；设 DOCMIND_TEST_NETWORK=1 才跑")
    def test_a_paid_model_comes_back_with_tiers_and_cny(self):
        result = pricing.openrouter_pricing("openai/gpt-6.1-sol-pro")
        self.assertNotIn("error", result)
        self.assertFalse(result["free"])
        self.assertGreater(result["in"], 0)
        self.assertEqual(result["currency"], "CNY")
        self.assertTrue(result["tiers"], "阶梯定价必须一并取回，否则长对话会被低估")
        self.assertIn("min_prompt_tokens", result["tiers"][0])


if __name__ == "__main__":
    unittest.main()
