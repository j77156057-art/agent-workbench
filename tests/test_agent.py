# -*- coding: utf-8 -*-
"""Agent ReAct 循环护栏单测：空参拦截、步数耗尽强制收尾、证据兜底、python_exec cwd。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod
import tools as tools_mod
from config import set_runtime


class _ScriptedLLM:
    """按调用顺序返回预设 ReAct 文本；stream 形态返回 list（finish_reason=None）。"""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = 0

    def chat(self, messages, stream=True, **kwargs):
        i = min(self.calls, len(self._scripts) - 1)
        self.calls += 1
        return [self._scripts[i]]

    def count_tokens(self, text):
        return 0


def _act(tool, inp):
    return f"Thought: t\nAction: {tool}\nAction Input: {inp}"


_FINAL_OK = "Thought: t\nFinal Answer: 已确认结论。"


def _run(agent):
    events = []
    for ev in agent.run("q", stream=True):
        events.append(ev)
    return events


class AgentGuardTests(unittest.TestCase):
    def setUp(self):
        self._old_tools = agent_mod.TOOLS

    def tearDown(self):
        agent_mod.TOOLS = self._old_tools
        set_runtime("code_root", "")

    def test_repeated_sentence_detector_ignores_normal_long_text(self):
        repeated = "请根据已经读取的文件继续核对血条归零后的处理逻辑。\n" * 3
        self.assertTrue(agent_mod._repeated_sentence(repeated))
        self.assertEqual(agent_mod._repeated_sentence("这是很长的正常说明。\n" * 2), "")

    def test_empty_arg_is_blocked_without_tool_step(self):
        """空 Action Input：不执行工具、不计步数，回填后模型可正常收尾。"""
        called = []

        def fake_search(arg):
            called.append(arg)
            return "不应被调用"

        agent_mod.TOOLS = {"search_code": {"func": fake_search}}
        a = agent_mod.Agent(llm=_ScriptedLLM([
            _act("search_code", ""),   # 空参，应被拦截
            _FINAL_OK,
        ]))
        events = _run(a)
        self.assertEqual(called, [])  # 工具函数从未执行
        self.assertTrue(any("缺少参数" in e.get("text", "") for e in events))
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("已确认结论", finals[0])

    def test_capability_question_does_not_authorize_file_write(self):
        """询问能力时先澄清目标，不调用任何写工具或模型脚本。"""
        called = []

        def fake_write(arg):
            called.append(arg)
            return "不应执行"

        agent_mod.TOOLS = {"apply_edit": {"func": fake_write}}
        a = agent_mod.Agent(llm=_ScriptedLLM([_act("apply_edit", "path: x")]))
        events = list(a.run("你可以帮我修改文件吗？", stream=True))
        self.assertEqual(called, [])
        self.assertEqual(len([e for e in events if e["type"] == "final"]), 1)
        self.assertIn("当前还没有执行任何写操作", events[-1]["text"])

    def test_project_bug_audit_does_not_start_from_historical_bug_records(self):
        """当前项目缺陷审查必须先取得代码/运行证据，再允许读取历史 bugs。"""
        calls = []

        def list_bugs(_arg):
            calls.append("dev_list_bugs")
            return '{"bugs":[{"title":"历史记录"}]}'

        def search(_arg):
            calls.append("search_code")
            return "[scripts/player.gd:L4] suspicious damage handling"

        agent_mod.TOOLS = {
            "dev_list_bugs": {"func": list_bugs},
            "search_code": {"func": search},
        }
        a = agent_mod.Agent(llm=_ScriptedLLM([
            _act("dev_list_bugs", ""),
            _act("search_code", "damage"),
            "Final Answer: 已检查当前项目代码。",
        ]))
        events = list(a.run("你看看目前的游戏有什么bug吗", stream=True))
        self.assertEqual(calls, ["search_code"])
        self.assertTrue(any("只返回历史归档" in e.get("text", "") for e in events
                            if e["type"] == "observation"))
        self.assertIn("已检查当前项目代码", events[-1]["text"])

    def test_location_answer_is_asked_to_read_source_before_final(self):
        """search_code 摘要不足以交付定位答案，必须补一次 read_file。"""
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "behaviors", "enemy.gd")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("func _ready():\n\tadd_to_group(\"enemy\")\n")
            set_runtime("code_root", root)
            calls = []

            def search(_arg):
                calls.append("search_code")
                return "[behaviors/enemy.gd:L2] add_to_group(\\\"enemy\\\")"

            def read(_arg):
                calls.append("read_file")
                return "=== behaviors/enemy.gd（第 1-2 行，共 2 行）===\n1: func _ready()\n2: add_to_group(\\\"enemy\\\")"

            agent_mod.TOOLS = {"search_code": {"func": search}, "read_file": {"func": read}}
            a = agent_mod.Agent(llm=_ScriptedLLM([
                _act("search_code", "enemy patrol"),
                "Final Answer: 敌人分组在 behaviors/enemy.gd:2。",
                "Thought: 读取原文\nAction: read_file\nAction Input: behaviors/enemy.gd",
                "Final Answer: 敌人分组在 behaviors/enemy.gd:2。",
            ]))
            events = list(a.run("敌人的巡逻逻辑在哪个文件？", stream=True))
            self.assertEqual(calls, ["search_code", "read_file"])
            self.assertTrue(any("缺少原文核验" in e.get("text", "") for e in events
                                if e["type"] == "reflection"))
            self.assertIn("behaviors/enemy.gd:2", events[-1]["text"])

    def test_request_context_stays_system_message(self):
        captured = []

        class CaptureLLM(_ScriptedLLM):
            def chat(self, messages, stream=True, **kwargs):
                captured.extend(messages)
                return super().chat(messages, stream=stream, **kwargs)

        a = agent_mod.Agent(llm=CaptureLLM([_FINAL_OK]))
        list(a.run("请回答 hello", stream=True,
                  system_context=["内部路由提示：仅供模型使用"], ingested_sources=["LICENSE.txt"]))
        user_messages = [item["content"] for item in captured if item.get("role") == "user"]
        self.assertEqual(user_messages[-1], "请回答 hello")
        self.assertIn("内部路由提示", "\n".join(item["content"] for item in captured
                                                  if item.get("role") == "system"))

    def test_streaming_prompt_leak_is_removed_even_when_split_across_tokens(self):
        """模型分片回显旧版系统前缀时，SSE token 与最终答案都不得泄漏。"""
        class ChunkedLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, stream=True, **kwargs):
                self.calls += 1
                return iter([
                    "【系统",
                    "提示】知识库中已上传以下文档：LICENSE。\n\n用户问",
                    "题：Final Answer: 真实答案。",
                ])

            def count_tokens(self, text):
                return 0

        events = list(agent_mod.Agent(llm=ChunkedLLM()).run("请回答这个测试问题。", stream=True))
        visible = "".join(e.get("text", "") for e in events if e.get("type") == "token")
        finals = [e.get("text", "") for e in events if e.get("type") == "final"]
        self.assertNotIn("系统提示", visible)
        self.assertNotIn("知识库中已上传", visible)
        self.assertEqual(finals, ["真实答案。"])

    def test_step_budget_forces_final_from_observations(self):
        """8 步后第 9 个动作先被强制收尾提示，模型据此给出 Final Answer。"""
        counter = {"n": 0}

        def fake_tool(arg):
            counter["n"] += 1
            return f"ok-{arg}"

        agent_mod.TOOLS = {"fake_tool": {"func": fake_tool}}
        scripts = [_act("fake_tool", f"inp{i}") for i in range(1, 9)]
        scripts.append(_act("fake_tool", "inp9"))  # 触发强制收尾 nudge
        scripts.append(_FINAL_OK)                 # 模型基于观察收尾
        with patch.object(agent_mod, "MAX_AGENT_STEPS", 8):
            a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
            events = _run(a)
        self.assertEqual(counter["n"], 8)          # 第 9 次未执行
        self.assertTrue(any("收尾" in e.get("text", "")
                            for e in events if e["type"] == "reflection"))
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(finals, ["已确认结论。"])

    def test_step_budget_fallback_carries_evidence(self):
        """强制收尾后模型仍要调工具：确定性兜底，含已执行清单与最后观察，不编造结论。"""
        def fake_tool(arg):
            return f"obs-{arg}"

        agent_mod.TOOLS = {"fake_tool": {"func": fake_tool}}
        scripts = [_act("fake_tool", f"inp{i}") for i in range(1, 9)]
        scripts.append(_act("fake_tool", "inp9"))   # 强制 nudge
        scripts.append(_act("fake_tool", "inp10"))  # 仍要工具 -> 证据兜底
        with patch.object(agent_mod, "MAX_AGENT_STEPS", 8):
            a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
            events = _run(a)
        finals = [e for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        text = finals[0]["text"]
        self.assertIn("最大工具调用步数", text)
        self.assertIn("fake_tool(inp1)", text)
        self.assertIn("fake_tool(inp8)", text)
        self.assertIn("obs-inp8", text)           # 最后真实观察
        self.assertNotIn("已确认结论", text)

    def test_project_audit_uses_dynamic_step_budget_in_run_loop(self):
        """项目缺陷审查的增量预算必须接入执行循环，而不只是停留在配置函数。"""
        called = []

        def fake_tool(arg):
            called.append(arg)
            return f"obs-{arg}"

        agent_mod.TOOLS = {"fake_tool": {"func": fake_tool}}
        scripts = [_act("fake_tool", f"audit{i}") for i in range(1, 5)]
        scripts.append(_FINAL_OK)
        with patch.object(agent_mod, "MAX_AGENT_STEPS", 2), \
                patch.object(agent_mod, "AUDIT_MAX_AGENT_STEPS", 4):
            a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
            events = list(a.run("你看看目前的游戏代码有什么 bug", stream=True))

        self.assertEqual(called, ["audit1", "audit2", "audit3", "audit4"])
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["已确认结论。"])

    def test_code_review_default_budget_allows_more_than_old_twelve_steps(self):
        called = []
        agent_mod.TOOLS = {"fake_tool": {"func": lambda arg: called.append(arg) or f"obs-{arg}"}}
        scripts = [_act("fake_tool", f"file{i}") for i in range(20)] + [_FINAL_OK]
        events = list(agent_mod.Agent(llm=_ScriptedLLM(scripts)).run("帮我审查代码", stream=True))
        self.assertEqual(len(called), 20)
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["已确认结论。"])

    def test_dynamic_budget_does_not_expand_ordinary_questions(self):
        """动态预算只匹配代码/项目审查，不因普通问题里出现“问题”二字就放宽。"""
        with patch.object(agent_mod, "MAX_AGENT_STEPS", 2), \
                patch.object(agent_mod, "CODE_MAX_AGENT_STEPS", 4), \
                patch.object(agent_mod, "AUDIT_MAX_AGENT_STEPS", 6):
            self.assertEqual(agent_mod._step_budget("这个问题是什么意思？"), 2)
            self.assertEqual(agent_mod._step_budget("请审查这个 Python 脚本"), 4)
            self.assertEqual(agent_mod._step_budget("目前这个游戏有哪些 bug？"), 6)

    def test_python_exec_runs_within_code_root(self):
        """配置 code_root 后，python_exec 的相对路径按代码根解析。"""
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            marker = os.path.join(d, "marker_t5.txt")
            with open(marker, "w", encoding="utf-8") as f:
                f.write("x")
            set_runtime("code_root", d)
            out = tools_mod.python_exec(
                "import os; print(os.path.exists('marker_t5.txt'))"
            )
            self.assertIn("True", out)

    def test_repeated_empty_args_terminate_via_repeat_guard(self):
        """连续空参：第 1 次回填，第 2/3 次重复升级，第 4 次强制收尾，仍不收尾则证据兜底。"""
        called = []

        def fake_search(arg):
            called.append(arg)
            return "不应被调用"

        agent_mod.TOOLS = {"search_code": {"func": fake_search}}
        scripts = [_act("search_code", "") for _ in range(6)]
        a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
        events = _run(a)
        self.assertEqual(called, [])  # 工具函数始终未执行
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("反复", finals[0])
        self.assertIn("已执行", finals[0])      # 证据兜底形态，而非裸停止语
        blocked = [e for e in events if e["type"] == "action" and "已拦截" in e["text"]]
        self.assertEqual(len(blocked), 1)       # 只有第 1 次发空参回填，后续被重复护栏截停

    def test_repeat_budget_forces_final_from_observations(self):
        """同参第 4 次（repeats=3）先强制收尾，模型可据此直接给出 Final Answer。"""
        counter = {"n": 0}

        def fake_tool(arg):
            counter["n"] += 1
            return f"obs-{arg}"

        agent_mod.TOOLS = {"fake_tool": {"func": fake_tool}}
        scripts = [_act("fake_tool", "inp1") for _ in range(4)]
        scripts.append(_FINAL_OK)  # 强制收尾后模型基于观察给出答案
        a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
        events = _run(a)
        self.assertEqual(counter["n"], 1)       # 工具只真正执行 1 次
        self.assertTrue(any("重复空转" in e.get("text", "")
                            for e in events if e["type"] == "reflection"))
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(finals, ["已确认结论。"])

    def test_forced_final_blocks_different_args_too(self):
        """强制收尾后模型换一个【不同参数】的工具调用：同样拦截并证据兜底。"""
        counter = {"n": 0}

        def fake_tool(arg):
            counter["n"] += 1
            return f"obs-{arg}"

        agent_mod.TOOLS = {"fake_tool": {"func": fake_tool}}
        scripts = [_act("fake_tool", "inp1") for _ in range(4)]
        scripts.append(_act("fake_tool", "other"))  # 换新参数也不许再探索
        a = agent_mod.Agent(llm=_ScriptedLLM(scripts))
        events = _run(a)
        self.assertEqual(counter["n"], 1)
        finals = [e for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        text = finals[0]["text"]
        self.assertIn("反复", text)
        self.assertIn("fake_tool(inp1)", text)
        self.assertIn("obs-inp1", text)
        self.assertNotIn("fake_tool(other)", text)  # 换参调用未执行、不入证据
        self.assertNotIn("已确认结论", text)

    def test_empty_knowledge_call_redirects_to_code_tools(self):
        """已配置代码库时，空参 search_knowledge/web_search 被强制改道代码工具。"""
        with tempfile.TemporaryDirectory() as d:
            set_runtime("code_root", d)
            grounded = "【系统提示】知识库中已上传以下文档：LICENSE。\n\n用户问题：单曲循环在哪实现？LOOP_ONE"
            msg = agent_mod._empty_arg_obs("web_search", grounded)
            self.assertIn("search_code", msg)
            self.assertIn("禁止再调用", msg)
            self.assertIn("LOOP_ONE", msg)              # 真实用户问题被提取出来
            self.assertNotIn("LICENSE", msg)            # 系统前缀不进回填文案

    def test_python_exec_unescapes_literal_newlines(self):
        """模型把多行写成单行（字面量 \\n）时，SyntaxError 后反转义一次再执行。"""
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            out = tools_mod.python_exec(r'print("a")\nprint("b")')
        self.assertIn("a\nb", out)
        self.assertNotIn("SyntaxError", out)

    def test_read_file_supports_line_range(self):
        """read_file 的 start:/end: 读取指定行段，带行号与总行数。"""
        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "ranged.txt")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(f"line{i}" for i in range(1, 301)))
            set_runtime("code_root", d)
            out = tools_mod.read_file("ranged.txt\nstart: 10\nend: 12")
            self.assertIn("第 10-12 行，共 300 行", out)
            self.assertIn("10: line10", out)
            self.assertIn("12: line12", out)
            self.assertNotIn("line9", out)


    def test_parse_inline_action_arg(self):
        """弱模型把参数写在 Action 同行括号里时，也能解析出工具名与参数。"""
        p = agent_mod.parse_response('Thought: t\nAction: search_code("Lyric")')
        self.assertEqual(p["action"], "search_code")
        self.assertEqual(p["action_input"], "Lyric")
        p2 = agent_mod.parse_response("Thought: t\nAction: grep(onCompletion)")
        self.assertEqual(p2["action_input"], "onCompletion")

    def test_normalize_keyword_style_args(self):
        """query:/pattern: 等关键字参数风格被还原为纯值，read_file 逗号行号被拆行。"""
        self.assertEqual(
            agent_mod._normalize_tool_arg("search_code", 'query: "enum Language"'),
            "enum Language",
        )
        self.assertEqual(
            agent_mod._normalize_tool_arg("grep", 'pattern: "onCompletion"'),
            "onCompletion",
        )
        norm = agent_mod._normalize_tool_arg(
            "read_file", 'path: "a/b.java", start: 315, end: 360'
        )
        self.assertEqual(norm, "a/b.java\nstart: 315\nend: 360")
        self.assertEqual(
            agent_mod._normalize_tool_arg("read_file", 'path="ui/hud.gd"'),
            "ui/hud.gd",
        )
        # 非关键字风格保持原样
        self.assertEqual(agent_mod._normalize_tool_arg("grep", "LOOP_ONE"), "LOOP_ONE")
        self.assertEqual(agent_mod._normalize_tool_arg("list_dir", "(顶层)"), ".")

    def test_run_passes_normalized_arg_to_tool(self):
        """端到端：run() 派发给工具函数/事件展示的必须是归一化后的参数（接线回归）。"""
        received = []

        def fake_read_file(arg):
            received.append(arg)
            return "ok"

        agent_mod.TOOLS = {"read_file": {"func": fake_read_file}}
        a = agent_mod.Agent(llm=_ScriptedLLM([
            'Thought: t\nAction: read_file(path: "a/b.java", start: 10, end: 30)',
            _FINAL_OK,
        ]))
        events = _run(a)
        self.assertEqual(received, ["a/b.java\nstart: 10\nend: 30"])
        shown = [e["text"] for e in events if e["type"] == "action"]
        self.assertEqual(shown, ["read_file(a/b.java\nstart: 10\nend: 30)"])

    def test_normalize_grep_path_scope(self):
        """grep 的同行/多行 path: 限定归一化为 <正则>\\npath: <范围>，纯正则保持不变。"""
        self.assertEqual(
            agent_mod._normalize_tool_arg(
                "grep", 'pattern: "language|en", path: "app/A.java"'
            ),
            "language|en\npath: app/A.java",
        )
        self.assertEqual(
            agent_mod._normalize_tool_arg("grep", 'pattern: "LOOP_ONE"\npath: "app/x.java"'),
            "LOOP_ONE\npath: app/x.java",
        )
        self.assertEqual(
            agent_mod._normalize_tool_arg("grep", "onCompletion\npath: app/M.java"),
            "onCompletion\npath: app/M.java",
        )
        self.assertEqual(agent_mod._normalize_tool_arg("grep", "onCompletion"), "onCompletion")

    def test_grep_scopes_to_file_or_dir(self):
        """grep 的 path: 限定只搜指定文件/目录；路径不存在与越界都被拦截。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            with open(os.path.join(d, "sub", "A.java"), "w", encoding="utf-8") as f:
                f.write("class A { int MARKER_A = 1; }\n")
            with open(os.path.join(d, "sub", "B.java"), "w", encoding="utf-8") as f:
                f.write("class B { int MARKER_B = 1; }\n")
            with open(os.path.join(d, "C.java"), "w", encoding="utf-8") as f:
                f.write("class C { int MARKER_C = 1; }\n")
            set_runtime("code_root", d)
            # 文件作用域：规范两行形式
            out = tools_mod.grep("MARKER_\npath: sub/A.java")
            self.assertIn("sub\\A.java:1", out.replace("/", "\\"))
            self.assertNotIn("MARKER_B", out)
            # 目录作用域：sub 下 A/B 命中，根目录 C 不命中
            out_dir = tools_mod.grep("MARKER_, path: sub")
            self.assertIn("MARKER_A", out_dir)
            self.assertIn("MARKER_B", out_dir)
            self.assertNotIn("MARKER_C", out_dir)
            # 路径不存在
            self.assertIn("路径不存在", tools_mod.grep("MARKER_\npath: sub/missing.java"))
            # 越界
            self.assertIn("拒绝访问", tools_mod.grep("MARKER_\npath: ../../escape.java"))

    def test_parse_explicit_action_input_still_wins(self):
        """显式 Action Input 行优先，行内括号不作为退路。"""
        text = 'Thought: t\nAction: search_code("ignored")\nAction Input: ApiProvider'
        p = agent_mod.parse_response(text)
        self.assertEqual(p["action"], "search_code")
        self.assertEqual(p["action_input"], "ApiProvider")


