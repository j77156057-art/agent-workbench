"""M4 Linux/X11/VNC frame_grabber 离线单测。

纪律：本机没有 X11/VNC/ImageMagick，全部走可注入 runner/transport + 合成 PNG/RFB 字节，
和 M1/M2/M3 注入假 grabber 自检是同一套路数。**真机取像路径不在本机执行**——这里验的是
形状契约、fail-closed 边界，以及最关键的一条：签名对齐 `screen_capture.capture_window`，
上层零改动。
"""
import io
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime.frame_grabber_linux import (  # noqa: E402
    to_bgra, png_to_frame, x11_grabber, vnc_frame_grabber, rfb_grab_frame,
)


# ---- 小工具：合成一张已知像素的 PNG ----------------------------------------

def _make_png(pixels_rgb):
    """pixels_rgb: list[list[(r,g,b)]] 逐行；用 Pillow 生成 PNG 字节。"""
    from PIL import Image
    h = len(pixels_rgb)
    w = len(pixels_rgb[0])
    img = Image.new("RGB", (w, h))
    for y in range(h):
        for x in range(w):
            img.putpixel((x, y), pixels_rgb[y][x])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---- to_bgra ----------------------------------------------------------------

def test_to_bgra_channel_order():
    # 1px 纯红 (R=255,G=10,B=20) → BGRX 应为 [B=20, G=10, R=255, pad]
    frame = to_bgra(1, 1, bytes([255, 10, 20]))
    assert frame is not None
    bgra, w, h = frame
    assert (w, h) == (1, 1)
    assert len(bgra) == 4
    assert bgra[0] == 20 and bgra[1] == 10 and bgra[2] == 255  # B,G,R
    assert bgra[3] == 0xFF


def test_to_bgra_rejects_bad_len():
    assert to_bgra(2, 2, bytes(2 * 2 * 3 - 1)) is None   # 少一字节，不补齐
    assert to_bgra(2, 2, bytes(2 * 2 * 3 + 3)) is None   # 多，不截断
    assert to_bgra(0, 1, b"") is None
    assert to_bgra(-1, 1, bytes(3)) is None


def test_to_bgra_len_is_w_h_4():
    frame = to_bgra(3, 2, bytes(3 * 2 * 3))
    assert frame is not None
    assert len(frame[0]) == 3 * 2 * 4


# ---- png_to_frame -----------------------------------------------------------

def test_png_to_frame_roundtrip():
    png = _make_png([[(255, 0, 0), (0, 255, 0)], [(0, 0, 255), (0, 0, 0)]])
    frame = png_to_frame(png)
    assert frame is not None
    bgra, w, h = frame
    assert (w, h) == (2, 2)
    assert len(bgra) == 2 * 2 * 4
    # 第一像素红 → BGRX = [0,0,255,*]
    assert bgra[0] == 0 and bgra[1] == 0 and bgra[2] == 255


def test_png_to_frame_rejects_junk():
    assert png_to_frame(b"") is None
    assert png_to_frame(b"not a png at all") is None


# ---- x11_grabber（注入 runner）---------------------------------------------

def test_x11_grabber_success_captures_window_id():
    png = _make_png([[(1, 2, 3)]])
    seen = {}

    def fake_runner(cmd):
        seen["cmd"] = cmd
        return 0, png

    grab = x11_grabber(runner=fake_runner)
    frame = grab(0x2a)   # hwnd 当 window id
    assert frame is not None
    assert frame[1:] == (1, 1)
    assert "import" in seen["cmd"]
    assert str(0x2a) in seen["cmd"]      # 传进来的 hwnd 被当作目标窗口 id
    assert seen["cmd"][-1] == "png:-"


def test_x11_grabber_explicit_window_id_overrides_hwnd():
    png = _make_png([[(0, 0, 0)]])
    seen = {}

    def fake_runner(cmd):
        seen["cmd"] = cmd
        return 0, png

    grab = x11_grabber(window_id=999, runner=fake_runner)
    frame = grab(0xdeadbeef)              # hwnd 被显式 window_id 覆盖
    assert frame is not None
    assert "999" in seen["cmd"]


