"""Linux/X11/VNC 取像通路（M4）：写一个签名对齐 capture_window 的 frame_grabber。

信条（docs/multimodal-verification-engine-eda-kickoff.md §1.1、§3.2 第2段、§4 M4）：
- **上层零改动。** 只交付一个 `frame_grabber(hwnd) -> (bgra_bytes, width, height) | None`，
  直接塞进 `screen_capture.capture_window(hwnd, frame_grabber=...)`。字节布局必须和
  GDI 一模一样：**BGRX**（每像素 B,G,R,pad，top-down，长度 = w*h*4）——这是
  `encode_frame/encode_and_save` 用 `Image.frombytes("RGB", ..., raw, "BGRX")`
  以及 `visual_regression.rgba_from_bgra` 直接 reshape 所要求的布局。对齐了它，
  `capture_window`/`grab_embedded`/`engine_visual_regression` 全链路不动。
- **fail-closed，不造假、不猜色。** 工具缺失 / 非零退出 / 帧截断 / 不支持的编码
  → 一律返回 None（"没拿到画面"），绝不拼一个看着像的帧糊弄上层。EDA 截图是验收证据，
  宁可没有也不能有假的。
- **只走能确定的解码路。** X11 侧统一以"拿到 PNG 字节 → Pillow 解"为准（ImageMagick
  `import -window <id> png:-`，PNG 是无损自描述格式，色序由 Pillow 保证）。
  **故意不手搓 XWD 二进制头解析**——xwd.h 变体多、字段偏移脆弱，在这台没有 X11 的机器上
  无法对真机 `xwd` 转储验真，写一个"看着能过"的解析器恰恰是造假。xwd-only（无 ImageMagick）
  环境是已知后续项，不在 M4 假装覆盖。
- **本机没有 X11/VNC 也能离线跑通。** 真机取像素走可注入的 `runner`（子进程）与
  `transport`（RFB 字节流）；默认实现按 `subprocess` 跑二进制、`socket` 连 VNC。
  替身喂合成 PNG / RFB 字节即可像 M1/M2/M3 一样离线验收"形状一致、上层不改"。
  **注意：本模块在测试机（Windows）只被 import 做契约测试，真机取像路径未在本机执行。**

依赖：顶层只用标准库（struct / shutil / typing）。Pillow、subprocess、socket 全部惰性导入，
保证在无 PIL / 无 X11 / 无网络的环境里 `import` 本模块也不炸。
"""
from __future__ import annotations

import struct
from typing import Callable, Optional, Tuple

# 一帧的规范化返回类型：(bgra_bytes, width, height)。与 screen_capture 保持一致。
Frame = Tuple[bytes, int, int]
Runner = Callable[[list], Tuple[int, bytes]]      # (argv) -> (returncode, stdout_bytes)
Recv = Callable[[int], bytes]                     # 读 n 字节（可能不足 n；上层自旋补齐）
Send = Callable[[bytes], None]


class GrabError(RuntimeError):
    """内部失败信号；对外统一收敛成 None，仅把原因写进 grabber.last_error 供诊断。"""


# ---- 像素/编码辅助 ---------------------------------------------------------

def to_bgra(width: int, height: int, rgb_bytes: bytes) -> Optional[Frame]:
    """把纯 RGB（每像素 R,G,B，长度 = w*h*3）转成 BGRX 帧。长度不符 → None（不补齐、不截断）。"""
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    need = width * height * 3
    if len(rgb_bytes) != need:
        return None
    out = bytearray(width * height * 4)
    o = 0
    for i in range(0, need, 3):
        out[o] = rgb_bytes[i + 2]      # B
        out[o + 1] = rgb_bytes[i + 1]  # G
        out[o + 2] = rgb_bytes[i]      # R
        out[o + 3] = 0xFF              # pad/alpha（下游 "BGRX" 解码会忽略这一字节）
        o += 4
    return bytes(out), width, height


def png_to_frame(data: bytes) -> Optional[Frame]:
    """PNG/JPEG 字节 → BGRX 帧。用 Pillow 解，任何失败 → None（不猜）。"""
    if not data:
        return None
    import io
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = int(img.size[0]), int(img.size[1])
        if w <= 0 or h <= 0:
            return None
        return to_bgra(w, h, img.tobytes())
    except Exception:
        return None


def _default_run(cmd: list) -> Tuple[int, bytes]:
    """默认 runner：subprocess 捕获 stdout；工具不存在/异常 → (-1, b'')。"""
    try:
        import subprocess
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=15, check=False)
        return int(proc.returncode), proc.stdout or b""
    except FileNotFoundError:
        return -1, b""
    except Exception:
        return -1, b""


