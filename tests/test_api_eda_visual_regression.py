"""新路由 /api/eda/visual-regression 的离线装配测试（EDA 面板接入真窗口取像）。

这条路由是"取像源装配"层：把真实 GDI 截屏（前台/嵌入/指定 HWND）归一成 FrameSource，
交给已提交的 M5 `eda_visual_regression.save_eda_golden` / `check_eda_regression` 做
确定性 diff。本机无 EDA 工具、也无稳定真窗口，故**替身帧**注入：monkeypatch
`screen_capture.grab_foreground` 返回确定性 BGRA，验的是**路由装配**是否正确，
比对逻辑本身由 test_eda_visual_regression.py 覆盖。

关键断言全部对齐开工文档 §0 信条：
- 同源帧 → identical/pass（passed:true）。
- 换了内容 → 绝不 pass（fail-closed：regression/localized，passed:false，可升级 VLM）。
- 没 golden → needs_golden（不自动放行）。
- 抓不到帧 → no_frame（passed:false、escalate，不静默通过）。
- 没给 view/bbox → roi_required（拒绝整屏）。
- 没配代码库 → ok:false，不 500。
"""
import os
import sys

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_routes.eda as eda_routes  # noqa: E402


def _frame(seed=0, w=64, h=64):
    """确定性假画面：随 seed 变的对角渐变 → 打包成 BGRA 字节（同 GDI 布局）。"""
    yy, xx = np.mgrid[0:h, 0:w]
    r = ((xx * 3 + seed * 17) % 256).astype(np.uint8)
    g = ((yy * 5 + seed * 31) % 256).astype(np.uint8)
    b = (((xx + yy) * 2 + seed * 7) % 256).astype(np.uint8)
    bgra = np.dstack([b, g, r, np.full((h, w), 255, np.uint8)])
    return bgra.tobytes(), w, h


class _Ctx:
    """最小 ctx：路由只用 _project_root_or_error / _request_project_id。"""
    def __init__(self, root):
        self._root = root

    def _project_root_or_error(self):
        return self._root

    def _request_project_id(self):
        return None


