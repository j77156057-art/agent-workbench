"""M2·①：把游戏引擎取像链接起来（流水线第 2~4 段）。

为什么单开这一层（见 docs/multimodal-verification-engine-eda-kickoff.md §2、§4 M2）：
M1 的 `visual_regression.compare` 只是"两张 RGB 怎么比"的纯像素层，它不关心画面从哪来、
也不负责升级 VLM。这一层是**胶水**——把 `grab_embedded` 出的引擎子窗口 BGRA 帧，剥成纯
RGB、按 ROI 裁好、丢给 `GoldenStore.compare_against_golden` 做不会骗人的确定性 diff，
再**只在超阈值/拿不准时**打一个"给 VLM 解释哪里变了"的升级包。

信条落点（全部对齐开工文档 §0）：
- 第 2 段取像用 `screen_capture.grab_embedded`，**帧来源可注入**（`source` 或
  `frame_grabber`）：真机走 GDI，测试喂替身，EDA/Linux 走 `frame_grabber` 注入版，上层零改动。
- 第 3 段只裁视口/canvas（`crop_roi` 复用 target_fingerprint 的带 padding 裁剪思路），
  **绝不喂整屏**——省 token、少幻觉。
- 第 4 段确定性 diff 当第一道闸；**这一层不自作主张跑 VLM**：只把 `roi_data_url +
  局部块信息 + 提问` 打包，交给调用方（或 `vlm_escalate` 回调）去走
  `vision.attach_tool_observation`。VLM 结论一律 `verified:false`，那是下一段的事。
- **fail-closed**：抓不到帧 → `no_frame`（不放行、升级）；没有 golden → `needs_golden`
  （透传 M1，不自动放行）。任何拿不准都不静默 pass。
"""
from __future__ import annotations

import base64
import hashlib
import io
from typing import Any, Callable, Iterable, Optional

import numpy as np

from agent_runtime import visual_regression as vr

__all__ = [
    "encode_rgb_data_url", "resolve_source", "capture_roi",
    "save_golden_from_capture", "check_visual_regression",
    "build_vlm_escalation",
]

# 抓帧来源：返回 grab_embedded 的 (bgra_raw, w, h, hwnd)，或 None（抓不到）。
FrameSource = Callable[[], Optional[tuple]]


def encode_rgb_data_url(rgb: np.ndarray) -> str:
    """RGB 数组 → 无损 PNG data_url，喂给 VLM 当"就照这几百像素解释"的观察图。

    用 PNG 不用 screen_capture.encode_frame 的 JPEG：ROI 已经定尺（默认 256）无需压边，
    而 JPEG 压缩噪声会和 M1"golden 与当前必须同源编码"的约定打架。
    """
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(vr.to_rgb(rgb), "RGB").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def resolve_source(*, source: Optional[FrameSource] = None,
                   project_id: Any = None, frame_grabber: Optional[Callable] = None,
                   strict_project: bool = False) -> FrameSource:
    """把各种取像入参归一成一个无参 callable，返回 (bgra, w, h, hwnd) 或 None。

    - 显式 `source`（测试/EDA 注入版）优先，原样返回，不碰 Windows。
    - 否则惰性绑 `screen_capture.grab_embedded`（Windows 域），把 frame_grabber 透传下去。
    """
    if source is not None:
        return source

    def _embedded_source() -> Optional[tuple]:
        # 惰性导入：非 Windows 或未装 ctypes 时也不至于在 import 本模块就炸。
        import screen_capture
        return screen_capture.grab_embedded(
            project_id, frame_grabber=frame_grabber, strict_project=strict_project)

    return _embedded_source


def _frame_to_roi(grabbed: tuple, bbox_norm: Optional[Iterable[float]],
                  out_size: int) -> np.ndarray:
    """(bgra, w, h, hwnd) → 纯 RGB →（有 bbox 则裁 ROI，否则整帧缩到定尺）。"""
    raw, w, h = grabbed[0], grabbed[1], grabbed[2]
    full = vr.to_rgb(vr.rgba_from_bgra(raw, w, h))
    if bbox_norm is not None:
        return vr.crop_roi(full, bbox_norm, out_size=out_size)
    return vr.resize_rgb(full, out_size)


def capture_roi(*, bbox_norm: Optional[Iterable[float]] = None,
                out_size: int = vr.DEFAULT_ROI_SIZE, source: Optional[FrameSource] = None,
                project_id: Any = None, frame_grabber: Optional[Callable] = None,
                strict_project: bool = False) -> dict[str, Any]:
    """取一帧并裁成 ROI RGB。返回 ``{"ok": True, "roi", "hwnd", "full_size"}`` 或
    ``{"ok": False, "error": "no_frame"|"capture_error", ...}``。抓帧本身失败绝不抛穿上层。"""
    src = resolve_source(source=source, project_id=project_id,
                         frame_grabber=frame_grabber, strict_project=strict_project)
    try:
        grabbed = src()
    except Exception as exc:  # fail-closed：取像异常也是"没拿到画面"，不能当通过
        return {"ok": False, "error": "capture_error", "reason": str(exc)[:200]}
    if not grabbed or len(grabbed) < 3:
        return {"ok": False, "error": "no_frame",
                "reason": "抓不到引擎嵌入帧（无登记表项目/窗口已关/取像返回空）。"}
    try:
        roi = _frame_to_roi(grabbed, bbox_norm, out_size)
    except Exception as exc:  # 尺寸/通道不合法等，同样 fail-closed
        return {"ok": False, "error": "capture_error", "reason": str(exc)[:200]}
    return {"ok": True, "roi": roi, "hwnd": grabbed[3] if len(grabbed) > 3 else None,
            "full_size": [int(grabbed[1]), int(grabbed[2])]}


