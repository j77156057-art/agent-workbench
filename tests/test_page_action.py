"""Wave13 回归：页面交互原语。

钉两件事：① 「点了到底有没有生效」的判定表不能含糊——no_change/error_seen 必须如实回，
隐藏/禁用/歧义元素必须拒绝执行；② 会话是长命的浏览器，回收证据必须是真的（作业对象解绑、
profile 释放），漏进程是这个仓库已经付过学费的事故。
浏览器相关用例在没有 Edge 时明确跳过，不假装通过。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import page_action as pa  # noqa: E402
from agent_runtime import visual_acceptance as visual  # noqa: E402


def _browser_available():
    try:
        return visual._edge_binary()
    except Exception:  # noqa: BLE001 - 没有就是没有
        return ""


BROWSER = _browser_available()

FIXTURE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>交互夹具</title></head><body>
<h1 id="head">计数 0</h1>
<button id="plus" data-testid="plus">加一</button>
<button id="broken">报错</button>
<button id="dupe">选项</button><button id="dupe2">选项</button>
<button id="off" disabled>禁用</button>
<div style="display:none"><button id="hid">隐藏</button></div>
<form id="form" action="/next.html"><input id="name" placeholder="名字"><button id="submit" type="submit">提交</button></form>
<div id="long">__LONG__</div>
<p id="echo">echo:</p>
<script>
  const input = document.getElementById('name');
  input.addEventListener('input', () => { document.getElementById('echo').textContent = 'echo:' + input.value; });
  document.getElementById('plus').addEventListener('click', () => {
    const n = Number(document.getElementById('head').dataset.n || 0) + 1;
    document.getElementById('head').dataset.n = n;
    document.getElementById('head').textContent = '计数 ' + n;
  });
  document.getElementById('broken').addEventListener('click', () => {
    console.error('夹具里的真实报错：无法继续');
    document.getElementById('head').style.color = 'red';
  });
</script></body></html>
"""

#: 一段【超过 4000 字】的正文，needle 落在第 4200 字之后：`mode: text` 因为上限读不到它，
#: `mode: contains` 必须读得到——这条差异就是两种模式的分界，也是"别拿整段文本判断在不在"
#: 的可执行版本。
LONG_TEXT = ("填" * 4200) + "紫终点-needle9" + ("绿" * 300)
FIXTURE_PAGE = FIXTURE_PAGE.replace("__LONG__", LONG_TEXT)


class FakeDevTools:
    """按脚本特征回话的假 CDP 通道：让判定表与派发顺序可以不启动浏览器就测到。"""

    def __init__(self, resolve=None, inspect=None, probe=10, probe_after=None, target=None,
                 error_on_drain=False, inspect_by_mode=None, corpus=None, full_text=None,
                 scanned_all=True):
        self.calls = []
        self.evaluations = []
        self.console = []
        self.failed_requests = []
        self.runtime_errors = []
        # 与 _DevTools 同形：列表会去重，事件计数不会。判定必须用后者。
        self.error_events = 0
        self.failure_events = 0
        self.runtime_events = 0
        self.resolve = {"count": 1, "nth": 0, "href": "http://127.0.0.1:1/", "title": "t",
                        "view": {"width": 960, "height": 540},
                        "matched": {"how": "selector", "tag": "button", "text": "加一", "id": "plus",
                                    "testid": "", "disabled": False, "x": 10, "y": 20, "w": 40,
                                    "h": 20, "visible": True},
                        "candidates": []}
        if resolve:
            self.resolve.update(resolve)
        self.inspect = {"count": 1, "found": True, "visible": True, "tag": "h1",
                        "href": "http://127.0.0.1:1/", "title": "t", "text": "计数 1"}
        if inspect:
            self.inspect.update(inspect)
        self.probe = probe
        self.probe_after = probe if probe_after is None else probe_after
        self._probe_calls = 0
        self.target = target or {"ok": True, "tag": "input", "focused": True, "value": "",
                                 "x": 30, "y": 30}
        self.error_on_drain = error_on_drain
        self.inspect_by_mode = inspect_by_mode or {}
        # corpus: {选择器: [每个元素的可见文字]}。命中数由假页面【真的数一遍】，不把结果写死成
        # 一个数——否则"搜错了字段/搜错了容器"这类错在假页面上根本测不出来。
        self.corpus = corpus or {}
        self.scanned_all = scanned_all
        if full_text is not None:
            # 模仿真实浏览器：页面里是全文，回传的 text 已被 slice 到 4000，全文长度另报
            self.inspect["text"] = full_text[:4000]
            self.inspect["full_length"] = len(full_text)

    def add_error(self, text="反复出现的同一条报错"):
        """模拟一条【重复】报错：列表去重、事件计数不去重——这正是判定必须看计数的原因。"""
        self.error_events += 1
        entry = {"level": "error", "text": text}
        if entry not in self.console:
            self.console.append(entry)

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {"data": ""}

    def evaluate(self, expression):
        self.evaluations.append(expression)
        if "MutationObserver" in expression:
            self._probe_calls += 1
            value = self.probe if self._probe_calls == 1 else self.probe_after
            return {"mutations": value, "observer": True}
        if "__docmindTarget = null" in expression:
            return dict(self.resolve)
        if "element-gone" in expression:
            return dict(self.target)
        if "elements_with_hit" in expression:      # CONTAINS_JS
            spec = json.loads(expression[expression.index("({") + 1:expression.rindex(")")])
            needle = str(spec.get("needle") or "")
            # corpus 值支持 "文字" 或 ("文字", hidden) ——假页面也得分清可见与不可见，
            # 否则"把不可见命中混进 hits"这种错在假页面上根本测不出来
            rows = [(row, False) if isinstance(row, str) else (row[0], bool(row[1]))
                    for row in self.corpus.get(spec.get("selector") or "body", [])]
            hits = hidden_hits = hidden_elements = with_hit = 0
            samples = []
            max_samples = int(spec.get("samples") or 0)      # 与真页面一样：上限由 spec 下发
            for text, hidden in rows:
                n = text.count(needle) if needle else 0
                if hidden:
                    hidden_hits += n
                    if n:
                        hidden_elements += 1
                    continue
                hits += n
                if n:
                    with_hit += 1
                    at = text.find(needle)
                    while at >= 0 and len(samples) < max_samples:
                        samples.append(text[max(0, at - 40): at + len(needle) + 40])
                        at = text.find(needle, at + len(needle))
            return {"needle": needle, "selector": spec.get("selector"),
                    "elements": len(rows), "elements_with_hit": with_hit, "hits": hits,
                    "hidden_hits": hidden_hits, "hidden_elements": hidden_elements,
                    "searched_chars": sum(len(text) for text, _ in rows),
                    "scanned_all": self.scanned_all, "samples": samples,
                    "href": "http://127.0.0.1:1/", "title": "t"}
        if "spec.mode === 'attr'" in expression:
            row = dict(self.inspect)
            # 按请求的 mode 分别回话：`<input>` 的 innerText 恒为空，只有 value 模式才拿得到
            # 内容。不区分的话"到底读的是哪个模式"这种错误在假页面上根本测不出来。
            mode = json.loads(expression[expression.index("({") + 1:expression.rindex(")")]).get("mode")
            if self.inspect_by_mode and mode in self.inspect_by_mode:
                row["text"] = self.inspect_by_mode[mode]
            return row
        return None

    def drain(self, seconds=0.5, **_kwargs):
        self.calls.append(("drain", {"seconds": seconds}))
        if self.error_on_drain:
            self.add_error()

    def dispatched(self, method):
        return [params for name, params in self.calls if name == method]


