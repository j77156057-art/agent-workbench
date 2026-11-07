"""运行画面截图：纯 ctypes（GDI）+ Pillow，非 Windows/无头安全降级。

设计边界：
* **不引入 pywin32**，只用 user32/gdi32 的 ctypes 绑定；
* **不提供按任意 HWND/PID 截图的入口**：抓取目标只能来自 desktop_bridge 的
  嵌入子窗口登记表或系统前台窗口，Agent 无法借此枚举/窥视其它进程窗口；
* **坐标系契约：返回图像的 (0,0) 就是客户区 (0,0)，尺寸等于客户区尺寸。**
  PrintWindow(PW_RENDERFULLCONTENT=2) 把**整个窗口**（含标题栏/边框）画进整窗
  大小的位图，再按非客户区偏移精确裁剪到客户区；PrintWindow 失败时回退
  GetDC(hwnd) + BitBlt（该路径本身就是客户区图像，不再裁剪）。裁剪偏移不是
  写死的 (8,31)，而是每次由 GetWindowRect + ClientToScreen + GetClientRect 现算；
* 尺寸不变量在运行期强制校验（容差 1px），不满足抛 CaptureGeometryError，
  绝不带着偏移继续点；
* DPI：先 ensure per-monitor v2，GetWindowRect/ClientToScreen/GetClientRect 与
  点击端（desktop_actions 的 client_origin）都返回物理像素，截图与点击同一空间；
* 底层取像素函数可注入（``frame_grabber``），测试用替身即可在任何平台跑。
"""
import ctypes
import ctypes.wintypes
import datetime
import io
import logging
import os
import random
import threading

# PW_RENDERFULLCONTENT：Win10 1607+，对 DirectComposition/GPU 内容也能出图；
# 老系统不认这个 flag 时 PrintWindow 返回失败，调用方自动回退 BitBlt。
PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
CAPTURE_MAX_EDGE = 1600
JPEG_QUALITY = 82
#: 截图尺寸必须等于客户区尺寸；只容忍 1px 的取整误差。
CLIENT_SIZE_TOLERANCE_PX = 1

logger = logging.getLogger(__name__)


class CaptureGeometryError(RuntimeError):
    """截图几何与客户区不一致：宁可失败，也不带着偏移去点。"""


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _libs():
    """配置并返回 (user32, gdi32)；非 Windows 返回 (None, None)。"""
    if os.name != "nt":
        return None, None
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.wintypes.RECT)]
    user32.GetClientRect.restype = ctypes.wintypes.BOOL
    user32.GetWindowRect.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.wintypes.RECT)]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    user32.ClientToScreen.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.wintypes.POINT)]
    user32.ClientToScreen.restype = ctypes.wintypes.BOOL
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.wintypes.UINT]
    user32.PrintWindow.restype = ctypes.wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.wintypes.DWORD]
    gdi32.BitBlt.restype = ctypes.wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.UINT,
        ctypes.wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO), ctypes.wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.wintypes.BOOL
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.wintypes.BOOL
    return user32, gdi32


_DPI_LOCK = threading.Lock()
_DPI_STATE = {"checked": False, "awareness": "unknown"}


def ensure_capture_dpi(hwnd=None):
    """进程级 DPI 感知只提升一次，返回本次抓帧空间的 {awareness, dpi}。

    必须在建窗口之前提到 per-monitor v2，否则 125%/150% 缩放下 GetWindowRect /
    ClientToScreen 返回的是被虚拟化的逻辑坐标，位图与点击会落在不同空间。
    提升失败（进程已用别的感知模式初始化）时如实记录当前模式，不假装成功。
    """
    with _DPI_LOCK:
        if not _DPI_STATE["checked"]:
            _DPI_STATE["checked"] = True
            try:
                import desktop_bridge
                ok, mode = desktop_bridge.ensure_dpi_awareness()
                _DPI_STATE["awareness"] = mode
                logger.info("capture dpi awareness ensure ok=%s mode=%s", ok, mode)
            except Exception as exc:  # noqa: BLE001 - 记录真实模式即可，不阻断抓帧
                logger.warning("capture dpi awareness setup failed: %s", exc)
        awareness = _DPI_STATE["awareness"]
    dpi = 96
    if hwnd:
        try:
            import desktop_bridge
            dpi = int(desktop_bridge.dpi_of(hwnd) or 96)
        except Exception:  # noqa: BLE001
            dpi = 96
    return {"awareness": awareness, "dpi": dpi}


