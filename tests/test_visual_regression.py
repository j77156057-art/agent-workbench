"""M1 确定性视觉回归层单测（纯 numpy/PIL，无需 GPU 或外部图）。"""
import numpy as np
import pytest

from agent_runtime import visual_regression as vr


def _flat(h=64, w=64, val=128):
    a = np.full((h, w, 3), val, dtype=np.uint8)
    return a


def test_identical_short_circuits():
    a = _flat()
    out = vr.compare(a, a.copy())
    assert out["verdict"] == "identical"
    assert out["passed"] is True
    assert out["escalate_to_vlm"] is False


def test_gross_change_is_regression():
    a = _flat(val=30)
    b = _flat(val=220)
    out = vr.compare(a, b)
    assert out["verdict"] == "regression"
    assert out["escalate_to_vlm"] is True
    assert out["ssim"] < vr.SSIM_PASS


def test_small_patch_caught_by_block_hash_not_mean_delta():
    """治本用例：全局均值差/SSIM 几乎不动，但某个局部块变了必须被抓并升级 VLM。"""
    a = _flat(val=100)
    b = a.copy()
    b[10:16, 10:16] = 255  # 一小块 6x6 点亮
    out = vr.compare(a, b)
    assert out["mean_abs_delta"] < vr.MEAN_DELTA_PASS        # 均值层面看不出
    assert out["ssim"] >= vr.SSIM_PASS                        # 结构层面也达标
    assert out["blocks"]["count"] >= 1                        # 块哈希抓到
    assert out["verdict"] == "localized"                      # 于是标拿不准
    assert out["escalate_to_vlm"] is True                     # 绝不静默放行


def test_rgba_strips_alpha_channel():
    bgra = np.zeros((4, 4, 4), dtype=np.uint8)
    bgra[:, :, :3] = 50
    bgra[:, :, 3] = 255
    arr = vr.rgba_from_bgra(bgra.tobytes(), 4, 4)
    rgb = vr.to_rgb(arr)
    assert rgb.shape == (4, 4, 3)
    assert np.all(rgb == 50)


def test_crop_roi_shape_and_bounds():
    full = _flat(h=200, w=300)
    roi = vr.crop_roi(full, [0.2, 0.2, 0.4, 0.5], out_size=96)
    assert roi.shape == (96, 96, 3)


def test_golden_store_roundtrip(tmp_path):
    store = vr.GoldenStore(tmp_path / "golden")
    a = _flat(val=77)
    assert store.exists("scene1") is False
    store.save("scene1", a, meta={"engine": "godot"})
    assert store.exists("scene1") is True
    loaded = store.load("scene1")
    assert np.array_equal(vr.to_rgb(loaded), vr.to_rgb(a))


def test_compare_against_missing_golden_is_fail_closed(tmp_path):
    store = vr.GoldenStore(tmp_path / "golden")
    out = store.compare_against_golden("nope", _flat())
    assert out["verdict"] == "needs_golden"
    assert out["passed"] is False
    assert out["escalate_to_vlm"] is True


def test_golden_store_rejects_traversal_key(tmp_path):
    store = vr.GoldenStore(tmp_path / "golden")
    with pytest.raises(ValueError):
        store.save("../evil", _flat())
