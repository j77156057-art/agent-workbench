# -*- coding: utf-8 -*-
"""治标两件：顶层 code_root 与项目不一致时，以项目为准 + 状态可被发现。

## 背景（`docs/known-defect-code-root-scope-too-coarse.md` §5）

实测事故：一个探针目录被设成**全局** code_root，四天后同一项目的另一个会话
问别的事，中间件某一环没设成上下文 -> 静默用全局残留 -> 检索器在 mean 题的
`calc.py` 底下搜 mean，5 个子任务 0/5 全败。

治本已由 `bbed9b8` 完成（优先级改成 会话 > 项目 > 全局）。这里做**治标**两件：

1. `config.reconcile_code_root_with_project()` —— 顶层与项目不一致时以项目为准。
   仍需要它的原因：**启动迁移**与**非请求场景**（测试/直调/MCP）走不到项目
   上下文，顶层值仍是全局残留。
2. `/api/code_root_status` —— 只读端点，把三层来源与一致性判断暴露出来，
   前端据此在界面上标出"检索可能跑在旧目录"。

## 契约

* 顶层与项目一致 -> 不改、`changed=False`；
* 不一致 -> 顶层被改写成项目 root，且回报 before/after；
* 无当前项目 / 项目功能关闭 -> **什么都不做**（单项目模式下顶层是唯一真源）；
* 只在显式调用时执行，导入期不偷偷改；
* 端点只读，且任何一层读不到都如实回报，不猜。

## ⚠ 治标第一件只做了一半（2026-10-11 撤回调用点）

`reconcile_code_root_with_project()` 写好并验证过，但**启动迁移里不调用它** ——
实测第一版在那里无条件跑，`tests/test_api_model_config.py::
test_restart_loads_key_from_current_project_not_stale_code_root` 立刻红：
那条判据**故意**造出「向后兼容指针指向 app、注册表指向 game」的形状，用来
验证「key 从当前项目读，不被 code_root 带偏」。刷掉顶层 stale 值等于抹掉
它要测的前提。

所以：**顶层 stale 值在启动路径上是被依赖的兼容形状，不是要治标的病。**
函数保留供显式调用（如将来某个明确的"清理残留"入口），
当前产品行为 = 治本（`bbed9b8` 的 会话 > 项目 > 全局）+ 只读状态端点。
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="docmind_reconcile_"))
os.environ["DOCMIND_STATE_DIR"] = str(_SANDBOX)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                       # noqa: E402
import projects as P                # noqa: E402
import api                          # noqa: E402  （端点类用例要用 api.app）


def tearDownModule():
    shutil.rmtree(_SANDBOX, ignore_errors=True)


class ReconcileTests(unittest.TestCase):
    """`config.reconcile_code_root_with_project`。"""

    def setUp(self):
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        config.STATE_FILE = str(_SANDBOX / "state.json")
        config._RUNTIME.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)

    def _mkproj(self, name):
        d = _SANDBOX / ("p-%s" % name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_no_change_when_consistent(self):
        """一致时不该动顶层。"""
        d = self._mkproj("same")
        pid = P.ensure_project(str(d))
        P.set_current(pid)
        config._RUNTIME["code_root"] = str(d)
        out = config.reconcile_code_root_with_project(reason="test")
        self.assertTrue(out["ok"])
        self.assertFalse(out["changed"])
        self.assertEqual(config._RUNTIME["code_root"], str(d))

    def test_inconsistent_top_is_overridden_by_project(self):
        """**核心判据**：顶层是残留、项目是别的 -> 顶层被改写成项目 root。"""
        d = self._mkproj("real")
        stale = self._mkproj("stale")
        pid = P.ensure_project(str(d))
        P.set_current(pid)
        config._RUNTIME["code_root"] = str(stale)      # 全局残留
        out = config.reconcile_code_root_with_project(reason="test")
        self.assertTrue(out["changed"], "没改 —— 残留会继续害人")
        self.assertEqual(out["before"], str(stale))
        self.assertEqual(out["after"], str(d))
        self.assertEqual(config._RUNTIME["code_root"], str(d))

    def test_no_current_project_does_nothing(self):
        """无当前项目：单项目语义下顶层是唯一真源，不许改。

        ⚠ 两条实测教训：
        1. `projects._STATE` 不存在，清 current 要走 `config.save_state`；
        2. 就算清了，`current_project_id()` 仍会**回落取 last_opened 最新者**，
           所以这里用一个**空的注册表 + 空 current** 的隔离状态文件来造
           「无当前项目」，而不是跟回落逻辑较劲。
        """
        empty = _SANDBOX / ("nocur-%s" % self.id().rsplit(".", 1)[-1])
        empty.mkdir(parents=True, exist_ok=True)
        state = empty / "state.json"
        state.write_text(json.dumps({"code_root": "D:/Whatever"}), encoding="utf-8")
        old_file, old_rt = config.STATE_FILE, dict(config._RUNTIME)
        try:
            config.STATE_FILE = str(state)
            config._RUNTIME.clear()
            config._RUNTIME["code_root"] = "D:/Whatever"
            out = config.reconcile_code_root_with_project(reason="test")
            self.assertFalse(out.get("changed"))
            self.assertEqual(config._RUNTIME["code_root"], "D:/Whatever")
            self.assertIn("无当前项目", out.get("note", ""))
        finally:
            config.STATE_FILE = old_file
            config._RUNTIME.clear()
            config._RUNTIME.update(old_rt)

    def test_projects_disabled_does_nothing(self):
        """`DOCMIND_PROJECTS=0` 一键回滚：顶层是唯一真源。

        ⚠ 必须 mock 掉 `current_project_id`。原版只 mock env —— 而
        `enabled()=False` 时 `current_project_id()` 返回 `LEGACY_ID`
        （见 projects.py 的分支），于是函数在第 32 行就被挡住，
        第 28 行的 `enabled()` 检查**根本没被执行到**。
        实测把第 28 行改成 `if False`，这条判据照样绿 —— 冗余保护掩护了它。
        让 `current_project_id()` 仍有返回值，才能单独验 `enabled()` 这一道。
        """
        d = self._mkproj("flag")
        pid = P.ensure_project(str(d))
        P.set_current(pid)
        config._RUNTIME["code_root"] = "D:/Other"
        with mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "0"}), \
                mock.patch.object(P, "current_project_id", return_value=pid):
            self.assertFalse(P.enabled(), "自检：mock 没生效")
            out = config.reconcile_code_root_with_project(reason="test")
            self.assertFalse(out.get("changed"))
            self.assertEqual(config._RUNTIME["code_root"], "D:/Other")
            self.assertIn("项目功能关闭", out.get("note", ""))

    def test_empty_top_is_left_alone(self):
        """顶层本来就是空：没什么可对账的，不许硬塞一个值进去。"""
        d = self._mkproj("emptytop")
        pid = P.ensure_project(str(d))
        P.set_current(pid)
        config._RUNTIME.pop("code_root", None)
        out = config.reconcile_code_root_with_project(reason="test")
        self.assertFalse(out.get("changed"))
        self.assertNotIn("code_root", config._RUNTIME,
                         "顶层空时被硬塞了值 —— 那是静默改写")

    def test_allow_override_false_leaves_explicit_value(self):
        """**「显式设置优先」是铁律**，高于治标本身。

        为什么必须有这条：第一版 reconcile 无条件把顶层刷成项目 root，
        结果 4 条 `test_explicit_code_root_*` 判据立刻红 —— 那是我引入的
        真回归。「显式 > 治标」这条优先级必须由 API 保证，不能只靠调用方记得。
        """
        d = self._mkproj("override")
        pid = P.ensure_project(str(d))
        P.set_current(pid)
        config._RUNTIME["code_root"] = "D:/UserExplicit"
        out = config.reconcile_code_root_with_project(reason="test",
                                                      allow_override=False)
        self.assertFalse(out.get("changed"))
        self.assertEqual(config._RUNTIME["code_root"], "D:/UserExplicit",
                         "allow_override=False 时顶层还是被改了")
        self.assertIn("显式设置优先", out.get("note", ""))
        # 反过来：默认（True）时仍会改
        out2 = config.reconcile_code_root_with_project(reason="test")
        self.assertTrue(out2.get("changed"))
        self.assertEqual(config._RUNTIME["code_root"], str(d))

    def test_import_does_not_reconcile(self):
        """导入期不许偷偷改 —— 那会抹掉用户手动设过顶层的意图。

        这里验的是「没有隐式调用」：`reconcile_code_root_with_project`
        只应在显式调用点（启动迁移之后）出现。
        """
        src = pathlib.Path(config.__file__).read_text(encoding="utf-8",
                                                      errors="replace")
        calls = [i + 1 for i, l in enumerate(src.splitlines())
                 if "reconcile_code_root_with_project(" in l
                 and "def " not in l and "reason=" in l]
        for ln in calls:
            self.assertIn("startup", src.splitlines()[ln - 1],
                          "第 %d 行有非 startup 的隐式调用" % ln)


class StatusEndpointTests(unittest.TestCase):
    """`/api/code_root_status`（只读）。"""

    def setUp(self):
        self._old_runtime = dict(config._RUNTIME)
        self._old_state_file = config.STATE_FILE
        self._old_ctx = (config._CTX_CODE_ROOT.get(),
                         config._CTX_SESSION_CODE_ROOT.get())
        config.STATE_FILE = str(_SANDBOX / "state.json")
        config._RUNTIME.clear()
        # ⚠⚠ 两个 contextvar 都必须存-还，且 setUp 里**先清空**。
        # 不清就会读到别的测试文件留下的上下文（实测：`source` 报成
        # `project_context` 而本用例根本没设过它）。
        # 这类"加了一层上下文"的状态，凡经手它的测试都必须自己清干净。
        config._CTX_CODE_ROOT.set(None)
        config._CTX_SESSION_CODE_ROOT.set(None)
        d = _SANDBOX / ("ep-%s" % self.id().rsplit(".", 1)[-1])
        d.mkdir(parents=True, exist_ok=True)
        self.root = d
        self.pid = P.ensure_project(str(d))
        P.set_current(self.pid)
        self.addCleanup(self._restore)

    def _restore(self):
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        config._CTX_CODE_ROOT.set(self._old_ctx[0])
        config._CTX_SESSION_CODE_ROOT.set(self._old_ctx[1])

    def _get(self):
        from fastapi.testclient import TestClient
        return TestClient(api.app).get("/api/code_root_status")

    def test_reports_effective_and_source(self):
        """经 TestClient 进去会走项目中间件，所以 source 是 `project_context`。

        ⚠ 第一版我断言 `source == "global"` —— 那是错的：中间件
        `set_context_code_root(root)` 会在请求进入时设上 `_CTX_CODE_ROOT`，
        TestClient 也在链路上。这里按**真实链路**断言。
        """
        config._RUNTIME["code_root"] = str(self.root)
        r = self._get().json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["effective"], str(self.root))
        self.assertEqual(r["source"], "project_context")
        self.assertEqual(r["project_root"], str(self.root))
        self.assertFalse(r["mismatch_top_vs_project"])

    def test_flags_mismatch(self):
        """**核心判据**：顶层是残留 -> `mismatch_top_vs_project=True` 且给出建议值。"""
        stale = _SANDBOX / ("stale-%s" % self.id().rsplit(".", 1)[-1])
        stale.mkdir(parents=True, exist_ok=True)
        config._RUNTIME["code_root"] = str(stale)
        r = self._get().json()
        self.assertTrue(r["mismatch_top_vs_project"],
                        "顶层与项目不一致却没标出来 —— 前端没法响这一声")
        self.assertEqual(r["suggested"], str(self.root))
        self.assertIn("以项目为准", r["hint"])

    def test_consistent_reports_no_mismatch(self):
        config._RUNTIME["code_root"] = str(self.root)
        r = self._get().json()
        self.assertFalse(r["mismatch_top_vs_project"])
        self.assertEqual(r["hint"], "")

    def test_is_read_only(self):
        """只读：调用前后顶层值不许被改。"""
        stale = _SANDBOX / ("ro-%s" % self.id().rsplit(".", 1)[-1])
        stale.mkdir(parents=True, exist_ok=True)
        config._RUNTIME["code_root"] = str(stale)
        before = dict(config._RUNTIME)
        self._get()
        self.assertEqual(config._RUNTIME, before,
                         "端点改了状态 —— 它声明是只读的")

    def test_layers_are_reported(self):
        """三层都要如实回报，缺的报空串，不许猜。"""
        config._RUNTIME["code_root"] = str(self.root)
        r = self._get().json()
        self.assertEqual(set(r["layers"]), {"session", "project_context", "global"})
        self.assertEqual(r["layers"]["global"], str(self.root))


if __name__ == "__main__":
    import api
    unittest.main(verbosity=2)