class SpecTests(unittest.TestCase):
    def test_selector_and_text_are_mutually_exclusive(self):
        with self.assertRaises(pa.PageActionError):
            pa._target_spec("#a", "文字")

    def test_something_must_be_given(self):
        for args in (("", ""), (None, None), ("   ", "")):
            with self.subTest(args=args):
                with self.assertRaises(pa.PageActionError):
                    pa._target_spec(*args)

    def test_rejected_selector_shapes(self):
        for bad in ("@import url(x)", "<script>alert(1)</script>", "javascript:void(0)"):
            with self.subTest(bad=bad):
                with self.assertRaises(pa.PageActionError):
                    pa._target_spec(bad)

    def test_length_bounds(self):
        with self.assertRaises(pa.PageActionError):
            pa._target_spec("#" + "a" * 500)
        with self.assertRaises(pa.PageActionError):
            pa._target_spec("", "很" * 201)

    def test_nth_validation(self):
        self.assertEqual(pa._target_spec("#a", nth="")["nth"], 0)
        self.assertEqual(pa._target_spec("#a", nth="2")["nth"], 2)
        for bad in ("-1", "abc", "100"):
            with self.subTest(bad=bad):
                with self.assertRaises(pa.PageActionError):
                    pa._target_spec("#a", nth=bad)


class EvalInjectionTests(unittest.TestCase):
    """页面脚本是固定常量 + JSON 字面量参数；这一条守的是整条链的注入面。"""

    def test_quotes_cannot_break_out_of_the_argument(self):
        fake = FakeDevTools()
        payload_text = 'a");window.pwned=1;//'
        spec = {"selector": "", "text": payload_text, "nth": 0, "scope": ""}
        pa._evaluate(fake, pa.RESOLVE_JS, spec)
        expression = fake.evaluations[-1]
        prefix = "(%s)(" % pa.RESOLVE_JS
        self.assertTrue(expression.startswith(prefix) and expression.endswith(")"), expression[:80])
        # 参数必须原样是一个 JSON 字面量：解回来还是那串恶意文本，且只出现一次（没有第二条语句）。
        self.assertEqual(json.loads(expression[len(prefix):-1])["text"], payload_text)
        self.assertEqual(expression.count("window.pwned"), 1)

    def test_unserializable_argument_fails_loud_instead_of_reaching_the_page(self):
        with self.assertRaises(TypeError):
            pa._evaluate(FakeDevTools(), pa.PROBE_JS, {"bad": object()})


class VerdictTests(unittest.TestCase):
    def base(self, **overrides):
        row = {"mutations": 10, "console": 0, "failed": 0, "runtime": 0,
               "href": "http://127.0.0.1:1/a", "title": "t"}
        row.update(overrides)
        return row

    def test_table(self):
        cases = [
            (self.base(mutations=10), self.base(mutations=12), "changed"),
            (self.base(), self.base(href="http://127.0.0.1:1/b"), "navigated"),
            (self.base(), self.base(title="别的标题"), "changed"),
            (self.base(), self.base(mutations=10), "no_change"),
            (self.base(), self.base(console=1), "error_seen"),
            (self.base(), self.base(failed=1), "error_seen"),
            (self.base(), self.base(runtime=1), "error_seen"),
            # 报错优先于变化：既变了又报错，判定必须是未通过
            (self.base(), self.base(mutations=40, console=1), "error_seen"),
        ]
        for before, after, expected in cases:
            with self.subTest(expected=expected, after=after):
                self.assertEqual(pa._verdict(before, after)["verdict"], expected)

    def test_navigation_restarts_the_observer_so_negative_delta_is_reloaded_not_minus_n(self):
        result = pa._verdict(self.base(mutations=30), self.base(mutations=2))
        self.assertEqual(result["verdict"], "reloaded")
        self.assertEqual(result["delta"]["mutations"], 0)

