# -*- coding: utf-8 -*-
"""测试卫生：一批用例把全局路径/开关泄漏给后续用例 —— `addCleanup` 记下了**新值**。

## 病灶（同类两处）

`tests/test_llm_stub.py:220-221` 与 `tests/test_chat_sse.py:108-109`：

    setattr(module, name, value)                                    # 先改
    self.addCleanup(setattr, module, name, getattr(module, name))    # 再存

`addCleanup(*args)` 的实参在**调用时**就求值，所以第 2 行记下的是第 1 行**刚写
进去**的临时路径，而不是原值。cleanup 于是「还原」成临时路径本身 —— 等于没还原。

外加两处 `agent_trace.TRACE_ENABLED = True`（llm_stub:222 / chat_sse:110）
**没有任何 cleanup**。

## 实测后果（`.tmp/probe_llm_stub_leak.py`）

cleanup 跑完后：

    TRACE_FILE    = D:\\Temp\\dm_llm_stub_favmc0fx\\traces.jsonl   （原：仓库根）
    SESSIONS_DIR  = D:\\Temp\\dm_llm_stub_favmc0fx\\sessions      （原：仓库根）
    泄漏的全局量 = ['TRACE_FILE', 'SESSIONS_DIR']

于是 `tests/test_state_root_isolation.py` 那 3 条红：

    sessions.SESSIONS_DIR 未落在 STATE_ROOT 下：D:\\Temp\\dm_llm_stub_9lcwdt9c\\sessions
    agent_trace.TRACE_FILE 未落在 STATE_ROOT 下：D:\\Temp\\dm_llm_stub_9lcwdt9c\\traces.jsonl

它们单跑是绿的，全量跑才红 —— 纯顺序依赖。

## 写法对照：全仓 14 处 addCleanup(setattr, ...) 里只有这 2 处是错的

其余 12 处都**先存原值再改**（`original` / `self._old` / `self._prev`）：

    original = getattr(module, name)
    setattr(module, name, value)
    self.addCleanup(setattr, module, name, original)

## 反向守卫的必要性

修这类问题最省事的写法是「把 `state_root_isolation` 的断言放宽/删掉」，或者
「让泄漏的用例 skip 掉」。所以下面**不测下游变绿**，而是直接断言这些全局量在
用例跑完后**等于原值** —— 守住卫生本身。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_memory  # noqa: E402
import agent_trace  # noqa: E402
import sessions  # noqa: E402
from tests import test_chat_sse, test_llm_stub  # noqa: E402

# 只 import **模块**，不 import 具体用例类：`from tests.test_x import SomeTests`
# 会让那些用例在**本文件里被 pytest 再收集一遍**（第一版就这么写，结果 9 条真实
# 用例在这里各跑了第二遍 —— 位置不同、上下文不同，一旦红就是假红）。
# 仓库里的先例（test_agent_memory / test_weather_recovery / test_runtime_contracts）
# 都只 import 下划线前缀的辅助类，pytest 不收集那些。
# 要触发的类用属性去取。
_LLM_STUB_CASES = (test_llm_stub.ProviderPathTurnTests,)
_CHAT_SSE_CASES = (test_chat_sse.RealTurnTests, test_chat_sse.MockProviderTests)

# 这些用例 setUp 里动的全局量。TRACED 三元组 + TRACE_ENABLED。
_GLOBALS = ((agent_trace, "TRACE_FILE"),
            (sessions, "SESSIONS_DIR"),
            (agent_memory, "DB_PATH"))


def _snapshot():
    return ([(m, n, getattr(m, n)) for m, n in _GLOBALS],
            agent_trace.TRACE_ENABLED)


class _Case:
    """借一个真实用例的 setUp，但把测试体换成空转。"""

    def __init__(self, cls):
        self._cls = cls

    def run(self, result):
        case = self._cls("runTest")
        case.setUp()
        try:
            case.doCleanups()
        finally:
            case.tearDown()


class GlobalStateLeakTests(unittest.TestCase):
    """反向守卫：这些用例**不许**把全局量留给后续用例。"""

    def _assert_restored(self, cls):
        before_paths, before_enabled = _snapshot()
        result = unittest.TestResult()
        _Case(cls).run(result)
        after_paths, after_enabled = _snapshot()
        leaked = [("%s.%s" % (m.__name__, n), old, now)
                  for (m, n, old), (_m2, _n2, now)
                  in zip(before_paths, after_paths) if old != now]
        self.assertEqual(
            leaked, [],
            "%s 的 setUp 把全局量改了没还原：%s"
            % (cls.__name__, "; ".join("%s %r -> %r" % (n, o, w) for n, o, w in leaked)))
        self.assertEqual(
            after_enabled, before_enabled,
            "%s 的 setUp 改了 agent_trace.TRACE_ENABLED 没还原：%r -> %r"
            % (cls.__name__, before_enabled, after_enabled))

    def test_llm_stub_turn_tests_restore_global_paths(self):
        for cls in _LLM_STUB_CASES:
            with self.subTest(cls=cls.__name__):
                self._assert_restored(cls)

    def test_chat_sse_turn_tests_restore_global_paths(self):
        # setUp 定义在基类 ChatTurnCase 上，两个子类都继承它、都会泄漏。
        for cls in _CHAT_SSE_CASES:
            with self.subTest(cls=cls.__name__):
                self._assert_restored(cls)

    def test_this_module_does_not_recollect_the_cases_it_touches(self):
        """**反向守卫**：本模块只许持有这些类的引用，不许把它们变成自己的用例。

        写成 `from tests.test_x import SomeTests` 时，pytest 会把那些用例在本文件里
        再收集一遍 —— 12 passed 里有 9 条是重复的，而且一旦红就是假红（本文件的
        位置/上下文跟 `test_chat_sse.py` 里的真实运行不同）。
        仓库先例：`test_agent_memory` / `test_weather_recovery` / `test_runtime_contracts`
        都只 import 下划线前缀的辅助类。
        """
        here = set(globals())
        for cls in _LLM_STUB_CASES + _CHAT_SSE_CASES:
            self.assertNotIn(cls, here,
                             "%s 被直接 import 进本文件命名空间 -> 会被二次收集，"
                             "请改用模块属性访问" % cls.__name__)

    def test_the_guard_would_notice_a_leak(self):
        """**反向守卫自身**：先故意泄漏一次，确认上面那条断言真的会红。

        没有这一条，「断言其实没断言到东西」和「修好了」会长得一模一样 ——
        这正是 `test_no_match_is_not_failure` 踩过的坑（守卫与实现同源失效）。
        """
        before_paths, before_enabled = _snapshot()
        tmp = os.path.join(os.environ.get("TEMP", "."), "dm_fake_leak_probe")
        for module, name in _GLOBALS:
            setattr(module, name, os.path.join(tmp, name))
        agent_trace.TRACE_ENABLED = not before_enabled
        try:
            after_paths, after_enabled = _snapshot()
            leaked = [n for (m, n, old), (_m2, _n2, now)
                      in zip(before_paths, after_paths) if old != now]
            self.assertTrue(leaked, "探针自己没泄漏成功，守卫就是装饰")
            self.assertNotEqual(after_enabled, before_enabled)
        finally:
            for module, name, old in before_paths:
                setattr(module, name, old)
            agent_trace.TRACE_ENABLED = before_enabled


if __name__ == "__main__":
    unittest.main()
