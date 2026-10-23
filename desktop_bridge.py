"""可选 Windows 原生子窗口桥接。纯 ctypes，无 pywin32 依赖；非 Windows 安全降级。

设计要点（都是实机踩出来的）：

* **嵌入是可逆的**：`embed` 会把子窗口的原始 parent/style/窗口矩形存下来，`detach`
  原样还回去。不保存状态就没法安全解绑——`SetParent(child, 0)` 之后子窗口会带着
  `WS_CHILD` 样式变成"不可见的顶层窗口"，看起来就是窗口消失了。
* **尺寸按宿主"客户区"算，不是窗口外框**。外框包含标题栏与边框，用它给子窗口定位
  会让引擎画面右侧/底部被裁掉一截，而且换 DPI 后裁得更多。
* **进程要显式声明 DPI 感知**。默认（无清单）的进程在 125%/150% 缩放下坐标会被 Windows
  虚拟化，而 Godot 自己是 per-monitor v2 感知的——父进程按虚拟坐标、子进程按物理像素，
  两边一对就是错位。`ensure_dpi_awareness()` 把进程提到 per-monitor v2。
* **focus 要跨线程**。子窗口属于引擎进程的线程，直接 `SetFocus` 会失败；
  必须先 `AttachThreadInput` 再 `SetFocus`，并用 `GetFocus()` 回读验证，不能只看返回值。
"""
import ctypes
import ctypes.wintypes
import os
import threading

# 宿主窗口表：project_id -> 宿主 hwnd。键 ``None`` 表示「无项目 / 全局默认宿主」，
# 等价于改造前的单一 `_HOST_HWND`（生命线：无项目键时行为与今天完全一致）。
# 桌面壳可为每个已打开的项目各注册一个宿主；未登记项目的查询回落到 ``None`` 键的默认宿主。
_HOST_STATE = {}
# 保护 _HOST_STATE 的并发读写（桌面壳注册线程 / API 线程池 / resize 回调并发）。
_HOST_LOCK = threading.RLock()
# child hwnd -> {'parent': 原始父窗口, 'style': 原始样式, 'orig_rect': 原始屏幕矩形,
#               'host': 宿主 hwnd, 'mode': 'rect'|'fill', 'offset_y', 'placed': 当前矩形,
#               'attached': [伙伴线程id], 'attached_by': 本进程调用线程id}
_CHILD_STATE = {}
# 保护 _CHILD_STATE 的并发读写：看门狗 daemon 线程(start_engine_watchdog)会并发 pop，
# API 线程池与桌面壳 resize 回调也会并发读写，避免复合操作（如 embed 的 if not in: 赋值）竞态。
_STATE_LOCK = threading.RLock()

# x64 下必须显式声明指针宽度的签名，否则 Python int 会被按 c_int 截断 HWND。
# WINFUNCTYPE 只存在于 Windows；Linux CI 仍需要安全导入本模块以测试降级路径，
# 此时用调用约定不会被实际执行的 CFUNCTYPE 占位，_user32() 会直接返回 None。
_WNDPROCTYPE = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
# 顶层窗口的"边框"类样式：嵌入时必须去掉，否则子窗口里会残留一圈标题栏/粗边框
_TOPLEVEL_STYLES = 0x80000000 | 0x00C00000 | 0x00040000 | 0x00030000 | 0x00080000

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001


def _user32():
    if os.name != 'nt':
        return None
    u = ctypes.windll.user32
    # 枚举/查询
    u.EnumWindows.argtypes = [_WNDPROCTYPE, ctypes.c_void_p]
    u.EnumWindows.restype = ctypes.c_bool
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsWindow.argtypes = [ctypes.c_void_p]
    u.IsWindow.restype = ctypes.c_bool
    u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    u.GetWindowThreadProcessId.restype = ctypes.c_ulong
    # 样式：64 位系统统一用 Ptr 版本（GWL_STYLE 时内部等价 Long）
    u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.GetWindowLongPtrW.restype = ctypes.c_longlong
    u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
    u.SetWindowLongPtrW.restype = ctypes.c_longlong
    # 嵌入 / 几何
    u.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.SetParent.restype = ctypes.c_void_p
    u.GetParent.argtypes = [ctypes.c_void_p]
    u.GetParent.restype = ctypes.c_void_p
    u.MoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_bool]
    u.MoveWindow.restype = ctypes.c_bool
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetWindowPos.restype = ctypes.c_bool
    u.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
    u.GetClientRect.restype = ctypes.c_bool
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
    u.GetWindowRect.restype = ctypes.c_bool
    u.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.POINT)]
    u.ClientToScreen.restype = ctypes.c_bool
    u.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.POINT)]
    u.ScreenToClient.restype = ctypes.c_bool
    # 焦点
    u.SetFocus.argtypes = [ctypes.c_void_p]
    u.SetFocus.restype = ctypes.c_void_p
    u.GetFocus.argtypes = []
    u.GetFocus.restype = ctypes.c_void_p
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.SetForegroundWindow.restype = ctypes.c_bool
    u.BringWindowToTop.argtypes = [ctypes.c_void_p]
    u.BringWindowToTop.restype = ctypes.c_bool
    u.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    u.AttachThreadInput.restype = ctypes.c_bool
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.IsIconic.restype = ctypes.c_bool
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.ShowWindow.restype = ctypes.c_bool
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetWindow.restype = ctypes.c_void_p
    # DPI（Win10 1607+；老系统取不到就退化为 96）
    if hasattr(u, 'GetDpiForWindow'):
        u.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        u.GetDpiForWindow.restype = ctypes.c_uint
    if hasattr(u, 'SetProcessDpiAwarenessContext'):
        u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
    if hasattr(u, 'GetThreadDpiAwarenessContext'):
        u.GetThreadDpiAwarenessContext.argtypes = []
        u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if hasattr(u, 'GetAwarenessFromDpiAwarenessContext'):
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_uint
    return u


