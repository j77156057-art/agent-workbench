# -*- coding: utf-8 -*-
"""派生账本（上下文快照）写不进去时，不许把主状态一起赔掉。

## 实测（`wf-3784257fd6d1`）

`/choose` 返回 **500**，栈是：

    game_workflow.choose
      game_workflow._resume_gate
        game_workflow._invoke_graph
          game_workflow._save                     ← 1030 行
            context_router.compress_layers_durable
              context_router.submit
                context_router._write
    PermissionError: [WinError 5] 拒绝访问。
      '.docmind\\.context-compression-ulve8jkn.tmp' -> '.docmind\\context_compression.json'

## 为什么这一条特别糟

`_save` 里压缩在**写状态之前**，而且**没有 try**。于是

    一个为「跨窗口交接」准备的派生账本写不进去
        → 整个 _save 抛异常
            → 主状态根本没落盘
                → /choose 500

写状态失败有 `strict` 分支可以抛，但那是**有意的**；这里是在一个**增强功能**
上意外抛的，代价是丢掉主记录。

## 两处修复

1. `_write` 里 `os.replace` 加有界退避重试——Windows 上目标被别的句柄读着时
   本来就会抛 `PermissionError: [WinError 5]`，这不是逻辑错误。
2. `_save` 里压缩包 try/except，失败就标 `valid: False` 并如实发事件，
   状态照常落库。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime import context_router as CR  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402


class ReplaceRetryTests(unittest.TestCase):
    """`_write` 必须扛住 Windows 的 `PermissionError`。"""

    class _Flaky:
        def __init__(self, real, failures):
            self.real = real
            self.failures = failures
            self.calls = 0

        def replace(self, src, dst):
            self.calls += 1
            if self.calls <= self.failures:
                raise PermissionError(5, "拒绝访问", dst)
            return self.real(src, dst)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # `JsonCompressionWorker` 把 `db_path` 派生为 JSON 路径
        # （去掉 .sqlite3 后缀或直接 + ".json"），所以这里传不带后缀的基名。
        self.store = CR.JsonCompressionWorker(os.path.join(self.tmp, "store"))
        self.path = self.store.path

    def _write(self, payload):
        obj = self.store
        real = os.replace
        flaky = self._Flaky(real, 2)
        os.replace = flaky.replace
        try:
            obj._write(payload)
        finally:
            os.replace = real
        return flaky.calls

    def test_it_retries_and_eventually_succeeds(self):
        calls = self._write({"a": 1})
        self.assertEqual(calls, 3,
                         "没有重试——一次瞬时的句柄占用就让整次保存失败")
        self.assertTrue(os.path.isfile(self.path))

    def test_the_content_actually_landed(self):
        """重试不是重点，**内容写对了**才是。"""
        import json

        self._write({"hello": "世界"})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"hello": "世界"})

    def test_it_gives_up_eventually_instead_of_looping_forever(self):
        obj = self.store
        real = os.replace

        def always(src, dst):
            raise PermissionError(5, "拒绝访问", dst)

        os.replace = always
        try:
            with self.assertRaises(PermissionError):
                obj._write({"a": 1})
        finally:
            os.replace = real

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        real = os.replace
        os.replace = lambda s, d: (_ for _ in ()).throw(
            PermissionError(5, "拒绝访问", d))
        try:
            with self.assertRaises(PermissionError):
                self.store._write({"a": 1})
        finally:
            os.replace = real
        leftovers = [n for n in os.listdir(self.tmp)
                     if n.startswith(".context-compression-")]
        self.assertEqual(leftovers, [],
                         "临时文件没清掉——每次失败都会在 .docmind 里留一份垃圾")


class SaveSurvivesCompressionFailureTests(unittest.TestCase):
    """压缩失败时状态必须照常落库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manager = GameWorkflowManager(self.tmp)
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]

    def _break_compression(self):
        import agent_runtime.game_workflow as gw

        original = gw.compress_layers_durable

        def boom(*a, **k):
            raise PermissionError(5, "拒绝访问", "context_compression.json")

        gw.compress_layers_durable = boom
        self.addCleanup(lambda: setattr(gw, "compress_layers_durable", original))

    def test_the_state_is_still_written_to_disk(self):
        self._break_compression()
        self.manager._save(self.manager._load(self.wid))
        path = self.manager._path(self.wid)
        self.assertTrue(os.path.isfile(path),
                        "压缩失败把主状态也赔掉了——这就是 /choose 500 的原因")

    def test_the_saved_state_is_valid_json_and_complete(self):
        import json

        self._break_compression()
        self.manager._save(self.manager._load(self.wid))
        path = self.manager._path(self.wid)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("workflow_id"), self.wid)

    def test_it_says_so_instead_of_failing_silently(self):
        """快照丢了要让人看见——否则跨窗口交接悄悄少东西，没人知道。"""
        self._break_compression()
        self.manager._save(self.manager._load(self.wid))
        kinds = [e.get("kind")
                 for e in self.manager.get(self.wid)["events"]]
        self.assertIn("context_snapshot_unavailable", kinds,
                      "快照写不进去却一声不响——跨窗口交接会少东西而没人知道")

    def test_the_snapshot_is_marked_invalid_rather_than_left_stale(self):
        """不能保留上一轮的快照装作成功——那会让下游引用过期内容。"""
        self._break_compression()
        state = self.manager._load(self.wid)
        state.context_layers["snapshots"] = {"旧的东西": "不该留下"}
        self.manager._save(state)
        layers = self.manager.get(self.wid).get("context_layers") or {}
        self.assertFalse((layers.get("snapshot_meta") or {}).get("valid", False),
                         "压缩失败了还标 valid——下游会当它可信")
        self.assertEqual(layers.get("snapshots"), {},
                         "留着上一轮的快照 = 让下游引用过期内容")

    def test_normal_saves_still_mark_it_valid(self):
        state = self.manager._load(self.wid)
        self.manager._save(state)
        layers = self.manager.get(self.wid).get("context_layers") or {}
        self.assertTrue((layers.get("snapshot_meta") or {}).get("valid"),
                        "正常路径反而标成不可用了")


if __name__ == "__main__":
    unittest.main()