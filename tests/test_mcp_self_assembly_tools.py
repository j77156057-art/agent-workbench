# -*- coding: utf-8 -*-
"""Agent 自助装配 MCP 工具链：search → (审批) add → probe → discover → (审批) decide。

全部离线：stdio 探活/发现用桩替换，不真正拉起子进程；审批写临时项目的台账。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
import agent as agent_mod
import mcp_capabilities
import mcp_client
from tools import (
    dev_mcp_search, dev_mcp_add, dev_mcp_probe, dev_mcp_discover,
    dev_mcp_decide, dev_mcp_remove, dev_approve,
    set_session_web_enabled, _session_web_enabled,
)
from game_workbench import (
    approval, list_mcp_proposals, remove_mcp_proposal,
)
from tools import _mcp_server_approval_target


def _parse(text):
    return json.loads(text)


class McpSelfAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docmind_mcp_self_")
        self.root = os.path.join(self.tmp.name, "project")
        os.makedirs(self.root)
        self._old_root = config.get_runtime("code_root")
        config.set_runtime("code_root", self.root)

    def tearDown(self):
        if self._old_root:
            config.set_runtime("code_root", self._old_root)
        else:
            config._RUNTIME.pop("code_root", None)
        self.tmp.cleanup()

    def _approve(self, action, target):
        # 模拟工作台上的用户确认；Agent 的 dev_approve 不能写入 MCP 审批。
        row = approval(self.root, action, "user", approved=True, target=target)
        self.assertEqual(row["user"], "user")

    def test_agent_cannot_self_approve_mcp(self):
        result = dev_approve("action: mcp_server\ntarget: arbitrary")
        self.assertIn("不能自行审批", result)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".docmind_approvals.jsonl")))

    # ---- search ------------------------------------------------------------
    def test_search_offline_directory_returns_template(self):
        token = set_session_web_enabled(False)
        try:
            r = _parse(dev_mcp_search("EDA"))
        finally:
            _session_web_enabled.reset(token)
        self.assertTrue(r["ok"])
        item = r["results"][0]
        self.assertEqual(item["id"], "eda")
        self.assertIn("template", item)
        self.assertEqual(item["template"]["transport"], "stdio")

    def test_agent_search_uses_registry_only_when_session_allows_web(self):
        calls = []

        def fake_registry(query):
            calls.append(query)
            if query != "easyeda":
                return {"ok": False, "candidates": []}
            return {"ok": True, "candidates": [{
                "transport": "stdio", "command": "npx",
                "args": ["-y", "@vlabsoft/easyeda-pro-mcp"], "url": "",
                "env": {}, "headers": {}, "command_unresolved": False,
                "provenance": {"server_name": "io.github.VLab-Software/easyeda-pro-mcp",
                               "description": "EasyEDA Pro bridge",
                               "url": "https://github.com/VLab-Software/easyeda_mcp",
                               "domain": "github.com"},
            }]}

        with patch("mcp_registry_bridge.registry_fn_for", return_value=fake_registry):
            token = set_session_web_enabled(True)
            try:
                result = _parse(dev_mcp_search("EDA"))
            finally:
                _session_web_enabled.reset(token)
        self.assertIn("easyeda", calls)
        options = result["results"][0]["connection_options"]
        self.assertTrue(any(o.get("id") == "io.github.VLab-Software/easyeda-pro-mcp"
                            for o in options))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".docmind_mcp.json")))

        token = set_session_web_enabled(False)
        try:
            with patch("mcp_registry_bridge.registry_fn_for", side_effect=AssertionError("network")):
                offline = _parse(dev_mcp_search("EDA"))
        finally:
            _session_web_enabled.reset(token)
        self.assertEqual(offline["results"][0]["source_status"], "offline_guide")

    def test_natural_language_agent_can_discover_mcp_without_preconfigured_server(self):
        class ScriptedLLM:
            def __init__(self):
                self.calls = 0
                self.first_messages = None
                self.first_tool_names = None

            def chat(self, messages, stream=True, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.first_messages = messages
                    self.first_tool_names = agent._effective_tool_names()
                response = ("Thought: 先查真实连接候选\nAction: dev_mcp_search\nAction Input: EDA"
                            if self.calls == 1 else "Final Answer: 已找到候选，等待用户选择。")
                return [response] if stream else response

            def count_tokens(self, _text):
                return 0

        def fake_registry(query):
            if query != "easyeda":
                return {"ok": False, "candidates": []}
            return {"ok": True, "candidates": [{
                "transport": "stdio", "command": "npx",
                "args": ["-y", "@vlabsoft/easyeda-pro-mcp"], "url": "",
                "env": {}, "headers": {}, "command_unresolved": False,
                "provenance": {"server_name": "io.github.VLab-Software/easyeda-pro-mcp",
                               "description": "EasyEDA Pro bridge",
                               "url": "https://github.com/VLab-Software/easyeda_mcp",
                               "domain": "github.com"},
            }]}

        old_state_root = config.STATE_ROOT
        config.STATE_ROOT = os.path.join(self.tmp.name, "state")
        try:
            llm = ScriptedLLM()
            agent = agent_mod.Agent(llm=llm, session_id="mcp-natural-language", tool_mode="react")
            with patch("mcp_registry_bridge.registry_fn_for", return_value=fake_registry):
                events = list(agent.run("帮我连接 EDA 的 MCP", web_enabled=True, stream=True))
            self.assertIn("dev_mcp_search", str(llm.first_messages))
            self.assertIn("dev_mcp_search", llm.first_tool_names)
            self.assertTrue(any(e.get("type") == "action" and "dev_mcp_search" in e.get("text", "")
                                for e in events))
            self.assertTrue(any(e.get("type") == "observation" and "easyeda-pro-mcp" in e.get("text", "")
                                for e in events))
            self.assertFalse(os.path.exists(os.path.join(self.root, ".docmind_mcp.json")))
        finally:
            config.STATE_ROOT = old_state_root

    def test_search_rejects_short_query(self):
        self.assertTrue(dev_mcp_search("e").startswith("MCP 搜索失败"))

    # ---- add：审批门 --------------------------------------------------------
    def test_add_blocked_then_approved_saves_server(self):
        arg = ("key: kicad\nlabel: KiCad MCP\ntransport: stdio\n"
               "command: uvx\nargs: kicad-mcp-pro --transport stdio\n")
        blocked = _parse(dev_mcp_add(arg))
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["action"], "mcp_server")
        target = blocked["target"]
        self.assertTrue(target.startswith("kicad:"))

        self._approve("mcp_server", target)
        ok = _parse(dev_mcp_add(arg))
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["server"]["transport"], "stdio")
        self.assertTrue(ok["server"]["enabled"])

        cfg = mcp_client.get_server_config(self.root, "kicad")
        self.assertEqual(cfg["command"], "uvx")
        self.assertEqual(cfg["args"], ["kicad-mcp-pro", "--transport", "stdio"])

    def test_add_blocked_records_proposal_for_one_click_confirm(self):
        # 回归：审批门 blocked 时把逐字连接配置暂存为「待装配提案」，
        # 让用户在连接器设置页一键确认（走 mcp_server_save_ep 同一条审批）
        # 即可放行——修掉「队列批准被拒、设置页无从复现参数」的死循环。
        arg = ("key: easyeda-pro\nlabel: EasyEDA Pro MCP\ntransport: stdio\n"
               "command: npx\nargs: -y @vlabsoft/easyeda-pro-mcp\n")
        blocked = _parse(dev_mcp_add(arg))
        self.assertTrue(blocked["blocked"])
        target = blocked["target"]
        self.assertTrue(target.startswith("easyeda-pro:"))
        # 提案负载随阻断返回，供用户看到「要批准的是什么」
        self.assertEqual(blocked["proposal"]["command"], "npx")
        self.assertEqual(blocked["proposal"]["args"],
                         ["-y", "@vlabsoft/easyeda-pro-mcp"])
        self.assertIn("设置", blocked["hint"])

        # 设置页轮询待装配提案：应拿到逐字相同的 config
        props = list_mcp_proposals(self.root)
        self.assertEqual([p["target"] for p in props], [target])
        pkey = props[0]["key"]
        pcfg = props[0]["config"]
        self.assertEqual(pkey, "easyeda-pro")
        self.assertEqual(_mcp_server_approval_target(pkey, pcfg), target)

        # 用户点「确认」= mcp_server_save_ep 的两步：写 hash 匹配审批 + 清提案
        approval(self.root, "mcp_server", "workbench-user",
                 approved=True, target=_mcp_server_approval_target(pkey, pcfg))
        remove_mcp_proposal(self.root, target)

        # 提案已清理，不再展示
        self.assertEqual(list_mcp_proposals(self.root), [])
        # agent 用相同参数重试 dev_mcp_add -> 放行落盘
        ok = _parse(dev_mcp_add(arg))
        self.assertTrue(ok["ok"], ok)
        cfg = mcp_client.get_server_config(self.root, "easyeda-pro")
        self.assertEqual(cfg["args"], ["-y", "@vlabsoft/easyeda-pro-mcp"])

    def test_add_short_circuits_when_already_installed_by_wizard(self):
        # 回归：自动连接向导落盘（mcp_autoconnect_confirm_ep 只 save_server、
        # 不写 mcp_server 审批行）后，agent 用完全相同参数重试 dev_mcp_add
        # 不该永远 blocked —— 相同 target 已在盘上=用户已装配，直接 ok。
        arg = ("key: easyeda-pro\nlabel: EasyEDA Pro MCP\ntransport: stdio\n"
               "command: npx\nargs: -y @vlabsoft/easyeda-pro-mcp\n")
        # 模拟向导：直接落盘，全程不碰审批台账
        mcp_client.save_server(self.root, "easyeda-pro", {
            "transport": "stdio", "command": "npx",
            "args": ["-y", "@vlabsoft/easyeda-pro-mcp"], "env": {}, "enabled": True,
        })
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".docmind_approvals.jsonl")))

        r = _parse(dev_mcp_add(arg))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["action"], "already_installed")
        self.assertEqual(r["server"], "easyeda-pro")
        self.assertNotIn("blocked", r)
        # 短路不写审批，也不落新配置（盘上仍是向导那份）
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".docmind_approvals.jsonl")))

        # 同 key 换 URL（不同 target）仍须走审批门，短路不误放行
        a2 = ("key: easyeda-pro\ntransport: stdio\n"
              "command: npx\nargs: -y @other/pkg\n")
        blocked2 = _parse(dev_mcp_add(a2))
        self.assertTrue(blocked2["blocked"], blocked2)

    def test_add_target_rebound_when_command_changes(self):
        a1 = "key: svc\ntransport: http\nurl: http://127.0.0.1:9000/mcp\n"
        blocked1 = _parse(dev_mcp_add(a1))
        self._approve("mcp_server", blocked1["target"])
        self.assertTrue(_parse(dev_mcp_add(a1))["ok"])
        # 换 URL 后旧 target 不匹配，必须重新审批（防止审批被换地址复用）
        a2 = "key: svc\ntransport: http\nurl: http://127.0.0.1:9999/mcp\n"
        blocked2 = _parse(dev_mcp_add(a2))
        self.assertTrue(blocked2["blocked"])
        self.assertNotEqual(blocked1["target"], blocked2["target"])

    def test_add_validates_inputs(self):
        self.assertFalse(_parse(dev_mcp_add("key: bad key!"))["ok"])
        self.assertFalse(_parse(dev_mcp_add("key: x\ntransport: stdio"))["ok"])
        self.assertFalse(_parse(dev_mcp_add("key: x\ntransport: http\nurl: ftp://x"))["ok"])
        bad_args = _parse(dev_mcp_add("key: x\ncommand: uvx\nargs_json: not-json"))
        self.assertFalse(bad_args["ok"])

    def test_args_json_takes_precedence(self):
        arg = ('key: js\ntransport: stdio\ncommand: uvx\n'
               'args: ignored words\nargs_json: ["pkg", "--flag", "a b"]\n')
        blocked = _parse(dev_mcp_add(arg))
        self._approve("mcp_server", blocked["target"])
        _parse(dev_mcp_add(arg))
        cfg = mcp_client.get_server_config(self.root, "js")
        self.assertEqual(cfg["args"], ["pkg", "--flag", "a b"])

    # ---- probe -------------------------------------------------------------
    def test_probe_unknown_server_fails_cleanly(self):
        r = _parse(dev_mcp_probe("missing"))
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_probe_returns_tool_names(self):
        # 预置一个已保存的 stdio 服务器，桩掉真正的会话
        mcp_client.save_server(self.root, "demo", {
            "transport": "stdio", "command": "uvx", "args": ["demo-mcp"], "enabled": True})
        with patch("mcp_client._session_for") as _sess:
            _sess.return_value.request.return_value = {
                "tools": [{"name": "ping"}, {"name": "read"}]}
            r = _parse(dev_mcp_probe("demo"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool_count"], 2)
        self.assertEqual(r["tools"], ["ping", "read"])

    # ---- discover + decide --------------------------------------------------
    def test_discover_then_approve_gates_routing(self):
        mcp_client.save_server(self.root, "eda-x", {
            "transport": "stdio", "command": "uvx", "args": ["eda"], "enabled": True})
        tools = [{"name": "run_drc", "description": "run pcb drc check",
                  "input_schema": {"type": "object", "properties": {}}}]
        with patch("mcp_client.list_tools", return_value={"ok": True, "tools": tools}):
            d = _parse(dev_mcp_discover("eda-x"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["candidate"]["status"], "pending")
        self.assertIn("drc", d["candidate"]["capabilities"])
        self.assertEqual(mcp_capabilities.active_for(self.root, "eda-x"), {})

        # 未审批直接 approve 被门禁拦截
        blocked = _parse(dev_mcp_decide("decision: approve\nkey: eda-x"))
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["action"], "mcp_capability")
        self._approve("mcp_capability", "eda-x")
        decided = _parse(dev_mcp_decide("decision: approve\nkey: eda-x"))
        self.assertTrue(decided["ok"])
        self.assertTrue(decided["routing_enabled"])
        active = mcp_capabilities.active_for(self.root, "eda-x")
        self.assertEqual(active["status"], "active")

    def test_decide_reject_needs_no_approval(self):
        mcp_client.save_server(self.root, "rj", {
            "transport": "stdio", "command": "uvx", "args": ["rj"], "enabled": True})
        tools = [{"name": "x", "description": "generic helper",
                  "input_schema": {"type": "object", "properties": {}}}]
        with patch("mcp_client.list_tools", return_value={"ok": True, "tools": tools}):
            _parse(dev_mcp_discover("rj"))
        r = _parse(dev_mcp_decide("decision: reject\nkey: rj"))
        self.assertTrue(r["ok"])
        self.assertFalse(r["routing_enabled"])
        self.assertEqual(mcp_capabilities.active_for(self.root, "rj"), {})

    # ---- remove -------------------------------------------------------------
    def test_remove_blocked_then_approved(self):
        mcp_client.save_server(self.root, "gone", {
            "transport": "stdio", "command": "uvx", "args": ["gone"], "enabled": True})
        blocked = _parse(dev_mcp_remove("gone"))
        self.assertTrue(blocked["blocked"])
        self._approve("mcp_server", "gone")
        with patch("mcp_client.close_server"):
            r = _parse(dev_mcp_remove("gone"))
        self.assertTrue(r["ok"])
        self.assertNotIn("gone", mcp_client.load_user_servers(self.root))

    # ---- 无代码库 ------------------------------------------------------------
    def test_tools_require_code_root(self):
        import tools as tools_mod
        config._RUNTIME.pop("code_root", None)
        with patch.object(tools_mod, "CODE_ROOT", ""):
            outs = (dev_mcp_search("EDA"), dev_mcp_add("key: x"),
                    dev_mcp_probe("x"), dev_mcp_discover("x"),
                    dev_mcp_decide("decision: approve\nkey: x"), dev_mcp_remove("x"))
        for out in outs:
            self.assertIn("未配置代码库", out)


if __name__ == "__main__":
    unittest.main()
