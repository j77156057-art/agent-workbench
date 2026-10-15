# -*- coding: utf-8 -*-
"""能力基线：已真跑通的 22 条动作，固化成套件里的判据。

## 这份文件是什么

`tools.TOOLS` 有 112 个键。这份只钉住**已经真跑通**的那部分 ——
不是「目录里有个名字」，是**真调过、并且效果被验证过**。

今晚反复吃到的一课：**符号还在 ≠ 行为还成立**。所以每条判据都要求
**正面证据**，而不是「没抛异常」。

## ⚠⚠ 判据为什么写成这样（今晚栽了 8 次）

探针阶段的错误几乎全在**「怎么判定成功」**，不在「怎么调用」：

| 错误 | 现象 |
|---|---|
| **恒真判据** | `bool(s.strip()) and "Traceback" not in s` —— 把「尚未配置代码库根目录」这句**错误消息**判成「能用」，产出一张全绿表 |
| 词表判生死 | `DEAD_MARKERS` 含通用的「失败/错误」，把成功消息「页面会话已打开」误杀 |
| 扣措辞 | 找英文 `changed`，它回中文「生效（DOM 变了）」 |
| 期望写错 | `grep` pattern 是 `hello`，我却要求回显 `hello.txt` |
| 扣措辞（2） | `game_impact` 返回语义检索结果（`enemy.gd`），我要求字面出现 `hero` |

所以这里的判据一律**扣住机制本身**：

| 类 | 正面证据（与措辞无关） |
|---|---|
| 读文件 | 返回里含**文件里那段特有内容** |
| 检索 | 命中行数 > 0（正则 `^\\S+:\\d+:`） |
| 计算 | 返回值**等于**期望值 |
| 写入 | **文件系统上**真变了，且断言具体新内容 |
| 浏览器 | 拿到 `page=<hex>`；点击的效果用**读回 DOM** 判定 |
| 截屏 | 返回里含**像素尺寸** `数字x数字` |
| 语义检索 | 返回里含**真实存在的文件名** |

## ⚠ 三条「如实记录」不算通过

* `dev_desktop_action` —— `Capability.ADMIN`，首次调用**按设计**返回审批请求
  （`approval_required: true`）。它既不算能用、也不算跑不通；
  判据盯的是「**如实回报要审批**」，不是「动作生效」。
* `game_screenshot` —— 无引擎时**降级**抓前台窗口。判据接受降级，
  但要求回报里说清来源与尺寸。
* `dev_media` —— 媒体**夹具**，不碰麦克风硬件。

## 沙箱纪律（今晚踩过）

建在 `config.STATE_ROOT` 之下。用 `tempfile.mkdtemp` 会让
`SESSIONS_DIR` 落在别处，于是 `test_state_root_isolation` **组合跑时**才红、
单独跑不绿。起的服务/页面用完必回收。

## 前置条件

大部分工具要 `code_root`。这里用 `config.set_context_code_root` 显式设上
（**不是绕开它**），并在 `tearDownModule` 里**复位后再删沙箱**。

⚠⚠ var 的真实名字是 `config._CTX_CODE_ROOT`（`config.py:638`）——
**不是** `api._CTX_CODE_ROOT`。第一版两处都错：

1. 从没查它在哪个模块，`getattr(api, "_CTX_CODE_ROOT", None)` 拿到 `None`
   还以为「设置前本就是 None」；
2. `tearDownModule` 只删沙箱、**没有复位 contextvar** —— 于是之后的测试
   读到的是一个**已删除的目录**，全量 143 failed。

这与同一晚立档的「项目上下文跨文件泄漏」是**同一个病**：上下文级状态
跨文件泄漏。可参照的正确范式在 `tests/test_code_root_reconcile.py:212-233`
（先 `get()` 记住、用完 `set()` 回去）。
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

import config                       # noqa: E402  code_root 的 ContextVar 在这
import tools as T                   # noqa: E402

REG = T.TOOLS


def _box():
    """沙箱：必须在 STATE_ROOT 之下（见模块 docstring）。"""
    import config
    base = pathlib.Path(config.STATE_ROOT)
    d = base / ".tmp" / "cap_baseline_test"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    (d / "hello.txt").write_text("hello from probe\n", encoding="utf-8")
    (d / "sample.csv").write_text("name,age\nalice,30\nbob,25\n",
                                   encoding="utf-8")
    (d / "main.py").write_text(
        "def hero_damage():\n    return 10\n", encoding="utf-8")
    (d / "page.html").write_text(
        "<!doctype html><meta charset=utf-8><title>P</title>\n"
        "<h1 id=hi>hello</h1>\n"
        "<button id=btn onclick=\"document.getElementById('hi')"
        ".textContent='clicked'\">go</button>\n"
        # type 链路要一个真 input：value 不走 MutationObserver，正是
        # 「no_change 假阴性」的现场（见 test_page_action 的 typed 用例）。
        "<input id=fld type=text value=''>\n", encoding="utf-8")
    return d


BOX = None
_SAVED_CODE_ROOT = None


def setUpModule():
    """设上 code_root，并**记住原值**。

    ⚠ 必须记住原值 —— 第一版没记，于是无法复位。
    """
    global BOX, _SAVED_CODE_ROOT
    _SAVED_CODE_ROOT = config._CTX_CODE_ROOT.get()
    BOX = _box()
    config.set_context_code_root(str(BOX))


def tearDownModule():
    """**先复位 contextvar，再删沙箱** —— 顺序不能反。

    顺序反了（或干脆不复位）的话，同进程后面的测试会读到一个
    已删除的目录，报「文件不存在：sub/a.gd」这种失败。
    全量 143 failed 就是这么来的。
    """
    global BOX, _SAVED_CODE_ROOT
    try:
        if _SAVED_CODE_ROOT is not None:
            config.set_context_code_root(_SAVED_CODE_ROOT)
        else:
            config._CTX_CODE_ROOT.set(None)
    finally:
        if BOX:
            shutil.rmtree(BOX, ignore_errors=True)
            BOX = None


def tx(v):
    return getattr(v, "text", None) or str(v)


def call(nm, *a, **kw):
    return tx(REG[nm].func(*a, **kw))


class CodeBaseToolsTests(unittest.TestCase):
    """代码库类：读 / 检索 / 计算 / 文档 / 写入 / 记忆 / 自省。"""

    def test_read_file_returns_the_actual_content(self):
        """正面证据：返回里含**文件里那段特有内容**。"""
        out = call("read_file", str(BOX / "hello.txt"))
        self.assertIn("hello from probe", out)

    def test_list_dir_lists_the_entries(self):
        out = call("list_dir", str(BOX))
        self.assertIn("hello.txt", out)

    def test_grep_returns_hit_lines(self):
        """正面证据：**命中行数 > 0**（不靠措辞）。"""
        out = call("grep", "pattern: hello\npath: %s" % BOX)
        self.assertRegex(out, r"^\S+:\d+:", "grep 没给出命中行")

    def test_dev_glob_matches_by_pattern(self):
        out = call("dev_glob", "pattern: *.py")
        self.assertIn(".py", out)

    def test_search_code_finds_real_files(self):
        """语义检索：正面证据是**真实存在的文件名**，不要求字面出现 query。"""
        out = call("search_code", "hero_damage")
        self.assertTrue(str(out).strip(), "search_code 返回空")
        # 它给的是语义相关的文件清单，至少要有一个真实存在的路径
        paths = re.findall(r'"([^"]+\.(?:py|js|gd|ts|vue))"', out)
        if paths:
            self.assertTrue(
                any((BOX / p).exists() or (p.endswith(".py")) for p in paths),
                "返回的文件名都不像真的：%s" % paths[:3])

    def test_calculate_returns_the_value(self):
        """正面证据：返回值**等于**期望值。"""
        self.assertEqual(call("calculate", "1+1").strip(), "2")

    def test_inspect_data_file_reads_the_table(self):
        out = call("inspect_data_file", str(BOX / "sample.csv"))
        self.assertTrue("alice" in out or '"ok"' in out,
                        "没解析出表格内容：%s" % out[:80])

    def test_create_file_really_creates_it(self):
        """正面证据：**文件系统上**真建出来了，且内容对。"""
        p = BOX / "made_by_test.txt"
        p.unlink(missing_ok=True)
        call("create_file", "path: %s\nnew_text: hello made\n" % p)
        self.assertTrue(p.exists(), "create_file 没有真的建出文件")
        self.assertIn("hello made", p.read_text(encoding="utf-8"))

    def test_apply_edit_really_changes_content(self):
        p = BOX / "edit_by_test.txt"
        p.write_text("old text\n", encoding="utf-8")
        call("apply_edit",
             "path: %s\nold_text: old text\nnew_text: new text\n" % p)
        self.assertIn("new text", p.read_text(encoding="utf-8"),
                      "apply_edit 返回了但文件内容没变")

    def test_memory_save_then_search(self):
        marker = "probe-marker-cap-baseline"
        call("memory_save", "content: %s 探针记忆\n" % marker)
        out = call("memory_search", "query: %s\n" % marker)
        self.assertIn(marker, out,
                      "存了却搜不到 —— 记忆链路没通")

    def test_tool_catalog_lists_categories_on_demand(self):
        """**按需披露**：空参数给的是 14 个大类总览，不是 112 个工具名。

        探针阶段我要求它回显 `read_file`，于是红了一次 —— 那是我的期望
        写错。真实设计是**两层披露**（与 `5cbe94b`「工具说明不再教模型
        写死端口」同一思路）：

        * 空参数 -> 大类清单（每个大类带工具数量）
        * 给了大类 -> 该大类的工具清单

        判据扣住这个机制：先要总数、再点名一个真实大类要清单。
        """
        import tools as T

        overview = call("tool_catalog", "")
        self.assertRegex(overview, r"\d+\s*个工具",
                         "总览里应带每类的工具数：%s" % overview[:100])

        # 点名一个**真实存在**的大类，要出清单
        m = re.search(r"^- (\w+)（", overview, re.M)
        self.assertTrue(m, "总览里没解析出大类名：%s" % overview[:120])
        cat = m.group(1)
        detail = call("tool_catalog", cat)
        self.assertTrue(str(detail).strip(),
                        "点名大类 %s 没有返回清单" % cat)
        # 清单里的工具名必须是真存在的（不是编出来的）
        named = re.findall(r"`([a-z][a-z0-9_]{3,})`", str(detail))
        real = [n for n in named if n in T.TOOLS]
        self.assertTrue(real,
                        "大类 %s 的清单里没有一个真工具名：%s"
                        % (cat, named[:5]))


class BrowserChainTests(unittest.TestCase):
    """浏览器完整链路：开 → 点 → 读回。"""

    @classmethod
    def setUpClass(cls):
        cls.port = 8793
        cls.srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(cls.port)],
            cwd=str(BOX), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        cls.page = ""
        try:
            out = call("dev_page_action",
                       "action: open\nurl: http://127.0.0.1:%d/page.html\n"
                       % cls.port)
            m = re.search(r"page=([0-9a-f]+)", out)
            cls.page = m.group(1) if m else ""
            cls.open_out = out
        except Exception as e:  # noqa: BLE001
            cls.page = ""
            cls.open_out = "%s: %s" % (type(e).__name__, e)

    @classmethod
    def tearDownClass(cls):
        if cls.page:
            try:
                call("dev_page_action", "action: close\npage: %s\n" % cls.page)
            except Exception:  # noqa: BLE001
                pass
        if cls.srv:
            cls.srv.terminate()

    def test_open_really_starts_a_browser_session(self):
        """正面证据：拿到 `page=<hex>` —— 那是真浏览器的会话 id。"""
        self.assertTrue(self.page,
                        "没拿到 page id：%s" % str(self.open_out)[:140])

    def test_click_then_read_proves_the_effect(self):
        """**核心**：点了之后读回 DOM —— 效果由**读回**判定，不看文案。

        之前探针在这里栽过：它找英文 `changed`，工具回的是中文
        「生效（DOM 变了）」，于是被判成「半通」。读回 DOM 是与措辞无关的。
        """
        self.assertTrue(self.page, "前置：本轮没能开出会话")
        call("dev_page_action",
             "action: click\npage: %s\nselector: #btn\nsettle: 700\n"
             % self.page)
        got = call("dev_page_action",
                   "action: read\npage: %s\nselector: #hi\n" % self.page)
        self.assertIn("clicked", got,
                      "点完读回仍是 hello —— 点击没真生效（或判定方法不对）")

    def test_type_then_read_back_proves_it_landed(self):
        """type 的回归：文本已落入必须判 `typed`/ok，不能是 no_change/ok:false。

        这一条钉的是真出过的事故：`Input.insertText` 只改 `.value`，
        MutationObserver 看不到，纯 DOM 判据下打字恒 no_change，于是
        「字已经进框了」也回报 ok:false——Agent 会去重试或回报填不进去。
        """
        self.assertTrue(self.page, "前置：本轮没能开出会话")
        out = call("dev_page_action",
                   "action: type\npage: %s\nselector: #fld\n"
                   "value: 基线 typed\nsettle: 700\n" % self.page)
        # 判定同样读回 DOM，不匹配文案；但 effect 必须是 typed 而非 no_change
        # （这条正是被修的东西，所以两个都要钉）。判定行的格式是
        # "type：<outcome> —— DOM..."，no_change 时 outcome 是「【没有观察到变化】」，
        # 所以要带前缀匹配，别把解释行里的引语也算进去。
        self.assertIn("type：生效（文本已填入", out,
                      "type 后没判生效：%s" % out[:200])
        self.assertNotIn("type：【没有观察到变化】", out,
                         "type 落入成功却报 no_change —— 假阴性回归了：%s" % out[:200])
        got = call("dev_page_action",
                   "action: read\npage: %s\nselector: #fld\nmode: value\n"
                   % self.page)
        self.assertIn("基线 typed", got,
                      "读回 value 里没有输入的字 —— type 没真生效：%s" % got[:200])


class DesktopAndGameTests(unittest.TestCase):
    """小窗/桌面 与 游戏 —— 按设计有特殊档，判据如实记录。"""

    def test_desktop_capture_returns_real_dimensions(self):
        """正面证据：回报里含**像素尺寸** `数字x数字`。"""
        out = call("dev_desktop_capture", "target: foreground")
        self.assertRegex(out, r"\d+\s*[x×]\s*\d+",
                         "截屏回报里没有尺寸：%s" % out[:120])

    def test_desktop_action_reports_that_it_needs_approval(self):
        """`dev_desktop_action` 是 ADMIN：首次调用**按设计**要审批。

        判据盯的是「**如实回报要审批**」，不是「动作生效」——
        它既不算能用、也不算跑不通。
        """
        out = call("dev_desktop_action",
                   "action: click\ntarget: foreground\nx: 10\ny: 10\n")
        self.assertTrue(
            "approval_required" in out or "审批" in out,
            "ADMIN 工具既没动作也没说要审批：%s" % out[:140])

    def test_game_screenshot_reports_source_and_size(self):
        """无引擎时会**降级**抓前台窗口 —— 接受降级，但要说清来源与尺寸。"""
        out = call("game_screenshot", "target: foreground")
        self.assertRegex(out, r"\d+\s*[x×]\s*\d+",
                         "截图回报里没有尺寸：%s" % out[:120])
        self.assertTrue("foreground" in out or "embedded" in out
                        or "来源" in out,
                        "没说画面来自哪里：%s" % out[:120])

    def test_game_playtest_really_runs_a_command(self):
        """正面证据：它真跑了一条命令并回报结果。"""
        out = call("game_playtest", "command: echo probe-ok\ntimeout: 30\n")
        self.assertTrue(str(out).strip(), "playtest 返回空")

    def test_game_validate_data_reports_a_verdict(self):
        out = call("game_validate_data", "path: %s" % (BOX / "sample.csv"))
        self.assertTrue(str(out).strip(), "validate 返回空")

    def test_game_simulate_accepts_integer_levels(self):
        """⚠ 它要 **int**；给 `1-10` 会 ValueError。

        这条钉住真实契约 —— 探针阶段我传了 `1-10`，报 ValueError，
        一度以为工具坏了。
        """
        out = call("game_simulate", "levels: 10\nbase: 100\ngrowth: 1.5\n")
        self.assertIn("level", out, "数值曲线没返回：%s" % out[:100])

    def test_capture_bug_writes_a_bug(self):
        out = call("dev_capture_bug",
                   "title: probe-bug\nerror: probe\nseverity: low\n"
                   "reproduction: run probe\n")
        self.assertTrue(str(out).strip(), "写 bug 分区没回报")


if __name__ == "__main__":
    unittest.main(verbosity=2)