"""simulate_growth 深化回归：多模型 + 摘要统计 + 向后兼容。

原 B 档实现是纯等比数列玩具；现支持 geometric/linear/logistic/diminishing 四种
成长模型并返回摘要统计。本测试锁定：向后兼容（geometric 数值不变）、各模型行为、
边界（levels 截断 / 未知 model 兜底 / base<=0 修正）与端点形态。
"""
import unittest

import game_workbench as gw


class TestSimulateGrowth(unittest.TestCase):
    def test_geometric_backward_compat(self):
        r = gw.simulate_growth(3, 100, 1.08)
        self.assertTrue(r["ok"])
        self.assertEqual(r["model"], "geometric")
        self.assertEqual(
            [(v["level"], v["value"]) for v in r["values"]],
            [(1, 100.0), (2, 108.0), (3, 116.64)],
        )
        # 摘要：base=100，翻倍发生在 level 2（108 >= 200? 否；需到 v>=200）
        # 此处仅验证摘要键存在与 total 正确
        self.assertIn("summary", r)
        self.assertEqual(r["summary"]["total"], round(100 + 108 + 116.64, 4))
        self.assertEqual(r["summary"]["count"], 3)

    def test_geometric_doubling_level(self):
        r = gw.simulate_growth(20, 100, 1.5)  # 100,150,225... 翻倍(>=200)在 level 3
        self.assertEqual(r["summary"]["doubling_level"], 3)

    def test_linear(self):
        r = gw.simulate_growth(3, 100, 2.0, model="linear")
        self.assertEqual(r["model"], "linear")
        vals = [v["value"] for v in r["values"]]
        self.assertEqual(vals, [100.0, 200.0, 300.0])
        self.assertEqual(r["summary"]["peak_increment"], 100.0)
        self.assertEqual(r["summary"]["doubling_level"], 2)

    def test_logistic_is_s_curve_bounded(self):
        r = gw.simulate_growth(50, 100, 1.08, model="logistic")
        self.assertEqual(r["model"], "logistic")
        vals = [v["value"] for v in r["values"]]
        # 单调增
        self.assertEqual(vals, sorted(vals))
        # 承载上限 K=base*20=2000，终值应显著低于 K
        self.assertLess(vals[-1], 2000)
        self.assertGreater(vals[-1], vals[0])
        # 拐点等级应给出且在合理区间
        self.assertIn("inflection_level", r["summary"])
        self.assertGreater(r["summary"]["inflection_level"], 1)

    def test_diminishing_is_concave(self):
        r = gw.simulate_growth(10, 100, 1.08, model="diminishing")
        self.assertEqual(r["model"], "diminishing")
        incs = [r["values"][i]["value"] - r["values"][i - 1]["value"] for i in range(1, 10)]
        # 凹函数：前期增量 > 后期增量
        self.assertGreater(incs[0], incs[-1])

    def test_unknown_model_falls_back_to_geometric(self):
        bogus = gw.simulate_growth(4, 100, 1.1, model="bogus_model")
        geo = gw.simulate_growth(4, 100, 1.1, model="geometric")
        self.assertEqual(
            [v["value"] for v in bogus["values"]],
            [v["value"] for v in geo["values"]],
        )

    def test_levels_clamp(self):
        self.assertEqual(len(gw.simulate_growth(0, 100, 1.1)["values"]), 1)
        self.assertEqual(len(gw.simulate_growth(99999, 100, 1.1)["values"]), 1000)

    def test_base_positive_fix(self):
        r = gw.simulate_growth(3, 0, 1.08)
        self.assertGreater(r["values"][0]["value"], 0)

    def test_summary_total_matches_sum(self):
        r = gw.simulate_growth(7, 50, 1.2, model="geometric")
        self.assertEqual(r["summary"]["total"], round(sum(v["value"] for v in r["values"]), 4))

    def test_endpoint_shape(self):
        # 端点直接返回函数 dict（含 values/summary），不二次包裹
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        resp = client.get("/api/simulate_growth", params={"levels": 5, "model": "logistic", "growth": 1.1})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "logistic")
        self.assertEqual(len(body["values"]), 5)
        self.assertIn("summary", body)
        self.assertIn("inflection_level", body["summary"])


if __name__ == "__main__":
    unittest.main()
