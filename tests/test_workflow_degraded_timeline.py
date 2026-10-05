# -*- coding: utf-8 -*-
"""降级信号必须到达时间线：结论不是模型原文时，界面不能说「已完成」。

## 症状

用户截图里那条子代理的结论是：

    模型连续 1 次调用都没有返回可用正文。这多半不是你的问题写得不清楚，
    而是上游/中转返回了空内容……

而时间线上它显示的是「子代理已完成」。

## 断在哪（不是界面、也不是 agent）

`degraded` 这个标在 `agent.py` 里打得很足 —— 连续空响应那条兜底 `final` 带
`degraded: True`（`agent.py:4903`），`_run_child` 读成 `final_degraded`（:5222），
子代理结果里也确实有 `"degraded": degraded`（:5308）。

断在**最后一跳**：工作流事件的 payload 把它丢了。

    模型空返回 -> output {"status": "ok", "degraded": True, "conclusion": "<诊断文字>"}
      -> subagent_complete / task_complete 事件：**没带 degraded**
      -> 时间线标签只看 kind -> 「子代理已完成」

这与本仓库反复出现的「把没能答记成答了」是同一族：**信号在到达判据前被丢掉**。

## 反向守卫

- 后端两个事件 payload 都必须带 `degraded`
- 前端时间线标签必须**因它而变**（防「只加字段、界面照旧显示已完成」）
- 且**不得**把正常完成也标成降级（防「一律加个后缀糊弄过去」）
"""
import io
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GW = os.path.join(ROOT, "agent_runtime", "game_workflow.py")
VUE = os.path.join(ROOT, "frontend", "src", "workbench", "components", "AutonomousCockpit.vue")
TS = os.path.join(ROOT, "frontend", "src", "workbench", "api", "agent.ts")


def _src(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class BackendCarriesTheSignalTests(unittest.TestCase):
    """后端：两个完成事件的 payload 都必须带 `degraded`。"""

    def _event_body(self, kind):
        """取出 emit("<kind>", {...}) 那个字典字面量的源码。"""
        src = _src(GW)
        i = src.find('emit("%s", {' % kind)
        self.assertNotEqual(i, -1, "找不到 emit(\"%s\", {...})" % kind)
        depth, j = 0, src.find("{", i)
        k = j
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    return src[j:k + 1]
            k += 1
        self.fail("emit(\"%s\") 的字典没闭合" % kind)

    def test_subagent_complete_carries_degraded(self):
        body = self._event_body("subagent_complete")
        # 断言**带引号的键名**。第一版写的是 assertIn("degraded", body)，而变异把键
        # 改成 `"degraded_but_never"` —— 子串包含，0 红放过去了。键名要整个匹配。
        self.assertIn('"degraded"', body,
                      "subagent_complete 不带 degraded 键 —— 信号在这里又断一次")

    def test_task_complete_carries_degraded(self):
        body = self._event_body("task_complete")
        self.assertIn('"degraded"', body,
                      "task_complete 不带 degraded 键 —— 信号在这里又断一次")

    def test_the_flag_is_read_from_the_subagent_result(self):
        """它必须读子结果里那个字段，而不是写死。"""
        body = self._event_body("subagent_complete")
        self.assertRegex(body, r'"degraded":\s*bool\(\s*output\.get\("degraded"\)',
                          "degraded 必须来自 output.get(\"degraded\")，写死就等于没接")

    def test_task_complete_reads_it_from_the_result_too(self):
        body = self._event_body("task_complete")
        self.assertRegex(body, r'"degraded":\s*bool\(\(\s*output\s+or\s+\{\}\s*\)\.get\("degraded"\)',
                          "task_complete 的 degraded 也必须来自子结果，不能写死")

    def test_the_type_declares_the_field(self):
        self.assertIn("degraded?: boolean", _src(TS),
                      "WorkflowEvent 没声明 degraded —— 前端读不到它")


class TimelineShowsItTests(unittest.TestCase):
    """前端：时间线标签必须因degraded 而变，且**只**在它为真时变。"""

    def _helper(self):
        src = _src(VUE)
        i = src.find("function timelineLabelOf(")
        self.assertNotEqual(i, -1,
                            "找不到 timelineLabelOf() —— 时间线渲染处没改成读 payload")
        j = src.find("\nfunction ", i + 10)
        return src[i:j if j > 0 else i + 700]

    def test_the_timeline_row_uses_the_degrade_aware_label(self):
        src = _src(VUE)
        row = [ln for ln in src.splitlines()
               if "acp-timeline-item" in ln or "<summary>" in ln]
        self.assertTrue(row, "找不到时间线行的模板")
        nxt = src.split("acp-timeline-item", 1)[1].splitlines()
        summary_line = next((ln for ln in nxt if "<summary>" in ln), "")
        self.assertIn("timelineLabelOf(event)", summary_line,
                      "时间线行仍在用静态的 timelineLabel[kind]，看不到 degraded：%r"
                      % summary_line.strip()[:110])

    def test_the_label_changes_when_degraded(self):
        body = self._helper()
        self.assertRegex(body, r"event\.degraded\s*\?",
                         "timelineLabelOf 没有按 degraded 分支")

    def test_it_says_the_conclusion_is_not_the_models_own_words(self):
        """文案要说清「结论不是模型原文」，而不是笼统的「失败」。"""
        body = self._helper()
        self.assertIn("结论非模型原文", body,
                      "降级标签必须说明结论的性质，否则用户仍会当成模型的答案")

    def test_a_normal_completion_is_not_marked_degraded(self):
        """**反向守卫**：不得把所有完成都标成降级。

        判据是「后缀只在 event.degraded 为真时出现」，所以这里查结构而不是查文案 ——
        写成 `基线 + (event.degraded ? '（降级）' : '')` 之外的形状（例如用
        `!event.degraded` 判断、或者无条件加后缀）都会在这里露出来。
        """
        body = self._helper()
        m = re.search(r"return\s+(.+)$", body, re.M)
        self.assertIsNotNone(m, "timelineLabelOf 没有 return")
        expr = m.group(1).strip()
        self.assertIn("event.degraded ?", expr,
                      "返回式必须以 event.degraded 为条件，否则会误标正常完成：%r" % expr)
        self.assertRegex(expr, r"^\S.*\?\s*`.*`\s*:\s*\S",
                         "降级分支与非降级分支都要有值：%r" % expr)

    def test_the_static_table_still_exists_for_other_kinds(self):
        """不得把静态表删掉 —— 几十个 kind 靠它。"""
        self.assertIn("const timelineLabel: Record<string, string>", _src(VUE))


if __name__ == "__main__":
    unittest.main()