def test_x11_grabber_tool_missing_fail_closed():
    def boom(cmd):
        raise FileNotFoundError("no import")
    grab = x11_grabber(runner=boom)
    assert grab(5) is None
    assert grab.last_error == "tool_failed"


def test_x11_grabber_nonzero_rc_fail_closed():
    grab = x11_grabber(runner=lambda cmd: (1, b"whatever"))
    assert grab(5) is None
    assert grab.last_error == "tool_failed"


def test_x11_grabber_empty_output_fail_closed():
    grab = x11_grabber(runner=lambda cmd: (0, b""))
    assert grab(5) is None
    assert grab.last_error == "empty_output"


def test_x11_grabber_undecodable_output_fail_closed():
    grab = x11_grabber(runner=lambda cmd: (0, b"garbage-not-png"))
    assert grab(5) is None
    assert grab.last_error == "undecodable_output"


def test_x11_grabber_bad_window_id():
    grab = x11_grabber(runner=lambda cmd: (0, b""))
    assert grab(0) is None
    assert grab.last_error == "bad_window_id"
    assert grab("nope") is None
    assert grab(-3) is None


# ---- RFB / VNC（注入 transport）--------------------------------------------

class FakeRFB:
    """可注入 RFB transport：按预置 server 字节流应答，记录 client 发送。"""

    def __init__(self, server_bytes):
        self._buf = bytearray(server_bytes)
        self.sent = bytearray()

    def recv(self, n):
        chunk = bytes(self._buf[:n])
        del self._buf[:len(chunk)]
        return chunk

    def send(self, b):
        self.sent += b


def _server_init(fb_w, fb_h, name=b"x", *, n_types=1, types=b"\x01",
                 refused=False):
    """拼一段 server→client 字节流：版本 + SecurityTypes + SecurityResult + ServerInit(+name)。"""
    out = bytearray()
    out += b"RFB 003.008\n"
    if refused:
        out += b"\x00" + struct.pack(">I", 5) + b"nope!"
        return bytes(out)
    out += bytes([n_types]) + types
    out += b"\x00\x00\x00\x00"   # SecurityResult OK (仅当含 None 认证时才会走到)
    # ServerInit: fb_w(H) fb_h(H) pixel_format(16) nameLen(I) + name
    pf = bytes([32, 24, 0, 1]) + struct.pack(">HHH", 255, 255, 255) + bytes([16, 8, 0, 0, 0, 0])
    out += struct.pack(">HH", fb_w, fb_h) + pf + struct.pack(">I", len(name)) + name
    return bytes(out)


def _raw_update(fb_w, fb_h, pixel_bytes, *, rects=1, enc=0, x=0, y=0, w=None, h=None):
    """拼一个 FramebufferUpdate（message-type 0）。"""
    w = fb_w if w is None else w
    h = fb_h if h is None else h
    out = bytearray()
    out += b"\x00" + b"\x00" + struct.pack(">H", rects)   # type, pad, n-rects
    out += struct.pack(">HHHH", x, y, w, h)
    out += struct.pack(">I", enc)
    out += pixel_bytes
    return bytes(out)


def test_rfb_happy_path():
    fb_w, fb_h = 2, 2
    pixels = bytes([10, 20, 30, 255] * 4)   # 4 像素，每像素 BGRX
    stream = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels)
    t = FakeRFB(stream)
    frame = rfb_grab_frame(t.recv, t.send)
    assert frame is not None
    bgra, w, h = frame
    assert (w, h) == (2, 2)
    assert bgra == pixels
    # client 确实协商了 32bpp SetPixelFormat（msg-type 0，bpp 字节 = 32）
    assert b"\x00\x00\x00\x00\x20" in bytes(t.sent) or any(t.sent[i:i + 1] == b"\x20" for i in range(len(t.sent)))


