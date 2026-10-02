# -*- coding: utf-8 -*-
"""T3：game_screenshot 工具与 screen_capture 模块（离线；抓帧全部注入替身）。"""
import base64
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import screen_capture  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime.tools import ToolResult, coerce_tool_spec  # noqa: E402
from config import set_runtime  # noqa: E402


def _png_b64(color=(0, 128, 255), size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _jpeg_bytes(color=(0, 128, 255), size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _bgra(width, height, pixel=(0, 0, 255, 255)):
    return bytes(pixel) * (width * height)


class ScreenCapturePureTests(unittest.TestCase):
    def test_zero_or_invalid_hwnd_is_none(self):
        self.assertIsNone(screen_capture.capture_window(0))

    def test_injected_grabber_none_propagates(self):
        self.assertIsNone(
            screen_capture.capture_window(123, frame_grabber=lambda hwnd: None))

    def test_injected_grabber_bytes_pass_through(self):
        raw = _bgra(2, 2)
        out = screen_capture.capture_window(
            123, frame_grabber=lambda hwnd: (raw, 2, 2))
        self.assertEqual(out, (raw, 2, 2))

    def test_encode_and_save_writes_jpeg_and_scales(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, ".docmind", "screenshots")
            raw = _bgra(2000, 100)
            saved = screen_capture.encode_and_save(raw, 2000, 100, out_dir)
            self.assertIsNotNone(saved)
            data_url, path, size = saved
            self.assertTrue(os.path.exists(path))
            self.assertTrue(path.endswith(".jpg"))
            with open(path, "rb") as fh:
                magic = fh.read(2)
            self.assertEqual(magic, b"\xff\xd8")  # JPEG SOI
            self.assertEqual(size, (1600, 80))
            self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
            decoded = base64.b64decode(data_url.split(",", 1)[1])
            self.assertEqual(decoded[:2], b"\xff\xd8")

    def test_encode_bad_bytes_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                screen_capture.encode_and_save(b"not-a-frame", 4, 4, tmp))

    def test_save_encoded_jpeg_round_trip(self):
        data_url = "data:image/jpeg;base64," + base64.b64encode(
            _jpeg_bytes()).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, ".docmind", "screenshots")
            path = screen_capture.save_encoded_jpeg(
                data_url, out_dir, tag="engine/capture")
            self.assertIsNotNone(path)
            self.assertTrue(os.path.exists(path))
            self.assertTrue(path.endswith(".jpg"))
            self.assertIn("enginecapture", path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(2), b"\xff\xd8")

    def test_save_encoded_jpeg_rejects_non_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                screen_capture.save_encoded_jpeg(
                    "data:image/png;base64,AAAA", tmp))
            self.assertIsNone(
                screen_capture.save_encoded_jpeg("not-a-data-url", tmp))


class EmbeddedTargetTests(unittest.TestCase):
    def test_picks_largest_alive_child_for_project(self):
        children = [
            {"hwnd": 1, "alive": True, "host": 10,
             "placed": {"width": 100, "height": 100}},
            {"hwnd": 2, "alive": True, "host": 10,
             "placed": {"width": 300, "height": 200}},
            {"hwnd": 3, "alive": False, "host": 10,
             "placed": {"width": 999, "height": 999}},
        ]
        with patch("desktop_bridge.embedded_children", return_value=children), \
                patch("desktop_bridge.host_hwnd", return_value=10):
            self.assertEqual(screen_capture._embedded_target(), 2)

    def test_no_children_returns_none(self):
        with patch("desktop_bridge.embedded_children", return_value=[]):
            self.assertIsNone(screen_capture._embedded_target())

    def test_project_id_filters_other_hosts_children(self):
        # 多宿主：别的项目有更大的子窗口，也不能截到它（review M-3）。
        children = [
            {"hwnd": 1, "alive": True, "host": 10,
             "placed": {"width": 300, "height": 200}},
            {"hwnd": 2, "alive": True, "host": 20,
             "placed": {"width": 999, "height": 999}},
        ]
        with patch("desktop_bridge.embedded_children", return_value=children), \
                patch("desktop_bridge.host_hwnd", lambda pid: 10 if pid == "prj-a" else None):
            self.assertEqual(screen_capture._embedded_target("prj-a"), 1)


class GameScreenshotToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_shot_")
        set_runtime("code_root", self.tmp)

    def tearDown(self):
        set_runtime("code_root", "")

    def test_mcp_source_short_circuits_window_grab(self):
        called = {"embedded": 0}

        def _embedded(*a, **k):
            called["embedded"] += 1
            return None

        # 与真实 _try_mcp_screenshot 一致：连接器原图经 _encode_observation_image
        # 重编码后一定是 JPEG data URL。
        mcp_hit = ("data:image/jpeg;base64,"
                   + base64.b64encode(_jpeg_bytes((1, 2, 3))).decode("ascii"),
                   "mcp:k/capture_screen")
        with patch.object(tools_mod, "_try_mcp_screenshot",
                          lambda root: mcp_hit), \
                patch.object(screen_capture, "grab_embedded", _embedded), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail("不应抓前台窗口")):
            result = tools_mod.game_screenshot("")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(result.data["images"], [mcp_hit[0]])
        # MCP 源也必须落盘，image_sources 给保存路径，文本同时保留连接器出处
        saved_path = result.data["image_sources"][0]
        self.assertTrue(os.path.exists(saved_path), saved_path)
        self.assertTrue(saved_path.endswith(".jpg"))
        self.assertIn(".docmind", saved_path)
        self.assertIn("mcp:k/capture_screen", result.text)
        self.assertIn(saved_path, result.text)
        self.assertEqual(called["embedded"], 0)
        self.assertEqual(result.artifacts[0]["adapter"], "game")
        self.assertEqual(result.artifacts[0]["path"], saved_path)

    def test_embedded_frame_saved_without_touching_foreground(self):
        frame = (_bgra(320, 240), 320, 240, 4321)
        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(screen_capture, "grab_embedded",
                             lambda *a, **k: frame), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail("embedded 命中不应抓前台")):
            result = tools_mod.game_screenshot("target: embedded")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(len(result.data["images"]), 1)
        path = result.data["image_sources"][0]
        self.assertTrue(os.path.exists(path))
        self.assertIn(".docmind", path)
        self.assertIn("screenshots", path)
        self.assertIn("320x240", result.text)
        self.assertEqual(result.artifacts[0]["metadata"]["capture_source"], "embedded")

    def test_falls_back_to_foreground(self):
        frame = (_bgra(64, 64), 64, 64, 111)
        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(screen_capture, "grab_embedded", lambda *a, **k: None), \
                patch.object(screen_capture, "grab_foreground", lambda *a, **k: frame):
            result = tools_mod.game_screenshot("")
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.data["image_sources"][0].endswith(".jpg"))
        self.assertIn("foreground", result.text)

    def test_foreground_target_skips_mcp_and_embedded(self):
        frame = (_bgra(32, 32), 32, 32, 7)
        with patch.object(tools_mod, "_try_mcp_screenshot",
                          lambda root: self.fail("foreground 目标不应探测 MCP")), \
                patch.object(screen_capture, "grab_embedded",
                             lambda *a, **k: self.fail("foreground 目标不应抓嵌入窗")), \
                patch.object(screen_capture, "grab_foreground", lambda *a, **k: frame):
            result = tools_mod.game_screenshot("target: foreground")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(len(result.data["images"]), 1)

    def test_embedded_passes_project_id_not_file_root(self):
        import projects
        seen = {}

        def _grab(pid=None, **kwargs):
            seen["pid"] = pid
            return None

        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(projects, "current_project_id",
                             return_value="prj-deadbeef123"), \
                patch.object(screen_capture, "grab_embedded", _grab), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: None):
            tools_mod.game_screenshot("")
        self.assertEqual(seen["pid"], "prj-deadbeef123")

    def test_no_window_returns_text_failure_without_data(self):
        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(screen_capture, "grab_embedded", lambda *a, **k: None), \
                patch.object(screen_capture, "grab_foreground", lambda *a, **k: None):
            result = tools_mod.game_screenshot("")
        self.assertIsInstance(result, str)
        self.assertIn("截图失败", result)


class McpScreenshotProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_mcp_shot_")
        set_runtime("code_root", self.tmp)

    def tearDown(self):
        set_runtime("code_root", "")

    def test_probe_uses_enabled_connector_image(self):
        directory = [{"key": "engine", "enabled": True}]
        listing = {"tools": [
            {"name": "list_scenes", "description": "列场景"},
            {"name": "capture_screen", "description": "截图当前画面"},
        ]}
        response = {"ok": True, "images": [
            {"data": _png_b64((1, 2, 3)), "mime_type": "image/png"}]}

        def _call(root, key, name, args, **kw):
            self.assertEqual((key, name), ("engine", "capture_screen"))
            return response

        with patch.object(tools_mod.mcp_client, "connector_directory",
                          return_value=directory), \
                patch.object(tools_mod.mcp_client, "list_tools",
                             return_value=listing), \
                patch.object(tools_mod.mcp_client, "call_tool_with_fallback",
                             side_effect=_call):
            hit = tools_mod._try_mcp_screenshot(self.tmp)
        self.assertIsNotNone(hit)
        data_url, source = hit
        self.assertEqual(source, "mcp:engine/capture_screen")
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

    def test_probe_all_failures_return_none(self):
        with patch.object(tools_mod.mcp_client, "connector_directory",
                          return_value=[]):
            self.assertIsNone(tools_mod._try_mcp_screenshot(self.tmp))

        directory = [{"key": "engine", "enabled": True}]
        with patch.object(tools_mod.mcp_client, "connector_directory",
                          return_value=directory), \
                patch.object(tools_mod.mcp_client, "list_tools",
                             side_effect=RuntimeError("engine offline")):
            self.assertIsNone(tools_mod._try_mcp_screenshot(self.tmp))

    def test_keyword_filter_skips_recording_tools(self):
        self.assertFalse(tools_mod._is_screenshot_connector_tool(
            {"name": "record_video", "description": "screenshot recording"}))
        self.assertTrue(tools_mod._is_screenshot_connector_tool(
            {"name": "capture_screen", "description": ""}))
        self.assertFalse(tools_mod._is_screenshot_connector_tool(
            {"name": "list_scenes", "description": ""}))


class RegistrationTests(unittest.TestCase):
    def test_tool_registered_readonly_parallel_safe(self):
        self.assertIn("game_screenshot", tools_mod.TOOLS)
        spec = coerce_tool_spec(
            "game_screenshot", tools_mod.TOOLS["game_screenshot"])
        self.assertEqual(spec.capability.value, "read_local")
        self.assertTrue(spec.parallel_safe)
        self.assertEqual(spec.group, "game")
        # 原来这里还有一句 assertNotIn(..., agent_mod._NO_PARALLEL_TOOLS)，
        # 但 _NO_PARALLEL_TOOLS 是运行期不读的死副本——上面 assertTrue(spec.parallel_safe)
        # 才是运行期真正拿到的那份判定，所以删掉那句，改断真实对象。

    def test_desktop_capture_is_readonly_observation(self):
        self.assertIn("dev_desktop_capture", tools_mod.TOOLS)
        spec = coerce_tool_spec(
            "dev_desktop_capture", tools_mod.TOOLS["dev_desktop_capture"])
        self.assertEqual(spec.capability.value, "read_local")
        self.assertEqual(spec.side_effect.value, "pure")

    def test_role_whitelists(self):
        roles = agent_mod._SUBAGENT_ROLES
        self.assertIn("game_screenshot", roles["tester"]["tools"])
        self.assertNotIn("game_screenshot", roles["planner"]["tools"])
        self.assertNotIn("game_screenshot", roles["dispatcher"]["tools"])

    def test_system_prompt_mentions_tool(self):
        self.assertIn("game_screenshot", agent_mod.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