def save_golden_from_capture(store: vr.GoldenStore, golden_key: str, *,
                             bbox_norm: Optional[Iterable[float]] = None,
                             out_size: int = vr.DEFAULT_ROI_SIZE,
                             source: Optional[FrameSource] = None, project_id: Any = None,
                             frame_grabber: Optional[Callable] = None,
                             strict_project: bool = False,
                             meta: Optional[dict] = None, overwrite: bool = True) -> dict[str, Any]:
    """在"当前即通过态"时抓一帧 ROI 存成 golden（无损 PNG）。

    `overwrite=False` 且 key 已存在时不覆盖，回 `{"saved": False, "verdict": "exists"}`——
    防止把已批准的基线意外刷成回退帧。
    """
    if not overwrite and store.exists(golden_key):
        return {"saved": False, "verdict": "exists", "golden_key": golden_key,
                "reason": "golden 已存在且 overwrite=False，未覆盖。"}
    cap = capture_roi(bbox_norm=bbox_norm, out_size=out_size, source=source,
                      project_id=project_id, frame_grabber=frame_grabber,
                      strict_project=strict_project)
    if not cap["ok"]:
        return {"saved": False, "verdict": cap["error"], "golden_key": golden_key,
                "reason": cap["reason"]}
    entry = store.save(golden_key, cap["roi"], meta=meta)
    return {"saved": True, "verdict": "saved", "golden_key": golden_key,
            "entry": entry, "hwnd": cap.get("hwnd")}


def build_vlm_escalation(roi: np.ndarray, result: dict[str, Any], golden_key: str) -> dict[str, Any]:
    """把"拿不准/回退"的比对结果打包成给 VLM 的解释请求（不含任何 VLM 调用）。

    只带 ROI 局部图 + 定位到的块信息，让 VLM 干"说清哪里变了"，而不是重新判通过与否。
    提问里明确"结论未核实(verified:false)、只依据图像"，对齐开工文档信条 4。
    """
    blocks = result.get("blocks") or {}
    idx = blocks.get("indices") or []
    grid = blocks.get("grid")
    detail = ("verdict=%s ssim=%s mean_abs_delta=%s 变化块=%d/%d(grid=%s)%s"
              % (result.get("verdict"), result.get("ssim"),
                 result.get("mean_abs_delta"), blocks.get("count", 0),
                 blocks.get("total", 0), grid,
                 (" 索引=%s" % idx[:12]) if idx else ""))
    reason = result.get("reason")
    question = (
        "这是一段引擎视口 ROI 相对已批准 golden 的差异比对结果（%s%s）。"
        "请用不超过 120 字说明画面上哪里变了、是否像回退（缺元素/穿帮/变色/破图）。"
        "只依据这张图判断，不要臆测代码或日志事实；你的结论视为未核实(verified:false)。"
        % (detail, (" 原因：" + reason) if reason else "")
    )
    return {
        "roi_data_url": encode_rgb_data_url(roi),
        "golden_key": golden_key,
        "localized_blocks": blocks,
        "question": question,
    }


def check_visual_regression(store: vr.GoldenStore, golden_key: str, *,
                            bbox_norm: Optional[Iterable[float]] = None,
                            out_size: int = vr.DEFAULT_ROI_SIZE,
                            source: Optional[FrameSource] = None, project_id: Any = None,
                            frame_grabber: Optional[Callable] = None,
                            strict_project: bool = False,
                            ssim_threshold: float = vr.SSIM_PASS,
                            delta_threshold: float = vr.MEAN_DELTA_PASS,
                            grid: int = 8,
                            vlm_escalate: Optional[Callable[[dict, dict], Any]] = None,
                            ) -> dict[str, Any]:
    """引擎视口回归主入口：抓帧 → 裁 ROI → vs golden 确定性 diff → 需要才备 VLM 升级包。

    返回 = M1 `compare`/`compare_against_golden` 的三态结果，外加：
      - `golden_key` / `hwnd` / `roi_sha256` / `full_size`（溯源用，别把哈希当结论）
      - `escalation`：仅当 `escalate_to_vlm` 为真时给出（roi_data_url + 局部块 + 提问）
      - `vlm_observation`：仅当传入 `vlm_escalate` 回调时执行并附上；不传则**绝不自动跑 VLM**。
    """
    cap = capture_roi(bbox_norm=bbox_norm, out_size=out_size, source=source,
                      project_id=project_id, frame_grabber=frame_grabber,
                      strict_project=strict_project)
    if not cap["ok"]:
        # 抓不到帧 / 取像异常：不判通过，也不假装比对——交人工/下一段核实。
        return {"verdict": cap["error"], "passed": False, "escalate_to_vlm": True,
                "golden_key": golden_key, "reason": cap["reason"],
                "blocks": {"count": 0, "total": grid * grid, "ratio": 0.0,
                           "indices": [], "grid": grid}}

    roi = cap["roi"]
    result = dict(store.compare_against_golden(
        golden_key, roi, ssim_threshold=ssim_threshold,
        delta_threshold=delta_threshold, grid=grid))
    result["golden_key"] = golden_key
    result["hwnd"] = cap.get("hwnd")
    result["full_size"] = cap.get("full_size")
    result["roi_sha256"] = hashlib.sha256(np.asarray(roi).tobytes()).hexdigest()

    if result.get("escalate_to_vlm"):
        esc = build_vlm_escalation(roi, result, golden_key)
        result["escalation"] = esc
        if vlm_escalate is not None:
            result["vlm_observation"] = vlm_escalate(esc, result)
    return result
