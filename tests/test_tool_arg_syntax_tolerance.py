"""工具的**参数语法**必须容得下模型实际写出的形态——一类"死在纯语法上"的兜底。

## 实测故障（godot_sample 定位"扣血在哪算"）

    定位代码  pattern: take_damage|CONTACT_DAMAGE  -> behaviors\\enemy.gd:7: CONTACT_DAMAGE = 10
    读取文件  path: behaviors/player.gd start: 1 end: 55   -> 文件不存在：behaviors/player.gd start: 1 end: 55
    读取文件  path: behaviors\\enemy.gd  start: 1 end: 50   -> 文件不存在：...
    读取文件  path: behaviors/player.gd start: 1 end: 52   -> 文件不存在：...
    读取文件  path: behaviors\\enemy.gd  start: 1 ...      -> 文件不存在：...

模型**文件定位是对的**（grep 命中了正确的文件与行号），却因为 `read_file` 不认
行内 `start:` / `end:` 而四次全部打不开。

而报错把整串原样回显成路径 —— 看起来像路径写错，于是模型去换分隔符、
再试四次，全失败。最后归因成"上游/中转返回空内容，直接重试通常就能拿到"。

**重试多少次都没用**：文件一直是存在的。

## 根因：两个工具的解析器不一致

`grep` 的参数解析器本来就有行内处理（`_RE_GREP_SCOPE_INLINE` / `_RE_GREP_PATTERN_KEY`），
`read_file` 却只认「独占一行」。工具说明写的是独占行，模型写的是行内，
而模型才是实际调用方。

## 与本项目其它 bug 同形

失败信号指向的对象不是真正的原因：分隔符？项目根？provider？都不是——是参数语法。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
import tools  # noqa: E402
from agent_runtime import capacity  # noqa: E402


class ReadFileArgumentSyntaxTests(unittest.TestCase):
    """模型实际会写的几种写法，都必须能打开同一个存在的文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        self.target = os.path.join(self.tmp.name, "behaviors", "player.gd")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("\n".join("line%d" % i for i in range(1, 121)))
        patcher = patch.object(tools, "_get_code_root", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _opens(self, arg):
        out = tools.read_file(arg)
        text = out if isinstance(out, str) else str(out)
        return "不存在" not in text and "拒绝访问" not in text and bool(text.strip())

    def test_inline_start_and_end(self):
        """本次故障的**确切写法**：模型把区间参数写在同一行。"""
        self.assertTrue(
            self._opens("behaviors/player.gd start: 1 end: 55"),
            tools.read_file("behaviors/player.gd start: 1 end: 55")[:90])

    def test_inline_with_backslash_separator(self):
        self.assertTrue(self._opens("behaviors\\player.gd start: 1 end: 55"))

    def test_inline_start_only(self):
        self.assertTrue(self._opens("behaviors/player.gd start: 1"))

    def test_comma_separated(self):
        """grep 早就能认这种写法——两个工具不该不一致。"""
        self.assertTrue(self._opens("behaviors/player.gd, start: 1, end: 55"))

    def test_newline_separated_still_works(self):
        """工具说明里的原写法不能被这次改动破坏。"""
        self.assertTrue(self._opens("behaviors/player.gd\nstart: 1\nend: 55"))

    def test_no_range_still_works(self):
        self.assertTrue(self._opens("behaviors/player.gd"))

    def test_the_range_is_actually_applied(self):
        """不只是"没报错"——区间必须真的生效，否则就成了放行一切。"""
        out = tools.read_file("behaviors/player.gd start: 10 end: 12")
        self.assertIn("line10", out)
        self.assertIn("line12", out)
        self.assertNotIn("line13", out,
                         "end: 没生效——宽容解析不能变成忽略区间参数")

    def test_a_genuinely_missing_file_still_says_so(self):
        """容错不等于吞掉真错误。"""
        out = tools.read_file("behaviors/nope.gd start: 1 end: 5")
        self.assertIn("文件不存在", out)
        self.assertNotIn("区间参数", out,
                         "路径真的不存在时不该提区间参数——那会把人引向错误的排查方向")


class PathArgumentIsConsistentAcrossToolsTests(unittest.TestCase):
    """三个吃路径的工具，对"路径该怎么写"必须**一致**。

    ## 为什么这条比逐个测试更重要

    同一个问题在本次会话里连撞了三层，每层都是"上一层修好、下一层撞墙"：

    | 层 | 模型写法 | 谁认 | 谁不认 |
    |---|---|---|---|
    | 1 | `a.gd start: 1 end: 55`（行内） | grep | read_file |
    | 2 | `path: a.gd start: 1`（多一个键名） | grep / list_dir | read_file |
    | 3 | 待观察 | ? | ? |

    每层都是靠**用户拿真实 trace 来撞**才发现的，而不是被任何测试挡住。

    所以这里不逐个列举写法，而是直接断言：`read_file` 必须接受 `list_dir` 与
    `grep` 已经接受的那些形态。下一层若是这三者之一先坏掉，这条会红；
    若是 read_file 又漏了新形态，下面那份「形态清单」会红。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        for name in ("player.gd", "enemy.gd"):
            with open(os.path.join(self.tmp.name, "behaviors", name),
                      "w", encoding="utf-8") as handle:
                handle.write("\n".join("line%d" % i for i in range(1, 61)))
        for fn in ("_get_code_root",):
            patcher = patch.object(tools, fn, return_value=self.tmp.name)
            patcher.start()
            self.addCleanup(patcher.stop)
        tools.clear_read_files()

    def _ok(self, text):
        return "不存在" not in text and "拒绝访问" not in text and bool(text.strip())

    def test_read_file_accepts_the_key_prefix_that_list_dir_accepts(self):
        """`path: a.gd` —— list_dir 认、grep 认，只有 read_file 不认。

        实测（godot_sample 定位扣血逻辑）：模型写
        `path: behaviors/player.gd start: 1 end: 55`，报「文件不存在：path:
        behaviors/player.gd」——而那个文件明明就在那儿。
        """
        bare = tools.read_file("behaviors/player.gd")
        keyed = tools.read_file("path: behaviors/player.gd")
        self.assertTrue(self._ok(bare))
        self.assertTrue(
            self._ok(keyed),
            "read_file 不接受 `path:` 键前缀，而 list_dir 与 grep 都接受——"
            "模型按后两者的写法调用 read_file 就必然失败：%s" % keyed[:80])

    def test_the_error_message_must_not_echo_the_key_prefix(self):
        """真·文件不存在时，报错里不该留着 `path:` 这种键名。

        「文件不存在：path: xxx」看起来像路径写错，实际上路径是对的、只是多了个键名。
        这种报错会把模型引向换路径/换分隔符，而真实原因是它自己的参数写法。
        """
        out = tools.read_file("path: behaviors/nope.gd")
        self.assertIn("文件不存在", out)
        self.assertNotIn(
            "path:", out.replace("path: behaviors/nope.gd", ""),
            "报错把键名原样回显成路径的一部分，看起来像路径写错了：%r" % out[:90])

    def test_key_prefix_with_range_is_still_accepted(self):
        """本次 trace 里的**确切写法**。"""
        out = tools.read_file("path: behaviors/player.gd start: 1 end: 55")
        self.assertTrue(self._ok(out), out[:90])
        self.assertIn("line55", out, "键名剥掉后区间必须照常生效")

    def test_a_windows_drive_letter_is_not_mistaken_for_a_key(self):
        """`C:\\...` 是盘符，不是 `file:` 键名——绝对路径不能被误剥。"""
        out = tools.read_file("C:\\definitely\\not\\here.gd")
        # 项目外的绝对路径走越界门禁（本项目的正确行为）。关键是整条路径**原样**
        # 出现在报错里——若被误认成 `file:` 键剥成 "C"，这里会变成 "不在代码根目录内：C"。
        self.assertIn("C:\\definitely\\not\\here.gd", out,
                      "盘符路径被键名规则误伤了：%r" % out[:90])

    def test_other_key_spellings_and_case_are_accepted(self):
        """`file:` / `filename:` / 大写 —— 键名规则别只认一种拼法。"""
        for arg in ("file: behaviors/player.gd",
                    "filename: behaviors/player.gd",
                    "FILE: behaviors/player.gd",
                    "Path: behaviors/player.gd"):
            self.assertTrue(self._ok(tools.read_file(arg)),
                            "%r 未被接受：%s" % (arg, tools.read_file(arg)[:70]))


class UnrecognisedRangeSyntaxIsNamedTests(unittest.TestCase):
    """认不出来的写法必须**点名**，否则报错会把语法问题伪装成路径问题。

    ## `start=1` 为什么不再出现在这个类里

    它原来拿 `start=1` 当「认不出的写法」的代表。2026-10 那轮把 `=` 加进
    `_RE_READ_START` 的分隔符之后（连同 `start_line=` / `start-line:`），
    **这一类已经合法可用**——继续断言它必须被点名，就是逼着把功能退回去。

    所以这里换成**仍然**认不出的形状来守同一个意图。守住意图的是
    「`区间参数未识别` 而不是 `文件不存在`」这条，不是某一个具体拼法。

    另有一处同类修正：`=` 也加进了 `_RE_PATH_KEY_LINE` 的分隔符，否则会出现
    「`start=` 认得、`path=` 不认」——**只放宽一半比不放宽更糟**，因为模型会
    以为自己已经写对了。键名放宽只影响 `read_file`（该正则全仓库仅
    `_strip_path_key` 使用，后者只在 `read_file` 里调用）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "behaviors", "player.gd"),
                  "w", encoding="utf-8") as handle:
            handle.write("x\n")
        patcher = patch.object(tools, "_get_code_root", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_equals_syntax_is_accepted_not_flagged(self):
        """`start=1` 现在直接可用——**别把功能退回去换一句更好听的报错**。"""
        out = tools.read_file("behaviors/player.gd start=1")
        self.assertIn("第 1-1 行", out,
                      "`start=1` 又被当成语法错误了：%s" % out[:90])
        self.assertNotIn("区间参数未识别", out)

    def test_equals_works_on_the_key_prefix_too(self):
        """只放宽区间参数、不放宽 `path=` 是不够的。"""
        out = tools.read_file("path=behaviors/player.gd start=1")
        self.assertNotIn("文件不存在", out,
                         "`path=` 没被剥掉——而 `start=` 认得、键名不认，"
                         "模型会以为自己已经写对了：%s" % out[:90])

    def test_line_range_shorthand_is_flagged(self):
        out = tools.read_file("behaviors/player.gd 行1-55")
        self.assertIn("区间参数", out)

    def test_a_non_numeric_range_value_is_flagged(self):
        """`start: abc` 曾落到裸「文件不存在」——语法问题伪装成路径问题。"""
        out = tools.read_file("behaviors/player.gd start: abc")
        self.assertIn("区间参数未识别", out, out[:90])
        self.assertNotIn("文件不存在", out)

    def test_ellipsis_range_is_flagged(self):
        out = tools.read_file("behaviors/player.gd start..5")
        self.assertIn("区间参数未识别", out, out[:90])

    def test_chinese_natural_language_range_is_flagged(self):
        """中文提示下模型会写「从1到5行」——同样不许报成「文件不存在」。"""
        out = tools.read_file("behaviors/player.gd 从1到5行")
        self.assertIn("区间参数未识别", out, out[:90])

    def test_every_unrecognised_shape_is_named_rather_than_called_missing(self):
        """一张表过一遍：每种形状都必须落在 `区间参数未识别`，绝不落进 `文件不存在`。"""
        for tail in ("行1-55", "line 1-55", "lines 1-55", "start: abc",
                     "start..5", "从1到5行", "从1至5行"):
            with self.subTest(tail=tail):
                out = tools.read_file("behaviors/player.gd " + tail)
                self.assertIn("区间参数未识别", out, "%r → %s" % (tail, out[:90]))
                self.assertNotIn("文件不存在", out, "%r 被报成路径问题" % tail)

    def test_a_real_missing_file_is_still_reported_as_missing(self):
        """点名语法问题不能反噬：真的不存在就是「文件不存在」。"""
        out = tools.read_file("behaviors/nope.gd")
        self.assertIn("文件不存在", out)
        self.assertNotIn("区间参数未识别", out)

    def test_paths_that_merely_look_like_syntax_are_not_flagged(self):
        """真路径里带 `start` / `行1-55` / `end=` 的不许被当成参数。

        前导的「行首/空白/逗号」边界就是为这件事准备的：这些 `start`
        前面是 `/`、后面是 `_`，不构成独立词。
        """
        for rel in ("docs/start.md", "start_v2.gd", "behaviors/end.md",
                    "lines.md", "行1-55.md"):
            with self.subTest(rel=rel):
                with tempfile.TemporaryDirectory() as tmp:
                    full = os.path.join(tmp, rel.replace("/", os.sep))
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w", encoding="utf-8") as handle:
                        handle.write("ok\n")
                    with patch.object(tools, "_get_code_root", return_value=tmp):
                        out = tools.read_file("path: " + rel)
                    self.assertNotIn("区间参数未识别", out,
                                     "%r 被误判成参数写法问题：%s" % (rel, out[:90]))
                    self.assertTrue(out.lstrip().startswith("==="),
                                    "%r 应该读得到内容：%s" % (rel, out[:90]))



class _ScriptedLLM:
    """按脚本逐条返回文本。

    ⚠ `count_tokens` 返回 0：让 `_history_window` 不裁剪历史，
    这样断言只针对空响应文案，不被历史长度干扰。
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def count_tokens(self, text):
        return 0

    def chat(self, messages, stream=True, enable_thinking=None, **kw):
        self.calls += 1
        return iter([self.steps[min(self.calls, len(self.steps)) - 1]])


#: 借用真实的失败标记，保证「工具失败」这条分支一定会走到。
#: 不用自造字符串：`_FAILURE_MARKERS` 是权威清单，抄一份就会漂移。
_FAILING_OBS = "错误：文件不存在：behaviors/__missing__.gd"
_FAILING_READ = {"func": lambda arg: _FAILING_OBS}


class EmptyResponseBlamesTheToolFailuresTests(unittest.TestCase):
    """空响应的归因必须把「工具反复失败」摆出来，而不是只让人去查 provider。

    ## 为什么判据是「运行时」而不是「源码里有这句话」

    原断言用 `inspect.getsource(Agent._run)` 搜字符串。实测两个问题：

    ① 把文案退回「中转返回空内容，直接重试通常就能拿到」（注释保留）
       -> 靠钉注释那条侥幸兜住，**而那正是它自己说「会假通过」的情形**
    ② 只改注释不动文案 -> **误红**（钉注释那条）

    **源码里有字符串 ≠ 用户能收到那句话。** godot_sample 实测就是这个形态：
    注释照旧在（记录着正确的推理），文案已经退回误导版本，
    于是用户被引去「直接重试通常就能拿到」，重试多少次都没用。
    源码搜索对这种漂移完全无感。

    所以真跑一次 `Agent._run`，断言**实际 yield 出来的 final 文本**。

    ## ⚠⚠ 真跑时踩到的三个坑（都写在这里，别再踩）

    **① capacity 读的是本机持久化状态。** 第一版真跑 `llm.chat` 被调用
    **0 次**，第一轮就返回「已达到本任务 Token 上限」。链条：

        _capacity_settings() -> capacity.get_settings()
                             -> config.get_runtime / load_state
        本机存着 task_token_budget=5000（界面存过的真实配置）
        -> RunBudget(5000).preflight(19807 字符的 prompt) -> False

    `normalize_settings({})` 是 0（不限），但 `get_settings()` 读到的是
    **用户机器上存过的值** —— 换台机器、或者用户改过设置，行为就不同。
    **测试不封闭。** 所以显式 patch capacity。

    **② 「读一个不存在的文件」不算工具失败。** 实测 `read_file` 在没配
    代码库根目录时返回「尚未配置代码库根目录，请先用 /api/ingest_code…」，
    而 `_is_failure` 判它 **False**（那不在 `_FAILURE_MARKERS` 里），
    `fail_total` 不增 -> 走的是「**没有**工具失败记录」那条分支。

    **③ 所以自己造一个必然失败的工具。** 与其依赖本机有没有配代码库、
    路径存不存在，不如把 `TOOLS["read_file"]` 换成一个必定返回失败标记的
    函数 —— 前提由测试自己钉住。

    ## 变异实测（四条全符合预期）

    ① 文案退回「直接重试通常就能拿到」 -> 红（防住假通过）
    ② 只改注释不动文案              -> 绿（不误红，旧版在这里误红）
    ③ 删掉「大概率无效」            -> 红
    ④ runtime 断言换回 getsource     -> 红（反向守卫生效）
    """

    #: 判文案的用例 —— 反向守卫只盯这两条
    JUDGING_METHODS = ("test_the_message_names_the_failing_tool",
                       "test_it_does_not_assert_the_provider_is_the_cause")

    def _final_text(self):
        llm = _ScriptedLLM([
            # 先调一次必然失败的工具 —— fail_total > 0
            "Thought: 核对\nAction: read_file\nAction Input: path: behaviors/x.gd",
            # 之后一直空响应：_run 的纠偏次数耗尽后走 `elif not acc.strip():`
            "", "", "", "", "", "", "", "", "", "",
        ])
        unlimited = {k: 0 for k in capacity.DEFAULTS if k != "local_auto_limit"}
        unlimited["local_auto_limit"] = True
        with patch.object(capacity, "get_settings",
                          return_value=dict(unlimited)), \
             patch.dict(agent_mod.TOOLS, {"read_file": _FAILING_READ}):
            a = agent_mod.Agent(llm=llm, session_id="t-empty-blame")
            events = list(a.run("读一个不存在的文件", stream=True))

        # 前提自检：工具真被调过、观察真被判成失败。
        # 少了这两条，断言红了也不知道是文案变了还是前提没成立。
        observations = [str(e.get("text") or "")
                        for e in events if e.get("type") == "observation"]
        self.assertTrue(observations, "工具没被调用 —— 假 LLM 脚本不对")
        self.assertTrue(any(agent_mod._is_failure(o) for o in observations),
                        "工具观察没被判成失败，验的是别的分支：%r" % (observations[:1],))
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals, "没有产出 final")
        return finals[-1].get("text") or ""

    def test_the_message_names_the_failing_tool(self):
        text = self._final_text()
        self.assertIn("次工具调用失败", text,
                      "工具连续失败时，空响应文案必须点出它——"
                      "只让用户查 provider 会把人引向错误方向")
        self.assertIn("最常失败", text,
                      "必须点名最常失败的工具，否则用户还得自己翻 trace")

    def test_it_does_not_assert_the_provider_is_the_cause(self):
        """原文案说「这多半不是你的问题写得不清楚」——在没有证据时过于笃定。

        ⚠ 判据是**用户实际收到的 final 文本**，不是源码里有这个字符串。
        钉源码会两头出错：钉注释 → 纯注释改写误红；只钉文案 → 那正是我们要的。
        """
        text = self._final_text()
        self.assertIn("单纯重试**大概率无效**", text,
                      "工具失败分支的文案必须真的劝住重试，"
                      "否则归因照样把人引向 provider（实测 godot_sample "
                      "就是被「直接重试通常就能拿到」误导，重试多少次都没用）")
        self.assertNotIn("重试通常就能拿到", text,
                         "退回了那条把用户引向 provider 的旧文案")

    def test_the_judgement_runs_the_agent_not_the_source(self):
        """反向守卫：**判文案那两条**不许退回「搜源码里有这句话」。

        防的是**下一次有人把判据改弱**：把运行时断言换回字符串搜索。

        ⚠ 范围只限 `JUDGING_METHODS`（判文案的两条）。
        扫全类会自己判红 —— 本类的守卫用例合法地用 `getsource`
        去检查源码，那正是它要禁止的形状出现在正确的地方。

        ⚠ 只看**代码**：用 tokenize 剥掉注释与字符串字面量，
        否则说明文字里提到 `getsource` 就会误判
        （先用正则剥除反而留下残渣 —— 实测踩过）。
        """
        import inspect
        import io
        import tokenize
        code_only = []
        for name in self.JUDGING_METHODS:
            body = inspect.getsource(getattr(self, name))
            for tok in tokenize.generate_tokens(io.StringIO(body).readline):
                if tok.type in (tokenize.STRING, tokenize.COMMENT):
                    continue
                code_only.append(tok.string)
        joined = " ".join(code_only)
        self.assertIn("_final_text", joined,
                      "判据必须真跑 Agent._run 并检查用户收到的文本")
        self.assertNotIn("getsource", joined,
                         "判据退回了「源码里有这句话」——那是注释与文案"
                         "可以各说各话的地方，godot_sample 实测就是这么漏过去的")

    def test_the_capacity_premise_is_pinned(self):
        """反向守卫：capacity 必须被显式钉住，不能读本机持久化状态。

        去掉 patch 之后，本机存着 task_token_budget=5000 时
        `llm.chat` 会被调用 0 次 —— 第一轮就判「Token 上限」返回。
        那不是代码 bug，是**测试依赖了运行者的机器状态**。
        """
        import inspect as _i
        src = _i.getsource(self._final_text)
        self.assertIn("capacity", src,
                      "_final_text 必须钉住 capacity，否则换个环境行为就变")
        self.assertNotIn("unittest.skip", src, "不许靠 skip 掩盖")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
