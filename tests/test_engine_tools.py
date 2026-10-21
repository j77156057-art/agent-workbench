# -*- coding: utf-8 -*-
"""agent 工具层的引擎控制出口（engine_start / engine_stop / engine_reload）——离线可跑。

## 为什么这样测

这三个函数的价值全在两件事上：**不重写启动逻辑**（底层护栏只有一份）和
**失败也要能读**（异常变成数据，而不是一句看不懂的 traceback）。

真去起一次引擎要 Godot + GPU + 桌面宿主，CI 上三样都没有。所以这里把
`game_workbench.engine_start / engine_stop / engine_reload` 整个打桩成替身，
然后断两件事：

1. **替身收到了什么**——特别是 `embed`：没有桌面宿主窗口时，必须**不**向底层
   请求嵌入（底层的 embed 分支会满世界找宿主窗口，白等一轮还容易把
   "没嵌上"说成"启动失败"）。
2. **返回值带出了什么**——底层的 `running`/`pid`/`gpu`/`embedded`/`embed_error`
   一个都不能丢；底层抛异常时必须变成 `ok=False` + 中文原因，而不是穿透出去。

## 打桩方式

`agent_runtime.engine_tools` 用的是 `import game_workbench` + 调用时取属性
（不是 `from game_workbench import engine_start`），所以这里 monkeypatch
`game_workbench.engine_start` **是生效的**——这是选模块级 import 的唯一理由：
from-import 会在导入时把函数对象绑死，测试根本打不到真正被调用的那个名字。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_workbench  # noqa: E402
from agent_runtime import engine_tools  # noqa: E402


def _install(monkeypatch, name, fake):
    """把底层函数换成替身；monkeypatch 会在用例结束后自动还原。"""
    monkeypatch.setattr(game_workbench, name, fake)


def _recorder(result=None, error=None):
    """造一个"会记调用参数的替身"，返回 (替身, 记录字典)。"""
    seen = {}

    def fake(root, *args, **kwargs):
        seen["root"] = root
        seen["args"] = args
        seen["kwargs"] = kwargs
        if error is not None:
            raise error
        return dict(result or {})

    return fake, seen


# ---- 参数校验：root 不合法时**不许**碰底层 ----

def test_empty_root_fails_without_touching_backend(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "running": True, "pid": 1})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start("", embed=True, host_hwnd=123)

    assert out["ok"] is False
    assert "项目根目录" in out["error"]
    assert seen == {}, "root 不合法就不该启动任何东西"


def test_root_that_is_not_a_directory_fails_with_the_path(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "running": True, "pid": 1})
    _install(monkeypatch, "engine_start", fake)

    missing = tmp_path / "not_a_project"
    out = engine_tools.engine_start(str(missing))

    assert out["ok"] is False
    assert str(missing) in out["error"], "报错要让人知道是哪个目录不对"
    assert seen == {}


def test_stop_and_reload_also_reject_an_empty_root(monkeypatch):
    fake, seen = _recorder({"ok": True})
    _install(monkeypatch, "engine_stop", fake)
    _install(monkeypatch, "engine_reload", fake)

    assert engine_tools.engine_stop("")["ok"] is False
    assert engine_tools.engine_reload("")["ok"] is False
    assert seen == {}


# ---- 正常路径：底层字段原样透传 ----

def test_start_passes_backend_fields_through(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "running": True, "pid": 4242,
                            "gpu": "GPU:0", "notice": "显存紧张"})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path), scene="res://main.tscn")

    assert out["ok"] is True
    assert out["running"] is True
    assert out["pid"] == 4242
    assert out["gpu"] == "GPU:0"
    assert out["notice"] == "显存紧张"
    # 调用参数也要对：场景透传，没要求嵌入就不许偷偷要嵌入
    assert seen["root"] == str(tmp_path)
    assert seen["kwargs"]["scene"] == "res://main.tscn"
    assert seen["kwargs"]["embed"] is False


def test_stop_passes_backend_fields_through(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "stopped": True, "pid": 99,
                            "detached": True, "killed": ["godot.exe"]})
    _install(monkeypatch, "engine_stop", fake)

    out = engine_tools.engine_stop(str(tmp_path))

    assert out["ok"] is True
    assert out["stopped"] is True
    assert out["pid"] == 99
    assert out["detached"] is True
    assert out["killed"] == ["godot.exe"]
    assert seen["root"] == str(tmp_path)


def test_reload_passes_backend_fields_through(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "running": True, "pid": 1234,
                            "reloaded": True, "reload_mode": "process_restart",
                            "previous_pid": 1000})
    _install(monkeypatch, "engine_reload", fake)

    out = engine_tools.engine_reload(str(tmp_path))

    assert out["ok"] is True
    assert out["reloaded"] is True
    assert out["reload_mode"] == "process_restart"
    assert out["pid"] == 1234
    assert out["previous_pid"] == 1000
    assert seen["root"] == str(tmp_path)


# ---- embed 语义：没有宿主窗口就不请求嵌入，但要**如实**说没嵌 ----

def test_embed_without_host_window_reports_not_embedded(monkeypatch, tmp_path):
    """embed=True 但没有桌面宿主：引擎照常起，但返回里必须看得出"没嵌"。"""
    fake, seen = _recorder({"ok": True, "running": True, "pid": 7})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path), embed=True, host_hwnd=0)

    assert out["ok"] is True, "没有宿主窗口不等于启动失败"
    assert out["running"] is True
    assert out["embedded"] is False, "没嵌上就必须说没嵌上"
    assert "宿主" in out["embed_error"]
    assert out["embed_requested"] is False
    # 关键：不能把 embed=True 递给底层——底层的 embed 分支会去找宿主窗口。
    assert seen["kwargs"]["embed"] is False


def test_embed_with_host_window_is_forwarded_to_the_backend(monkeypatch, tmp_path):
    fake, seen = _recorder({"ok": True, "running": True, "pid": 8,
                            "embedded": True, "hwnd": 4321})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path), embed=True, host_hwnd=4321)

    assert seen["kwargs"]["embed"] is True
    assert seen["kwargs"]["host_hwnd"] == 4321
    assert out["embedded"] is True
    assert out["embed_requested"] is True


def test_backend_embed_failure_is_not_swallowed(monkeypatch, tmp_path):
    """底层嵌入失败（窗口没出来）时，embed_error 必须是底层的原话，不能被覆盖。"""
    fake, _ = _recorder({"ok": True, "running": True, "pid": 9, "embedded": False,
                         "embed_error": "等待引擎窗口超时。"})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path), embed=True, host_hwnd=4321)

    assert out["embedded"] is False
    assert out["embed_error"] == "等待引擎窗口超时。"


def test_already_running_engine_keeps_its_embedded_flag(monkeypatch, tmp_path):
    """引擎已经在跑（底层直接返回 engine_status）时，别把 embedded 覆盖成 False。"""
    fake, _ = _recorder({"ok": True, "running": True, "pid": 5, "embedded": True})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path), embed=True, host_hwnd=0)

    assert out["embedded"] is True, "已经在宿主窗口里嵌着，就别再说没嵌"


def test_start_without_embed_still_reports_embedded_false(monkeypatch, tmp_path):
    """没要求嵌入时也要有明确的 embedded=False，而不是缺这个键让模型猜。"""
    fake, _ = _recorder({"ok": True, "running": True, "pid": 10})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path))

    assert out["embedded"] is False
    assert out["embed_requested"] is False


# ---- 异常兜底：异常必须变成数据失败 ----

def test_start_backend_exception_becomes_a_data_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("GPU 租约拿不到")

    _install(monkeypatch, "engine_start", boom)

    out = engine_tools.engine_start(str(tmp_path))

    assert out["ok"] is False
    assert "启动引擎失败" in out["error"]
    assert "GPU 租约拿不到" in out["error"], "原因要带出来，否则模型没法下一步"


def test_stop_backend_exception_becomes_a_data_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("进程树杀不掉")

    _install(monkeypatch, "engine_stop", boom)

    out = engine_tools.engine_stop(str(tmp_path))

    assert out["ok"] is False
    assert "停止引擎失败" in out["error"]
    assert "进程树杀不掉" in out["error"]


def test_reload_backend_exception_becomes_a_data_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("重启超时")

    _install(monkeypatch, "engine_reload", boom)

    out = engine_tools.engine_reload(str(tmp_path))

    assert out["ok"] is False
    assert "热重载失败" in out["error"]
    assert "重启超时" in out["error"]


def test_backend_failure_result_is_passed_through_untouched(monkeypatch, tmp_path):
    """底层自己判失败（GPU 正忙 / 找不到可执行文件）时，错误原因要原样透传。"""
    fake, _ = _recorder({"ok": False, "error": "GPU 资源正忙，无法启动引擎。",
                         "reason": "exclusive"})
    _install(monkeypatch, "engine_start", fake)

    out = engine_tools.engine_start(str(tmp_path))

    assert out["ok"] is False
    assert out["error"] == "GPU 资源正忙，无法启动引擎。"
    assert out["reason"] == "exclusive"
