"""缓存要"标出来"——不只是显示 token 数，还要说清它省了多少钱、以及**不能**怎么算。

用户看到的诉求是"没标出缓存"。但真正缺的不只是那列数字：账本里 290 万 cache_read
token 一直在参与计费，界面上一个数都没有，于是既看不出缓存有没有生效，也看不出它值多少钱。

这个文件钉住三件容易算错的事：

1. **省下的钱必须与实际收的钱出自同一份单价**（含阶梯档）。曾经想在前端算，于是两处
   逻辑各写一遍；阶梯档一改就对不上，而且没人会发现。
2. **缓存写入不是"省"**。DeepSeek 写缓存 1.25x 输入价、Anthropic 1.25x——比全价**贵**。
   把它算进节省会得出比实际便宜的数。
3. **没配单价时不能返回 0 冒充"省了 0 元"**。那和把"免费"说成"没算"是同一种错：
   数字对，含义错。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pricing  # noqa: E402


class Case(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cache-saved-")
        self._old = pricing.PRICING_FILE
        pricing.PRICING_FILE = os.path.join(self.tmp, "pricing.json")

    def tearDown(self):
        pricing.PRICING_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, data):
        with open(pricing.PRICING_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


class SavingMatchesTheChargeTests(Case):
    """省下的钱 = 不用缓存会收的 − 实际收的。这条等式必须严格成立。"""

    def test_the_saving_equals_the_difference_in_charges(self):
        self.seed({"deepseek": {"in": 1.0, "out": 2.0, "cache_read": 0.1}})
        in_t, cr_t, out_t = 4_000_000, 2_700_000, 480_000
        saved = pricing.cache_saved_cny("deepseek", "", in_t, cr_t, 0)
        charged = pricing.cost_cny("deepseek", "", in_t, out_t, cr_t, 0)
        full = pricing.cost_cny("deepseek", "", in_t, out_t, 0, 0)
        self.assertAlmostEqual(saved, full - charged, places=9)
        # 反过来也成立：充值时确实便宜了这么多
        self.assertAlmostEqual(charged + saved, full, places=9)

    def test_no_cache_hit_saves_nothing(self):
        self.seed({"deepseek": {"in": 1.0, "out": 2.0, "cache_read": 0.1}})
        self.assertEqual(pricing.cache_saved_cny("deepseek", "", 1000, 0, 0), 0.0)


class CacheWriteIsNotASavingTests(Case):
    """写入价高于全价——算成"省"会得出比实际更便宜的钱。"""

    def test_a_pricey_write_reduces_the_claimed_saving(self):
        self.seed({"deepseek": {"in": 1.0, "out": 2.0,
                                "cache_read": 0.1, "cache_creation": 1.25}})
        without_write = pricing.cache_saved_cny("deepseek", "", 10000, 5000, 0)
        with_write = pricing.cache_saved_cny("deepseek", "", 10000, 5000, 3000)
        self.assertLess(with_write, without_write,
                        '写入比全价贵，不能让它虚增「省下的钱」')

    def test_it_never_returns_a_negative_saving(self):
        """只写缓存、没命中任何缓存时，写入的成本不该变成"省了 -X 元"。"""
        self.seed({"deepseek": {"in": 1.0, "out": 2.0,
                                "cache_read": 0.1, "cache_creation": 2.0}})
        self.assertGreaterEqual(pricing.cache_saved_cny("deepseek", "", 10000, 0, 5000), 0.0)

    def test_a_free_cache_price_saves_nothing_rather_than_inflating(self):
        """缓存价 == 全价（没折扣）时省 0，不是负数也不是"全额"。"""
        self.seed({"p": {"in": 1.0, "out": 2.0, "cache_read": 1.0}})
        self.assertEqual(pricing.cache_saved_cny("p", "", 10000, 5000, 0), 0.0)


class NoFakeZeroTests(Case):
    """没配单价时不能拿 0 冒充"省了 0 元"。"""

    def test_an_unpriced_provider_reports_zero(self):
        self.assertEqual(pricing.cache_saved_cny("neverheardof", "x", 10000, 5000), 0.0)

    def test_a_local_provider_reports_zero(self):
        self.assertEqual(pricing.cache_saved_cny("ollama", "qwen3:4b", 10000, 5000), 0.0)

    def test_a_free_model_reports_zero(self):
        """真的免费的模型：省 0 是**对的**（本来就不用花钱），不是"没算"。"""
        self.seed({"custom": {"models": {"free/one": {"in": 0.0, "out": 0.0,
                                                       "free": True}}}})
        self.assertEqual(pricing.cache_saved_cny("custom", "free/one", 10000, 5000), 0.0)


class TierConsistencyTests(Case):
    """阶梯档下，"省"与"收"必须用同一档——否则长上下文会算出负的节省。"""

    def test_both_use_the_same_tier(self):
        self.seed({"custom": {"models": {"m": {
            "in": 10.0, "out": 20.0, "cache_read": 1.0,
            "overrides": [{"min_prompt_tokens": 200000, "in": 20.0,
                           "cache_read": 2.0}]}}}})
        # 300k 输入落在 override 档：输入 20、缓存读 2
        saved = pricing.cache_saved_cny("custom", "m", 300000, 100000, 0)
        charged = pricing.cost_cny("custom", "m", 300000, 1000, 100000, 0)
        full = pricing.cost_cny("custom", "m", 300000, 1000, 0, 0)
        self.assertAlmostEqual(saved, full - charged, places=9)
        self.assertGreater(saved, 0, "高档位下缓存仍有折扣，不该算出 0 或负数")

    def test_the_same_helpers_are_reused_not_reimplemented(self):
        """刻意的结构约束：单价解析只有一份实现。"""
        import inspect
        save_src = inspect.getsource(pricing.cache_saved_cny)
        self.assertIn("_rates(", save_src,
                      "cache_saved_cny 必须走 _rates，不要自己再解析一遍单价")


class TraceAggregationTests(unittest.TestCase):
    """按 provider 的聚合要带上缓存节省，设置页那列才有数可显示。"""

    def _run(self, rows):
        import agent_trace
        original = agent_trace.TRACE_FILE
        tmp = tempfile.mkdtemp(prefix="trace-cache-")
        agent_trace.TRACE_FILE = os.path.join(tmp, "t.jsonl")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(setattr, agent_trace, "TRACE_FILE", original)
        with open(agent_trace.TRACE_FILE, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return agent_trace.summary()

    def _rows(self):
        # 两行**不同 model**，同一个 provider：deepseek-chat 与 deepseek-reasoner
        return [
            {"provider": "deepseek", "model": "deepseek-chat", "prompt_tokens": 100000,
             "completion_tokens": 1000, "cache_read_tokens": 50000,
             "cache_creation_tokens": 0, "cost_cny": 0.0, "aborted": False,
             "error": None, "elapsed_ms": 5},
            {"provider": "deepseek", "model": "deepseek-reasoner", "prompt_tokens": 200000,
             "completion_tokens": 2000, "cache_read_tokens": 80000,
             "cache_creation_tokens": 0, "cost_cny": 0.0, "aborted": False,
             "error": None, "elapsed_ms": 5},
        ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cache-seed-")
        self._old = pricing.PRICING_FILE
        pricing.PRICING_FILE = os.path.join(self.tmp, "pricing.json")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, pricing, "PRICING_FILE", self._old)
        self.seed({
            "deepseek": {"models": {
                "deepseek-chat": {"in": 1.0, "out": 2.0, "cache_read": 0.1},
                "deepseek-reasoner": {"in": 4.0, "out": 8.0, "cache_read": 0.4},
            }},
        })

    def seed(self, data):
        with open(pricing.PRICING_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_each_provider_slot_reports_its_saving(self):
        agg = self._run(self._rows())
        slot = agg["by_provider"]["deepseek"]
        self.assertIn("cache_saved_cny", slot)
        expected = (50000 * (1.0 - 0.1) + 80000 * (4.0 - 0.4)) / 1_000_000.0
        self.assertAlmostEqual(slot["cache_saved_cny"], expected, places=9)

    def test_each_row_uses_its_own_model_price(self):
        """同一个 provider 换过模型时，统一用最后见到的 model 会按错价折算。"""
        agg = self._run(self._rows())
        chat_only = 50000 * (1.0 - 0.1) / 1_000_000.0
        reasoner_only = 80000 * (4.0 - 0.4) / 1_000_000.0
        got = agg["by_provider"]["deepseek"]["cache_saved_cny"]
        self.assertAlmostEqual(got, chat_only + reasoner_only, places=9)
        # 用错价（reasoner 单价折算全部）会得到一个明显不同的数
        wrong = 130000 * (4.0 - 0.4) / 1_000_000.0
        self.assertNotAlmostEqual(got, wrong, places=6)

    def test_the_total_matches_the_sum_of_providers(self):
        agg = self._run(self._rows())
        total = agg["cache_saved_cny"]
        parts = sum(s.get("cache_saved_cny", 0.0) for s in agg["by_provider"].values())
        self.assertAlmostEqual(total, parts, places=9)

    def test_a_pricing_failure_does_not_break_the_stats(self):
        """节省是附注；取不到不能让整个统计挂掉。"""
        saved = pricing.cache_saved_cny
        pricing.cache_saved_cny = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            agg = self._run(self._rows())
        finally:
            pricing.cache_saved_cny = saved
        self.assertEqual(agg["cache_read_tokens"], 130000)
        self.assertEqual(agg["cache_saved_cny"], 0.0)


class FrontendGuardTests(unittest.TestCase):
    """前端只负责显示。省的钱必须在后端算完——单价和阶梯档都在那边。"""

    def _read(self, rel):
        with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as handle:
            return handle.read()

    def test_the_settings_page_reads_the_backend_saving(self):
        source = self._read("frontend/src/workbench/components/SettingsView.vue")
        self.assertIn("cache_saved_cny", source)

    def test_the_saving_is_not_recomputed_in_the_frontend(self):
        """前端自己乘单价 = 两处实现 = 迟早对不上。"""
        import re
        source = self._read("frontend/src/workbench/components/SettingsView.vue")
        code = re.sub(r"//.*|/\*.*?\*/|\*.*", "", source, flags=re.S)
        self.assertNotRegex(code, r"cacheRead[A-Za-z]*\s*\*\s*[\w.]*[Pp]rice",
                            "前端不许自己拿 token 数乘单价折算节省")

    def test_the_trace_type_declares_the_new_fields(self):
        source = self._read("frontend/src/workbench/api/harness.ts")
        self.assertIn("cache_saved_cny", source)

    def test_the_provider_table_has_a_cache_column(self):
        source = self._read("frontend/src/workbench/components/SettingsView.vue")
        self.assertIn("缓存命中", source)
        self.assertIn("sv-cache", source)

    def test_the_grid_has_a_column_for_the_new_cell(self):
        """加了一列却没改 grid-template-columns = 单元格挤到下一行，表格直接错位。

        注意计数方式：`minmax(90px, 1fr)` 里**有空格**，直接 `split()` 会数成两列，
        于是一条真错位的表格也能"通过"。所以按顶层逗号/空白数，忽略括号内的内容。
        """
        source = self._read("frontend/src/workbench/components/SettingsView.vue")
        import re

        def columns(spec):
            """数 grid 规格的列数：括号内的空格不算分隔符。"""
            spec = re.sub(r"\([^()]*\)", "X", spec)
            return len([part for part in spec.split() if part])

        grids = re.findall(r"grid-template-columns:\s*([^;]+);", source)
        self.assertTrue(grids)
        self.assertTrue(any(columns(g) == 5 for g in grids),
                        "供应商表有 5 个表头（含新增的缓存列），grid 也要是 5 列：%s"
                        % [(g.strip(), columns(g)) for g in grids])


class BundleTests(unittest.TestCase):
    """产物里必须有这些文案，否则用户看到的还是没标缓存的旧页面。"""

    def setUp(self):
        import glob
        self.blob = ""
        found = False
        for path in glob.glob(os.path.join(ROOT, "web", "assets", "*.js")):
            found = True
            with open(path, encoding="utf-8", errors="replace") as handle:
                self.blob += handle.read()
        if not found:
            self.skipTest("web/assets 不存在——先跑 `cd frontend && npm run build`")

    def test_the_cache_labels_reached_the_bundle(self):
        for text in ("缓存命中", "含缓存命中"):
            self.assertIn(text, self.blob, "%r 不在产物里，说明没重新构建" % text)


if __name__ == "__main__":
    unittest.main()