"""EDA 版图/波形 ROI golden diff 上线（M5，流水线第 3~5 段·域注册 + 复用）。

信条（docs/multimodal-verification-engine-eda-kickoff.md §3.2 第2~5段、§4 M5）：
- **先做"和上次比"，不做"看图找 bug"。** 这一层只做"当前 ROI vs golden 的确定性 diff"，
  全部复用已有零件：ROI 裁切用 `visual_regression.crop_roi`（带 padding + 定尺），
  diff/三态判定用 `engine_visual_regression.check_visual_regression`（内部走 M1 的
  SSIM + block_hashes/region_hash + mean_abs_delta）。**不新写任何像素比较逻辑**，避免漂移。
- **只裁目标 ROI，绝不喂整屏。** 第 3 段的 ROI 定义在本层（域注册）里给；没给 view/bbox
  直接 fail-closed 拒绝整屏比对（省 token、少幻觉）。四类默认视口矩形只是"常见起点"，
  真机应按 §0 域注册契约覆盖。
- **接上 M4 的 Linux grabber。** `frame_source_from_grabber` 把 M4 `frame_grabber(hwnd)->
  (bgra,w,h)` 适配成引擎要的零参 `FrameSource()->(bgra,w,h,hwnd)`，于是 M4→M5 一条链，
  上层（capture_window / check_visual_regression）一行不改。
- **region_hash 抓 1px。** `compare` 的 `BLOCK_RATIO_ESCALATE=0.0`：哪怕只有一块哈希变了，
  也标 `localized` 并 `escalate_to_vlm=True`——这正是版图 via/接点缺失这种"日志写不全、
  人一眼看出、均值差又太钝"的场景。
- **VLM 只解释不裁决。** 只透传 `check_visual_regression` 打好的升级包，可选回调执行；
  本层绝不自作主张跑 VLM，结论仍 `verified:false`（那是下一段/调用方的事）。
- **fail-closed。** 没帧来源 / 取像返回 None / 没 golden → 一律不判通过，交人工或下一段。

依赖：numpy（随 visual_regression/engine 已有），无新增重依赖，无 EDA SDK。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from agent_runtime import engine_visual_regression as evr
from agent_runtime import visual_regression as vr

__all__ = [
    "EDA_VIEWS", "resolve_roi", "frame_source_from_grabber",
    "check_eda_regression", "save_eda_golden",
]

# 归一 0~1 的 bbox [x1,y1,x2,y2]（喂 vr.crop_roi）。真机按 §0 域注册覆盖。
# 值取"常见 EDA 窗口里目标面板的粗略位置"，只是可用起点，非精确标定。
EDA_VIEWS: dict[str, list[float]] = {
    # 版图查看器（KLayout/Virtuoso/floorplan）：中间画布区，避开顶部菜单+左右工具条。
    "layout":   [0.15, 0.15, 0.85, 0.85],
    # 波形查看器（Verdi/GTKWave）：下半部波形 pane，避开顶部工具栏与左侧信号名列表。
    "waveform": [0.20, 0.45, 0.95, 0.95],
    # 原理图/网表可视化：整张图纸区。
    "schematic": [0.10, 0.10, 0.90, 0.90],
    # 资源/时序/功耗热力图（FPGA 利用率/congestion）：主图区。
    "heatmap":  [0.12, 0.15, 0.90, 0.90],
}


def resolve_roi(view: Optional[str] = None,
                bbox_norm: Optional[Iterable[float]] = None) -> Optional[list[float]]:
    """显式 bbox_norm 优先；否则查 EDA_VIEWS[view]；都没有 → None（拒绝整屏）。"""
    if bbox_norm is not None:
        bb = [float(v) for v in bbox_norm]
        if len(bb) != 4:
            raise ValueError("bbox_norm 必须是 4 个数 [x1,y1,x2,y2]")
        return bb
    if view is not None and view in EDA_VIEWS:
        return list(EDA_VIEWS[view])
    return None


def frame_source_from_grabber(grabber: Callable[[Any], Optional[tuple]],
                              window_id: Any = None) -> evr.FrameSource:
    """把 M4 的 `frame_grabber(hwnd)->(bgra,w,h)|None` 包成引擎要的零参 FrameSource。

    FrameSource 返回 `(bgra,w,h,hwnd)`（第 4 位给 capture_roi 溯源），或 None（抓不到，
    上层照旧 fail-closed 成 no_frame，绝不放行）。grabber 抛异常也归一成 None。
    """
    def source() -> Optional[tuple]:
        try:
            frame = grabber(window_id)
        except Exception:
            return None
        if not frame or len(frame) < 3:
            return None
        raw, w, h = frame[0], frame[1], frame[2]
        return raw, int(w), int(h), window_id

    return source


def _fail(verdict: str, reason: str, *, escalate: bool = False) -> dict[str, Any]:
    return {"verdict": verdict, "passed": False, "escalate_to_vlm": escalate,
            "reason": reason}


def check_eda_regression(store: vr.GoldenStore, golden_key: str, *,
                         view: Optional[str] = None,
                         bbox_norm: Optional[Iterable[float]] = None,
                         grabber: Optional[Callable[[Any], Optional[tuple]]] = None,
                         source: Optional[evr.FrameSource] = None,
                         window_id: Any = None,
                         out_size: int = vr.DEFAULT_ROI_SIZE,
                         ssim_threshold: float = vr.SSIM_PASS,
                         delta_threshold: float = vr.MEAN_DELTA_PASS,
                         grid: int = 8,
                         vlm_escalate: Optional[Callable[[dict, dict], Any]] = None,
                         ) -> dict[str, Any]:
    """EDA 版回归主入口：定 ROI → 取帧（复用 M4 grabber 或注入 source）→ vs golden 确定性 diff。

    结果就是 `engine.check_visual_regression` 的三态（identical/pass/localized/regression/
    needs_golden/no_frame/capture_error）外加 `golden_key`/`roi_sha256`/`full_size` 溯源字段。
    本层只在 ROI 或帧来源缺失时先行 fail-closed，比对逻辑完全交给已验证的 M1/M2 零件。
    """
    bbox = resolve_roi(view, bbox_norm)
    if bbox is None:
        return _fail("roi_required",
                     "未给 view 或 bbox_norm：拒绝整屏比对（省 token、少幻觉，对齐 §2 第3段）。")

    src = source
    if src is None:
        if grabber is None:
            return _fail("no_source",
                         "没给帧来源（frame_grabber 或 source），无法取像。",
                         escalate=True)
        src = frame_source_from_grabber(grabber, window_id)

    return evr.check_visual_regression(
        store, golden_key, bbox_norm=bbox, out_size=out_size, source=src,
        ssim_threshold=ssim_threshold, delta_threshold=delta_threshold, grid=grid,
        vlm_escalate=vlm_escalate)


def save_eda_golden(store: vr.GoldenStore, golden_key: str, *,
                    view: Optional[str] = None,
                    bbox_norm: Optional[Iterable[float]] = None,
                    grabber: Optional[Callable[[Any], Optional[tuple]]] = None,
                    source: Optional[evr.FrameSource] = None,
                    window_id: Any = None,
                    out_size: int = vr.DEFAULT_ROI_SIZE,
                    meta: Optional[dict] = None,
                    overwrite: bool = True) -> dict[str, Any]:
    """在"当前即通过态"时抓 ROI 存 golden（无损 PNG）。与 check 用同一 ROI，保证同源可比。

    没给 ROI / 帧来源 → fail-closed，不拿整屏或空帧污染基线。overwrite=False 且已存在时
    不覆盖（防把已批准基线刷成回退帧）。
    """
    bbox = resolve_roi(view, bbox_norm)
    if bbox is None:
        return {"saved": False, "verdict": "roi_required",
                "reason": "未给 view 或 bbox_norm：拒绝把整屏存成 golden。"}
    src = source
    if src is None:
        if grabber is None:
            return {"saved": False, "verdict": "no_source",
                    "reason": "没给帧来源（frame_grabber 或 source），无法取像存 golden。"}
        src = frame_source_from_grabber(grabber, window_id)

    return evr.save_golden_from_capture(
        store, golden_key, bbox_norm=bbox, out_size=out_size, source=src,
        meta=meta, overwrite=overwrite)
