# -*- coding: utf-8 -*-
"""T3：game_screenshot 工具与 screen_capture 模块（离线；抓帧全部注入替身）。"""
import base64
import ctypes
import io
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

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


def _gradient_bgra(width, height):
    """每像素用 (x, y) 编码的合成 BGRA 帧，裁剪后可逐像素核对来源坐标。"""
    raw = bytearray(width * height * 4)
    for y in range(height):
        row = y * width * 4
        for x in range(width):
            offset = row + x * 4
            raw[offset] = x & 0xFF
            raw[offset + 1] = y & 0xFF
            raw[offset + 2] = 7
            raw[offset + 3] = 255
    return bytes(raw)


class CaptureGeometryContractTests(unittest.TestCase):
    """截图坐标系契约：输出图像的 (0,0) 必须是客户区的 (0,0)。"""

    # 实测的 8,31 是这台机器一个典型顶层窗口的非客户区偏移；它不该写死，
    # 只作为「已知矩形 → 偏移」的核对样例。
    WINDOW = {"x": 100, "y": 100, "width": 816, "height": 631}
    CLIENT = {"width": 800, "height": 600}
    ORIGIN = {"x": 108, "y": 131}

    def test_known_rect_crop_offset(self):
        crop = screen_capture.compute_client_crop(
            self.WINDOW, self.CLIENT, self.ORIGIN)
        self.assertEqual((crop["left"], crop["top"]), (8, 31))
        self.assertEqual((crop["right"], crop["bottom"]), (8, 0))
        self.assertEqual((crop["width"], crop["height"]), (800, 600))

    def test_full_window_frame_is_cropped_to_client(self):
        width, height = 100, 60
        window = {"x": 10, "y": 20, "width": width, "height": height}
        client = {"width": 80, "height": 40}
        origin = {"x": 15, "y": 26}  # 左 5、上 6
        crop = screen_capture.compute_client_crop(window, client, origin)
        raw, out_w, out_h = screen_capture.crop_client_frame(
            _gradient_bgra(width, height), width, height, crop)
        self.assertEqual((out_w, out_h), (80, 40))
        self.assertEqual((raw[0], raw[1], raw[3]), (5, 6, 255))
        self.assertEqual((raw[-4], raw[-3]), (5 + 80 - 1, 6 + 40 - 1))
        screen_capture.assert_frame_matches_client(out_w, out_h, 80, 40)

    def test_size_mismatch_raises_geometry_error(self):
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.assert_frame_matches_client(798, 600, 800, 600)
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.assert_frame_matches_client(800, 598, 800, 600)
        # 1px 取整误差是允许的
        screen_capture.assert_frame_matches_client(801, 599, 800, 600)

    def test_crop_rejects_client_outside_window(self):
        bad_origin = {"x": 90, "y": 100}  # 客户区原点在外框左侧之外
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.compute_client_crop(self.WINDOW, self.CLIENT, bad_origin)
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.compute_client_crop(
                self.WINDOW, {"width": 900, "height": 600}, self.ORIGIN)

    def test_crop_frame_rejects_bad_bytes_and_bounds(self):
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.crop_client_frame(b"short", 2, 2, {
                "left": 0, "top": 0, "width": 2, "height": 2})
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.crop_client_frame(
                _gradient_bgra(4, 4), 4, 4,
                {"left": 3, "top": 0, "width": 2, "height": 2})

    def test_crop_reports_incomplete_or_nonpositive_geometry(self):
        # 缺字段和零尺寸都必须走「拒绝」而不是 KeyError/ZeroDivision。
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.compute_client_crop({"x": 0}, self.CLIENT, self.ORIGIN)
        with self.assertRaises(screen_capture.CaptureGeometryError):
            screen_capture.compute_client_crop(
                {"x": 0, "y": 0, "width": 0, "height": 600},
                self.CLIENT, {"x": 0, "y": 0})


