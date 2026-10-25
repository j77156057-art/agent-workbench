# -*- coding: utf-8 -*-
"""Agent 自主生成项目预览适配器的工具契约测试。"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
import tools
from agent_runtime.context_router import ContextRouter
from agent_runtime.preview_adapters import build_preview_bundle
from agent_runtime.tools import ToolResult


class PreviewAdapterToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docmind_preview_adapter_")
        self.root = self.tmp.name
        self.old_root = config.get_runtime("code_root")
        config.set_runtime("code_root", self.root)

    def tearDown(self):
        if self.old_root:
            config.set_runtime("code_root", self.old_root)
        else:
            config._RUNTIME.pop("code_root", None)
        self.tmp.cleanup()

    def test_create_writes_project_scoped_pending_draft(self):
        result = json.loads(tools.dev_preview_adapter_create(
            "id: cad-scene\nlabel: CAD 场景\ndomain: cad\n"
            "artifact_kinds: [model, image]\nrefresh_tool: cad.capture\n"
            "validation: [检查模型数量, 检查截图来源]"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["manifest"]["status"], "pending")
        path = os.path.join(self.root, ".docmind", "preview-adapters", "cad-scene.json")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(json.loads(open(path, encoding="utf-8").read())["status"], "pending")

    def test_create_requires_project(self):
        with patch.object(tools, "_get_code_root", return_value=""):
            result = json.loads(tools.dev_preview_adapter_create("id: cad"))
        self.assertFalse(result["ok"])
        self.assertIn("未配置当前项目", result["error"])

    def test_approve_rejects_invalid_decision_and_then_activates(self):
        created = json.loads(tools.dev_preview_adapter_create("id: cad\ndomain: cad"))
        self.assertTrue(created["ok"])
        invalid = json.loads(tools.dev_preview_adapter_approve("id: cad\ndecision: maybe"))
        self.assertFalse(invalid["ok"])
        approved = json.loads(tools.dev_preview_adapter_approve("id: cad\ndecision: approve"))
        self.assertTrue(approved["ok"])
        self.assertEqual(approved["manifest"]["status"], "active")

    def test_generated_manifest_only_enriches_metadata_without_executing_code(self):
        marker = os.path.join(self.root, "should-not-exist")
        tools.dev_preview_adapter_create(
            "id: hostile\ndomain: hostile\n"
            f"capture_adapter: python:open('{marker}','w')")
        tools.dev_preview_adapter_approve("id: hostile\ndecision: approve")
        bundle = build_preview_bundle({
            "project_root": self.root,
            "status": "completed",
            "results": {"results": {"task": {"artifacts": [
                {"id": "x", "adapter": "hostile", "kind": "text", "path": "out.txt"}
            ]}}},
        })
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(bundle["artifacts"][0]["metadata"]["generated_adapter"], "true")

    def test_adapter_intent_routes_developer_tools_and_documents_flow(self):
        plan = ContextRouter().route("请为 CAD 软件构建一个预览适配器", code_root=self.root)
        self.assertIn("developer", plan.tool_groups)
        joined = "\n".join(plan.messages)
        self.assertIn("dev_preview_adapter_create", joined)
        self.assertIn("dev_preview_adapter_approve", joined)

    def test_refresh_requires_active_and_rejects_arbitrary_code(self):
        tools.dev_preview_adapter_create("id: cad\nrefresh_tool: python:open('x','w')")
        pending = json.loads(tools.dev_preview_adapter_refresh("id: cad"))
        self.assertFalse(pending["ok"])
        tools.dev_preview_adapter_approve("id: cad\ndecision: approve")
        rejected = json.loads(tools.dev_preview_adapter_refresh("id: cad"))
        self.assertFalse(rejected["ok"])
        self.assertIn("refresh_tool", rejected["error"])

    def test_refresh_reuses_allowlisted_builtin_tool(self):
        tools.dev_preview_adapter_create("id: cad\nrefresh_tool: builtin:game_screenshot")
        tools.dev_preview_adapter_approve("id: cad\ndecision: approve")

        def fake_capture(_arg):
            return "captured"

        with patch.dict(tools.TOOLS, {"game_screenshot": {"func": fake_capture}}):
            result = json.loads(tools.dev_preview_adapter_refresh(
                "id: cad\narguments: {}"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool"], "game_screenshot")
        self.assertEqual(result["result"], "captured")

    def test_refresh_forwards_mcp_through_existing_boundary(self):
        tools.dev_preview_adapter_create("id: cad\nrefresh_tool: mcp:easyeda/capture_view")
        tools.dev_preview_adapter_approve("id: cad\ndecision: approve")
        with patch.object(tools, "dev_mcp_call", return_value='{"ok": true}') as call:
            result = json.loads(tools.dev_preview_adapter_refresh(
                'id: cad\narguments: {"scene":"main"}'))
        self.assertTrue(result["ok"])
        call.assert_called_once_with(
            'key: easyeda\nname: capture_view\narguments: {"scene":"main"}')

    def test_desktop_capture_marks_native_visual_evidence(self):
        source = ToolResult(
            ok=True, text="captured", data={"images": ["image"]},
            artifacts=[{"id": "shot", "adapter": "game", "kind": "image"}],
        )
        with patch.object(tools, "game_screenshot", return_value=source):
            result = tools.dev_desktop_capture("target: foreground")
        self.assertTrue(result.ok)
        self.assertEqual(result.artifacts[0]["adapter"], "native")
        self.assertEqual(result.artifacts[0]["metadata"]["capture_adapter"], "desktop_computer_use")

    def test_desktop_action_requires_project_and_rejects_invalid_inputs(self):
        with patch.object(tools, "_get_code_root", return_value=""):
            result = json.loads(tools.dev_desktop_action("action: click\ntarget: embedded\nx: 1\ny: 2"))
        self.assertFalse(result["ok"])
        self.assertIn("未配置当前项目", result["error"])

        invalid_action = json.loads(tools.dev_desktop_action("action: shell\ntarget: embedded"))
        self.assertFalse(invalid_action["ok"])
        invalid_target = json.loads(tools.dev_desktop_action("action: click\ntarget: hwnd\nx: 1\ny: 2"))
        self.assertFalse(invalid_target["ok"])

    def test_desktop_action_executes_directly_and_never_echoes_text(self):
        """「留地板、拆其余」：可逆的桌面点击/输入不再逐次弹审批卡，直接执行。

        幸存的地板是隐私——送进输入层的文本绝不能回显到工具结果里。逐次审批门
        （gate）已被拆成死代码，这里断言的是当前真实行为。
        """
        import desktop_actions
        sent = {}

        def fake_perform(action, **kw):
            sent["action"] = action
            sent["text"] = kw.get("text", "")
            return {"ok": True, "action": action, "target": kw.get("target"),
                    "events": 2, "window": {"width": 200, "height": 100}, "error": ""}

        with patch.object(desktop_actions, "perform", side_effect=fake_perform):
            result = json.loads(tools.dev_desktop_action(
                "action: type\ntarget: embedded\ntext: super secret"))

        self.assertTrue(result["ok"])
        self.assertEqual(sent["action"], "type")
        # 文本确实原样送到了输入层……
        self.assertEqual(sent["text"], "super secret")
        # ……但绝不出现在返回给调用方/日志的结果里。
        self.assertNotIn("super secret", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("blocked", result, "逐次审批卡已拆，桌面可逆动作不再有 blocked 门")

    def test_desktop_action_save_maps_to_control_s(self):
        import desktop_actions
        with patch("desktop_actions.perform", return_value={"ok": True}) as perform:
            result = json.loads(tools.dev_desktop_action("action: save\ntarget: foreground"))
        self.assertTrue(result["ok"])
        # save 直接透传到 perform（无逐次审批卡），由 perform 内部映射 Control_L+s
        self.assertEqual(perform.call_args.args[0], "save")
        self.assertEqual(perform.call_args.kwargs["key"], "")
        with patch.object(desktop_actions, "_resolve_target", return_value=(1, {
                "origin": {"x": 0, "y": 0}, "rect": {"width": 10, "height": 10}}, "")), \
             patch.object(desktop_actions, "_focus", return_value=(True, "")), \
             patch.object(desktop_actions, "_press_key", return_value=(True, 1)) as press:
            direct = desktop_actions.perform("save", target="foreground")
        self.assertTrue(direct["ok"])
        press.assert_called_once_with("Control_L+s")

    def test_desktop_action_does_not_fake_success_when_bridge_fails(self):
        with patch("desktop_actions.perform",
                   return_value={"ok": False, "error": "Windows 未确认输入事件"}):
            result = json.loads(tools.dev_desktop_action(
                "action: click\ntarget: embedded\nx: 4\ny: 5"))
        self.assertFalse(result["ok"])
        self.assertIn("Windows", result["error"])


if __name__ == "__main__":
    unittest.main()
