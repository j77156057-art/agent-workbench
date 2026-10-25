# -*- coding: utf-8 -*-
"""格式停滞那一刻，**出站消息**必须落盘——那是唯一还没量过的东西。

## 这个探针要抓什么

实测阻断（`wf-2c5612c6ee53` / `wf-87c0df3224aa`）：原生 FC 通道上模型
**第 1 轮正常，第 2 轮起不产出可解析输出**，连续两轮即触发格式停滞诊断。

已排除的假设有一串：通道可用、最简 schema 可用、111 个真实工具可用、
schema 形状不畸形、`output_budget=43253` 充足、`last_tool_calls` 形状正确。

也就是说：**手上每一个字段都「正常」，而现象不正常。** 唯一没量的是
**第 2 轮发给模型的消息到底长什么样**。

## 为什么不能事后从 trace 反推

trace 存的是**观察**（工具返回了什么），不是**请求**（我们问了什么）。
而 `messages` 在 ReAct 循环里每轮重建，循环一退出就没了。格式停滞那一刻是
**唯一**能拿到它的时刻——这也是为什么落盘点选在判定为停滞之后、而不是每轮。

## 为什么落盘能定位

落的是 `messages` 原样（含 assistant 历史与 tool 结果），于是能直接看四件事——
它们恰好覆盖「第 2 轮起坏掉」最常见的成因：

1. 有没有 `role="tool"` 但**没有**对应的 `tool_call_id`（上游会 400 或静默忽略）；
2. 有没有 assistant 消息的 `tool_calls` 是**空数组**；
3. 消息条数/字符数在第 1 轮与第 2 轮之间跳变了多少；
4. 最后一个 assistant 消息的形状与模型自己的输出对不对得上。

这四件事此前**一条都看不到**。
"""
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


class _FakeLLM:
    """只需要 `_dump_stall_probe` 读到的那几个属性。"""

    provider = "deepseek"
    model = "deepseek-chat"
    capability = {"native_tools": True}
    output_budget = 43253
    last_usage = {"completion_tokens": 0, "prompt_tokens": 1840}
    last_tool_calls = []
    last_finish_reason = None
    last_stream_ref = None
    last_raw_head = ""


REAL_MESSAGES = [
    {"role": "system", "content": "你是子代理"},
    {"role": "user", "content": "读 behaviors/player.gd"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "read_file",
                      "arguments": '{"path":"behaviors/player.gd"}'}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "=== player.gd ===\nextends CharacterBody3D"},
]


