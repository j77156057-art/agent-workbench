# -*- coding: utf-8 -*-
"""主对话的「未变化存根」读记录必须按**会话**隔离（不是进程级共享）。

## 现场（真实 `/api/chat` HTTP 多轮钻测暴露）

一个全新 `session_id`，**第一次** `read_file` 读 `player.gd`/`enemy.gd` 时，
observation 直接回了：

    === behaviors\\player.gd（未变化）===
    整份文件已在本次对话中读过、且此后未改动；直接引用那次结果，**不要重复读取**。

可从没读过这些文件的**新会话**，凭什么「已读过」？因为从前主对话不绑定读记录作用域，
`_read_store()` 回落到**模块级全局**——于是会话 B 首读会话 A 读过的文件被挡成存根，
那句「本次对话已读过」对 B **是假的**（B 压根没见过那次读取），损害定位/引用题可靠性。

## 修法（A：主对话也按会话隔离，复用子代理那套机制）

会话的 Agent 实例持有一份长活 `(set(), {})`，`run()` 用 `bind_read_conversation`
把它绑进上下文、`finally` 里 `end_read_conversation` 还原。于是：
- 同一会话跨轮复用同一份 store → 「别重复读」省 token 收益**保留**；
- 不同会话各持一份 → 互不污染（本文件核心）。

与子代理 `begin_read_conversation` 的区别：子代理每次开**新空份**（每个子代理都是全新
对话）；主对话绑的是**实例已有的那份**（会话跨轮要连续）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config as C  # noqa: E402
import tools as T  # noqa: E402

#: 存根只在它比被替代的内容更短时才发（`_STUB_MIN_SAVING`），所以必须用大文件。
_BIG_LINES = 4000
_BODY = "x" * 400 + "\n"


class _Project:
    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "big.gd"), "w", encoding="utf-8") as f:
            f.write(_BODY * _BIG_LINES)
        self._prev_root = C.get_runtime("code_root")
        self._prev_rules = C.get_runtime("project_rules")
        C.set_runtime("code_root", self.root)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        return self

    def __exit__(self, *exc):
        T.clear_read_files()
        C.set_runtime("code_root", self._prev_root)
        C.set_runtime("project_rules", self._prev_rules)
        return False


class MainConversationIsPerSessionTests(unittest.TestCase):
    """机制层（无需 LLM）：用两份不同的 store 模拟两个会话。"""

    def _read(self):
        return T.read_file("path: big.gd")

    def test_session_b_first_read_is_not_blocked_by_session_a(self):
        """核心：会话 A 读过全文，会话 B 首读必须真读到内容、不吃假存根。"""
        with _Project():
            store_a = (set(), {})   # 会话 A 的实例 store
            store_b = (set(), {})   # 会话 B 的实例 store
            tok_a = T.bind_read_conversation(store_a)
            try:
                out_a1 = self._read()
                self.assertNotIn("未变化", out_a1)
                self.assertIn(_BODY[:40], out_a1,
                              "会话 A 首读应当真的读到正文")
                # 会话 A 第二轮：命中自己的存根（省 token，合法）
                self.assertIn("未变化", self._read())
            finally:
                T.end_read_conversation(tok_a)

            tok_b = T.bind_read_conversation(store_b)
            try:
                out = self._read()
                self.assertNotIn("未变化", out,
                                 "新会话首读拿到「已在本次对话中读过」的存根——"
                                 "那份内容它压根没见过，定位/引用题会失准")
                self.assertIn(_BODY[:40], out, "会话 B 应当真的读到正文")
            finally:
                T.end_read_conversation(tok_b)

    def test_same_session_reuses_across_turns(self):
        """会话隔离不能把跨轮省 token 整个关掉：绑**同一份** store 跨轮仍命中。"""
        with _Project():
            store = (set(), {})     # 一个会话的实例 store，跨两个回合沿用
            tok1 = T.bind_read_conversation(store)
            try:
                self.assertNotIn("未变化", self._read())
            finally:
                T.end_read_conversation(tok1)
            tok2 = T.bind_read_conversation(store)
            try:
                self.assertIn("未变化", self._read(),
                              "同一会话跨轮重复读没被拦住——省 token 收益丢了")
            finally:
                T.end_read_conversation(tok2)

    def test_default_still_module_global_when_unbound(self):
        """不进任何会话作用域时仍回落到模块全局（兼容直接调工具的测试）。"""
        with _Project():
            files, coverage = T._read_store()
            self.assertIs(files, T._READ_FILES)
            self.assertIs(coverage, T._READ_COVERAGE)

    def test_reset_clears_live_session_stores(self):
        """代码库重置必须连 live 会话 store 一起清，否则过期存根跨重索引残留。"""
        with _Project():
            store = (set(), {})
            tok = T.bind_read_conversation(store)
            try:
                self._read()
                self.assertIn("未变化", self._read())  # 已记录
            finally:
                T.end_read_conversation(tok)
            # 复刻 api 重置的 fan-out：清所有 live store
            for _ag_store in (store,):
                _ag_store[0].clear()
                _ag_store[1].clear()
            tok2 = T.bind_read_conversation(store)
            try:
                out = self._read()
                self.assertNotIn("未变化", out,
                                 "重置后仍命中旧存根——重索引后新任务会被假存根挡")
            finally:
                T.end_read_conversation(tok2)


class _NullLLM:
    """占位 client：构造 Agent 用，不触网、不被调用。"""
    provider = ""
    model = ""
    base_url = ""
    capability = {"vision": None}

    def chat(self, messages, stream=True, **kwargs):
        raise AssertionError("测试不应调用 LLM")

    def count_tokens(self, text):
        return 0


class AgentRunWiringTests(unittest.TestCase):
    """接线层：`Agent.run` 必须真的绑/还原，否则上面全是空谈。"""

    def _src(self):
        import inspect
        import agent
        return inspect.getsource(agent.Agent.run)

    def test_run_binds_the_instance_store(self):
        src = self._src()
        self.assertIn("bind_read_conversation(self._read_store)", src,
                      "run() 没绑本会话的 store——主对话会退回进程级全局、跨会话泄漏")

    def test_run_restores_in_finally(self):
        src = self._src()
        tail = src[src.rindex("finally:"):]
        self.assertIn("end_read_conversation(_read_conv_token)", tail[:400],
                      "end 不在 finally 里：异常/断连路径会把读记录永久留在错误作用域")

    def test_agent_holds_a_fresh_store_per_instance(self):
        """每个会话 Agent 各持一份独立 store（互不共享），且初始为空。"""
        import agent
        a = agent.Agent(llm=_NullLLM())
        b = agent.Agent(llm=_NullLLM())
        self.assertEqual(tuple(len(x) for x in a._read_store), (0, 0))
        self.assertIsNot(a._read_store, b._read_store,
                         "两份 store 竟是同一对象——会话间仍会互串")


if __name__ == "__main__":
    unittest.main()
