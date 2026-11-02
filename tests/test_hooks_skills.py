# -*- coding: utf-8 -*-
"""hooks 热插拔与 skill 热插拔（离线）。"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_trace
import hooks
import sessions
import skills


class _SeqLLM:
    provider = "fake"
    model = "seq"

    def __init__(self, scripts):
        self.scripts = scripts
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, **kw):
        s = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 3, "completion_tokens": 1}
        self.last_tool_calls = []
        if stream:
            def g():
                for ch in s:
                    yield ch
            return g()
        return s

    def count_tokens(self, text):
        return max(1, len(text) // 3)


class _HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_hook_")
        self._old = hooks.HOOKS_DIR
        hooks.HOOKS_DIR = self.tmp

    def tearDown(self):
        hooks.HOOKS_DIR = self._old
        hooks.reload()

    def _write(self, name, code):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(code)


class HookRegistryTests(_HookBase):
    def test_fine_grained_workflow_hooks_cover_tools_mcp_and_subagents(self):
        self._write("lifecycle.py", (
            "def before_tool(payload):\n"
            "    return {'block': payload.get('tool') == 'blocked', 'reason': 'manual'}\n"
            "def before_mcp(payload):\n"
            "    return {'block': True, 'reason': 'mcp review'}\n"
            "def after_subagent(payload):\n"
            "    return None\n"
        ))
        hooks.reload()
        self.assertTrue(hooks.list_hooks()["workflow_counts"]["before_mcp"])
        self.assertTrue(hooks.run_workflow("before_tool", {"tool": "blocked"})["blocked"])
        self.assertTrue(hooks.run_workflow("before_mcp", {})["blocked"])

    def test_all_kinds_registered_and_run(self):
        self._write("h.py", (
            "def pre_tool(name, arg):\n"
            "    return {'arg': arg + ' [h1]'}\n"
            "def post_tool(name, arg, obs):\n"
            "    return {'obs': obs + ' <post>'}\n"
            "def pre_turn(q):\n"
            "    return {'question': q + ' [t]'}\n"
            "def post_turn(record):\n"
            "    record['seen'] = True\n"
        ))
        r = hooks.reload()
        self.assertEqual(r["loaded"], 1)
        self.assertEqual(hooks.list_hooks()["counts"],
                         {"pre_tool": 1, "post_tool": 1, "pre_turn": 1, "post_turn": 1})
        self.assertEqual(hooks.run_pre_tool("t", "x"), (False, "", "x [h1]"))
        self.assertEqual(hooks.run_post_tool("t", "x", "OBS"), "OBS <post>")
        self.assertEqual(hooks.run_pre_turn("Q"), (False, "", "Q [t]"))

    def test_pre_tool_block(self):
        self._write("b.py", (
            "def pre_tool(name, arg):\n"
            "    if name == 'danger':\n"
            "        return {'block': True, 'reason': 'nope'}\n"
        ))
        hooks.reload()
        blocked, reason, arg = hooks.run_pre_tool("danger", "x")
        self.assertTrue(blocked)
        self.assertEqual(reason, "nope")

    def test_broken_hook_is_isolated(self):
        self._write("ok.py", "def pre_tool(n, a):\n    return {'arg': a + ' ok'}\n")
        self._write("bad.py", "raise RuntimeError('boom')\n")
        r = hooks.reload()
        self.assertEqual(r["loaded"], 1)
        self.assertTrue(any("boom" in e["error"] for e in r["errors"]))
        # 好钩子仍然生效
        self.assertEqual(hooks.run_pre_tool("t", "x")[2], "x ok")

    def test_hook_exception_does_not_break_run(self):
        self._write("boom.py", "def pre_tool(n, a):\n    raise ValueError('kaboom')\n")
        hooks.reload()
        self.assertEqual(hooks.run_pre_tool("t", "x"), (False, "", "x"))

    def test_missing_dir_is_noop(self):
        hooks.HOOKS_DIR = os.path.join(self.tmp, "nope")
        self.assertEqual(hooks.reload()["loaded"], 0)
        self.assertFalse(hooks.has_any())

    def test_declarative_breakpoint_persists_and_matches_payload(self):
        hooks.reload()
        saved = hooks.set_breakpoint("before_mcp", match="godot", reason="引擎调用审核")
        self.assertTrue(saved["block"])
        hooks.reload()
        self.assertTrue(hooks.list_hooks()["breakpoints"]["before_mcp"]["enabled"])
        self.assertFalse(hooks.run_workflow("before_mcp", {"connector": "web"})["blocked"])
        blocked = hooks.run_workflow("before_mcp", {"connector": "godot"})
        self.assertTrue(blocked["blocked"])
        self.assertIn("引擎调用审核", blocked["reason"])
        self.assertTrue(hooks.remove_breakpoint("before_mcp")["removed"])

    def test_workflow_event_scope_captures_only_while_active(self):
        hooks.reload()
        events = []
        sink = lambda kind, payload, result: events.append((kind, payload, result))
        with hooks.workflow_event_scope(sink):
            hooks.run_workflow("before_tool", {"tool": "calculate"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "before_tool")
        self.assertEqual(events[0][1]["tool"], "calculate")
        self.assertFalse(events[0][2]["blocked"])
        hooks.run_workflow("after_tool", {"tool": "calculate"})
        self.assertEqual(len(events), 1)

    def test_workflow_event_observation_failure_does_not_break_workflow(self):
        self._write("broken.py", (
            "def before_tool(payload):\n"
            "    raise RuntimeError('observe failed')\n"
        ))
        hooks.reload()
        with hooks.workflow_event_scope(
                lambda kind, payload, result: (_ for _ in ()).throw(RuntimeError("sink failed"))):
            result = hooks.run_workflow("before_tool", {"tool": "calculate"})
        self.assertFalse(result["blocked"])
        self.assertEqual(len(result["errors"]), 1)

        captured = []
        with hooks.workflow_event_scope(lambda kind, payload, result: captured.append(result)):
            result = hooks.run_workflow("before_tool", {"tool": "calculate"})
        self.assertFalse(result["blocked"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["errors"][0]["hook"], "before_tool")


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_skill_")
        self._old = skills.SKILLS_DIR
        self._old_builtin = skills.BUILTIN_SKILLS_DIR
        skills.SKILLS_DIR = self.tmp
        skills.BUILTIN_SKILLS_DIR = os.path.join(self.tmp, "builtin")

    def tearDown(self):
        skills.SKILLS_DIR = self._old
        skills.BUILTIN_SKILLS_DIR = self._old_builtin
        skills.reload()

    def test_loads_frontmatter_and_plain_markdown(self):
        os.makedirs(os.path.join(self.tmp, "audio"))
        with open(os.path.join(self.tmp, "audio", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: 音频工程\ndescription: 混音与总线\nwhen_to_use: 问音频时\n---\n正文A")
        with open(os.path.join(self.tmp, "net.md"), "w", encoding="utf-8") as f:
            f.write("# 联网研究\n正文B")
        r = skills.reload()
        self.assertEqual(r["loaded"], 2)
        self.assertIn("音频工程", skills.use_skill("音频工程"))
        self.assertIn("正文B", skills.use_skill("net"))

    def test_catalog_lists_without_body(self):
        with open(os.path.join(self.tmp, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: S\ndescription: D\nwhen_to_use: W\n---\nSECRETBODY")
        skills.reload()
        cat = skills.catalog_text()
        self.assertIn("S", cat)
        self.assertNotIn("SECRETBODY", cat)

    def test_use_skill_accepts_name_prefix(self):
        with open(os.path.join(self.tmp, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: S\ndescription: D\n---\nBODY")
        skills.reload()
        self.assertIn("BODY", skills.use_skill("name: S"))

    def test_missing_skill_reports_available(self):
        with open(os.path.join(self.tmp, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: S\ndescription: D\n---\nB")
        skills.reload()
        out = skills.use_skill("不存在")
        self.assertIn("未找到技能", out)
        self.assertIn("S", out)

    def test_hot_reload_picks_up_new_skill(self):
        skills.reload()
        self.assertEqual(skills.list_skills()["count"], 0)
        with open(os.path.join(self.tmp, "new.md"), "w", encoding="utf-8") as f:
            f.write("# 新技能\n内容")
        skills.reload()
        self.assertEqual(skills.list_skills()["count"], 1)

    def test_user_skill_overrides_builtin_and_references_are_ignored(self):
        builtin = os.path.join(skills.BUILTIN_SKILLS_DIR, "documents")
        os.makedirs(builtin)
        with open(os.path.join(builtin, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: documents\ndescription: built in\n---\nBUILTIN")
        with open(os.path.join(builtin, "reference.md"), "w", encoding="utf-8") as f:
            f.write("# Must not become a skill")
        with open(os.path.join(skills.SKILLS_DIR, "documents.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: documents\ndescription: custom\n---\nCUSTOM")
        result = skills.reload()
        self.assertEqual(result["loaded"], 1)
        self.assertIn("CUSTOM", skills.use_skill("documents"))
        self.assertEqual(skills.list_skills()["items"][0]["source"], "user")

    def test_skill_version_conflict_stats_and_rollback(self):
        os.makedirs(skills.BUILTIN_SKILLS_DIR)
        with open(os.path.join(skills.BUILTIN_SKILLS_DIR, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: S\nversion: 1.0.0\n---\nBUILTIN")
        with open(os.path.join(skills.SKILLS_DIR, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: S\nversion: 2.0.0\n---\nUSER")
        skills.reload()
        self.assertTrue(skills.list_skills()["conflicts"])
        skills.use_skill("S")
        skills.record_result("S", True, score=1.0)
        stats = skills.skill_statistics("S")
        self.assertEqual(stats["uses"], 1)
        self.assertEqual(stats["successes"], 1)
        self.assertFalse(skills.skill_regression("S", baseline_success_rate=0.9)["regressed"])
        skills.record_result("S", False, score=0.0)
        regression = skills.skill_regression("S", baseline_success_rate=0.9)
        self.assertTrue(regression["scored"])
        self.assertTrue(regression["regressed"])
        saved = skills.save_user_skill("new-skill", "desc", "body", version="3.0.0")
        self.assertEqual(saved["version"], "3.0.0")
        self.assertTrue(skills.rollback_user_skill("new-skill")["rolled_back"])

    def test_skill_update_preserves_history_and_rollback_restores_previous(self):
        skills.save_user_skill("versioned", "desc", "旧正文", version="1.0.0")
        updated = skills.update_user_skill("versioned", "desc2", "新正文", version="2.0.0")
        self.assertTrue(updated["history_saved"])
        listed = next(item for item in skills.list_skills()["items"] if item["name"] == "versioned")
        self.assertEqual(listed["version"], "2.0.0")
        self.assertEqual(listed["history_versions"][0]["version"], "1.0.0")
        self.assertIn("新正文", skills.use_skill("versioned"))
        rolled = skills.rollback_user_skill("versioned")
        self.assertTrue(rolled["restored_previous"])
        self.assertIn("旧正文", skills.use_skill("versioned"))
        self.assertEqual(skills.list_skills()["count"], 1)

    def test_skill_update_failure_restores_previous_file(self):
        skills.save_user_skill("recoverable", "desc", "旧正文", version="1.0.0")
        with patch.object(skills, "save_user_skill", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                skills.update_user_skill("recoverable", "desc", "新正文", version="2.0.0")
        skills.reload()
        self.assertIn("旧正文", skills.use_skill("recoverable"))


class AgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_hs_")
        self._hooks_dir = hooks.HOOKS_DIR
        self._skills_dir = skills.SKILLS_DIR
        self._builtin_skills_dir = skills.BUILTIN_SKILLS_DIR
        self._trace = agent_trace.TRACE_FILE
        self._sess = sessions.SESSIONS_DIR
        hooks.HOOKS_DIR = os.path.join(self.tmp, "hooks")
        skills.SKILLS_DIR = os.path.join(self.tmp, "skills")
        skills.BUILTIN_SKILLS_DIR = os.path.join(self.tmp, "builtin-skills")
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "t.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "s")
        os.makedirs(hooks.HOOKS_DIR)
        os.makedirs(skills.SKILLS_DIR)

    def tearDown(self):
        hooks.HOOKS_DIR = self._hooks_dir
        skills.SKILLS_DIR = self._skills_dir
        skills.BUILTIN_SKILLS_DIR = self._builtin_skills_dir
        agent_trace.TRACE_FILE = self._trace
        sessions.SESSIONS_DIR = self._sess
        hooks.reload()
        skills.reload()

    def test_skill_catalog_injected_into_prompt(self):
        with open(os.path.join(skills.SKILLS_DIR, "s.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: 音频工程\ndescription: 混音\n---\nBODY")
        skills.reload()
        a = agent_mod.Agent(llm=_SeqLLM([]))
        msgs = a._build_messages("q")
        self.assertTrue(any("可用技能" in (m.get("content") or "") for m in msgs))

    def test_pre_tool_hook_rewrites_arg_in_real_run(self):
        with open(os.path.join(hooks.HOOKS_DIR, "h.py"), "w", encoding="utf-8") as f:
            f.write("def pre_tool(name, arg):\n    return {'arg': '2+2'}\n")
        hooks.reload()
        llm = _SeqLLM(["Action: calculate\nAction Input: 1+1", "Final Answer: 完成"])
        a = agent_mod.Agent(llm=llm)
        events = list(a.run("算", stream=True))
        obs = " ".join(e["text"] for e in events if e["type"] == "observation")
        self.assertIn("4", obs, obs)   # 参数被钩子改写成 2+2

    def test_pre_tool_hook_blocks_execution(self):
        with open(os.path.join(hooks.HOOKS_DIR, "h.py"), "w", encoding="utf-8") as f:
            f.write("def pre_tool(name, arg):\n    return {'block': True, 'reason': '出于安全'}\n")
        hooks.reload()
        llm = _SeqLLM(["Action: calculate\nAction Input: 1+1", "Final Answer: 完成"])
        a = agent_mod.Agent(llm=llm)
        events = list(a.run("算", stream=True))
        obs = " ".join(e["text"] for e in events if e["type"] == "observation")
        self.assertIn("[钩子拦截]", obs)
        self.assertIn("出于安全", obs)

    def test_pre_turn_hook_can_block_turn(self):
        with open(os.path.join(hooks.HOOKS_DIR, "h.py"), "w", encoding="utf-8") as f:
            f.write("def pre_turn(q):\n    return {'block': True, 'reason': '维护中'}\n")
        hooks.reload()
        a = agent_mod.Agent(llm=_SeqLLM(["Final Answer: 不该出现"]))
        events = list(a.run("q", stream=True))
        self.assertEqual(events[-1]["type"], "final")
        self.assertIn("维护中", events[-1]["text"])
        self.assertEqual(agent_trace.recent(1)[0]["outcome"], "hook_blocked")

    def test_network_timeout_hook_blocks_sequential_tool(self):
        with open(os.path.join(hooks.HOOKS_DIR, "timeout.py"), "w", encoding="utf-8") as f:
            f.write(
                "def network_timeout(payload):\n"
                "    if payload.get('error_kind') == 'timeout':\n"
                "        return {'block': True, 'reason': '网络超时需人工审核'}\n"
            )
        hooks.reload()

        def timeout_probe(_arg):
            raise TimeoutError("upstream timed out")

        llm = _SeqLLM([
            "Action: web_timeout_probe\nAction Input: ping",
            "Final Answer: 已记录",
        ])
        agent = agent_mod.Agent(
            llm=llm,
            tool_registry={"web_timeout_probe": {
                "description": "test network probe", "func": timeout_probe,
            }},
        )
        events = list(agent.run("检查网络", stream=True, web_enabled=True))
        observations = " ".join(e.get("text", "") for e in events if e["type"] == "observation")
        self.assertIn("网络超时需人工审核", observations)
        self.assertTrue(any(e["type"] == "reflection" for e in events))


if __name__ == "__main__":
    unittest.main()
