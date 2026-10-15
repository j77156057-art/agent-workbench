"""Wave10 回归：把连接器的 MCP 工具投影成一等 TOOLS 条目。

这里最怕两件事：默认就改变行为（每个会话凭空多出一堆工具），以及【换个入口就绕过
边界】。所以用例同时钉住：没打 inline_tools 时零注入、注入的条目只进实例不进全局
注册表、参数错误与调用异常都回文本而不是抛出、以及 list_tools 的握手不会每次构造
Agent 都跑一遍。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import mcp_bridge  # noqa: E402


class FakeMCPError(Exception):
    pass


class FakeMCP:
    """替代 mcp_client：只记录调用次数，不做任何网络/子进程。

    `as_list=True` 时 server_configs 返回 **真实形状**（mcp_client 返回的是带 key 的
    列表，不是字典），用来验证桥接层两种形状都吃得下。
    """

    def __init__(self, servers=None, tools=None, fail=(), as_list=False):
        self.servers = servers or {}
        self.tools = tools or {}
        self.fail = set(fail)
        self.as_list = as_list
        self.list_calls = []
        self.calls = []

    def server_configs(self, root):
        if not self.as_list:
            return dict(self.servers)
        return [{"key": key, **cfg} for key, cfg in self.servers.items()]

    def list_tools(self, root, key):
        self.list_calls.append(key)
        if key in self.fail:
            raise FakeMCPError("连不上")
        return {"ok": True, "server": key, "tools": self.tools.get(key, []),
                "count": len(self.tools.get(key, []))}

    def call_tool(self, root, key, name, arguments=None, timeout=30):
        self.calls.append({"server": key, "tool": name, "arguments": arguments,
                           "timeout": timeout})
        if key == "boom":
            raise FakeMCPError("进程挂了")
        return {"ok": True, "text": "OK from %s/%s args=%s" % (key, name,
                                                              json.dumps(arguments, ensure_ascii=False))}


def _server(inline=True, enabled=True, transport="stdio", read_only=False):
    cfg = {"transport": transport, "url": "http://x/mcp"}
    if inline:
        cfg["inline_tools"] = True
    if not enabled:
        cfg["enabled"] = False
    if read_only:
        cfg["inline_read_only"] = True
    return cfg


def _tool(name, required=("query",), props=None, description="做一件事"):
    return {"name": name, "description": description,
            "input_schema": {"type": "object",
                             "properties": props or {r: {"type": "string"} for r in required},
                             "required": list(required)}}


class _BridgeCase(unittest.TestCase):
    def setUp(self):
        self._prev_root = config.get_runtime("code_root")
        self._prev_mcp = sys.modules.get("mcp_client")
        self._prev_switch = os.environ.get("DOCMIND_MCP_INLINE")
        self.tmp = tempfile.mkdtemp(prefix="docmind_bridge_")
        config.set_runtime("code_root", self.tmp)
        mcp_bridge.reset_cache()

    def tearDown(self):
        config.set_runtime("code_root", self._prev_root)
        mcp_bridge.reset_cache()
        if self._prev_mcp is None:
            sys.modules.pop("mcp_client", None)
        else:
            sys.modules["mcp_client"] = self._prev_mcp
        if self._prev_switch is None:
            os.environ.pop("DOCMIND_MCP_INLINE", None)
        else:
            os.environ["DOCMIND_MCP_INLINE"] = self._prev_switch
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, fake):
        sys.modules["mcp_client"] = fake
        mcp_bridge.reset_cache()
        return fake


# --------------------------------------------------------------------------- #
# 收集与上限
# --------------------------------------------------------------------------- #

class DiscoveryCases(_BridgeCase):
    def test_real_server_configs_shape_is_a_list(self):
        # mcp_client.server_configs 返回 [{"key":...}]，不是 {key: cfg}
        fake = self.install(FakeMCP(servers={"demo": _server(), "unity": _server(enabled=False)},
                                    tools={"demo": [_tool("a"), _tool("b")]}, as_list=True))
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual([row["name"] for row in out["entries"]],
                         ["mcp__demo__a", "mcp__demo__b"])
        self.assertEqual(out["skipped"][0]["server"], "unity")
        self.assertEqual(fake.list_calls, ["demo"])

    def test_nothing_is_inlined_without_the_flag(self):
        fake = self.install(FakeMCP(servers={"demo": _server(inline=False),
                                             "other": _server()},
                                    tools={"demo": [_tool("a")], "other": [_tool("b")]}))
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual([row["name"] for row in out["entries"]], ["mcp__other__b"])
        self.assertEqual(fake.list_calls, ["other"])   # 没打标记的连问都不问

    def test_disabled_server_is_reported_as_skipped(self):
        fake = self.install(FakeMCP(servers={"demo": _server(enabled=False)}))
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["skipped"][0]["reason"], "未启用")

    def test_listing_failure_is_recorded_not_raised(self):
        self.install(FakeMCP(servers={"demo": _server()}, fail={"demo"}))
        out = mcp_bridge.discover(self.tmp)
        self.assertTrue(out["ok"])
        self.assertIn("连不上", out["skipped"][0]["reason"])

    def test_per_server_cap(self):
        many = [_tool("t%02d" % i) for i in range(15)]
        self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": many}))
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual(len(out["entries"]), mcp_bridge.PER_SERVER)
        self.assertTrue(any("dev_mcp_call" in n for n in out["notes"]))

    def test_total_cap_across_servers(self):
        servers = {"s1": _server(), "s2": _server(), "s3": _server()}
        tools = {k: [_tool("t%02d" % i) for i in range(15)] for k in servers}
        self.install(FakeMCP(servers=servers, tools=tools))
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual(len(out["entries"]), mcp_bridge.TOTAL)
        self.assertTrue(any("上限" in row["reason"] for row in out["skipped"]))

    def test_master_switch_disables_everything(self):
        self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        os.environ["DOCMIND_MCP_INLINE"] = "0"
        self.assertEqual(mcp_bridge.discover(self.tmp)["entries"], [])
        os.environ["DOCMIND_MCP_INLINE"] = "1"
        self.assertEqual(len(mcp_bridge.discover(self.tmp)["entries"]), 1)

    def test_list_tools_is_cached_until_refresh(self):
        fake = self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        mcp_bridge.discover(self.tmp)
        mcp_bridge.discover(self.tmp)
        self.assertEqual(fake.list_calls, ["demo"])
        mcp_bridge.discover(self.tmp, refresh=True)
        self.assertEqual(len(fake.list_calls), 2)

    def test_config_change_invalidates_the_cache(self):
        servers = {"demo": _server()}
        fake = self.install(FakeMCP(servers=servers, tools={"demo": [_tool("a")]}))
        self.assertEqual(len(mcp_bridge.discover(self.tmp)["entries"]), 1)
        self.assertEqual(fake.list_calls, ["demo"])
        mcp_bridge.discover(self.tmp)                       # 命中缓存，不再握手
        self.assertEqual(fake.list_calls, ["demo"])
        servers["second"] = _server()
        fake.servers = servers
        fake.tools["second"] = [_tool("b")]
        out = mcp_bridge.discover(self.tmp)
        self.assertEqual(len(out["entries"]), 2)
        self.assertIn("second", fake.list_calls)            # 改了配置立刻生效


class NamingCases(_BridgeCase):
    def test_names_are_schema_safe_and_stable(self):
        self.install(FakeMCP(servers={"godot.ai": _server()},
                             tools={"godot.ai": [_tool("create node/x")]}))
        first = mcp_bridge.discover(self.tmp)["entries"][0]["name"]
        self.assertRegex(first, r"^[A-Za-z0-9_]+$")
        self.assertEqual(first, mcp_bridge._safe_name("godot.ai", "create node/x"))
        self.assertEqual(len(mcp_bridge.discover(self.tmp)["entries"]), 1)

    def test_same_tool_on_different_servers_does_not_collide(self):
        self.install(FakeMCP(servers={"a1": _server(), "b2": _server()},
                             tools={"a1": [_tool("search")], "b2": [_tool("search")]}))
        names = [row["name"] for row in mcp_bridge.discover(self.tmp)["entries"]]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)

    def test_overlong_names_get_a_digest_suffix_and_stay_unique(self):
        long_server = "s" * 40
        long_tool = "t" * 60
        one = mcp_bridge._safe_name(long_server, long_tool)
        two = mcp_bridge._safe_name(long_server, long_tool + "x")
        self.assertLessEqual(len(one), mcp_bridge.NAME_LIMIT)
        self.assertRegex(one, r"^[A-Za-z0-9_]+$")
        self.assertNotEqual(one, two)
        self.assertEqual(one, mcp_bridge._safe_name(long_server, long_tool))


# --------------------------------------------------------------------------- #
# 注册项的形状与调用包装
# --------------------------------------------------------------------------- #

class SpecCases(_BridgeCase):
    def _entry(self, transport="stdio", read_only=False):
        self.install(FakeMCP(servers={"demo": _server(transport=transport,
                                                      read_only=read_only)},
                             tools={"demo": [_tool("search", required=("query",),
                                                   props={"query": {"type": "string"},
                                                          "max": {"type": "integer"}},
                                                   description="检索外部资料")]}))
        return mcp_bridge.discover(self.tmp)["entries"]

    def test_description_carries_server_schema_hint(self):
        text = self._entry()[0]["description"]
        self.assertIn("MCP 连接器 demo", text)
        self.assertIn("query(string,必填)", text)
        self.assertIn("max(integer,可选)", text)

    def test_conservative_defaults(self):
        from agent_runtime.tools import Capability, SideEffect
        specs = mcp_bridge.build_specs(self._entry())
        spec = list(specs.values())[0]
        self.assertIs(spec.capability, Capability.NETWORK)
        self.assertIs(spec.side_effect, SideEffect.MUTATING)
        self.assertFalse(spec.parallel_safe)
        self.assertEqual(spec.group, "mcp")
        self.assertIn("developer", spec.applications)

    def test_http_transport_is_under_the_web_switch(self):
        spec = list(mcp_bridge.build_specs(self._entry(transport="http")).values())[0]
        self.assertEqual(spec.group, "web")

    def test_read_only_marker_makes_it_parallel_safe(self):
        from agent_runtime.tools import SideEffect
        spec = list(mcp_bridge.build_specs(self._entry(read_only=True)).values())[0]
        self.assertTrue(spec.parallel_safe)
        self.assertIs(spec.side_effect, SideEffect.PURE)


class CallWrapperCases(_BridgeCase):
    def setUp(self):
        super().setUp()
        self.single = {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}
        self.multi = {"type": "object", "properties": {"a": {"type": "string"},
                                                       "b": {"type": "string"}},
                      "required": ["a", "b"]}

    def test_json_object_is_forwarded(self):
        fake = self.install(FakeMCP())
        out = mcp_bridge._make_call("demo", "search", self.single)('{"query": "猫"}')
        self.assertIn("OK from demo/search", out)
        self.assertEqual(fake.calls[0]["arguments"], {"query": "猫"})

    def test_bare_text_maps_and_forwards(self):
        fake = self.install(FakeMCP())
        mcp_bridge._make_call("demo", "search", self.single)("猫")
        self.assertEqual(fake.calls[-1]["arguments"], {"query": "猫"})

    def test_bare_text_maps_to_a_single_optional_string_field(self):
        # harness 自己的 MCP server 把 input 声明成可选；只认必填会把
        # 「dev_glob *.vue」这种自然写法判成参数错误
        schema = {"type": "object", "properties": {"input": {"type": "string"}},
                  "required": []}
        fake = self.install(FakeMCP())
        out = mcp_bridge._make_call("demo", "dev_glob", schema)("*.vue")
        self.assertIn("OK from demo/dev_glob", out)
        self.assertEqual(fake.calls[-1]["arguments"], {"input": "*.vue"})
        self.assertIn("input(string,可选)", mcp_bridge._schema_hint(schema))

    def test_two_optional_fields_still_need_json(self):
        schema = {"type": "object",
                  "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
        fake = self.install(FakeMCP())
        out = mcp_bridge._make_call("demo", "search", schema)("bare words")
        self.assertIn("JSON", out)
        self.assertEqual(fake.calls, [])

    def test_ambiguous_text_is_rejected_with_guidance(self):
        fake = self.install(FakeMCP())
        out = mcp_bridge._make_call("demo", "search", self.multi)("just words")
        self.assertIn("JSON", out)
        self.assertEqual(fake.calls, [])

    def test_json_that_is_not_an_object_is_rejected(self):
        fake = self.install(FakeMCP())
        out = mcp_bridge._make_call("demo", "search", self.multi)("[1,2]")
        self.assertIn("JSON 对象", out)
        self.assertEqual(fake.calls, [])

    def test_empty_argument_is_allowed(self):
        fake = self.install(FakeMCP())
        mcp_bridge._make_call("demo", "ping", {"type": "object"})("")
        self.assertEqual(fake.calls[-1]["arguments"], {})

    def test_exception_becomes_text(self):
        self.install(FakeMCP(servers={"boom": _server()}))
        out = mcp_bridge._make_call("boom", "go", self.single)('{"query":"x"}')
        self.assertIn("MCP 调用失败", out)

    def test_various_result_shapes_render_to_text(self):
        self.install(FakeMCP())
        for payload, needle in (({"ok": True, "text": "abc"}, "abc"),
                                ({"ok": True, "content": "xyz"}, "xyz"),
                                ({"ok": True, "result": {"a": 1}}, '"a"'),
                                ("plain", "plain")):
            with mock.patch("mcp_client.call_tool", return_value=payload):
                out = mcp_bridge._make_call("demo", "search", self.single)('{"query":"x"}')
            self.assertIn(needle, out, str(payload))

    def test_failed_result_is_not_hidden(self):
        self.install(FakeMCP())
        with mock.patch("mcp_client.call_tool", return_value={"ok": False, "error": "超时"}):
            out = mcp_bridge._make_call("demo", "search", self.single)('{"query":"x"}')
        self.assertIn("超时", out)

    def test_empty_response_says_so(self):
        self.install(FakeMCP())
        with mock.patch("mcp_client.call_tool", return_value={"ok": True, "text": ""}):
            out = mcp_bridge._make_call("demo", "search", self.single)('{"query":"x"}')
        self.assertIn("空内容", out)


# --------------------------------------------------------------------------- #
# attach 与 Agent 集成
# --------------------------------------------------------------------------- #

class AttachCases(_BridgeCase):
    def test_attach_adds_specs_to_a_dict(self):
        self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        tools = {"grep": "existing"}
        report = mcp_bridge.attach(tools, self.tmp)
        self.assertEqual(report["added"], ["mcp__demo__a"])
        self.assertIn("mcp__demo__a", tools)

    def test_attach_never_clobbers_a_builtin(self):
        name = mcp_bridge._safe_name("demo", "grep")
        self.install(FakeMCP(servers={"demo": _server()},
                             tools={"demo": [{"name": "grep", "description": "撞名",
                                              "input_schema": {}}]}))
        tools = {name: "builtin"}
        mcp_bridge.attach(tools, self.tmp)
        self.assertEqual(tools[name], "builtin")

    def test_other_applications_get_nothing(self):
        self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        tools = {}
        report = mcp_bridge.attach(tools, self.tmp, application_id="it_support")
        self.assertEqual(report["added"], [])
        self.assertEqual(tools, {})

    def test_empty_root_is_a_noop(self):
        fake = self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        self.assertEqual(mcp_bridge.attach({}, "")["added"], [])
        self.assertEqual(fake.list_calls, [])

    def test_status_reports_the_cap(self):
        self.install(FakeMCP(servers={"demo": _server()}, tools={"demo": [_tool("a")]}))
        st = mcp_bridge.status(self.tmp)
        self.assertEqual(st["inline"], 1)
        self.assertEqual(st["cap"]["total"], mcp_bridge.TOTAL)


class AgentIntegrationCases(_BridgeCase):
    def test_agent_instance_gets_the_tools_but_global_registry_does_not(self):
        import agent as agent_mod
        self.install(FakeMCP(servers={"demo": _server()},
                             tools={"demo": [_tool("search",
                                                   props={"query": {"type": "string"}},
                                                   description="外部检索")]}))

        class _NullLLM:
            provider = "mock"

            def chat(self, messages, stream=True, **kwargs):
                raise AssertionError("不该在这里调用模型")

        instance = agent_mod.Agent(llm=_NullLLM())
        # 工具权威：注入发生在实例上、来自操作者配置（不是 prompt）。但注入的
        # 连接器工具一律是 NETWORK 能力，`_web_blocked`（9f8ef17，2026-10-14 防御
        # 兜底）在联网关时把它们对模型隐藏 —— 默认 web_enabled=False。
        self.assertIn("mcp__demo__search", instance.tools)
        self.assertNotIn("mcp__demo__search", instance._effective_tool_names())
        # 联网开：这批外网连接器工具才真正暴露给模型（同一实例，按回合开关）。
        instance.web_enabled = True
        self.assertIn("mcp__demo__search", instance._effective_tool_names())
        # 全局注册表（所有会话共用）不能被撑大 —— 注入只进实例。
        instance.web_enabled = False
        self.assertNotIn("mcp__demo__search", agent_mod.TOOLS)

    def test_default_config_injects_nothing(self):
        import agent as agent_mod
        self.install(FakeMCP(servers={"demo": _server(inline=False)},
                             tools={"demo": [_tool("search")]}))

        class _NullLLM:
            provider = "mock"

            def chat(self, messages, stream=True, **kwargs):
                raise AssertionError("不该在这里调用模型")

        instance = agent_mod.Agent(llm=_NullLLM())
        self.assertEqual([n for n in instance.tools if n.startswith("mcp__")], [])
        self.assertEqual(instance.mcp_inline["added"], [])


if __name__ == "__main__":
    unittest.main()