class WebGateTests(unittest.TestCase):
    """联网开关：默认关闭时 web_* 工具文本通道拒绝执行、原生 schema 剔除。"""

    def setUp(self):
        self._old_tools = agent_mod.TOOLS

    def tearDown(self):
        agent_mod.TOOLS = self._old_tools

    def test_web_tool_blocked_in_text_channel_when_off(self):
        calls = []

        def fake_web(arg):
            calls.append(arg)
            return "不应被调用"

        def fake_local(arg):
            return "本地结果"

        agent_mod.TOOLS = {
            "web_search": {"func": fake_web},
            "search_code": {"func": fake_local},
        }
        a = agent_mod.Agent(llm=_ScriptedLLM([
            _act("web_search", "今天的新闻"),
            _FINAL_OK,
        ]))
        self.assertFalse(a.web_enabled, "新 Agent 的联网必须默认关闭")
        events = _run(a)
        self.assertEqual(calls, [], "联网关闭时 web_search 绝不能执行")
        obs = [e["text"] for e in events if e["type"] == "observation"]
        self.assertTrue(any("联网未开启" in t for t in obs))

    def test_web_tool_runs_when_enabled(self):
        calls = []

        def fake_web(arg):
            calls.append(arg)
            return "· 标题\n  https://example.test/a"

        agent_mod.TOOLS = {"web_search": {"func": fake_web}}
        a = agent_mod.Agent(llm=_ScriptedLLM([
            _act("web_search", "今天的新闻"),
            _FINAL_OK,
        ]))
        a.web_enabled = True
        _run(a)
        self.assertEqual(calls, ["今天的新闻"])

    def test_changed_web_query_is_allowed_after_unsatisfactory_result(self):
        """改写查询是合法重试；只有同工具同参数才触发重复调用护栏。"""
        calls = []

        def fake_web(arg):
            calls.append(arg)
            if len(calls) == 1:
                return "搜索未返回结果"
            return "· 相关结果\n  https://example.test/useful"

        agent_mod.TOOLS = {"web_search": {"func": fake_web}}
        a = agent_mod.Agent(llm=_ScriptedLLM([
            _act("web_search", "原始问题"),
            _act("web_search", "原始问题 site:example.test"),
            _FINAL_OK,
        ]))
        a.web_enabled = True
        events = _run(a)
        self.assertEqual(calls, ["原始问题", "原始问题 site:example.test"])
        self.assertFalse(any("重复调用" in e.get("text", "") for e in events
                             if e["type"] == "reflection"))
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["已确认结论。"])

    def test_offline_repeat_same_web_call_stops_early(self):
        """联网关闭时同参重复 web_search：既被拦截，又能触发防重复升级及时收尾。

        回归缺陷：两个早退分支（联网拦截 / 子代理白名单拒绝）以前只 yield 观察就
        continue，未登记已执行签名，导致 `sig in executed` 恒为假、repeats 永不增长，
        弱模型同参空转要耗尽 15 次推理才报「已达到最大推理步数」。
        """
        calls = []

        def fake_web(arg):
            calls.append(arg)
            return "不应被调用"

        agent_mod.TOOLS = {"web_search": {"func": fake_web}}
        # 单条脚本 → _ScriptedLLM 用最后一条兜底反复返回同一「同参 web_search」。
        a = agent_mod.Agent(llm=_ScriptedLLM([_act("web_search", "x")]))
        self.assertFalse(a.web_enabled, "新 Agent 的联网默认关闭")
        events = _run(a)
        self.assertEqual(calls, [], "联网关闭时 web_search 绝不能执行")
        actions = [e for e in events if e["type"] == "action"]
        self.assertLessEqual(len(actions), 4, "同参空转应被及时截停，而非刷满最大步数")
        self.assertTrue(any("联网未开启" in e.get("text", "")
                            for e in events if e["type"] == "observation"))
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1, "事件流最终必须产出 final")

    def test_effective_tool_schemas_filter_web_tools(self):
        # 名单从运行期的判定依据（spec.group == "web"）现算，不再取自
        # agent._WEB_TOOLS —— 那是一张运行期不读的死副本，用它遍历等于
        # 校验一份和真实行为无关的清单，绿灯是假的。
        web_tools = sorted(
            name for name, value in tools_mod.TOOLS.items()
            if agent_mod.coerce_tool_spec(name, value).group == "web")
        self.assertTrue(web_tools, "运行期应至少识别出若干 web 组工具")

        a = agent_mod.Agent(llm=_ScriptedLLM([_FINAL_OK]))
        names_off = set(a._effective_tool_names())
        for name in web_tools:
            self.assertNotIn(name, names_off, f"联网关闭时 {name} 不应暴露给模型")
        self.assertIn("search_code", names_off)

        a.web_enabled = True
        names_on = set(a._effective_tool_names())
        for name in web_tools:
            self.assertIn(name, names_on, f"联网开启后 {name} 应出现在 schema 中")

    def test_system_prompt_declares_web_status(self):
        a = agent_mod.Agent(llm=_ScriptedLLM([_FINAL_OK]))
        joined_off = " ".join(str(m.get("content", "")) for m in a._build_messages("q"))
        self.assertIn("联网已关闭", joined_off)
        a.web_enabled = True
        joined_on = " ".join(str(m.get("content", "")) for m in a._build_messages("q"))
        self.assertIn("联网已开启", joined_on)


