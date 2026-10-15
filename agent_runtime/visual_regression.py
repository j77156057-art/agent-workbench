"""确定性视觉回归层（M1）：golden 参考 + SSIM / 区域块哈希 diff。

为什么先要这一层（见 docs/multimodal-verification-engine-eda-kickoff.md 第 4~6 段）：
VLM 又慢又贵、还会"睁眼说瞎话"。所以先把**当前画面**和**上一版批准的 golden**做一次
不会幻觉的确定性比对——明显没变的直接放行、明显回退的直接拦下，只有落在中间模糊带
（像素有差异但不知轻重）才升级到 VLM 去解释"哪里变了"。这条闸就是省钱 + 防假绿灯的关键。

设计约定：
- **只吃纯 RGB**，不碰 BGRX 的 alpha/占位通道（历史上 `target_still_matches` 把无用
  通道也算进均值，这里从入口就剥掉）。
- **golden 用无损 PNG 存**，比对的两张图必须同源编码，否则 JPEG 压缩噪声会把阈值糊掉。
- **区域块哈希**（grid 切块逐块 sha256）专治"全局均值几乎不动、但某个 1px via 没了"
  这种 `target_still_matches` 的 24×24 均值差抓不到的细粒度回退。
- **SSIM 纯 numpy**（高斯窗、separable via einsum），不引 scikit-image/opencv，避免
  和 GPU/重依赖打架。
- **fail-closed**：拿不准（有像素差但没超全局阈值）一律 escalate 交 VLM，不自作主张放行。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

# ---- 常量 ------------------------------------------------------------------
DEFAULT_ROI_SIZE = 256          # 比对前统一缩放到这个边长，golden 与当前必须同尺寸
_SSIM_WIN = 11                  # 高斯窗
_SSIM_SIGMA = 1.5
_DATA_RANGE = 255.0
_C1 = (0.01 * _DATA_RANGE) ** 2
_C2 = (0.03 * _DATA_RANGE) ** 2

# 默认阈值：给"视口/UI 回归"这档够用；EDA 版图/波形建议收紧（见 ssim_threshold 入参）。
SSIM_PASS = 0.98                # 结构相似度低于它 → 明显回退
MEAN_DELTA_PASS = 8.0           # 逐像素绝对差均值高于它 → 明显回退
BLOCK_RATIO_ESCALATE = 0.0      # 只要有块哈希不同就至少标 localized
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


# ---- 像素/取像辅助 ---------------------------------------------------------
def rgba_from_bgra(raw: bytes, width: int, height: int) -> np.ndarray:
    """screen_capture 的 BGRA 字节 → ``(H,W,4)`` uint8（含 alpha）。"""
    arr = np.frombuffer(bytes(raw), dtype=np.uint8)
    need = int(width) * int(height) * 4
    if arr.size < need:
        raise ValueError("BGRA 缓冲区不足：%d < %d" % (arr.size, need))
    return arr.reshape(height, width, 4)


def to_rgb(img: np.ndarray) -> np.ndarray:
    """任意 (H,W,[C]) 数组 → 纯 RGB (H,W,3)，剥掉 alpha/占位通道。"""
    a = np.asarray(img)
    if a.ndim == 2:
        return np.repeat(a[:, :, None], 3, axis=2)
    if a.shape[2] == 3:
        return a.astype(np.uint8, copy=False)
    if a.shape[2] >= 3:
        return a[:, :, :3].astype(np.uint8, copy=False)
    raise ValueError("无法转 RGB：shape=%r" % (a.shape,))


def gray(img: np.ndarray) -> np.ndarray:
    """Rec.601 luma，float64。"""
    rgb = to_rgb(img).astype(np.float64)
    return rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114


def resize_rgb(img: np.ndarray, size: int = DEFAULT_ROI_SIZE) -> np.ndarray:
    """缩放到 (size,size) RGB uint8，让 golden 与当前同尺寸可比。"""
    from PIL import Image
    pil = Image.fromarray(to_rgb(img), "RGB").resize((size, size), Image.LANCZOS)
    return np.asarray(pil, dtype=np.uint8)


def crop_roi(full_rgb: np.ndarray, bbox_norm: Iterable[float], *,
             pad: float = 0.5, out_size: int = DEFAULT_ROI_SIZE) -> np.ndarray:
    """归一 0~1 的 bbox → 带 padding 裁邻域 → 固定尺寸 RGB。

    复用 visual_targeting.target_fingerprint 的"pad 到一半宽高、clamp 到边界"思路，
    但输出 RGB 供后续 SSIM/块哈希用。
    """
    x1, y1, x2, y2 = [float(v) for v in bbox_norm]
    h, w = full_rgb.shape[:2]
    pad_x = max(0.03, (x2 - x1) * pad)
    pad_y = max(0.03, (y2 - y1) * pad)
    left = max(0, int((x1 - pad_x) * w))
    top = max(0, int((y1 - pad_y) * h))
    right = min(w, max(left + 1, int((x2 + pad_x) * w)))
    bottom = min(h, max(top + 1, int((y2 + pad_y) * h)))
    crop = full_rgb[top:bottom, left:right, :3]
    return resize_rgb(crop, out_size)


# ---- SSIM（纯 numpy） ------------------------------------------------------
def _gauss1d(size: int, sigma: float) -> np.ndarray:
    x = np.arange(size, dtype=np.float64) - (size - 1) / 2.0
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    return k / k.sum()


def _gauss_filter(img: np.ndarray, size: int = _SSIM_WIN, sigma: float = _SSIM_SIGMA) -> np.ndarray:
    """2D 高斯加权有效滑动均值（valid 卷积）。

    separable：用 1D 高斯的 outer 积拼出 2D 核，靠 sliding_window_view + einsum 做
    有效卷积，纯 numpy 无 scipy。四张 SSIM 统计图都走这同一函数，形状自然一致。
    """
    from numpy.lib.stride_tricks import sliding_window_view
    a = img.astype(np.float64)
    k1 = _gauss1d(size, sigma)
    K = np.outer(k1, k1)
    h, w = a.shape
    if h < size or w < size:
        a = np.pad(a, size, mode="reflect")
        h, w = a.shape
    view = sliding_window_view(a, (size, size))          # (h-size+1, w-size+1, size, size)
    return np.einsum("ijkl,kl->ij", view, K)


def ssim(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    """两张 RGB 的灰度 SSIM 均值，范围约 [-1,1]；越接近 1 越像。"""
    ga = gray(a_rgb)
    gb = gray(b_rgb)
    if ga.shape != gb.shape:
        raise ValueError("SSIM 尺寸不一致：%r vs %r" % (ga.shape, gb.shape))
    mu_a = _gauss_filter(ga)
    mu_b = _gauss_filter(gb)
    aa = _gauss_filter(ga * ga)
    bb = _gauss_filter(gb * gb)
    ab = _gauss_filter(ga * gb)
    va = aa - mu_a * mu_a
    vb = bb - mu_b * mu_b
    vab = ab - mu_a * mu_b
    num = (2 * mu_a * mu_b + _C1) * (2 * vab + _C2)
    den = (mu_a ** 2 + mu_b ** 2 + _C1) * (va + vb + _C2)
    return float((num / den).mean())


# ---- 区域块哈希（细粒度定位） ----------------------------------------------
def block_hashes(rgb: np.ndarray, grid: int = 8) -> list[str]:
    """把 RGB 切成 grid×grid 块，逐块 sha256。任一块内哪怕 1px 变化都能被抓到。"""
    a = to_rgb(rgb)
    h, w = a.shape[:2]
    if grid < 1:
        raise ValueError("grid 必须 ≥1")
    # 用 linspace 直接切 grid+1 条边界，非整除/非方形都能均分，块数恒为 grid×grid。
    rows = np.linspace(0, h, grid + 1, dtype=int)
    cols = np.linspace(0, w, grid + 1, dtype=int)
    out: list[str] = []
    for i in range(grid):
        for j in range(grid):
            block = a[rows[i]:rows[i + 1], cols[j]:cols[j + 1]]
            out.append(hashlib.sha256(block.tobytes()).hexdigest())
    return out


def changed_blocks(a_hashes: list[str], b_hashes: list[str], grid: int = 8) -> dict[str, Any]:
    if len(a_hashes) != len(b_hashes):
        raise ValueError("块哈希长度不一致")
    idx = [n for n, (x, y) in enumerate(zip(a_hashes, b_hashes)) if x != y]
    total = len(a_hashes)
    return {
        "count": len(idx),
        "total": total,
        "ratio": len(idx) / total if total else 0.0,
        "indices": idx,
        "grid": grid,
    }


def mean_abs_delta(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    a = to_rgb(a_rgb).astype(np.int16)
    b = to_rgb(b_rgb).astype(np.int16)
    if a.shape != b.shape:
        raise ValueError("diff 尺寸不一致：%r vs %r" % (a.shape, b.shape))
    return float(np.abs(a - b).mean())


# ---- 比对（核心） ----------------------------------------------------------
def compare(current_rgb: np.ndarray, golden_rgb: np.ndarray, *,
            ssim_threshold: float = SSIM_PASS,
            delta_threshold: float = MEAN_DELTA_PASS,
            grid: int = 8) -> dict[str, Any]:
    """当前画面 vs golden，出三态判定 + 是否升级 VLM。

    verdict：
      - ``identical``  逐像素全等 → 直接放行，无需 VLM。
      - ``pass``       结构 & 均值都达标 → 放行。
      - ``regression`` 超阈值 → 明显回退，建议直接拦下（仍交 VLM 说清哪坏了）。
      - ``localized``  全局达标但有块变了 → 拿不准，**必须**升级 VLM（治 1px via）。

    ``escalate_to_vlm`` 对非 identical 都为 True——把决定权交给下一段，这一层不自作主张。
    """
    cur = resize_rgb(current_rgb)
    gold = resize_rgb(golden_rgb)
    exact = bool(np.array_equal(cur, gold))
    if exact:
        return {"verdict": "identical", "passed": True, "escalate_to_vlm": False,
                "ssim": 1.0, "mean_abs_delta": 0.0,
                "blocks": {"count": 0, "total": grid * grid, "ratio": 0.0,
                           "indices": [], "grid": grid}}
    s = ssim(cur, gold)
    d = mean_abs_delta(cur, gold)
    bh = changed_blocks(block_hashes(cur, grid), block_hashes(gold, grid), grid)
    if s < ssim_threshold or d > delta_threshold:
        verdict = "regression"
    elif bh["count"] > BLOCK_RATIO_ESCALATE:
        verdict = "localized"
    else:
        verdict = "pass"
    return {"verdict": verdict, "passed": verdict in ("pass", "identical"),
            "escalate_to_vlm": verdict != "identical",
            "ssim": round(s, 5), "mean_abs_delta": round(d, 3), "blocks": bh}


# ---- golden 存取（无损 PNG + 清单） ----------------------------------------
@dataclass
class GoldenStore:
    """按 key 存无损 golden，配一份 manifest.json 记元数据与阈值。

    路径守则（对齐 visual_feedback.evidence_path）：key 只能是安全字符，落盘路径
    必须在 root 内，防目录穿越。
    """
    root: Path
    _manifest_name = "manifest.json"

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_key(self, key: str) -> str:
        if not _KEY_RE.match(str(key or "")):
            raise ValueError("golden key 非法：%r" % str(key)[:60])
        return key

    def _path(self, key: str) -> Path:
        key = self._safe_key(key)
        p = (self.root / f"{key}.png").resolve()
        if self.root not in p.parents:
            raise ValueError("golden 路径越界：%s" % p)
        return p

    def _manifest(self) -> dict[str, Any]:
        m = self.root / self._manifest_name
        try:
            return json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, data: dict[str, Any]) -> None:
        (self.root / self._manifest_name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def save(self, key: str, rgb: np.ndarray, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """存/覆盖一帧 golden（无损 PNG），并更新清单里的哈希与元数据。"""
        p = self._path(key)
        from PIL import Image
        arr = to_rgb(rgb)
        Image.fromarray(arr, "RGB").save(p, format="PNG", optimize=True)
        entry = {
            "sha256": hashlib.sha256(arr.tobytes()).hexdigest(),
            "size": [int(arr.shape[1]), int(arr.shape[0])],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            entry["meta"] = meta
        m = self._manifest()
        m[key] = entry
        self._write_manifest(m)
        return entry

    def load(self, key: str) -> np.ndarray | None:
        p = self._path(key)
        if not p.is_file():
            return None
        from PIL import Image
        with Image.open(p) as im:
            return np.asarray(im.convert("RGB"), dtype=np.uint8)

    def compare_against_golden(self, key: str, current_rgb: np.ndarray, **kwargs) -> dict[str, Any]:
        """和已存 golden 比；golden 缺失 → fail-closed 判 needs_golden（不自动放行）。"""
        gold = self.load(key)
        if gold is None:
            return {"verdict": "needs_golden", "passed": False, "escalate_to_vlm": True,
                    "reason": "没有 golden 参考，无法判定回归；请先在通过态存 golden。"}
        return compare(current_rgb, gold, **kwargs)


__all__ = [
    "rgba_from_bgra", "to_rgb", "gray", "resize_rgb", "crop_roi",
    "ssim", "block_hashes", "changed_blocks", "mean_abs_delta", "compare",
    "GoldenStore", "DEFAULT_ROI_SIZE", "SSIM_PASS", "MEAN_DELTA_PASS",
]
