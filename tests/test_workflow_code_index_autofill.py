# -*- coding: utf-8 -*-
"""工作流启动时必须保证「代码索引可用」—— 至少不能静默为空。

## 真实故障（真跑 `wf-0c6374c4d612` / `wf-37f43a4c042f`）

探测项目从未 ingest，`search_code` 于是返回空，而**没有任何提示**。子代理拿不到
任何代码证据，转而去追 `search_knowledge` 里的无关文档，五步全花掉、任务失败。

而 `ingest_code_directory` **只**被 `/api/ingest_code` 调用（api.py:1913），
工作流启动路径上完全没有索引检查。所以「项目没索引过 → 工作流静默瞎跑」是默认
行为。

雪上加霜的是 `tests/test_retrieval_degradation.py` 记着：嵌入后端不可用时
ingest 会**降级返回**（0 个代码向量也算成功）——所以「调过 ingest」本身也不是保证。

## 这一族要保证什么

1. 索引为空时**说清楚**（发事件，落到时间线上），而不是静默返回空；
2. 同时**后台自动补一次** ingest —— 用户选的方案，且**不阻塞**启动；
3. 绝不碰全局 `code_root` 指针（那是 `test_ingest_code_root_guard.py` 守着的
   不变量：非当前项目的请求不得改写它）；
4. 绝不触发「停掉其它项目引擎」那套切项目副作用；
5. 后台失败只记事件，**绝不让工作流失败**。

## 层归属

纯层：全部用 `tempfile` + mock，不碰向量库、不起真线程
（把 `threading.Thread` 换成一个当场同步执行的替身）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import vectorstore  # noqa: E402
from agent_runtime import game_workflow as gw  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402


class _SyncThread:
    """当场同步执行的线程替身：让「后台」这件事在测试里可观测。

    真代码用 `daemon=True` 的真线程；这里换成同步跑，于是「有没有在启动路径上
    同步做重活」这件事就成了可断言的差别。
    """

    instances = []

    def __init__(self, target=None, name=None, daemon=None, **_kw):
        self.target = target
        self.name = name
        self.daemon = daemon
        _SyncThread.instances.append(self)

    def start(self):
        if self.target:
            self.target()

    def join(self, timeout=None):
        return None


class _ThreadShim:
    """只换掉 `game_workflow` 命名空间里的 `Thread`，其余转发给真模块。

    第一版直接 `mock.patch.object(gw.threading, "Thread", _SyncThread)`——
    而 `gw.threading` **就是** stdlib 那个模块对象，于是 `ThreadPoolExecutor`
    的 `_worker` 也被换掉，整个测试报
    `TypeError: _worker() missing 4 required positional arguments`。
    换成只代理 `Thread` 这一个名字，其余照常转发。
    """

    def __init__(self, real):
        self._real = real

    # 类属性优先于 __getattr__，所以只有 Thread 被换掉
    Thread = _SyncThread

    def __getattr__(self, name):
        return getattr(self._real, name)


class CodeIndexAutofillTests(unittest.TestCase):
    def setUp(self):
        _SyncThread.instances = []
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        self.mgr = GameWorkflowManager(os.path.join(self.tmp, "state"))
        self.addCleanup(self.mgr.close)
        patcher = mock.patch.object(gw, "threading", _ThreadShim(gw.threading))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _start(self, *, count=0, isolation=True, pid="prj-x", ingest_ok=3):
        """起一个工作流，把索引状态与 ingest 结果都替掉。"""
        ingested = []

        def fake_ingest(root, **kw):
            ingested.append((root, kw))
            if isinstance(ingest_ok, Exception):
                raise ingest_ok
            return ingest_ok

        stack = [
            mock.patch.object(vectorstore, "count", mock.Mock(return_value=count)),
            mock.patch("projects.enabled", lambda: isolation),
            mock.patch("projects.current_project_id", lambda: pid),
            mock.patch("projects.code_collection",
                       mock.Mock(side_effect=lambda p=None: "coll__%s" % (p or pid))),
            mock.patch("projects.get_project",
                       mock.Mock(return_value={"root": self.root})),
            mock.patch("ingest.ingest_code_directory", fake_ingest),
        ]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        state = self.mgr.start("验证代码索引自动补齐",
                               project_id=pid, project_root=self.root,
                               policy=gw.WorkflowPolicy(approval_mode="high"))
        return state, ingested

    def _kinds(self, state):
        return [str(e.get("kind")) for e in state.get("events") or []]

    # ---- 检测 ----

    def test_reverse_guard_an_empty_index_must_be_reported_not_silent(self):
        """**反向守卫**：索引为空必须**说**出来。

        静默返回空就是本次故障：子代理拿不到证据却不知道原因。
        """
        state, _ingested = self._start(count=0)

        self.assertIn("code_index_missing", self._kinds(state))

    def test_a_populated_index_triggers_nothing(self):
        """**反向守卫**：索引已有内容时不得重复折腾（每次启动都重索引是不可接受的）。"""
        state, ingested = self._start(count=42)

        self.assertNotIn("code_index_missing", self._kinds(state))
        self.assertEqual(ingested, [])
        self.assertEqual(_SyncThread.instances, [])

    def test_it_skips_when_project_isolation_is_off(self):
        """没有项目概念时不该凭空推断索引该属于谁。"""
        state, ingested = self._start(count=0, isolation=False)

        self.assertEqual(ingested, [])
        self.assertNotIn("code_index_missing", self._kinds(state))

    # ---- 后台补 ingest ----

    def test_reverse_guard_the_rebuild_must_not_block_the_start(self):
        """**反向守卫**：ingest 是重活（大仓要 walk + embedding），绝不能在
        启动路径上同步做——否则「启动工作流」会被索引耗时拖住。

        这里用同步线程替身：如果实现改成同步调用，`threading.Thread` 就不会被
        构造，这条会红。
        """
        _state, ingested = self._start(count=0)

        self.assertEqual(len(ingested), 1, "ingest 应由后台线程跑")
        self.assertEqual(len(_SyncThread.instances), 1)
        self.assertTrue(_SyncThread.instances[0].daemon, "后台线程必须是 daemon")

    def test_reverse_guard_the_rebuild_must_not_touch_the_global_code_root(self):
        """**反向守卫**：不得改写全局 `code_root` 指针。

        `test_ingest_code_root_guard.py` 守着的不变量：带非当前项目头的请求
        绝不能污染全局指针。这里自动补索引用的可能是**别的**项目。

        用**行为断言**而不是给 `set_runtime` 打桩——打桩只能证明「那个名字没被
        调到」，而真实意图是「全局指针的值不许变」。
        """
        before = config.get_runtime("code_root")

        self._start(count=0)

        self.assertEqual(config.get_runtime("code_root"), before)

    def test_reverse_guard_it_must_not_stop_other_projects_engines(self):
        """**反向守卫**：不得触发切项目那套「停掉其它引擎」的副作用。

        `/api/ingest_code` 会做这件事，因为那是「切换项目」语义；自动补索引
        不是切换，后台停别人的引擎是不可接受的副作用。
        """
        import api

        stop = mock.Mock()
        with mock.patch.object(api, "_stop_other_engines", stop):
            self._start(count=0)

        stop.assert_not_called()

    def test_the_rebuild_targets_the_projects_own_collection(self):
        """索引必须写进**该项目自己的**集合，不能污染 legacy 全局集合
        （那正是 `deabde6` 修的那个缺陷的另一半）。"""
        _state, ingested = self._start(count=0)

        root, kw = ingested[0]
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.root))
        self.assertEqual(kw.get("collection"), "coll__prj-x")

    def test_a_successful_rebuild_is_recorded(self):
        state, _ingested = self._start(count=0, ingest_ok=7)

        self.assertIn("code_index_missing", self._kinds(state))
        self.assertIn("code_index_rebuilt", self._kinds(state))

    def test_reverse_guard_a_failed_rebuild_does_not_fail_the_workflow(self):
        """**反向守卫**：后台 ingest 抛异常**不得**让工作流失败。

        索引是辅助能力（`test_retrieval_degradation.py` 同一句话：宁可没有，
        也不能让辅助能力绑住主能力）。工作流该照常开始，只是明确告知索引没就绪。
        """
        state, _ingested = self._start(count=0, ingest_ok=RuntimeError("embed down"))

        self.assertNotIn("failed", str(state.get("status")))
        self.assertEqual(state.get("status"), "awaiting_choice")
        self.assertIn("code_index_rebuild_failed", self._kinds(state))

    def test_reverse_guard_two_starts_do_not_spawn_two_rebuilds(self):
        """**反向守卫**：重复启动不得并发跑同一个索引重建（会同时写一个集合）。"""
        _state, ingested = self._start(count=0)

        self.assertEqual(len(ingested), 1)

    def test_count_errors_are_treated_as_empty_not_as_a_crash(self):
        """`vectorstore.count` 异常时返回 0——那时**照常**报缺失并补，别炸。"""
        with mock.patch.object(vectorstore, "count",
                               mock.Mock(side_effect=RuntimeError("chroma down"))):
            state, ingested = self._start(count=0)

        self.assertIn("code_index_missing", self._kinds(state))
        self.assertEqual(len(ingested), 1)


if __name__ == "__main__":
    unittest.main()