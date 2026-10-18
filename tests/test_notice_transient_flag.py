"""BUG-2 回归锁：运行期 nudge 必须带 transient=True，否则前端把它焊成永久横幅。

背景（对应缺陷清单 BUG-2、docs/known-defect-notice-whitelist-detached.md）：
前端 useChatStream.ts 的分流判据是——notice 事件带 `transient: true` 才进 `turn.progress`
（final/done 时会被清空），否则进 `turn.notices`（永久挂在回合上）。

历史事故恰恰在**后端漏发这个标志**：ReAct 主循环里三条运行期 nudge
（"正在接续完整回答" ×2、"正在继续执行" ×1）都缺 `transient`，于是它们进了永久列表，
连"模型答对了的回合"底下也会永远焊一行假的「模型输出尚未结束，正在继续执行。」。
前端路由是对的（见 frontend/tests/noticeRouting.test.mjs 已全绿），漏的是后端没标。

因此这条测试要钉死**后端侧**：
1. 行为：跑一轮真的触发 nudge，断言那条 notice 事件确实带 `transient is True`。
2. 源码守卫：扫描 agent.py，凡是文案命中运行期 nudge 的 yield 一律必须含
   `"transient"`。防止将来新增同类提示又忘了标（这正是当年翻车的模式）。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402


class _FakeLLM:
    """按脚本回放模型输出的假 LLM（沿用 tests/test_format_stall.py 的形状）。"""

    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}

    def chat(self, messages, stream=True, **kw):
        script = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        if stream:
            def gen():
                for ch in script:
                    yield ch
            return gen()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


BARE = "玩家扣血在 TakeDamage 里算的。"
COMPLIANT_FINAL = "Final Answer: 玩家扣血在 TakeDamage 里算的。"

# 运行期 nudge 文案（这些必须是 transient；答完就该消失）
_TRANSIENT_NUDGES = (
    "模型单次输出已满，正在接续完整回答。",
    "模型输出尚未结束，正在继续执行。",
)


def _collect_notice_events(scripts, question="玩家受伤扣多少血在哪算的？"):
    llm = _FakeLLM(scripts)
    agent = agent_mod.Agent(llm=llm)
    notices = []
    for event in agent.run(question):
        if event.get("type") == "notice":
            notices.append(event)
    return notices


class NudgeTransientBehaviorTests(unittest.TestCase):
    """真跑一轮触发 nudge，断言事件带 transient=True。"""

    def test_running_nudge_carries_transient_flag(self):
        # 第一次裸正文（缺 Action/Final Answer 标记）→ 触发"正在继续执行"nudge，
        # 第二次合规收尾。这一条 nudge 若是永久通知就是 BUG-2。
        events = _collect_notice_events([BARE, COMPLIANT_FINAL])
        self.assertTrue(events, "这个脚本至少应触发一条运行期 nudge")
        for ev in events:
            if ev.get("text") in _TRANSIENT_NUDGES:
                self.assertIs(ev.get("transient"), True,
                              "运行期 nudge %r 必须带 transient=True，"
                              "否则前端会把它焊成永久横幅（BUG-2 复发）" % ev.get("text"))

    def test_no_transient_nudge_leaks_as_persistent(self):
        """换个角度：只要文案命中运行期 nudge，事件就必须是 transient 的。"""
        events = _collect_notice_events([BARE, COMPLIANT_FINAL])
        leaked = [ev for ev in events
                  if ev.get("text") in _TRANSIENT_NUDGES and ev.get("transient") is not True]
        self.assertEqual(leaked, [],
                         "有运行期 nudge 没带 transient=True：%r" % [e.get("text") for e in leaked])


class NudgeTransientSourceGuardTests(unittest.TestCase):
    """静态守卫：agent.py 里每条运行期 nudge 的 yield 都必须含 transient。

    扫描策略：找出所有 `yield {"type": "notice", ...}` 且文案命中 _TRANSIENT_NUDGES
    的行，断言其字典里含 `"transient"`。不依赖真的触发到，覆盖面更全、抗未来回归。
    """

    def test_all_transient_nudge_yields_are_marked(self):
        src_path = os.path.join(ROOT, "agent.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            source = fh.read()

        # 逐条 yield notice 语句（单行字典形式即可覆盖当前代码风格）
        yield_re = re.compile(r'yield\s+\{[^{}]*"type":\s*"notice"[^{}]*\}')
        matches = yield_re.findall(source)
        self.assertTrue(matches, "应至少能匹配到 notice 的 yield 语句（解析正则失效了？）")

        offenders = []
        for stmt in matches:
            text_hit = next((t for t in _TRANSIENT_NUDGES if t in stmt), None)
            if text_hit and "transient" not in stmt:
                offenders.append(text_hit)
        self.assertEqual(offenders, [],
                         "以下运行期 nudge 的 yield 缺 transient 标志：%r" % offenders)

    def test_compression_notice_stays_persistent(self):
        """反向保护：'自动压缩为摘要'是已发生的一次性事实告知，本就该常驻，
        不该被误标成 transient。此断言防止有人一刀切给所有 notice 加 transient。"""
        src_path = os.path.join(ROOT, "agent.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        for stmt in re.findall(r'yield\s+\{[^{}]*"type":\s*"notice"[^{}]*\}', source):
            if "自动压缩为摘要" in stmt:
                self.assertNotIn("transient", stmt,
                                 "压缩告知是事实通知，不应被清掉")


if __name__ == "__main__":
    unittest.main()
