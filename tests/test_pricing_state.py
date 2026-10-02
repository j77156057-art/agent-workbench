"""三种「都是 0」必须能被分开——这是本文件存在的全部理由。

用户看到的 `¥0.0000` 有三种截然不同的含义，要求他做的事也完全不同：

| 状态 | 含义 | 该做什么 |
|---|---|---|
| `free` | 官方标价就是 0，确认过 | 什么都不用做 |
| `local` | 本地模型，不打 API | 什么都不用做 |
| `unknown` | **没配置单价** | 去 `dev_pricing(action: apply)` 配 |
| `known` | 有单价且算出钱了 | 正常 |

不分开的后果正是实测到的：用户用 OpenRouter 的免费模型，看到 0 元，
无法判断这是省了钱还是系统根本没在算账——于是「费用不对」变成一个
查不出原因的问题。

**本文件里有两个我自己写出来又修掉的 bug，各带一条用例**：

1. `DEFAULT_PRICING["custom"]` 里放了 `in: 0.0` 当占位 → `_load_pricing` 会把它
   merge 进来 → `pricing_state` 的"是否配过价"判成 True → **收费模型漏配单价时
   不报警**。改成不带 in/out、只留 `unpriced: True` 标记。
2. 界面自己拿 `cost === 0` 反推"免费" → 等于把"没算"说成"免费"。
   所以前端必须读后端给的 `pricing_state`，状态缺失时退回纯金额、不硬猜。
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
        self.tmp = tempfile.mkdtemp(prefix="pstate-")
        self._old = pricing.PRICING_FILE
        pricing.PRICING_FILE = os.path.join(self.tmp, "pricing.json")

    def tearDown(self):
        pricing.PRICING_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, data):
        with open(pricing.PRICING_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


class FreeStateTests(Case):

    def test_a_marked_free_model_reads_as_free(self):
        self.seed({"custom": {"models": {"free/one": {
            "in": 0.0, "out": 0.0, "free": True}}}})
        info = pricing.pricing_state("custom", "free/one")
        self.assertEqual(info["state"], pricing.PRICING_STATE_FREE)
        self.assertTrue(info["is_free"])
        self.assertIn("免费", info["note"])

    def test_it_renders_with_a_reason(self):
        self.seed({"custom": {"models": {"free/one": {
            "in": 0.0, "out": 0.0, "free": True}}}})
        text = pricing.render_cost("custom", "free/one", 0.0)
        self.assertIn("免费", text)
        self.assertIn("¥0", text)

    def test_zero_prices_without_the_flag_are_not_claimed_as_free(self):
        """这是最关键的一条：光看数字是 0 不能推出"免费"。必须有显式标记。"""
        self.seed({"custom": {"models": {"maybe/zero": {"in": 0.0, "out": 0.0}}}})
        info = pricing.pricing_state("custom", "maybe/zero")
        self.assertNotEqual(info["state"], pricing.PRICING_STATE_FREE)

    def test_local_providers_are_their_own_state(self):
        info = pricing.pricing_state("ollama", "qwen3:4b")
        self.assertEqual(info["state"], pricing.PRICING_STATE_LOCAL)
        self.assertIn("本地", pricing.render_cost("ollama", "qwen3:4b", 0.0))


class UnknownStateTests(Case):
    """漏配单价是唯一**要求用户行动**的状态，必须能被认出来。"""

    def test_an_unconfigured_model_is_unknown(self):
        self.seed({"custom": {"models": {"other/paid": {"in": 1.0, "out": 2.0}}}})
        info = pricing.pricing_state("custom", "brand/new/paid-model")
        self.assertEqual(info["state"], pricing.PRICING_STATE_UNKNOWN)
        self.assertIn("未配置单价", pricing.render_cost("custom", "brand/new/paid-model", 0.0))

    def test_a_provider_placeholder_of_zero_must_not_count_as_configured(self):
        """我自己写出来的 bug：内置表里给 custom 放 `in: 0.0` 当占位，而
        `_load_pricing` 会 merge 进来 —— 于是"配过价"判成 True，收费模型漏配时
        **不报警**。占位必须改用 `unpriced: True` 标记，且该标记要压过 in/out。"""
        builtin = pricing.DEFAULT_PRICING["custom"]
        self.assertTrue(builtin.get("unpriced"),
                        "custom 的内置表必须标 unpriced")
        self.assertNotIn("in", builtin,
                         "custom 的内置表不能带 in 占位——那会被当成已配置单价")
        # 即使某个配置文件（或旧版本遗留）写了 in:0.0，unpriced 也必须赢
        self.seed({"custom": {"in": 0.0, "out": 0.0}})
        merged = pricing._load_pricing()["custom"]
        self.assertTrue(merged.get("unpriced"))
        self.assertFalse(("in" in merged or "out" in merged)
                         and not merged.get("unpriced"))

    def test_a_configured_pricing_file_beats_the_placeholder(self):
        """用户真写了单价之后，就该判 known（正常计价），不再报"未配置"。"""
        self.seed({"custom": {"models": {"real/paid": {"in": 12.0, "out": 34.0}}}})
        info = pricing.pricing_state("custom", "real/paid")
        self.assertEqual(info["state"], pricing.PRICING_STATE_KNOWN)
        self.assertEqual(pricing.render_cost("custom", "real/paid", 46.0), "¥46.0000")

    def test_an_unknown_provider_is_unknown(self):
        self.assertEqual(pricing.pricing_state("neverheardof", "x")["state"],
                         pricing.PRICING_STATE_UNKNOWN)


class RenderCostTests(Case):

    def test_a_nonzero_cost_gets_no_explanation(self):
        """有金额时再加「免费」就成了笑话。"""
        for state_setup in ({"custom": {"models": {"m": {"in": 0, "out": 0, "free": True}}}},
                            None):
            if state_setup:
                self.seed(state_setup)
            text = pricing.render_cost("custom", "m" if state_setup else "qwen", 1.5)
            self.assertEqual(text, "¥1.5000")
            self.assertNotIn("（", text)

    def test_the_note_says_what_to_do_about_it(self):
        """unknown 的说明必须给出下一步，光说"未知"等于没说。"""
        self.seed({"custom": {"models": {"x": {"in": 1.0, "out": 1.0}}}})
        info = pricing.pricing_state("custom", "not-configured")
        self.assertIn("dev_pricing", info["note"])


class TraceAggregationTests(unittest.TestCase):
    """按 provider 的统计要带上状态，否则设置页那张表还是只有一列 0。"""

    def _rows(self):
        return [{"provider": "custom", "model": "free/one", "prompt_tokens": 10,
                 "completion_tokens": 5, "cache_read_tokens": 0,
                 "cache_creation_tokens": 0, "cost_cny": 0.0, "aborted": False,
                 "error": None, "elapsed_ms": 10}]

    def test_each_provider_slot_carries_its_state(self):
        import agent_trace
        original = agent_trace.TRACE_FILE
        tmp = tempfile.mkdtemp(prefix="trace-")
        agent_trace.TRACE_FILE = os.path.join(tmp, "t.jsonl")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(setattr, agent_trace, "TRACE_FILE", original)
        with open(agent_trace.TRACE_FILE, "w", encoding="utf-8") as handle:
            for row in self._rows():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        agg = agent_trace.summary()
        slot = agg["by_provider"]["custom"]
        self.assertIn("pricing_state", slot,
                      "按 provider 的统计必须带状态，设置页靠它区分三种 0")
        self.assertEqual(slot["model"], "free/one")

    def test_a_failing_pricing_lookup_does_not_break_the_stats(self):
        """状态只是附注；取不到不能让整个统计挂掉（那是"为了附注把主功能弄坏"）。"""
        import agent_trace
        original = agent_trace.TRACE_FILE
        tmp = tempfile.mkdtemp(prefix="trace2-")
        agent_trace.TRACE_FILE = os.path.join(tmp, "t.jsonl")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(setattr, agent_trace, "TRACE_FILE", original)
        with open(agent_trace.TRACE_FILE, "w", encoding="utf-8") as handle:
            for row in self._rows():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        saved = pricing.pricing_state
        pricing.pricing_state = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            agg = agent_trace.summary()
        finally:
            pricing.pricing_state = saved
        self.assertEqual(agg["total_cost_cny"], 0.0)
        self.assertEqual(agg["by_provider"]["custom"]["pricing_state"], "")


class FrontendGuardTests(unittest.TestCase):
    """前端不许自己拿 `cost === 0` 反推"免费"——那正是原先的毛病。"""

    def _read(self, rel):
        with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as handle:
            return handle.read()

    def test_a_shared_cost_label_helper_exists(self):
        path = os.path.join("frontend", "src", "workbench", "composables", "costLabel.ts")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, path)), path)

    def test_the_helper_distinguishes_the_three_states(self):
        source = self._read("frontend/src/workbench/composables/costLabel.ts")
        for word in ("free", "local", "unknown"):
            self.assertIn(word, source, "costLabel 必须认识 %s 状态" % word)

    def test_the_settings_table_uses_the_helper(self):
        source = self._read("frontend/src/workbench/components/SettingsView.vue")
        self.assertIn("costLabel", source)
        self.assertIn("pricing_state", source)

    def test_the_trace_type_declares_the_state(self):
        source = self._read("frontend/src/workbench/api/harness.ts")
        self.assertIn("pricing_state", source)


if __name__ == "__main__":
    unittest.main()