# ---- X11 grabber（ImageMagick import → PNG，可注入 runner） ----------------

def x11_grabber(window_id: Optional[int] = None, *,
                runner: Optional[Runner] = None,
                require_known_tool: bool = False):
    """返回一个 `frame_grabber(hwnd)`，签名对齐 screen_capture.capture_window。

    - 目标窗口 id：优先显式 `window_id`，否则把调用方传进来的 `hwnd` 当 X11 window id
      （保持 capture_window 的"传一个窗口句柄"语义）。
    - 取像：`import -window <id> png:-`（ImageMagick）→ PNG 字节 → Pillow → BGRX。
    - 任一环节失败（id 非法 / 工具缺失 / 非零退出 / 空输出 / 坏 PNG）→ None，并把原因
      记进返回函数的 `last_error` 属性，方便真机排查。
    - `require_known_tool=True` 时用 `shutil.which("import")` 先探测；探测不到直接 fail-closed
      （注入 runner 做测试时保持 False，否则会因本机没装 ImageMagick 而误判不可用）。
    """
    run = runner or _default_run

    def grabber(hwnd) -> Optional[Frame]:
        grabber.last_error = None  # type: ignore[attr-defined]
        try:
            wid = int(window_id if window_id is not None else hwnd)
        except (TypeError, ValueError):
            grabber.last_error = "bad_window_id"  # type: ignore[attr-defined]
            return None
        if wid <= 0:
            grabber.last_error = "bad_window_id"  # type: ignore[attr-defined]
            return None
        if require_known_tool and runner is None:
            import shutil
            if shutil.which("import") is None:
                grabber.last_error = "tool_unavailable"  # type: ignore[attr-defined]
                return None

        cmd = ["import", "-window", str(wid), "png:-"]
        try:
            rc, out = run(cmd)
        except Exception:
            rc, out = -1, b""
        if rc != 0:
            grabber.last_error = "tool_failed"  # type: ignore[attr-defined]
            return None
        if not out:
            grabber.last_error = "empty_output"  # type: ignore[attr-defined]
            return None
        frame = png_to_frame(out)
        if frame is None:
            grabber.last_error = "undecodable_output"  # type: ignore[attr-defined]
            return None
        return frame

    grabber.last_error = None  # type: ignore[attr-defined]
    grabber.name = "x11"  # type: ignore[attr-defined]
    return grabber


# ---- 最小 RFB（VNC）客户端：握手 + 32bpp raw 帧 ----------------------------