def set_host(hwnd, project_id=None):
    """登记（或清除）宿主窗口。返回写入的 hwnd（清除时为 None）。

    兼容两种调用形式（“按项目分桶”是 P5 新增，旧调用方语法完全不变）：

    * ``set_host(12345)`` / ``set_host(None)``：旧形式，登记/清除**默认（无项目）宿主**；
    * ``set_host(12345, 'prjA')`` 或 ``set_host('prjA', 12345)``：登记**指定项目**的宿主
      （首参为 ``str`` 时按“project_id 在前”解读，兼容团队约定的参数顺序）。

    ``hwnd`` 为空（None / 0）时**清除**该项目的登记。
    """
    # 首参是字符串 → 它是 project_id（团队约定 set_host(project_id, hwnd)）；
    # 否则首参是 hwnd（旧形式 set_host(hwnd) 或 set_host(hwnd, project_id)）。
    if isinstance(hwnd, str):
        hwnd, project_id = project_id, hwnd
    key = project_id or None
    value = int(hwnd) if hwnd else None
    with _HOST_LOCK:
        if value is None:
            _HOST_STATE.pop(key, None)
        else:
            _HOST_STATE[key] = value
    return value


def host_hwnd(project_id=None):
    """取某项目的宿主 hwnd。

    未登记的项目、或 ``project_id`` 为空 → **回落默认（None 键）宿主**，保持与改造前
    `get_host()` 一致（生命线）。因此单宿主场景（桌面壳只注册默认宿主）行为不变。
    """
    key = project_id or None
    with _HOST_LOCK:
        if key in _HOST_STATE:
            return _HOST_STATE.get(key)
        return _HOST_STATE.get(None)


def get_host():
    """默认（无项目）宿主 hwnd；等价旧 `get_host()`。"""
    with _HOST_LOCK:
        return _HOST_STATE.get(None)


def hosts_snapshot():
    """全部宿主登记快照 ``{project_id(或 None): hwnd}``（供状态查询）。"""
    with _HOST_LOCK:
        return dict(_HOST_STATE)


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------
def ensure_dpi_awareness():
    """把当前进程提到 per-monitor v2 DPI 感知；返回 (ok, 模式名)。

    不做这件事的后果：进程在 125%/150% 缩放下拿到的是"虚拟化后的坐标"，
    而 Godot 自己按物理像素渲染，父/子窗口两边坐标系不一致 —— 表现就是嵌入画面
    尺寸对不上、鼠标点不准。必须在建宿主窗口之前调用。
    """
    user32 = _user32()
    if user32 is None:
        return False, 'non-windows'
    if hasattr(user32, 'SetProcessDpiAwarenessContext'):
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True, dpi_awareness()
    try:  # 老系统兜底：系统级感知，至少不虚拟化
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True, dpi_awareness()
    except Exception:  # noqa: BLE001
        pass
    try:
        if user32.SetProcessDPIAware():
            return True, dpi_awareness()
    except Exception:  # noqa: BLE001
        pass
    return False, dpi_awareness()


def dpi_awareness():
    """返回可读的 DPI 感知模式名。"""
    user32 = _user32()
    if user32 is None:
        return 'non-windows'
    names = {0: 'unaware', 1: 'system', 2: 'per-monitor'}
    if hasattr(user32, 'GetThreadDpiAwarenessContext') and hasattr(
            user32, 'GetAwarenessFromDpiAwarenessContext'):
        ctx = user32.GetThreadDpiAwarenessContext()
        return names.get(int(user32.GetAwarenessFromDpiAwarenessContext(ctx)), 'unknown')
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return names.get(value.value, 'unknown')
    except Exception:  # noqa: BLE001
        return 'unknown'


