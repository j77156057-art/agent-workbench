"""agent 工具层的引擎控制出口：engine_start / engine_stop / engine_reload。

## 这层是什么

HTTP 端点 `POST /api/engine/start|stop|reload`（api.py）早就能真正控制引擎，
但那只是**给人点 UI 用的**出口。模型此前只能等用户手动点完，再用 `game_screenshot`
看一眼已经跑起来的引擎 —— 于是「起引擎 → 截图 → 看画面 → 迭代」这条闭环对模型是断的：
模型自己不能起，也就没法自己判断"这轮改完画面到底变了没有"。

本模块是**同一条路径上的 agent 出口**：tools.py 把下面三个普通函数包成 agent 工具即可。
本模块自己**不是** agent 工具，也不做参数解析、不返回 ToolResult —— 它只负责
「校验参数 → 调底层 → 把结果翻译成模型能读的数据」。

## 为什么复用 game_workbench，而不是在这里重写一套启动逻辑

启动引擎远不止一行 `subprocess.Popen`：它要先拿 GPU 软租约（把卡号注入
CUDA_VISIBLE_DEVICES）、把 stdout/stderr 接到项目的 engine.log、登记进程与看门狗、
失败时释放租约、启动后按需把窗口嵌进桌面宿主。这些护栏**只在 game_workbench
那一份实现里**。

在这里另起一套，等于凭空多出第二套 GPU 租约、第二份引擎日志、第二份进程表：
用户点 UI 起的引擎和模型自己起的引擎互相看不见（`engine_status` 认不出来），
租约也会泄漏 —— 那正是"引擎还在跑但显存说正忙"这类诡异现象的来源。
所以这里**只调底层，不重写**：HTTP 端点和 agent 工具共用同一份护栏、同一份日志、
同一份状态。

## host_hwnd 从哪来

`host_hwnd` 是**桌面宿主窗口的句柄**（Win32 HWND），由调用方（tools.py 的工具包装层）
从桌面宿主取到后传进来；纯 Web / 无桌面宿主的场景没有这个句柄，就是 0。

句柄为 0 时**不做嵌入**：底层的 embed 分支会满世界找宿主窗口，没有宿主时既白等一轮
轮询，又容易把"没嵌上"说成"启动失败"。此时引擎照常启动，返回里**如实**带
`embedded=False` 与 `embed_error`，让模型能分清三件事：
「引擎根本没起来」「引擎起来了但没嵌进窗口」「引擎起来了且已嵌入」。

## 返回体约定

一律是 dict，且一定含 `ok: bool`：

- 成功：底层结果**原样透传**（`running`/`pid`/`gpu`/`embedded`/`embed_error`/`notice` 等键
  一个都不丢）；额外只补一个 `embed_requested`（是否向底层请求过嵌入），
  用来区分「压根没要求嵌入」和「要求了但没嵌上」。
- 失败：`{"ok": False, "error": "……"}`，文案是中文且能指下一步动作。
- 底层抛异常也走失败分支：agent 工具层只会把异常变成一句难读的话，
  **数据失败**才能让模型自己决定下一步。

## 一个调用方必须知道的坑

底层在请求嵌入时会**同步轮询最多 6 秒**（30 次 × 0.2s）去找引擎窗口。
tools.py 侧应当把这三个函数放到线程池里调用，否则会卡住 agent 的事件循环。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# 刻意用 `import game_workbench` 而不是 `from game_workbench import engine_start`：
# from-import 会在**导入时**把函数对象绑死，测试里 monkeypatch `game_workbench.engine_start`
# 将不生效（打桩打不到真正被调用的那个名字）。模块级 import + 调用时再取属性，
# 才让这层可以被离线打桩验证，也才让"底层换实现"时这里不用改。
import game_workbench

__all__ = ["engine_start", "engine_stop", "engine_reload"]

#: 底层 `engine_start` 在"没有桌面宿主"时用的同一句文案，这里自己写一遍是刻意的：
#: 模型看到的是**同一套措辞**，不会因为走 agent 通道就换一种说法。
#: 后面补的那半句是给模型看的：引擎是起来了的，只是没嵌进窗口，别去重试启动。
_NO_HOST_EMBED_ERROR = ("没有桌面宿主窗口（请用桌面端启动工作台）。"
                        "引擎已启动，但没有嵌入窗口。")


def _root_error(root: Any) -> Optional[Dict[str, Any]]:
    """参数校验：root 必须是一个已存在的目录。

    为什么在这里挡：底层的 `_root()` 只做 `abspath`，空串会变成**进程当前目录** ——
    "没配置代码库"就会变成"在仓库根目录里乱起一个引擎"，比报错糟得多。
    """
    if not root or not str(root).strip():
        return {"ok": False,
                "error": "未提供项目根目录：请先配置代码库，或在调用时传入项目目录。"}
    path = os.path.abspath(str(root))
    if not os.path.isdir(path):
        return {"ok": False,
                "error": "项目根目录不存在或不是目录：%s。请传入有效的项目目录。" % path}
    return None


def _coerce_hwnd(host_hwnd: Any) -> int:
    """把句柄收敛成整数；拿不准的一律当 0（= 没有桌面宿主 = 不嵌入）。"""
    try:
        return int(host_hwnd or 0)
    except (TypeError, ValueError):
        return 0


def _as_result(raw: Any, default_ok: bool) -> Optional[Dict[str, Any]]:
    """把底层的返回值收敛成 dict。

    返回 None 表示底层给了一个**没法用**的东西（不是 dict）—— 这种情况下不能
    让它冒充成功，必须当成失败处理。
    """
    if not isinstance(raw, dict):
        return None
    out = dict(raw)
    if "ok" not in out:
        # 底层正常路径都带 ok；这里只是兜底，免得"缺 ok"让工具层误判成成功。
        out["ok"] = bool(default_ok)
    return out


def engine_start(root: str, *, scene: str = "", embed: bool = False,
                 host_hwnd: int = 0) -> Dict[str, Any]:
    """启动引擎（走 `game_workbench.engine_start` 的同一条路径）。

    - `scene`：直接透传给底层；Godot 下带 scene 会以编辑器模式打开该场景。
    - `embed` / `host_hwnd`：嵌入是**双条件**——调用方要 embed，**且**真的有桌面宿主
      窗口。缺一不满足就不请求嵌入，并把"没嵌上"如实写进返回值。
    """
    invalid = _root_error(root)
    if invalid is not None:
        return invalid
    hwnd = _coerce_hwnd(host_hwnd)
    want_embed = bool(embed) and hwnd > 0
    try:
        raw = game_workbench.engine_start(root, scene=scene or "",
                                          host_hwnd=hwnd, embed=want_embed)
    except Exception as exc:  # noqa: BLE001 —— 异常必须变成数据失败，不能穿透到 agent 工具层
        return {"ok": False, "error": "启动引擎失败：%s" % exc}
    out = _as_result(raw, default_ok=bool(isinstance(raw, dict) and raw.get("running")))
    if out is None:
        return {"ok": False,
                "error": "启动引擎失败：底层返回了非预期的结果（%s）。" % type(raw).__name__}
    if out.get("ok"):
        # 记录"是否向底层请求过嵌入"：模型据此区分「没要求嵌」与「要求了但没嵌上」。
        out["embed_requested"] = want_embed
        if embed and hwnd <= 0:
            # 说实话：没有宿主就是没嵌。别让"没嵌上"被模型读成"启动失败"而去重试启动。
            if not out.get("embedded"):
                out["embedded"] = False
                out["embed_error"] = _NO_HOST_EMBED_ERROR
        else:
            # 底层只在**自己发起的嵌入**上写 embedded；这里补齐，让"引擎在跑但没嵌"
            # 永远有一个明确的 False，而不是一个缺失的键。
            out.setdefault("embedded", False)
    return out


def engine_stop(root: str) -> Dict[str, Any]:
    """停止引擎（走 `game_workbench.engine_stop` 的同一条路径）。

    底层会先解除嵌入再结束进程、并杀掉整棵进程树（Godot 的 `*_console.exe` 只是
    转发器，只 terminate 直接子进程会留下孤儿窗口）——这些都在底层，这里不重复实现。
    """
    invalid = _root_error(root)
    if invalid is not None:
        return invalid
    try:
        raw = game_workbench.engine_stop(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "停止引擎失败：%s" % exc}
    out = _as_result(raw, default_ok=bool(isinstance(raw, dict) and raw.get("stopped")))
    if out is None:
        return {"ok": False,
                "error": "停止引擎失败：底层返回了非预期的结果（%s）。" % type(raw).__name__}
    return out


def engine_reload(root: str) -> Dict[str, Any]:
    """热重载引擎（走 `game_workbench.engine_reload` 的同一条路径）。

    底层是"可审计的快速重启"（保存启动参数与嵌入矩形 → 停旧进程 → 同参重启），
    并会在返回值里带上 `reloaded` / `reload_mode` / `previous_pid`。
    非 Godot 引擎或引擎没在跑时，底层会自己返回 ok=False 和中文原因，这里原样透传。
    """
    invalid = _root_error(root)
    if invalid is not None:
        return invalid
    try:
        raw = game_workbench.engine_reload(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "热重载失败：%s" % exc}
    out = _as_result(raw, default_ok=bool(isinstance(raw, dict) and raw.get("running")))
    if out is None:
        return {"ok": False,
                "error": "热重载失败：底层返回了非预期的结果（%s）。" % type(raw).__name__}
    return out
