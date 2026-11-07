import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_runtime.enterprise_sandbox import (
    SandboxUnavailable,
    enforce_file_write_boundary,
    enforce_mcp_boundary,
    execute_desktop_action,
)


class FileWriteBoundaryTests(unittest.TestCase):
    def test_safe_mode_allows_project_files_but_rejects_escape_and_secrets(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_external_access_mode", return_value="safe"):
            enforce_file_write_boundary(project_root=root, target=str(Path(root, "src", "main.py")))
            with self.assertRaises(SandboxUnavailable):
                enforce_file_write_boundary(project_root=root, target=str(Path(root).parent / "outside.py"))
            with self.assertRaises(SandboxUnavailable):
                enforce_file_write_boundary(project_root=root, target=str(Path(root, ".env")))

    def test_high_mode_allows_external_but_never_secrets(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_external_access_mode", return_value="high"):
            enforce_file_write_boundary(project_root=root, target=str(Path(root).parent / "outside.py"))
            for name in ("private.key", "id_ed25519", "store.ppk", ".netrc"):
                with self.subTest(name=name), self.assertRaises(SandboxUnavailable):
                    enforce_file_write_boundary(project_root=root, target=str(Path(root).parent / name))

    def test_external_resolver_no_longer_requires_directory_whitelist(self):
        import tools
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXTERNAL_DIRS": ""}):
            outside = str(Path(root, "outside.txt"))
            with patch("tools.external_access_high", return_value=False):
                self.assertIn("仅项目内写", tools._resolve_external_path(outside)[1])
            with patch("tools.external_access_high", return_value=True):
                self.assertEqual(tools._resolve_external_path(outside), (outside, None))

    def test_local_mode_keeps_existing_external_write_policy(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            enforce_file_write_boundary(project_root=root, target=str(Path(root).parent / "outside.py"))

    def test_central_write_fails_before_creating_a_safe_mode_escape(self):
        import tools
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_external_access_mode", return_value="safe"):
            target = Path(root).parent / "enterprise-escape-probe.txt"
            self.addCleanup(lambda: target.unlink(missing_ok=True))
            with self.assertRaises(SandboxUnavailable):
                tools._write_project_file(str(target), "blocked", root=root, record=False)
            self.assertFalse(target.exists())

    def test_external_delete_uses_the_same_sensitive_file_boundary(self):
        import tools
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "project")
            root.mkdir()
            target = Path(temporary, ".env")
            target.write_text("not-a-real-secret", encoding="utf-8")
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                    patch("tools.external_access_high", return_value=True), \
                    patch("tools._get_code_root", return_value=str(root)), \
                    patch("config.get_external_access_mode", return_value="high"):
                result = tools.delete_external_file(f"path: {target}\nconfirm: yes")
            self.assertIn("拒绝执行", result)
            self.assertTrue(target.exists())


class McpBoundaryTests(unittest.TestCase):
    def test_declared_side_effects_and_fallback_are_closed(self):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}):
            for flags in ((True, False), (False, True)):
                with self.subTest(flags=flags), self.assertRaises(SandboxUnavailable):
                    enforce_mcp_boundary(side_effect=flags[0], allow_side_effect_fallback=flags[1])

    def test_static_read_and_unproven_results_are_distinguished(self):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_mcp_unproven_mode", return_value="allow"):
            read = enforce_mcp_boundary(side_effect=False, allow_side_effect_fallback=False,
                                        name="list_files", arguments={})
            unknown = enforce_mcp_boundary(side_effect=False, allow_side_effect_fallback=False,
                                           name="mutate_custom", arguments={})
            self.assertEqual(read["status"], "proven_read_only")
            self.assertEqual(unknown["status"], "unproven_read_only")

    def test_unproven_call_is_allowed_and_marked_in_result(self):
        import tools
        events = []

        def record_hook(kind, payload):
            events.append((kind, payload))
            return {"blocked": False}

        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_mcp_unproven_mode", return_value="allow"), \
                patch("tools.get_runtime", return_value=root), \
                patch("tools.mcp_client.get_server_config", return_value={"enabled": True}), \
                patch("tools.mcp_client.call_tool_with_fallback", return_value={"ok": True}) as call, \
                patch("hooks.run_workflow", side_effect=record_hook):
            result = json.loads(tools.dev_mcp_call("key: demo\nname: mutate_custom"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["boundary"]["status"], "unproven_read_only")
            self.assertTrue(dict(events)["after_mcp"]["ok"])
            call.assert_called_once()

    def test_large_payload_cannot_truncate_away_the_unproven_marker(self):
        import tools
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_mcp_unproven_mode", return_value="allow"), \
                patch("tools.get_runtime", return_value=root), \
                patch("tools.mcp_client.get_server_config", return_value={"enabled": True}), \
                patch("tools.mcp_client.call_tool_with_fallback", return_value="x" * 7000):
            result = json.loads(tools._dev_mcp_call_impl("key: demo\nname: mutate_custom"))
            self.assertEqual(result["boundary"]["status"], "unproven_read_only")

    def test_tool_dispatch_does_not_call_connector_after_declared_side_effect(self):
        import tools
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("tools.get_runtime", return_value=root), \
                patch("tools.mcp_client.get_server_config", return_value={"enabled": True}), \
                patch("tools.mcp_client.call_tool_with_fallback") as call:
            result = tools._dev_mcp_call_impl("key: demo\nname: write\nside_effect: true")
            self.assertIn("拒绝执行", result)
            call.assert_not_called()

    def test_unproven_reject_mode_blocks_connector_and_hook_marks_failure(self):
        import tools
        events = []

        def record_hook(kind, payload):
            events.append((kind, payload))
            return {"blocked": False}

        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_mcp_unproven_mode", return_value="reject"), \
                patch("tools.get_runtime", return_value=root), \
                patch("tools.mcp_client.get_server_config", return_value={"enabled": True}), \
                patch("tools.mcp_client.call_tool_with_fallback") as call, \
                patch("hooks.run_workflow", side_effect=record_hook):
            result = tools.dev_mcp_call("key: demo\nname: mutate_custom")
            self.assertIn("拒绝执行", result)
            call.assert_not_called()
            after = dict(events)["after_mcp"]
            self.assertFalse(after["ok"])


class DesktopApprovalTests(unittest.TestCase):
    def _rows(self, root):
        import project_state
        path = project_state.path(root, "desktop_approvals.jsonl")
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]

    def test_user_mode_waits_for_approval_without_executing(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="user"), \
                patch("game_workbench.require_approval", return_value={"blocked": True}):
            perform = Mock(return_value={"ok": True})
            result = execute_desktop_action(root=root, channel="desktop", action="click",
                                            target="foreground", details={"x": 1}, executor=perform)
            self.assertTrue(result["blocked"])
            perform.assert_not_called()
            self.assertEqual(self._rows(root)[0]["decision"], "pending")

    def test_user_approval_and_model_auto_approval_both_leave_actor_reason_and_outcome(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}):
            with patch("config.get_desktop_approval_mode", return_value="user"), \
                    patch("game_workbench.require_approval", return_value=None):
                result = execute_desktop_action(root=root, channel="desktop", action="click",
                                                target="foreground", details={"x": 1},
                                                executor=lambda: {"ok": True})
                self.assertEqual(result["approval"]["actor"], "user:approval_queue")
            with patch("config.get_desktop_approval_mode", return_value="model"), \
                    patch("agent_runtime.enterprise_sandbox._model_desktop_decision",
                          return_value=(True, "仅点击项目内保存按钮", "model:test")):
                result = execute_desktop_action(root=root, channel="browser", action="click",
                                                target="public_browser", details={"selector": "#save"},
                                                executor=lambda: {"ok": True})
                self.assertEqual(result["approval"]["actor"], "model:test")
            rows = self._rows(root)
            self.assertEqual(rows[-2]["reason"], "仅点击项目内保存按钮")
            self.assertEqual(rows[-2]["details"]["selector"], "#save")
            self.assertTrue(rows[-1]["outcome"]["ok"])

    def test_model_rejection_is_audited_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="model"), \
                patch("agent_runtime.enterprise_sandbox._model_desktop_decision",
                      return_value=(False, "目标窗口不属于当前项目", "model:test")):
            perform = Mock(return_value={"ok": True})
            result = execute_desktop_action(root=root, channel="vision", action="click",
                                            target="foreground", details={"proposal_id": "p1"}, executor=perform)
            self.assertFalse(result["ok"])
            perform.assert_not_called()
            self.assertEqual(self._rows(root)[0]["decision"], "rejected")

    def test_mode_switch_is_persistent_and_conservative_by_default(self):
        import config
        with patch.dict(config._RUNTIME, {}, clear=True), patch("config.save_state"):
            self.assertEqual(config.get_desktop_approval_mode(), "strict")
            config.set_desktop_approval_mode("model")
            self.assertEqual(config.get_desktop_approval_mode(), "model")
            config.set_desktop_approval_mode("invalid")
            self.assertEqual(config.get_desktop_approval_mode(), "strict")
            self.assertEqual(config.get_mcp_unproven_mode(), "reject")
            config.set_mcp_unproven_mode("allow")
            self.assertEqual(config.get_mcp_unproven_mode(), "allow")
            config.set_mcp_unproven_mode("invalid")
            self.assertEqual(config.get_mcp_unproven_mode(), "reject")

    def test_strict_asks_for_every_physical_action_while_user_reuses_summary(self):
        import game_workbench
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}):
            outcomes = {}
            for mode in ("strict", "user"):
                root = Path(temporary, mode)
                root.mkdir()
                perform = Mock(return_value={"ok": True})
                with patch("config.get_desktop_approval_mode", return_value=mode):
                    first = execute_desktop_action(root=str(root), channel="desktop", action="click",
                                                   target="foreground", details={"x": 1}, executor=perform)
                    game_workbench.approval(str(root), "desktop_action", "pytest-user",
                                            approved=True, target=first["target"])
                    second = execute_desktop_action(root=str(root), channel="desktop", action="click",
                                                    target="foreground", details={"x": 1}, executor=perform)
                    third = execute_desktop_action(root=str(root), channel="desktop", action="click",
                                                   target="foreground", details={"x": 1}, executor=perform)
                outcomes[mode] = (first, second, third, perform.call_count)
            self.assertTrue(outcomes["strict"][0]["approval_required"])
            self.assertTrue(outcomes["strict"][1]["ok"])
            self.assertTrue(outcomes["strict"][2]["approval_required"])
            self.assertEqual(outcomes["strict"][3], 1)
            self.assertTrue(outcomes["user"][0]["approval_required"])
            self.assertTrue(outcomes["user"][1]["ok"])
            self.assertTrue(outcomes["user"][2]["ok"])
            self.assertEqual(outcomes["user"][3], 2)

    def test_legacy_state_defaults_without_rewriting_explicit_high(self):
        import config
        with tempfile.TemporaryDirectory() as root:
            state = Path(root, "old-state.json")
            state.write_text('{"external_access_mode":"high"}', encoding="utf-8")
            with patch("config.STATE_FILE", str(state)), patch.dict(config._RUNTIME, {}, clear=True):
                config._apply_persisted_state()
                self.assertEqual(config.get_desktop_approval_mode(), "strict")
                self.assertEqual(config.get_mcp_unproven_mode(), "reject")
                self.assertEqual(config.get_external_access_mode(), "high")

    def test_model_judge_uses_model_decision_and_reason(self):
        from agent_runtime.enterprise_sandbox import _model_desktop_decision
        client = Mock()
        client.chat.return_value = '{"approve":true,"reason":"动作只读取当前页面"}'
        with patch("llm.LLMClient", return_value=client), \
                patch("config.get_runtime", side_effect=lambda key: {"llm_provider": "test", "llm_model": "judge-v1"}.get(key)):
            approved, reason, actor = _model_desktop_decision({"action": "click"})
            self.assertTrue(approved)
            self.assertEqual(reason, "动作只读取当前页面")
            self.assertEqual(actor, "model:test/judge-v1")
            client.chat.return_value = '{"approve":"false","reason":"字符串不是布尔值"}'
            with self.assertRaises(SandboxUnavailable):
                _model_desktop_decision({"action": "click"})

    def test_model_service_failure_is_audited_with_actor_error_and_action(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="model"), \
                patch("config.get_runtime", side_effect=lambda key: {"llm_provider": "test", "llm_model": "judge-v1"}.get(key)), \
                patch("agent_runtime.enterprise_sandbox._model_desktop_decision",
                      side_effect=RuntimeError("approval service down")):
            perform = Mock(return_value={"ok": True})
            result = execute_desktop_action(root=root, channel="desktop", action="click",
                                            target="foreground", details={"x": 7}, executor=perform)
            self.assertFalse(result["ok"])
            perform.assert_not_called()
            row = self._rows(root)[0]
            self.assertEqual(row["decision"], "error")
            self.assertEqual(row["actor"], "model:test/judge-v1")
            self.assertEqual(row["error_kind"], "RuntimeError")
            self.assertEqual(row["details"], {"x": 7})
            self.assertTrue(row["time"])

    def test_audit_write_failure_never_executes_the_action(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="model"), \
                patch("agent_runtime.enterprise_sandbox._model_desktop_decision",
                      return_value=(True, "允许", "model:test/judge-v1")), \
                patch("agent_runtime.enterprise_sandbox._desktop_audit_write",
                      side_effect=OSError("audit disk full")):
            perform = Mock(return_value={"ok": True})
            with self.assertRaises(OSError):
                execute_desktop_action(root=root, channel="desktop", action="click",
                                       target="foreground", details={"x": 7}, executor=perform)
            perform.assert_not_called()

    def test_browser_sink_uses_the_same_boundary(self):
        from agent_runtime import browser_tool
        with patch("agent_runtime.browser_tool.execute_desktop_action", return_value={"ok": False, "error": "stop"}) as gate, \
                patch("agent_runtime.browser_tool._run_browser_action") as perform:
            result = browser_tool.run_browser_action("root", {"action": "click", "selector": "#save"})
            self.assertFalse(result["ok"])
            gate.assert_called_once()
            perform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