class _ProbeEnvMixin(unittest.TestCase):
    """每个用例一个干净的 `.tmp/stall_probe` 沙箱。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_dir = agent._STALL_PROBE_DIR
        agent._STALL_PROBE_DIR = os.path.join(self.tmp, "probe")
        self.addCleanup(self._restore_dir)
        self._saved_env = os.environ.pop("DOCMIND_PROBE_LLM", None)
        if self._saved_env is not None:
            self.addCleanup(lambda: os.environ.__setitem__("DOCMIND_PROBE_LLM",
                                                           self._saved_env))

    def _restore_dir(self):
        agent._STALL_PROBE_DIR = self._saved_dir

    def dumps(self):
        files = sorted(glob.glob(os.path.join(agent._STALL_PROBE_DIR, "*.json")))
        self.assertTrue(files, "没有落盘任何文件")
        return [json.load(io.open(path, encoding="utf-8")) for path in files]


class ProbeIsOffByDefaultTests(_ProbeEnvMixin):
    """默认不该在生产路径上付这份I/O。"""

    def test_nothing_is_written_when_the_env_is_unset(self):
        os.environ.pop("DOCMIND_PROBE_LLM", None)
        agent._dump_stall_probe(_FakeLLM(), REAL_MESSAGES, "acc")
        self.assertEqual(glob.glob(os.path.join(agent._STALL_PROBE_DIR, "*")), [],
                         "探针默认开着——每次停滞都在生产机上写文件")

    def test_empty_env_string_also_counts_as_off(self):
        os.environ["DOCMIND_PROBE_LLM"] = "   "
        agent._dump_stall_probe(_FakeLLM(), REAL_MESSAGES, "acc")
        self.assertEqual(glob.glob(os.path.join(agent._STALL_PROBE_DIR, "*")), [])


class DiagnosticNeverBreaksTheWorkflowTests(_ProbeEnvMixin):
    """诊断失败必须吞掉——否则探针自己变成故障源。"""

    def test_an_unwritable_directory_does_not_raise(self):
        agent._STALL_PROBE_DIR = os.path.join("N:", "根本不存在", "的", "目录")
        agent._dump_stall_probe(_FakeLLM(), REAL_MESSAGES, "acc")  # 不抛就算过

    def test_a_none_llm_does_not_raise(self):
        """`self.llm` 理论上不会是 None，但诊断不该依赖这个前提。"""
        agent._dump_stall_probe(None, REAL_MESSAGES, "acc")

    def test_unserialisable_objects_do_not_raise(self):
        class _Weird:
            pass

        agent._dump_stall_probe(_FakeLLM(), [{"role": "user", "content": _Weird()}],
                                _Weird())


class OutboundMessagesAreVerbatimTests(_ProbeEnvMixin):
    """落盘必须是**原样**，裁剪过的消息证明不了任何事。"""

    def setUp(self):
        super().setUp()
        os.environ["DOCMIND_PROBE_LLM"] = "1"
        agent._dump_stall_probe(_FakeLLM(), REAL_MESSAGES, "I have partial evidence.",
                                session_id="sess-1")

    def test_the_messages_are_written_verbatim(self):
        self.assertEqual(self.dumps()[0]["messages"], REAL_MESSAGES,
                         "出站消息被改写了——那份快照证明不了上游看到了什么")

    def test_the_assistant_tool_call_id_is_preserved(self):
        """`tool_call_id` 对不上是「第 2 轮起坏掉」的头号成因，必须看得到。"""
        rows = self.dumps()[0]["messages"]
        call_ids = [tc["id"] for msg in rows
                    for tc in (msg.get("tool_calls") or [])]
        tool_ids = [msg.get("tool_call_id") for msg in rows
                    if msg.get("role") == "tool"]
        self.assertTrue(call_ids, "tool_call id 没落盘")
        for tool_id in tool_ids:
            self.assertIn(tool_id, call_ids,
                          "role=tool 的 id 在出站消息里找不到对应调用")

    def test_the_message_count_is_recorded(self):
        self.assertEqual(self.dumps()[0]["message_count"], len(REAL_MESSAGES))

    def test_it_records_what_the_upstream_said(self):
        row = self.dumps()[0]
        self.assertEqual(row["acc_chars"], len("I have partial evidence."))
        self.assertEqual(row["usage"], {"completion_tokens": 0, "prompt_tokens": 1840})
        self.assertEqual(row["session_id"], "sess-1")


class FinishReasonIsReadableTests(unittest.TestCase):
    """`finish_reason` 是分辨「模型自己停了」与「被长度掐了」的唯一凭据。

    而非流式路径上**原本完全没有**这个信息。
    """

    def test_stream_reason_is_read_from_the_stream_object(self):
        class _Stream:
            finish_reason = "length"

        llm = _FakeLLM()
        llm.last_stream_ref = _Stream()
        self.assertEqual(agent._probe_finish_reason(llm), "length")

    def test_non_stream_reason_comes_from_the_client(self):
        llm = _FakeLLM()
        llm.last_finish_reason = "stop"
        self.assertEqual(agent._probe_finish_reason(llm), "stop")

    def test_undecided_reads_as_none(self):
        """`None` 本身就是信息：说明这条走的是流式、值还没定下来。"""
        llm = _FakeLLM()
        llm.last_stream_ref = None
        llm.last_finish_reason = None
        self.assertIsNone(agent._probe_finish_reason(llm))


class ProbeIsWiredToTheStallBranchTests(unittest.TestCase):
    """落盘点必须**在停滞分支上**——挂在别处的探针一次也跑不到。

    §9 的老教训：断言写得像在断某个东西，得先确认那条代码路径被走到过。
    """

    def setUp(self):
        import inspect
        self.src = inspect.getsource(agent.Agent._run)

    def test_the_call_site_exists(self):
        self.assertIn("_dump_stall_probe(", self.src)

    def test_it_is_called_next_to_the_stall_diagnostic(self):
        """落盘必须在 `format_stall` 分支内、且在抛出 degraded final **之前**——
        那是唯一还拿得到 messages/acc 现场的时刻。

        `_run` 里有不止一处探针（不同停滞分支各一处），所以按「离第一处探针的
        字符距离」判会量到别处、被无关的大段注释撑爆。这里改成结构判据：从停滞
        判定往下，本分支自己的落盘调用必须早于它 yield 的 degraded final。
        """
        src = self.src
        stall_at = src.find('turn.outcome = "format_stall"')
        self.assertNotEqual(stall_at, -1, "停滞分支不在了——探针会失去落盘点")
        probe_at = src.find("_dump_stall_probe(", stall_at)
        self.assertNotEqual(probe_at, -1, "停滞分支内没有落盘调用——探针挂到了别处")
        yield_at = src.find('"degraded": True,', probe_at)
        self.assertNotEqual(yield_at, -1, "停滞分支的 degraded 收尾不在了")
        self.assertLess(probe_at, yield_at,
                        "落盘发生在抛出 degraded final 之后——messages 现场已释放")
        self.assertIn("messages", src[probe_at:probe_at + 120],
                      "探针没接 messages 现场")

    def test_the_probe_never_replaces_the_user_facing_conclusion(self):
        """探针是**旁路**：它不能改 `acc`，也不能插进要返回的那段文字里。"""
        probe_at = self.src.find("_dump_stall_probe(")
        after = self.src[probe_at:probe_at + 400]
        self.assertNotIn("acc =", after,
                         "落盘时改了 acc——用户看到的就不是模型的真实输出了")
        self.assertIn("yield", after, "落盘之后应当正常返回，不该把控制流改掉")


if __name__ == "__main__":
    unittest.main()