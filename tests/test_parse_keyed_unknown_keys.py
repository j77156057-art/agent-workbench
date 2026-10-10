# -*- coding: utf-8 -*-
"""`_parse_keyed` 必须把未知键名报出来，不许静默污染上一个字段。

## 实测事故（2026-10-10）

交付代理调 `dev_page_action`：

    action: open
    url: http://127.0.0.1:8099/
    width: 1280          ← 这是 preview_project 的键
    height: 720

旧解析器只认自己的 keys，`url` 的值一路吃到字符串末尾，取到
`'http://127.0.0.1:8099/\\nwidth: 1280\\nheight: 720'`，
拼出 `http://127.0.0.1:8099/width: 1280height: 720` -> 404 ->
标题 "Error response" -> 工具还回报「打开时就有 1 条 console 错误」，
**把账算在网页头上**，而真因是参数里有个它不认识的键。

`dev_page_action` 的 keys 里确实没有 width/height —— 那次是把
`preview_project` 的键名喂错了工具。

## 契约

* `url` 等已知字段的值**不被未知键污染**；
* 未知键名通过 `__unknown__` 回报（返回值仍是 dict，61 处调用点零改动）；
* `width`/`height` 在 `dev_page_action` 里被显式拒绝，并说明它们属于谁；
* **正常输入零误报** —— 多行字段值、JSON 体、纯文本都不该冒出未知键。
"""

import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                            # noqa: E402
from tools import _parse_keyed           # noqa: E402

# `dev_page_action` 实际用的键集（width/height 已列进来，见 tools.py 的说明）。
# ⚠ 用它而不是另抄一份 —— 抄一份就会与实现漂移，测试绿着而真工具已变。
PAGE_ACTION_KEYS = ["action", "page", "url", "entry", "selector", "text", "nth",
                    "scope", "value", "key", "state", "mode", "attr", "needle",
                    "timeout", "settle", "screenshot", "width", "height"]


class ParseKeyedKeepsKnownFieldsCleanTests(unittest.TestCase):
    """已知字段的值不许被未知键毁掉。"""

    def test_url_is_not_polluted_by_unknown_keys(self):
        """**核心判据**：就是那次事故的输入。

        用 `dev_page_action` 真实的键集（已含 width/height）—— 这样
        `url` 才会停在该停的地方。若将来那个键集又把 width/height 删了，
        这条会红，而那正是缺陷复发。
        """
        d = _parse_keyed(
            "action: open\n"
            "url: http://127.0.0.1:8099/\n"
            "width: 1280\n"
            "height: 720\n",
            PAGE_ACTION_KEYS)
        self.assertEqual(d.get("url"), "http://127.0.0.1:8099/",
                         "url 被 width/height 污染了 —— 会拼出 404 地址")
        self.assertEqual(d.get("action"), "open")

    def test_page_action_key_set_still_contains_borrowed_keys(self):
        """反向守卫：width/height 必须留在 `dev_page_action` 的键集里。

        它们本该属于 preview_project；列在这里**不是为了支持**，而是为了
        在下面被显式拒绝。若哪天从键集里删掉，url 就重新被污染（M1 变异那条）。
        """
        import tools
        src = pathlib.Path(tools.__file__).read_text(encoding="utf-8",
                                                     errors="replace")
        i = src.index("def dev_page_action")
        # 窗口要覆盖到 keys 列表那一行（它在 docstring 之后），取大一点
        seg = src[i:i + 6000]
        self.assertIn('"width", "height"', seg,
                      "dev_page_action 的键集里没有 width/height —— "
                      "调用方写错时 url 会重新被污染")

    def test_unknown_keys_are_reported(self):
        d = _parse_keyed("url: http://x/\nfoo: 1\nbar: 2\n", ["url"])
        self.assertEqual(d.get("__unknown__"), ["foo", "bar"],
                         "未知键名没被报出来")

    def test_no_unknown_key_means_no_extra_entry(self):
        """正常输入不该多出 `__unknown__`，否则调用方无法区分。"""
        d = _parse_keyed("action: open\nurl: http://x/\n", PAGE_ACTION_KEYS)
        self.assertNotIn("__unknown__", d)

    def test_return_value_is_still_a_dict(self):
        """61 处调用点靠这个保持兼容 —— 不许改成元组。"""
        d = _parse_keyed("a: 1\n", ["a"])
        self.assertIsInstance(d, dict)

    # ---------- 不许误报（这一半同样重要） ----------

    def test_multiline_field_value_is_not_an_unknown_key(self):
        """字段值的续写行不是键。"""
        d = _parse_keyed(
            "path: a/b.py\nold_text: 第一行\n继续写第二行\nnew_text: 改\n",
            ["path", "old_text", "new_text"])
        self.assertIsNone(d.get("__unknown__"))
        self.assertEqual(d.get("old_text"), "第一行\n继续写第二行")

    def test_json_body_is_not_an_unknown_key(self):
        d = _parse_keyed('body: {"a": 1, "b": {"c": 2}}\n', ["body"])
        self.assertIsNone(d.get("__unknown__"))

    def test_plain_text_without_any_key_is_not_unknown(self):
        d = _parse_keyed("就一段文字\n没有键\n", ["action"])
        self.assertIsNone(d.get("__unknown__"))

    def test_empty_input_is_safe(self):
        self.assertEqual(_parse_keyed("", ["action"]), {})

    def test_colon_inside_value_on_same_line_is_safe(self):
        """值里同行的冒号不算未知键；尾随换行是既有行为，不一起改。"""
        d = _parse_keyed("url: http://x/ shell: /bin/sh\n", ["url"])
        self.assertEqual(d.get("url"), "http://x/ shell: /bin/sh\n")
        self.assertIsNone(d.get("__unknown__"))

    def test_duplicate_unknown_key_reported_once(self):
        d = _parse_keyed("url: x\nfoo: 1\nfoo: 2\n", ["url"])
        self.assertEqual(d.get("__unknown__"), ["foo"])


