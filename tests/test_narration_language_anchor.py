# -*- coding: utf-8 -*-
"""BUG-5 回归：过程流「面向用户的进展说明」必须锚定用户提问的语言。

实测事故：用户用中文问「联机同步怎么对时」，过程流旁白整段漏英文
（"I'll look into the project's networking/sync code…"）；另一轮又全中文，
中英乱跳。根因是 `_NARRATION_CLAUSE` 只说「用一两句自然语言说明你在做什么」，
**没约束解说语言** —— 模型默认拿英文思考，旁白就跟着漂。

本文件锁两件事：
  · `_NARRATION_CLAUSE` 里确有「跟随用户提问语言 / 不要中英混杂」这条硬约束；
  · 开启 narration 时该条款确实被拼进最终系统提示（漏拼 = 那条路径上语言没锚）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402


class NarrationLanguageAnchorTests(unittest.TestCase):
    def test_clause_requires_user_language(self):
        clause = agent_mod._NARRATION_CLAUSE
        # 必须显式要求「跟随用户提问的语言」并禁止中英混杂。
        self.assertIn("跟随用户提问的语言", clause,
                      "narration 条款没锚定语言，过程流旁白会中英乱跳（BUG-5）")
        self.assertIn("不要中英混杂", clause,
                      "缺『不要中英混杂』——半中半英正是实测泄漏的样子")

    def test_clause_keeps_code_identifiers_verbatim(self):
        # 语言锚定不能顺手禁掉必要的英文标识符（命令/路径/函数名）。
        clause = agent_mod._NARRATION_CLAUSE
        self.assertIn("保持原文", clause,
                      "应保留命令/路径/代码标识符原文，否则连 grep 都要翻译")

    def test_clause_is_appended_when_narration_on(self):
        base = "你是一个严谨的多工具问答 Agent。"
        out = agent_mod._prompt_for_channel(base, native=False, narration=True)
        self.assertIn(agent_mod._NARRATION_CLAUSE.strip(), out.strip(),
                      "narration=True 却没拼上条款 → 那条路径上语言没锚")
        self.assertIn("跟随用户提问的语言", out)

    def test_clause_absent_when_narration_off(self):
        base = "你是一个严谨的多工具问答 Agent。"
        out = agent_mod._prompt_for_channel(base, native=False, narration=False)
        self.assertNotIn("跟随用户提问的语言", out,
                         "narration 关着却拼了条款——不该无中生有")

    def test_text_contract_anchors_thought_language(self):
        # BUG-5 根因：泄漏的是模型自己的 Thought/过程文字，narration 条款管不到它。
        # 文本协议契约必须把语言锚定从「回答」扩到「展示给用户看的思考/解说/旁白」。
        c = agent_mod._FORMAT_CONTRACT
        self.assertIn("过程解说", c, "文本契约只锚了回答，没锚过程旁白 → Thought 仍会漏英文")
        self.assertIn("不得中英混杂", c)

    def test_native_contract_anchors_thought_language(self):
        c = agent_mod._NATIVE_FORMAT_CONTRACT
        self.assertIn("过程解说", c, "原生契约只锚了回答，没锚推理/旁白 → 过程流仍会漏英文")
        self.assertIn("不得中英混杂", c)


class LanguageIronRuleAnchorTests(unittest.TestCase):
    """BUG-5 第二轮：埋在中段的语言条款压不住 DeepSeek 的原生 reasoning_content。

    实测复钻（2026-10-16，deepseek-flash，中文问「敌人是怎么发现并追击玩家的」）：
    开头旁白是中文，但思维链中途漂回英文 —— "Now I need to answer…" /
    "I have the actual file content. Let me also confirm…" / "Actually the rules say…"。
    这些走的是**独立于 narration 的 reasoning_content 通道**，中段条款够不着。
    修法：把「语言铁律」提到两份契约**最顶部**（模型对首段指令最敏感），
    并**逐字点名**这些英文失败模式，让模型认出并改写。
    """

    def test_both_contracts_lead_with_language_iron_rule(self):
        for name in ("_FORMAT_CONTRACT", "_NATIVE_FORMAT_CONTRACT"):
            c = getattr(agent_mod, name)
            self.assertIn("语言铁律", c, f"{name} 缺『语言铁律』前置块")
            # 必须显式覆盖 reasoning/思考链，而不只是「回答」。
            self.assertIn("推理链", c, f"{name} 语言铁律没点名 reasoning 思维链")
            self.assertTrue(c.lstrip().startswith("【语言铁律"),
                            f"{name} 语言铁律没放在最顶部（模型对首段最敏感）")

    def test_iron_rule_names_observed_english_patterns(self):
        # 逐字点名实测泄漏的英文推理开头，是这一轮的关键增强。
        for name in ("_FORMAT_CONTRACT", "_NATIVE_FORMAT_CONTRACT"):
            c = getattr(agent_mod, name)
            for bad in ("Now I need to", "Let me check"):
                self.assertIn(bad, c,
                              f"{name} 没点名失败模式 {bad!r} → 模型认不出自己在犯这个")

    def test_native_prompt_has_anchor_exactly_once(self):
        # 组装原生提示时，语言铁律只能出现一次（换契约时不能既留旧的又拼新的）。
        out = agent_mod._prompt_for_channel(
            agent_mod._SYSTEM_PROMPT_FULL, native=True, narration=True)
        self.assertEqual(out.count("语言铁律"), 1,
                         "语言铁律重复或漏拼 → 指令冲突/没锚定")


if __name__ == "__main__":
    unittest.main()
