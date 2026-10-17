"""计数/统计类问题的稳妥路径引导（④ 之度量层补强 · 提示词层）。

背景：grep 40 条静默截断 + search_code 只回 top-K，模型拿被截断的观察肉眼加总，
把真值 196 的函数数报成"准确总数 194"。工具层截断已如实披露（grep/ingest/search_code
top-K），度量层内部矛盾自检已落地（output_audit._count_breakdown_conflict）。这里补的是
**预防性引导**：在系统提示里明确教模型——要交数字时先 dev_glob 定文件全集再逐文件精确数、
或用 python_exec 真跑统计，而非靠人眼相加；且必须交代口径、够不到全量禁用"准确总数"口吻。

这条测试只锁"引导存在且带对关键动作"，防它被静默删改；不断言逐字，措辞可微调。
"""

import unittest

import agent


class CountingGuidanceTests(unittest.TestCase):
    def _assert_guidance(self, prompt: str, where: str):
        # 命中"计数/统计类问题"这条子项
        self.assertIn("计数/统计类问题", prompt, "%s 缺少计数类问题引导" % where)
        # 点明截断陷阱的来源（grep 40 / search_code top-K），否则模型不知道为何别加总
        self.assertIn("肉眼加总", prompt, "%s 未点明'别肉眼加总'" % where)
        # 给出稳妥路径的两个抓手：dev_glob 定文件全集 / python_exec 真跑统计
        self.assertIn("dev_glob", prompt, "%s 未引导用 dev_glob 定文件全集" % where)
        self.assertIn("python_exec", prompt, "%s 未引导用 python_exec 聚合计数" % where)
        # 诚实口径要求：禁用"准确总数"包装未自核的数字
        self.assertIn("准确总数", prompt, "%s 未约束'准确总数'口吻" % where)

    def test_raw_system_prompt_has_counting_guidance(self):
        self._assert_guidance(agent._SYSTEM_PROMPT_FULL, "原始提示")

    def test_assembled_system_prompt_keeps_counting_guidance(self):
        # 装配（含 progressive 工具段分层）不得吞掉引导段的这条子项
        self._assert_guidance(agent.SYSTEM_PROMPT, "装配后提示")

    def test_counting_guidance_is_a_bare_subbullet(self):
        # 落在引导段，用 "  · " 兄弟样式，不是被当成工具块（防污染工具域一致性守卫）
        line = next((ln for ln in agent._SYSTEM_PROMPT_FULL.splitlines()
                     if "计数/统计类问题" in ln), "")
        self.assertTrue(line.startswith("  · "), "计数引导不是引导段的 '  · ' 子项：%r" % line[:24])


if __name__ == "__main__":
    unittest.main()
