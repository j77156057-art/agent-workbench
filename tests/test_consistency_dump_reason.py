"""覆盖率徽章「假阴」修复的回归锁（BUG-7 相关，P1）。

背景：`api._compute_consistency` 用「observation 关键词在 final 里的字面重合率」判
"覆盖存疑"。逐行裸铺源码把观察里的行号/路径原样抄进答案，重合率≈100% → 判 ok →
钻测里最差的「非答案」反而不弹徽章。修法不是去污染命中率（那会显示"覆盖率仅100%"
这种荒唐话），而是补一条**正交信号** `_consistency_reason`：认出裸铺源码就打
`'verbatim_dump'`，前端据此显示一句语义正确的提示。

顺带给这个**此前全无测试**的 `_compute_consistency` 补第一批单测，把命中率语义钉住，
确保正交改动没回退它的既有行为。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import api as api_mod  # noqa: E402

# 一屏带行号的裸铺清单：逐行 `N:\t<代码>`，且**原样含有观察里的标识符**
# （get_node / take_damage / contact_damage）→ 关键词命中率≈1 → 判 ok，
# 但内容仍是逐行源码（_is_raw_source_echo=True）。这正是钻测的"徽章假阴"。
_OBS = ["contact_damage take_damage get_node 这三者在 12:45 出现"]
_DUMP = "\n".join(
    "%d:\tcontact_damage = take_damage(get_node(\"n%d\"))" % (i, i)
    for i in range(1, 21)
)


class ConsistencyReasonTests(unittest.TestCase):
    def test_verbatim_dump_is_flagged_even_though_coverage_is_perfect(self):
        # 这正是钻测的假阴：命中率 ok，但内容是裸铺源码 → reason 必须给 verbatim_dump。
        c, ratio = api_mod._compute_consistency(_OBS, _DUMP)
        self.assertEqual(c, "ok", "裸铺抄全了关键词，命中率判 ok（前提，别改这条语义）")
        self.assertGreater(ratio, 0.5)
        self.assertEqual(api_mod._consistency_reason(_OBS, _DUMP), "verbatim_dump")

    def test_synthesized_conclusion_has_no_reason(self):
        got = api_mod._consistency_reason(
            _OBS, "结论：take_damage 只在 player.gd:12 被调用，CONTACT_DAMAGE 是伤害常量。")
        self.assertIsNone(got, "正常综合结论不该被标成裸铺 dump")

    def test_empty_or_error_final_has_no_reason(self):
        self.assertIsNone(api_mod._consistency_reason([], ""))
        self.assertIsNone(api_mod._consistency_reason(_OBS, None))

    def test_never_raises_on_weird_input(self):
        # 永静默：诊断层绝不因脏输入打断终态 SSE。
        self.assertIn(api_mod._consistency_reason(["x"], "短答案"), (None, "verbatim_dump"))


class ComputeConsistencyLockTests(unittest.TestCase):
    """`_compute_consistency` 此前**零测试**——补第一批，钉住命中率语义不被正交改动带偏。"""

    def test_few_keywords_is_forced_ok(self):
        self.assertEqual(api_mod._compute_consistency([], "随便一句话"), ("ok", 1.0))
        # 关键词数 < 3 也按"无观察可核验"放行，不误报。
        self.assertEqual(api_mod._compute_consistency(["就一个 zzzzzzzz 词"], "没有别的")[0], "ok")

    def test_missing_keywords_drop_to_warn(self):
        obs = ["alpha_context beta_context gamma_context"]  # 抽出 3 个 ≥6 字标识符
        final = "完全没提到这些词的摘要"
        c, ratio = api_mod._compute_consistency(obs, final)
        self.assertEqual(c, "warn", "关键词都没进摘要→覆盖存疑")
        self.assertLess(ratio, 0.5)

    def test_covered_keywords_are_ok(self):
        obs = ["alpha_context beta_context gamma_context"]
        final = "alpha_context 与 beta_context 及 gamma_context 都在结论里体现了。"
        c, _ = api_mod._compute_consistency(obs, final)
        self.assertEqual(c, "ok")


if __name__ == "__main__":
    unittest.main()