class _FakeUser32:
    """最小 user32 替身：按配置填写 RECT/POINT，但不调用任何 Win32。"""

    def __init__(self, *, window=(10, 20, 110, 80), client=(0, 0, 80, 40),
                 origin=(15, 26), window_ok=True, client_ok=True,
                 origin_ok=True, print_result=1, dc=1):
        self.window = window
        self.client = client
        self.origin = origin
        self.window_ok = window_ok
        self.client_ok = client_ok
        self.origin_ok = origin_ok
        self.print_result = print_result
        self.dc = dc
        self.print_calls = 0
        self.released = []

    def GetWindowRect(self, _hwnd, ref):
        if not self.window_ok:
            return 0
        ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = self.window
        return 1

    def GetClientRect(self, _hwnd, ref):
        if not self.client_ok:
            return 0
        ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = self.client
        return 1

    def ClientToScreen(self, _hwnd, ref):
        if not self.origin_ok:
            return 0
        ref._obj.x, ref._obj.y = self.origin
        return 1

    def GetDC(self, _hwnd):
        return self.dc

    def ReleaseDC(self, hwnd, dc):
        self.released.append((hwnd, dc))
        return 1

    def PrintWindow(self, _hwnd, _dc, _flags):
        self.print_calls += 1
        return self.print_result


class _FakeGdi32:
    """最小 gdi32 替身：GetDIBits 按请求高度返回，buffer 留零。"""

    def __init__(self, *, bitblt=1, scan_rows=None):
        self.bitblt = bitblt
        self.scan_rows = scan_rows
        self.bitblt_calls = 0
        self.deleted = []
        self.dcs_deleted = []

    def CreateCompatibleDC(self, _dc):
        return 2

    def CreateCompatibleBitmap(self, _dc, _w, _h):
        return 3

    def SelectObject(self, _dc, _obj):
        return 4

    def BitBlt(self, *_args):
        self.bitblt_calls += 1
        return self.bitblt

    def GetDIBits(self, _dc, _bmp, _start, rows, _buffer, _info, _usage):
        return rows if self.scan_rows is None else self.scan_rows

    def DeleteObject(self, obj):
        self.deleted.append(obj)
        return 1

    def DeleteDC(self, dc):
        self.dcs_deleted.append(dc)
        return 1


