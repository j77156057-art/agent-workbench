# -*- coding: utf-8 -*-
"""`task_token_budget` 的下界：连一次问答都跑不完的值，必须被拒。

## 这条锁的是什么

真事故：机器上存着 `task_token_budget = 5000`，于是**任何问题都答不了**——
`RunBudget.preflight` 在**第一轮**就返回 False，`_run` 直接 return，
`llm.chat` 被调用 **0 次**，用户只看到「（已达到本任务 Token 上限，已停止继续调用模型。）」。

界面不报错、设置页不提示、服务照常启动 —— **根因完全不可见**。
实测：一次「你好」的最小提示词约 4766 token，加每轮输出预留 3072
= 7838；跑完一次带工具的问答约需 15676。所以下界取 16000。

## 判据怎么写的（重要）

**不复刻实现。** 这里断言的是三件可独立核对的事：

1. `RunBudget(下界).preflight(<最小提示词>, 输出预留)` 必须为 True
   —— 直接问**权威实现**「这个值够不够」，而不是问「我的公式算出来是多少」
2. `RunBudget(下界 - 1)` 对同一组输入必须为 False
   —— 差一个 token 就不够，所以下界再少 1 一定失败。这两条同时成立，
   才说明下界**贴着**真实需求，不是随手取的整数字
3. 端点对更小的值返 400，且错误文案说清「至少要多少」

第 ①② 条是这套判据的关键：它们让下界数字**跟着实现走**。
若哪天提示词变短了（或换了个更省的模型），第 ① 条会先红，
提醒重新量 —— 而不是让一个凭空的数字永远钉在那里。

## 分层

与其他非法值一致：
* **请求边界** → 400（`tests/test_capacity_api_negative_400.py` 已覆盖负数）
* **开机路径** → `get_settings` 自己 try 住回落 DEFAULTS，服务起得来
  （存量配置里就有关键字时不能炸 —— 见 `test_a_saved_bad_value_does_not_block_startup`）
"""

import asyncio
import inspect
import json
import pathlib
import unittest
from unittest.mock import patch

import agent as agent_mod
from agent_runtime import capacity
from agent_runtime.run_budget import RunBudget

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _LLM:
    """最小可用的假 LLM：只为让 `_build_messages` / `_fit_budget` 工作。"""

    def __init__(self):
        self.calls = 0

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, enable_thinking=None, **kw):
        self.calls += 1
        return iter(["Final Answer: 好"])


def _smallest_prompt_chars() -> int:
    """实测「最小提示词」的字符数。

    ⚠⚠ **必须显式清掉项目长期记忆，否则量到的长度依赖本机状态。**

    实踩：`_build_messages` 会经 `agent_memory.context(scope, query)` 追加一条
    `【项目长期记忆：…】` 的 user 消息（309 字符），它来自持久化的记忆库。
    同一份代码：

        独立跑（读到本机记忆）  6 条 / 19064 字符
        pytest 里（读不到）      5 条 / 18755 字符

    差 309 —— 正好是那一条。于是「下界够不够」这个结论会**随运行者的机器而变**。
    按本机长度定的下界，换台机器就是错的。

    所以把记忆检索**显式置空**，量「不带任何用户数据的最小提示词」——
    那才是下界该保证的底线：再少也不可能了。

    ⚠ 这不是「为了让测试通过而缩小输入」：带记忆的提示词**更长**，
    需求只会更高。所以用不带记忆的长度当下界是**保守**的一侧；
    真实运行时若带了记忆，preflight 会更早耗尽 —— 那是用户自己的选择，
    不影响「下界保证能跑完一次不带工具的问答」这个承诺。
    """
    import agent_memory
    with patch.object(agent_memory, "context", return_value=""):
        a = agent_mod.Agent(llm=_LLM(), session_id="min")
        head = a._build_messages("你好")
    chars = sum(len(str(m.get("content") or ""))
                for m in a._fit_budget(head, []) if isinstance(m, dict))
    return chars


