# -*- coding: utf-8 -*-
"""按【后台任务 id】找到它自己启动的窗口，并抓这个窗口的画面。

为什么要有这条路
----------------
agent 用 `run_command(<启动命令>, background: true)` 把游戏跑起来之后，想"看见"它，
只有三条路：

1. `game_screenshot(target: foreground)` —— 抓的是**当前有焦点的窗口**。那个焦点
   几乎总是这个聊天应用自己（实测抓到 1706x1018 的 WorkBuddy 窗口，而不是 804x604
   的游戏窗口），于是模型拿着聊天应用的画面当"游戏运行画面"，越看越糊涂。
2. 引擎那条路（`game_workbench.engine_window_target`）—— 只认**引擎登记表**里的进程。
   tkinter / Pygame 这类普通 GUI 程序根本不在登记表里，等于没有。
3. 这里：**任务 id → 它的进程 → 它的顶层窗口**。

边界（很重要）
--------------
* pid 只来自**本进程自己启动的后台任务登记表**，不是"截任意窗口"的后门：
  外部进程 id 一概不认。
* 抓不到窗口就返回 None，由调用方**明确报错**——**不许**静默退回前台窗口。
  静默退回正是"抓到聊天应用却以为看到了游戏"的坑本身。
* 找窗只认顶层可见窗口，且会连**后代进程**一起找（console 版包装器会派生子进程，
  真正的窗口在子进程上）。
"""
from __future__ import annotations

import re
import time

__all__ = [
    "parse_job_id",
    "job_pid",
    "restore_window",
    "job_window_target",
    "capture_job_window",
    "describe_missing_window",
]

# 任务 id 的键名：模型常写 job_id / jobid / job id。
_JOB_KEYS = {"job_id", "jobid", "job", "job id", "任务id", "任务 id"}
# 裸 id 形态（没有冒号）：后台任务 id 是 uuid4().hex[:10] —— 十六进制。
_BARE_ID_RE = re.compile(r"^[0-9a-f]{6,32}$", re.IGNORECASE)


def parse_job_id(arg) -> str:
    """从工具参数里取出后台任务 id；取不到返回空串。

    为什么不复用 tools._parse_keyed：那个函数的**已知键**之外的值会一路吞进上一个
    字段（`limit` 键污染 `job_id` 就是这么来的）。这里要的是一个**只认 job_id、
    绝不影响别的字段**的独立解析，所以自己按行取。
    """
    text = str(arg or "").strip()
    if not text:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key.strip().lower() in _JOB_KEYS:
            return value.strip()
    # 整段只有一个裸 token 且像任务 id（十六进制）时，按裸 id 处理。
    if "\n" not in text and " " not in text and _BARE_ID_RE.match(text):
        return text
    return ""


def job_pid(job_id) -> int:
    """后台任务主进程 pid；任务不在登记表里（或已清理）返回 0。"""
    try:
        from agent_runtime import process_runner
    except Exception:  # noqa: BLE001 - 拿不到任务表就当"没有窗口"
        return 0
    try:
        return int(process_runner.job_pid(str(job_id or "").strip()))
    except Exception:  # noqa: BLE001
        return 0


def _bridge_restore():
    """desktop_bridge 里的 restore_window（还没入库时返回 None）。"""
    try:
        import desktop_bridge
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(desktop_bridge, "restore_window", None)
    return fn if callable(fn) else None


def restore_window(hwnd):
    """把窗口还原并置前，返回 {ok, restored, foreground}。

    为什么需要：有些游戏窗口启动后会自己最小化（客户区塌成 0x0），此时既抓不到
    像素、也读不到有效尺寸；还原 + 置前后才落得到真实句柄。

    实现说明：优先用 `desktop_bridge.restore_window`（若已入库），否则用这里自带的
    最小实现。**不能只依赖前者** —— 那份实现还没进仓库，只在别人的在制品工作树里
    有；只依赖它的代码在 CI（干净检出）上会直接 AttributeError。
    """
    bridge_restore = _bridge_restore()
    if bridge_restore is not None:
        try:
            return bridge_restore(hwnd)
        except Exception:  # noqa: BLE001 - 还原失败不该让整次抓取崩掉
            pass
    return _restore_window_own(hwnd)