def compute_client_crop(window_rect, client_rect, client_origin):
    """由窗口外框、客户区尺寸、客户区屏幕原点算出客户区在整窗图像里的裁剪矩形。

    纯函数（不依赖 Win32），契约测试可直接用已知矩形验证：
        left = client_origin.x - window_rect.x
        top  = client_origin.y - window_rect.y
        right/bottom = 外框到客户区右侧/底部的非客户区宽度
    几何自相矛盾（客户区跑到外框外）时抛 CaptureGeometryError。
    """
    try:
        wx, wy = int(window_rect["x"]), int(window_rect["y"])
        ww, wh = int(window_rect["width"]), int(window_rect["height"])
        cw, ch = int(client_rect["width"]), int(client_rect["height"])
        ox, oy = int(client_origin["x"]), int(client_origin["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureGeometryError("无法计算客户区裁剪：几何参数不完整（%s）" % exc)
    if ww <= 0 or wh <= 0 or cw <= 0 or ch <= 0:
        raise CaptureGeometryError(
            "无法计算客户区裁剪：外框 %sx%s、客户区 %sx%s 必须为正数"
            % (ww, wh, cw, ch))
    left, top = ox - wx, oy - wy
    right = (wx + ww) - (ox + cw)
    bottom = (wy + wh) - (oy + ch)
    if left < 0 or top < 0:
        raise CaptureGeometryError(
            "客户区原点 (%d,%d) 落在外框 (%d,%d) 左上角之外，无法裁剪" % (ox, oy, wx, wy))
    if left + cw > ww or top + ch > wh:
        raise CaptureGeometryError(
            "客户区 %dx%d + 偏移 (%d,%d) 超出外框 %dx%d，无法裁剪"
            % (cw, ch, left, top, ww, wh))
    return {"left": left, "top": top, "width": cw, "height": ch,
            "right": right, "bottom": bottom,
            "window": {"width": ww, "height": wh},
            "client": {"width": cw, "height": ch}}


def crop_client_frame(raw, width, height, crop):
    """把整窗 BGRA 帧按 crop 裁成客户区帧，返回 ``(bytes, width, height)``。

    任何尺寸不符（帧本身、裁剪越界）都抛 CaptureGeometryError。
    """
    width, height = int(width), int(height)
    left, top = int(crop["left"]), int(crop["top"])
    cw, ch = int(crop["width"]), int(crop["height"])
    raw = bytes(raw)
    if width <= 0 or height <= 0 or len(raw) != width * height * 4:
        raise CaptureGeometryError(
            "整窗帧字节数与 %dx%d 不符（%d 字节），无法裁剪" % (width, height, len(raw)))
    if cw <= 0 or ch <= 0 or left < 0 or top < 0 \
            or left + cw > width or top + ch > height:
        raise CaptureGeometryError(
            "裁剪矩形 (%d,%d) %dx%d 超出整窗帧 %dx%d" % (left, top, cw, ch, width, height))
    stride = width * 4
    row_bytes = cw * 4
    rows = [raw[row * stride + left * 4: row * stride + left * 4 + row_bytes]
            for row in range(top, top + ch)]
    return b"".join(rows), cw, ch


def assert_frame_matches_client(width, height, client_width, client_height,
                                tolerance=CLIENT_SIZE_TOLERANCE_PX, context=""):
    """运行期不变量：截图尺寸必须等于客户区尺寸（容差 1px），否则直接失败。"""
    dw = abs(int(width) - int(client_width))
    dh = abs(int(height) - int(client_height))
    if dw > int(tolerance) or dh > int(tolerance):
        raise CaptureGeometryError(
            "截图尺寸 %dx%d 与客户区 %dx%d 不一致（差 %dpx/%dpx，容差 %dpx）%s。"
            "坐标系不可信，已拒绝返回该帧。"
            % (int(width), int(height), int(client_width), int(client_height),
               dw, dh, int(tolerance), (" " + context) if context else ""))


def _client_geometry(user32, hwnd):
    """读取 (窗口外框, 客户区尺寸, 客户区屏幕原点)，任一失败返回 None。"""
    window = ctypes.wintypes.RECT()
    client = ctypes.wintypes.RECT()
    point = ctypes.wintypes.POINT(0, 0)
    if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
        return None
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    return (
        {"x": int(window.left), "y": int(window.top),
         "width": int(window.right - window.left),
         "height": int(window.bottom - window.top)},
        {"width": int(client.right - client.left),
         "height": int(client.bottom - client.top)},
        {"x": int(point.x), "y": int(point.y)},
    )


def _render_window_bgra(hwnd, user32, gdi32, width, height, *, print_window):
    """把窗口画进指定尺寸的 32 位位图并读回 BGRA；失败返回 None。

    print_window=True 时整窗渲染（图像原点 = 外框左上角，调用方负责裁剪）；
    False 时从客户区 DC BitBlt（图像原点 = 客户区左上角）。
    """
    hwnd_dc = mem_dc = bmp = prev_bmp = None
    try:
        hwnd_dc = user32.GetDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        if not mem_dc or not bmp:
            return None
        prev_bmp = gdi32.SelectObject(mem_dc, bmp)
        if print_window:
            if int(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT) or 0) != 1:
                return None
        else:
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, hwnd_dc, 0, 0, SRCCOPY):
                return None
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负值：top-down，免去行序翻转
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        scanned = gdi32.GetDIBits(mem_dc, bmp, 0, height, buffer,
                                  ctypes.byref(info), DIB_RGB_COLORS)
        if scanned != height:
            return None
        return bytes(buffer), width, height
    except Exception:
        return None
    finally:
        if gdi32 is not None:
            # 必须先把位图选出 mem_dc 再 DeleteObject，否则 GDI 删除失败、
            # 每次截图泄漏一个 DIB 对象（长驻进程会累积 GDI 句柄与内存）。
            if bmp and mem_dc and prev_bmp:
                gdi32.SelectObject(mem_dc, prev_bmp)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if hwnd_dc is not None and user32 is not None:
                user32.ReleaseDC(hwnd, hwnd_dc)


