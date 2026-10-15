# -*- coding: utf-8 -*-
"""M2·① 引擎取像链单测：抓帧全部喂注入替身，不碰 GDI/网络/VLM。"""
import numpy as np
import pytest

from agent_runtime import visual_regression as vr
from agent_runtime import engine_visual_regression as ev


def _bgra(width, height, pixel=(0, 0, 255, 255)):
    """BGRA 字节帧（对齐 test_game_screenshot 的取像替身约定）。"""
    return bytes(pixel) * (width * height)


def _bgra_from_rgb(rgb):
    """(H,W,3) uint8 → BGRX 字节 + (w,h)，供 fake source 复用真实像素。"""
    h, w = rgb.shape[:2]
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[:, :, 0] = rgb[:, :, 2]  # B
    out[:, :, 1] = rgb[:, :, 1]  # G
    out[:, :, 2] = rgb[:, :, 0]  # R
    out[:, :, 3] = 255           # X/占位
    return out.tobytes(), w, h


def _flat(h=64, w=64, val=128):
    return np.full((h, w, 3), val, dtype=np.uint8)


def _source(frame):
    """把 (bgra, w, h, hwnd) 或 None 包成无参 callable。"""
    return lambda: frame


def _full_frame_source(rgb, hwnd=4321):
    raw, w, h = _bgra_from_rgb(rgb)
    return _source((raw, w, h, hwnd))


# ---- encode / source 归一 ---------------------------------------------------
def test_encode_rgb_data_url_is_lossless_png():
    url = ev.encode_rgb_data_url(_flat(val=42))
    assert url.startswith("data:image/png;base64,")


def test_resolve_source_prefers_injected():
    called = {"n": 0}

    def _s():
        called["n"] += 1
        return _bgra(2, 2), 2, 2, 1

    src = ev.resolve_source(source=_s)
    assert src() is not None
    assert called["n"] == 1