class CapturePlatformPathTests(unittest.TestCase):
    """Win32 绑定/DPI/几何/渲染路径：用替身覆盖，CI（Linux）也能跑。"""

    GEOMETRY = (
        {"x": 10, "y": 20, "width": 100, "height": 60},
        {"width": 80, "height": 40},
        {"x": 15, "y": 26},
    )

    def test_libs_binds_signatures_on_windows(self):
        user32, gdi32 = MagicMock(), MagicMock()
        windll = types.SimpleNamespace(user32=user32, gdi32=gdi32)
        with patch.object(screen_capture.os, "name", "nt"), \
                patch.object(screen_capture.ctypes, "windll", windll, create=True):
            got_user32, got_gdi32 = screen_capture._libs()
        self.assertIs(got_user32, user32)
        self.assertIs(got_gdi32, gdi32)
        self.assertEqual(user32.GetWindowRect.restype, ctypes.wintypes.BOOL)
        self.assertEqual(gdi32.GetDIBits.restype, ctypes.c_int)

    def test_ensure_capture_dpi_is_process_wide_and_reports_dpi(self):
        calls = []
        bridge = types.SimpleNamespace(
            ensure_dpi_awareness=lambda: (calls.append("ensure") or (True, "per-monitor-v2")),
            dpi_of=lambda _hwnd: 120)
        with patch.dict(screen_capture._DPI_STATE,
                        {"checked": False, "awareness": "unknown"}, clear=True), \
                patch.dict(sys.modules, {"desktop_bridge": bridge}):
            self.assertEqual(screen_capture.ensure_capture_dpi(),
                             {"awareness": "per-monitor-v2", "dpi": 96})
            self.assertEqual(screen_capture.ensure_capture_dpi(7),
                             {"awareness": "per-monitor-v2", "dpi": 120})
        self.assertEqual(calls, ["ensure"])

    def test_ensure_capture_dpi_keeps_going_when_platform_calls_fail(self):
        def boom():
            raise RuntimeError("no dpi api")

        def bad_dpi(_hwnd):
            raise RuntimeError("no dpi")

        bridge = types.SimpleNamespace(ensure_dpi_awareness=boom, dpi_of=bad_dpi)
        with patch.dict(screen_capture._DPI_STATE,
                        {"checked": False, "awareness": "unknown"}, clear=True), \
                patch.dict(sys.modules, {"desktop_bridge": bridge}):
            self.assertEqual(screen_capture.ensure_capture_dpi(9),
                             {"awareness": "unknown", "dpi": 96})

    def test_client_geometry_success_and_each_failure(self):
        self.assertEqual(
            screen_capture._client_geometry(_FakeUser32(), 123), self.GEOMETRY)
        for kwargs in ({"window_ok": False}, {"client_ok": False}, {"origin_ok": False}):
            with self.subTest(**kwargs):
                self.assertIsNone(
                    screen_capture._client_geometry(_FakeUser32(**kwargs), 123))

    def test_render_window_bgra_returns_none_when_printwindow_fails(self):
        user32, gdi32 = _FakeUser32(print_result=0), _FakeGdi32()
        self.assertIsNone(screen_capture._render_window_bgra(
            123, user32, gdi32, 100, 60, print_window=True))
        # 失败也要把 GDI 对象还回去，长驻进程不能漏句柄。
        self.assertEqual(gdi32.deleted, [3])
        self.assertEqual(gdi32.dcs_deleted, [2])
        self.assertEqual(user32.released, [(123, 1)])

    def test_capture_window_printwindow_full_then_crop(self):
        user32, gdi32 = _FakeUser32(), _FakeGdi32()
        with patch.object(screen_capture, "_libs", return_value=(user32, gdi32)), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "per-monitor-v2", "dpi": 96}):
            out = screen_capture.capture_window(123)
        self.assertIsNotNone(out)
        raw, out_w, out_h = out
        self.assertEqual((out_w, out_h), (80, 40))
        self.assertEqual(len(raw), 80 * 40 * 4)
        self.assertEqual(user32.print_calls, 1)

    def test_capture_window_falls_back_to_client_bitblt(self):
        user32, gdi32 = _FakeUser32(print_result=0), _FakeGdi32()
        with patch.object(screen_capture, "_libs", return_value=(user32, gdi32)), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "per-monitor-v2", "dpi": 96}):
            out = screen_capture.capture_window(123)
        self.assertEqual(out[1:], (80, 40))
        self.assertEqual(gdi32.bitblt_calls, 1)

    def test_capture_window_returns_none_when_both_render_paths_fail(self):
        user32, gdi32 = _FakeUser32(print_result=0), _FakeGdi32()
        with patch.object(screen_capture, "_libs", return_value=(user32, gdi32)), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "unknown", "dpi": 96}), \
                patch.object(screen_capture, "_render_window_bgra", return_value=None):
            self.assertIsNone(screen_capture.capture_window(123))

    def test_capture_window_returns_none_when_geometry_unavailable(self):
        with patch.object(screen_capture, "_libs",
                          return_value=(_FakeUser32(window_ok=False), _FakeGdi32())), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "unknown", "dpi": 96}):
            self.assertIsNone(screen_capture.capture_window(123))

    def test_capture_window_returns_none_for_empty_client_or_no_win32(self):
        with patch.object(screen_capture, "_libs",
                          return_value=(_FakeUser32(client=(0, 0, 0, 0)), _FakeGdi32())), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "unknown", "dpi": 96}):
            self.assertIsNone(screen_capture.capture_window(123))
        with patch.object(screen_capture, "_libs", return_value=(None, None)):
            self.assertIsNone(screen_capture.capture_window(123))

    def test_capture_window_size_mismatch_is_fatal_by_default(self):
        user32, gdi32 = _FakeUser32(print_result=0), _FakeGdi32()
        bad_frame = (_bgra(78, 40), 78, 40)  # 比客户区窄 2px，超出 1px 容差
        with patch.object(screen_capture, "_libs", return_value=(user32, gdi32)), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "unknown", "dpi": 96}), \
                patch.object(screen_capture, "_render_window_bgra",
                             side_effect=[None, bad_frame]):
            with self.assertRaises(screen_capture.CaptureGeometryError):
                screen_capture.capture_window(123)

    def test_capture_window_size_mismatch_can_be_tolerated(self):
        user32, gdi32 = _FakeUser32(print_result=0), _FakeGdi32()
        bad_frame = (_bgra(78, 40), 78, 40)
        with patch.object(screen_capture, "_libs", return_value=(user32, gdi32)), \
                patch.object(screen_capture, "ensure_capture_dpi",
                             return_value={"awareness": "unknown", "dpi": 96}), \
                patch.object(screen_capture, "_render_window_bgra",
                             side_effect=[None, bad_frame]), \
                self.assertLogs(screen_capture.logger, level="ERROR"):
            out = screen_capture.capture_window(123, strict_geometry=False)
        self.assertEqual(out[1:], (78, 40))


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
        # 产物路径是项目相对路径（前端拼预览 URL）；绝对路径只在 image_sources 里。
        self.assertEqual(result.artifacts[0]["path"],
                         os.path.relpath(saved_path, self.tmp).replace("\\", "/"))

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

    def test_prefers_project_engine_window_before_foreground(self):
        """嵌入不可用时先抓本项目引擎独立窗口——否则模型截到的是聊天应用。"""
        frame = (_bgra(640, 360), 640, 360)
        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(screen_capture, "grab_embedded", lambda *a, **k: None), \
                patch("game_workbench.engine_status",
                      return_value={"ok": True, "running": True, "pid": 4321}), \
                patch("desktop_bridge.find_window",
                      return_value=(9876, "StarVoyager")), \
                patch("desktop_bridge.restore_window",
                      return_value={"ok": True, "restored": True, "foreground": True}), \
                patch.object(screen_capture, "capture_window",
                             lambda hwnd: frame if int(hwnd) == 9876 else None), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail(
                                 "命中本项目引擎窗口时不应再抓前台窗口")):
            result = tools_mod.game_screenshot("")
        self.assertIsInstance(result, ToolResult)
        self.assertIn("engine 窗口「StarVoyager」", result.text)
        self.assertIn("640x360", result.text)
        self.assertEqual(
            result.artifacts[0]["metadata"]["capture_source"], "engine")

    def test_restores_minimized_engine_window_before_capturing(self):
        """Godot 游戏窗口会自最小化（客户区 0x0）→ 抓图前必须先还原，否则退回前台截到聊天应用。"""
        frame = (_bgra(853, 480), 853, 480)
        order = []

        def _restore(hwnd):
            order.append(("restore", int(hwnd)))
            return {"ok": True, "restored": True, "foreground": True}

        def _capture(hwnd):
            order.append(("capture", int(hwnd)))
            return frame if int(hwnd) == 9876 else None

        with patch.object(tools_mod, "_try_mcp_screenshot", lambda root: None), \
                patch.object(screen_capture, "grab_embedded", lambda *a, **k: None), \
                patch("game_workbench.engine_status",
                      return_value={"ok": True, "running": True, "pid": 4321}), \
                patch("desktop_bridge.find_window",
                      return_value=(9876, "StarVoyager (DEBUG)")), \
                patch("desktop_bridge.restore_window", side_effect=_restore), \
                patch.object(screen_capture, "capture_window", side_effect=_capture), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail("还原后应能抓到引擎窗口，不该退回前台")):
            result = tools_mod.game_screenshot("")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(order, [("restore", 9876), ("capture", 9876)],
                         "必须先把引擎窗口还原再抓，顺序反了就会抓到前台聊天应用")

    def test_foreground_target_skips_mcp_and_embedded(self):
        frame = (_bgra(32, 32), 32, 32, 7)
        with patch.object(tools_mod, "_try_mcp_screenshot",
                          lambda root: self.fail("foreground 目标不应探测 MCP")), \
                patch.object(screen_capture, "grab_embedded",
                             lambda *a, **k: self.fail("foreground 目标不应抓嵌入窗")), \
                patch("game_workbench.engine_status",
                      side_effect=AssertionError("foreground 目标不应探测引擎窗口")), \
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
