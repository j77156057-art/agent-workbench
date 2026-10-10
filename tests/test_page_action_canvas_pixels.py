# -*- coding: utf-8 -*-
"""canvas 类页面：画面变了必须被看见，且不许把"没得比"说成"没变"。

## 实测事故（2026-10-10）

交付代理操作 Godot Web 导出页面（`http://127.0.0.1:8099/`）：

    click #canvas      -> 「没有观察到变化」
    press ArrowRight   -> 「没有观察到变化」
    press ArrowUp      -> 「没有观察到变化」

三张截图大小分别是 **14914 / 14728 / 14445 字节** —— 画面确实在变。
但工具仍说没变，Agent 只能拿它当负面证据，最后卡在 "PIL not available"，
**任务结论悬空**。

根因：`agent_runtime/page_action.py:_verdict` 只看
`mutations / console / failed / runtime / href / title` 六项。
截图照了（`_capture`），**但从不参与判定**。canvas 类页面
（Godot/WebGL 游戏、地图、可视化）的点击与按键本就不产生 DOM 变更，
于是"真的动了"和"什么都没发生"回报一模一样。

## 契约

* 同一会话内，前后两帧截图摘要不同 -> `effect: "pixels"`，`ok: True`；
* 摘要相同且 DOM 也没变 -> 仍是 `no_change`（不许虚报生效）；
* **本会话第一张截图** -> 不许说"画面没变"，必须标明"不构成证据"；
* 没要求截图 -> 不产生 `pixels` 信号（不许凭空多一个"生效"）；
* `error_seen` / 离开回环 -> 仍然压过 `pixels`（安全边界优先）。
"""

import base64
import hashlib
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                                   # noqa: E402
from agent_runtime import page_action as pa      # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="docmind_canvas_"))


def tearDownModule():
    shutil.rmtree(SB, ignore_errors=True)


class _FakeDevtools:
    """最小 CDP 替身：只实现 page_action 用到的几个面。

    ⚠ `_signals` 的 mutations 必须与 `_BEFORE` 一致 —— `_verdict` 有一条
    「动作后 mutations 比动作前小 => reloaded」。我的 fake 一开始恒返回 0，
    而 `_BEFORE` 是 3，于是每条用例都被判 reloaded（测试数据错，不是代码错）。
    """

    def __init__(self, shots, mutations=3):
        self._shots = list(shots)
        self._mutations = mutations
        self.console = []
        self.failed_requests = []
        self.error_events = 0
        self.failure_events = 0
        self.runtime_events = 0
        self.calls = 0

    def call(self, method, params=None):
        if method != "Page.captureScreenshot":
            return {}
        self.calls += 1
        if not self._shots:
            return {}
        return {"data": base64.b64encode(self._shots.pop(0)).decode("ascii")}

    def drain(self, seconds):
        return None

    def evaluate(self, js, args=None):
        return {"href": "http://127.0.0.1:8099/", "title": "StarVoyager",
                "mutations": self._mutations}


def _row(shots):
    devtools = _FakeDevtools(shots)
    row = pa._Session("p1", None, devtools, SB, "http://127.0.0.1:8099/", {}, None, None)
    return row


_BEFORE = {"mutations": 3, "console": 0, "failed": 0, "runtime": 0,
           "href": "http://127.0.0.1:8099/", "title": "StarVoyager"}


def _finish(row, screenshot=True):
    """跑一次 `_finish`，返回结果 dict。"""
    return pa._finish(row, "press", dict(_BEFORE), settle_ms=100,
                      screenshot=screenshot, matched={"tag": "document"})


