import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent
import agent_eval
import tools
from agent_runtime.tools import ToolResult, execute_tool
from tests.test_agent import _ScriptedLLM, _act


class RuntimeContracts(unittest.TestCase):
    def test_auto_verification_uses_instance_checker(self):
        checker = unittest.mock.Mock(return_value=json.dumps({
            "ran": ["browser:layout", "browser:click"], "passed": True, "failures": []}))
        registry = {"create_file": {"func": lambda value: "已创建 " + value},
                    "self_verify": {"func": checker}}
        with patch.object(agent, "self_verify", side_effect=AssertionError("global checker must not run")):
            instance = agent.Agent(tool_registry=registry, llm=_ScriptedLLM([
                _act("create_file", "index.html"), "Final Answer: 已完成浏览器验证。" ]))
            events = list(instance.run("创建 index.html 页面"))
        checker.assert_called_once_with("scope: auto\nfiles: index.html")
        self.assertTrue(instance.last_turn_record["verified"])
        self.assertTrue(any(event.get("type") == "observation" and "browser:click" in event.get("text", "") for event in events))

    def test_instance_checker_failure_blocks_success_claim(self):
        registry = {"create_file": {"func": lambda value: "已创建 " + value},
                    "self_verify": {"func": lambda _: json.dumps({
                        "ran": ["browser:click"], "passed": False,
                        "failures": [{"scope": "browser", "file": "index.html", "error": "click did not work"}]})}}
        instance = agent.Agent(tool_registry=registry, llm=_ScriptedLLM([
            _act("create_file", "index.html"), "Final Answer: 所有功能都正常。" ]))
        events = list(instance.run("创建 index.html 页面"))
        self.assertFalse(instance.last_turn_record["verified"])
        final = next(event for event in events if event.get("type") == "final")
        self.assertEqual(final.get("verification_status"), "unverified")
        # 未验证终态必须带 degraded=True，否则前端两处「未完成」徽章（只认
        # m.degraded）会把它染成绿色「已完成」。
        self.assertTrue(final.get("degraded"))
        self.assertNotIn("所有功能都正常", final["text"])

    def test_successful_production_preview_closes_write_verification(self):
        registry = {
            "create_file": {"func": lambda value: "已创建 index.html"},
            "self_verify": {"func": lambda _: json.dumps({
                "ran": ["html:syntax"], "passed": True, "failures": []})},
            "preview_project": {"func": lambda _: ToolResult(
                True, "真实浏览器预览已完成",
                data={"images": []},
                artifacts=[{"id": "visual-preview", "kind": "image",
                            "path": ".docmind/visual-evidence/preview.png"}])},
        }
        instance = agent.Agent(tool_registry=registry, llm=_ScriptedLLM([
            _act("create_file", "path: index.html"),
            _act("preview_project", "entry: index.html"),
            "Final Answer: 已完成并通过真实画面验收。",
        ]))
        events = list(instance.run("创建网页并进行真实画面验收"))
        self.assertTrue(instance.last_turn_record["verified"])
        self.assertTrue(any(event.get("artifacts") for event in events))

    def test_structured_result_does_not_guess_status_from_text(self):
        result = ToolResult(True, "历史错误已修复")
        self.assertIs(execute_tool(lambda _: result, "", lambda _: True), result)

    def test_exception_does_not_escape_or_leak_message(self):
        def bad(_):
            raise RuntimeError("secret-key")
        result = execute_tool(bad, "", lambda _: False)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "exception")
        self.assertNotIn("secret-key", result.text)

    def test_sequential_exception_becomes_observation(self):
        def bad(_):
            raise RuntimeError("secret-key")
        with patch.object(agent, "TOOLS", {"lookup": {"func": bad}}):
            a = agent.Agent(llm=_ScriptedLLM([_act("lookup", "x"), "Final Answer: 查询失败。"] ))
            events = list(a.run("查询资料"))
        self.assertTrue(any(e["type"] == "observation" and "RuntimeError" in e["text"] for e in events))
        self.assertTrue(any(e["type"] == "final" for e in events))

    def test_failed_second_write_invalidates_success_and_final(self):
        registry = {"create_file": {"func": lambda value: "已创建 " + value}}
        responses = [json.dumps({"ran": ["compile:a.py"], "passed": True, "failures": []}),
                     json.dumps({"ran": [], "passed": False,
                                 "failures": [{"scope": "backend", "file": "b.py", "error": "syntax"}]})]
        with patch.object(agent, "TOOLS", registry), patch.object(agent, "self_verify", side_effect=responses):
            a = agent.Agent(llm=_ScriptedLLM([_act("create_file", "a.py"), _act("create_file", "b.py"),
                                            "Final Answer: 全部成功。"] ))
            events = list(a.run("请创建 a.py 和 b.py 文件"))
        final = next(e for e in events if e["type"] == "final")
        self.assertEqual(final.get("verification_status"), "unverified")
        self.assertTrue(final.get("degraded"))
        self.assertNotIn("全部成功", final["text"])
        self.assertFalse(a.last_turn_record["verified"])
        self.assertEqual(a.last_turn_record["verification"]["targets"], 2)
        self.assertEqual(a.history[-1]["assistant"], final["text"])

    def test_outside_verification_never_runs(self):
        with tempfile.TemporaryDirectory() as root, patch.object(tools, "_get_code_root", return_value=root), \
                patch.object(tools, "_sv_verify_backend") as checker:
            result = json.loads(tools.self_verify("files: ../outside.py"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failures"][0]["scope"], "permission")
        checker.assert_not_called()


class StrictEvaluation(unittest.TestCase):
    def test_prefix_is_not_tool_identity(self):
        self.assertFalse(agent_eval._action_called("read_file", ["read_file_unsafe(x)"]))

    def test_gate_rejects_missing_duplicate_unscored_and_broken_records(self):
        passing = {"id": "A", "final": "ok", "expect": {"must_include": ["ok"]}}
        with tempfile.TemporaryDirectory() as directory:
            baseline = os.path.join(directory, "base.jsonl")
            current = os.path.join(directory, "current.jsonl")
            with open(baseline, "w", encoding="utf-8") as f:
                f.write(json.dumps(passing) + "\n" + json.dumps(dict(passing, id="B")) + "\n")
            variants = [[passing], [passing, passing], [passing, {"id": "C"}], [passing, "broken"]]
            for rows in variants:
                with self.subTest(rows=rows):
                    with open(current, "w", encoding="utf-8") as f:
                        for row in rows:
                            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
                    self.assertFalse(agent_eval.golden_gate_ok(current, baseline))
