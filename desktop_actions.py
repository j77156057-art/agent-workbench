"""Small, project-scoped Windows input bridge for approved desktop actions.

The module intentionally accepts only a resolved embedded, engine, or foreground
target. It does not enumerate arbitrary windows, run a shell, or expose HWND values
to the Agent. The "engine" target is resolved by the module itself from the project
root via game_workbench (this project's own standalone engine window) — it never
takes a caller-supplied handle, so it is not a backdoor to click any window.
Every action is one shot; callers must capture a fresh observation before issuing
the next action.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import time
from typing import Any


_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "shift": 0x10, "shift_l": 0x10, "control": 0x11, "ctrl": 0x11,
    "control_l": 0x11, "alt": 0x12, "alt_l": 0x12,
}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def _user32():
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = ctypes.wintypes.BOOL
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    return user32


def _engine_target(root):
    """把"本项目自己启动的引擎独立窗口"解析成句柄（0 表示没有）。

    找窗/还原逻辑全在 game_workbench.engine_window_target（按 root → 引擎进程表里的
    **本项目** pid → 该 pid 的顶层窗口），desktop_actions 只是转交 root 拿回句柄——
    **不接受外部传入的 hwnd**，保持"我们负责选窗、Agent 不点名"的安全不变式。
    """
    if not root:
        return 0
    try:
        import game_workbench
        found = game_workbench.engine_window_target(root)
        return int(found[0]) if found else 0
    except Exception:  # noqa: BLE001 - 解析不了就是"没有目标窗口"，绝不猜别的窗
        return 0


def _resolve_target(target: str, project_id: str | None = None, root: str | None = None):
    if os.name != "nt":
        return None, None, "桌面操作仅支持 Windows。"
    try:
        import desktop_bridge
        import screen_capture
        if target == "embedded":
            hwnd = screen_capture._embedded_target(project_id)
        elif target == "engine":
            hwnd = _engine_target(root)
        else:
            hwnd = int(_user32().GetForegroundWindow() or 0)
        if not hwnd or not desktop_bridge.is_window(hwnd):
            return None, None, "没有找到可操作的目标窗口。"
        origin = desktop_bridge.client_origin(hwnd) or desktop_bridge.window_rect(hwnd)
        rect = desktop_bridge.client_rect(hwnd)
        if not origin or not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
            return None, None, "无法读取目标窗口客户区。"
        return hwnd, {"origin": origin, "rect": rect}, ""
    except Exception as exc:  # noqa: BLE001
        return None, None, "目标窗口解析失败（%s）。" % type(exc).__name__


def _focus(hwnd: int, target: str, project_id: str | None = None) -> tuple[bool, str]:
    try:
        import desktop_bridge
        if target in {"embedded", "engine"}:
            host = desktop_bridge.host_hwnd(project_id) if target == "embedded" else hwnd
            result = desktop_bridge.focus(hwnd, hwnd_host=host, keep_attached=False)
            return bool(result.get("ok")), str(result.get("error") or "")
        ok = bool(_user32().SetForegroundWindow(ctypes.c_void_p(hwnd)))
        return ok, "" if ok else "无法激活前台窗口"
    except Exception as exc:  # noqa: BLE001
        return False, "窗口激活失败（%s）。" % type(exc).__name__


def _send(events: list[_INPUT]) -> tuple[bool, int]:
    user32 = _user32()
    if user32 is None or not events:
        return False, 0
    array = (_INPUT * len(events))(*events)
    inserted = int(user32.SendInput(len(events), array, ctypes.sizeof(_INPUT)))
    return inserted == len(events), inserted


def _mouse_click(x: int, y: int) -> tuple[bool, int]:
    user32 = _user32()
    if user32 is None or not user32.SetCursorPos(int(x), int(y)):
        return False, 0
    down = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, None))
    up = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, None))
    return _send([down, up])


def _mouse_drag(x1: int, y1: int, x2: int, y2: int) -> tuple[bool, int]:
    user32 = _user32()
    if user32 is None or not user32.SetCursorPos(int(x1), int(y1)):
        return False, 0
    down = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, None))
    up = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, None))
    ok_down, count_down = _send([down])
    time.sleep(0.08)
    user32.SetCursorPos(int(x2), int(y2))
    ok_up, count_up = _send([up])
    return ok_down and ok_up, count_down + count_up


def _unicode_type(text: str) -> tuple[bool, int]:
    raw = str(text or "")
    if not raw or len(raw) > 8000:
        return False, 0
    units = raw.encode("utf-16-le", "surrogatepass")
    events = []
    for index in range(0, len(units), 2):
        scan = int.from_bytes(units[index:index + 2], "little")
        events.append(_INPUT(type=1, ki=_KEYBDINPUT(0, scan, _KEYEVENTF_UNICODE, 0, None)))
        events.append(_INPUT(type=1, ki=_KEYBDINPUT(0, scan, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None)))
    return _send(events)


def _key_code(name: str) -> int | None:
    value = str(name or "").strip().lower()
    if value in _VK:
        return _VK[value]
    if len(value) == 1 and value.isalnum():
        return ord(value.upper())
    return None


def _press_key(key: str) -> tuple[bool, int]:
    parts = [part.strip() for part in str(key or "").split("+") if part.strip()]
    codes = [_key_code(part) for part in parts]
    if not parts or any(code is None for code in codes):
        return False, 0
    downs = [_INPUT(type=1, ki=_KEYBDINPUT(code, 0, 0, 0, None)) for code in codes]
    ups = [_INPUT(type=1, ki=_KEYBDINPUT(code, 0, _KEYEVENTF_KEYUP, 0, None)) for code in reversed(codes)]
    return _send(downs + ups)


def _inside(point: tuple[int, int], rect: dict[str, Any]) -> bool:
    x, y = point
    return 0 <= x < int(rect.get("width", 0)) and 0 <= y < int(rect.get("height", 0))


def _describe_point(x: int, y: int, rect: dict[str, Any]) -> dict[str, Any]:
    """把客户区坐标换成一个**可核对的位置描述**。

    实测的幻觉：853×480 的画面里点 (640, 360)，模型说成「画面中央」——
    实际是 75%/75% 处（右下）。只给裸坐标，模型很容易凭感觉形容位置；
    这里把比例和九宫格区域一起返回，叙述就必须对得上数字。
    """
    x, y = int(x), int(y)
    width = max(1, int(rect.get("width") or 1))
    height = max(1, int(rect.get("height") or 1))
    col = min(2, max(0, (3 * x) // width))
    row = min(2, max(0, (3 * y) // height))
    names = (("左上", "上", "右上"), ("左", "中", "右"), ("左下", "下", "右下"))
    return {"x": x, "y": y, "width": width, "height": height,
            "x_ratio": round(x / width, 3), "y_ratio": round(y / height, 3),
            "region": names[row][col]}


def perform(action: str, *, target: str = "embedded", project_id: str | None = None,
            root: str | None = None,
            x: int | None = None, y: int | None = None,
            to_x: int | None = None, to_y: int | None = None,
            text: str = "", key: str = "", expected_hwnd: int | None = None) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action == "save":
        key = "Control_L+s"
        action = "key"
    if action not in {"click", "type", "drag", "key"}:
        return {"ok": False, "error": "action 只能是 click、type、drag、key 或 save。"}
    hwnd, info, error = _resolve_target(target, project_id, root)
    if error:
        return {"ok": False, "error": error}
    if expected_hwnd is not None and hwnd != expected_hwnd:
        return {"ok": False, "error": "目标窗口在操作前已切换。"}
    focused, focus_error = _focus(hwnd, target, project_id)
    if not focused:
        return {"ok": False, "error": focus_error or "目标窗口未能获得焦点。"}
    if expected_hwnd is not None:
        current_hwnd, current_info, current_error = _resolve_target(target, project_id, root)
        if current_error or current_hwnd != expected_hwnd or current_info["rect"] != info["rect"]:
            return {"ok": False, "error": "目标窗口或尺寸在激活后已变化。"}
        info = current_info
    origin, rect = info["origin"], info["rect"]
    ox, oy = int(origin.get("x", 0)), int(origin.get("y", 0))
    extra: dict[str, Any] = {}
    if action == "click":
        if x is None or y is None or not _inside((int(x), int(y)), rect):
            return {"ok": False, "error": "click 坐标必须位于目标窗口客户区内。"}
        extra["point"] = _describe_point(int(x), int(y), rect)
        ok, count = _mouse_click(ox + int(x), oy + int(y))
    elif action == "drag":
        if any(value is None for value in (x, y, to_x, to_y)) or not _inside((int(x), int(y)), rect) or not _inside((int(to_x), int(to_y)), rect):
            return {"ok": False, "error": "drag 起点和终点必须位于目标窗口客户区内。"}
        extra["from"] = _describe_point(int(x), int(y), rect)
        extra["to"] = _describe_point(int(to_x), int(to_y), rect)
        ok, count = _mouse_drag(ox + int(x), oy + int(y), ox + int(to_x), oy + int(to_y))
    elif action == "type":
        ok, count = _unicode_type(text)
    else:
        ok, count = _press_key(key)
    return {"ok": bool(ok), "action": action, "target": target,
            "events": count, "window": {"width": rect["width"], "height": rect["height"]},
            "error": "" if ok else "Windows 未确认全部输入事件已送达，请重新观察后再决定是否重试。",
            **extra}


__all__ = ["perform"]
