"""回归锁：中间证据不得冒充最终答案。

实据（longrun 工作台，35 步 / 16.5s）：模型读完 agent.py 后跑了一段 python 统计
`continue`，最终答案却是 `代码执行结果：\n27 [5860, 5902, ...]`——用户等了半天，
拿到的是一串行号。根因有三层，本锁逐层钉住：

1. `_should_verbatim_passthrough` 曾把「计数 + 行号清单」当成产出即答案；
2. 模型自己写出综合结论后，被上一步 verbatim 工具的原始输出**无条件覆盖**；
3. 即使两条捷径都漏过，终态也没有任何硬门拦「数字清单 = 答案」。

覆盖：检测器单元判据 + 端到端（拒透传 → 逼综合 → 仍裸清单则 degraded）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import (  # noqa: E402
    TOOLS,
    _asks_to_continue,
    _is_answer_continuation_fragment,
    _is_bare_evidence_answer,
    _is_bare_evidence_dump,
    _is_observation_echo,
    _is_source_like_dump,
    _is_thin_final,
    _should_verbatim_passthrough,
)

BARE_LIST = "27 [5860, 5902, 5930, 6011, 6088]"
BARE_NUM_LINES = "5860\n5902\n5930"
SCALAR = "42"
CALC_PROSE = "caseA dps=25.0\n到0需要 3.2 秒\n结论: 会掉到0"
SYNTHESIZED = (
    "agent.py 的 run 主循环里一共有 27 处 continue：它们分布在参数校验、"
    "工具批次派发、格式纠偏、证据核验等分支下，只有对应条件成立时才会执行到。"
)
_NOT_SYNTHESIZED = "数字/行号清单"   # 只有本守卫的 nudge 文案会含这个词
_NOTE = "未能综合成对问题的回答"      # 只有本守卫的 degraded 交付会追加这句

# 真实 DeepSeek 工作台漏网的形状：总行数 + 五六行 while/def，**没有行号前缀**，
# 行数不到 10，旧判据 `_is_raw_source_echo` / `_is_bare_evidence_dump` 全部漏判，
# 于是被 verbatim 捷径当成最终答案直接下发（257 字符）。
MIXED_CODE_DUMP = (
    "总行数 8298\n"
    "1274     while True:\n"
    "3759         while True:\n"
    "5202     def run(self, question, stream=True, images=None, deadline=None, *,\n"
    "5360     def _run_shell(self, question, stream=True, images=None, "
    "deadline=None, cancel_event=None):\n"
    "5876         while True:"
)
# 看起来像“文本报表”的中间观察：不含源码行、也不只是数字，但超过自动交付的
# 长度上限（240），必须交回模型综合；模型若原样重贴，则由终态门 C 拦下。
ECHO_OBS = (
    "文件 agent.py 共 8298 行，主循环从第 5857 行开始，包含多处分支与重试路径。\n"
    "其中参数校验位于 5922 行附近，工具批次派发位于 6165 行附近。\n"
    "格式纠偏逻辑集中在 6312 行到 6510 行之间，失败重试由预算控制。\n"
    "证据核验与终态守卫位于 6686 行之后，负责拦截未综合的中间输出。\n"
    "整体结构呈现为单主循环加多段条件跳转，修改时需要注意各分支的退出条件。\n"
    "此外，多个辅助函数与缓存层穿插其中，读代码时应先看主循环再看被调函数。\n"
    "如果只统计行号而不解释分支语义，得到的只是一份位置清单，不能算作结论。"
)
_ECHO_REFLECTION = "原样重贴工具输出"
_ECHO_NOTE = "工具原始输出"

# 真实工作台截头答案（435 字符）：开头是半句话 + 第 28 条，前半截被原生通道的
# final 覆盖丢了。守卫只认**开头**：正常答案里的行号引用、行内代码、从 1 开始的
# 列表都不能命中，否则会把正常回答也打回重写。
RUN1_FRAGMENT = (
    '` 或补 `Action:`"的 Nudge。\n'
    '28. `agent.py:6597`、`agent.py:6608` —— 位于 6580–6619 前置拦截区内，同属'
    '"拦截后回填 Observation 再重来"这一类。\n'
    '29. `agent.py:6608` 的另一处分支。\n\n'
    '## 三、核实边界\n\n'
    '- 除上述两处外，其余 27 处均有原文行号支撑。\n'
    '（核验：PASS · 6 处引用可核验）'
)
_CONT_REFLECTION = "从中间开始"     # 只有截头守卫的 reflection
_CONT_NUDGE = "前半部分已经丢失"     # 只有截头守卫的纠偏文案
_CONT_NOTE = "前半部分缺失"          # 只有截头守卫的 degraded 交付


# ---------------------------------------------------------------------------
# 判据：只认「只有数字/行号」的形态，短标量与散文永不命中
# ---------------------------------------------------------------------------
class BareEvidenceDumpDetectorTests(unittest.TestCase):
    def test_count_plus_line_list_is_evidence(self):
        self.assertTrue(_is_bare_evidence_dump(BARE_LIST),
                        "计数 + 行号清单必须被认成中间证据")

    def test_harness_prefix_is_stripped_before_check(self):
        self.assertTrue(_is_bare_evidence_dump("代码执行结果：\n" + BARE_LIST),
                        "verbatim 包装前缀不能让它漏判")

    def test_multiline_numeric_only_is_evidence(self):
        self.assertTrue(_is_bare_evidence_dump(BARE_NUM_LINES))

    def test_short_scalar_is_not_evidence(self):
        self.assertFalse(_is_bare_evidence_dump(SCALAR),
                         "标量结果就是答案，不能拦")
        self.assertFalse(_is_bare_evidence_dump("3.2"))

    def test_prose_numeric_conclusion_is_not_evidence(self):
        self.assertFalse(_is_bare_evidence_dump(CALC_PROSE),
                         "带中文结论的数字计算不能被误判成清单")

    def test_three_item_list_is_not_flagged(self):
        self.assertFalse(_is_bare_evidence_dump("[1, 2, 3]"),
                         "判据要保守：短列表可能本来就是答案")

    def test_empty_is_not_evidence(self):
        self.assertFalse(_is_bare_evidence_dump(""))
        self.assertFalse(_is_bare_evidence_dump(None))


class BareEvidenceAnswerTests(unittest.TestCase):
    """终态判据比工具观察判据更保守：解释里可以引用行号，不能把引用当清单。"""

    def test_pure_dump_is_gated(self):
        self.assertTrue(_is_bare_evidence_answer(BARE_LIST))
        self.assertTrue(_is_bare_evidence_answer("代码执行结果：\n" + BARE_LIST),
                        "verbatim 包装后的纯清单仍必须命中")

    def test_multiline_numeric_dump_is_gated(self):
        self.assertTrue(_is_bare_evidence_answer(BARE_NUM_LINES))

    def test_prose_quoting_a_line_list_is_not_gated(self):
        text = SYNTHESIZED + "具体行号如 [5860, 5902, 5964, 6011]。"
        self.assertFalse(_is_bare_evidence_answer(text),
                         "成段解释里引用一串行号不能被当成裸清单，否则会误判 degraded")

    def test_short_scalar_is_not_gated(self):
        self.assertFalse(_is_bare_evidence_answer(SCALAR))


class SourceLikeDumpTests(unittest.TestCase):
    def test_short_unnumbered_source_dump_is_flagged(self):
        self.assertTrue(_is_source_like_dump(MIXED_CODE_DUMP),
                        "总行数 + 几行 while/def（无行号、不足 10 行）必须被认成源码 dump")

    def test_computed_prose_is_not_source(self):
        self.assertFalse(_is_source_like_dump(CALC_PROSE),
                         "数字计算摘要不能被误判成源码")

    def test_indented_chinese_prose_is_not_source(self):
        self.assertFalse(_is_source_like_dump(
            "  结论: 这段逻辑没有问题。\n  原因: 分支都覆盖到了。\n"
            "  建议: 保持现状即可。\n  备注: 后续可补测试。"),
            "缩进的散文（ASCII 冒号）不能被当成代码行")

    def test_long_text_report_is_not_source(self):
        self.assertFalse(_is_source_like_dump(ECHO_OBS))


class ObservationEchoTests(unittest.TestCase):
    def test_exact_repost_is_echo(self):
        self.assertTrue(_is_observation_echo(ECHO_OBS, ECHO_OBS))

    def test_one_line_intro_plus_repost_is_still_echo(self):
        self.assertTrue(_is_observation_echo("结果如下：\n" + ECHO_OBS, ECHO_OBS),
                        "只加一句“结果如下”不算综合")

    def test_real_synthesis_is_not_echo(self):
        final = (
            "agent.py 的主循环是一条单循环 + 多段条件跳转的结构。"
            "参数校验、批次派发、格式纠偏和证据核验各自有独立退出条件，"
            "因此改动任一分支都要同时检查后续重试预算与终态守卫。\n" + ECHO_OBS)
        self.assertFalse(_is_observation_echo(final, ECHO_OBS),
                         "成段综合结论不能被当成原样重贴")

    def test_short_scalar_echo_is_not_gated(self):
        self.assertFalse(_is_observation_echo(SCALAR, SCALAR))


# ---------------------------------------------------------------------------
# 判据：截头回答只认「开头」，正常答案里的引用/行内代码不能命中
# ---------------------------------------------------------------------------
class AnswerContinuationFragmentTests(unittest.TestCase):
    def test_run1_truncated_answer_is_fragment(self):
        self.assertTrue(_is_answer_continuation_fragment(RUN1_FRAGMENT),
                        "从半句话 + 28. 开始的真实截头答案必须命中")

    def test_numbered_start_at_28_is_fragment(self):
        self.assertTrue(_is_answer_continuation_fragment("28. 前置拦截区\n29. 后续分支"))

    def test_leads_with_punctuation_is_fragment(self):
        self.assertTrue(_is_answer_continuation_fragment("，主循环还有 27 处 continue。"))

    def test_normal_answer_from_item_one_is_not_fragment(self):
        self.assertFalse(_is_answer_continuation_fragment("1. 参数校验\n2. 批次派发"),
                         "从 1 开始的列表是正常答案")

    def test_chinese_heading_one_is_not_fragment(self):
        self.assertFalse(_is_answer_continuation_fragment(
            "## 一、结论\n\n主循环共 27 处 continue。"))

    def test_inline_code_start_is_not_fragment(self):
        self.assertFalse(_is_answer_continuation_fragment(
            "`agent.py` 的主循环分三层，入口在 5446 行。"),
            "以行内代码开头的正常句子不能被当成截头")

    def test_explicit_continuation_question_is_recognised(self):
        self.assertTrue(_asks_to_continue("从第 28 条继续"))
        self.assertTrue(_asks_to_continue("接着写"))
        self.assertFalse(_asks_to_continue("这套代码主循环整体是怎么运作的"))


class ShouldVerbatimPassthroughTests(unittest.TestCase):
    def test_python_exec_bare_list_is_blocked(self):
        self.assertFalse(_should_verbatim_passthrough("python_exec", BARE_LIST),
                         "行号清单不是产出即答案，必须交回模型综合")

    def test_python_exec_short_source_dump_is_blocked(self):
        self.assertFalse(_should_verbatim_passthrough("python_exec", MIXED_CODE_DUMP),
                         "无行号的短源码 dump 不能走 verbatim 捷径")

    def test_python_exec_long_output_is_blocked(self):
        self.assertGreater(len(ECHO_OBS), 240)
        self.assertFalse(_should_verbatim_passthrough("python_exec", ECHO_OBS),
                         "超过 240 字符的输出一律回模型综合，不能自动下发")

    def test_python_exec_scalar_still_passes(self):
        self.assertTrue(_should_verbatim_passthrough("python_exec", SCALAR),
                        "真正的标量结果仍应走 verbatim 捷径")

    def test_python_exec_short_computation_still_passes(self):
        self.assertTrue(_should_verbatim_passthrough("python_exec", CALC_PROSE),
                        "短的三行计算摘要仍应走 verbatim 捷径")

    def test_calculate_is_unaffected(self):
        self.assertTrue(_should_verbatim_passthrough("calculate", BARE_LIST))


class ThinFinalTests(unittest.TestCase):
    def test_bare_list_is_thin(self):
        self.assertTrue(_is_thin_final(BARE_LIST))

    def test_real_explanation_is_not_thin(self):
        self.assertFalse(_is_thin_final(SYNTHESIZED),
                         "模型写出的实质解释绝不能被当成薄 final 覆盖")


# ---------------------------------------------------------------------------
# 端到端：走真实 ReAct 循环（python_exec 用桩替换，不跑子进程）
# ---------------------------------------------------------------------------
class _ScriptedFakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def chat(self, messages, stream=True, **kw):
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        if stream:
            def gen():
                for ch in text:
                    yield ch
            return gen()
        return text

    def count_tokens(self, text):
        return max(0, len(text) // 3)


def _registry_with_python_exec(output):
    """把 python_exec 换成固定返回的桩；其余工具保持真实注册表。"""
    registry = dict(TOOLS)
    base = dict(registry["python_exec"])
    base["func"] = lambda code: output
    registry["python_exec"] = base
    return registry


class EvidenceDumpAnswerGateTests(unittest.TestCase):
    def _run(self, scripts, output, question="这套代码主循环整体是怎么运作的，帮我理一理"):
        llm = _ScriptedFakeLLM(scripts)
        agent = agent_mod.Agent(
            llm=llm, tool_registry=_registry_with_python_exec(output))
        events = list(agent.run(question))
        return llm, events

    @staticmethod
    def _guard_continuation(events):
        return [e for e in events
                if e.get("type") == "reflection"
                and _CONT_REFLECTION in str(e.get("text", ""))]

    @staticmethod
    def _guard_reflections(events):
        return [e for e in events
                if e.get("type") == "reflection"
                and _NOT_SYNTHESIZED in str(e.get("text", ""))]

    def test_bare_list_after_tool_is_not_delivered_directly(self):
        # 第一步跑 python 得到行号清单；第二步模型只把清单当 Final → 必须被逼综合，
        # 第三步给出真结论 → 正常交付，且不能出现「代码执行结果」的 verbatim 包装。
        scripts = [
            "Thought: 先统计一下\nAction: python_exec\nAction Input: print(1)",
            "Final Answer:\n" + BARE_LIST,
            "Final Answer:\n" + SYNTHESIZED,
        ]
        _llm, events = self._run(scripts, BARE_LIST)
        self.assertEqual(len(self._guard_reflections(events)), 1,
                         "裸清单第一次出现时应恰好触发一次综合纠偏")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertFalse(finals[0].get("degraded"),
                         "模型随后给出了综合结论，不该标 degraded")
        self.assertIn("27 处 continue", finals[0]["text"])
        self.assertNotIn("代码执行结果", finals[0]["text"],
                         "行号清单被 verbatim 直接下发了")

    def test_bare_list_after_later_non_verbatim_tool_still_blocked(self):
        # python_exec 之后又调了 list_dir：last_action 已不是 python_exec。
        # 若只看 last_action，这串行号会漏拦；ran_python_exec 必须兜住。
        scripts = [
            "Thought: 先统计一下\nAction: python_exec\nAction Input: print(1)",
            "Thought: 再看一眼目录\nAction: list_dir\nAction Input: .",
            "Final Answer:\n" + BARE_LIST,
            "Final Answer:\n" + SYNTHESIZED,
        ]
        _llm, events = self._run(scripts, BARE_LIST)
        self.assertEqual(len(self._guard_reflections(events)), 1,
                         "final 前面的最后一步不是 verbatim 工具时，仍须拦住数字清单")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertFalse(finals[0].get("degraded"))
        self.assertIn("27 处 continue", finals[0]["text"])

    def test_mixed_source_dump_is_not_delivered_directly(self):
        # 真实 DeepSeek 漏网形状：python_exec 打印“总行数 + 几行 while/def”，
        # 无行号、不足 10 行。修前被 verbatim 捷径直接当 final 下发；
        # 修后必须回到模型，由模型写出综合结论。
        scripts = [
            "Thought: 先统计一下 continue 的位置\nAction: python_exec\nAction Input: print(1)",
            "Final Answer:\n" + SYNTHESIZED,
        ]
        llm, events = self._run(scripts, MIXED_CODE_DUMP)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertFalse(finals[0].get("degraded"))
        self.assertIn("27 处 continue", finals[0]["text"])
        self.assertNotIn("代码执行结果", finals[0]["text"],
                         "混合源码摘要被 verbatim 直接下发了")
        self.assertEqual(llm.calls, 2, "拦住捷径后应让模型再综合一轮")

    def test_model_reposting_long_observation_is_degraded(self):
        # 观察超过自动交付上限（240 字符）→ 回模型；模型若原样重贴，
        # 终态门 C 先逼综合一次，仍重贴则如实标 degraded。
        scripts = [
            "Thought: 先读一下结构\nAction: python_exec\nAction Input: print(1)",
            "Final Answer:\n" + ECHO_OBS,
        ]
        _llm, events = self._run(scripts, ECHO_OBS)
        echo_refl = [e for e in events
                     if e.get("type") == "reflection"
                     and _ECHO_REFLECTION in str(e.get("text", ""))]
        self.assertEqual(len(echo_refl), 1, "原样重贴应恰好纠偏一次")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].get("degraded"),
                        "仍原样重贴时必须标 degraded，不能假完成")
        self.assertIn(_ECHO_NOTE, finals[0]["text"])

    def test_still_bare_after_nudge_is_degraded_with_note(self):
        # 纠偏后仍只给清单 → 如实标 degraded 并注明未综合（fail-closed）。
        scripts = [
            "Thought: 先统计一下\nAction: python_exec\nAction Input: print(1)",
            "Final Answer:\n" + BARE_LIST,
        ]
        _llm, events = self._run(scripts, BARE_LIST)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].get("degraded"),
                        "仍只有行号清单时必须标 degraded，不能假完成")
        self.assertIn(_NOTE, finals[0]["text"])
        self.assertLessEqual(len(self._guard_reflections(events)), 1,
                             "纠偏必须有界，不能死循环")

    def test_scalar_result_still_passes_through_immediately(self):
        # 反向锁：真正的标量结果仍走 verbatim 捷径，一步交付、不标 degraded。
        llm, events = self._run(
            ["Thought: 算一下\nAction: python_exec\nAction Input: print(42)"],
            SCALAR)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("代码执行结果：\n42", finals[0]["text"])
        self.assertFalse(finals[0].get("degraded"))
        self.assertEqual(llm.calls, 1, "标量结果不该再多跑一轮模型")

    # ---------------------------------------------------------------- 截头 final
    def test_front_truncated_answer_is_rewritten(self):
        # 实据：435 字符的 final 从半句话 + 28. 开始。先要求它从头重写一份完整答案。
        scripts = [
            "Final Answer:\n" + RUN1_FRAGMENT,
            "Final Answer:\n" + SYNTHESIZED,
        ]
        llm, events = self._run(scripts, "x")
        self.assertEqual(len(self._guard_continuation(events)), 1,
                         "截头答案应恰好触发一次重写要求：%r" % [e.get("text") for e in events])
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertFalse(finals[0].get("degraded"),
                         "模型随后给出了完整答案，不该标 degraded")
        self.assertIn("27 处 continue", finals[0]["text"])
        self.assertEqual(llm.calls, 2, "截头后应再让模型重写一轮")

    def test_still_truncated_after_nudge_is_degraded_with_note(self):
        # 纠偏后仍从中间开始 → 如实标 degraded 并注明前半部分缺失（fail-closed）。
        _llm, events = self._run(["Final Answer:\n" + RUN1_FRAGMENT], "x")
        self.assertEqual(len(self._guard_continuation(events)), 1)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].get("degraded"),
                        "仍截头时不能假装完整交付")
        self.assertIn(_CONT_NOTE, finals[0]["text"])

    def test_explicit_continue_question_allows_midway_answer(self):
        # 用户明确说「从第 28 条继续」时，从中间开始正是预期行为，守卫必须让路。
        _llm, events = self._run(
            ["Final Answer:\n" + RUN1_FRAGMENT], "x", question="从第 28 条继续")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertFalse(finals[0].get("degraded"))
        self.assertFalse(self._guard_continuation(events))

    # 下面两条必须走**原生并行批次**：顺序 ReAct 里 verbatim 工具执行成功就立即
    # 收尾（6752），根本到不了「模型写 final 后是否被覆盖」那条分支；而批里的
    # gen_video_prompt 不经过立即收尾，正好覆盖 6899 的覆盖决策。
    def _run_native_batch(self, final_prose):
        import json

        class _NativeLLM:
            provider = "deepseek"
            model = "deepseek-chat"

            def __init__(self):
                self.calls = 0
                self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
                self.last_tool_calls = []
                self.last_finish_reason = None

            def clone(self):
                return self

            def count_tokens(self, text):
                return 1

            def chat(self, messages, stream=True, **kw):
                self.calls += 1
                if self.calls == 1:
                    self.last_tool_calls = [
                        {"name": "list_dir",
                         "arguments": json.dumps({"path": "."}, ensure_ascii=False)},
                        {"name": "gen_video_prompt",
                         "arguments": json.dumps({"input": "橘猫 / 窗台"}, ensure_ascii=False)},
                    ]
                    prose = "我先并行看一下目录并生成提示词。"
                else:
                    self.last_tool_calls = []
                    prose = final_prose
                self.last_finish_reason = None
                return iter([prose]) if stream else prose

        registry = dict(TOOLS)
        base = dict(registry["gen_video_prompt"])
        base["func"] = lambda arg: "一只橘猫坐在窗台上，阳光斜照，电影感镜头。"
        registry["gen_video_prompt"] = base
        llm = _NativeLLM()
        agent = agent_mod.Agent(llm=llm, tool_mode="native", tool_registry=registry)
        events = list(agent.run("这套代码主循环整体是怎么运作的，帮我理一理"))
        return events

    def test_substantive_model_final_is_not_overwritten_by_tool_output(self):
        # 上一步是 verbatim 工具（gen_video_prompt），模型已写出实质解释 → 保留解释。
        # 修前会被无条件换成工具原文，把模型的组织与说明整个丢掉。
        events = self._run_native_batch(
            "根据你的描述，我把画面扩写成了镜头提示词，并补充了光线与景别。")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("镜头提示词", finals[0]["text"],
                      "模型的综合结论被工具原始输出覆盖了")

    def test_thin_model_final_still_gets_exact_tool_result(self):
        # 反向锁：模型 final 只是薄复述时，仍用工具交付物兜底。
        events = self._run_native_batch("好的。")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("一只橘猫坐在窗台上", finals[0]["text"],
                      "薄 final 应由工具交付物兜底")


if __name__ == "__main__":
    unittest.main()
