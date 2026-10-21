# -*- coding: utf-8 -*-
"""开发舱「重试该成员」按钮、以及「实际工具调用」面板。

三个缺陷全部来自**真实跑过一次工作流**才暴露，单元测试与既有测试一个都没拦住。

## 一、重试按钮 100% 报500

``workflow_callbacks`` 返回 ``{runner, synth_runner, replanner}``，端点把这三个
一起 splat 进 ``GameWorkflowManager.retry_subagent``，而后者签名里**没有**
``replanner``：

    TypeError: GameWorkflowManager.retry_subagent() got an unexpected keyword
    argument 'replanner'

前端拿到的不是 JSON，只能显示「响应不是合法JSON（HTTP 500）」。
签名与调用方脱节，却没有任何测试覆盖这个端点。

## 二、「实际工具调用」恒为 0 条

面板按 ``kind in {before_tool, after_tool, before_mcp, after_mcp}`` 从**持久化
事件数组**里过滤。而工具生命周期事件只进子代理自己的 hook 缓冲（``record_hook``），
从不落到工作流事件流——面板没撒谎，它读的那条流里确实没有工具事件。
同一屏的任务面板里却明明列着 read_file / grep / python_exec。

## 三、hook 缓冲 48 条配额被工具事件吃光

``record_hook`` 的上限检查在**最前面**：每次工具调用发 before+after 两条，
**24 次工具调用就把配额用光**，之后连 trace 里的工具事件也不再记录。
"""
import ast
import inspect
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402
from agent import (  # noqa: E402
    SINK_LIFECYCLE_HOOKS, SINK_TOOL_HOOKS, _exc_traceback, _short_root, _tool_event_payload,
    _tool_event_text,
)
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402

ROUTE_FILE = os.path.join(ROOT, "api_routes", "agent.py")