def _read_exact(recv: Recv, n: int) -> bytes:
    """从 transport 精确读满 n 字节；提前 EOF（空返回）→ GrabError（不返回短帧）。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = recv(n - len(buf))
        if not chunk:
            raise GrabError("rfb_stream_truncated")
        buf += chunk
    return bytes(buf[:n])


def rfb_grab_frame(recv: Recv, send: Send, *, require_single_rect: bool = True) -> Optional[Frame]:
    """在给定 transport 上跑一遍最小 RFB，取一帧整屏 framebuffer。

    流程：版本协商(3.8) → SecurityTypes 要求 None → ClientInit → SetPixelFormat(32bpp,
    little-endian, red<<16|green<<8|blue<<0 → 内存字节序正是 B,G,R,X) → 全帧
    FramebufferUpdateRequest(incremental=0) → 解 raw 编码(0) 单矩形。

    fail-closed 面很宽：非 RFB 服务 / server 拒绝 / 需要认证（DES 离线不实现）/
    非 raw 编码 / 分块矩形 / 流截断 → 一律返回 None，绝不猜、绝不拼半帧。
    """
    try:
        server_ver = _read_exact(recv, 12)
        if not server_ver.startswith(b"RFB "):
            raise GrabError("not_an_rfb_server")
        send(b"RFB 003.008\n")

        n_types = _read_exact(recv, 1)[0]
        if n_types == 0:
            reason_len = int(struct.unpack(">I", _read_exact(recv, 4))[0])
            _read_exact(recv, reason_len)
            raise GrabError("rfb_server_refused")
        types = list(_read_exact(recv, n_types))
        if 1 not in types:
            # 1 = None 认证。没有 None 就不做（VNC-Auth/DES 离线不支持，宁缺毋假）。
            raise GrabError("rfb_requires_auth")
        send(bytes([1]))
        if _read_exact(recv, 4) != b"\x00\x00\x00\x00":
            raise GrabError("rfb_auth_failed")

        init = _read_exact(recv, 24)
        fb_w, fb_h = struct.unpack(">HH", init[0:4])
        name_len = struct.unpack(">I", init[20:24])[0]
        _read_exact(recv, name_len)
        if fb_w <= 0 or fb_h <= 0:
            raise GrabError("rfb_bad_geometry")

        # SetPixelFormat：32bpp/depth24/little/truecolor，red@16 green@8 blue@0。
        # 于是每个像素在网络上按 4 字节 B,G,R,X 排布 —— 与目标 BGRX 布局一致，raw 段可直接用。
        set_pf = bytearray()
        set_pf.append(0)               # message-type: SetPixelFormat
        set_pf += b"\x00\x00\x00"      # padding
        set_pf.append(32)              # bits-per-pixel
        set_pf.append(24)              # depth
        set_pf.append(0)               # big-endian-flag = 0（little）
        set_pf.append(1)               # true-colour-flag
        set_pf += struct.pack(">HHH", 255, 255, 255)   # red/green/blue max
        set_pf += struct.pack(">BBB", 16, 8, 0)        # red/green/blue shift
        set_pf += b"\x00\x00\x00"      # padding
        send(bytes(set_pf))

        # FramebufferUpdateRequest：整帧，非增量。
        req = bytearray()
        req.append(0)                  # message-type: FramebufferUpdateRequest
        req.append(0)                  # incremental
        req += struct.pack(">HHHH", 0, 0, fb_w, fb_h)
        send(bytes(req))

        # 读一个 FramebufferUpdate（message-type 0）。
        msg_type = _read_exact(recv, 1)[0]
        if msg_type != 0:
            raise GrabError("unexpected_rfb_message")
        _read_exact(recv, 1)           # padding
        n_rects = struct.unpack(">H", _read_exact(recv, 2))[0]
        if n_rects != 1:
            # M4 只保证"整屏单矩形"这一最常见更新；多矩形/tiled 是已知后续项，不拼凑。
            raise GrabError("multi_rect_unsupported")
        x, y, w, h = struct.unpack(">HHHH", _read_exact(recv, 8))
        enc = struct.unpack(">I", _read_exact(recv, 4))[0]
        if enc != 0:
            raise GrabError("unsupported_encoding")
        if require_single_rect and (x != 0 or y != 0 or w != fb_w or h != fb_h):
            raise GrabError("partial_rect_unsupported")
        raw = _read_exact(recv, w * h * 4)
        return bytes(raw), w, h
    except (GrabError, struct.error):
        return None
    except Exception:
        return None


def vnc_frame_grabber(host: str = "127.0.0.1", port: int = 5900, *,
                      transport: Optional[object] = None):
    """返回一个 `frame_grabber(hwnd)`；hwnd 被忽略（VNC 抓的是整个远端 framebuffer）。

    - 注入 `transport`（鸭子类型，带 `recv(n)->bytes` 与 `send(bytes)->None`）用于离线测试；
      否则惰性建 socket 连接真机。
    - 连接失败 / RFB 任一步 fail-closed → None，并把原因写进 `last_error`。
    """
    def _open_socket():
        import socket
        s = socket.create_connection((host, port), timeout=10)

        def recv(n: int) -> bytes:
            try:
                return s.recv(n)
            except Exception:
                return b""

        def send(b: bytes) -> None:
            s.sendall(b)

        return recv, send, s

    def grabber(hwnd=None) -> Optional[Frame]:
        grabber.last_error = None  # type: ignore[attr-defined]
        if transport is not None:
            recv = getattr(transport, "recv", None)
            send = getattr(transport, "send", None)
            if recv is None or send is None:
                grabber.last_error = "bad_transport"  # type: ignore[attr-defined]
                return None
            frame = rfb_grab_frame(recv, send)
            if frame is None:
                grabber.last_error = "rfb_failed"  # type: ignore[attr-defined]
            return frame
        try:
            recv, send, sock = _open_socket()
        except Exception:
            grabber.last_error = "connect_failed"  # type: ignore[attr-defined]
            return None
        try:
            frame = rfb_grab_frame(recv, send)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if frame is None:
            grabber.last_error = "rfb_failed"  # type: ignore[attr-defined]
        return frame

    grabber.last_error = None  # type: ignore[attr-defined]
    grabber.name = "vnc"  # type: ignore[attr-defined]
    return grabber


__all__ = [
    "Frame", "GrabError", "to_bgra", "png_to_frame",
    "x11_grabber", "vnc_frame_grabber", "rfb_grab_frame",
]
