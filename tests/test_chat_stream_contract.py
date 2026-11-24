# -*- coding: utf-8 -*-
"""`/api/chat` 的 SSE 事件流**结构性锚点**（docs/backend-decomposition.md §14.6）。

## 为什么要有这一份

拆分 `api.py:chat`（656 行 → 编排 + 13 个方法）之后，「事件流不变」这条约束
如果只靠「跑一遍看看没坏」，那要等到真出事才发现 —— 而真出事的形态恰恰是
「看起来还能跑」的那种：多一帧、少一帧、顺序换了。

所以这里把契约写成**结构上就绕不过去**的形状：

1. 编排层（`chat`）里**不许出现 yield** —— 帧只允许在 `_chat_stream_*` 里发；
   否则「有人在编排层顺手加了一帧」没有任何东西会拦；
2. turn / route 两帧的**相对次序**被钉住（不是「都出现过」）；
3. done 帧必须在收尾（`finally`）**之后**发 —— 收尾没跑完就发 done，
   前端会以为整轮已经落定；
4. 心跳（`: keep-alive`）仍在事件泵里 —— 它不是事件（`parse_events` 会丢掉），
   但被吞掉前端就失去断连探测能力。

行为级的等价性由事件流对拍与既有的 `agent_runtime/chat_sse.py` 判定表负责；
这里只钉**形状**。
"""
import inspect
import unittest

import api


class ChatStreamContractTests(unittest.TestCase):
    """事件流契约的结构性锚点。"""

    def test_the_orchestration_layer_yields_nothing(self):
        """编排层不许产帧。"""
        src = inspect.getsource(api.chat)
        self.assertNotIn("yield ", src,
                         "编排层出现了 yield —— SSE 帧只允许在 _chat_stream_* 里发")

    def test_turn_comes_before_route(self):
        """turn → route 的次序不许换。"""
        src = inspect.getsource(api._chat_stream_open)
        i_turn = src.index("'type':'turn'")
        i_route = src.index("'type':'route'")
        self.assertLess(i_turn, i_route,
                        "turn 帧必须排在 route 帧前面 —— 前端靠 route 判路由")

    def test_done_is_emitted_after_the_cleanup(self):
        """done 帧在收尾之后。"""
        src = inspect.getsource(api._chat_event_stream)
        lines = src.splitlines()
        i_finally = next(i for i, ln in enumerate(lines)
                         if ln.strip() == "finally:")
        i_done = next(i for i, ln in enumerate(lines)
                       if "done" in ln and "yield" in ln)
        self.assertGreater(i_done, i_finally,
                           "done 帧必须在 finally 之后 —— 收尾没跑完就发 done，前端会以为整轮已经落定")

    def test_the_keepalive_heartbeat_is_still_there(self):
        """心跳不是事件，但不能被吞。"""
        src = inspect.getsource(api._chat_stream_pump)
        self.assertIn(": keep-alive", src,
                      "心跳行没了 —— 长思考期间前端会以为后端死了")


if __name__ == "__main__":
    unittest.main()