def _workflow_callback_keys():
    """从 api_routes/agent.py 里**真的**取出 workflow_callbacks 的返回键。

    刻意不把键名硬编码在测试里：那个 dict 一改，测试必须跟着失败——
    这正是本缺陷的成因（签名与调用方各写各的）。
    """
    tree = ast.parse(open(ROUTE_FILE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "workflow_callbacks":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                return [k.value for k in sub.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    raise AssertionError("api_routes/agent.py 里找不到 workflow_callbacks 的 return dict")


class RetryEndpointContractTests(unittest.TestCase):
    """端点 splat 的每个键，方法签名都必须收得下。"""

    def test_retry_subagent_accepts_every_key_the_endpoint_passes(self):
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
        for key in _workflow_callback_keys():
            self.assertTrue(has_var_kw or key in params,
                            "端点会把 %r 传给 retry_subagent，但签名里没有它"
                            " —— UI 上的「重试该成员」每次都 500" % key)

    def test_the_missing_key_was_replanner(self):
        """把这个 bug 的具体形态钉住：曾经缺的正是 replanner。"""
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        self.assertIn("replanner", params,
                      "replanner 仍未回到签名上")

    def test_replanner_is_optional(self):
        """可选性很重要：没有规划器时重试必须照常工作，而不是报参数错。"""
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        self.assertIs(params["replanner"].default, None)


class ToolEventsReachTheWorkflowStreamTests(unittest.TestCase):
    """工具生命周期事件必须落进持久事件流，面板才有东西可显示。"""

    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, tasks=[{"id": "t1", "task": "改个常量",
                                            "depends_on": []}])

    def _kinds(self):
        return [e.get("kind") for e in self.manager.get(self.wid)["events"]]

    def test_a_before_tool_step_becomes_a_persisted_workflow_event(self):
        self.manager.record_agent_step(self.wid, "t1", "coder", {
            "type": "before_tool", "text": "调用前 · read_file"})
        self.assertIn("before_tool", self._kinds(),
                      "工具事件没有落进持久事件流——面板会显示 0 条")

    def test_all_four_hook_kinds_land(self):
        for kind in SINK_TOOL_HOOKS:
            self.manager.record_agent_step(self.wid, "t1", "coder", {
                "type": kind, "text": "x · y"})
        kinds = self._kinds()
        for kind in SINK_TOOL_HOOKS:
            self.assertIn(kind, kinds, "%s 没有落进事件流" % kind)

    def test_tool_events_carry_task_and_role(self):
        self.manager.record_agent_step(self.wid, "t1", "tester", {
            "type": "after_tool", "text": "调用后 · grep"})
        ev = [e for e in self.manager.get(self.wid)["events"]
              if e.get("kind") == "after_tool"][0]
        self.assertEqual(ev.get("task_id"), "t1")
        self.assertEqual(ev.get("role"), "tester")

    def test_plain_steps_still_do_not_pollute_the_workflow_stream(self):
        """thought/action/observation 不落持久流（那是刻意的：避免每步落盘膨胀）。"""
        for step in ("thought", "action", "observation"):
            self.manager.record_agent_step(self.wid, "t1", "coder",
                                           {"type": step, "text": "x"})
        self.assertEqual(self._kinds().count("subagent_step"), 0)

    def test_the_two_kind_sets_are_the_downstream_contract(self):
        """``SINK_TOOL_HOOKS`` / ``SINK_LIFECYCLE_HOOKS`` 是**下游约定**，不是实现细节。

        面板按前者从持久事件数组里过滤工具生命周期，后者再加上模型等待这一对。
        这两个集合一旦被改小，下游只会以为「从来就没有工具事件」——
        而**顺着它遍历**的用例会跟着一起变空，于是**空转通过**：
        ``test_all_four_hook_kinds_land`` 遍历的是被测常量本身，
        常量被清空时它一条都不跑，绿得毫无意义。

        所以期望集合在这里写死：它要能抓住「集合被清空 / 被裁剪」这类变异。
        """
        self.assertEqual(set(SINK_TOOL_HOOKS),
                         {"before_tool", "after_tool", "before_mcp", "after_mcp"})
        self.assertEqual(set(SINK_LIFECYCLE_HOOKS),
                         set(SINK_TOOL_HOOKS) | {"llm_wait_start", "llm_wait_end"})


class ToolEventTextTests(unittest.TestCase):
    """事件行是给人和 UI 看的摘要，不是证据主体。"""

    def test_it_names_the_tool_and_the_stage(self):
        text = _tool_event_text("before_tool", {"tool": "read_file"}, {})
        self.assertIn("read_file", text)
        self.assertIn("调用前", text)

    def test_a_blocked_call_says_so(self):
        text = _tool_event_text("after_tool", {"tool": "apply_edit"},
                                {"blocked": True, "reason": "需要审批"})
        self.assertIn("被拦截", text)
        self.assertIn("需要审批", text)

    def test_it_reports_duration_when_known(self):
        text = _tool_event_text("after_tool",
                                {"tool": "grep", "duration_ms": 42}, {})
        self.assertIn("42", text)

    def test_a_toolless_payload_does_not_crash(self):
        self.assertEqual(_tool_event_text("before_tool", {}, {}), "before_tool")


class ExecutionRootIsReportedTests(unittest.TestCase):
    """每次工具调用都要**自报执行目录**——读侧写侧都覆盖。

    ## 为什么必须有

    试做区隔离静态排查过三次，每次都判「scope 机制正常、是 contextvar 不跨
    线程」，三次都没定位到问题。而判据其实一直在手上：子代理一次 grep 命中了
    `.docmind\\web\\index.audio.position.worklet.js`——那个目录只存在于**真实
    项目**和**仓库根**，**试做区副本里没有**（复制时排除了 `.docmind`）。
    那次执行用的根根本不是试做区。

    此前看不出来，是因为没有任何事件带过执行目录，只能靠「观察是空的」间接
    猜，而那次的观察还是错配的（见 test_parallel_traj_pairing.py）。

    之前给 `python_exec` 加的「执行目录」只覆盖写侧一条路，而且实测四次真跑
    一次都没触发——计划从没派给有 `python_exec` 的角色。
    """

    #: 三个候选根：真实项目、试做区 workspace、仓库根。
    #: `_get_code_root()` 在**什么都没设**时返回第三个——这一点很关键，
    #: 说明「读到空结果」完全可能只是落到了仓库根，而不是试做区失效。
    CANDIDATE_ROOTS = [
        r"D:\WorkBuddy\godot_sample",
        r"D:\WorkBuddy\rag-agent\.docmind\project_stages\wf-abc\workspace",
        r"D:\WorkBuddy\rag-agent",
    ]

    def test_the_short_form_separates_all_three_roots(self):
        """分不开就等于没报——这是这条诊断的全部意义。"""
        shorts = [_short_root(r) for r in self.CANDIDATE_ROOTS]
        self.assertEqual(len(set(shorts)), 3,
                         "短形式分不开三个候选根：%s" % shorts)

    def test_the_payload_carries_the_root(self):
        payload = _tool_event_payload("read_file", "path: a.gd", depth=2,
                                      session_id="s")
        for key in ("tool", "argument_chars", "depth", "session_id", "code_root"):
            self.assertIn(key, payload)
        self.assertEqual(payload["tool"], "read_file")
        self.assertEqual(payload["depth"], 2)

    def test_the_event_text_shows_the_short_root(self):
        payload = _tool_event_payload("read_file", "path: a.gd")
        payload["code_root"] = self.CANDIDATE_ROOTS[1]
        text = _tool_event_text("before_tool", payload, {})
        self.assertIn("@wf-abc/workspace", text,
                      "事件行里看不到执行目录——这条诊断就白加了")

    def test_the_text_does_not_carry_the_full_path(self):
        """完整路径会把事件行撑到没法看；payload 里留着完整值即可。"""
        payload = {"tool": "grep", "code_root": self.CANDIDATE_ROOTS[1]}
        text = _tool_event_text("before_tool", payload, {})
        self.assertNotIn("project_stages\\wf-abc", text)
        self.assertLess(len(text), 80, "事件行太长：%r" % text)

    def test_an_empty_root_is_simply_omitted(self):
        text = _tool_event_text("before_tool", {"tool": "grep",
                                                "code_root": ""}, {})
        self.assertNotIn("@", text)

    def test_a_broken_root_lookup_cannot_break_the_tool_call(self):
        """诊断字段取不到时不能把工具调用搞挂——那比没有诊断更坏。"""
        import agent as agent_mod

        orig = agent_mod._tool_code_root

        def boom():
            raise RuntimeError("取根失败")

        agent_mod._tool_code_root = boom
        try:
            payload = _tool_event_payload("grep", "pattern: x")
        finally:
            agent_mod._tool_code_root = orig
        self.assertEqual(payload.get("code_root"), "")
        self.assertEqual(payload.get("tool"), "grep",
                         "取根失败把工具名也弄丢了——那这条诊断成了负收益")
        self.assertIn("grep", _tool_event_text("before_tool", payload, {}))

    def test_both_call_sites_use_the_same_builder(self):
        """顺序路径和并行批次路径各有一处 `before_tool`。

        §9 的教训：「上限写两处，就等于写了一处旧的」。所以这里钉住两处
        都走同一个构造函数——将来改字段只改一处。
        """
        import inspect

        import agent as agent_mod

        src = inspect.getsource(agent_mod)
        # `argument_chars` 只允许出现在构造函数里。出现第二次就说明有人在调用
        # 点又手写了一份 payload，字段会分叉。
        self.assertEqual(
            src.count('"argument_chars": len('), 1,
            "「argument_chars: len(」出现了 %d 次——顺序/并行路径里有人自己手写"
            "了一份 payload" % src.count('"argument_chars": len('))
        # `before_tool` 一处在 `run`（顺序路径）、一处在 `_run_batch`（并行
        # 批次），所以这里查整个模块而不是某一个方法。
        self.assertGreaterEqual(
            src.count('run_workflow("before_tool", _tool_event_payload('), 2,
            "两处 before_tool 都该走统一构造函数")

    def test_the_trace_keeps_the_root(self):
        """事件文本里只有短形式；trace JSON 里要留完整值。

        排查「读取全是空」时需要程序化比对每次调用落在哪个根上，而
        `record_hook` 的字段白名单漏一个字段，trace 就少一份证据。
        """
        import inspect

        import agent as agent_mod

        src = inspect.getsource(agent_mod.Agent._run_child)
        self.assertIn('"code_root"', src,
                      "record_hook 的白名单里没有 code_root——"
                      "trace 里就看不到执行目录，排查只能靠人眼扫文本")


class TracebackIsEvidenceTests(unittest.TestCase):
    """子代理失败只存 `type: msg` 时，排查只剩一句标签。"""

    def test_it_names_the_function_that_raised(self):
        try:
            raise AttributeError("'NoneType' object has no attribute 'strip'")
        except AttributeError as exc:
            text = _exc_traceback(exc)
        self.assertIn("AttributeError", text)
        self.assertIn("strip", text)
        self.assertIn("test_it_names_the_function_that_raised", text,
                      "栈里没有抛出点——那这条栈依然没法用来定位")

    def test_it_degrades_instead_of_raising(self):
        class Weird(BaseException):
            def __str__(self):
                raise RuntimeError("连 __str__ 都炸")

        text = _exc_traceback(Weird())
        self.assertIn("Weird", text)

    def test_agent_exposes_the_helper_it_uses(self):
        self.assertTrue(callable(agent._exc_traceback))


class _ToolCallingLLM:
    """每轮调一次真工具（`read_file`，**每次换目标**），第 `ROUNDS` 轮之后才给结论。

    必须**真的调工具**：工具事件是 `record_hook` 在工具调用前后发出来的，
    一个只输出文本的假模型永远触发不了 `record_hook`——那几条转发/配额的判据
    在纯层里走不到，就是这个原因。

    工具名取 `read_file`：它在 researcher 的白名单里
    （`_SUBAGENT_ROLES['researcher']['tools']`），而 `list_dir` 不在——用错工具
    会被「受限」拦掉，`record_hook` 同样不发事件，症状与「没转发」一模一样。
    实测踩过：`list_dir` 那版收到 0 条工具事件，差点当成转发坏了。

    **每轮必须换目标**：`_run` 里有一道「反复用完全相同的参数调用同一工具」
    的防空转守卫，参数不变的话第二轮就被它掐停，`trace.hooks` 只剩 14 条——
    远够不到 `>= 48`，于是「配额抬到 10**9」「配额检查挪掉」那两条变异
    在现场层照样绿（实测：ROUNDS 调到 30 也是只走 1 步）。
    """

    provider = "fake"
    model = "tool-caller"

    #: 子代理的真实 cwd 是仓库根。目标池必须**够大**：`_run` 的防空转守卫按
    #: 「工具+参数」去重，参数用尽后必然重复——目标池只有 4 个文件时第二轮就被
    #: 掐停，缓冲停在26 条，`>= 48` 那道线形同虚设（实测）。
    #: 现在从真实目录里扫，取前 30 个：仓库根 + `agent_runtime/` 一共 140+ 个 .py。
    @classmethod
    def _targets(cls):
        import glob
        found = sorted(glob.glob(os.path.join(ROOT, "*.py"))
                       + glob.glob(os.path.join(ROOT, "agent_runtime", "*.py")))
        return [os.path.relpath(p, ROOT).replace("\\", "/")
                for p in found[:cls.ROUNDS]] or ["agent.py"]

    #: 30 轮 × (before_tool + after_tool + llm_wait_start + llm_wait_end) ≈ 120 条，
    #: 稳稳越过 48——48 是配额线，产不到它就量不出配额被摘掉的后果。
    ROUNDS = 30

    def __init__(self):
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []
        self.targets = self._targets()

    def clone(self):
        return _ToolCallingLLM()

    def chat(self, messages, stream=True, **kw):
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.last_tool_calls = []
        self.i += 1
        target = self.targets[(self.i - 1) % len(self.targets)]
        if self.i >= self.ROUNDS:
            text = "Thought: 够了\nFinal Answer: 读完了，主 Agent 在 agent.py。"
        else:
            text = ("Thought: 读 %s\nAction: read_file\nAction Input: path: %s"
                    % (target, target))
        if stream:
            return (ch for ch in text)
        return text

    def count_tokens(self, text):
        return 1


class ToolHooksActuallyFireTests(unittest.TestCase):
    """**真跑一次子代理**，验工具事件真的从 `record_hook` 走到了 sink。

    这一类与 `ToolEventsReachTheWorkflowStreamTests` 的分工：那一类直接调
    `manager.record_agent_step(...)` 往流里塞，验的是「流收不收」；
    这一类不碰 manager，只接 `_run_child` 的 `step_sink`，验的是
    「`record_hook` 到底有没有把工具事件发出来」。

    没有这一类，`record_hook` 里那道 `if str(kind) in SINK_TOOL_HOOKS:`
    可以整个改成 `if False:` 而**没有任何测试变红**——实测那正是变异测试
    报「活下来」的原因，不是守卫没牙，是这条路径压根没被走到过。
    """

    def setUp(self):
        # `read_file` 靠 `tools` 的 `code_root` 才读得到东西。没设的话每次调用
        # 都返回「尚未配置代码库根目录」——那也算一次工具调用（hook 照发），
        # 但读不到内容，测试会误以为工具没生效。实测踩过。
        #
        # **必须还原**：这个 `code_root` 是**全局运行时状态**，原先设完就留着，
        # 于是本类跑完之后整个进程里 `tools.search_code` 都带着它。后果是
        # `tests/test_orchestrator.py::TraceFeedbackTests::test_child_trace_is_bounded`
        # 单独跑绿、跟在���个类后面跑就红（断言报「3 not greater than 3」）。
        #
        # 症状之所以离得这么远：残留的 code_root 让 `search_code` 走真检索，
        # 而那一步在本机恰好**空检索**；而「空检索」被子代理判成**工具失败**，
        # 于是 `_TOOL_FAIL_LIMIT=3` 触发、只跑 3 步，`n_steps` 与保留的轨迹一样长，
        # 断言「真实步数应大于保留轨迹长度」就炸了。
        import tools as _tools
        previous = _tools.get_runtime("code_root")
        _tools.set_runtime("code_root", ROOT)
        self.addCleanup(_tools.set_runtime, "code_root", previous)

    def _sinked(self):
        seen = []
        out = agent.Agent(llm=_ToolCallingLLM())._run_child(
            "researcher", "读一下 agent.py", step_sink=seen.append)
        self.sink = seen
        return seen, out

    @staticmethod
    def _hooks(out):
        """子代理本地hook 缓冲——就是那句`>= 48` 配额管的东西。

        它和 `step_sink` 是**两处**：配额检查只管本地缓冲（`out["trace"]["hooks"]`），
        而转发到工作流事件流走 `emit_sink`。旧代码把配额检查放在最前面直接
        `return`，于是 24 次工具调用就把缓冲用光、工具事件从trace 里消失。
        """
        return list(((out or {}).get("trace") or {}).get("hooks") or [])

    def test_the_child_really_calls_a_tool(self):
        """自证前提：这一类必须真的调过工具，否则下面几条全是空转。"""
        seen, out = self._sinked()
        types = [row.get("type") for row in seen]
        self.assertTrue(
            any(str(t) in SINK_TOOL_HOOKS for t in types),
            "这一轮一次工具都没调成（收到 %r）——下面几条断言全是空转，"
            "先查白名单与工具名" % (types[:8],))
        self.assertTrue(
            any(str(h.get("kind")) in SINK_TOOL_HOOKS for h in self._hooks(out)),
            "trace.hooks 里没有工具事件——配额或转发那条路坏了")

    def test_both_sides_of_every_tool_call_reach_the_sink(self):
        """**调过的那次工具调用**，前后两侧都要到。

        只断言 `before_tool` / `after_tool`：`before_mcp` / `after_mcp` 要真调一次
        MCP 工具才发得出，这个假模型不调 MCP。四种 kind 都在 `SINK_TOOL_HOOKS` 里，
        所以「集合里有的 kind 必须都到」是**错**的断言——它会把「没调 MCP」
        误报成「MCP 事件丢了」。
        """
        seen, _out = self._sinked()
        types = [row.get("type") for row in seen]
        for kind in ("before_tool", "after_tool"):
            self.assertIn(kind, types,
                          "工具事件 %s 没有到达 sink——面板会显示 0 条" % kind)

    def test_the_hook_buffer_holds_the_tool_events(self):
        """`trace.hooks` 里必须留着工具事件——配额检查把它掐掉就断这条。

        这次只调1~2 次工具，离 48 还远；配额被压到 1 或被挪到最前面时，
        缓冲里就只剩1 条，工具事件从 trace 里消失。
        """
        _seen, out = self._sinked()
        hooks = self._hooks(out)
        tool_rows = [h for h in hooks if str(h.get("kind")) in SINK_TOOL_HOOKS]
        self.assertGreaterEqual(
            len(tool_rows), 2,
            "trace.hooks 里只有 %d 条工具事件（共 %d 条 hook）——"
            "配额检查把它提前掐死了"
            % (len(tool_rows), len(hooks)))

    def test_the_buffer_stays_bounded(self):
        """缓冲被卡在48 上——而 48 其实是**两道**截断里较小的那一道。

        `record_hook` 里有一句 `if len(hook_events) >= 48: return`，而
        `_child_trace` 里还有一句 `[:48]`。所以从 `out["trace"]["hooks"]`
        看过去，**恒为 48**，无论前一句怎么改——实测把配额抬到 10**9、
        以及把检查整个删掉，观测到的都是 48 条。

        这条断言因此只能钉住「trace 里的 hooks 不超过 48」这个**结果**，
        断不了「是哪一道截断起的作用」。要断后者得能观测 `_child_trace`
        之前的缓冲，而那条出口不存在——所以 `battery_workflow_events` 里
        对应的两条变异只能记「本环境未验证」，不能记抓住。
        """
        _seen, out = self._sinked()
        hooks = self._hooks(out)
        self.assertLessEqual(
            len(hooks), 48,
            "trace.hooks 有 %d 条——越过 48 了，两道截断都被摘掉了" % len(hooks))

    def test_the_quota_actually_bites_at_a_higher_step_count(self):
        """自证「配额真的在生效」：把工具轮数提上去，缓冲也不会涨过 48。

        没有这条，`test_the_buffer_stays_bounded` 可能是**恒真**的——比如
        子代理只肯走 10 步时缓冲本来就到不了 48。实测踩过：目标池只有 4 个
        文件时防空转守卫第二轮就掐停，缓冲停在 26 条，48 那道线形同虚设。
        """
        _seen, out = self._sinked()
        hooks = self._hooks(out)
        self.assertGreaterEqual(
            len(_ToolCallingLLM()._targets()), 20,
            "目标池太小：防空转守卫会提前掐停，缓冲涨不到配额线")
        self.assertGreaterEqual(
            len(hooks), 40,
            "缓冲只有 %d 条，离配额 48 还太远——上面那条断言量不到东西" % len(hooks))

    def test_llm_wait_pairs_reach_the_sink_too(self):
        """`llm_wait_start/end` 走同一个 sink（98% 静默那条修复的回归守卫）。"""
        seen, _out = self._sinked()
        types = [row.get("type") for row in seen]
        self.assertIn("llm_wait_start", types,
                      "模型等待没有到达 sink——时间线会回到 98% 静默")
        self.assertIn("llm_wait_end", types, "模型等待只有头没有尾")


if __name__ == "__main__":
    unittest.main()