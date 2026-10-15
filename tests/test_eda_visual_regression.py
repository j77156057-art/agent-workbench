"""M5 EDA 版图/波形 ROI golden diff 离线单测。

本机无 EDA/无 Linux 取像，全部喂合成"版图"帧 + 注入 source / M4 假 grabber。
验收口径（开工文档 §4 M5）：**同 testcase 两版图，注入一个 via 缺失能被 region_hash 抓到**；
且整条链复用 M1/M2 比对零件与 M4 grabber，上层零改动。
"""
import io
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime import visual_regression as vr            # noqa: E402
from agent_runtime.eda_visual_regression import (            # noqa: E402
    EDA_VIEWS, resolve_roi, frame_source_from_grabber,
    check_eda_regression, save_eda_golden,
)
from agent_runtime.frame_grabber_linux import x11_grabber    # noqa: E402


# ---- 合成版图：网格底 + 若干亮点 via；去掉一个 via 即"缺失"回退 -------------

def _layout(missing_via=False):
    """300x300 灰底网格图，画几个亮 via 方块；missing_via=True 时抹掉其中一个。"""
    arr = np.zeros((300, 300, 3), dtype=np.uint8)
    arr[:, :] = (30, 40, 50)                       # 暗底
    for i in range(0, 300, 20):                    # 网格线
        arr[i, :, :] = (90, 100, 110)
        arr[:, i, :] = (90, 100, 110)
    vias = [(60, 60), (120, 120), (180, 60), (60, 180)]
    for (cy, cx) in vias:
        if missing_via and (cy, cx) == (120, 120):
            continue                               # 注入"via 缺失"
        arr[cy:cy + 12, cx:cx + 12] = (240, 220, 40)  # 亮黄 via
    return arr


def _source_from_rgb(arr):
    """RGB(H,W,3) → 零参 FrameSource()->(bgra,w,h,hwnd)，top-down BGRX。"""
    h, w = arr.shape[:2]
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    bgra = np.dstack([arr[:, :, 2:3], arr[:, :, 1:2], arr[:, :, 0:1], alpha])
    raw = np.ascontiguousarray(bgra, dtype=np.uint8).tobytes()
    return lambda: (raw, w, h, 0x1)