class SubagentPlanningRoleTests(unittest.TestCase):
    def test_dispatcher_role_is_read_only_and_available(self):
        spec = agent_mod._SUBAGENT_ROLES["dispatcher"]
        self.assertIn("search_code", spec["tools"])
        self.assertIn("read_file", spec["tools"])
        self.assertNotIn("apply_edit", spec["tools"])
        self.assertIn("任务派发", spec["hint"])

    def test_planner_role_is_available_and_read_only(self):
        spec = agent_mod._SUBAGENT_ROLES["planner"]
        self.assertIn("search_code", spec["tools"])
        self.assertIn("read_file", spec["tools"])
        self.assertNotIn("apply_edit", spec["tools"])
        self.assertIn("任务拆解", spec["hint"])

    def test_planner_cannot_widen_tools_or_enable_mcp(self):
        class CaptureLLM(_ScriptedLLM):
            def __init__(self):
                super().__init__([_FINAL_OK])
                self.messages = []

            def clone(self):
                return self

            def chat(self, messages, stream=True, **kwargs):
                self.messages.append(messages)
                return super().chat(messages, stream=stream, **kwargs)

        llm = CaptureLLM()
        registry = {
            "search_code": {"func": lambda _arg: "代码证据"},
            "apply_edit": {"func": lambda _arg: "不应调用"},
            "dev_mcp_call": {"func": lambda _arg: "不应调用"},
        }
        parent = agent_mod.Agent(llm=llm, tool_registry=registry)
        result = parent._run_child(
            "planner", "拆解文件和依赖", tool_allowlist=["apply_edit", "dev_mcp_call"],
            mcp_policy="allow", reflect=False)
        self.assertEqual(result["status"], "ok")
        prompt = "\n".join(str(item.get("content") or "") for item in llm.messages[-1]
                             if isinstance(item, dict))
        self.assertIn("MCP=deny", prompt)
        effective = prompt.rsplit("允许工具=", 1)[-1].split("\n", 1)[0]
        self.assertIn("search_code", effective)
        self.assertNotIn("apply_edit", effective)
        self.assertNotIn("dev_mcp_call", effective)

    def test_dispatcher_cannot_enable_mcp(self):
        class CaptureLLM(_ScriptedLLM):
            def __init__(self):
                super().__init__([_FINAL_OK])
                self.messages = []

            def clone(self):
                return self

            def chat(self, messages, stream=True, **kwargs):
                self.messages.append(messages)
                return super().chat(messages, stream=stream, **kwargs)

        llm = CaptureLLM()
        registry = {"search_code": {"func": lambda _arg: "代码证据"},
                    "dev_mcp_call": {"func": lambda _arg: "不应调用"}}
        parent = agent_mod.Agent(llm=llm, tool_registry=registry)
        result = parent._run_child("dispatcher", "拆解文件并分派任务",
                                   tool_allowlist=["dev_mcp_call"],
                                   mcp_policy="allow", reflect=False)
        self.assertEqual(result["status"], "ok")
        prompt = "\n".join(str(item.get("content") or "")
                             for item in llm.messages[-1] if isinstance(item, dict))
        self.assertIn("MCP=deny", prompt)
        self.assertNotIn("dev_mcp_call", prompt.rsplit("允许工具=", 1)[-1].split("\n", 1)[0])


