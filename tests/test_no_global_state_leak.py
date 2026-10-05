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
import config  # noqa: E402
import sessions  # noqa: E402
import tools  # noqa: E402
from tests import test_chat_sse, test_error_budget, test_llm_stub  # noqa: E402

# 只 import **模块**，不 import 具体用例类：`from tests.test_x import SomeTests`
# 会让那些用例在**本文件里被 pytest 再收集一遍**（第一版就这么写，结果 9 条真实
# 用例在这里各跑了第二遍 —— 位置不同、上下文不同，一旦红就是假红）。
# 仓库里的先例（test_agent_memory / test_weather_recovery / test_runtime_contracts）
# 都只 import 下划线前缀的辅助类，pytest 不收集那些。
# 要触发的类用属性去取。
_LLM_STUB_CASES = (test_llm_stub.ProviderPathTurnTests,)
_CHAT_SSE_CASES = (test_chat_sse.RealTurnTests, test_chat_sse.MockProviderTests)
# 第二次现身：`test_error_budget` 把 `code_root` 留在一个**已被删掉的**临时目录上。
_ERROR_BUDGET_CASES = (test_error_budget.ErrorTextTests,
                       test_error_budget.StubMustBeCheaperThanContentTests)

# 这些用例 setUp 里动的全局量：三个路径 + TRACE_ENABLED。
_GLOBALS = ((agent_trace, "TRACE_FILE"),
            (sessions, "SESSIONS_DIR"),
            (agent_memory, "DB_PATH"))

# `code_root` 不在上面的 getattr 体系里（它在 config._RUNTIME 上，且经请求上下文
# 回落），单独看。附带 `tools` 的已读文件表 —— test_error_budget 也动它。
_CODE_ROOT = "code_root"


def _code_root():
    return config.get_runtime(_CODE_ROOT)


def _snapshot():
    return ([(m, n, getattr(m, n)) for m, n in _GLOBALS],
            agent_trace.TRACE_ENABLED, _code_root())


class _Case:
    """借一个真实用例的 setUp，但把测试体换成空转。

    `run_body=False` 时只跑 setUp/doCleanups —— 用于 setUp 里就设全局量的用例。
    `test_error_budget.StubMustBeCheaperThanContentTests` 不行：它把 code_root
    设在 `_setup()` 里、由**测试体**调用，所以必须真跑测试体，否则守卫看不到。
    """

    def __init__(self, cls, run_body=False, method=None):
        self._cls = cls
        self._run_body = run_body
        self._method = method

    def run(self, result):
        if self._method is None:
            names = [n for n in dir(self._cls)
                     if n.startswith("test") and callable(getattr(self._cls, n))]
            method = names[0] if (self._run_body and names) else "runTest"
        else:
            method = self._method
        case = self._cls(method)
        case.setUp()
        try:
            if method != "runTest":
                try:
                    getattr(case, method)()
                except Exception:  # noqa: BLE001
                    pass          # 测试体本身红不红与本守卫无关
        finally:
            case.doCleanups()
            case.tearDown()


