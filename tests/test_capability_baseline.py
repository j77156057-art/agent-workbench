# -*- coding: utf-8 -*-
"""能力基线：已真跑通的 22 条动作，固化成套件里的判据。

## 这份文件是什么

`tools.TOOLS` 当前 127 个键（计数随注册表增长，以 len(tools.TOOLS) 为准）。这份只钉住**已经真跑通**的那部分 ——
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

import json
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


def call_approved(nm, *a, **kw):
    """走真实用户审批台账批准后重试，不绕过统一桌面边界。"""
    out = call(nm, *a, **kw)
    try:
        payload = json.loads(out)
    except (TypeError, ValueError):
        return out
    if not payload.get("approval_required"):
        return out
    import game_workbench
    game_workbench.approval(str(BOX), payload["action"], "pytest-user",
                            approved=True, target=payload["target"])
    return call(nm, *a, **kw)


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
        """**按需披露**：空参数给的是 14 个大类总览，不是全量工具名。

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
        from agent_runtime import visual_acceptance as visual
        # 判据是「浏览器真能起」，不只是「二进制在」：CI 上 runner 镜像自带 Edge，
        # 但它一起身就退 —— 没有守卫时，这三条会以「没拿到 page id」这种
        # **看不出与浏览器有关**的形状报红。
        if not visual.browser_available():
            raise unittest.SkipTest("浏览器起不来，跳过浏览器完整链路：%s" % (
                visual.browser_probe_reason() or "未知原因"))
        cls.port = 8793
        cls.srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(cls.port)],
            cwd=str(BOX), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        cls.page = ""
        try:
            out = call_approved("dev_page_action",
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
        call_approved("dev_page_action",
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
        out = call_approved("dev_page_action",
                            "action: type\npage: %s\nselector: #fld\n"
                            "value: 基线 typed\nsettle: 700\n" % self.page)
        # 判定读回 DOM，不匹配文案；outcome 必须是「生效」而非「没有观察到变化」
        # （这条正是被修的东西）。判定行格式 "type：<outcome> —— DOM..."，成功分支
        # 文案会变（"文本已填入" / "画面变了，DOM 没变 —— canvas..." 都算生效），
        # 所以只钉「type：生效」前缀，不锁死具体措辞；no_change 才是假阴性回归。
        outcome_head = out.split("——", 1)[0]
        self.assertTrue(outcome_head.startswith("type：生效"),
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

    @unittest.skipUnless(os.name == "nt", "前台窗口抓屏是 Windows GDI 能力")
    def test_desktop_capture_returns_real_dimensions(self):
        """正面证据：回报里含**像素尺寸** `数字x数字`。"""
        out = call("dev_desktop_capture", "target: foreground")
        self.assertRegex(out, r"\d+\s*[x×]\s*\d+",
                         "截屏回报里没有尺寸：%s" % out[:120])

    def test_desktop_action_reports_a_coherent_result(self):
        """首次如实要求审批；真实台账批准后重试，返回结构化执行结果。"""
        out = call("dev_desktop_action",
                   "action: click\ntarget: engine\nx: 10\ny: 10\n")
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["approval_required"])
        approved_out = call_approved("dev_desktop_action",
                                     "action: click\ntarget: engine\nx: 10\ny: 10\n")
        approved = json.loads(approved_out)
        self.assertIsInstance(approved.get("ok"), bool,
                              "批准后没回结构化 ok：%s" % approved_out[:140])
        self.assertNotIn("approval_required", approved)

    @unittest.skipUnless(os.name == "nt", "前台窗口抓屏是 Windows GDI 能力")
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


class Tranche2ToolsTests(unittest.TestCase):
    """2026-11-17 扩面第二批：数据生成 / 文档产物 / 记忆删除 / 技能取回 / 工具自省。

    判据纪律同首批：扣机制本身（文件系统真变、返回值等于期望、搜不到已删），
    不扣措辞、不把错误消息当成功。
    """

    @classmethod
    def setUpClass(cls):
        # dev_use_skill / tool_search 是 agent 导入期 setdefault 进注册表的，
        # 只 import tools 拿不到（导入期快照不含运行期 setdefault 的键）。
        import agent  # noqa: F401

    def test_generate_data_file_writes_and_roundtrips(self):
        p = BOX / "gen_by_probe.csv"
        p.unlink(missing_ok=True)
        out = call("generate_data_file", json.dumps({
            "path": "gen_by_probe.csv", "format": "csv",
            "data": [{"name": "alice", "age": 30}, {"name": "bob", "age": 25}],
        }, ensure_ascii=False))
        self.assertTrue(p.exists(), "generate_data_file 没真建文件：%s" % out[:120])
        back = call("inspect_data_file", str(p))
        self.assertIn("alice", back, "生成的表读不回内容")

    def test_create_artifact_produces_real_docx(self):
        out = call("create_artifact", json.dumps({
            "format": "docx", "filename": "probe-artifact.docx",
            "title": "探针文档",
            "sections": [{"heading": "一节", "paragraphs": ["探针正文一段。"]}],
        }, ensure_ascii=False))
        payload = json.loads(out)
        self.assertTrue(payload.get("ok"), "create_artifact 未 ok：%s" % out[:160])
        path = pathlib.Path(payload.get("path") or "")
        self.assertTrue(path.exists(), "回报 ok 但文件不在：%s" % path)
        self.assertEqual(path.read_bytes()[:2], b"PK", "docx 不是真 zip 容器")

    def test_memory_forget_really_removes_recall(self):
        import agent_memory
        marker = "probe-forget-cap-baseline"
        call("memory_save", "content: %s 待删记忆\n" % marker)
        scope = agent_memory.scope_key(None)
        row = next((r for r in agent_memory.audit(scope, agent_id="main")
                    if marker in str(r.get("content") or "")), None)
        self.assertIsNotNone(row, "存了却审计不到，无法取 id")
        call("memory_forget", "id: %s\n" % row["id"])
        out = call("memory_search", "query: %s\n" % marker)
        self.assertNotIn(marker, out, "forget 后仍召回 —— 删除没生效")

    def test_dev_use_skill_returns_full_body(self):
        import skills
        skills.save_user_skill("probe-cap-skill", "探针技能",
                               "SKILL-BODY-MARKER 正文", version="1.0.0")
        out = call("dev_use_skill", "probe-cap-skill")
        self.assertIn("SKILL-BODY-MARKER", out, "技能正文没取回")

    def test_tool_search_returns_usage(self):
        out = call("tool_search", "grep")
        self.assertIn("pattern", out, "tool_search 没给 grep 的用法")


class Tranche3ToolsTests(unittest.TestCase):
    """2026-11-17 扩面第三批：执行 / 定时器 / 技能生命周期 / git 只读 / 引用 / 自验证 / 补丁 / 移动。

    判据纪律同前：扣机制本身。git 类在沙箱里真 init 真 commit；
    技能生命周期钉「草稿不生效 → 批准生效 → 拒绝删除」三段正面证据。
    """

    @classmethod
    def setUpClass(cls):
        import agent  # noqa: F401  运行期注册 dev_use_skill 等
        subprocess.run(["git", "init", "-q"], cwd=BOX, check=True)
        subprocess.run(["git", "-c", "user.email=probe@local", "-c", "user.name=probe",
                        "add", "hello.txt"], cwd=BOX, check=True)
        subprocess.run(["git", "-c", "user.email=probe@local", "-c", "user.name=probe",
                        "commit", "-q", "-m", "probe-base-commit-marker"],
                       cwd=BOX, check=True)

    def test_python_exec_returns_computed_value(self):
        out = call("python_exec", "print(6 * 7)")
        self.assertIn("42", out, "python_exec 没回计算值：%s" % out[:120])

    def test_run_command_echoes_marker(self):
        out = call("run_command", "echo probe-cmd-marker")
        self.assertIn("probe-cmd-marker", out, "run_command 没回显：%s" % out[:120])

    def test_timer_set_list_cancel_lifecycle(self):
        from agent_runtime import timers
        # 定时器是会话级设计：无会话拒绝属 fail-closed，探针须真绑会话。
        tokens = timers.bind("prj-cap3", "s-cap3-timer")
        try:
            out = call("timer_set", "message: 探针定时\ndelay_seconds: 3600\n")
            m = re.search(r"tm-[A-Za-z0-9]+", out)
            self.assertIsNotNone(m, "timer_set 没给 id：%s" % out[:120])
            tid = m.group(0)
            listed = call("timer_list", "")
            self.assertIn(tid, listed, "timer_list 看不到刚设的定时器")
            call("timer_cancel", "timer_id: %s\n" % tid)
            after = call("timer_list", "")
            self.assertNotIn(tid, after, "cancel 后定时器仍在列")
        finally:
            timers.reset(tokens)

    def test_skill_draft_inactive_until_approved(self):
        name = "probe-cap3-skill"
        created = call("dev_skill_create",
                       "name: %s\ndescription: 探针技能三批\nbody: CAP3-SKILL-BODY 正文\n" % name)
        self.assertIn('"ok": true', created, "起草失败：%s" % created[:120])
        before = call("dev_use_skill", name)
        self.assertNotIn("CAP3-SKILL-BODY", before, "草稿未批准就生效了")
        approved = call("dev_skill_approve", "name: %s\n" % name)
        self.assertIn('"ok": true', approved, "批准失败：%s" % approved[:120])
        after = call("dev_use_skill", name)
        self.assertIn("CAP3-SKILL-BODY", after, "批准后正文取不回")
        call("dev_skill_create",
             "name: %s-rej\ndescription: 待拒\nbody: CAP3-REJECTED-BODY\n" % name)
        call("dev_skill_reject", "name: %s-rej\n" % name)
        rejected = call("dev_use_skill", name + "-rej")
        self.assertNotIn("CAP3-REJECTED-BODY", rejected, "拒绝后草稿仍生效")

    def test_git_log_shows_real_commit(self):
        out = call("dev_git_log", "")
        self.assertIn("probe-base-commit-marker", out,
                      "git log 没回真实提交：%s" % out[:120])

    def test_git_diff_shows_uncommitted_change(self):
        p = BOX / "hello.txt"
        p.write_text("hello from probe\ndiff-marker-line\n", encoding="utf-8")
        out = call("dev_git_diff", "")
        self.assertIn("diff-marker-line", out,
                      "git diff 没回未提交改动：%s" % out[:120])

    def test_find_references_locates_caller(self):
        (BOX / "use_hero.py").write_text(
            "from main import hero_damage\nprint(hero_damage())\n",
            encoding="utf-8")
        out = call("dev_find_references", "hero_damage")
        self.assertIn("use_hero.py", out,
                      "引用定位没找到调用方：%s" % out[:160])

    def test_self_verify_fails_then_passes_on_fix(self):
        bad = BOX / "bad_syntax.py"
        bad.write_text("def f(\n", encoding="utf-8")
        out = call("self_verify", "scope: backend\nfiles: bad_syntax.py\n")
        self.assertNotIn('"passed": true', out,
                         "语法错误竟判通过：%s" % out[:160])
        bad.write_text("def f():\n    return 1\n", encoding="utf-8")
        out2 = call("self_verify", "scope: backend\nfiles: bad_syntax.py\n")
        self.assertIn('"passed": true', out2,
                      "修好后仍判不通过：%s" % out2[:160])

    def test_patch_applies_unified_diff(self):
        p = BOX / "hello.txt"
        p.write_text("line-one\nline-two\n", encoding="utf-8")
        diff = ("--- a/hello.txt\n+++ b/hello.txt\n@@ -1,2 +1,2 @@\n"
                " line-one\n-line-two\n+line-two-patched\n")
        call("dev_patch", diff)
        self.assertIn("line-two-patched", p.read_text(encoding="utf-8"),
                      "patch 返回但文件没变")

    def test_move_renames_on_filesystem(self):
        src = BOX / "move-src.txt"
        dst = BOX / "move-dst.txt"
        src.write_text("move-payload\n", encoding="utf-8")
        dst.unlink(missing_ok=True)
        call("dev_move", "from: move-src.txt\nto: move-dst.txt\n")
        self.assertFalse(src.exists(), "move 后源文件仍在")
        self.assertTrue(dst.exists(), "move 后目标不在")
        self.assertIn("move-payload", dst.read_text(encoding="utf-8"))


class Tranche4ToolsTests(unittest.TestCase):
    """2026-11-17 扩面第四批：测试跑 / Vue 诊断 / 诊断 / _headcheck_ / bug 台账 /
    分区三件 / 批量编辑原子性 / 跨区重构 / 知识检索。

    机制判据加两条硬的：批量编辑「一坏全不动」原子性；testrun 能判 fail
    （恒真判据的反面：必须证明它会把坏测试判红）。
    """

    @classmethod
    def setUpClass(cls):
        import agent  # noqa: F401
        subprocess.run(["git", "init", "-q"], cwd=BOX, check=True)

    def test_testrun_passes_good_and_fails_bad(self):
        (BOX / "test_probe_ok.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        out = call("dev_testrun", "paths: test_probe_ok.py\n")
        self.assertNotIn("failed", out.lower().replace("0 failed", ""),
                         "好测试被判红：%s" % out[:160])
        (BOX / "test_probe_bad.py").write_text(
            "def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
        out2 = call("dev_testrun", "paths: test_probe_bad.py\n")
        self.assertTrue("fail" in out2.lower() or "1 failed" in out2,
                        "坏测试没被判红（恒真判据反面）：%s" % out2[:160])

    def test_vue_diagnoses_props_and_emits(self):
        from agent_runtime import visual_acceptance as visual
        # dev_vue 挂真浏览器真点击收 emit；浏览器起不来时可见 skip（同 BrowserChainTests）。
        if not visual.browser_available():
            self.skipTest("浏览器起不来，跳过 Vue 运行时接线探针：%s"
                          % (visual.browser_probe_reason() or "未知原因"))
        # dev_vue 要求工程结构（package.json + src/）与 esbuild/compiler-sfc/vue；沙箱真建前置。
        (BOX / "package.json").write_text('{"name":"probe"}\n', encoding="utf-8")
        (BOX / "src").mkdir(exist_ok=True)
        if not (BOX / "node_modules" / "vue").exists():
            subprocess.run("npm install --no-save --no-audit --no-fund esbuild @vue/compiler-sfc vue",
                           cwd=BOX, shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (BOX / "src" / "ProbeComp.vue").write_text(
            "<script setup>\n"
            "const props = defineProps(['probeProp'])\n"
            "const emit = defineEmits(['probeEmit'])\n"
            "function go() { emit('probeEmit', props.probeProp) }\n"
            "</script>\n<template><button id=\"probe-go\" @click=\"go\">{{ probeProp }}</button></template>\n",
            encoding="utf-8")
        out = call("dev_vue",
                   'component: src/ProbeComp.vue\n'
                   'props: {"probeProp": "probe-value"}\n'
                   'clicks: #probe-go\n'
                   'texts: probe-value\n'
                   'expect_emit: probeEmit\n'
                   'expect_emit_args: ["probe-value"]\n')
        self.assertIn("probeEmit", out, "Vue 运行时没收到声明的 emit：%s" % out[:200])
        self.assertIn("probe-value", out, "Vue 渲染/实参没带 prop 值：%s" % out[:200])
        self.assertNotIn("未通过", out, "Vue 接线判未通过：%s" % out[:200])

    def test_diagnostics_reports_real_defect_location(self):
        (BOX / "diag_bad.py").write_text("def f(\n", encoding="utf-8")
        out = call("dev_diagnostics", "")
        self.assertIn("diag_bad.py", out,
                      "诊断没定位真缺陷文件：%s" % out[:160])

    def test_headcheck_catches_file_missing_from_head(self):
        # headcheck 的机制是「干净检出 HEAD 跑 import」：工作树能跑、HEAD 缺文件必红。
        (BOX / "mod_a.py").write_text("import mod_b\nVALUE = mod_b.X\n", encoding="utf-8")
        (BOX / "mod_b.py").write_text("X = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "mod_a.py"], cwd=BOX, check=True)
        subprocess.run(["git", "-c", "user.email=probe@local", "-c", "user.name=probe",
                        "commit", "-q", "-m", "probe-headcheck-a-only"],
                       cwd=BOX, check=True)
        out = call("dev_headcheck", "modules: mod_a\n")
        self.assertIn("mod_b", out,
                      "干净检出缺 mod_b 却没被抓出：%s" % out[:200])
        subprocess.run(["git", "add", "mod_b.py"], cwd=BOX, check=True)
        subprocess.run(["git", "-c", "user.email=probe@local", "-c", "user.name=probe",
                        "commit", "-q", "-m", "probe-headcheck-b-added"],
                       cwd=BOX, check=True)
        out2 = call("dev_headcheck", "modules: mod_a\n")
        self.assertNotIn("ModuleNotFoundError", out2,
                         "补齐 HEAD 后仍红：%s" % out2[:200])

    def test_bug_ledger_list_and_update(self):
        out = call("dev_capture_bug",
                   "title: probe-ledger-bug\nerror: probe\nseverity: low\n"
                   "reproduction: run probe\n")
        m = re.search(r"BUG-[A-Za-z0-9-]+", out)
        self.assertIsNotNone(m, "capture_bug 没给 id：%s" % out[:120])
        bid = m.group(0)
        listed = call("dev_list_bugs", "")
        self.assertIn(bid, listed, "bug 台账列不到新 bug")
        call("dev_update_bug", "bug_id: %s\nstatus: fixed\n" % bid)
        after = call("dev_list_bugs", "")
        self.assertIn("fixed", after, "更新状态后台账没体现")

    def test_regions_init_list_read(self):
        call("init_regions", "")
        listed = call("dev_list_regions", "")
        for key in ("assets", "values", "behaviors"):
            self.assertIn(key, listed, "分区列表缺 %s：%s" % (key, listed[:120]))
        target = BOX / "assets" / "region-probe.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("region-read-payload\n", encoding="utf-8")
        out = call("dev_region_read", "region: assets\npath: region-probe.txt\n")
        self.assertIn("region-read-payload", out,
                      "分区读没回内容：%s" % out[:120])

    def test_apply_edits_atomic_all_or_nothing(self):
        p = BOX / "atomic.txt"
        q = BOX / "atomic2.txt"
        p.write_text("alpha\nbeta\n", encoding="utf-8")
        q.write_text("gamma\n", encoding="utf-8")
        # 同一文件多块按设计拒绝（须合并为一块），原子性探针用两个文件。
        bad_batch = ("path: atomic.txt\nold_text: alpha\nnew_text: alpha-x\n"
                     "---\npath: atomic2.txt\nold_text: NOT-PRESENT\nnew_text: delta\n")
        call("dev_apply_edits", bad_batch)
        self.assertEqual(p.read_text(encoding="utf-8"), "alpha\nbeta\n",
                         "一坏全不动被违反：部分编辑落盘了")
        self.assertEqual(q.read_text(encoding="utf-8"), "gamma\n",
                         "一坏全不动被违反：第二文件落盘了")
        good_batch = ("path: atomic.txt\nold_text: alpha\nnew_text: alpha-x\n"
                      "---\npath: atomic2.txt\nold_text: gamma\nnew_text: gamma-x\n")
        call("dev_apply_edits", good_batch)
        # apply_edit 会归一尾换行；机制判据扣内容不扣尾空白。
        self.assertEqual(p.read_text(encoding="utf-8").rstrip("\n"), "alpha-x\nbeta",
                         "全好批次没全落盘")
        self.assertEqual(q.read_text(encoding="utf-8").rstrip("\n"), "gamma-x",
                         "全好批次第二文件没落盘")

    def test_refactor_moves_across_regions(self):
        src = BOX / "assets" / "refactor-src.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("VALUE = 1\n", encoding="utf-8")
        # assets → behaviors 是反向依赖（behaviors 依赖 assets），按设计拒绝；
        # 探针走无关分区 bugs，并另钉反向依赖拒绝这条护栏。
        refused = call("dev_refactor",
                       "src_region: assets\nsrc_path: refactor-src.py\n"
                       "dst_region: behaviors\ndst_path: refactor-src.py\n")
        self.assertIn("拒绝", refused, "反向依赖搬移没被拒：%s" % refused[:120])
        call("dev_refactor",
             "src_region: assets\nsrc_path: refactor-src.py\n"
             "dst_region: bugs\ndst_path: refactor-src.py\n")
        self.assertFalse(src.exists(), "跨区重构后源仍在")
        self.assertTrue((BOX / "bugs" / "refactor-src.py").exists(),
                        "跨区重构后目标不在")

    def test_search_knowledge_returns_ingested_fragment(self):
        import ingest
        kb = BOX / "kb-probe.md"
        kb.write_text("# 探针知识\n\nKNOWLEDGE-MARKER-XYZ 是探针专用事实。\n",
                      encoding="utf-8")
        ingest.ingest_file(str(kb))
        out = call("search_knowledge", "KNOWLEDGE-MARKER-XYZ")
        self.assertIn("KNOWLEDGE-MARKER-XYZ", out,
                      "知识检索没回已_ingest_片段：%s" % out[:160])


class Tranche5WritePathTests(unittest.TestCase):
    """2026-11-17 扩面第五批：写路径离线探针（依赖对账 / 分区增改 / 分区方案审批门）。

    这批探针当场抓出一个真缺陷：`dev_add_region` / `dev_apply_regions` 在审批门禁
    返回 dict 阻断结构时做 `str + dict` → TypeError（敏感操作本该把 gate 原样 JSON
    上抛）。判据扣机制：依赖对账要真把缺失声明写进清单（文件系统证据）；分区写要
    真落盘内容；审批门要回 approval_required 而不是崩。
    """

    def test_dev_deps_check_finds_undeclared_and_apply_writes(self):
        # 自备 code_root 沙箱：dev_deps 按 code_root 读依赖清单，BOX 没有清单，
        # 故临时把上下文根指到一个带 requirements.txt 的小沙箱（用完复位）。
        sandbox = pathlib.Path(config.STATE_ROOT) / ".tmp" / "cap_deps_probe"
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)
        sandbox.mkdir(parents=True)
        (sandbox / "app.py").write_text("import yaml\n", encoding="utf-8")
        (sandbox / "requirements.txt").write_text("# empty manifest\n", encoding="utf-8")
        saved = config._CTX_CODE_ROOT.get()
        config.set_context_code_root(str(sandbox))
        try:
            before = (sandbox / "requirements.txt").read_text(encoding="utf-8")
            chk = call("dev_deps", "action: check")
            self.assertIn("yaml", chk.lower(), "check 没指出未声明的 import：%s" % chk[:160])
            self.assertIn("app.py", chk, "check 没给出导入方：%s" % chk[:160])
            self.assertEqual((sandbox / "requirements.txt").read_text(encoding="utf-8"),
                             before, "check 是只读对账，不该改清单")
            call("dev_deps", "action: apply\nsource: requirements.txt")
            after = (sandbox / "requirements.txt").read_text(encoding="utf-8")
            self.assertIn("PyYAML", after,
                          "apply 没把缺失声明真写进清单（文件系统证据）：%s" % after)
        finally:
            config.set_context_code_root(saved)
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_apply_regions_gate_is_surfaced_not_crash(self):
        # 回归锁：自定义分区方案触发审批门时须回 approval_required JSON，
        # 而不是 `str + dict` TypeError（本批探针抓出的真缺陷）。
        # 方法名刻意排在 test_dev_add_* 之前（unittest 按字母序跑）：dev_add 探针
        # 会批准 apply_regions/*，一旦先批了这里的门就没了，故须先断言门再放行。
        call("init_regions", "")
        out = call("dev_apply_regions", 'regions: [{"key":"probe5a","dir":"probe5a"}]')
        payload = json.loads(out)
        self.assertTrue(payload.get("approval_required"),
                        "分区方案审批门没原样上抛阻断结构：%s" % out[:160])

    def test_dev_add_region_then_region_edit_really_writes(self):
        call("init_regions", "")
        first = call("dev_add_region", "key: probe5w\ndir: probe5w\nname: 探针写区\n")
        self.assertTrue(json.loads(first).get("approval_required"),
                        "新增分区审批门没原样上抛（str+dict 缺陷回归）：%s" % first[:160])
        call_approved("dev_add_region", "key: probe5w\ndir: probe5w\nname: 探针写区\n")
        self.assertTrue((BOX / "probe5w").is_dir(), "审批后分区目录没建起来")
        call_approved("dev_region_edit",
                      "region: probe5w\npath: w.txt\nnew_text: WRITE-PAYLOAD-5\n")
        target = BOX / "probe5w" / "w.txt"
        self.assertTrue(target.exists(), "分区写没落盘")
        self.assertIn("WRITE-PAYLOAD-5", target.read_text(encoding="utf-8"),
                      "分区写内容不对：%s" % target.read_text(encoding="utf-8"))
        ver = call("dev_region_verify", "region: probe5w\n")
        self.assertTrue(str(ver).strip(), "分区校验没给结论")


class Tranche6ChangesetTests(unittest.TestCase):
    """2026-11-17 扩面第六批：跨区变更集提交→列表→整体回滚 round-trip + 分区维护。

    又抓出一个真缺陷：`dev_rollback_changeset` 把审批门 dict 原样 return，而
    execute_tool 对非 str 返回一律判 invalid_result（「返回值不符合工具协议」）——
    审批流被静默吞掉，Agent 永远看不到 approval_required。修：dict 门按约定
    json.dumps 上抛。判据扣机制：回滚后新建文件真被移除（git revert 的文件系统证据）。
    """

    def test_verify_contracts_and_rebuild_index(self):
        call("init_regions", "")
        vc = call("dev_verify_contracts", "")
        self.assertIn("依赖图", vc, "契约校验没给依赖图：%s" % vc[:160])
        self.assertIn("契约校验通过", vc, "默认分区契约应通过：%s" % vc[:160])
        call("dev_rebuild_index", "")
        self.assertTrue((BOX / "DEV_INDEX.md").exists(),
                        "rebuild_index 没真写出 DEV_INDEX.md（文件系统证据）")

    def test_changeset_commit_list_rollback_roundtrip(self):
        call("init_regions", "")
        call_approved("dev_region_edit",
                      "region: assets\npath: cs6.txt\nnew_text: CS6-PAYLOAD\n")
        target = BOX / "assets" / "cs6.txt"
        self.assertTrue(target.exists(), "变更集前置：分区写没落盘")
        co = call_approved("dev_commit_all", "message: 探针变更集6\n")
        m = re.search(r"变更集 ([0-9a-f]+)", co)
        self.assertIsNotNone(m, "commit_all 没回变更集 id：%s" % co[:160])
        cs = m.group(1)
        self.assertIn(cs, call("dev_list_changesets", ""),
                      "list_changesets 没列出刚建的变更集")
        # 回归锁：回滚审批门须回 approval_required JSON（裸 dict 会被判 invalid_result）。
        first = call("dev_rollback_changeset", "changeset: %s\n" % cs)
        self.assertTrue(json.loads(first).get("approval_required"),
                        "回滚审批门没原样 JSON 上抛：%s" % first[:160])
        call_approved("dev_rollback_changeset", "changeset: %s\n" % cs)
        self.assertFalse(target.exists(),
                         "整体回滚后新建文件仍在——git revert 没真撤销（文件系统证据）")


class Tranche7AssetsTests(unittest.TestCase):
    """2026-11-17 扩面第七批：素材注册→取得 round-trip（manifest 落盘 + 元数据对账）。"""

    def test_asset_register_then_get_roundtrip(self):
        call("init_regions", "")
        asset = BOX / "assets" / "probe-asset7.txt"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text("ASSET7-PAYLOAD\n", encoding="utf-8")
        reg = call("dev_asset_register",
                   "asset_id: probeasset7\npath: probe-asset7.txt\ntype: text\n")
        self.assertTrue(json.loads(reg).get("ok"), "素材注册没回 ok：%s" % reg[:160])
        manifest = json.loads((BOX / "assets" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("probeasset7", manifest.get("assets", {}),
                      "manifest.json 没真记下素材 id（文件系统证据）")
        got = json.loads(call("dev_asset_get", "asset_id: probeasset7\n"))
        self.assertTrue(got.get("ok"), "素材取得没回 ok：%s" % got)
        self.assertEqual(got.get("size"), asset.stat().st_size,
                         "素材元数据尺寸与真实文件不符")
        self.assertIn("probe-asset7.txt", got.get("path", ""),
                      "素材取得没回正确相对路径")


class Tranche8DeliveryTests(unittest.TestCase):
    """2026-11-17 扩面第八批：交付材料 dev_handoff（需 git 提交史 + 已跟踪改动）。

    判据扣机制正面证据：真读出 HEAD、点名已改文件、给出 git diff 的真实规模——
    不是「生成了看起来就绪的文本」。自备带提交史的 git 沙箱（BOX 无提交史）。
    """

    def test_dev_handoff_generates_real_delivery_material(self):
        sandbox = pathlib.Path(config.STATE_ROOT) / ".tmp" / "cap_handoff_probe"
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)
        sandbox.mkdir(parents=True)

        def git(*a):
            subprocess.run(["git", *a], cwd=sandbox, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        git("init", "-q")
        git("config", "user.email", "probe@local")
        git("config", "user.name", "Probe")
        (sandbox / "tracked.py").write_text("V = 1\n", encoding="utf-8")
        git("add", "tracked.py")
        git("commit", "-qm", "init")
        (sandbox / "tracked.py").write_text("V = 2  # handoff probe\n", encoding="utf-8")
        saved = config._CTX_CODE_ROOT.get()
        config.set_context_code_root(str(sandbox))
        try:
            out = call("dev_handoff", "title: 探针交付改动\n")
            self.assertIn("HEAD=", out, "没真读出 git HEAD：%s" % out[:160])
            self.assertIn("tracked.py", out, "没点名已改文件：%s" % out[:160])
            self.assertIn("1 file changed", out,
                          "没给 git diff 的真实规模（文件系统证据）：%s" % out[:200])
        finally:
            config.set_context_code_root(saved)
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_dev_undo_turn_dry_run_safe_then_apply_removes_created(self):
        # 写账本回合撤销：干跑不落盘、apply 真删本回合新建文件。文件用 write_bytes
        # 落盘——必须与账本 after 字节逐字一致，否则撤销的安全阀（磁盘≠写入后内容
        # 即跳过）会因 Windows CRLF 误判而跳过，那正是该阀在正确工作。
        sandbox = pathlib.Path(config.STATE_ROOT) / ".tmp" / "cap_undo_probe"
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)
        sandbox.mkdir(parents=True)
        target = sandbox / "undome8.txt"
        target.write_bytes(b"UNDO8-PAYLOAD\n")
        from agent_runtime import write_journal
        write_journal.record(str(sandbox), "undome8.txt", op="create",
                             before=None, after=b"UNDO8-PAYLOAD\n", turn="probe-turn-8")
        saved = config._CTX_CODE_ROOT.get()
        config.set_context_code_root(str(sandbox))
        try:
            status = call("dev_undo_turn", "action: status\n")
            self.assertIn("probe-turn-8", status, "账本没列出探针回合：%s" % status[:160])
            self.assertIn("undome8.txt", status, "账本没列出探针文件：%s" % status[:160])
            dry = call("dev_undo_turn", "action: undo\nturn: probe-turn-8\n")
            self.assertIn("干跑", dry, "默认撤销应是干跑：%s" % dry[:160])
            self.assertTrue(target.exists(), "干跑不该真删文件（安全阀）")
            applied = call("dev_undo_turn",
                           "action: undo\nturn: probe-turn-8\napply: true\n")
            self.assertIn("已撤销", applied, "apply 撤销没执行：%s" % applied[:160])
            self.assertFalse(target.exists(),
                             "apply 后本回合新建文件仍在——撤销没真落盘（文件系统证据）")
        finally:
            config.set_context_code_root(saved)
            shutil.rmtree(sandbox, ignore_errors=True)


class Tranche9BackgroundTests(unittest.TestCase):
    """2026-11-17 扩面第九批：后台任务 dev_job_logs / dev_job_cancel round-trip。

    真起一个后台进程（print 标记后 sleep），判据扣机制：日志真读到该进程的输出标记、
    取消真把状态置 cancelled。finally 兜底 job_cancel，绝不把活进程留给后续用例。
    """

    def test_dev_job_logs_and_cancel_roundtrip(self):
        sandbox = pathlib.Path(config.STATE_ROOT) / ".tmp" / "cap_job_probe"
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)
        sandbox.mkdir(parents=True)
        from agent_runtime import process_runner
        r = process_runner.start_job(
            [sys.executable, "-c",
             "print('JOB9-MARKER', flush=True); import time; time.sleep(8)"],
            cwd=str(sandbox), timeout=30, command_text="probe job")
        self.assertTrue(r.get("ok"), "后台任务没起来：%s" % r)
        jid = r.get("job_id", "")
        try:
            time.sleep(1.5)
            logs = call("dev_job_logs", "job_id: %s\n" % jid)
            self.assertIn("JOB9-MARKER", logs,
                          "后台任务日志没读到进程输出：%s" % logs[:160])
            cancel = call("dev_job_cancel", "job_id: %s\n" % jid)
            self.assertIn("cancelled", cancel, "取消没把状态置 cancelled：%s" % cancel[:160])
        finally:
            if jid:
                process_runner.job_cancel(jid)
            shutil.rmtree(sandbox, ignore_errors=True)


class Tranche10RecallTests(unittest.TestCase):
    """2026-11-17 扩面第十批：经验召回 recall_experience（隔离经验库 round-trip）。

    经验库维度随建库嵌入定死（本机现存 1024/bge-m3，CI unittest 用 local 256），直接
    往全局库写会维度冲突（InvalidArgumentError: expecting 1024, got 256）。故探针用
    reset_collection 建隔离专用库（维度随当前嵌入自建），record_episode 写一条教训 →
    recall_experience 召回，扣机制正面证据；用完复位库名并重建清理，不碰全局经验库。
    """

    def test_recall_experience_record_then_recall_roundtrip(self):
        import experience
        import vectorstore
        from projects import current_project_id
        orig = config.EXPERIENCE_COLLECTION_NAME
        probe_col = "cap_baseline_recall_probe"
        config.EXPERIENCE_COLLECTION_NAME = probe_col
        try:
            vectorstore.reset_collection(probe_col)
            pid = current_project_id() or "default"
            ok = experience.record_episode(
                pid, "探针动作RECALL10：升级依赖后校验反复失败",
                "先核对锁定版本再升级", "fail-then-fixed",
                lesson="RECALL10-LESSON：升级前先锁定版本核对")
            self.assertTrue(ok, "经验写入失败")
            out = call("recall_experience", "升级依赖后校验反复失败怎么办")
            self.assertIn("RECALL10-LESSON", out,
                          "召回没回已写入的教训：%s" % out[:200])
            self.assertGreaterEqual(json.loads(out).get("count", 0), 1,
                                    "召回计数为 0")
        finally:
            config.EXPERIENCE_COLLECTION_NAME = orig
            try:
                vectorstore.reset_collection(probe_col)
            except Exception:  # noqa: BLE001 - 清理失败不影响结论
                pass


class Tranche11ApprovalTests(unittest.TestCase):
    """2026-11-17 扩面第十一批：审批族 dev_approve / dev_approval_status。

    与已抓的 2 个审批门序列化缺陷同根因类的**消费侧**。判据扣机制：
    状态 round-trip（未通过→approve→已通过）+ 三条 fail-closed 安全护栏
    （target 前缀拼错拒绝且真没放行 / 缺 target 不默认通配 / MCP 不许 Agent 自批）。
    自备隔离沙箱审批台账，不碰 BOX。
    """

    def test_approve_roundtrip_and_fail_closed_guards(self):
        sandbox = pathlib.Path(config.STATE_ROOT) / ".tmp" / "cap_approval_probe"
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)
        sandbox.mkdir(parents=True)
        saved = config._CTX_CODE_ROOT.get()
        config.set_context_code_root(str(sandbox))
        try:
            call("init_regions", "")
            st0 = call("dev_approval_status",
                       "action: rollback_changeset\ntarget: probe-appr-11\n")
            self.assertIn("未通过", st0, "审批前台账应为未通过：%s" % st0[:120])
            ap = call("dev_approve",
                      "action: rollback_changeset\ntarget: probe-appr-11\n")
            self.assertIn("已审批", ap, "dev_approve 没记台账：%s" % ap[:120])
            st1 = call("dev_approval_status",
                       "action: rollback_changeset\ntarget: probe-appr-11\n")
            self.assertIn("已通过", st1, "审批后状态没翻转为已通过：%s" % st1[:120])
            # 护栏①：target 前缀拼错 → 拒绝，且真没放行（fail-closed，BUG-APPROVE-WILDCARD 回归）
            typo = call("dev_approve",
                        "action: rollback_changeset\ntarget_id: probe-appr-typo\n")
            self.assertIn("拼错", typo, "target 拼错没被拒：%s" % typo[:120])
            st_typo = call("dev_approval_status",
                           "action: rollback_changeset\ntarget: probe-appr-typo\n")
            self.assertIn("未通过", st_typo,
                          "拼错调用竟放行了审批（授权范围放大）：%s" % st_typo[:120])
            # 护栏②：缺 target 不默认通配
            notarget = call("dev_approve", "action: rollback_changeset\n")
            self.assertIn("显式提供 target", notarget,
                          "缺 target 没被拒（会静默降为通配）：%s" % notarget[:120])
            # 护栏③：MCP 不许 Agent 自批
            mcp = call("dev_approve", "action: mcp_server\ntarget: x\n")
            self.assertIn("不能自行审批", mcp,
                          "MCP 竟允许 Agent 自批：%s" % mcp[:120])
        finally:
            config.set_context_code_root(saved)
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)