@pytest.fixture
def vr(tmp_path, monkeypatch):
    """挂路由到最小 app + 注入替身前台帧。返回 (post, box)：改 box['frame'] 即改画面。"""
    box = {"frame": _frame(0), "alive": True}

    def fake_foreground(*a, **k):
        if not box["alive"]:
            return None
        raw, w, h = box["frame"]
        return (raw, w, h, 12345)

    monkeypatch.setattr("screen_capture.grab_foreground", fake_foreground)
    app = FastAPI()
    app.include_router(eda_routes.build_router(_Ctx(str(tmp_path))))
    client = TestClient(app)

    def post(mode, **over):
        body = {"mode": mode, "source": "foreground", "view": "layout",
                "golden_key": "t1"}
        body.update(over)
        r = client.post("/api/eda/visual-regression", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    return post, box


# ---- 正常链：存基线 → 同源比对通过 ----

def test_save_then_check_identical_passes(vr):
    post, _box = vr
    sv = post("save")
    assert sv["ok"] is True and sv["mode"] == "save"
    assert sv["saved"] is True, sv
    ck = post("check")
    assert ck["mode"] == "check"
    assert ck["verdict"] in ("identical", "pass"), ck
    assert ck["passed"] is True
    assert ck["golden_key"] == "t1"


# ---- fail-closed：换画面绝不放行 ----

def test_changed_frame_never_passes(vr):
    post, box = vr
    assert post("save")["saved"] is True
    box["frame"] = _frame(1)            # 内容变了（不同 seed 的渐变）
    ck = post("check")
    assert ck["passed"] is False, ck
    assert ck["verdict"] in ("regression", "localized"), ck
    assert ck.get("escalate_to_vlm") is True
    # 变化时应有可交 VLM/人看的局部 ROI 缩略图。
    assert ck.get("escalation", {}).get("roi_data_url", "").startswith("data:image/png")


# ---- 没 golden → needs_golden（不自动放行）----

def test_check_without_golden_is_needs_golden(vr):
    post, _box = vr
    ck = post("check", golden_key="nope")
    assert ck["verdict"] == "needs_golden"
    assert ck["passed"] is False


# ---- 抓不到帧 → no_frame（不静默通过）----

def test_no_frame_fail_closed(vr):
    post, box = vr
    box["alive"] = False
    ck = post("check")
    assert ck["verdict"] == "no_frame"
    assert ck["passed"] is False
    assert ck.get("escalate_to_vlm") is True


# ---- 拒绝整屏：没给 view/bbox → roi_required ----

def test_missing_roi_rejects_fullscreen(vr):
    post, _box = vr
    ck = post("check", view=None)
    assert ck["verdict"] == "roi_required"
    assert ck["passed"] is False


# ---- save 覆盖保护：overwrite=False 且已存在 → 不覆盖 ----

def test_save_no_overwrite_keeps_existing(vr):
    post, _box = vr
    assert post("save", golden_key="ow")["saved"] is True
    again = post("save", golden_key="ow", overwrite=False)
    assert again["saved"] is False
    assert again["verdict"] == "exists"


# ---- 没配代码库（_project_root_or_error 返回空）→ ok:false，不 500 ----

def test_missing_project_root_fails_gracefully(monkeypatch):
    monkeypatch.setattr("screen_capture.grab_foreground",
                        lambda *a, **k: _frame(0) + (1,))

    class _NoRoot:
        def _project_root_or_error(self):
            return None

    app = FastAPI()
    app.include_router(eda_routes.build_router(_NoRoot()))
    client = TestClient(app)
    r = client.post("/api/eda/visual-regression",
                    json={"mode": "check", "view": "layout", "golden_key": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "未配置代码库" in (body.get("error") or "")


# ---- 平台可切换取像后端（gdi/x11/vnc）分派 ----

def _client_for(tmp_path):
    app = FastAPI()
    app.include_router(eda_routes.build_router(_Ctx(str(tmp_path))))
    return TestClient(app)


def test_resolve_capture_auto_by_platform(monkeypatch):
    monkeypatch.setattr(eda_routes.os, "name", "nt")
    assert eda_routes.resolve_capture("auto") == "gdi"
    assert eda_routes.resolve_capture(None) == "gdi"          # 缺省即 auto
    monkeypatch.setattr(eda_routes.os, "name", "posix")
    assert eda_routes.resolve_capture("auto") == "x11"        # Linux 目标机默认走 X11
    assert eda_routes.resolve_capture("vnc") == "vnc"         # 显式值原样透传
    assert eda_routes.resolve_capture("bogus") == "gdi"       # 未知值保守回落


def test_make_frame_source_x11_wraps_m4_grabber(monkeypatch):
    frame = _frame(0)                                         # (bgra_bytes, w, h)
    seen = {}

    def fake_x11_grabber(window_id=None, **k):
        seen["wid"] = window_id

        def grabber(hwnd):
            return frame
        return grabber

    monkeypatch.setattr("agent_runtime.frame_grabber_linux.x11_grabber", fake_x11_grabber)
    req = eda_routes.VisualRegressionReq(capture="x11", window_id=777, view="layout")
    src = eda_routes.make_frame_source(req)
    out = src()
    assert seen["wid"] == 777                                 # window_id 透传给 M4 grabber
    assert out is not None and out[3] == 777                  # 第 4 位是 window_id（FrameSource 契约）
    assert out[1] == frame[1] and out[2] == frame[2]


def test_route_x11_save_then_check(tmp_path, monkeypatch):
    frame = _frame(0)
    monkeypatch.setattr("agent_runtime.frame_grabber_linux.x11_grabber",
                        lambda window_id=None, **k: (lambda hwnd: frame))
    client = _client_for(tmp_path)
    r = client.post("/api/eda/visual-regression", json={
        "mode": "save", "capture": "x11", "window_id": 55, "view": "layout", "golden_key": "x11k"})
    b = r.json()
    assert b["ok"] is True and b["capture"] == "x11" and b["saved"] is True, b
    r2 = client.post("/api/eda/visual-regression", json={
        "mode": "check", "capture": "x11", "window_id": 55, "view": "layout", "golden_key": "x11k"})
    b2 = r2.json()
    assert b2["capture"] == "x11" and b2["verdict"] in ("identical", "pass") and b2["passed"] is True, b2


def test_route_vnc_fail_closed(tmp_path, monkeypatch):
    # VNC 连不上（grabber 返回 None）→ 路由照旧 fail-closed 成 no_frame，绝不假绿。
    monkeypatch.setattr("agent_runtime.frame_grabber_linux.vnc_frame_grabber",
                        lambda host="127.0.0.1", port=5900, **k: (lambda hwnd=None: None))
    client = _client_for(tmp_path)
    r = client.post("/api/eda/visual-regression", json={
        "mode": "check", "capture": "vnc", "vnc_host": "10.0.0.9", "vnc_port": 5901,
        "view": "layout", "golden_key": "v"})
    b = r.json()
    assert b["capture"] == "vnc" and b["verdict"] == "no_frame" and b["passed"] is False, b


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
