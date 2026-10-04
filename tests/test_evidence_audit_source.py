# -*- coding: utf-8 -*-
"""证据审计必须读**真实执行记录**，不能从 trail 文本反推。

## 实测损失

模型并行 read_file 了四个区间（enemy.gd:1-12、enemy.gd:40-50、player.gd:30-49、
ui/hud.gd:1-30），证据齐备，答案里每个行号也都对。但结尾挂上了：

    （证据复核：以上文件/行号尚未完成read_file 原文核验，请勿据此修改代码。）

**这句话当时是假的**，而且有害——它让用户不要相信一组其实已经核实过的行号。

## 根因：审计从文本协议反推证据

`_audit_live_answer` 扫trail 里的 `Action:` 文本标记来重建步骤。但原生FC 的
**并行批次**路径往 trail 写的是：

    {"role": "assistant", "content": "（并行调用）read_file、read_file"}

**没有 `Action:` 标记**——只有顺序路径（`_native_queue`）会合���出 `Action:` 行。
于是并行读取（也就是模型最自然的用法）一律被判成「零步骤 -> 未核验」。

这是本项目同一个根因的第五次现身：**判断信号来自一个不适用的协议**。

| | 表面症状 | 真实原因 |
|---|---|---|
| 11 个写盘工具 | 测试全绿 | 断言的是运行期不读的死副本 |
| 检索降级 | 名为 hybrid | dense 腿一断，全断 |
| 原生 FC 失败 | 模型"不守格式" | harness 给了两套矛盾契约 |
| read_file | 文件不存在 | 参数语法不被接受 |
| 正确答案被扔 | "没给出结论" | 文本协议的启发式套到结构化通道 |
| **本文件** | **"尚未核验"** | **从trail 文本反推证据，而那条路径不写标记** |
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402


class AuditUsesRealExecutionsTests(unittest.TestCase):
    """核心：传了真实执行记录就用它，不再反推。"""

    def _audit(self, steps):
        return agent_mod._audit_live_answer(
            "扣多少看 behaviors/enemy.gd:7，实际改血在 behaviors/player.gd:39。",
            "玩家受伤扣多少血在哪算的？", trail=None, executed_steps=steps)

    def test_successful_reads_count_as_verified(self):
        steps = [
            {"action": "read_file", "ok": True},
            {"action": "read_file", "ok": True},
            {"action": "read_file", "ok": True},
            {"action": "read_file", "ok": True},
        ]
        out = self._audit(steps)
        self.assertFalse(
            out.get("required") and not out.get("ok"),
            "四次成功 read_file 之后仍被判未核验——会给用户挂上"
            "「尚未核验、请勿据此修改代码」这种当时为假的警告：%r" % out)

    def test_failed_reads_do_not_count_as_verified(self):
        """严格性必须保留：读失败的文件不算核验过。"""
        steps = [{"action": "read_file", "ok": False}]
        out = self._audit(steps)
        self.assertTrue(out.get("required"),
                        "全部 read_file 失败却不再要求核验——那等于放行未经验证的行号")

    def test_no_steps_still_requires_verification(self):
        out = self._audit([])
        self.assertTrue(out.get("required"),
                        "零步骤却不再要求核验——门禁整个失效了")

    def test_it_does_not_fall_back_to_text_parsing_when_steps_given(self):
        """传了 steps 就必须用它，哪怕 trail 里有看似合理的标记。

        不然的话两条来源会互相污染，一条修好另一条照样错。
        """
        trail = [{"role": "assistant", "content": "Action: read_file\nAction Input: a.gd"}]
        out = agent_mod._audit_live_answer(
            "behaviors/enemy.gd:7 的常量 10。", "扣血在哪算？",
            trail=trail, executed_steps=[])
        self.assertTrue(out.get("required"),
                        "传了空的执行记录却仍从 trail 文本里捞出了步骤——"
                        "两条来源必须互斥，否则修好一条另一条照样错")


class BothExecutionPathsRecordTests(unittest.TestCase):
    """两条执行路径（顺序 / 并行批次）都必须写入记录。

    只修一条的话：模型单文件读取时正常、一旦并行就又开始挂假警告——
    而并行恰恰是它最常用的形态。
    """

    def test_the_sequential_path_records_it(self):
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn('audit_steps.append({', src,
                      "顺序路径没有记录真实执行——单文件读取会重新开始挂假警告")
        self.assertIn('parsed["action"]', src,
                      "顺序路径的动作名没进记录")

    def test_the_parallel_batch_path_records_it(self):
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn('audit_steps.extend({', src,
                      "并行批次路径没有记录真实执行——而它恰恰不写 `Action:` 标记，"
                      "靠 trail 反推只能得到零步骤")

    # ---- 记录必须**带参数**，否则下游拿不到路径 -----------------------------
    #
    # 实测（`wf-f609ea8daf22` 的 `t3`）：结论尾部自带一行
    #     （核验：FAIL · 28 处引用可核验 · 2 处未通过（路径打不开）：player.gd:?、enemy.gd:?）
    # 而同一份结论在工作流侧 `review verdict=PASS`。
    #
    # 差别在`action` 字段只存了 `read_file` 四个字：裸文件名兜底靠「本轮读过
    # 哪些路径」建索引，索引来源就是这个字段，于是索引恒空，**每一条裸文件名
    # 引用都被判成幻觉路径**。而它同时是模型自己写进结论里的那句话。

    def test_the_record_carries_the_arguments(self):
        """只记裸工具名 = 把路径丢掉 = 下游拿不到「这轮读过什么」。"""
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertNotRegex(
            src, r'audit_steps\.(?:append|extend)\(\{"action":\s*(?:nm|parsed\["action"\])',
            "audit_steps 只记了裸工具名——路径参数被丢掉，"
            "「本轮读过哪些路径」的索引恒空，裸文件名引用全被判幻觉。"
            "是这次故障的直接来源")

    def test_both_paths_use_the_full_action_shape(self):
        import inspect
        from agent_runtime.output_audit import _read_paths_from_actions
        src = inspect.getsource(agent_mod.Agent._run)
        # 断言用**不含裸引号**的稳定子串：早先直接抄源码片段 `'%s(%s)' % (parsed["action"]'`
        # 本身就是一句不完整的 Python，第一跑就 SyntaxError。
        self.assertIn('"%s(%s)"', src,
                      "两条路径都没有用 `工具名(参数)` 的整句形状")
        self.assertIn('parsed["action_input"]', src,
                      "顺序路径没把 action_input 拼进记录——路径参数丢了")
        self.assertIn("(nm, _ar)", src,
                      "并行路径没把批次实参拼进记录——路径参数丢了")
        # 端到端：拼出来的形状必须真的能被路径提取器认出来
        self.assertEqual(
            _read_paths_from_actions(["read_file(path: behaviors/player.gd)"]),
            ["behaviors/player.gd"])

    def test_the_audit_is_called_with_the_records(self):
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn(
            "_audit_live_answer(final_text, question, trail, audit_steps)", src,
            "记录了却没传给审计——等于白记")


class TheFallbackStillWorksTests(unittest.TestCase):
    """缺省时保留文本反推兜底（其它调用方/历史结构），但必须标明它只对文本协议可靠。"""

    def test_omitting_steps_falls_back_to_trail_parsing(self):
        trail = [
            {"role": "assistant", "content": "Action: read_file\nAction Input: a.gd"},
            {"role": "user", "content": "Observation: === a.gd === 内容"},
        ]
        out = agent_mod._audit_live_answer(
            "behaviors/player.gd:39 是扣血语句。", "扣血在哪算？", trail=trail)
        self.assertIsInstance(out, dict)

    def test_the_docstring_says_it_is_only_reliable_for_text_protocol(self):
        import inspect
        doc = inspect.getdoc(agent_mod._audit_live_answer) or ""
        self.assertIn("文本协议", doc,
                      "必须写明兜底路径只对文本协议可靠——否则后人不会知道"
                      "为什么新代码必须传 executed_steps")


if __name__ == "__main__":
    unittest.main()