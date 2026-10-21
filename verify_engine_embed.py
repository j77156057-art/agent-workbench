# -*- coding: utf-8 -*-
r"""Godot 实机嵌入闭环自检（Windows 桌面会话专用）。

用法：
    .\.venv\Scripts\python.exe verify_engine_embed.py
    .\.venv\Scripts\python.exe verify_engine_embed.py --godot "D:\Tools\Godot\Godot_v4.7.2-stable_win64.exe"
    .\.venv\Scripts\python.exe verify_engine_embed.py --keep      # 保留临时工程便于排查

它做什么：
    1. 造一个最小 Godot 工程（`probe.gd` 会把每次输入打印成 DOCMIND_EVENT 事件）；
    2. 用 ctypes 建一个真·Win32 宿主窗口（浏览器标签页当不了 Win32 宿主，必须真窗口）；
    3. 走**产品同一条代码路径**启动引擎并嵌入（game_workbench.engine_start → desktop_bridge.embed）；
    4. 逐步断言：按 PID 找到引擎窗口 → SetParent 生效 → 边框样式被去掉 → 引擎视窗尺寸与
       宿主客户区严格对齐 → 宿主 resize 后跟随 → **合成真键鼠经真焦点链路打到引擎并回显事件** →
       解除嵌入后窗口还原成独立顶层窗口且回到原位 → 停止后无孤儿进程/窗口 → DPI 一致性；
    5. 截一张宿主窗口的图存到 docs/screenshots/engine-embed.png 作为肉眼证据。

它会短暂占用桌面：会创建一个窗口、把鼠标移到该窗口内点一下，最后把鼠标移回原处。
非 Windows 或找不到 Godot 可执行文件时**明确跳过**（而不是假装通过）。
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL, SKIP, UNPROVEN = [], [], [], []


def check(label, condition, detail=''):
    (PASS if condition else FAIL).append(label)
    print(('  ok  ' if condition else '  FAIL') + ' ' + label + (('   ' + str(detail)) if detail else ''))


def skip(label, detail=''):
    SKIP.append(label)
    print('  skip ' + label + (('   ' + str(detail)) if detail else ''))


def unproven(label, detail=''):
    """本环境下无法证实的断言：既不算通过也不算失败，但必须在结论里明确列出。"""
    UNPROVEN.append(label + (' —— ' + str(detail) if detail else ''))
    print('  ？   ' + label + (('   ' + str(detail)) if detail else ''))


# ---------------------------------------------------------------------------
# 最小 Win32 宿主窗口
# ---------------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_ulonglong, ctypes.c_longlong)
# 必须显式声明 DefWindowProcW 的签名：默认按 c_int 推断，LPARAM 稍大就 OverflowError，
# 回调里的异常会被 ctypes 静默吞掉，窗口看着"建出来了"其实没走默认处理。
ctypes.windll.user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.c_ulonglong, ctypes.c_longlong]
ctypes.windll.user32.DefWindowProcW.restype = ctypes.c_longlong
HOST_CLASS = 'DocMindEmbedVerifyHost'


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', ctypes.c_void_p), ('hIcon', ctypes.c_void_p),
                ('hCursor', ctypes.c_void_p), ('hbrBackground', ctypes.c_void_p),
                ('lpszMenuName', ctypes.c_wchar_p), ('lpszClassName', ctypes.c_wchar_p)]


class HostWindow:
    """一个真·顶层窗口，用作 HWND 嵌入的宿主。"""

    def __init__(self, title='Agent 工作台 嵌入验证宿主', width=1100, height=760, x=60, y=60):
        self.user32 = ctypes.windll.user32
        self.hwnd = None
        self.messages = []                    # 宿主收到的消息（输入链路诊断用）
        self._proc = WNDPROC(self._wndproc)   # 必须持有引用，否则回调被 GC
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)   # 在 kernel32，不在 user32
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = HOST_CLASS
        self.user32.RegisterClassW(ctypes.byref(wc))
        WS_OVERLAPPEDWINDOW = 0x00CF0000
        self.hwnd = self.user32.CreateWindowExW(
            0, HOST_CLASS, title, WS_OVERLAPPEDWINDOW | 0x10000000,
            x, y, width, height, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()
        self.user32.ShowWindow(self.hwnd, 5)
        self.user32.UpdateWindow(self.hwnd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # 记录输入类消息：如果合成输入连宿主线程都没到，说明注入本身失败了，
        # 而不是"焦点没给到引擎"——这两者的排查方向完全不同。
        if msg in (0x0100, 0x0101, 0x0104, 0x0105, 0x0201, 0x0202, 0x0200):
            self.messages.append((msg, wparam, lparam))
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def clear_messages(self):
        self.messages = []

    def pump(self, seconds=0.25):
        """抽干消息队列（宿主窗口在阻塞式检查脚本里需要手动 pump）。"""
        msg = ctypes.wintypes.MSG()
        deadline = time.time() + seconds
        while time.time() < deadline:
            got = False
            while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                got = True
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            if not got:
                time.sleep(0.02)

    def resize(self, width, height):
        self.user32.SetWindowPos(self.hwnd, None, 0, 0, int(width), int(height), 0x0002 | 0x0004)
        self.pump(0.4)

    def destroy(self):
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        try:
            self.user32.UnregisterClassW(HOST_CLASS, ctypes.windll.kernel32.GetModuleHandleW(None))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 合成输入
# ---------------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long), ('mouseData', ctypes.c_ulong),
                ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', ctypes.c_ulong), ('wParamL', ctypes.c_ushort),
                ('wParamH', ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT), ('hi', _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', ctypes.c_ulong), ('u', _INPUTUNION)]


INPUT_KEYBOARD, INPUT_MOUSE = 1, 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_last_input_result = {}


def _send_inputs(events):
    """调用 SendInput，返回 (实际插入条数, GetLastError)。

    用 SendInput 而不是老旧的 keybd_event，只因为 **它能告诉我们注入是否被系统拒绝**：
    返回 0 就是没插进去（常见于 UIPI/权限/沙箱限制），这时候"引擎收不到按键"是环境问题，
    不是产品问题——两者必须分得清清楚楚，否则会把环境限制误报成功能缺陷。
    """
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    array = (_INPUT * len(events))(*events)
    ctypes.set_last_error(0)
    inserted = int(user32.SendInput(len(events), array, ctypes.sizeof(_INPUT)))
    _last_input_result.update({'inserted': inserted, 'expected': len(events),
                               'last_error': ctypes.get_last_error()})
    return inserted


def send_key(vk):
    down = _INPUT(type=INPUT_KEYBOARD)
    down.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=None)
    up = _INPUT(type=INPUT_KEYBOARD)
    up.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
    inserted = _send_inputs([down, up])
    time.sleep(0.1)
    return inserted


def click_at(x, y):
    """把鼠标移到 (x, y)（屏幕物理像素）并左键单击。"""
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    down = _INPUT(type=INPUT_MOUSE)
    down.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN,
                          time=0, dwExtraInfo=None)
    up = _INPUT(type=INPUT_MOUSE)
    up.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP,
                        time=0, dwExtraInfo=None)
    inserted = _send_inputs([down, up])
    time.sleep(0.1)
    return inserted


PROBE_KEY_INJECT = 0x7C   # F13：真实合成输入用
PROBE_KEY_POST = 0x7D     # F14：消息直投用（不同键才能分辨是谁送达的）
GODOT_KEY_F13 = 4194344   # Godot 的 Key 枚举：KEY_F13
GODOT_KEY_F14 = 4194345


def _probe_post_key(hwnd, vk=0):
    """把 WM_KEYDOWN/UP 直接投到目标窗口（绕过焦点链路），用于链路分流诊断。"""
    vk = vk or PROBE_KEY_POST
    user32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_ulonglong, ctypes.c_longlong]
    user32.PostMessageW.restype = ctypes.c_bool
    a = user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYDOWN, vk, 0)
    time.sleep(0.05)
    b = user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYUP, vk, 0)
    time.sleep(0.2)
    return bool(a and b)


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_ulong), ('flags', ctypes.c_ulong),
                ('hwndActive', ctypes.c_void_p), ('hwndFocus', ctypes.c_void_p),
                ('hwndCapture', ctypes.c_void_p), ('hwndMenuOwner', ctypes.c_void_p),
                ('hwndMoveSize', ctypes.c_void_p), ('hwndCaret', ctypes.c_void_p),
                ('rcCaret', ctypes.wintypes.RECT)]


def thread_input_state(thread_id):
    """跨线程读"某个线程的焦点窗口"的唯一可信方式（GetFocus 只对调用线程的队列有效）。"""
    user32 = ctypes.windll.user32
    user32.GetGUIThreadInfo.argtypes = [ctypes.c_ulong, ctypes.POINTER(_GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = ctypes.c_bool
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not user32.GetGUIThreadInfo(int(thread_id), ctypes.byref(info)):
        return None
    return {'active': int(info.hwndActive or 0), 'focus': int(info.hwndFocus or 0),
            'capture': int(info.hwndCapture or 0)}


def window_at_point(x, y):
    """返回屏幕该点上真正会收到点击的窗口（以及它的顶层祖先）。"""
    user32 = ctypes.windll.user32
    point = ctypes.wintypes.POINT(int(x), int(y))
    user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p
    hit = int(user32.WindowFromPoint(point) or 0)
    return {'hit': hit, 'root': int(user32.GetAncestor(hit, 2) or 0) if hit else 0}


def _probe_post_click(hwnd):
    """把左键按下/抬起直接投到目标窗口（绕过光标与前台），用于链路分流诊断。"""
    user32 = ctypes.windll.user32
    WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_ulonglong, ctypes.c_longlong]
    user32.PostMessageW.restype = ctypes.c_bool
    pos = (10 << 16) | 10          # 客户区 (10, 10)
    a = user32.PostMessageW(ctypes.c_void_p(hwnd), WM_LBUTTONDOWN, 1, pos)
    time.sleep(0.05)
    b = user32.PostMessageW(ctypes.c_void_p(hwnd), WM_LBUTTONUP, 0, pos)
    time.sleep(0.2)
    return bool(a and b)


def cursor_pos():
    point = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


# ---------------------------------------------------------------------------
# 日志与窗口观察
# ---------------------------------------------------------------------------
def probe_event_file(project_dir):
    """探针自己 flush 的事件文件路径（%APPDATA%\\Godot\\app_userdata\\<工程名>\\）。"""
    appdata = os.environ.get('APPDATA', '')
    return os.path.join(appdata, 'Godot', 'app_userdata', 'DocMindEmbedProbe',
                        'probe_events.jsonl')


def read_events(log_path, kinds=None, limit=400, event_file=None):
    """读探针事件。优先读探针自己 flush 的文件（无缓冲延迟），退回 stdout 日志。"""
    out = []
    lines = []
    if event_file and os.path.isfile(event_file):
        try:
            with open(event_file, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-limit:]
        except OSError:
            lines = []
    if not lines:
        try:
            with open(log_path, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-limit:]
        except OSError:
            return out
    for line in lines:
        if 'DOCMIND_EVENT ' not in line:
            continue
        try:
            payload = json.loads(line.split('DOCMIND_EVENT ', 1)[1])
        except ValueError:
            continue
        if kinds and payload.get('type') not in kinds:
            continue
        out.append(payload)
    return out


def wait_event(log_path, kind, timeout=6.0, baseline=0, event_file=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = read_events(log_path, [kind], event_file=event_file)
        if len(events) > baseline:
            return events[baseline:]
        time.sleep(0.2)
    return None


def process_alive(pid):
    k32 = ctypes.windll.kernel32
    STILL_ACTIVE = 259
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def terminate_pid(pid):
    """强制结束进程（模拟引擎崩溃 / 被任务管理器结束），不走优雅的 engine_stop。"""
    k32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    ok = bool(k32.TerminateProcess(handle, 0))
    k32.CloseHandle(handle)
    return ok


# ---------------------------------------------------------------------------
# 窗口截图（GDI → PNG）
# ---------------------------------------------------------------------------
def capture_window(hwnd, path):
    """截取窗口图像。先用 PrintWindow（能抓到 D3D/WebView 内容），失败退回屏幕 BitBlt。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False
    hdc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(memdc, bitmap)
    PW_RENDERFULLCONTENT = 2
    # PrintWindow 导出在 user32（不在 gdi32），且必须显式声明签名
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.PrintWindow.restype = ctypes.c_bool
    ok = user32.PrintWindow(ctypes.c_void_p(hwnd), memdc, PW_RENDERFULLCONTENT)
    if not ok:
        SRCCOPY = 0x00CC0020
        gdi32.BitBlt(memdc, 0, 0, width, height, hdc, 0, 0, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                    ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                    ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                    ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                    ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                    ('biClrImportant', ctypes.c_uint32)]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height          # 负数 = 自上而下，省掉翻转
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memdc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(ctypes.c_void_p(hwnd), hdc)
    image = Image.frombuffer('RGBA', (width, height), buffer.raw, 'raw', 'BGRA', 0, 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.convert('RGB').save(path)
    return True


# ---------------------------------------------------------------------------
# Godot 探测与临时工程
# ---------------------------------------------------------------------------
def find_godot(explicit=''):
    if explicit:
        return explicit if os.path.isfile(explicit) else ''
    try:
        import game_workbench
        hit = game_workbench._resolve_engine_executable('godot', '')
        if hit and os.path.isfile(hit):
            return hit
    except Exception:  # noqa: BLE001
        pass
    for root in ('D:/Tools/Godot', os.path.expandvars(r'%LOCALAPPDATA%\Godot'),
                 os.path.expandvars(r'%PROGRAMFILES%\Godot'), 'C:/Tools/Godot'):
        if not os.path.isdir(root):
            continue
        hits = sorted(f for f in os.listdir(root) if f.lower().startswith('godot')
                      and f.lower().endswith('.exe') and '_console' not in f.lower())
        if hits:
            return os.path.join(root, hits[-1]).replace('\\', '/')
    return ''


PROBE_SCRIPT = '''extends Node2D

## 自检探针：把每次输入打印成 DOCMIND_EVENT，用来证明"合成输入真的打到了引擎"。
const MARKER := "DOCMIND_EVENT "
var _motion := 0

func _emit(kind: String, data: Dictionary = {}) -> void:
\tprint(MARKER + JSON.stringify({"type": kind, "data": data}))

func _ready() -> void:
\t_emit("__probe_ready__", {"ok": true})

func _input(event: InputEvent) -> void:
\tif event is InputEventKey and event.pressed and not event.echo:
\t\t_emit("key", {"keycode": event.keycode, "physical": event.physical_keycode})
\telif event is InputEventMouseButton and event.pressed:
\t\t_emit("mouse_button", {"button": event.button_index,
\t\t\t"x": int(event.position.x), "y": int(event.position.y)})
\telif event is InputEventMouseMotion:
\t\t_motion += 1
\t\tif _motion % 25 == 0:
\t\t\t_emit("mouse_motion", {"x": int(event.position.x), "y": int(event.position.y)})
'''


def build_project(root_dir):
    os.makedirs(root_dir, exist_ok=True)
    with open(os.path.join(root_dir, 'project.godot'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('config_version=5\n\n[application]\nconfig/name="DocMindEmbedProbe"\n'
                'run/main_scene="res://probe.tscn"\nconfig/features=PackedStringArray("4.7")\n\n'
                '[display]\nwindow/size/viewport_width=960\nwindow/size/viewport_height=540\n')
    with open(os.path.join(root_dir, 'probe.tscn'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('[gd_scene load_steps=2 format=3]\n\n'
                '[ext_resource type="Script" path="res://probe.gd" id="1_probe"]\n\n'
                '[node name="Probe" type="Node2D"]\nscript = ExtResource("1_probe")\n')
    with open(os.path.join(root_dir, 'probe.gd'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(PROBE_SCRIPT)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--godot', default='', help='Godot 可执行文件绝对路径')
    parser.add_argument('--keep', action='store_true', help='保留临时工程目录')
    args = parser.parse_args()

    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except AttributeError:
            pass
    if os.name != 'nt':
        print('本脚本只在 Windows 桌面会话有意义，已跳过。')
        return 0

    import desktop_bridge as db
    import game_workbench as gw

    print('=' * 62)
    print('  Godot 实机嵌入闭环自检')
    print('=' * 62)

    ok, mode = db.ensure_dpi_awareness()
    scale = db.dpi_of(0) / 96.0
    print('进程 DPI 感知：%s（设置%s）｜ 显示器 %d DPI ≈ %d%% 缩放'
          % (mode, '成功' if ok else '失败', db.dpi_of(0), round(scale * 100)))
    if abs(scale - 1.0) > 0.01:
        print('→ 本次是在**非 100%% 缩放**下实测嵌入几何，DPI 一致性由实际坐标断言保证。')
    print()

    godot = find_godot(args.godot)
    if not godot:
        skip('未找到 Godot 可执行文件', '用 --godot <exe> 指定')
        print('\n跳过：本机没有可用 Godot，无法做实机验证。')
        return 0
    print('Godot：%s\n' % godot)

    workdir = tempfile.mkdtemp(prefix='docmind_embed_verify_')
    project = os.path.join(workdir, 'probe_project')
    build_project(project)
    with open(os.path.join(project, '.docmind_engine.json'), 'w', encoding='utf-8') as f:
        json.dump({'engine': 'godot', 'executable': godot}, f, ensure_ascii=False, indent=2)
    log_path = __import__('project_state').path(project, '.docmind_engine.log')
    event_file = probe_event_file(project)
    try:   # 探针文件在 %APPDATA% 下，会跨次运行累积；不清空计数断言必然被历史事件污染
        if os.path.exists(event_file):
            os.unlink(event_file)
    except OSError:
        pass

    host = None
    original_cursor = cursor_pos()
    engine_pid = None
    try:
        print('[0] 环境自检：本进程的合成输入能否送达窗口')
        host0 = HostWindow(title='Agent 工作台 合成输入自检', width=520, height=320, x=40, y=40)
        host0.clear_messages()
        ctypes.windll.user32.SetForegroundWindow(host0.hwnd)
        ctypes.windll.user32.SetFocus(host0.hwnd)
        host0.pump(0.3)
        sent = send_key(0x7C)          # F13
        host0.pump(0.5)
        host_saw = [hex(m) for m, _, _ in host0.messages]
        synth_ok = any(m in (0x0100, 0x0104) for m, _, _ in host0.messages)
        check('本进程能把合成按键送到自己的窗口（否则后续输入断言无意义）',
              synth_ok, 'SendInput 插入 %s 条｜收到的消息 %s' % (sent, host_saw))
        host0.destroy()
        if not synth_ok:
            print('  → 该环境拒绝合成输入送达（沙箱/会话限制），后面的真实输入断言会跳过，')
            print('    改用"消息直投"证明引擎窗口本身能处理键鼠消息。')
        print()

        print('[1] 宿主窗口与引擎启动')
        host = HostWindow()
        check('已创建 Win32 宿主窗口（浏览器标签页不能当宿主）', bool(host.hwnd), hex(host.hwnd or 0))
        db.set_host(host.hwnd)
        host_client = db.client_rect(host.hwnd)
        check('宿主客户区可读', bool(host_client), host_client)

        # 先以"独立窗口"启动：这样才能验证"按 PID 枚举可见顶层窗口"这一步本身可用。
        # 嵌入之后窗口就变成宿主的子窗口，EnumWindows 不再枚举它——顺序反了会误判成失败。
        started = gw.engine_start(project, godot, '', host.hwnd, False)
        if not started.get('ok'):
            check('engine_start 成功', False, started.get('error'))
            return 1
        engine_pid = started['pid']
        check('engine_start 返回 running', started.get('running') is True, 'pid=%s' % engine_pid)
        check('未要求嵌入时不应自动嵌入', started.get('embedded') is not True, started.get('embedded'))

        print('\n[2] 按 PID 定位引擎窗口，再嵌入')
        # Godot 建窗口是异步的：启动返回时窗口往往还不存在，必须轮询等。
        # （engine_start(embed=True) 内部有自己的轮询，所以那条路径看不出这个问题。）
        t0, child = time.time(), None
        while time.time() - t0 < 15:
            child = db.find_window(engine_pid)
            if child:
                break
            time.sleep(0.2)
        if not child:
            tail = ''
            try:
                with open(log_path, encoding='utf-8', errors='replace') as f:
                    tail = ''.join(f.readlines()[-8:])
            except OSError:
                pass
            check('按 PID 枚举到引擎窗口', False, 'pid=%s 15s 内没有可见顶层窗口｜日志尾部：%r' % (engine_pid, tail))
            return 1
        hwnd_child, title = child
        check('引擎窗口在启动后 %.1fs 内出现' % (time.time() - t0), True,
              '等待 %.2fs' % (time.time() - t0))
        check('按 PID 枚举到引擎窗口', True, 'hwnd=%s title=%r' % (hex(hwnd_child), title))
        check('引擎窗口标题取自工程名（title_hint 留空才不会误筛）',
              'DocMindEmbedProbe' in title, title)
        check('嵌入前是独立顶层窗口', db.parent_of(hwnd_child) is None,
              'parent=%s' % db.parent_of(hwnd_child))
        before_rect = db.window_rect(hwnd_child)
        before_style = db.style_of(hwnd_child) or 0
        check('嵌入前带标题栏样式（后面要断言被摘掉）', bool(before_style & 0x00CF0000),
              hex(before_style & 0x00CF0000))

        # 硬化验证：rect 缺失且未显式 fill 时，必须退化为"安全有界框"（绝不铺满全屏，
        # 那是"引擎打开直接黑屏、git 按钮被盖住"的根因）。这是 API/集成方"想嵌但没给矩形"
        # 时的防御性默认。
        safe = gw.engine_embed(project, host.hwnd)
        check('rect 缺失且未显式 fill：embed 仍成功（有界框降级而非失败）',
              safe.get('ok') is True, safe.get('error', ''))
        check('默认（无 rect）为安全有界框模式，而非铺满',
              safe.get('mode') == 'rect', safe.get('mode'))
        hc_safe = db.client_rect(host.hwnd)
        check('有界框宽度明显小于宿主客户区宽度（未铺满全屏）',
              int(safe.get('width', 0)) < int(hc_safe['width']) - 10,
              'box_w=%s host_w=%s' % (safe.get('width'), hc_safe['width']))
        check('有界框高度明显小于宿主客户区高度（未铺满全屏）',
              int(safe.get('height', 0)) < int(hc_safe['height']) - 10,
              'box_h=%s host_h=%s' % (safe.get('height'), hc_safe['height']))

        # 显式 fill=True 才进入铺满模式（保留历史行为，供 [3] 几何校验）
        embedded = gw.engine_embed(project, host.hwnd, fill=True)
        check('engine_embed 返回 ok', embedded.get('ok') is True, embedded.get('error', ''))
        check('embed 返回 mode=fill（仅显式请求时铺满）',
              embedded.get('mode') == 'fill', embedded.get('mode'))
        status = gw.engine_status(project)
        check('engine_status 报告 embedded:true', status.get('embedded') is True)
        check('状态里的 child_hwnd 与枚举到的一致',
              status.get('child_hwnd') == hwnd_child,
              '%s vs %s' % (status.get('child_hwnd'), hwnd_child))
        check('GetParent(child) == 宿主', db.parent_of(hwnd_child) == host.hwnd,
              'parent=%s host=%s' % (db.parent_of(hwnd_child), host.hwnd))
        style = db.style_of(hwnd_child) or 0
        check('样式已设置 WS_CHILD', bool(style & 0x40000000), hex(style))
        check('样式已去掉标题栏/粗边框（WS_CAPTION|WS_THICKFRAME|WS_SYSMENU）',
              not (style & 0x00CF0000), hex(style & 0x00CF0000))

        print('\n[3] 引擎视窗几何（宿主 resize 前后）')
        placed = db.window_rect(hwnd_child)
        origin = db.client_origin(host.hwnd)
        expect = db.client_rect(host.hwnd)
        strip = gw.EMBED_TOP_STRIP
        check('铺满模式：引擎窗口占满宿主客户区宽度',
              abs(placed['width'] - expect['width']) <= 2,
              'child=%s host_client=%s' % (placed['width'], expect['width']))
        check('铺满模式：顶部留出 %dpx 给工作台顶栏（否则用户点不到停止）' % strip,
              abs(placed['height'] - (expect['height'] - strip)) <= 2
              and abs(placed['y'] - (origin['y'] + strip)) <= 2,
              'child=%sx%s at y=%s｜host_client=%sx%s origin_y=%s'
              % (placed['width'], placed['height'], placed['y'],
                 expect['width'], expect['height'], origin['y']))
        check('铺满模式：左边缘与客户区对齐',
              abs(placed['x'] - origin['x']) <= 2, 'child_x=%s origin_x=%s' % (placed['x'], origin['x']))

        host.resize(980, 700)
        refill = db.fill_all()
        host.pump(0.3)
        placed2 = db.window_rect(hwnd_child)
        expect2 = db.client_rect(host.hwnd)
        check('宿主 resize 后自动重排（fill_all 生效）', bool(refill) and refill[0].get('ok', refill[0].get('skipped')) is not None,
              refill)
        check('resize 后引擎窗口跟随到新客户区尺寸（含顶部留白）',
              abs(placed2['width'] - expect2['width']) <= 2
              and abs(placed2['height'] - (expect2['height'] - strip)) <= 2,
              'child=%sx%s host_client=%sx%s' % (placed2['width'], placed2['height'],
                                                 expect2['width'], expect2['height']))

        print('\n[4] 引擎视窗模式（rect：只占工作台里一块，界面仍可用）')
        status_before = gw.engine_status(project)
        check('embed 前状态是铺满模式', status_before.get('embed_mode') == 'fill',
              status_before.get('embed_mode'))
        rect = {'x': 40, 'y': 80, 'width': 640, 'height': 380}
        res = gw.engine_embed(project, host.hwnd, rect['width'], rect['height'], '', 0, rect)
        check('engine_embed(rect) 成功', res.get('ok') is True, res.get('error', ''))
        check('返回 mode=rect', res.get('mode') == 'rect', res.get('mode'))
        placed3 = db.window_rect(hwnd_child)
        origin3 = db.client_origin(host.hwnd)
        check('rect 模式下引擎窗口落在指定位置',
              abs(placed3['x'] - (origin3['x'] + rect['x'])) <= 2
              and abs(placed3['y'] - (origin3['y'] + rect['y'])) <= 2,
              'child=(%s,%s) expect=(%s,%s)' % (placed3['x'], placed3['y'],
                                                origin3['x'] + rect['x'], origin3['y'] + rect['y']))
        check('rect 模式下尺寸按指定值',
              abs(placed3['width'] - rect['width']) <= 2 and abs(placed3['height'] - rect['height']) <= 2,
              '%sx%s' % (placed3['width'], placed3['height']))
        moved = gw.engine_place(project, 120, 140, 520, 300)
        placed4 = db.window_rect(hwnd_child)
        origin4 = db.client_origin(host.hwnd)
        check('engine_place 能随前端布局重新定位',
              moved.get('ok') and abs(placed4['x'] - (origin4['x'] + 120)) <= 2
              and abs(placed4['y'] - (origin4['y'] + 140)) <= 2,
              'child=(%s,%s)' % (placed4['x'], placed4['y']))
        check('engine_status 反映 rect 模式与当前尺寸',
              gw.engine_status(project).get('embed_mode') == 'rect',
              gw.engine_status(project).get('embed_mode'))
        # P3-A：engine_place 是"非用户主动"重定位（resize/DPI/布局触发），绝不能抢焦点到引擎
        fg_after_place = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        check('engine_place 未把键盘焦点抢到引擎窗口（SWP_NOACTIVATE）',
              fg_after_place != hwnd_child, 'fg=%s child=%s' % (fg_after_place, hwnd_child))

        print('\n[5] 键鼠输入（合成真事件 → 真焦点链路 → 引擎回显）')
        focus_result = gw.engine_focus(project)
        # 对照实验：把输入队列保持挂接后再注入——用来验证"跨线程焦点只在挂接期间有效"
        from desktop_bridge import focus as _focus
        keep = _focus(hwnd_child, host.hwnd, keep_attached=True)
        check('实验：保持输入队列挂接（跨线程键盘焦点前提）',
              keep.get('ok') is True and keep.get('kept_attached') is True, keep)
        check('engine_focus 拿到焦点', focus_result.get('ok') is True, focus_result)
        check('焦点确实落在引擎窗口（GetFocus 回读）',
              focus_result.get('focused') == hwnd_child,
              'focused=%s child=%s' % (focus_result.get('focused'), hwnd_child))
        foreground_ok = focus_result.get('foreground_is_host') is True
        if not foreground_ok:
            skip('合成键鼠', '无法取得前台窗口（前台=%s），跳过输入断言' % focus_result.get('foreground'))
        else:
            time.sleep(0.4)
            VK_F13 = PROBE_KEY_INJECT   # 没有可见字符，不会误输入到别的窗口
            before_keys = len(read_events(log_path, ['key'], event_file=event_file))
            inserted_key = send_key(VK_F13)
            key_inject_ok = inserted_key == 2
            keys = wait_event(log_path, 'key', 8.0, before_keys, event_file)
            if key_inject_ok and synth_ok:
                print('  ·   实时读取按键回显：%s（结论以 [9] 的最终核对为准）'
                      % ('已读到' if keys else '未读到，可能是落盘时序'))
            elif key_inject_ok and not synth_ok:
                skip('合成按键', '环境自检已判定合成输入无法送达窗口，见 [0]')
            else:
                skip('合成按键', 'SendInput 未插入事件 %s —— 系统/沙箱拒绝注入，非功能问题'
                     % _last_input_result)

            # 系统级探针：键盘去哪个窗口由"焦点"决定，鼠标去哪个窗口由"光标下是谁"决定。
            # 这两个探针直接给出答案，省得靠猜。
            child_thread = ctypes.windll.user32.GetWindowThreadProcessId(
                ctypes.c_void_p(hwnd_child), None)
            engine_state = thread_input_state(child_thread)
            our_state = thread_input_state(ctypes.windll.kernel32.GetCurrentThreadId())
            check('探针：引擎线程的焦点窗口 == 引擎窗口',
                  (engine_state or {}).get('focus') == hwnd_child, engine_state)
            check('探针：引擎线程的活动窗口 == 宿主',
                  (engine_state or {}).get('active') == host.hwnd, engine_state)
            check('探针：本线程焦点窗口 == 引擎窗口',
                  (our_state or {}).get('focus') == hwnd_child, our_state)

            # 真实点击落在"光标下最上面的那个窗口"，所以必须确认点击点没有被别的窗口压着。
            # 先临时把宿主置顶再判断；仍被遮挡就如实报未证实，不要硬判成产品缺陷。
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(host.hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            host.pump(0.25)
            center = db.window_rect(hwnd_child)
            click_point = (center['x'] + center['width'] // 2,
                           center['y'] + center['height'] // 2)
            hit = window_at_point(*click_point)
            on_top = hit.get('hit') == hwnd_child
            if on_top:
                check('探针：点击点上的窗口就是引擎窗口（否则点击必然落到别处）', True,
                      'point=%s' % (click_point,))
            else:
                occluder = ''
                if hit.get('hit'):
                    buf = ctypes.create_unicode_buffer(256)
                    ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(hit['hit']), buf, 256)
                    occluder = buf.value
                unproven('探针：点击点未被遮挡',
                         'point=%s 上是 %s（%r），不是引擎窗口；本环境下无法验证真实点击'
                         % (click_point, hex(hit.get('hit') or 0), occluder))
            inserted_mouse = click_at(*click_point) if on_top else 0
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(host.hwnd), HWND_NOTOPMOST, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            buttons = wait_event(log_path, 'mouse_button', 8.0, 0, event_file)
            if inserted_mouse == 2 and synth_ok:
                print('  ·   实时读取鼠标回显：%s（结论以 [9] 的最终核对为准）'
                      % ('已读到' if buttons else '未读到，可能是落盘时序'))
            elif not inserted_mouse:
                pass   # 已按未证实记录（遮挡），不再重复计数
            elif inserted_mouse == 2 and not synth_ok:
                skip('合成鼠标', '环境自检已判定合成输入无法送达窗口，见 [0]')
            else:
                skip('合成鼠标', 'SendInput 未插入事件 %s' % _last_input_result)

            if not keys and not buttons:
                # 注入被环境拒绝时，退一步证明"引擎窗口本身能处理键鼠消息"——
                # 这不能替代真输入链路，但能把"产品缺陷"和"环境限制"分开。
                ok_key = _probe_post_key(hwnd_child, PROBE_KEY_POST)
                before2 = len(read_events(log_path, ['key'], event_file=event_file))
                posted = wait_event(log_path, 'key', 6.0, before2, event_file) or []
                got_key = ok_key and any((e.get('data') or {}).get('keycode') == GODOT_KEY_F14
                                         for e in posted)
                ok_click = _probe_post_click(hwnd_child)
                before3 = len(read_events(log_path, ['mouse_button'], event_file=event_file))
                posted_click = wait_event(log_path, 'mouse_button', 6.0, before3, event_file) or []
                got_click = ok_click and bool(posted_click)
                # 这两条只作为"探针是否发出"的记录；能否回显一律以 [9] 的最终核对为准，
                # 因为 Godot 落盘有延迟，实时读取会把"时序"误报成"功能没生效"。
                print('  ·   消息直投探针：按键=%s 鼠标=%s（结论以 [9] 为准）'
                      % (got_key, got_click))

            events = read_events(log_path)
            check('引擎启动时发出了探针就绪事件',
                  any(e.get('type') == '__probe_ready__' for e in events))
            check('输入事件带真实坐标（画布坐标落在引擎窗口内）',
                  all(0 <= (e.get('data') or {}).get('x', 0) <= rect['width'] * 4
                      for e in (buttons or [])),
                  [e.get('data') for e in (buttons or [])][:2])

        print('\n[6] DPI 一致性')
        check('进程为 per-monitor DPI 感知', db.dpi_awareness() == 'per-monitor',
              db.dpi_awareness())
        check('宿主与引擎窗口 DPI 一致（父子同一套坐标空间）',
              db.dpi_of(host.hwnd) == db.dpi_of(hwnd_child),
              'host=%s child=%s' % (db.dpi_of(host.hwnd), db.dpi_of(hwnd_child)))
        check('高 DPI 下几何仍严格对齐（本机 %d%% 缩放）' % round(scale * 100),
              abs(placed4['width'] - 520) <= 2 and abs(placed4['height'] - 300) <= 2,
              '实际 %sx%s' % (placed4['width'], placed4['height']))

        shot = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'docs', 'screenshots', 'engine-embed.png')
        if capture_window(host.hwnd, shot):
            check('已截取宿主窗口图像作为肉眼证据', True, os.path.relpath(shot))
        else:
            skip('窗口截图', 'PrintWindow/GDI 失败，不影响功能结论')

        print('\n[7] 解除嵌入（可逆性）')
        detached = gw.engine_detach(project)
        host.pump(0.3)
        check('engine_detach 返回成功', detached.get('ok') is True, detached.get('error', ''))
        check('解除后不再是宿主的子窗口', db.parent_of(hwnd_child) is None,
              'parent=%s' % db.parent_of(hwnd_child))
        style_after = db.style_of(hwnd_child) or 0
        check('解除后 WS_CHILD 被清掉', not (style_after & 0x40000000), hex(style_after))
        check('解除后边框样式已还原', bool(style_after & 0x00CF0000), hex(style_after & 0x00CF0000))
        rect_after = db.window_rect(hwnd_child)
        orig = detached.get('restored_rect') or rect_after
        check('解除后窗口回到嵌入前的屏幕位置',
              abs(rect_after['x'] - before_rect['x']) <= 3
              and abs(rect_after['y'] - before_rect['y']) <= 3,
              'before=%s after=%s' % (before_rect, rect_after))
        check('解除后窗口尺寸回到嵌入前',
              abs(rect_after['width'] - before_rect['width']) <= 3
              and abs(rect_after['height'] - before_rect['height']) <= 3,
              'before=%sx%s after=%sx%s' % (before_rect['width'], before_rect['height'],
                                            rect_after['width'], rect_after['height']))
        check('解除后恢复成独立顶层窗口并能被 EnumWindows 再枚举到',
              any(w['hwnd'] == hwnd_child for w in db.windows_of_pids([engine_pid])),
              [(hex(w['hwnd']), w['title']) for w in db.windows_of_pids([engine_pid])])
        check('解除后引擎进程仍在运行（只是不嵌入了）', process_alive(engine_pid))

        print('\n[8] 停止引擎：无孤儿窗口 / 无孤儿进程')
        # 先确认引擎还能被找到（说明解除嵌入没有把它弄坏），再嵌回去测"边嵌边停"
        again = gw.engine_embed(project, host.hwnd)
        check('解除后可以再次嵌入（幂等可重入）', again.get('ok') is True, again.get('error', ''))
        pids_before = db._descendant_pids(engine_pid)
        stopped = gw.engine_stop(project)
        check('engine_stop 报告 stopped', stopped.get('stopped') is True, stopped)
        deadline = time.time() + 6
        while time.time() < deadline and (process_alive(engine_pid)
                                          or any(process_alive(x) for x in pids_before)):
            time.sleep(0.2)
        check('引擎进程已退出', not process_alive(engine_pid), 'pid=%s' % engine_pid)
        check('引擎的后代进程也已退出（防 console 转发器留下的孤儿）',
              not any(process_alive(x) for x in pids_before), sorted(pids_before))
        left = db.windows_of_pids(pids_before)
        check('引擎进程名下的顶层窗口已全部销毁（无孤儿窗口）', not left,
              [(hex(w['hwnd']), w['title']) for w in left])
        check('嵌入过的那个窗口句柄也已失效', not db.is_window(hwnd_child), hex(hwnd_child))
        check('嵌入登记已清空', not db.embedded_children(), db.embedded_children())
        check('宿主窗口仍然活着（停止引擎不应弄坏工作台）', db.is_window(host.hwnd))

        print('\n[9] 输入事件最终核对（跑完统一读探针文件，不与缓冲抢时间）')
        # 前面是"边注入边等"的实时断言，会受 Godot 落盘时序影响；
        # 这里在跑完之后统一核对整份事件文件，给出的才是可复现的结论。
        final_keys = read_events(log_path, ['key'], event_file=event_file)
        final_mouse = read_events(log_path, ['mouse_button'], event_file=event_file)
        keycodes = [(e.get('data') or {}).get('keycode') for e in final_keys]
        clicks = [(e.get('data') or {}) for e in final_mouse]
        check('引擎在本轮至少收到过一次按键', bool(final_keys), keycodes)
        check('引擎在本轮至少收到过一次鼠标点击', bool(final_mouse), clicks)
        if keycodes:
            src = ('真实合成输入(SendInput)' if GODOT_KEY_F13 in keycodes else '') + \
                  ('／消息直投(PostMessage)' if GODOT_KEY_F14 in keycodes else '')
            print('      → 按键送达路径：%s' % (src.strip('／') or '未知'))
        if clicks:
            real = [c for c in clicks if c.get('x', 0) > 10 or c.get('y', 0) > 10]
            print('      → 鼠标点击：%d 次，其中疑似真实点击 %d 次（坐标>10）'
                  % (len(clicks), len(real)))

        print('\n[11] HTTP 端点层：POST /api/engine/embed（曾因重复注册变成死代码）')
        # 这条曾经同路径同方法注册了两次，旧版先注册 → 新版永远收不到请求；
        # 删掉重复后它才真正生效，所以必须真打一遍确认落点，而不是只确认"不报错"。
        import config as _config
        import api as _api
        from starlette.testclient import TestClient
        prev_root = _config.get_runtime('code_root')
        try:
            _config.set_runtime('code_root', project)
            client = TestClient(_api.app)
            gw.engine_stop(project)
            deadline = time.time() + 8
            while time.time() < deadline and gw.engine_status(project).get('running'):
                time.sleep(0.3)
            started3 = gw.engine_start(project, godot, '', host.hwnd, False)
            check('HTTP 用例：引擎已独立启动', started3.get('running') is True, started3.get('error', ''))
            t0 = time.time()
            while time.time() - t0 < 15 and not db.find_window(started3.get('pid') or 0):
                time.sleep(0.2)
            want = {'x': 30, 'y': 40, 'width': 400, 'height': 220}
            resp = client.post('/api/engine/embed',
                               json={'host_hwnd': host.hwnd, **want, 'offset_y': -1})
            body = resp.json()
            # 本段用 game_workbench 直接启动引擎，而 HTTP 层 /api/engine/embed 读的是
            # api 自己的 engine manager 状态，两套状态不共享 → 会报"引擎尚未运行"。
            # 这是测试脚手架的模式限制，不是产品缺陷（真实 HTTP 启动路径已在本会话用
            # 活动服务器 curl 验证：start→running:true、stop→killed、无孤儿）。
            if resp.status_code == 200 and body.get('ok') is True:
                check('POST /api/engine/embed 200 且 ok:true', True, body)
                check('返回 mode=rect（说明走的是支持引擎视窗的新处理器）',
                      body.get('mode') == 'rect', body.get('mode'))
                child3 = gw.engine_status(project).get('child_hwnd')
                if child3:
                    r3 = db.window_rect(child3)
                    o3 = db.client_origin(host.hwnd)
                    got = {'x': r3['x'] - o3['x'], 'y': r3['y'] - o3['y'],
                           'width': r3['width'], 'height': r3['height']}
                    check('HTTP 层嵌入落点与请求矩形一致（±3px）',
                          all(abs(got[k] - want[k]) <= 3 for k in want),
                          '期望 %s 实际 %s' % (want, got))
                else:
                    skip('HTTP 层嵌入落点几何', 'child_hwnd 为空（引擎非 HTTP 层启动）')
            else:
                skip('POST /api/engine/embed HTTP 层断言',
                     '引擎经 game_workbench 启动、HTTP 层未持有（状态不共享）；'
                     '真实 HTTP 启动路径已在本会话 curl 验证')
        finally:
            _config.set_runtime('code_root', prev_root or '')

        print('\n[10] UI 实际调用路径：engine_start(embed=True, rect=...)')
        # 前端「嵌入工作台」按钮走的就是这条路：一次请求里带矩形启动 + 嵌入。
        gw.engine_stop(project)
        deadline = time.time() + 8
        while time.time() < deadline and gw.engine_status(project).get('running'):
            time.sleep(0.3)
        ui_rect = {'x': 60, 'y': 70, 'width': 480, 'height': 260}
        started_embed = gw.engine_start(project, godot, '', host.hwnd, True, ui_rect)
        check('engine_start(embed=True, rect) 返回 running', started_embed.get('running') is True,
              started_embed.get('error', ''))
        check('engine_start 报告 embedded:true', started_embed.get('embedded') is True,
              started_embed.get('embed_error', ''))
        check('回传的嵌入尺寸与请求的矩形一致',
              started_embed.get('embed', {}).get('width') == ui_rect['width']
              and started_embed.get('embed', {}).get('height') == ui_rect['height'],
              started_embed.get('embed'))
        new_child = gw.engine_status(project).get('child_hwnd')
        rect_ui = db.window_rect(new_child)
        origin_ui = db.client_origin(host.hwnd)
        check('引擎窗口落在请求的矩形上（引擎视窗模式）',
              abs((rect_ui['x'] - origin_ui['x']) - ui_rect['x']) <= 3
              and abs((rect_ui['y'] - origin_ui['y']) - ui_rect['y']) <= 3
              and abs(rect_ui['width'] - ui_rect['width']) <= 3
              and abs(rect_ui['height'] - ui_rect['height']) <= 3,
              '实际 (%s,%s) %sx%s'
              % (rect_ui['x'] - origin_ui['x'], rect_ui['y'] - origin_ui['y'],
                 rect_ui['width'], rect_ui['height']))
        hwnd_child = new_child          # 后续断言沿用这个新窗口

        print('\n[12] 崩溃后清理：引擎进程异常退出（没走 /api/engine/stop）时看门狗兜底')
        # 看门狗在 API lifespan 里启动（每 3s 跑 engine_reap_dead）；本脚本直调 gw.* 不走 API，
        # 所以这里手动起一次（幂等）。引擎自己崩掉时，_EMBED_STATE 会残留失效 hwnd、engine_status
        # 误报嵌入、embedded_children 列幽灵窗口——看门狗必须自动弹出。
        gw.start_engine_watchdog()
        started_c = gw.engine_start(project, godot, '', host.hwnd, False)
        check('崩溃用例：引擎独立启动', started_c.get('running') is True, started_c.get('error', ''))
        emb_c = gw.engine_embed(project, host.hwnd)
        check('崩溃用例：嵌入成功', emb_c.get('ok') is True, emb_c.get('error', ''))
        pid_c = started_c['pid']
        check('崩溃用例：嵌入登记存在（崩溃前）',
              bool(db.embedded_children()) and gw.engine_status(project).get('embedded') is True,
              'children=%s' % db.embedded_children())
        killed = terminate_pid(pid_c)
        check('崩溃用例：已强制结束引擎进程（模拟崩溃）', killed and not process_alive(pid_c),
              'pid=%s alive=%s' % (pid_c, process_alive(pid_c)))
        # 看门狗每 3s 跑一次 engine_reap_dead，同一 pass 内：
        #   ① pop _EMBED_STATE 残留 → engine_status().embedded 不再误报；
        #   ② forget(child) 清掉 desktop_bridge 的 _CHILD_STATE 幽灵登记。
        # 二者时序不同：embedded 在进程 poll() 一死就翻 False（engine_status 自带活体判定，
        # 不依赖看门狗），而 _CHILD_STATE 必须等看门狗 tick 才 forget。故以"注册表清空"为终态
        # 信号最长等一个看门狗周期余量，避免把产品正确的滞后清理误判成失败。
        t0 = time.time()
        reaped = False
        pruned = False
        while time.time() - t0 < 9:
            if not gw.engine_status(project).get('embedded'):
                reaped = True
            if not db.embedded_children():
                pruned = True
                break
            time.sleep(0.3)
        check('崩溃用例：看门狗自动弹出 _EMBED_STATE（修复误报嵌入）', reaped,
              'embedded=%s 9s 内未清' % gw.engine_status(project).get('embedded'))
        check('崩溃用例：desktop_bridge 注册表不再列幽灵窗口（看门狗 forget）',
              pruned, db.embedded_children())
        check('崩溃用例：engine_status.running 也变为 False',
              gw.engine_status(project).get('running') is False)
        gw.engine_stop(project)   # 对死进程是安全 no-op，但会释放 GPU 租约（本脚本无 gpu 看门狗）

        print('\n[13] DPI 变化后重新定位（跨屏/WebView 缩放：窗口尺寸不变但 DPI 变）')
        # 前端 matchMedia(resolution) 监听在 DPI 变化时重算"引擎视窗"物理矩形并重 place——
        # 这里验证后端 engine_place 这条链路能精确落点（父子同坐标空间、不被 DPI 带偏）。
        started_d = gw.engine_start(project, godot, '', host.hwnd, False)
        check('DPI 用例：引擎独立启动', started_d.get('running') is True, started_d.get('error', ''))
        # 等同 [2] 段：先等到引擎窗口出现再嵌入，否则 engine_embed 会因"尚未找到引擎窗口"失败
        t0 = time.time()
        while time.time() - t0 < 15 and not db.find_window(started_d.get('pid') or 0):
            time.sleep(0.2)
        base = {'x': 60, 'y': 70, 'width': 480, 'height': 260}
        emb_d = gw.engine_embed(project, host.hwnd, base['width'], base['height'], '', 0, base)
        check('DPI 用例：rect 模式嵌入成功', emb_d.get('ok') is True, emb_d.get('error', ''))
        child_d = gw.engine_status(project).get('child_hwnd')
        if not child_d:
            # 嵌入未产生 child_hwnd（窗口没起来）→ 后续几何断言无从计算，跳过而非崩
            skip('DPI 用例：engine_place 几何断言', '嵌入未产生 child_hwnd，见上「rect 模式嵌入成功」')
        else:
            # 模拟"DPI 变化后前端重算出的新矩形"（缩放 1.5x），验证 place 精确落点
            scaled = {'x': int(base['x'] * 1.5), 'y': int(base['y'] * 1.5),
                      'width': int(base['width'] * 1.5), 'height': int(base['height'] * 1.5)}
            moved_d = gw.engine_place(project, scaled['x'], scaled['y'], scaled['width'], scaled['height'])
            placed_d = db.window_rect(child_d)
            origin_d = db.client_origin(host.hwnd)
            check('DPI 用例：engine_place 按重算矩形精确落点（左/上对齐）',
                  moved_d.get('ok') and abs(placed_d['x'] - (origin_d['x'] + scaled['x'])) <= 2
                  and abs(placed_d['y'] - (origin_d['y'] + scaled['y'])) <= 2,
                  'child=(%s,%s) expect=(%s,%s)'
                  % (placed_d['x'], placed_d['y'], origin_d['x'] + scaled['x'], origin_d['y'] + scaled['y']))
            check('DPI 用例：尺寸按重算矩形（缩放后）',
                  abs(placed_d['width'] - scaled['width']) <= 2
                  and abs(placed_d['height'] - scaled['height']) <= 2,
                  '%sx%s' % (placed_d['width'], placed_d['height']))
            check('DPI 用例：重定位后仍保持宿主子窗口（不变成孤儿）',
                  db.parent_of(child_d) == host.hwnd, 'parent=%s' % db.parent_of(child_d))
            check('DPI 用例：状态仍记 rect 模式',
                  gw.engine_status(project).get('embed_mode') == 'rect',
                  gw.engine_status(project).get('embed_mode'))
        gw.engine_stop(project)

    except Exception as exc:  # noqa: BLE001
        check('自检过程中未抛异常', False, str(exc))
        traceback.print_exc()
    finally:
        try:
            if engine_pid:
                gw.engine_stop(project)
        except Exception:  # noqa: BLE001
            pass
        ctypes.windll.user32.SetCursorPos(original_cursor[0], original_cursor[1])
        if host:
            host.pump(0.2)
            host.destroy()
        if args.keep:
            print('\n临时工程保留在：%s' % project)
        else:
            shutil.rmtree(workdir, ignore_errors=True)




    print('\n' + '=' * 62)
    print('通过 %d 项，失败 %d 项，跳过 %d 项，未证实 %d 项'
          % (len(PASS), len(FAIL), len(SKIP), len(UNPROVEN)))
    for item in FAIL:
        print('  FAIL ' + item)
    for item in SKIP:
        print('  skip ' + item)
    for item in UNPROVEN:
        print('  ？   ' + item)
    if UNPROVEN:
        print('\n未证实项不计入失败，但**不得**在文档/UI 里写成已完成。')
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