class CalculateToolTests(unittest.TestCase):
    """calculate：AST 白名单求值 + 比较运算支持（回归「9.9 和 9.11 哪个大」）。"""

    def test_arithmetic(self):
        self.assertEqual(tools_mod.calculate("23*45+12"), "1047")
        self.assertEqual(tools_mod.calculate("(123+456)*2"), "1158")
        self.assertEqual(tools_mod.calculate("2**10"), "1024")
        self.assertEqual(tools_mod.calculate("7//2"), "3")
        self.assertEqual(tools_mod.calculate("7%3"), "1")
        self.assertTrue(tools_mod.calculate("9.11-9.9").startswith("-0.79"))

    def test_comparison_returns_chinese_bool(self):
        self.assertEqual(tools_mod.calculate("9.9 > 9.11"), "成立（True）")
        self.assertEqual(tools_mod.calculate("9.11 > 9.9"), "不成立（False）")
        self.assertEqual(tools_mod.calculate("9.11 != 9.9"), "成立（True）")
        self.assertEqual(tools_mod.calculate("(2+3) >= 5"), "成立（True）")

    def test_rejects_non_arithmetic_nodes(self):
        bad = tools_mod._CALC_BAD
        # 名称/调用/属性/布尔运算是代码注入的入口，必须一律拒绝
        self.assertEqual(tools_mod.calculate("__import__('os')"), bad)
        self.assertEqual(tools_mod.calculate("open('x')"), bad)
        self.assertEqual(tools_mod.calculate("1 or 2"), bad)
        self.assertEqual(tools_mod.calculate("().__class__"), bad)
        self.assertIn("计算失败", tools_mod.calculate("1/0"))
        self.assertEqual(tools_mod.calculate(""), "未提供表达式。")


