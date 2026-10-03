"""harness 自己的话必须和**传输层协议**一致——一个"自相矛盾"的兜底。

## 这次的实际故障（stealth/space-bunny-alpha，原生function-calling）

    搜索阶段 ✓  search_code + grep 都走原生通道成功
    执行提示 ✓  触发证据纠偏「请先调用 read_file…再重新给出 Final Answer」
    失败       模型输出散文「需要用 read_file 读取原文核验…」
               → 判格式失守 → 再纠偏「补上 Action: / Action Input:」
               → 再失守 → 硬失败，且已搜到的 player.gd L22-38 / enemy.gd 全部丢弃

根因不是模型不守规矩，是**harness 在原生通道下要求了传输层表达不了的东西**：
原生 function-calling 里，模型无法"手写一个 Action"——它只能发出真正的 tool call
或写正文。而系统提示与全部纠偏文案都无条件要求 `Action:` / `Final Answer:` 标记。

模型收到的是两套矛盾契约。它只能尽力照做（写散文），然后被自己的照做判成失败。

## 三条同形 bug

| |表面症状 | 真实原因 |
|---|---|---|
| 11 个写盘工具 | 测试全绿 | 断言的是运行期不读的死副本 |
| 快照 | 写入无报错 | 字段漏搬，写得进读不回来 |
| 检索降级 | 名为 hybrid | dense 腿一断，全断 |
| **本文件** | **模型"不守格式"** | **harness 自己给了两套矛盾指令** |

共同点：失败信号所指向的对象不是真正的原因。

## 刻意不做的一件事

**不**把"裸散文"直接当最终答案。因为上面那条trace 恰好证明散文可能是**计划**
（「需要用 read_file 读取原文核验…」）而不是结论——把它当答案等于告诉用户
"我再去查一下"，比报错更糟。判定权留在解析层，不靠提示词去猜。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import _prompt_for_channel  # noqa: E402


class ChannelAwareContractTests(unittest.TestCase):
    """原生通道下，系统提示不得要求手写文本协议的标记。"""

    def test_native_mode_drops_the_text_protocol_contract(self):
        react_prompt = agent_mod.SYSTEM_PROMPT
        native_prompt = _prompt_for_channel(react_prompt, native=True)

        self.assertNotIn(
            agent_mod._REACT_CONTRACT_MARK, native_prompt,
            "原生通道下仍在要求 `Action:` / `Final Answer:`——"
            "而传输层根本不支持手写调用")
        self.assertIn(
            agent_mod._NATIVE_CONTRACT_MARK, native_prompt,
            "原生契约没被换上：模型会同时收到两套互相矛盾的格式要求")

    def test_native_contract_states_the_override_explicitly(self):
        """必须点名那些要被忽略的标记，否则模型不知道要忽略什么。"""
        native_prompt = _prompt_for_channel(agent_mod.SYSTEM_PROMPT, native=True)
        self.assertIn("不适用", native_prompt,
                      "没有声明遗留文本协议段落本轮不适用——"
                      "模型仍会读到「必须以 Final Answer: 开头」这类要求")
        self.assertIn("Final Answer:", native_prompt,
                      "override 条款必须点名那个标记，否则模型不知道要忽略什么")

    def test_the_trailing_reinforcement_is_removed(self):
        """**末尾**那段重申是模型最后读到的指令，必须真的换掉。

        原文末尾写着「**不要只写一段正文而不带任何标记**」。只换开头契约、
        指望 override 条款压住末尾——这正是实测失败的做法：两套契约并存时，
        模型很可能照最后读到的那份走，于是被自己的照做判成失守。
        """
        native_prompt = _prompt_for_channel(agent_mod.SYSTEM_PROMPT, native=True)
        self.assertNotIn(agent_mod._REACT_TAIL_MARK, native_prompt,
                         "提示词末尾那段文本协议格式说明仍在——"
                         "它是模型最后读到的指令，权重最高")
        self.assertNotIn(
            "不要只写一段正文而不带任何标记", native_prompt,
            "末尾仍在命令模型「不要只写不带标记的正文」")
        self.assertIn(
            "不需要任何格式标记", native_prompt,
            "末尾的收尾要求必须换成原生通道的说法")

    def test_no_text_protocol_instruction_survives_anywhere(self):
        """兜底：原生提示里不该再残留任何"手写标记"式的硬要求。

        逐条列举而非只查标记——因为散述里出现 `Final Answer:` 本身是允许的
        （override 条款要靠它点名），要求的是**要求手写**的那类句子。
        """
        native_prompt = _prompt_for_channel(agent_mod.SYSTEM_PROMPT, native=True)
        forbidden = [
            "必须直接以 `Final Answer:` 开头",
            "任何情况下都要带上 `Action:` 或 `Final Answer:` 其中之一",
            "要么 `Thought:` + `Action:` + `Action Input:`",
            "格式硬边界：",
            "Thought: 你的思考过程",
        ]
        left = [f for f in forbidden if f in native_prompt]
        self.assertEqual(left, [],
                         "原生提示里仍残留要求手写标记的硬性条款：\n  %s"
                         % "\n  ".join(left))

    def test_the_rest_of_the_prompt_survives(self):
        """只换契约块，别把工具清单之类正文吞掉。"""
        react_prompt = agent_mod.SYSTEM_PROMPT
        native_prompt = _prompt_for_channel(react_prompt, native=True)
        self.assertIn("你是一个严谨的多工具问答 Agent", native_prompt,
                      "替换契约块时把提示词主体弄丢了")

    def test_react_mode_is_untouched(self):
        """文本协议的行为**不得**改变——那是另一批用户的既有路径。"""
        react_prompt = agent_mod.SYSTEM_PROMPT
        self.assertEqual(_prompt_for_channel(react_prompt, native=False), react_prompt)
        self.assertEqual(
            _prompt_for_channel(_prompt_for_channel(react_prompt, True), True),
            _prompt_for_channel(react_prompt, True),
            "重复调用不幂等——每轮都会再加一段契约")


def _shell_source():
    """主循环在 `Agent._run`。

    早先的版本 inspect 了 `run`（只做参数保存与转发），再试`_run_shell`（也不是它），
    于是永远查不到循环里的文案——一个「看起来在检查、实际什么都没查」的测试。
    检查器必须先确认自己看的是对的地方。
    """
    import inspect
    src = inspect.getsource(agent_mod.Agent._run)
    assert "format_stalls" in src, "查错了函数：_run 里没有 format_stalls"
    return src


class ChannelAwareRecoveryMessageTests(unittest.TestCase):
    """纠偏与失败文案同样必须说当前通道的话。"""

    def _messages(self):
        return _shell_source()

    def test_the_format_stall_message_explains_the_real_cause(self):
        src = self._messages()
        self.assertNotIn(
            "它的回答其实是有内容的", src,
            "这句断言没有证据：无法区分「结论」和「下一步计划」，"
            "而 trace 里那段散文就是计划")
        self.assertIn("原生 function-calling", src,
                      "失败文案必须说清是原生通道，否则用户会去换模型"
                      "（换模型解决不了 harness 自相矛盾）")

    def test_the_stall_branch_keeps_the_model_output(self):
        """失败时丢掉模型实际产出，等于把已经查到的证据一并扔掉。"""
        src = self._messages()
        self.assertIn(
            "模型最后一次输出", src,
            "格式失守时没有回显模型实际输出——用户只看到「执行失败」，"
            "已经搜到的候选文件与行号一个字都不剩")

    def test_the_stall_output_is_labelled_as_unverified(self):
        """回显，但不能当成结论交出去。"""
        src = self._messages()
        self.assertIn("不要当作结论", src,
                      "回显的中间输出必须标明未经核验，"
                      "否则就是把「我再去查一下」当成答案")

    def test_the_nudge_does_not_ask_for_markers_in_native_mode(self):
        """原生通道里要求补 `Action:` 是要求一个不存在的动作。"""
        src = self._messages()
        self.assertIn(
            "不要写任何 Action:/Final Answer: 标记", src,
            "原生通道的纠偏仍在要求补文本协议标记——"
            "这是本次故障的直接成因，模型因此连续两轮被判失守")


class TheCheckItselfWorksTests(unittest.TestCase):
    """这类"检查提示词里有没有某句话"的测试很容易变成永远绿的灯，所以自测。"""

    def test_it_would_catch_a_regression(self):
        """把契约换回去，测试必须变红。"""
        broken = _prompt_for_channel(agent_mod.SYSTEM_PROMPT, native=False)
        self.assertIn(
            agent_mod._REACT_CONTRACT_MARK, broken,
            "检查器对「契约还在」这件事本身失效了")


if __name__ == "__main__":
    unittest.main()