def _restore_window_own(hwnd):
    """自带的最简还原实现（Windows）：SW_RESTORE + BringWindowToTop + SetForegroundWindow。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001 - 非 Windows 或拿不到 user32
        return {"ok": False, "restored": False, "foreground": False, "error": "仅 Windows 支持"}
    try:
        handle = int(hwnd)
        if not user32.IsWindow(handle):
            return {"ok": False, "restored": False, "foreground": False,
                    "error": "窗口句柄已失效"}
        restored = False
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE
            restored = True
        user32.BringWindowToTop(handle)
        foreground = bool(user32.SetForegroundWindow(handle))
        return {"ok": True, "restored": restored, "foreground": foreground}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "restored": False, "foreground": False, "error": str(exc)}


def job_window_target(job_id, title_hint: str = ""):
    """定位后台任务自己启动的顶层窗口，返回 (hwnd, title)；找不到返回 None。

    找窗前先还原：不少游戏窗口启动后会自己最小化（客户区塌成 0x0），此时既抓不到
    像素也读不到有效尺寸，restore_window 把它还原 + 置前后才落得到真实句柄。
    """
    pid = job_pid(job_id)
    if not pid:
        return None
    try:
        import desktop_bridge
    except Exception:  # noqa: BLE001
        return None
    try:
        found = desktop_bridge.find_window(pid, title_hint or "")
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    try:
        hwnd, title = int(found[0]), str(found[1] or "")
    except (TypeError, ValueError, IndexError):
        return None
    try:
        restore_window(hwnd)
    except Exception:  # noqa: BLE001 - 还原失败不该让整次抓取崩掉
        pass
    # 还原/置前后窗口要重建客户区，立刻抓可能抓到 0x0 的旧画面。
    time.sleep(0.35)
    return hwnd, title


def capture_job_window(job_id, title_hint: str = ""):
    """抓后台任务自己启动的窗口画面。

    返回 ((raw, width, height, hwnd), title)；抓不到返回 None。
    """
    target = job_window_target(job_id, title_hint)
    if not target:
        return None
    hwnd, title = target
    try:
        import screen_capture
        grabbed = screen_capture.capture_window(hwnd)
    except Exception:  # noqa: BLE001
        return None
    if not grabbed:
        return None
    try:
        raw, width, height = grabbed
    except (TypeError, ValueError):
        return None
    return (raw, width, height, hwnd), title


def describe_missing_window(job_id) -> str:
    """给"抓不到窗口"配一句**能指路**的说明。

    模型看到"截图失败"会立刻重试或改去抓前台窗口（又是错的画面）。这句必须说清
    是哪种情况：任务根本不存在 / 已经结束所以窗口没了 / 还在跑但没有窗口。
    """
    ident = str(job_id or "").strip()
    try:
        from agent_runtime import process_runner
        info = process_runner.job_logs(ident)
    except Exception:  # noqa: BLE001
        info = {}
    if not isinstance(info, dict) or not info.get("ok"):
        return ("后台任务 %s 不存在或已被清理：请先用 dev_job_logs（不带 job_id）"
                "列出当前在跑的任务，照着上面的 id 重来。" % (ident or "<空>"))
    state = str(info.get("state") or "")
    if state in ("done", "failed", "timeout", "cancelled"):
        return ("后台任务 %s 已经结束（state=%s），它开的窗口随之没了——"
                "要看画面得在它还活着的时候抓。" % (ident, state))
    return ("后台任务 %s 还在跑（state=%s），但没有找到它的顶层窗口："
            "程序可能还在启动，或者这条命令根本没有窗口（控制台程序请直接看日志）。"
            % (ident, state or "running"))