class CalculateInterpretTests(unittest.TestCase):
    """calculate 成功后回填观察、让模型把数字解读成自然语言结论，而不是直接抛数字。"""

    def test_result_fed_back_and_model_conclusion_used(self):
        llm = _ScriptedLLM([
            _act("calculate", "9.11 - 9.9"),
            "Thought: t\nFinal Answer: 9.9 更大，因为 9.11 减 9.9 得到负数。",
        ])
        a = agent_mod.Agent(llm=llm)
        events = _run(a)
        self.assertEqual(llm.calls, 2, "成功后应再给模型一轮解读，而不是立即收尾")
        obs = [e["text"] for e in events if e["type"] == "observation"]
        self.assertTrue(any(t.startswith("-0.79") for t in obs), obs)
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("9.9 更大", finals[0])
        self.assertFalse(finals[0].startswith("计算结果为："))

    def test_native_mode_also_interprets_calculate(self):
        llm = _ScriptedLLM([
            "Action: calculate\nAction Input: 9.9 > 9.11",
            "Thought: t\nFinal Answer: 成立，所以 9.9 更大。",
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = _run(a)
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertIn("9.9 更大", finals[-1])

    def test_fallback_emits_value_when_model_never_concludes(self):
        # 模型拿到 2 之后仍反复同一调用：强制收尾耗尽 -> 计算结果确定性兜底
        llm = _ScriptedLLM([_act("calculate", "1+1")])
        a = agent_mod.Agent(llm=llm)
        events = _run(a)
        finals = [e["text"] for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0], "计算结果为：2")


class DynamicOrchestrationContractTests(unittest.TestCase):
    """真实 Agent 编排路径的离线契约：反思失败后由 LLM 给出补救任务。"""

    class _PlannerLLM:
        provider = "fake"
        model = "contract"

        def __init__(self):
            self.prompts = []
            self.reflections = 0
            self.last_usage = {"prompt_tokens": 3, "completion_tokens": 2}

        def clone(self):
            # 保持同一份可观测账本，模拟真实 client 的独立 clone。
            return self

        def count_tokens(self, text):
            return len(str(text or "")) // 4

        def chat(self, messages, stream=True, **_kwargs):
            prompt = "\n".join(str(item.get("content") or "")
                                 for item in messages if isinstance(item, dict))
            self.prompts.append(prompt)
            if "子代理任务反思器" in prompt:
                self.reflections += 1
                if self.reflections == 1:
                    return '{"ok":false,"issues":["证据不足"],"next_step":"换一种检索策略"}'
                return '{"ok":true,"issues":[],"next_step":"交给主 Agent 复核"}'
            if "第 1 次重规划" in prompt:
                return ('{"add":[{"id":"recovery","role":"researcher",'
                        '"task":"换一种检索策略并记录来源","depends_on":[],'
                        '"persona":"谨慎的补救研究员","tools":[],"mcp":"deny",'
                        '"reflection":true}]}')
            return "Final Answer: 已完成受限检索并形成结论。"

    def test_llm_reflection_failure_drives_bounded_replan(self):
        llm = self._PlannerLLM()
        planner = agent_mod.Agent(llm=llm, tool_registry={})
        report = planner.orchestrate({
            "tasks": [{"id": "initial", "role": "researcher",
                       "task": "整理接口名称", "depends_on": [],
                       "mcp": "deny", "reflection": True}],
        }, synth=False, max_parallel=1, replan=True, max_replans=1)

        self.assertEqual(report["replans"], 1)
        self.assertEqual(report["revisions"][0]["added"], ["recovery"])
        self.assertEqual(report["results"]["initial"]["reflection"]["source"], "llm")
        self.assertFalse(report["results"]["initial"]["reflection"]["ok"])
        self.assertEqual(report["results"]["recovery"]["status"], "ok")
        self.assertTrue(report["results"]["recovery"]["reflection"]["ok"])
        self.assertTrue(any("执行轨迹" in prompt for prompt in llm.prompts
                            if "重规划" in prompt))

    def test_subagent_context_is_isolated_from_parent_history(self):
        llm = self._PlannerLLM()
        parent = agent_mod.Agent(llm=llm, session_id="parent-session", tool_registry={})
        parent.history = [{"user": "父会话私有内容", "assistant": "不要泄漏"}]
        result = parent._run_child("researcher", "只返回独立结论", reflect=False)
        self.assertEqual(result["status"], "ok")
        child_prompt = "\n".join(llm.prompts[-1:])
        self.assertIn("只返回独立结论", child_prompt)
        self.assertNotIn("父会话私有内容", child_prompt)
        self.assertNotIn("不要泄漏", child_prompt)
        self.assertEqual(parent.history[0]["user"], "父会话私有内容")


if __name__ == "__main__":
    unittest.main()