def _png_from_rgb(arr):
    from PIL import Image
    img = Image.fromarray(arr, "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return vr.GoldenStore(root=str(tmp_path / "golden"))


# ---- ROI 注册/解析 ---------------------------------------------------------

def test_resolve_roi_view_default_and_override():
    assert resolve_roi(view="layout") == EDA_VIEWS["layout"]
    assert resolve_roi(bbox_norm=[0, 0, 1, 1]) == [0.0, 0.0, 1.0, 1.0]
    # 显式 bbox 优先于 view
    assert resolve_roi(view="waveform", bbox_norm=[0.1, 0.1, 0.2, 0.2]) == [0.1, 0.1, 0.2, 0.2]
    assert resolve_roi() is None


def test_resolve_roi_four_eda_categories_present():
    for k in ("layout", "waveform", "schematic", "heatmap"):
        assert k in EDA_VIEWS
        assert len(EDA_VIEWS[k]) == 4


def test_resolve_roi_bad_length_raises():
    with pytest.raises(ValueError):
        resolve_roi(bbox_norm=[0.1, 0.2, 0.3])


# ---- M4 grabber → FrameSource 适配 ----------------------------------------

def test_frame_source_from_grabber_adds_hwnd():
    grab = lambda wid: (b"\x00" * 16, 2, 2)        # 3 元组（M4 契约）
    src = frame_source_from_grabber(grab, window_id=0x77)
    out = src()
    assert out == (b"\x00" * 16, 2, 2, 0x77)       # 4 元组，补上 window_id


def test_frame_source_none_when_grabber_fails():
    assert frame_source_from_grabber(lambda wid: None)() is None
    def boom(wid):
        raise RuntimeError("no x11")
    assert frame_source_from_grabber(boom)() is None


# ---- M5 核心验收：注入 via 缺失被 region_hash 抓到 -------------------------

def test_via_missing_caught_by_region_hash(store):
    # 先存"通过态"golden（完整版图）
    saved = save_eda_golden(store, "blkA", bbox_norm=[0.05, 0.05, 0.95, 0.95],
                            source=_source_from_rgb(_layout()))
    assert saved["saved"] is True

    # 同 testcase，抹掉一个 via 后重跑 → 应被 localized 抓到并升级 VLM
    res = check_eda_regression(store, "blkA", bbox_norm=[0.05, 0.05, 0.95, 0.95],
                               source=_source_from_rgb(_layout(missing_via=True)))
    assert res["verdict"] == "localized"
    assert res["passed"] is False
    assert res["escalate_to_vlm"] is True
    assert res["blocks"]["count"] >= 1
    # 全局指标仍"看着还行"（均值差小、SSIM 高），恰恰说明只有 region_hash 抓得住 1 处小缺失
    assert res["mean_abs_delta"] < vr.MEAN_DELTA_PASS
    assert res["ssim"] > vr.SSIM_PASS


def test_identical_layout_passes(store):
    save_eda_golden(store, "same", view="layout", source=_source_from_rgb(_layout()))
    res = check_eda_regression(store, "same", view="layout", source=_source_from_rgb(_layout()))
    assert res["verdict"] == "identical"
    assert res["passed"] is True
    assert res["escalate_to_vlm"] is False


def test_gross_change_is_regression(store):
    save_eda_golden(store, "gross", view="layout", source=_source_from_rgb(_layout()))
    flipped = np.full((300, 300, 3), 200, dtype=np.uint8)   # 整片换色
    res = check_eda_regression(store, "gross", view="layout", source=_source_from_rgb(flipped))
    assert res["verdict"] == "regression"
    assert res["passed"] is False


# ---- fail-closed 面 --------------------------------------------------------

def test_missing_golden_needs_golden(store):
    res = check_eda_regression(store, "ghost", view="layout",
                               source=_source_from_rgb(_layout()))
    assert res["verdict"] == "needs_golden"
    assert res["passed"] is False
    assert res["escalate_to_vlm"] is True


def test_refuses_full_screen_without_roi(store):
    res = check_eda_regression(store, "blk", source=_source_from_rgb(_layout()))
    assert res["verdict"] == "roi_required"
    assert res["passed"] is False


def test_requires_frame_source(store):
    res = check_eda_regression(store, "blk", view="layout")   # 没 grabber 也没 source
    assert res["verdict"] == "no_source"
    assert res["passed"] is False


def test_save_golden_refuses_full_screen(store):
    out = save_eda_golden(store, "blk", source=_source_from_rgb(_layout()))
    assert out["saved"] is False
    assert out["verdict"] == "roi_required"


def test_grabber_none_frame_is_no_frame(store):
    # ROI 给了，但 grabber 取像返回 None → 上层归一成 no_frame，绝不判通过
    res = check_eda_regression(store, "blk", view="waveform",
                               grabber=lambda wid: None)
    assert res["verdict"] == "no_frame"
    assert res["passed"] is False


# ---- M4 → M5 全链：真·x11_grabber(注入 runner 出 PNG) → ROI → golden diff ---

def test_end_to_end_m4_grabber_to_m5_diff(store):
    base_png = _png_from_rgb(_layout())
    bad_png = _png_from_rgb(_layout(missing_via=True))

    grab_ok = x11_grabber(runner=lambda cmd: (0, base_png))
    saved = save_eda_golden(store, "chain", view="layout", grabber=grab_ok, window_id=0x5)
    assert saved["saved"] is True

    grab_bad = x11_grabber(runner=lambda cmd: (0, bad_png))
    res = check_eda_regression(store, "chain", view="layout", grabber=grab_bad, window_id=0x5)
    assert res["verdict"] == "localized"
    assert res["passed"] is False
    assert res["blocks"]["count"] >= 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
