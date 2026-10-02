"""`dev_pricing`：联网检索单价并写入价格表。

**这个入口存在的理由**是三种"0"必须分开，它们在界面上原本长得一模一样：

| 情形 | 含义 | 该做什么 |
|---|---|---|
| 没配单价 | 不知道花了多少 | 去检索 |
| 模型真的免费 | 就是 0 元 | 写进去，让运行台明说"免费" |
| 检索失败 / 模型 id 拼错 | 不知道 | 重试或改 id |

都报 0 的话，用户永远分不清该干什么——而"分不清"正是本仓反复吃亏的地方。

要防的回归：
1. **不能把"没查到"报成"免费"**（守它：`test_a_missing_model_is_not_free`）；
2. **不能把"检索失败"报成"免费"**（`test_a_network_failure_is_not_free`）；
3. **apply 不能抹掉别人的条目**（`test_apply_keeps_other_entries`）——这是最容易造成
   静默损失的一处：新配一个模型却把同 provider 下已配好的其它模型清空；
4. **apply 之后要能读回来**（`test_apply_verifies_by_reading_back`）：只看"没抛异常"
   就说写好了，是这类工具最容易骗人的地方；
5. **阶梯定价要跟着一起写进去**（`test_tiers_survive_a_save_and_a_reload`）。

联网用例默认 skip，设 `DOCMIND_TEST_NETWORK=1` 才跑。
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
import tools  # noqa: E402

NETWORK = os.getenv("DOCMIND_TEST_NETWORK") == "1"


class PricingFileCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devpricing-")
        self._old = pricing.PRICING_FILE
        pricing.PRICING_FILE = os.path.join(self.tmp, "pricing.json")

    def tearDown(self):
        pricing.PRICING_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, data):
        with open(pricing.PRICING_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def read(self):
        with open(pricing.PRICING_FILE, encoding="utf-8") as handle:
            return json.load(handle)


class SaveModelPricingTests(PricingFileCase):

    ENTRY = {"in": 14.2, "out": 71.0, "cache_read": 0.71, "cache_creation": 17.75}

    def test_it_writes_and_reads_back(self):
        saved = pricing.save_model_pricing("custom", "a/b", dict(self.ENTRY))
        self.assertTrue(saved["ok"], saved["error"])
        self.assertEqual(saved["verified"]["in"], 14.2)
        self.assertEqual(pricing.price_detail("custom", "a/b"), (14.2, 71.0, 0.71, 17.75))

    def test_apply_keeps_other_entries(self):
        """最容易造成静默损失的一处：新配一个模型，把同 provider 下别人配好的清空。"""
        self.seed({"custom": {"models": {"other/model": {"in": 1.0, "out": 2.0}}}})
        pricing.save_model_pricing("custom", "new/model", dict(self.ENTRY))
        models = self.read()["custom"]["models"]
        self.assertIn("other/model", models)
        self.assertIn("new/model", models)
        self.assertEqual(models["other/model"], {"in": 1.0, "out": 2.0})

    def test_apply_keeps_other_providers(self):
        self.seed({"qwen": {"models": {"qwen-plus": {"in": 0.9, "out": 2.1}}}})
        pricing.save_model_pricing("custom", "a/b", dict(self.ENTRY))
        self.assertEqual(self.read()["qwen"]["models"]["qwen-plus"], {"in": 0.9, "out": 2.1})

    def test_apply_reports_the_previous_value(self):
        self.seed({"custom": {"models": {"a/b": {"in": 1.0, "out": 2.0}}}})
        saved = pricing.save_model_pricing("custom", "a/b", dict(self.ENTRY))
        self.assertEqual(saved["previous"], {"in": 1.0, "out": 2.0})

    def test_a_free_model_can_be_recorded(self):
        """免费模型也该写进去——写进去运行台就能明说"免费"，而不是显示一个
        含义不明的 0。"""
        saved = pricing.save_model_pricing(
            "custom", "free/model", {"in": 0.0, "out": 0.0})
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["verified"]["in"], 0.0)

    def test_tiers_survive_a_save_and_a_reload(self):
        entry = dict(self.ENTRY)
        entry["overrides"] = [{"min_prompt_tokens": 272000, "in": 28.4, "out": 106.5,
                               "cache_read": 1.42, "cache_creation": 35.5}]
        pricing.save_model_pricing("custom", "a/b", entry)
        # 重新读文件（模拟下一次进程启动）
        self.assertIn("overrides", self.read()["custom"]["models"]["a/b"])
        # 阶梯真的生效
        high = pricing.cost_cny("custom", "a/b", 300_000, 1_000)
        self.assertAlmostEqual(high, (300_000 * 28.4 + 1_000 * 106.5) / 1_000_000)
        low = pricing.cost_cny("custom", "a/b", 200_000, 1_000)
        self.assertAlmostEqual(low, (200_000 * 14.2 + 1_000 * 71.0) / 1_000_000)

    def test_missing_fields_are_refused(self):
        self.assertFalse(pricing.save_model_pricing("custom", "x", {"in": 1.0})["ok"])
        self.assertFalse(pricing.save_model_pricing("", "", dict(self.ENTRY))["ok"])

    def test_a_broken_existing_file_is_not_fatal(self):
        with open(pricing.PRICING_FILE, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        saved = pricing.save_model_pricing("custom", "a/b", dict(self.ENTRY))
        self.assertTrue(saved["ok"], saved["error"])


class DeepMergeTests(PricingFileCase):
    """`models` 必须逐模型深合并，不能整块替换。"""

    def test_builtin_defaults_survive_a_partial_override(self):
        """`_load_pricing` 里 models 用的是浅 update —— 新写一个模型就会把同 provider
        下内置的其它模型清空。这条钉住深合并。"""
        self.seed({"qwen": {"models": {"qwen-turbo-custom": {"in": 0.5, "out": 1.0}}}})
        table = pricing._load_pricing()
        self.assertIn("qwen-plus", table["qwen"]["models"],
                      "内置的 qwen-plus 不该被文件里的一条 models 冲掉")

    def test_builtin_top_level_prices_survive(self):
        self.seed({"qwen": {"models": {"x": {"in": 1.0, "out": 2.0}}}})
        table = pricing._load_pricing()
        self.assertEqual(table["qwen"]["in"], pricing.DEFAULT_PRICING["qwen"]["in"])


class LookupSemanticsTests(unittest.TestCase):
    """「免费」与「没查到」必须可区分。"""

    def test_an_empty_model_name_is_refused(self):
        result = pricing.openrouter_pricing("")
        self.assertIn("error", result)
        self.assertNotIn("free", result)

    def test_a_network_failure_is_not_free(self):
        import urllib.error
        import urllib.request as request

        saved = request.urlopen
        request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("offline"))
        try:
            result = pricing.openrouter_pricing("any/model", timeout=1)
        finally:
            request.urlopen = saved
        self.assertIn("error", result)
        self.assertNotIn("free", result,
                         "检索失败绝不能报成免费——那会让用户以为省了钱")

    @unittest.skipUnless(NETWORK, "需要联网；设 DOCMIND_TEST_NETWORK=1 才跑")
    def test_a_missing_model_is_not_free(self):
        result = pricing.openrouter_pricing("totally/made-up-model-xyz")
        self.assertIn("error", result)
        self.assertNotIn("free", result)


class ToolTests(unittest.TestCase):

    def test_it_is_registered_with_one_catalog_line(self):
        import agent
        self.assertIn("dev_pricing", tools.TOOLS)
        rows = [line for line in agent._SYSTEM_PROMPT_FULL.splitlines()
                if line.startswith("- dev_pricing")]
        self.assertEqual(len(rows), 1)

    def test_an_unknown_action_is_refused(self):
        out = tools.TOOLS["dev_pricing"]["func"]("action: teleport")
        self.assertIn("只认", out)

    def test_a_missing_model_says_so_rather_than_reporting_free(self):
        """没有模型名时必须说"没给"，不能顺着往下报一个免费结论。"""
        import config
        original = config.get_runtime("llm_model")
        try:
            config.set_runtime("llm_model", "")
            out = tools.TOOLS["dev_pricing"]["func"]("action: fetch")
        finally:
            config.set_runtime("llm_model", original)
        self.assertIn("没有可用的模型名", out)


if __name__ == "__main__":
    unittest.main()