# ---- 主链：三态 + 升级包 -----------------------------------------------------
def test_identical_frame_short_circuits_no_vlm(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    rgb = _flat(val=90)
    store.save("hud1", vr.crop_roi(rgb, [0.0, 0.0, 1.0, 1.0]))
    out = ev.check_visual_regression(store, "hud1", bbox_norm=[0.0, 0.0, 1.0, 1.0],
                                      source=_full_frame_source(rgb))
    assert out["verdict"] == "identical"
    assert out["passed"] is True
    assert out["escalate_to_vlm"] is False
    assert "escalation" not in out


def test_gross_change_is_regression_with_escalation(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    golden = _flat(val=30)
    roi = vr.crop_roi(golden, [0, 0, 1, 1])
    store.save("hud1", roi)
    current = _flat(val=220)
    out = ev.check_visual_regression(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                      source=_full_frame_source(current))
    assert out["verdict"] == "regression"
    assert out["escalate_to_vlm"] is True
    assert out["escalation"]["roi_data_url"].startswith("data:image/png;base64,")
    assert "verified:false" in out["escalation"]["question"]


def test_localized_patch_escalates_with_block_info(tmp_path):
    """治本用例链上版：全局均值/SSIM 不动，但 1 块变了 → localized + 升级 + 带块索引。"""
    store = vr.GoldenStore(tmp_path / "g")
    a = _flat(h=256, w=256, val=100)
    store.save("hud1", vr.crop_roi(a, [0, 0, 1, 1]))
    b = a.copy()
    b[40:52, 40:52] = 255  # 一小块点亮
    out = ev.check_visual_regression(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                      source=_full_frame_source(b))
    assert out["mean_abs_delta"] < vr.MEAN_DELTA_PASS
    assert out["ssim"] >= vr.SSIM_PASS
    assert out["verdict"] == "localized"
    assert out["escalate_to_vlm"] is True
    assert out["escalation"]["localized_blocks"]["count"] >= 1
    assert out["escalation"]["localized_blocks"]["indices"]


def test_missing_golden_is_fail_closed(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    out = ev.check_visual_regression(store, "nope",
                                      source=_full_frame_source(_flat()))
    assert out["verdict"] == "needs_golden"
    assert out["passed"] is False
    assert out["escalate_to_vlm"] is True
    assert out["escalation"]["roi_data_url"]  # 缺基线也要给图，好让人/下一段存 golden


def test_no_frame_is_fail_closed_not_pass(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    store.save("hud1", vr.crop_roi(_flat(), [0, 0, 1, 1]))
    out = ev.check_visual_regression(store, "hud1", source=_source(None))
    assert out["verdict"] == "no_frame"
    assert out["passed"] is False
    assert out["escalate_to_vlm"] is True


def test_capture_error_is_fail_closed(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")

    def _boom():
        raise RuntimeError("GDI 挂了")

    out = ev.check_visual_regression(store, "hud1", source=_boom)
    assert out["verdict"] == "capture_error"
    assert out["passed"] is False


# ---- VLM 只在给了回调时才跑（这层绝不自作主张） -------------------------------
def test_vlm_callback_only_runs_when_supplied(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    store.save("hud1", vr.crop_roi(_flat(val=30), [0, 0, 1, 1]))
    calls = {"n": 0}

    def _vlm(esc, res):
        calls["n"] += 1
        assert esc["roi_data_url"].startswith("data:image/png")
        return {"observation_text": "疑似回退", "verified": False}

    out_cb = ev.check_visual_regression(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                        source=_full_frame_source(_flat(val=220)),
                                        vlm_escalate=_vlm)
    assert calls["n"] == 1
    assert out_cb["vlm_observation"]["verified"] is False

    out_no = ev.check_visual_regression(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                        source=_full_frame_source(_flat(val=220)))
    assert calls["n"] == 1          # 没传回调，不该再触发
    assert "vlm_observation" not in out_no


# ---- 存 golden 的捕获路径 ----------------------------------------------------
def test_save_golden_from_capture_roundtrip(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    rgb = _flat(h=100, w=100, val=66)
    res = ev.save_golden_from_capture(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                       source=_full_frame_source(rgb),
                                       meta={"engine": "godot"})
    assert res["saved"] is True
    assert store.exists("hud1")
    # 用同一帧再查 → identical
    out = ev.check_visual_regression(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                      source=_full_frame_source(rgb))
    assert out["verdict"] == "identical"


def test_save_golden_no_overwrite_protects_existing(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    store.save("hud1", vr.crop_roi(_flat(val=10), [0, 0, 1, 1]))
    before = store.load("hud1")
    res = ev.save_golden_from_capture(store, "hud1", bbox_norm=[0, 0, 1, 1],
                                       source=_full_frame_source(_flat(val=250)),
                                       overwrite=False)
    assert res["saved"] is False
    assert res["verdict"] == "exists"
    assert np.array_equal(store.load("hud1"), before)  # 基线没被刷掉


def test_save_golden_no_frame_does_not_clobber(tmp_path):
    store = vr.GoldenStore(tmp_path / "g")
    store.save("hud1", vr.crop_roi(_flat(val=10), [0, 0, 1, 1]))
    res = ev.save_golden_from_capture(store, "hud1", source=_source(None))
    assert res["saved"] is False
    assert res["verdict"] == "no_frame"


def test_crop_roi_bbox_limits_region(tmp_path):
    """只裁右下象限时，左上角的改动不该进 ROI → 判 identical（治'别喂整屏'）。"""
    store = vr.GoldenStore(tmp_path / "g")
    a = _flat(h=200, w=200, val=120)
    bbox = [0.5, 0.5, 0.9, 0.9]
    store.save("hud1", vr.crop_roi(a, bbox))
    b = a.copy()
    b[10:30, 10:30] = 0  # 只动左上角（在 ROI 外，padding 也够不到）
    out = ev.check_visual_regression(store, "hud1", bbox_norm=bbox,
                                      source=_full_frame_source(b))
    assert out["verdict"] == "identical"
