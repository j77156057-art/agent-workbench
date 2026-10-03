# -*- coding: utf-8 -*-
"""执行期该绑哪个根——这条错了，试做区隔离就是一句空话。

## 实测（不是推断）

修之前，连续两轮工作流的**每一次**工具调用都落在真实项目：

    wf-7cc2e86620ce   python_exec 自报「执行目录：D:\\WorkBuddy\\godot_sample
                      ← 不在试做区内，相对路径会写进真实项目」
    wf-db3caef9623f   6 次 before_tool 全部 @WorkBuddy/godot_sample

而在 `scoped_runner` 内部架的探针读到的是

    want == actual == …\\project_stages\\wf-db3caef9623f\\workspace   matches=True

也就是说 `set_context_code_root` **确实生效了**，紧接着 `runner` 进来第一件事
就把它盖回真实项目。探针的价值就在这儿：只验 `set_context_code_root` 本身成立
（此前三次静态排查就是这么下的结论）根本不足以说明问题，因为真正被覆盖的是
**调用它的那一层**。

修之后（`wf-3073fc5f2403`）：**24 次工具调用全部** `@wf-3073fc5f2403/workspace`。

## 为什么是「每次调用时才解析」而不是绑定时抓成字符串

试做区是在**执行时**才创建的（`_execute_unleased` 里
`if state.stage_enabled and not state.project_stage: create_stage(...)`），
而绑定发生在执行请求到达时、试做区存在**之前**。抓成字符串的话，试做区建好了
也没人重新读它——这正是原 bug 的第二层。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api_routes.agent import _stage_workspace_root  # noqa: E402

PROJECT = os.path.join(tempfile.gettempdir(), "wfstage-proj")


class StageRootWinsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "project_stages", "wf-1",
                                      "workspace")
        os.makedirs(self.workspace)
        self.state = {"workflow_id": "wf-1"}

    def _live(self, **over):
        base = {"project_stage": {"workspace_root": self.workspace}}
        base.update(over)
        return base

    def test_a_staged_workflow_binds_to_the_workspace(self):
        got = _stage_workspace_root(self.state, PROJECT,
                                    live_lookup=lambda: self._live())
        self.assertEqual(got, self.workspace,
                         "绑回真实项目 = 写操作绕过试做区 = 隔离形同虚设")

    def test_without_a_stage_it_falls_back_to_the_project_root(self):
        got = _stage_workspace_root(self.state, PROJECT,
                                    live_lookup=lambda: {})
        self.assertEqual(got, PROJECT)

    def test_a_stage_pointing_at_a_missing_directory_falls_back(self):
        """试做区目录被删了还硬绑那个路径，工具会全线报「文件不存在」。"""
        got = _stage_workspace_root(
            self.state, PROJECT,
            live_lookup=lambda: self._live(
                project_stage={"workspace_root": os.path.join(self.tmp, "nope")}))
        self.assertEqual(got, PROJECT)

    def test_a_broken_lookup_falls_back_instead_of_raising(self):
        """读不到持久状态不该让派发崩掉——退回项目根是明确的降级，不是静默。"""
        def boom():
            raise RuntimeError("state 读不出来")

        got = _stage_workspace_root(self.state, PROJECT, live_lookup=boom)
        self.assertEqual(got, PROJECT)

    def test_a_lookup_returning_none_is_handled(self):
        self.assertEqual(
            _stage_workspace_root(self.state, PROJECT, live_lookup=lambda: None),
            PROJECT)

    def test_no_workflow_id_means_no_lookup_and_the_project_root(self):
        """没有 wid 就无从查试做区，直接用项目根——不构造默认查找器。

        走默认路径（不注入 `live_lookup`）时若还去查，那次查询会打到
        `WORKFLOWS.get("")`，既是无效 IO 也可能误命中别的状态。
        """
        import api_routes.agent as mod

        calls = []
        original = mod.WORKFLOWS.get

        def spy(wid=""):
            calls.append(wid)
            return original(wid) if wid else {}

        mod.WORKFLOWS.get = spy
        try:
            self.assertEqual(_stage_workspace_root({}, PROJECT), PROJECT)
        finally:
            mod.WORKFLOWS.get = original
        self.assertEqual(calls, [], "没有 wid 还去查了")


class ItIsResolvedPerCallTests(unittest.TestCase):
    """根必须在**每次调用**时解析——这正是原 bug 的第二层。"""

    def test_a_stage_created_after_binding_is_picked_up(self):
        tmp = tempfile.mkdtemp()
        workspace = os.path.join(tmp, "ws")
        os.makedirs(workspace)
        live = {"project_stage": None}
        resolver = lambda: _stage_workspace_root(  # noqa: E731
            {"workflow_id": "wf-2"}, PROJECT, live_lookup=lambda: live)

        # 绑定那一刻：试做区还不存在。
        self.assertEqual(resolver(), PROJECT,
                         "试做区未建时应当是项目根")

        # 执行开始后试做区建好了 —— 同一个 resolver 必须立刻改口。
        live["project_stage"] = {"workspace_root": workspace}
        self.assertEqual(resolver(), workspace,
                         "试做区建好后仍绑真实项目——这就是原 bug："
                         "绑定时把根抓成了字符串，之后再没人重读")


if __name__ == "__main__":
    unittest.main()