def test_rfb_not_an_rfb_server():
    t = FakeRFB(b"HTTP/1.1 200\r\n")
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_server_refused():
    t = FakeRFB(_server_init(2, 2, refused=True))
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_requires_auth_fail_closed():
    # types = [2]（VNC auth），client 只支持 None → fail-closed
    stream = b"RFB 003.008\n" + bytes([1]) + bytes([2])
    t = FakeRFB(stream)
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_unsupported_encoding():
    fb_w, fb_h = 2, 2
    pixels = bytes([0, 0, 0, 0] * 4)
    # enc=2 (tight) 而非 0 (raw)
    stream = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels, enc=2)
    t = FakeRFB(stream)
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_partial_rect_fail_closed():
    fb_w, fb_h = 4, 4
    pixels = bytes([0, 0, 0, 0] * (2 * 2))
    # 只给一个 2x2 的局部矩形（不是整屏）→ M4 不拼半帧
    stream = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels, w=2, h=2)
    t = FakeRFB(stream)
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_multi_rect_fail_closed():
    fb_w, fb_h = 2, 2
    pixels = bytes([0, 0, 0, 0] * 4)
    stream = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels, rects=2)
    t = FakeRFB(stream)
    assert rfb_grab_frame(t.recv, t.send) is None


def test_rfb_truncated_stream_fail_closed():
    fb_w, fb_h = 2, 2
    pixels = bytes([0, 0, 0, 0] * 4)
    full = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels)
    t = FakeRFB(full[:-3])   # 截掉末尾几字节 → 读不满，None 而不是半帧
    assert rfb_grab_frame(t.recv, t.send) is None


def test_vnc_frame_grabber_wraps_transport_success():
    fb_w, fb_h = 2, 1
    pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    stream = _server_init(fb_w, fb_h) + _raw_update(fb_w, fb_h, pixels)
    t = FakeRFB(stream)
    grab = vnc_frame_grabber(transport=t)
    frame = grab()
    assert frame == (pixels, 2, 1)
    assert grab.last_error is None


def test_vnc_frame_grabber_bad_transport_fail_closed():
    class NoMethods:
        pass
    grab = vnc_frame_grabber(transport=NoMethods())
    assert grab() is None
    assert grab.last_error == "bad_transport"


def test_vnc_frame_grabber_rfb_failure_records_error():
    t = FakeRFB(b"HTTP/1.1 200\r\n")   # 不是 RFB
    grab = vnc_frame_grabber(transport=t)
    assert grab() is None
    assert grab.last_error == "rfb_failed"


# ---- 契约对齐：签名匹配 capture_window，上层零改动（M4 验收核心）---------

def test_capture_window_passthrough_is_byte_identical():
    """把 linux grabber 塞进 screen_capture.capture_window，返回值与直接调用逐字节一致，
    证明 frame_grabber 注入点即取像接入点，上层代码一行不改。"""
    import screen_capture

    png = _make_png([[(11, 22, 33), (44, 55, 66)], [(77, 88, 99), (0, 0, 0)]])
    grab = x11_grabber(runner=lambda cmd: (0, png))

    direct = grab(0x1234)
    via_capture = screen_capture.capture_window(0x1234, frame_grabber=grab)
    assert direct is not None and via_capture is not None
    assert via_capture == direct
    assert len(via_capture) == 3
    bgra, w, h = via_capture
    assert (w, h) == (2, 2)
    assert len(bgra) == 2 * 2 * 4


def test_frame_feeds_downstream_visual_regression():
    """grabber 产出的 BGRX 帧能被 visual_regression.rgba_from_bgra 直接 reshape，
    且能被 encode_frame 编成 data_url —— 即下游取像/回归链路无需改动。"""
    import screen_capture
    from agent_runtime import visual_regression as vr

    png = _make_png([[(5, 6, 7), (8, 9, 10)]])
    grab = x11_grabber(runner=lambda cmd: (0, png))
    bgra, w, h = screen_capture.capture_window(0x99, frame_grabber=grab)

    arr = vr.to_rgb(vr.rgba_from_bgra(bgra, w, h))
    assert arr.shape == (h, w, 3)

    url = screen_capture.encode_frame(bgra, w, h)
    assert isinstance(url, str) and url.startswith("data:image/jpeg;base64,")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