class ActionDispatchTests(unittest.TestCase):
    def test_click_moves_presses_then_releases_at_the_center(self):
        fake = FakeDevTools(probe=10, probe_after=12)
        row = self._session(fake)
        try:
            result = pa.click(row.root, page=row.id, selector="#plus", screenshot=False)
        finally:
            pa.close_all()
        events = fake.dispatched("Input.dispatchMouseEvent")
        self.assertEqual([event["type"] for event in events],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        for event in events:
            self.assertEqual((event["x"], event["y"]), (30, 30))   # 10 + 40/2, 20 + 20/2
        self.assertEqual(result["effect"], "changed")
        self.assertTrue(result["ok"])

    def test_click_is_refused_before_any_event_when_resolution_fails(self):
        for resolve in ({"count": 0, "matched": None, "candidates": []},
                        {"count": 2, "candidates": [{"tag": "button", "text": "甲", "visible": True,
                                                     "disabled": False, "x": 1, "y": 1, "w": 4, "h": 4}]},
                        {"matched": {"tag": "button", "text": "隐藏", "visible": False,
                                     "disabled": False, "x": 1, "y": 1, "w": 4, "h": 4}},
                        {"matched": {"tag": "button", "text": "禁用", "visible": True,
                                     "disabled": True, "x": 1, "y": 1, "w": 4, "h": 4}}):
            with self.subTest(resolve=list(resolve)[:2]):
                fake = FakeDevTools(resolve=resolve)
                row = self._session(fake)
                try:
                    with self.assertRaises(pa.PageActionError):
                        pa.click(row.root, page=row.id, selector="#x", screenshot=False)
                finally:
                    pa.close_all()
                self.assertEqual(fake.dispatched("Input.dispatchMouseEvent"), [],
                                 "定位没成立就绝不能派发鼠标事件")

    def test_nth_disambiguates(self):
        fake = FakeDevTools(resolve={"count": 2, "nth": 1,
                                     "matched": {"tag": "button", "text": "选项", "visible": True,
                                                 "disabled": False, "x": 50, "y": 5, "w": 20, "h": 10}})
        row = self._session(fake)
        try:
            result = pa.click(row.root, page=row.id, text="选项", nth=1, screenshot=False)
        finally:
            pa.close_all()
        self.assertEqual(result["at"], [60, 10])

    def test_explicit_nth_zero_is_not_treated_as_unspecified(self):
        """`nth: 0` 是【显式要第一个】，与"没写 nth"必须能区分。

        以前 `_target_spec` 把两者都归成 0，歧义判定又写 `spec["nth"] == 0`，于是
        多项命中时**无论怎么指定都点不到第一个**——调用方只能改选择器。这个缺陷是
        组件守卫（`dev_vue`）在 `.wmd-item#0` 上撞出来的，不是读代码看出来的。
        """
        self.assertTrue(pa._target_spec(selector=".item", nth=0)["nth_given"])
        self.assertEqual(pa._target_spec(selector=".item", nth=0)["nth"], 0)
        for unset in ("", None):
            self.assertFalse(pa._target_spec(selector=".item", nth=unset)["nth_given"],
                             "nth=%r 被判成了显式指定" % (unset,))
        fake = FakeDevTools(resolve={"count": 2, "nth": 0,
                                     "matched": {"tag": "button", "text": "第一项", "visible": True,
                                                 "disabled": False, "x": 10, "y": 20, "w": 20,
                                                 "h": 10}})
        row = self._session(fake)
        try:
            result = pa.click(row.root, page=row.id, selector=".item", nth=0, screenshot=False)
        finally:
            pa.close_all()
        # 假页面不会有 DOM 变化，所以 ok/effect 本来就是 no_change/False；
        # 这条要断言的是【没有被歧义拒掉、事件真的派发了】。
        self.assertEqual(result["effect"], "no_change", result)
        self.assertEqual(result["at"], [20, 25])
        self.assertEqual(len(fake.dispatched("Input.dispatchMouseEvent")), 3)

    def test_ambiguous_without_nth_is_still_refused(self):
        """另一半：没写 nth 时多项命中仍然拒绝，且一个鼠标事件都不派发。"""
        fake = FakeDevTools(resolve={"count": 2, "matched": {
            "tag": "button", "text": "第一项", "visible": True, "disabled": False,
            "x": 10, "y": 20, "w": 20, "h": 10}})
        row = self._session(fake)
        try:
            with self.assertRaises(pa.PageActionError) as caught:
                pa.click(row.root, page=row.id, selector=".item", screenshot=False)
        finally:
            pa.close_all()
        self.assertIn("未指定 nth", str(caught.exception))
        self.assertEqual(fake.dispatched("Input.dispatchMouseEvent"), [])

    def test_out_of_viewport_center_is_refused(self):
        fake = FakeDevTools(resolve={"matched": {"tag": "div", "text": "在视口外", "visible": True,
                                                 "disabled": False, "x": 5000, "y": 5, "w": 20,
                                                 "h": 10}})
        row = self._session(fake)
        try:
            with self.assertRaises(pa.PageActionError) as caught:
                pa.click(row.root, page=row.id, selector="#far", screenshot=False)
        finally:
            pa.close_all()
        self.assertIn("不在视口内", str(caught.exception))

    def test_type_inserts_text_and_evidences_that_it_landed(self):
        # 真页面里 <input> 的 innerText 是空的，只有 value 能读到；fake 按 mode 分开回话。
        # ⚠ probe_after 不传 = 与 probe 相等 = **零 mutation**，这是真实浏览器的形态：
        # `Input.insertText` 只改 .value，MutationObserver 结构性看不到（实测恒 0）。
        # 以前这里传 probe_after=6 造出 2 个 mutation，把「no_change 假阴性」正好掩盖掉——
        # 测试因此一直是绿的，而真世界上打字全部回报 ok:false。
        fake = FakeDevTools(probe=4,
                            inspect_by_mode={"value": "你好", "text": ""})
        row = self._session(fake)
        try:
            result = pa.type_text(row.root, page=row.id, selector="#name", value="你好",
                                 screenshot=False)
        finally:
            pa.close_all()
        inserted = fake.dispatched("Input.insertText")
        self.assertEqual(inserted, [{"text": "你好"}])
        self.assertTrue(result["focused"])
        self.assertTrue(result["value_landed"], "回读必须证实文本真的进了元素")
        self.assertEqual(result["value_seen"], "你好")
        # 判定也必须站在证据这边：文本已落入 ⇒ ok，且 effect 是 typed 而不是 no_change。
        self.assertEqual(result["effect"], "typed", result)
        self.assertTrue(result["ok"], "字已经进框了，ok 不能是 False（假阴性会让 Agent 去重试）")
        self.assertEqual(result["delta"]["mutations"], 0, "fake 必须复现零 mutation 的真实现场")

    def test_type_with_empty_payload_is_not_a_success(self):
        """空 payload 时 `"" in seen` 恒真——不拦就等于「什么都没做也算成功」。"""
        fake = FakeDevTools(probe=4, inspect_by_mode={"value": "已有内容", "text": ""})
        row = self._session(fake)
        try:
            result = pa.type_text(row.root, page=row.id, selector="#name", value="",
                                 screenshot=False)
        finally:
            pa.close_all()
        self.assertFalse(result["value_landed"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["effect"], "no_change")

    def test_type_reports_when_the_text_did_not_land(self):
        fake = FakeDevTools(probe=4, probe_after=6, inspect_by_mode={"value": "", "text": ""})
        row = self._session(fake)
        try:
            result = pa.type_text(row.root, page=row.id, selector="#name", value="你好",
                                 screenshot=False)
        finally:
            pa.close_all()
        self.assertFalse(result["value_landed"])
        # 没落入时 ok 必须仍由 DOM 判据说话：这里 fake 造了 mutation，所以是 changed。
        self.assertEqual(result["effect"], "changed")

    def test_press_on_an_inert_page_is_no_change_not_changed(self):
        """页面前一刻的地址/标题必须真读回来。以前拿硬编码空标题当基线，任何一次按键
        都会被比对成"标题变了"，把没生效说成生效。"""
        fake = FakeDevTools()
        row = self._session(fake)
        try:
            result = pa.press_key(row.root, page=row.id, key="Tab", screenshot=False)
        finally:
            pa.close_all()
        self.assertEqual(result["effect"], "no_change")
        self.assertFalse(result["ok"])

    def test_type_refuses_when_the_element_vanished_after_locating(self):
        fake = FakeDevTools(target={"ok": False, "reason": "element-gone"})
        row = self._session(fake)
        try:
            with self.assertRaises(pa.PageActionError) as caught:
                pa.type_text(row.root, page=row.id, selector="#name", value="x", screenshot=False)
        finally:
            pa.close_all()
        self.assertIn("消失了", str(caught.exception))
        self.assertEqual(fake.dispatched("Input.insertText"), [], "元素没了还往里插字就是打进空气")

    def test_type_rejects_oversize_payload_before_touching_the_page(self):
        row = self._session(FakeDevTools())
        try:
            with self.assertRaises(pa.PageActionError):
                pa.type_text(row.root, page=row.id, selector="#name", value="x" * (pa.MAX_VALUE_CHARS + 1))
        finally:
            pa.close_all()

    def test_press_uses_the_key_table_and_rejects_anything_else(self):
        fake = FakeDevTools()
        row = self._session(fake)
        try:
            pa.press_key(row.root, page=row.id, key="Enter", screenshot=False)
            events = fake.dispatched("Input.dispatchKeyEvent")
            self.assertEqual([event["type"] for event in events], ["keyDown", "keyUp"])
            self.assertEqual(events[0]["text"], "\r")
            self.assertEqual(events[0]["windowsVirtualKeyCode"], 13)
            with self.assertRaises(pa.PageActionError) as caught:
                pa.press_key(row.root, page=row.id, key="Control+a")
        finally:
            pa.close_all()
        self.assertIn("不在支持列表", str(caught.exception))

    def test_read_modes_are_validated(self):
        row = self._session(FakeDevTools(inspect={"text": "计数 7"}))
        try:
            self.assertEqual(pa.read_page(row.root, page=row.id, selector="#head")["text"], "计数 7")
            with self.assertRaises(pa.PageActionError):
                pa.read_page(row.root, page=row.id, selector="#head", mode="xml")
            with self.assertRaises(pa.PageActionError):
                pa.read_page(row.root, page=row.id, selector="#head", mode="attr")
        finally:
            pa.close_all()

    def test_read_reports_truncation_truthfully(self):
        """页面里 9000 字、回传被砍到 4000：`truncated` 必须为真。
        以前 JS 先 slice(0,4000)、Python 再判 `len(text) > 4000`——那个条件结构上永远为假，
        "读了一半"看起来像"读完了"。"""
        row = self._session(FakeDevTools(full_text="字" * 9000))
        try:
            got = pa.read_page(row.root, page=row.id, selector="#big", mode="text")
        finally:
            pa.close_all()
        self.assertEqual(len(got["text"]), 4000)
        self.assertTrue(got["truncated"], "截断标记必须是活的，否则整段读取都是假干净")
        self.assertEqual(got["full_length"], 9000)
        short = self._session(FakeDevTools(inspect={"text": "短", "full_length": 1}))
        try:
            self.assertFalse(pa.read_page(short.root, page=short.id,
                                          selector="#small")["truncated"])
        finally:
            pa.close_all()

    def test_contains_finds_a_needle_beyond_the_read_limit(self):
        """这条是新模式的立命之处：needle 在第 4200 字，整段读取拿不到，页面内搜索照样命中。"""
        row = self._session(FakeDevTools(corpus={"#long": [LONG_TEXT]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector="#long", mode="contains",
                               needle="紫终点-needle9")
        finally:
            pa.close_all()
        self.assertTrue(got["found"], got)
        self.assertEqual(got["hits"], 1)
        self.assertEqual(got["elements"], 1)
        self.assertGreater(got["searched_chars"], 4000)
        self.assertIn("紫终点-needle9", "".join(got["samples"]))
        self.assertTrue(got["scanned_all"])

    def test_contains_is_allowed_to_combine_container_and_needle(self):
        """`_target_spec` 禁 selector+text 同给（定位会互相矛盾），contains 必须绕开那道围栏：
        它的 selector 是【容器】、needle 是【要搜的词】，两者本来就该同时存在。"""
        fake = FakeDevTools(corpus={".panel": ["甲 needle9 乙", "丙"]})
        row = self._session(fake)
        try:
            got = pa.read_page(row.root, page=row.id, selector=".panel", mode="contains",
                               needle="needle9")
        finally:
            pa.close_all()
        self.assertEqual(got["hits"], 1)
        self.assertEqual(got["elements"], 2)
        self.assertEqual(got["elements_with_hit"], 1, "要能区分「哪几个元素里有」")

    def test_contains_counts_per_element(self):
        row = self._session(FakeDevTools(corpus={".row": ["x needle y needle", "needle"]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector=".row", mode="contains",
                               needle="needle")
        finally:
            pa.close_all()
        self.assertEqual((got["hits"], got["elements_with_hit"]), (3, 2))

    def test_an_unmatched_container_is_not_evidence(self):
        """容器一个都没匹配到时，"没找到"这句话毫无内容——必须如实标出来。"""
        row = self._session(FakeDevTools(corpus={".panel": ["有货"]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector="#nope", mode="contains",
                               needle="needle")
            text = pa.render(got)
        finally:
            pa.close_all()
        self.assertEqual(got["elements"], 0)
        self.assertFalse(got["found"])
        self.assertEqual(got["searched_chars"], 0)
        self.assertIn("不构成任何证据", text)

    def test_a_bailed_scan_is_flagged_as_incomplete(self):
        row = self._session(FakeDevTools(corpus={".panel": ["x"]}, scanned_all=False))
        try:
            got = pa.read_page(row.root, page=row.id, selector=".panel", mode="contains",
                               needle="needle")
            text = pa.render(got)
        finally:
            pa.close_all()
        self.assertFalse(got["scanned_all"])
        self.assertTrue(got["truncated"], "没扫完要复用同一个「被截断」语义，别另起一套字段")
        self.assertIn("没扫完", text)

    def test_contains_is_validated_before_touching_the_page(self):
        for needle in ("", "   ", "x" * 300):
            fake = FakeDevTools(corpus={".panel": ["x"]})
            row = self._session(fake)
            try:
                with self.assertRaises(pa.PageActionError):
                    pa.read_page(row.root, page=row.id, selector=".panel", mode="contains",
                                 needle=needle)
            finally:
                pa.close_all()
            self.assertEqual([item for item in fake.evaluations if "elements_with_hit" in item],
                             [], "参数不合法就绝不能把脚本派发到页面")

    def test_contains_container_still_passes_the_selector_fence(self):
        """容器是 selector，就必须过 `_target_spec` 那道围栏——不能因为"它是搜索范围"
        就允许选项样式、`javascript:`、超长或穿越写法混进 querySelectorAll。"""
        for bad in ("<div>", "javascript:alert(1)", "@x", "x" * 500):
            fake = FakeDevTools(corpus={".panel": ["x"]})
            row = self._session(fake)
            try:
                with self.assertRaises(pa.PageActionError):
                    pa.read_page(row.root, page=row.id, selector=bad, mode="contains",
                                 needle="needle")
            finally:
                pa.close_all()
            self.assertEqual([item for item in fake.evaluations if "elements_with_hit" in item], [])

    def test_contains_counts_every_hit_but_shows_only_a_few(self):
        """命中数与样本数是两件事：上限只该砍样本，不该让"命中 5 处"报成"3 处"。"""
        row = self._session(FakeDevTools(corpus={".p": ["needle " * 5]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector=".p", mode="contains",
                               needle="needle")
        finally:
            pa.close_all()
        self.assertEqual(got["hits"], 5)
        self.assertEqual(len(got["samples"]), pa.MAX_SAMPLES,
                         "样本必须【正好】收满上限：只断言 ≤ 的话删掉限量也照样绿")

    def test_hits_count_visible_text_and_hidden_matches_are_reported_separately(self):
        """「页面上有这句话」与「DOM 里有这句话」必须是两个数：
        `innerText` 对未渲染元素会退回 `textContent`（真浏览器实测），不分开计数的话
        一个折叠面板里的文案会被当成"用户看得见"。"""
        row = self._session(FakeDevTools(corpus={
            ".p": ["needle 看得见", ("needle 藏在折叠区里", True)]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector=".p", mode="contains",
                               needle="needle")
            text = pa.render(got)
        finally:
            pa.close_all()
        self.assertEqual(got["hits"], 1)
        self.assertEqual((got["hidden_hits"], got["hidden_elements"]), (1, 1))
        self.assertTrue(got["found"])
        self.assertEqual(len(got["samples"]), 1, "样本只回可见的那处")
        self.assertIn("当前不可见", text)

    def test_a_needle_only_in_hidden_text_is_not_a_hit(self):
        row = self._session(FakeDevTools(corpus={".p": [("只有隐藏版", True)]}))
        try:
            got = pa.read_page(row.root, page=row.id, selector=".p", mode="contains",
                               needle="只有隐藏版")
        finally:
            pa.close_all()
        self.assertFalse(got["found"], "不可见命中不能把 found 点亮")
        self.assertEqual(got["hidden_hits"], 1)

    def test_the_scan_bound_is_set_in_one_place(self):
        """上限由 Python 传进页面（`spec.limit`），不是 JS 里写死的另一套数字——
        否则调一个地方、另一个地方还在按老上限静默截断。"""
        fake = FakeDevTools(corpus={".p": ["x"]})
        row = self._session(fake)
        try:
            pa.read_page(row.root, page=row.id, selector=".p", mode="contains", needle="x")
        finally:
            pa.close_all()
        expr = next(item for item in fake.evaluations if "elements_with_hit" in item)
        sent = json.loads(expr[expr.index("({") + 1:expr.rindex(")")])
        self.assertEqual(sent["limit"], pa.MAX_SCAN_CHARS)
        self.assertEqual(sent["samples"], pa.MAX_SAMPLES)

    def test_contains_sends_the_needle_as_data_not_as_script(self):
        """参数走 JSON 字面量：带引号与括号的 needle 原样送达，不该把脚本写坏或被执行。"""
        hostile = "'); alert(1); ('"
        fake = FakeDevTools(corpus={".panel": ["前 " + hostile + " 后"]})
        row = self._session(fake)
        try:
            got = pa.read_page(row.root, page=row.id, selector=".panel", mode="contains",
                               needle=hostile)
        finally:
            pa.close_all()
        self.assertEqual(got["hits"], 1, "needle 必须原样参与搜索，不能被转义掉")
        expr = next(item for item in fake.evaluations if "elements_with_hit" in item)
        sent = json.loads(expr[expr.index("({") + 1:expr.rindex(")")])
        self.assertEqual(sent["needle"], hostile)
        self.assertEqual(sent["selector"], ".panel")

    def test_wait_reports_timeout_as_failure_with_what_it_saw(self):
        fake = FakeDevTools(inspect={"count": 0, "found": False, "visible": False, "text": ""})
        fake.console = [{"level": "error", "text": "页面炸了"}]
        row = self._session(fake)
        try:
            result = pa.wait_for(row.root, page=row.id, selector="#later", state="present", timeout=0.2)
        finally:
            pa.close_all()
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["console_errors"][0]["text"], "页面炸了")

    def test_wait_states_that_need_a_selector_refuse_without_one(self):
        row = self._session(FakeDevTools())
        try:
            for state in ("present", "visible", "absent"):
                with self.subTest(state=state):
                    with self.assertRaises(pa.PageActionError):
                        pa.wait_for(row.root, page=row.id, state=state, timeout=0.1)
            with self.assertRaises(pa.PageActionError):
                pa.wait_for(row.root, page=row.id, selector="#a", state="somewhen")
        finally:
            pa.close_all()

    def test_only_an_observed_change_counts_as_success(self):
        """ok 的语义表：结构化消费方（MCP / 工作流门）读的就是这个字段。
        no_change / error_seen / 离开本机 都必须是 False——否则"点了没反应"会被当成成功。"""
        seen = []
        for name, fake in (
                ("changed", FakeDevTools(probe=10, probe_after=12)),
                ("no_change", FakeDevTools()),
                ("error_seen", FakeDevTools(error_on_drain=True)),
                ("error_seen", FakeDevTools(probe=10, probe_after=12, error_on_drain=True)),
                ("navigated", FakeDevTools(inspect={"href": "http://example.com/x"}))):
            with self.subTest(name=name):
                row = self._session(fake)
                try:
                    result = pa.click(row.root, page=row.id, selector="#plus", screenshot=False)
                finally:
                    pa.close_all()
                self.assertEqual(result["effect"], name)
                self.assertEqual(result["ok"], name == "changed" and not result["left_loopback"],
                                 "%s 的 ok 不对" % name)
                seen.append(name)
        self.assertEqual(sorted(seen), ["changed", "error_seen", "error_seen",
                                       "navigated", "no_change"])

    def test_a_repeated_identical_error_still_fails_the_action(self):
        """_DevTools 的 console 列表会去重：同一条报错每次动作都复现时列表长度不动。
        判定必须走去重前的事件计数，否则一直报错的页面会被报成"没问题"。"""
        fake = FakeDevTools(probe=10, probe_after=12, error_on_drain=True)
        # 上一次动作就报过同一条：列表里已有它，但事件计数还是 0。
        fake.console.append({"level": "error", "text": "反复出现的同一条报错"})
        row = self._session(fake)
        try:
            result = pa.click(row.root, page=row.id, selector="#plus", screenshot=False)
        finally:
            pa.close_all()
        self.assertEqual(fake.error_events, 1, "动作期间又报了一次")
        self.assertEqual(len(fake.console), 1, "列表去重后长度不动——所以不能用它判定")
        self.assertEqual(result["effect"], "error_seen")
        self.assertFalse(result["ok"])

    def test_an_action_that_leaves_the_loopback_is_reported_and_failed(self):
        """只在 open 时校验回环是不够的：页内跳转/重定向能把浏览器带到外网机器。"""
        fake = FakeDevTools(probe=10, probe_after=12, inspect={"href": "http://10.1.2.3/"})
        row = self._session(fake)
        try:
            result = pa.click(row.root, page=row.id, selector="#plus", screenshot=False)
        finally:
            pa.close_all()
        self.assertTrue(result["left_loopback"])
        self.assertFalse(result["ok"])
        self.assertIn("已离开本机", pa.render(result))


    def test_read_without_a_selector_falls_back_to_the_whole_page(self):
        """校验用的选择器和真正查询用的选择器必须是同一个：以前校验 "html"、查询 "" ，
        空选择器的读取永远返回 found=false。"""
        fake = FakeDevTools()
        row = self._session(fake)
        try:
            pa.read_page(row.root, page=row.id)
        finally:
            pa.close_all()
        sent = [json.loads(item[item.index("({") + 1:item.rindex(")")])
                for item in fake.evaluations if "spec.mode === 'attr'" in item]
        self.assertEqual(sent[-1]["selector"], "html")


    def _session(self, fake):
        with pa._lock:
            scratch = Path(tempfile.mkdtemp(prefix="docmind_page_"))
            # 这个临时目录以前没人删：一块电池把 ActionDispatchTests 重复跑十几遍，
            # 实测一小时内堆出 893 个空目录。测试自己造的垃圾自己收。
            self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
            row = pa._Session("fake%05d" % len(pa._sessions), _EmptyStack(), fake,
                              scratch, "http://127.0.0.1:1/", {}, None, None)
            pa._sessions[row.id] = row
        return row


class _EmptyStack:
    """替会话占位：close 时只需要证明"先关 stack 再取快照"这个顺序是对的。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SessionRegistryTests(unittest.TestCase):
    def tearDown(self):
        pa.close_all()

    def test_unknown_page_id_is_an_explicit_error(self):
        for value in ("", "  ", "nope"):
            with self.subTest(value=value):
                with self.assertRaises(pa.PageActionError) as caught:
                    pa.read_page("x", page=value)
                self.assertTrue(str(caught.exception))

    def test_idle_sessions_are_swept_without_a_background_thread(self):
        fake = FakeDevTools()
        row = pa._Session("tTL00001", _EmptyStack(), fake, Path(tempfile.mkdtemp()), "u", {}, None, None)
        with pa._lock:
            pa._sessions[row.id] = row
        row.last_used = time.monotonic() - pa.IDLE_TTL_SECONDS - 1
        # _sweep 只除名、不在锁里关浏览器；真正回收由调用方在锁外做——两步都要有。
        dead = pa._sweep()
        self.assertEqual([stale.id for stale in dead], [row.id])
        self.assertNotIn(row.id, pa.live_pages())
        for stale in dead:
            stale.close()
        self.assertTrue(row.stack.closed)

    def test_cap_is_enforced_before_launching_another_browser(self):
        for index in range(pa.MAX_SESSIONS):
            row = pa._Session("cap%04d" % index, _EmptyStack(), FakeDevTools(),
                              Path(tempfile.mkdtemp()), "u", {}, None, None)
            with pa._lock:
                pa._sessions[row.id] = row
        with self.assertRaises(pa.PageActionError) as caught:
            pa.open_page(tempfile.mkdtemp())
        self.assertIn("最多", str(caught.exception))

    def test_two_concurrent_opens_cannot_both_take_the_last_slot(self):
        """名额必须"先占再起浏览器"。只数已登记会话的话，两个并发 open 各自看到 MAX-1，
        于是同时起两个真浏览器——上限形同虚设。"""
        root = Path(tempfile.mkdtemp())
        (root / "index.html").write_text("<title>t</title>", encoding="utf-8", newline="\n")
        for index in range(pa.MAX_SESSIONS - 1):
            row = pa._Session("half%04d" % index, _EmptyStack(), FakeDevTools(),
                              root, "u", {}, None, None)
            with pa._lock:
                pa._sessions[row.id] = row
        original = pa._session_gen
        results = []

        def slow(timeout, flags, cleanup):
            time.sleep(0.4)         # 另一个线程必须在这段时间里【已经看过】名额，否则测不出竞态
            raise visual.VisualAcceptanceError("假浏览器起不来（这条只验名额）")
            yield None

        pa._session_gen = slow

        def attempt():
            try:
                pa.open_page(root)
                results.append("opened")
            except pa.PageActionError as exc:
                results.append("refused" if "最多" in str(exc) else "other")
            except visual.VisualAcceptanceError:
                results.append("launched")

        try:
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        finally:
            pa._session_gen = original
        self.assertEqual(sorted(results), ["launched", "refused"],
                         "恰好一个占到名额、另一个被拒：%s" % results)
        with pa._lock:
            self.assertEqual(pa._reservations, 0, "失败路径必须把名额还回去")
        pa.close_all()

    def test_close_reports_the_reaping_evidence_not_a_vacuous_true(self):
        cleanup = {}
        stack = _EmptyStack()
        row = pa._Session("evid0001", stack, FakeDevTools(), Path(tempfile.mkdtemp()), "u",
                          cleanup, None, None)
        with pa._lock:
            pa._sessions[row.id] = row
        result = pa.close_page(row.id)
        self.assertTrue(stack.closed)
        self.assertFalse(result["ok"], "没有作业对象/profile 证据就不能声称回收成功")
        self.assertEqual(result["cleanup"], {"spawned": True})

    def test_close_populates_cleanup_after_the_stack_closes(self):
        class FillingStack:
            def close(self):
                self.owner["job_closed"] = True
                self.owner["profile_released"] = True

        cleanup = {}
        stack = FillingStack()
        stack.owner = cleanup
        row = pa._Session("fill0001", stack, FakeDevTools(), Path(tempfile.mkdtemp()), "u",
                          cleanup, None, None)
        with pa._lock:
            pa._sessions[row.id] = row
        result = pa.close_page(row.id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleanup"]["job_closed"])

    def test_close_all_is_idempotent(self):
        self.assertGreaterEqual(pa.close_all(), 0)
        self.assertEqual(pa.close_all(), 0)
        self.assertEqual(pa.live_pages(), [])

    def test_open_refuses_a_non_loopback_url_before_starting_a_browser(self):
        with self.assertRaises(visual.VisualAcceptanceError):
            pa.open_page(tempfile.mkdtemp(), url="http://example.com:80/")
        # 校验失败也要把占住的名额还回去——漏还的话，后面的 open 会莫名"名额已满"
        # （这条变异跑出来过一次，是真事故）。
        with pa._lock:
            self.assertEqual(pa._reservations, 0)


class RenderTests(unittest.TestCase):
    def test_no_change_is_worded_as_not_success(self):
        text = pa.render({"action": "click", "effect": "no_change", "ok": False,
                          "delta": {"mutations": 0},
                          "element": {"tag": "button", "text": "选项", "w": 10, "h": 10},
                          "url": "http://127.0.0.1:1/"})
        self.assertIn("没有观察到变化", text)
        self.assertIn("不要当成成功", text)


    def test_error_seen_is_marked_failed(self):
        text = pa.render({"action": "type", "effect": "error_seen", "delta": {"mutations": 3, "console": 1},
                          "element": {"tag": "input", "text": "", "w": 10, "h": 10},
                          "console_errors": [{"text": "炸了"}], "url": "http://127.0.0.1:1/"})
        self.assertIn("未通过", text)
        self.assertIn("炸了", text)

    def test_locate_shows_the_ambiguity_flag(self):
        text = pa.render({"action": "locate", "count": 2, "ambiguous": True, "view": {"width": 1, "height": 1},
                          "candidates": [{"tag": "button", "text": "选项", "w": 1, "h": 1, "x": 0, "y": 0,
                                          "visible": True, "disabled": False}] * 2})
        self.assertIn("歧义", text)

    def test_list_and_close_have_their_own_lines(self):
        self.assertEqual(pa.render({"action": "list", "live": []}), "在途页面会话 0 个。")
        text = pa.render({"action": "close", "page": "abc", "ok": False,
                          "cleanup": {"job_closed": False, "profile_released": True}, "live": []})
        self.assertIn("收尾【不完整】", text)
        self.assertIn("请检查 msedge 进程", text)
        self.assertNotIn("已回收", text)
        missing = pa.render({"action": "close", "page": "abc", "ok": False,
                             "error": "找不到会话（可能已回收）。", "live": []})
        self.assertIn("没有回收到东西", missing)
        good = pa.render({"action": "close", "page": "abc", "ok": True,
                          "cleanup": {"job_closed": True, "profile_released": True}, "live": []})
        self.assertIn("已回收", good)


@unittest.skipUnless(BROWSER, "未找到 Edge/Chromium，跳过真实页面交互")
class BrowserActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_page_e2e_")
        (Path(self.tmp) / "index.html").write_text(FIXTURE_PAGE, encoding="utf-8", newline="\n")
        (Path(self.tmp) / "next.html").write_text("<title>下一页</title><p>done</p>",
                                                  encoding="utf-8", newline="\n")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.page = pa.open_page(self.tmp)
        self.addCleanup(pa.close_page, self.page["page"])

    def test_click_actually_changes_the_page_and_is_reported(self):
        self.assertEqual(self.page["title"], "交互夹具")
        result = pa.click(self.tmp, page=self.page["page"], selector="#plus", screenshot=False)
        self.assertEqual(result["effect"], "changed")
        self.assertGreater(result["delta"]["mutations"], 0)
        read = pa.read_page(self.tmp, page=self.page["page"], selector="#head")
        self.assertEqual(read["text"], "计数 1")
        pa.click(self.tmp, page=self.page["page"], text="加一", screenshot=False)
        self.assertEqual(pa.read_page(self.tmp, page=self.page["page"], selector="#head")["text"], "计数 2")

    def test_typing_reaches_the_page_listener(self):
        typed = pa.type_text(self.tmp, page=self.page["page"], selector="#name", value="真实输入")
        self.assertEqual(typed["effect"], "changed")
        waited = pa.wait_for(self.tmp, page=self.page["page"], selector="#echo", state="text",
                             value="真实输入", timeout=3)
        self.assertTrue(waited["ok"], waited)

    def test_ambiguous_hidden_and_disabled_targets_are_refused(self):
        with self.assertRaises(pa.PageActionError) as caught:
            pa.click(self.tmp, page=self.page["page"], text="选项")
        self.assertIn("命中多个", str(caught.exception))
        self.assertEqual(pa.click(self.tmp, page=self.page["page"], text="选项",
                                 nth=1, screenshot=False)["element"]["id"], "dupe2")
        for selector, reason in (("#off", "禁用"), ("#hid", "不可见"), ("#nope", "没有找到")):
            with self.subTest(selector=selector):
                with self.assertRaises(pa.PageActionError) as caught:
                    pa.click(self.tmp, page=self.page["page"], selector=selector)
                self.assertIn(reason, str(caught.exception))

    def test_a_console_error_during_an_action_fails_the_verdict(self):
        result = pa.click(self.tmp, page=self.page["page"], selector="#broken", screenshot=False)
        self.assertEqual(result["effect"], "error_seen")
        self.assertFalse(result["ok"])
        self.assertIn("真实报错", json.dumps(result["console_errors"], ensure_ascii=False))

    def test_navigation_is_distinguished_from_a_dom_change(self):
        pa.type_text(self.tmp, page=self.page["page"], selector="#name", value="甲", screenshot=False)
        result = pa.click(self.tmp, page=self.page["page"], selector="#submit", screenshot=False)
        self.assertEqual(result["effect"], "navigated")
        self.assertGreaterEqual(result["delta"]["mutations"], 0)
        self.assertIn("next.html", str(result["url"]))
        with self.assertRaises(pa.PageActionError):
            pa.click(self.tmp, page=self.page["page"], text="加一")

    def test_screenshot_is_written_inside_the_project(self):
        result = pa.click(self.tmp, page=self.page["page"], selector="#plus", screenshot=True)
        self.assertTrue(result["screenshot"].startswith(".docmind/page-action/"))
        self.assertTrue((Path(self.tmp) / result["screenshot"]).is_file())

    def test_a_long_page_is_searchable_even_though_the_read_is_capped(self):
        """真浏览器上把两种模式的分工验一遍：整段读取被 4000 字上限砍掉（且必须承认砍了），
        页面内搜索照样命中第 4200 字之后的词。"""
        capped = pa.read_page(self.tmp, page=self.page["page"], selector="#long", mode="text")
        self.assertGreater(capped["full_length"], 4000, capped["full_length"])
        self.assertTrue(capped["truncated"])
        self.assertNotIn("紫终点-needle9", capped["text"], "读取不该拿到全文，否则上限没生效")
        hit = pa.read_page(self.tmp, page=self.page["page"], selector="#long", mode="contains",
                           needle="紫终点-needle9")
        self.assertTrue(hit["found"], hit)
        self.assertEqual(hit["elements"], 1)
        self.assertGreater(hit["searched_chars"], 4000)
        self.assertIn("needle9", "".join(hit["samples"]))
        miss = pa.read_page(self.tmp, page=self.page["page"], selector="#nope",
                            mode="contains", needle="紫终点-needle9")
        self.assertFalse(miss["found"])
        self.assertEqual(miss["elements"], 0)
        self.assertIn("不构成任何证据", pa.render(miss))

    def test_contains_searches_visible_text_only(self):
        """真浏览器实测：`innerText` 对 `display:none` 的元素会退回 `textContent`，
        所以"只搜可见文字"必须靠显式的渲染判定（`getClientRects()`），不能指望 innerText。
        不可见那边的命中另报 `hidden_hits`，既不混进 `hits`，也不静默丢掉。"""
        hidden = pa.read_page(self.tmp, page=self.page["page"], selector="#hid",
                              mode="contains", needle="隐藏")
        self.assertEqual(hidden["elements"], 1, "容器是存在的，问题在文字不可见")
        self.assertEqual(hidden["hits"], 0, hidden)
        self.assertEqual(hidden["hidden_hits"], 1, "不可见命中要如实报出来，不能假装搜不到")
        self.assertFalse(hidden["found"])
        self.assertIn("当前不可见", pa.render(hidden))
        shown = pa.read_page(self.tmp, page=self.page["page"], selector="button",
                             mode="contains", needle="选项")
        self.assertEqual((shown["hits"], shown["elements_with_hit"]), (2, 2),
                         "两个可见按钮各命中一次：计数要按元素分开，不能只报「整页命中」")

    def test_close_leaves_no_session_and_reports_real_reaping(self):
        result = pa.close_page(self.page["page"])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["cleanup"]["job_closed"])
        self.assertTrue(result["cleanup"]["profile_released"])
        self.assertEqual(pa.live_pages(), [])


class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_page_tool_")
        config.set_runtime("code_root", self.tmp)
        self.addCleanup(config.set_runtime, "code_root", self._prev)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_registered_in_all_three_places(self):
        import agent
        import tools
        self.assertIn("dev_page_action", tools.TOOLS)
        self.assertIn("dev_page_action", [row["function"]["name"] for row in tools.tool_schemas()])
        self.assertIn("- dev_page_action(", agent._SYSTEM_PROMPT_FULL)
        self.assertIn("dev_page_action", agent.SYSTEM_PROMPT)

    def test_it_is_exposed_as_a_write_tool_over_mcp(self):
        from agent_runtime import mcp_server
        self.assertIn("dev_page_action", mcp_server.WRITE_TOOLS)
        self.assertNotIn("dev_page_action", mcp_server.READ_ONLY_TOOLS)

    def test_needle_and_mode_actually_reach_the_module(self):
        """文档里写的每个参数都必须真的落到模块上：`needle` 一旦漏接，contains 会静默
        变成"搜一个空词"并永远报没找到——本仓为 `coverage:` 这类漏接付过学费。"""
        import tools
        from agent_runtime import page_action
        seen = {}

        def fake_read(root, **kwargs):
            seen.update(kwargs)
            return {"action": "read", "ok": True, "mode": kwargs.get("mode"),
                    "needle": kwargs.get("needle"), "selector": kwargs.get("selector"),
                    "hits": 1, "elements": 1, "elements_with_hit": 1, "searched_chars": 9000,
                    "scanned_all": True, "samples": ["…needle9…"], "text": "", "count": 1,
                    "truncated": False, "error": ""}

        original = page_action.read_page
        page_action.read_page = fake_read
        try:
            out = tools.TOOLS["dev_page_action"]["func"](
                "action: read\npage: p1\nselector: .panel\nmode: contains\nneedle: needle9")
        finally:
            page_action.read_page = original
        self.assertEqual((seen["needle"], seen["mode"], seen["selector"]),
                         ("needle9", "contains", ".panel"))
        self.assertIn("命中 1 处", out)

    def test_bad_actions_and_args_come_back_as_text_not_tracebacks(self):
        import tools
        call = tools.TOOLS["dev_page_action"]["func"]
        self.assertIn("不支持 action", call("action: teleport"))
        self.assertIn("缺少 page", call("action: click\nselector: #a"))
        self.assertIn("找不到会话", call("action: close\npage: zzzzzz"))

    def test_screenshot_attachment_refuses_paths_outside_the_project(self):
        import tools
        self.assertIsNone(tools._screenshot_data_url("../outside.png", self.tmp))
        self.assertIsNone(tools._screenshot_data_url("C:\\Windows\\win.ini", self.tmp))
        self.assertIsNone(tools._screenshot_data_url(".docmind/page-action/none.png", self.tmp))


if __name__ == "__main__":
    unittest.main()
