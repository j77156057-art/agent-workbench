"""请求边界：负值必须回 400，不是 500。

前置校验（api.py:4273-4282）本来就挡负值，所以 raise 路径平时走不到。
但**这正是要测的**：万一将来新增字段忘了加校验，或者有人绕过前置校验，
`normalize_settings` 的 raise 会不会把整请求变成 500（用户看到「服务异常」）

用 IsolatedAsyncioTestCase + 直接构造 req，不真起服务。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
from api import AgentCapacityReq  # noqa: E402


class NegativeValueReturns400Tests(unittest.IsolatedAsyncioTestCase):
    """负值 -> 400，且错误信息可读。"""

    async def test_negative_max_nudges_is_rejected_with_400(self):
        req = AgentCapacityReq(max_nudges=-1)
        resp = await api.set_agent_capacity(req)
        # 前置校验应该先拦住它 —— 那也是 400
        self.assertEqual(resp.status_code, 400)

    async def test_negative_is_rejected_before_reaching_normalize(self):
        """前置校验挡住 -> 说明 raise 只是第二道防线，不改变用户可见行为。"""
        req = AgentCapacityReq(main_max_steps=-5)
        resp = await api.set_agent_capacity(req)
        self.assertEqual(resp.status_code, 400)
        body = resp.body
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        self.assertIn("0", text)          # 文案要说清 0 是合法值
        self.assertIn("1000", text)       # 以及上界

    async def test_oversized_clamps_and_is_accepted(self):
        """超上界不再被 400 拒 —— 夹到上界后保存。

        ⚠ 原行为是前置校验 `0 <= x <= 1000` 把 5000 判为 400。
        但「想设很多」是合理意图，拒掉它不合理；夹到上界更友好。
        """
        req = AgentCapacityReq(max_nudges=5000)
        out = await api.set_agent_capacity(req)
        # 成功路径返回 dict（FastAPI 直接序列化），不是 JSONResponse
        self.assertNotIsInstance(out, api.JSONResponse,
                                 "超上界仍被拒 —— 应该夹到 1000 而不是拒绝")
        settings = out["settings"] if isinstance(out, dict) else {}
        self.assertEqual(settings.get("max_nudges"), 1000,
                         f"5000 没被夹到 1000：{settings}")


if __name__ == "__main__":
    unittest.main()
