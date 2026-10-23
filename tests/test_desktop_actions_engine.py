# -*- coding: utf-8 -*-
"""desktop_actions 的"引擎独立窗口"目标解析（离线可跑）。

安全不变式：desktop_actions **自己**按 root→本项目引擎进程表拿句柄，绝不接受外部
传入的 HWND。这几条测的是"解析路径正确 + 解析不到时 fail-closed"，不真点屏幕。
"""
import os

import pytest

import desktop_actions
import game_workbench


def test_engine_target_delegates_to_engine_window_target(monkeypatch):
    """_engine_target 只是把 root 转交给 game_workbench.engine_window_target 并取句柄。"""
    monkeypatch.setattr(game_workbench, "engine_window_target",
                        lambda root, hint="": (5555, "StarVoyager (DEBUG)"))
    assert desktop_actions._engine_target("D:/proj") == 5555


def test_engine_target_without_root_is_zero():
    """没有项目根目录：返回 0（没有目标），绝不拿空 root 去猜某个窗口。"""
    assert desktop_actions._engine_target("") == 0
    assert desktop_actions._engine_target(None) == 0


def test_engine_target_none_when_no_engine(monkeypatch):
    """底层找不到本项目引擎：返回 0，交给上层判"没有可操作目标窗口"。"""
    monkeypatch.setattr(game_workbench, "engine_window_target",
                        lambda root, hint="": None)
    assert desktop_actions._engine_target("D:/proj") == 0


def test_engine_target_swallows_exception(monkeypatch):
    """解析途中抛异常也要吞成 0（fail-closed），不能把栈穿出去、更不能误点别的窗。"""
    def boom(root, hint=""):
        raise RuntimeError("窗口枚举炸了")

    monkeypatch.setattr(game_workbench, "engine_window_target", boom)
    assert desktop_actions._engine_target("D:/proj") == 0


@pytest.mark.skipif(os.name != "nt", reason="目标解析在非 Windows 上短路返回错误")
def test_resolve_target_engine_branch_uses_resolved_handle(monkeypatch):
    """_resolve_target("engine", root=...) 走本项目引擎句柄，并读出客户区尺寸。"""
    import desktop_bridge

    monkeypatch.setattr(desktop_actions, "_engine_target", lambda root: 4242, raising=False)
    monkeypatch.setattr(desktop_bridge, "is_window", lambda hwnd: True)
    monkeypatch.setattr(desktop_bridge, "client_origin",
                        lambda hwnd: {"x": 100, "y": 50})
    monkeypatch.setattr(desktop_bridge, "client_rect",
                        lambda hwnd: {"width": 800, "height": 600})
    monkeypatch.setattr(desktop_bridge, "window_rect",
                        lambda hwnd: {"x": 0, "y": 0, "width": 800, "height": 600})

    hwnd, info, error = desktop_actions._resolve_target("engine", None, "D:/proj")

    assert error == ""
    assert hwnd == 4242
    assert info["rect"]["width"] == 800


@pytest.mark.skipif(os.name != "nt", reason="目标解析在非 Windows 上短路返回错误")
def test_resolve_target_engine_branch_fails_closed_without_window(monkeypatch):
    """解析不到引擎句柄：返回明确错误，绝不退回前台去点聊天应用。"""
    import desktop_bridge

    monkeypatch.setattr(desktop_actions, "_engine_target", lambda root: 0, raising=False)
    monkeypatch.setattr(desktop_bridge, "is_window", lambda hwnd: True)

    hwnd, info, error = desktop_actions._resolve_target("engine", None, "D:/proj")

    assert hwnd is None and info is None
    assert "没有找到可操作的目标窗口" in error


def test_describe_point_reports_real_region():
    """853×480 上的 (640,360) 是 75%/75%（右下），不是「画面中央」。"""
    point = desktop_actions._describe_point(640, 360, {"width": 853, "height": 480})
    assert point["x_ratio"] == 0.75
    assert point["y_ratio"] == 0.75
    assert point["region"] == "右下"
    center = desktop_actions._describe_point(426, 240, {"width": 853, "height": 480})
    assert center["region"] == "中"


def test_perform_click_returns_placement(monkeypatch):
    """点击结果必须带归一化位置，供上层叙述与核验对齐。"""
    info = {"origin": {"x": 100, "y": 50},
            "rect": {"width": 800, "height": 600}}
    monkeypatch.setattr(desktop_actions, "_resolve_target",
                        lambda target, project_id, root: (4242, info, ""))
    monkeypatch.setattr(desktop_actions, "_focus",
                        lambda hwnd, target, project_id: (True, ""))
    monkeypatch.setattr(desktop_actions, "_mouse_click",
                        lambda x, y: (True, 2))

    result = desktop_actions.perform("click", target="embedded", x=400, y=300)

    assert result["ok"] is True
    assert result["events"] == 2
    assert result["point"]["x_ratio"] == 0.5
    assert result["point"]["y_ratio"] == 0.5
    assert result["point"]["region"] == "中"