def dpi_of(hwnd):
    """窗口所在显示器的 DPI（96 = 100%）。取不到时返回 96。"""
    user32 = _user32()
    if user32 is None:
        return 96
    try:
        if hasattr(user32, 'GetDpiForWindow'):
            value = int(user32.GetDpiForWindow(int(hwnd)))
            if value:
                return value
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(ctypes.windll.gdi32.GetDeviceCaps(ctypes.windll.user32.GetDC(0), 88)) or 96
    except Exception:  # noqa: BLE001
        return 96


def client_rect(hwnd):
    """客户区尺寸（物理像素）。"""
    user32 = _user32()
    if user32 is None:
        return None
    rect = ctypes.wintypes.RECT()
    if not user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
        return None
    return {'width': int(rect.right - rect.left), 'height': int(rect.bottom - rect.top)}


def window_rect(hwnd):
    """窗口外框在屏幕上的矩形（物理像素）。"""
    user32 = _user32()
    if user32 is None:
        return None
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
        return None
    return {'x': int(rect.left), 'y': int(rect.top),
            'width': int(rect.right - rect.left), 'height': int(rect.bottom - rect.top)}


def client_origin(hwnd):
    """客户区左上角在屏幕上的坐标。"""
    user32 = _user32()
    if user32 is None:
        return None
    point = ctypes.wintypes.POINT(0, 0)
    if not user32.ClientToScreen(int(hwnd), ctypes.byref(point)):
        return None
    return {'x': int(point.x), 'y': int(point.y)}


def style_of(hwnd):
    user32 = _user32()
    if user32 is None:
        return None
    return int(user32.GetWindowLongPtrW(int(hwnd), GWL_STYLE))


def is_window(hwnd):
    user32 = _user32()
    if user32 is None or not hwnd:
        return False
    return bool(user32.IsWindow(int(hwnd)))


def parent_of(hwnd):
    user32 = _user32()
    if user32 is None:
        return None
    return int(user32.GetParent(int(hwnd)) or 0) or None


def restore_window(hwnd):
    """把最小化的独立顶层窗口还原并带到前台，返回 {ok, restored, foreground}。

    为什么需要：Godot 游戏窗口在启动几秒后会**自己最小化**（客户区塌成 0x0），
    此后 PrintWindow / BitBlt 都抓不到像素（capture_window 返回 None），截图工具
    只能退回前台窗口——于是模型截到的是聊天应用而不是游戏。agent 要"看见"它自己
    刚起的引擎，就得先把那个最小化窗口还原。

    只接受调用方**已经通过引擎进程表拿到**的 hwnd（不是外部任意句柄），且只做
    还原+置前，不改动窗口内容——这不是"操作任意窗口"的后门。任何异常都吞掉并
    返回 ok=False，让调用方继续走原有兜底路径。
    """
    user32 = _user32()
    if user32 is None or not hwnd:
        return {'ok': False, 'error': '仅 Windows 支持'}
    try:
        h = int(hwnd)
        if not is_window(h):
            return {'ok': False, 'restored': False, 'foreground': False,
                    'error': '窗口句柄已失效'}
        restored = False
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)  # SW_RESTORE
            restored = True
        user32.BringWindowToTop(h)
        fg = bool(user32.SetForegroundWindow(h))
        return {'ok': True, 'restored': restored, 'foreground': fg}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'restored': False, 'foreground': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
def _enum_windows(user32, match):
    found = []

    def cb(hwnd, _):
        if match(int(hwnd) if hwnd else 0, found):
            return True
        return True

    user32.EnumWindows(_WNDPROCTYPE(cb), None)
    return found[0] if found else None