class TaskTokenBudgetLowerBoundTests(unittest.TestCase):
    MIN = capacity.MIN_TASK_TOKEN_BUDGET

    # ---------- 下界本身贴着真实需求（判据问权威实现，不复刻公式）----------

    #: 下界要保证「跑完 3 轮问答」。实测：
    #:   无长期记忆时 3 轮 = 18672
    #:   带 309 字符记忆时 = 18906   <- 下界取这个（保守侧）
    MIN_ROUNDS = 3

    #: 带 `【项目长期记忆：…】` 时的提示词长度（实测 309 字符）。
    #: 下界按「带记忆」定，所以判据要用带记忆的长度来验 ——
    #: 否则会验出一个比下界更宽松的条件。
    MEMORY_CHARS = 309

    def test_the_bound_covers_a_whole_multi_tool_answer(self):
        """下界必须够跑完 **3 轮**问答，且**贴着**这个需求。

        ⚠ 判据问的是**权威实现**（`RunBudget`）能不能撑住，
        不是拿我的公式去算 —— 那样就成了复刻生产逻辑。

        ⚠⚠ **长度必须加上项目长期记忆。** 下界取的是「带记忆」的值
        （18906），而 `_smallest_prompt_chars()` 是**无记忆**的（18672 口径）。
        两者差 309 字符 ≈ 77 token —— 恰好跨在临界点上，
        用无记忆的长度验会得出「18905 也够」的错误结论。

        三条一起断言，缺一不可：
          下界     -> 撑满 MIN_ROUNDS 轮
          下界 - 1 -> 撑不到（差一个 token 就不够）
          事故值 5000 -> 撑 0 轮
        """
        chars = _smallest_prompt_chars() + self.MEMORY_CHARS
        out = agent_mod.LLM_MAX_TOKENS

        def rounds_survived(limit):
            """真按 `RunBudget` 的用法跑：每轮 preflight 通过后 record。

            ⚠⚠ **必须调 `record`** —— 它是**累计**的（`total_tokens` 只加不减）。
            只反复 `preflight` 而不 record，等于每次从 0 出发，
            判据会以为任何大数都够 —— 那是第一版栽的地方。
            """
            rb = RunBudget(limit)
            alive = 0
            for _ in range(self.MIN_ROUNDS):
                if not rb.preflight(chars, out):
                    break
                alive += 1
                rb.record(None, prompt_chars=chars, output_chars=out)
            return alive

        got = rounds_survived(self.MIN)
        self.assertEqual(got, self.MIN_ROUNDS,
                         "下界 %d 只撑住 %d/%d 轮问答 —— 这个值本身不可用"
                         % (self.MIN, got, self.MIN_ROUNDS))

        low = rounds_survived(self.MIN - 1)
        self.assertLess(low, self.MIN_ROUNDS,
                        "下界减 1 仍撑满 %d 轮 -> 下界取大了，没贴着"
                        "「跑完 3 轮问答」这个需求。"
                        "若提示词变短了，请重新量并更新 MIN_TASK_TOKEN_BUDGET"
                        % self.MIN_ROUNDS)

        self.assertEqual(rounds_survived(5000), 0,
                         "事故值 5000 竟然撑得住 —— 若不为 0，说明这次量出来的"
                         "下界依据变了，请重新实测再改")

    def test_the_bound_holds_with_and_without_project_memory(self):
        """下界在**有/无长期记忆**两种运行态下都成立。

        记忆那条是持久化数据 —— 换台机器可能没有，多用几次会变长。
        下界若只按其中一种量，另一种运行态下就会失效。
        """
        out = agent_mod.LLM_MAX_TOKENS
        bare = _smallest_prompt_chars()
        for label, chars in (("无记忆", bare), ("带记忆", bare + self.MEMORY_CHARS)):
            rb = RunBudget(self.MIN)
            alive = 0
            for _ in range(self.MIN_ROUNDS):
                if not rb.preflight(chars, out):
                    break
                alive += 1
                rb.record(None, prompt_chars=chars, output_chars=out)
            self.assertEqual(alive, self.MIN_ROUNDS,
                             "%s（%d 字符）下只撑 %d/%d 轮 —— 下界必须对两种"
                             "运行态都成立" % (label, chars, alive, self.MIN_ROUNDS))

    # ---------- 契约边界 ----------

    def test_zero_means_unlimited_and_is_untouched(self):
        """0 = 不限，必须原样保留（夹到下界会把「不限」变成「跑一次」）。"""
        self.assertEqual(
            capacity.normalize_settings({"task_token_budget": 0})["task_token_budget"], 0)

    def test_at_and_above_the_bound_pass_through(self):
        for v in (self.MIN, self.MIN + 1, 100000):
            self.assertEqual(
                capacity.normalize_settings({"task_token_budget": v})["task_token_budget"],
                v, "下界及以上应原样保留 —— 夹取会改掉用户填的语义")

    def test_below_the_bound_raises_not_clamps(self):
        """⚠ 必须 raise，不能夹到下界。

        夹到 16000 会把用户填的 5000 悄悄变成 16000 ——
        他要的是「少花 token」，拿到的却是「能跑一次完整问答」，
        语义不是他填的那个。而留着 5000 更糟：问答直接死且不报错。
        """
        with self.assertRaises(ValueError) as ctx:
            capacity.normalize_settings({"task_token_budget": self.MIN - 1})
        msg = str(ctx.exception)
        self.assertIn(str(self.MIN), msg,
                      "错误文案必须说清至少要多少，否则用户不知道填多少")

    # ---------- 分层：开机路径不能炸 ----------

    def test_a_saved_bad_value_does_not_block_startup(self):
        """存量配置里就是 5000 时，`get_settings` 必须回落而不是抛。

        否则服务起不来 —— 那比问答失效更严重。
        """
        import config
        bad = {"max_agents": 64, "main_max_steps": 1000, "child_max_steps": 1000,
               "max_nudges": 1000, "max_final_continuations": 1000,
               "task_token_budget": 5000, "local_auto_limit": True}
        with patch.object(config, "get_runtime", return_value=dict(bad)), \
             patch.object(config, "load_state", return_value=dict(bad)):
            got = capacity.get_settings()
        self.assertEqual(got["task_token_budget"], 0,
                         "存量坏值应回落到 DEFAULTS（0=不限），"
                         "而不是让服务起不来")

    # ---------- 端点 ----------

    def test_the_endpoint_returns_400_with_an_actionable_message(self):
        """直接打 API 也必须被挡住，且文案说清要填多少。"""
        import api

        class Req:
            task_token_budget = 5000

            def __init__(self):
                for k in ("max_agents", "main_max_steps", "child_max_steps",
                          "max_nudges", "max_final_continuations"):
                    setattr(self, k, 0)

            def model_dump(self):
                return {"max_agents": 0, "main_max_steps": 0, "child_max_steps": 0,
                        "max_nudges": 0, "max_final_continuations": 0,
                        "task_token_budget": self.task_token_budget}

        resp = asyncio.run(api.set_agent_capacity(Req()))
        self.assertEqual(getattr(resp, "status_code", 200), 400)
        body = json.loads(resp.body)
        self.assertFalse(body["ok"])
        self.assertIn(str(capacity.MIN_TASK_TOKEN_BUDGET), body["error"],
                      "400 的文案必须给出可用下界，用户才知道填多少")

    # ---------- 下界随 ranges 下发 ----------

    def test_the_bound_is_published_to_the_frontend(self):
        """前端要能读到下界，否则界面无从提示「至少要这么多」。"""
        import api
        # ⚠ 直接 await 协程，不要 asyncio.run(asyncio.run(...)) ——
        # 内层已经跑完拿到 dict，再套一层会报 "a coroutine was expected"。
        payload = asyncio.run(api.get_agent_capacity())
        self.assertEqual(payload["ranges"]["task_token_budget_min"],
                         capacity.MIN_TASK_TOKEN_BUDGET,
                         "ranges 里没有下界 —— 前端拿不到它")


if __name__ == "__main__":
    unittest.main()