def capture_window(hwnd, *, frame_grabber=None, strict_geometry=True):
    """抓取单个窗口**客户区**，返回 ``(bgra_bytes, width, height)``；失败返回 None。

    坐标系契约：图像的 (0,0) = 客户区的 (0,0)，且宽高等于客户区宽高。

    实现选型：**整窗 PrintWindow + 按非客户区偏移裁剪**（不用 PW_CLIENTONLY）。
    * PW_CLIENTONLY(0x1) 与 PW_RENDERFULLCONTENT(0x2) 的组合位（3）没有文档保证，
      部分 Windows 版本会整窗空白或忽略 CLIENTONLY，GPU/DWM 内容尤其不可靠；
    * 整窗 PrintWindow 已在本机实测能出图（含 DirectComposition 内容）；
    * 偏移不写死：每次由 GetWindowRect、GetClientRect、ClientToScreen(0,0) 现算
      （left = client_origin.x - window_rect.x；top 同理），DPI 变化自动跟随。

    尺寸不变量在返回前强制校验；不一致抛 CaptureGeometryError（strict_geometry=False
    时仅记录日志）。``frame_grabber(hwnd)`` 注入时直接使用注入实现（测试替身）。
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        return None
    if frame_grabber is not None:
        return frame_grabber(hwnd)
    user32, gdi32 = _libs()
    if user32 is None:
        return None
    dpi_info = ensure_capture_dpi(hwnd)
    geometry = _client_geometry(user32, hwnd)
    if not geometry:
        return None
    window_rect, client_rect, client_origin = geometry
    cw, ch = client_rect["width"], client_rect["height"]
    if cw <= 0 or ch <= 0:
        return None
    crop = compute_client_crop(window_rect, client_rect, client_origin)
    ww, wh = window_rect["width"], window_rect["height"]

    full = _render_window_bgra(hwnd, user32, gdi32, ww, wh, print_window=True)
    if full:
        path = "printwindow-full+crop"
        raw, out_w, out_h = crop_client_frame(full[0], full[1], full[2], crop)
    else:
        # PrintWindow 失败（GPU/最小化/旧系统）：回退客户区 BitBlt，该路径本身就是
        # 客户区图像，不再裁剪；尺寸不变量继续兜底。
        path = "bitblt-client"
        client_frame = _render_window_bgra(hwnd, user32, gdi32, cw, ch, print_window=False)
        if not client_frame:
            return None
        raw, out_w, out_h = client_frame
    try:
        assert_frame_matches_client(out_w, out_h, cw, ch, context="path=%s" % path)
    except CaptureGeometryError:
        if strict_geometry:
            raise
        logger.error("capture geometry mismatch tolerated: path=%s image=%dx%d client=%dx%d",
                     path, out_w, out_h, cw, ch)
    logger.info(
        "capture hwnd=%s path=%s client=%dx%d image=%dx%d crop=(%d,%d) "
        "nonclient_right=%d nonclient_bottom=%d dpi=%s awareness=%s",
        hwnd, path, cw, ch, out_w, out_h, crop["left"], crop["top"],
        crop["right"], crop["bottom"], dpi_info["dpi"], dpi_info["awareness"])
    return raw, out_w, out_h


def _embedded_target(project_id=None, *, strict_project=False):
    """从嵌入登记表选当前项目的子窗口 HWND；不接受外部传入任意 HWND。

    多候选时选 placed 面积最大者（引擎主视口通常大于 splash/工具窗）；
    登记表为空 / 窗口已死 / 无有效矩形时返回 None。
    """
    try:
        import desktop_bridge
    except Exception:
        return None
    children = [c for c in desktop_bridge.embedded_children()
                if c.get("alive") and (c.get("placed") or {}).get("width")]
    if not children:
        return None
    if strict_project:
        # Desktop bridge keeps a legacy default-host fallback. An approved
        # project action must never silently inherit another project's host.
        host = desktop_bridge.hosts_snapshot().get(project_id) if project_id else None
        if not host:
            return None
    else:
        host = desktop_bridge.host_hwnd(project_id) if project_id else None
    if host is not None:
        matched = [c for c in children if c.get("host") == host]
        if strict_project and not matched:
            return None
        if matched:
            children = matched

    def _area(item):
        placed = item.get("placed") or {}
        return int(placed.get("width") or 0) * int(placed.get("height") or 0)

    return int(max(children, key=_area)["hwnd"])


def grab_embedded(project_id=None, *, frame_grabber=None, strict_project=False):
    """抓取当前项目嵌入的引擎窗口，返回 (bgra, w, h, hwnd) 或 None。"""
    hwnd = _embedded_target(project_id, strict_project=strict_project)
    if not hwnd:
        return None
    frame = capture_window(hwnd, frame_grabber=frame_grabber)
    if not frame:
        return None
    raw, width, height = frame
    return raw, width, height, hwnd


def grab_foreground(*, frame_grabber=None):
    """抓取系统前台窗口，返回 (bgra, w, h, hwnd) 或 None。"""
    user32, _gdi = _libs()
    if user32 is None:
        return None
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    frame = capture_window(hwnd, frame_grabber=frame_grabber)
    if not frame:
        return None
    raw, width, height = frame
    return raw, width, height, hwnd


def save_encoded_jpeg(data_url, out_dir, tag="mcp"):
    """把已重编码的 JPEG data URL 落盘，返回保存路径；任何失败返回 None。

    连接器截图源与本地 GDI 抓帧走同一保存目录/时间戳命名，保证观察文本
    对所有成功源都给出可审计的文件路径。
    """
    try:
        import base64
        header, _, b64 = str(data_url or "").partition(",")
        if not b64 or "image/jpeg" not in header:
            return None
        payload = base64.b64decode(b64, validate=False)
        if not payload or payload[:2] != b"\xff\xd8":
            return None
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_tag = "".join(ch for ch in str(tag) if ch.isalnum() or ch in "-_")[:24] or "mcp"
        path = os.path.join(
            out_dir, f"shot-{stamp}-{safe_tag}-{random.randint(0, 9999):04d}.jpg")
        with open(path, "wb") as handle:
            handle.write(payload)
        return path
    except Exception:
        return None


def encode_and_save(raw, width, height, out_dir):
    """BGRA 像素 → 最长边 1600 JPEG q82，落盘并返回 (data_url, path, (w, h))。

    任何编码/落盘失败返回 None；调用方据此走「截图不可用」文案。
    """
    try:
        from PIL import Image
        img = Image.frombytes("RGB", (int(width), int(height)), bytes(raw),
                              "raw", "BGRX")
        if max(img.size) > CAPTURE_MAX_EDGE:
            img.thumbnail((CAPTURE_MAX_EDGE, CAPTURE_MAX_EDGE), Image.LANCZOS)
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(
            out_dir, f"shot-{stamp}-{random.randint(0, 9999):04d}.jpg")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        payload = buf.getvalue()
        with open(path, "wb") as handle:
            handle.write(payload)
        import base64
        data_url = "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")
        return data_url, path, img.size
    except Exception:
        return None


def encode_frame(raw, width, height):
    """把持续观察帧编码到内存，不在项目里反复生成截图文件。"""
    try:
        import base64
        from PIL import Image
        img = Image.frombytes("RGB", (int(width), int(height)), bytes(raw), "raw", "BGRX")
        if max(img.size) > CAPTURE_MAX_EDGE:
            img.thumbnail((CAPTURE_MAX_EDGE, CAPTURE_MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None