def _descendant_pids(pid):
    """通过 Toolhelp 快照返回 pid 的全部后代进程（console 版 Godot 会派生 GUI 子进程）。"""
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.wintypes.DWORD), ("cntUsage", ctypes.wintypes.DWORD),
                    ("th32ProcessID", ctypes.wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.wintypes.DWORD),
                    ("cntThreads", ctypes.wintypes.DWORD),
                    ("th32ParentProcessID", ctypes.wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {pid}
    parents = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    result = {pid}
    changed = True
    while changed:  # 逐层扩散到全部后代
        changed = False
        for child, parent in parents.items():
            if parent in result and child not in result:
                result.add(child)
                changed = True
    return result


def _enum_all(user32, match):
    """枚举全部顶层窗口，match(hwnd, found) 往 found 追加，返回完整列表。"""
    found = []

    def cb(hwnd, _):
        match(int(hwnd) if hwnd else 0, found)
        return True

    user32.EnumWindows(_WNDPROCTYPE(cb), None)
    return found


def _rank_windows(candidates, title_hint=''):
    """从候选窗口里挑"最像游戏主视口"的那个（纯逻辑，可单测）。

    打分维度（从高到低）：标题命中 title_hint > 非最小化窗口 > 可见面积大。
    引擎常有 splash / 控制台 / 主视口多个顶层窗口，取第一个会抓错，必须按这个顺序选。
    """
    hint = (title_hint or '').lower()

    def score(c):
        s = 0
        if hint and hint in (c.get('title') or '').lower():
            s += 100
        if not c.get('iconic'):
            s += 10
        s += min(int(c.get('area', 0)), 10_000_000) // 10_000
        return s

    best = None
    best_s = -1
    for c in candidates:
        sc = score(c)
        if sc > best_s:
            best_s = sc
            best = c
    return best


def find_window(pid: int, title_hint: str = ''):
    """通过进程 PID（含其全部后代进程）枚举顶层可见窗口，返回 (hwnd, title)。

    选窗口策略见 `_rank_windows`：标题命中 > 非最小化 > 面积大。引擎有多个顶层窗口
    （splash / 控制台 / 主视口）时，取第一个会抓错，必须打分挑最优。
    """
    user32 = _user32()
    if user32 is None:
        return None
    pids = _descendant_pids(int(pid))

    def match(hwnd, found):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value not in pids or not user32.IsWindowVisible(hwnd):
            return False
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        if not title:
            return False
        rect = window_rect(hwnd)
        area = (rect['width'] * rect['height']) if rect else 0
        found.append({'hwnd': hwnd, 'title': title,
                      'iconic': bool(user32.IsIconic(hwnd)), 'area': area})
        return False

    candidates = _enum_all(user32, match)
    if not candidates:
        return None
    best = _rank_windows(candidates, title_hint)
    return (best['hwnd'], best['title'])


def windows_of_pids(pids):
    """枚举属于给定进程集合的所有顶层窗口。

    ⚠️ 注意收集用的列表必须是**本函数自己的**：`_enum_windows` 会把回调的第二个参数
    当作自己的内部收集器，往那个列表里 append 的结果出了 `_enum_windows` 就丢了——
    这个 bug 让"停止后没有孤儿窗口"的断言变成了空转的假通过，务必别再写回去。
    """
    user32 = _user32()
    if user32 is None:
        return []
    target = {int(x) for x in pids}
    out = []

    def match(hwnd, _found):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value not in target:
            return False
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        out.append({'hwnd': hwnd, 'pid': owner.value, 'title': buf.value,
                    'visible': bool(user32.IsWindowVisible(hwnd)),
                    'parent': int(user32.GetParent(hwnd) or 0) or None})
        return False

    _enum_windows(user32, match)
    return out


def find_host(title_hint='Agent Workbench'):
    """查找桌面宿主窗口；浏览器模式/无桌面壳时返回 None。"""
    user32 = _user32()
    if user32 is None:
        return None

    def match(hwnd, found):
        if not user32.IsWindowVisible(hwnd):
            return False
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if title_hint.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return False

    return _enum_windows(user32, match)


# ---------------------------------------------------------------------------
# 嵌入 / 解除 / 尺寸同步
# ---------------------------------------------------------------------------
def _target_size(hwnd_host, width, height, offset_y, cached=None):
    """算出子窗口应该占的尺寸。给定宽高就用给定值，否则按宿主客户区减顶部留白。

    宿主最小化 / 尚未布局完成时 GetClientRect 会返回 0×0——这时若上一次有有效的
    宿主矩形（cached）就回退用它，避免嵌入失败或引擎被 MoveWindow(0,0,0,0) 塌成不可见。
    """
    if width and height:
        return int(width), int(height), None
    rect = client_rect(hwnd_host)
    if rect and rect['width'] > 0 and rect['height'] > 0:
        return rect['width'], max(1, rect['height'] - int(offset_y)), rect
    if cached and int(cached.get('width', 0)) > 0 and int(cached.get('height', 0)) > 0:
        return (int(cached['width']),
                max(1, int(cached['height']) - int(offset_y)),
                cached)
    return None, None, '无法读取宿主客户区尺寸（窗口可能已最小化）'


def embed(hwnd_child: int, hwnd_host: int, width=None, height=None,
          offset_y: int = 0, title: str = '', rect=None, fill: bool = False):
    """把引擎窗口嵌进宿主客户区，并记录原始状态以便 `detach` 还原。

    三种尺寸模式：

    * **rect 模式（推荐）**：`rect={'x','y','width','height'}`，由前端给出工作台里那块
      "引擎视窗"的屏幕/客户坐标。引擎只占这一块，工作台 UI 照常可用。
    * **fill 模式**：`fill=True` 显式请求时，按宿主客户区铺满（`offset_y` 是顶部留白）。
      这会盖住整个工作台界面——包括"停止引擎"按钮——所以**只能由调用方显式声明**，
      绝不作为"没给 rect"的默默兜底（那正是"引擎打开直接黑屏"的根因）。
    * **有界框模式（默认安全降级）**：`rect` 缺失且未显式 `fill` 时，在宿主客户区内嵌一个
      居中、四周留边的小窗。既不铺满全屏（不会黑屏盖住 git 按钮等），也不强制退化成独立窗口
      ——引擎仍嵌在工作台里，只是占一小块。这是"rect 拿不到"时的防御性默认。
    """
    user32 = _user32()
    if user32 is None:
        return {'ok': False, 'error': '仅 Windows 支持 HWND 嵌入'}
    try:
        hwnd_child, hwnd_host = int(hwnd_child), int(hwnd_host)
        if not is_window(hwnd_child):
            return {'ok': False, 'error': '引擎窗口句柄无效（可能已退出）'}
        if not is_window(hwnd_host):
            return {'ok': False, 'error': '宿主窗口句柄无效（桌面壳可能已关闭）'}

        mode = 'rect'
        if rect and int(rect.get('width') or 0) > 0 and int(rect.get('height') or 0) > 0:
            pos_x, pos_y = int(rect.get('x') or 0), int(rect.get('y') or 0)
            size_w, size_h, host_rect = int(rect['width']), int(rect['height']), None
        elif fill:
            # 显式铺满：调用方明确要全屏嵌入（会盖住工作台界面），仅此情形才允许。
            mode = 'fill'
            pos_x, pos_y = 0, int(offset_y)
            with _STATE_LOCK:
                cached = (_CHILD_STATE.get(hwnd_child) or {}).get('last_host_client')
            size_w, size_h, host_rect = _target_size(hwnd_host, width, height, offset_y, cached)
            if not size_w:
                return {'ok': False, 'error': host_rect}
        else:
            # 硬化：rect 缺失也不许铺满全屏。退化为"宿主客户区内的安全有界框"，
            # 既不黑屏盖住工作台，也不强制退化成独立窗口（引擎仍嵌着）。
            mode = 'rect'
            with _STATE_LOCK:
                cached = (_CHILD_STATE.get(hwnd_child) or {}).get('last_host_client')
            host_w, host_h, host_rect = _target_size(hwnd_host, width, height, 0, cached)
            if not host_w:
                # 连宿主客户区都读不到（窗口最小化 / 尚未布局）：放弃嵌入，让引擎当独立窗口跑。
                return {'ok': False,
                        'error': '引擎视窗矩形无效且无法读取宿主客户区，已退化为独立窗口运行（' + str(host_rect) + '）'}
            margin = max(32, int(min(host_w, host_h) * 0.10))
            size_w = max(80, host_w - 2 * margin)
            size_h = max(80, host_h - 2 * margin)
            pos_x = max(0, (host_w - size_w) // 2)
            pos_y = max(0, (host_h - size_h) // 2)

        if hwnd_child not in _CHILD_STATE:
            with _STATE_LOCK:
                if hwnd_child not in _CHILD_STATE:
                    _CHILD_STATE[hwnd_child] = {
                        'parent': int(user32.GetParent(hwnd_child) or 0) or None,
                        'style': style_of(hwnd_child),
                        # 原始屏幕矩形：解除嵌入时用它把窗口放回原处
                        'orig_rect': window_rect(hwnd_child),
                        'host': hwnd_host,
                    }
        style = style_of(hwnd_child)
        user32.SetWindowLongPtrW(hwnd_child, GWL_STYLE,
                                 (style | WS_CHILD | WS_VISIBLE) & ~_TOPLEVEL_STYLES)
        user32.SetParent(hwnd_child, hwnd_host)
        # 不带动 SWP_NOZORDER：要把它抬到宿主子窗口的顶层，否则会被 WebView2 盖住。
        # 不带 SWP_NOACTIVATE：嵌入是用户主动操作，焦点交给游戏是预期行为。
        user32.SetWindowPos(hwnd_child, ctypes.c_void_p(0), pos_x, pos_y, size_w, size_h,
                            SWP_FRAMECHANGED | SWP_SHOWWINDOW)
        with _STATE_LOCK:
            state = _CHILD_STATE[hwnd_child]
            state['mode'] = mode
            state['offset_y'] = int(offset_y)
            state['placed'] = {'x': pos_x, 'y': pos_y, 'width': size_w, 'height': size_h}
            # 缓存当前有效的宿主客户区矩形：宿主最小化 / 尚未布局时 GetClientRect 返 0，
            # 之后 fill_host / embed 可回退到此，避免引擎塌缩（见 _target_size）。
            state['last_host_client'] = host_rect or client_rect(hwnd_host)
        return {'ok': True, 'hwnd': hwnd_child, 'host_hwnd': hwnd_host, 'title': title,
                'mode': mode, 'x': pos_x, 'y': pos_y, 'offset_y': int(offset_y),
                'width': size_w, 'height': size_h,
                'host_client': host_rect or client_rect(hwnd_host),
                'dpi': dpi_of(hwnd_child), 'host_dpi': dpi_of(hwnd_host)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def place(hwnd_child: int, x: int, y: int, width: int, height: int):
    """把已嵌入的子窗口移到新的矩形（前端布局变化时同步）。

    ⚠️ 必须带 SWP_NOACTIVATE：这是**非用户主动**的重定位（resize / DPI 变化 / 布局变动触发），
    绝不能把键盘焦点从工作台抢到游戏——否则用户正在工作台打字，窗口一缩放焦点就飞走。
    （embed 是用户主动点"嵌入"，才不带 NOACTIVATE。）
    """
    user32 = _user32()
    if user32 is None:
        return {'ok': False, 'error': '仅 Windows 支持'}
    try:
        hwnd_child = int(hwnd_child)
        if not is_window(hwnd_child):
            return {'ok': False, 'error': '引擎窗口已失效'}
        x, y, width, height = int(x), int(y), max(1, int(width)), max(1, int(height))
        user32.SetWindowPos(hwnd_child, ctypes.c_void_p(0), x, y, width, height,
                            SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOACTIVATE)
        with _STATE_LOCK:
            state = _CHILD_STATE.get(hwnd_child)
            if state is not None:
                state['placed'] = {'x': x, 'y': y, 'width': width, 'height': height}
        return {'ok': True, 'hwnd': hwnd_child, 'x': x, 'y': y,
                'width': width, 'height': height}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def fill_host(hwnd_child: int, offset_y=None):
    """按宿主当前客户区重排"铺满模式"的子窗口（宿主 resize 时调用）。

    不能拿宿主的"窗口外框尺寸"当子窗口尺寸：外框含标题栏与边框，
    直接用会把引擎画面裁掉一截。rect 模式（前端指定的引擎视窗）由前端自己重新 `place`，
    这里不插手，否则两边会互相打架。
    """
    user32 = _user32()
    if user32 is None:
        return {'ok': False, 'error': '仅 Windows 支持'}
    try:
        hwnd_child = int(hwnd_child)
        state = _CHILD_STATE.get(hwnd_child) or {}
        if state.get('mode') == 'rect':
            return {'ok': True, 'skipped': 'rect-mode', 'hwnd': hwnd_child}
        hwnd_host = int(state.get('host') or user32.GetParent(hwnd_child) or 0)
        if not hwnd_host or not is_window(hwnd_host):
            return {'ok': False, 'error': '宿主窗口已失效'}
        top = state.get('offset_y', 0) if offset_y is None else offset_y
        size_w, size_h, rect = _target_size(hwnd_host, None, None, top,
                                           state.get('last_host_client'))
        if not size_w:
            return {'ok': False, 'error': rect}
        user32.MoveWindow(hwnd_child, 0, int(top), size_w, size_h, True)
        with _STATE_LOCK:
            state['placed'] = {'x': 0, 'y': int(top), 'width': size_w, 'height': size_h}
            # 刷新缓存（宿主可能从最小化恢复、尺寸变化）
            state['last_host_client'] = rect or client_rect(hwnd_host)
        return {'ok': True, 'hwnd': hwnd_child, 'host_hwnd': hwnd_host,
                'width': size_w, 'height': size_h, 'offset_y': int(top),
                'host_client': rect}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def fill_all(offset_y=None, project_id=None):
    """把所有"铺满模式"的嵌入窗口按当前宿主客户区重排（桌面壳 resize 事件用）。

    ``project_id`` 为 ``None`` 时**行为与改造前完全一致**（重排全部嵌入窗口）；给出项目 id
    时只重排嵌进**该项目宿主**的窗口——多宿主（每项目一个窗口）场景下，某个宿主的 resize
    只应影响挂在它下面的那批窗口，不能去动别的项目的引擎画面。
    """
    with _STATE_LOCK:
        hwnds = list(_CHILD_STATE)
    if project_id is not None:
        target = host_hwnd(project_id)
        hwnds = [h for h in hwnds if (_CHILD_STATE.get(h) or {}).get('host') == target]
    # fill_host 内部也会取 _STATE_LOCK；用 RLock 重入安全
    return [fill_host(hwnd, offset_y) for hwnd in hwnds]


def _detach_input_queues(state):
    """撤销 focus(keep_attached=True) 留下的跨线程输入队列挂接（尽力而为）。

    挂接以 (by_thread, partner_thread) 成对记录；引擎进程退出或 detach 时按所有对
    再调一次 AttachThreadInput(..., False)。前台线程会变（用户切到别的应用），所以
    必须累积全部历史挂接并逐个解除，不能只解除最近一次——否则旧挂接永远残留在线程上。
    目标线程已死时 AttachThreadInput 会失败，捕获后忽略即可。
    """
    user32 = _user32()
    if user32 is None:
        return
    pairs = [(int(b), int(t)) for b, t in (state.get('attached_pairs') or [])]
    by = state.get('attached_by')
    attached = state.get('attached') or []
    for t in attached:  # 兼容旧字段
        if t and by:
            pairs.append((int(by), int(t)))
    for by_t, partner in pairs:
        if by_t and partner and by_t != partner:
            try:
                user32.AttachThreadInput(ctypes.c_ulong(by_t), ctypes.c_ulong(partner), False)
            except Exception:  # noqa: BLE001
                pass
    state['attached_pairs'] = []
    state['attached'] = []


def detach(hwnd_child: int):
    """解除嵌入，恢复原始父窗口 / 样式 / 屏幕位置；返回还原后的状态。"""
    user32 = _user32()
    if user32 is None:
        return {'ok': False, 'error': '仅 Windows 支持'}
    try:
        hwnd_child = int(hwnd_child)
        with _STATE_LOCK:
            state = _CHILD_STATE.pop(hwnd_child, None)
        if not is_window(hwnd_child):
            return {'ok': True, 'already_gone': True}
        if state is None:
            return {'ok': True, 'was_embedded': False}
        _detach_input_queues(state)   # 清掉 keep_attached 留下的跨线程输入挂接
        parent = state.get('parent') or None
        style = state.get('style')
        if style is not None:
            user32.SetWindowLongPtrW(hwnd_child, GWL_STYLE, style)
        user32.SetParent(hwnd_child, ctypes.c_void_p(parent or 0))
        rect = state.get('orig_rect')
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW
        if rect:
            user32.SetWindowPos(hwnd_child, None, rect['x'], rect['y'],
                                rect['width'], rect['height'], flags)
        else:
            user32.SetWindowPos(hwnd_child, None, 0, 0, 0, 0,
                                flags | SWP_NOMOVE | SWP_NOSIZE)
        return {'ok': True, 'was_embedded': True, 'hwnd': hwnd_child,
                'restored_parent': parent, 'restored_rect': rect,
                'style': style_of(hwnd_child), 'dpi': dpi_of(hwnd_child)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def embedded_children():
    """当前处于嵌入状态的子窗口列表（供状态查询与"防孤儿"检查）。"""
    user32 = _user32()
    with _STATE_LOCK:
        items = list(_CHILD_STATE.items())
    out = []
    for hwnd, state in items:
        out.append({'hwnd': hwnd, 'host': state.get('host'), 'mode': state.get('mode'),
                    'placed': state.get('placed'),
                    'alive': is_window(hwnd) if user32 else False})
    return out


def forget(hwnd_child):
    """引擎进程已退出时清理登记（窗口随进程销毁，没有可解除的东西）。"""
    with _STATE_LOCK:
        state = _CHILD_STATE.pop(int(hwnd_child), None)
    if state:
        _detach_input_queues(state)   # 进程已死，挂接多已自动释放，这里尽力清理
    return state is not None


# ---------------------------------------------------------------------------
# 焦点
# ---------------------------------------------------------------------------
def focus(hwnd_child: int, hwnd_host=None, keep_attached: bool = False):
    """把键盘焦点真正交给子窗口，并用 `GetFocus()` 回读验证。

    跨进程 `SetFocus` 直接调用一定失败（焦点属于线程的输入队列），
    必须先 `AttachThreadInput` 把两个线程的输入队列接起来。

    ``keep_attached=True`` 时**不解除挂接**。这是个真实需要：Windows 只把键盘输入
    投给"活动输入队列"里的焦点窗口；引擎窗口属于另一个进程/线程，一旦解除挂接，
    焦点就退回宿主自己的队列，之后合成的/用户的按键都不会再到引擎——
    表现就是"窗口看着嵌进去了，但按键没反应"。
    """
    user32 = _user32()
    if user32 is None:
        return {'ok': False, 'error': '仅 Windows 支持'}
    try:
        hwnd_child = int(hwnd_child)
        if not is_window(hwnd_child):
            return {'ok': False, 'error': '引擎窗口已失效'}
        host = int(hwnd_host or 0)
        if not host:
            state = _CHILD_STATE.get(hwnd_child) or {}
            host = int(state.get('host') or user32.GetParent(hwnd_child) or 0)
        if host and user32.IsIconic(host):
            user32.ShowWindow(host, 9)  # SW_RESTORE

        k32 = ctypes.windll.kernel32
        target_thread = user32.GetWindowThreadProcessId(hwnd_child, None)
        foreground = user32.GetForegroundWindow()
        foreground_thread = (user32.GetWindowThreadProcessId(foreground, None)
                             if foreground else 0)
        current_thread = k32.GetCurrentThreadId()
        with _STATE_LOCK:
            target_state = _CHILD_STATE.setdefault(int(hwnd_child), {})
        via = []
        attached_pairs = []   # [(by_thread_id, partner_thread_id), ...]
        try:
            # 只 AttachThreadInput 到引擎线程还不够：后台进程调用 SetForegroundWindow
            # 会被系统直接拒绝（"不允许抢前台"）。必须同时接上当前前台线程的输入队列，
            # Windows 才认我们是"有资格设置前台"的那个进程。
            for thread in {int(target_thread), int(foreground_thread)}:
                if thread and thread != int(current_thread):
                    if user32.AttachThreadInput(int(current_thread), thread, True):
                        attached_pairs.append((int(current_thread), thread))
            if host:
                user32.BringWindowToTop(host)
                if user32.SetForegroundWindow(host):
                    via.append('setforeground')
            user32.SetFocus(hwnd_child)
            via.append('setfocus')
            got = int(user32.GetFocus() or 0)
            if got != hwnd_child:
                # 有些引擎会在收到 WM_SETFOCUS 前自己抢回焦点，再试一次
                user32.SetFocus(hwnd_child)
                got = int(user32.GetFocus() or 0)
            now_foreground = int(user32.GetForegroundWindow() or 0)
            if keep_attached:
                with _STATE_LOCK:
                    existing = target_state.get('attached_pairs') or []
                    merged = list(existing)
                    for p in attached_pairs:
                        if p not in merged:
                            merged.append(p)
                    target_state['attached_pairs'] = merged
                    # 兼容旧字段（attached 只记伙伴线程，供返回/旧调用方读）
                    target_state['attached'] = [t for _, t in merged]
                    target_state['attached_by'] = int(current_thread)
        finally:
            if not keep_attached:
                for by_t, partner in attached_pairs:
                    try:
                        user32.AttachThreadInput(ctypes.c_ulong(by_t),
                                                 ctypes.c_ulong(partner), False)
                    except Exception:  # noqa: BLE001
                        pass
        partner_threads = [t for _, t in attached_pairs]
        return {'ok': got == hwnd_child, 'focused': got, 'hwnd': hwnd_child,
                'attached': partner_threads, 'kept_attached': bool(keep_attached),
                'foreground': now_foreground,
                'foreground_is_host': (now_foreground == host),
                'via': via}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def resize(hwnd_child: int, width: int, height: int):
    """按显式宽高调整（保留给旧调用方）。新代码优先用 `fill_host`。"""
    user32 = _user32()
    if user32 is None:
        return False
    try:
        return bool(user32.MoveWindow(int(hwnd_child), 0, 0, int(width), int(height), True))
    except Exception:  # noqa: BLE001
        return False


def terminate_tree(pid: int, timeout: float = 5.0):
    """结束进程及其全部后代，返回被结束的 pid 列表。

    Godot 的 `*_console.exe` 是转发器（会派生子进程）；只 terminate 直接子进程
    会留下一个真正的 GUI 进程和它的窗口 —— 那就是"孤儿窗口"的来源。

    等待策略：先一次性 TerminateProcess 整棵进程树，再用 WaitForMultipleObjects
    一次性并行等所有句柄（整体超时），而不是对每个句柄串行 WaitForSingleObject(满超时)——
    后者在 console+GUI 多进程时要把超时累加好几轮，停止明显变慢。
    """
    if os.name != 'nt':
        return []
    k32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    pids = sorted(_descendant_pids(int(pid)) - {os.getpid()}, reverse=True)
    killed = []
    handles = []
    for target in pids:
        handle = k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(target))
        if not handle:
            continue
        k32.TerminateProcess(handle, 1)
        handles.append((target, handle))
        killed.append(target)
    if handles:
        # MAXIMUM_WAIT_OBJECTS = 64，超过要分批
        for i in range(0, len(handles), 64):
            batch = handles[i:i + 64]
            arr = (ctypes.c_void_p * len(batch))(*[h for _, h in batch])
            try:
                k32.WaitForMultipleObjects(len(arr), arr, True, int(timeout * 1000))
            except Exception:  # noqa: BLE001
                pass
        for _, handle in handles:
            try:
                k32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
    return killed