class CanvasPixelsVerdictTests(unittest.TestCase):
    """画面变了要被判成生效。"""

    def setUp(self):
        self._old = config.get_runtime("code_root")
        config.set_runtime("code_root", str(SB))
        self.addCleanup(config.set_runtime, "code_root", self._old)

    def test_different_frames_are_pixels(self):
        """**核心判据**：DOM 没变但两帧不同 -> `pixels` + ok。"""
        row = _row([b"frame-A", b"frame-B"])
        _finish(row)                      # 第一帧：建立基线，no prev
        r = _finish(row)                  # 第二帧：不同 -> pixels
        self.assertEqual(r["effect"], "pixels",
                         "前后两帧不同却仍判 no_change —— 那次事故的形态")
        self.assertTrue(r["ok"], "pixels 必须算生效")
        self.assertTrue(r["pixels_changed"])

    def test_same_frame_stays_no_change(self):
        """反向守卫：两帧相同且 DOM 没变 -> 仍是无变化，不许虚报生效。"""
        row = _row([b"same", b"same"])
        _finish(row)
        r = _finish(row)
        self.assertEqual(r["effect"], "no_change")
        self.assertFalse(r["ok"])
        self.assertFalse(r["pixels_changed"])

    def test_first_screenshot_is_not_evidence_of_no_change(self):
        """"没得比"必须说出来，不许读成"画面没变"。"""
        row = _row([b"only-one"])
        r = _finish(row)
        self.assertEqual(r["effect"], "no_change")
        self.assertTrue(r["screenshot"], "自检：这次确实截了图")
        self.assertFalse(r["prev_shot"], "自检：确实没有上一帧")
        text = pa.render(r)
        self.assertIn("不构成证据", text,
                      "'画面没变'在只有一帧时不是证据，必须标明")

    def test_without_screenshot_no_pixels_signal(self):
        """没要求截图 -> 没有 pixels 信号，不许凭空多一个"生效"。"""
        row = _row([])
        r = _finish(row, screenshot=False)
        self.assertEqual(r["effect"], "no_change")
        self.assertFalse(r["pixels_changed"])
        self.assertEqual(r["shot_digest"], "")

    def test_error_seen_beats_pixels(self):
        """安全边界：**同时**有新报错和画面变化时，必须仍是未通过。

        上一版只造了"有报错、没画面变化"的组合 —— 那时 verdict 本来就是
        error_seen、pixels 分支没触发，删掉 `ok = False` 那条也照样绿（恒真）。
        真正的风险组合是**两者同时发生**：canvas 游戏里一次误点既报错又动了画面。
        """
        row = _row([b"a", b"b"])
        _finish(row)                       # 第一帧，建立基线
        row.devtools.error_events = 1      # 动作后冒出一条 console error
        r = _finish(row)                   # 第二帧画面也变了
        self.assertEqual(r["effect"], "error_seen")
        self.assertTrue(r["pixels_changed"], "自检：画面确实也变了")
        self.assertFalse(r["ok"],
                         "pixels 把 error_seen 翻成了成功 —— 安全边界被击穿")

    def test_left_loopback_still_beats_pixels(self):
        """离开回环是安全边界，不能被画面变化洗白。"""
        row = _row([b"a", b"b"])
        _finish(row)
        # 让动作后的 href 变成非回环地址
        row.devtools.evaluate = lambda js, args=None: {
            "href": "https://example.com/", "title": "StarVoyager",
            "mutations": 3}
        r = _finish(row)
        self.assertTrue(r["left_loopback"], "自检：确实离开了回环")
        self.assertFalse(r["ok"], "离开回环仍算成功 —— 安全边界失效")

    def test_render_mentions_canvas_for_pixels(self):
        """措辞要能指导下一步，而不是只丢一个 effect 名。"""
        row = _row([b"x", b"y"])
        _finish(row)
        r = _finish(row)
        self.assertEqual(r["effect"], "pixels")
        text = pa.render(r)
        self.assertIn("画面变了", text)
        self.assertIn("DOM 没变", text,
                      "没说清'变了的是什么、没变的是什么' —— Agent 无法据此判断")

    def test_render_pixels_gives_actionable_guidance(self):
        """必须告诉 Agent 这类页面该以什么为准，否则它仍会去改 DOM。

        上一版只断 `"canvas" in text`，而 `outcome` 字典里本就有
        "canvas 类页面" 字样 —— 断言被别处满足，变异删掉真正那段解释时
        测试照样绿（恒真）。这里改成断**只有解释段才有的句子**。
        """
        row = _row([b"x", b"y"])
        _finish(row)
        r = _finish(row)
        text = pa.render(r)
        self.assertIn("要以这一条为准", text,
                      "没告诉 Agent 这类页面该以画面为准 —— 它会继续去改 DOM")

    def test_render_digest_is_shown_for_review(self):
        """摘要要出现在回报里，否则使用者无法自行复核两帧。"""
        row = _row([b"f1", b"f2"])
        a = _finish(row)
        b = _finish(row)
        text = pa.render(b)
        self.assertIn(a["shot_digest"], text, "回报里没有当前帧摘要")
        self.assertIn("上一帧", text, "回报里没有上一帧摘要")

    def test_digest_is_recorded_for_review(self):
        """摘要落进结果，供结构化消费方复核（也可自行比对文件）。"""
        row = _row([b"f1", b"f2"])
        a = _finish(row)
        b = _finish(row)
        self.assertTrue(a["shot_digest"])
        self.assertEqual(b["prev_shot"], a["shot_digest"],
                         "上一帧摘要没接上 —— 比对就断了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