class PageActionRejectsBorrowedKeysTests(unittest.TestCase):
    """`dev_page_action` 必须显式拒绝 `preview_project` 的键。"""

    def setUp(self):
        # dev_page_action 第一件事就是 `_get_code_root()`，没配就提前返回
        # "尚未配置代码库根目录" —— 那样根本测不到参数校验。指到临时目录。
        self._old = config.get_runtime("code_root")
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="docmind_pka_"))
        config.set_runtime("code_root", str(self._tmp))
        self.addCleanup(config.set_runtime, "code_root", self._old)
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _call(self, arg):
        import tools
        return tools.TOOLS["dev_page_action"]["func"](arg)

    def test_code_root_is_configured(self):
        """自检：别再把「没配 code_root」当成「校验生效」。"""
        out = self._call("action: open\nurl: http://127.0.0.1:1/\n")
        self.assertNotIn("尚未配置代码库根目录", out,
                         "测试环境没配上 code_root，后面几条都是空判")

    def test_width_height_are_refused_with_guidance(self):
        """核心判据：给出能指导下一步的错法，而不是让 URL 烂掉。"""
        out = self._call(
            "action: open\nurl: http://127.0.0.1:8099/\nwidth: 1280\nheight: 720\n")
        self.assertIn("preview_project", out,
                      "没说清这两个键属于谁 —— 调用方不知道该往哪放")
        self.assertIn("width", out)
        self.assertIn("height", out)
        self.assertNotIn("Error response", out)

    def test_unknown_key_is_refused_with_available_list(self):
        out = self._call("action: open\nurl: http://x/\nfoo: 1\n")
        self.assertIn("不认识", out)
        self.assertIn("selector", out, "没告诉调用方可用的参数名")

    def test_valid_call_is_not_blocked(self):
        """反向守卫：合法的 open 不许被误拦。"""
        out = self._call("action: open\nurl: http://127.0.0.1:1/\n")
        self.assertNotIn("不认识", out)
        self.assertNotIn("preview_project", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