class GlobalStateLeakTests(unittest.TestCase):
    """反向守卫：这些用例**不许**把全局量留给后续用例。"""

    def _assert_restored(self, cls, run_body=False, method=None):
        before_paths, before_enabled, before_root = _snapshot()
        result = unittest.TestResult()
        _Case(cls, run_body=run_body, method=method).run(result)
        after_paths, after_enabled, after_root = _snapshot()
        who = "%s.%s" % (cls.__name__, method) if method else cls.__name__
        leaked = [("%s.%s" % (m.__name__, n), old, now)
                  for (m, n, old), (_m2, _n2, now)
                  in zip(before_paths, after_paths) if old != now]
        self.assertEqual(
            leaked, [],
            "%s 把全局路径改了没还原：%s"
            % (who, "; ".join("%s %r -> %r" % (n, o, w) for n, o, w in leaked)))
        self.assertEqual(
            after_enabled, before_enabled,
            "%s 改了 agent_trace.TRACE_ENABLED 没还原：%r -> %r"
            % (who, before_enabled, after_enabled))
        # code_root 单独比。它留在一个已被 `tmp.cleanup` 删掉的路径上时，后续用例会
        # `in_root=True` 而 `exists=False` —— 引用路径是按**仓库相对路径**写的，
        # 在临时目录里当然打不开（evidence_audit 那条红就是这么来的）。
        #
        # 注意它跟 `references_exist` 的关系不是「文件存不存在」那么直接：
        # `output_audit.py:337-346` 在 `root` 为空（**没有**代码根）时会**主动**把
        # `gate_checks["references_exist"]` 改写成 `successful_read`、给 `no_root`
        # 这个 PARTIAL 理由。所以 code_root 为空时判据反而是宽松的；一旦它变成
        # 一个**非空**的错路径，就会走进严格的文件系统分支而判红。
        self.assertEqual(
            after_root, before_root,
            "%s 改了 code_root 没还原：%r -> %r"
            % (who, before_root, after_root))

    def test_llm_stub_turn_tests_restore_global_paths(self):
        for cls in _LLM_STUB_CASES:
            with self.subTest(cls=cls.__name__):
                self._assert_restored(cls)

    def test_chat_sse_turn_tests_restore_global_paths(self):
        # setUp 定义在基类 ChatTurnCase 上，两个子类都继承它、都会泄漏。
        for cls in _CHAT_SSE_CASES:
            with self.subTest(cls=cls.__name__):
                self._assert_restored(cls)

    def test_error_budget_tests_restore_code_root(self):
        """第二次现身：`test_error_budget` 把 `code_root` 留在一个**已被删掉的**路径上。

        实测（`.tmp/probe_coderoot_hunt.py`）：三条用例各自留下一条
        `None -> '' -> tmpqotxv6ex -> tmpmzkfy7uq -> tmp3d6tx0v9`，最后一个目录
        已被 `tmp.cleanup` 删掉。

        后果：`tests/test_evidence_audit_source.py` 的「四次成功 read_file 之后仍被判
        未核验」**单跑绿、与本文件同跑就红**，值是
        `{'references_exist': False, 'in_root': True, 'exists': False}`。
        同一文件里 `ErrorTextTests` 有还原、`StubMustBeCheaperThanContentTests._setup`
        没有 —— 同文件内不一致。

        必须 `run_body=True`：这些用例把 code_root 设在 `_setup()` 里、由测试体调用，
        只跑 setUp 的话守卫根本看不到。

        **每条用例都要跑**，不能只跑第一条：第一版守卫只跑 `names[0]`，于是变异 B
        （`_setup` 在一个**循环里被调 6 次**、`lambda` 晚绑定导致 6 个 cleanup 全还原
        成同一个值）**没被咬住** —— 而那正是我自己的第一版修法。
        """
        loader = unittest.TestLoader()
        for cls in _ERROR_BUDGET_CASES:
            names = [n for n in dir(cls)
                     if n.startswith("test") and callable(getattr(cls, n))]
            for method in sorted(names):
                with self.subTest(cls=cls.__name__, method=method):
                    self._assert_restored(cls, run_body=True, method=method)

    def test_this_module_does_not_recollect_the_cases_it_touches(self):
        """**反向守卫**：本模块只许持有这些类的引用，不许把它们变成自己的用例。

        写成 `from tests.test_x import SomeTests` 时，pytest 会把那些用例在本文件里
        再收集一遍 —— 12 passed 里有 9 条是重复的，而且一旦红就是假红（本文件的
        位置/上下文跟 `test_chat_sse.py` 里的真实运行不同）。
        仓库先例：`test_agent_memory` / `test_weather_recovery` / `test_runtime_contracts`
        都只 import 下划线前缀的辅助类。
        """
        here = set(globals())
        for cls in _LLM_STUB_CASES + _CHAT_SSE_CASES + _ERROR_BUDGET_CASES:
            self.assertNotIn(cls, here,
                             "%s 被直接 import 进本文件命名空间 -> 会被二次收集，"
                             "请改用模块属性访问" % cls.__name__)

    def test_the_guard_would_notice_a_leak(self):
        """**反向守卫自身**：先故意泄漏一次，确认上面那条断言真的会红。

        没有这一条，「断言其实没断言到东西」和「修好了」会长得一模一样 ——
        这正是 `test_no_match_is_not_failure` 踩过的坑（守卫与实现同源失效）。
        """
        before_paths, before_enabled, before_root = _snapshot()
        tmp = os.path.join(os.environ.get("TEMP", "."), "dm_fake_leak_probe")
        for module, name in _GLOBALS:
            setattr(module, name, os.path.join(tmp, name))
        agent_trace.TRACE_ENABLED = not before_enabled
        config.set_runtime("code_root", tmp)
        try:
            after_paths, after_enabled, after_root = _snapshot()
            leaked = [n for (m, n, old), (_m2, _n2, now)
                      in zip(before_paths, after_paths) if old != now]
            self.assertTrue(leaked, "探针自己没泄漏成功，守卫就是装饰")
            self.assertNotEqual(after_enabled, before_enabled)
            self.assertNotEqual(after_root, before_root,
                                "探针没能改到 code_root，那条 code_root 断言就没被验过")
        finally:
            for module, name, old in before_paths:
                setattr(module, name, old)
            agent_trace.TRACE_ENABLED = before_enabled
            config.set_runtime("code_root", before_root)


if __name__ == "__main__":
    unittest.main()
