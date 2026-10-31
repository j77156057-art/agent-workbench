# -*- coding: utf-8 -*-
"""L2/L3/L4 注入上限随模型窗口缩放：下限不许跌破，上限不许失控，宪法永远固定。

动机不是"大窗口要多塞记忆"，而是挡两头：
- 小窗口模型上，把比例当唯一依据会算出 230 字符这种连宪法都装不下的数（实测 8k 窗口
  → 预算 4608 tok → 5% 只有 230 tok）；
- 百万窗口上，同一比例会给出 35k tok 的记忆空间，而卡住注入的一直是**内容量**
  （实测 L2 用 32%、L4 用 0%）—— 放大预算只会让固化堆得更凶。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from agent_runtime.memory import constitution, injection


class _StubLLM:
    provider = "stub"
    model = "stub"

    def __init__(self, budget):
        self.prompt_budget = budget

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        return iter(["Final Answer: 好"])


class DiscretionaryBudgetTests(unittest.TestCase):
    def test_small_window_never_drops_below_today(self):
        """**反向守卫**：8k 本地模型上必须逐字等于今天的固定值。"""
        caps = injection.discretionary_budgets(4608)
        self.assertEqual(caps, dict(injection.DISCRETIONARY_FLOORS))

    def test_big_window_grows_but_stays_capped(self):
        mid = injection.discretionary_budgets(87307)
        self.assertGreater(mid["L2"], injection.DISCRETIONARY_FLOORS["L2"])
        self.assertLess(mid["L2"], injection.DISCRETIONARY_CAPS["L2"])
        # 相对比例不许随窗口漂移：L2 > L4 > L3
        self.assertGreater(mid["L2"], mid["L4"])
        self.assertGreater(mid["L4"], mid["L3"])
        floors = injection.discretionary_budgets(0)
        huge = injection.discretionary_budgets(702034)
        for key, cap in injection.DISCRETIONARY_CAPS.items():
            with self.subTest(key=key):
                self.assertLessEqual(huge[key], cap)
                self.assertGreaterEqual(huge[key], floors[key])
        self.assertLessEqual(sum(huge.values()), sum(injection.DISCRETIONARY_CAPS.values()))
        # 各层 cap 不是同一比例，所以 L3 先顶到帽；剩下的余量**不挪给别层**，
        # 于是有效总池小于 cap 合计 —— 这是刻意的：宁可少注入，也不随窗口失控。
        self.assertEqual(huge["L3"], injection.DISCRETIONARY_CAPS["L3"])
        self.assertGreater(sum(huge.values()), sum(floors.values()))

    def test_constitution_and_profile_are_not_scalable(self):
        """L0/L1 换个大模型不会变长，放进这张表就是设计错误。"""
        for key in ("L0", "L1", "rules", "summary"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    injection.layer_chars(key, 87307)
        self.assertEqual(constitution.INJECT_MAX_CHARS, 600)

    def test_unknown_budget_falls_back_to_floors(self):
        """估不出模型预算时按最小权限处理：退回下限，绝不放大。"""
        with patch.object(injection, "_current_prompt_tokens", return_value=0):
            self.assertEqual(injection.discretionary_budgets(),
                             dict(injection.DISCRETIONARY_FLOORS))


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_inj_")
        self._saved = {key: os.environ.get(key)
                       for key in ("DOCMIND_MEMORY_DIR", "DOCMIND_MEMORY_V2")}
        os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(self.tmp, "memory")
        config._RUNTIME.pop("memory_v2", None)
        config.set_runtime("memory_v2", True)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._RUNTIME.pop("memory_v2", None)

    def test_builders_honor_the_passed_ceiling(self):
        from agent_runtime.memory import hooks
        for index in range(12):
            hooks.add("prj-x", "s-x", "user_agreement", "约定 %d：%s" % (index, "很长" * 30))
        default = injection.build_session_injection("s-x", "prj-x")
        grown = injection.build_session_injection("s-x", "prj-x", max_chars=2400)
        self.assertTrue(default and grown)
        self.assertLessEqual(len(default[0]["text"]), 600)
        self.assertGreater(len(grown[0]["text"]), len(default[0]["text"]),
                           "传了更大的天花板却没注入更多内容 —— 旋钮没接到 builder")

    def test_agent_passes_its_own_prompt_budget(self):
        """接线本身也要钉住：Agent 必须把**自己那个模型**的预算传进来。
        漏传就静默退回下限，面板与真实注入还会各算各的，没人会发现。"""
        import agent as agent_mod
        seen = {}
        real = injection.layer_chars

        def spy(key, prompt_tokens):
            seen[key] = prompt_tokens
            return real(key, prompt_tokens)

        instance = agent_mod.Agent(llm=_StubLLM(200000), session_id="s-wire",
                                    project_id="prj-wire")
        with patch.object(injection, "layer_chars", side_effect=spy):
            instance._build_messages("随便问问")
        self.assertEqual(set(seen), {"L2", "L3", "L4"}, "三层里有人没接上缩放")
        self.assertEqual(set(seen.values()), {instance._prompt_budget()},
                         "用的不是本模型的 prompt 预算")

    def test_panel_reports_the_budget_it_actually_used(self):
        from agent_runtime.memory import context_panel
        with patch.object(injection, "_current_prompt_tokens", return_value=702034):
            view = context_panel.next_view("prj-panel", "s-panel")
            expected = injection.discretionary_budgets(702034)
        layers = {layer["key"]: layer for layer in view["layers"]}
        for key in ("L2", "L3", "L4"):
            self.assertEqual(layers[key]["budget"], expected[key],
                             "面板显示的预算和它真正用的不是一份")
            self.assertGreaterEqual(layers[key]["budget"],
                                    injection.DISCRETIONARY_FLOORS[key])
        self.assertEqual(layers["L0"]["budget"], constitution.INJECT_MAX_CHARS,
                         "宪法那层不许跟着窗口漂")


if __name__ == "__main__":
    unittest.main()
