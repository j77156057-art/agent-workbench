"""EDA 域验证路由：结构化解析 / skill 查表 / 双信号合并门（**只读**，不执行回滚）。

领域路由约定同 `engine.py`：`build_router(ctx)` 里 `ctx` 即 `api` 模块本体（依赖容器），
用 `ctx._project_root_or_error` 拿代码库根。

安全边界（对齐开工文档 §0 + M6）：
- **本路由只"判 + 建议"，绝不执行 git 回滚。** 真实回滚须经 `regions.rollback_changeset`
  的 `require_approval` 人工审批门；HTTP 层开一个"自动 revert"入口 = 静默放行风险，
  与全链 fail-closed 信条冲突。合并门返回 `decision.action ∈ {accept,rollback,escalate}`，
  `rollback` 只是"建议回滚"的信号，交由人工/下一道有审批门的动作接口执行。
- 视觉结论坚持 `verified:false` 起步：`/api/eda/verify` 只有显式传 `vlm_verified=true`
  才允许把 `localized` 提级为 MET（映射逻辑在 `verification_gate.visual_state`）。
- 解析/合并全程 fail-closed：拿不到证据一律 `uncertain`/`needs_golden`，不返回假绿。

依赖：复用已提交的 `agent_runtime.eda_tcl` / `eda_adapters` / `verification_gate`，无新逻辑。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool


class ParseReportReq(BaseModel):
    tool: str
    kind: str
    text: str


class VerifyReq(BaseModel):
    # M3 EdaFact.to_dict() 形状；单个 dict 或 dict 列表（多份 fact 折叠）。
    structured: Any
    # M1/M2/M5 的比对结果（含 verdict）；可缺省（缺 → 视觉 UNCERTAIN）。
    visual: Optional[dict] = None
    # 仅当上层明确"VLM 已核实通过"才置 True；默认 None 不放行。
    vlm_verified: Optional[bool] = None
    # 显式指定纳入门禁的结构化判定量键；缺省用 timing_met/drc_clean/lvs_clean。
    critical: Optional[list] = None


class VisualRegressionReq(BaseModel):
    # 第 3~5 段真机视觉回归：抓一个真实窗口的 ROI，与已批准 golden 做确定性 diff。
    golden_key: str = "layout_default"
    view: Optional[str] = None            # layout/waveform/schematic/heatmap（EDA_VIEWS）
    bbox_norm: Optional[list] = None      # 显式 [x1,y1,x2,y2] 归一框，覆盖 view
    mode: str = "check"                   # "check"（比对）| "save"（存基线）
    source: str = "foreground"            # "foreground" | "embedded" | "hwnd"
    hwnd: Optional[int] = None            # source="hwnd" 时的目标窗口句柄
    overwrite: bool = True                # save 模式：是否覆盖已存在 golden
    out_size: int = 256
    ssim_threshold: float = 0.98
    delta_threshold: float = 8.0
    grid: int = 8


def build_router(ctx) -> APIRouter:
    router = APIRouter(tags=["eda"])

    @router.post("/api/eda/parse-report")
    async def parse_report(req: ParseReportReq):
        """把 Vivado/KLayout/Verdi 报告文本解析成结构化事实（M3）。未知组合 fail-closed。"""
        from agent_runtime.eda_tcl import parse_report as _parse
        try:
            fact = _parse(req.tool, req.kind, req.text)
        except Exception as exc:  # 解析器不该抛，抛了也归一成 ok:false，不 500
            return {"ok": False, "tool": req.tool, "kind": req.kind,
                    "error": "parse_exception", "reason": str(exc)[:200]}
        return fact.to_dict()

    @router.get("/api/eda/skills")
    async def skills(tool: str = "vivado"):
        """返回该 EDA 工具的 skill 文本（M0 eda_adapters）。SKILL.md 缺失 → 优雅降级空串。"""
        try:
            from eda_adapters import skill_for_tool
            return {"ok": True, "tool": tool, "skill": skill_for_tool(tool)}
        except Exception as exc:
            return {"ok": False, "tool": tool, "skill": "", "error": str(exc)[:200]}

    @router.post("/api/eda/verify")
    async def verify(req: VerifyReq):
        """第6段双信号合并门 + 第7段验收建议（**只判不动手**）。

        规则：任一信号 UNMET → unmet（建议回滚）；否则有 UNCERTAIN → escalate 给人；
        全 MET 才 accept。本接口**不**执行回滚——真实 revert 须走带人工审批门的动作接口。
        """
        from agent_runtime import verification_gate as vg
        st_state, st_reasons = vg.structured_state(req.structured, critical=req.critical)
        vi_state, vi_reasons = vg.visual_state(req.visual, vlm_verified=req.vlm_verified)
        merged = vg.merge_signals((st_state, st_reasons), (vi_state, vi_reasons))
        decision = vg.acceptance_decision(merged)
        return {
            "ok": True,
            "structured_state": st_state,
            "visual_state": vi_state,
            "merged": merged,
            "decision": decision,
            "executes_rollback": False,
            "note": "只判不执行回滚；真实回滚须经 regions.rollback_changeset 的 require_approval 人工门。",
        }

    @router.post("/api/eda/visual-regression")
    async def visual_regression(req: VisualRegressionReq):
        """第 3~5 段真机视觉回归：抓一个**真实窗口**的 ROI → SSIM + 8×8 块哈希 vs golden →
        算出 verdict（不再手选）。复用已提交的 M1/M2/M4/M5 零件，本层只做取像源装配。

        诚实边界：本机通常没装 Vivado/KLayout，`source` 取的是**真实 GDI 截屏**（前台窗口 /
        嵌入引擎窗口 / 指定 HWND），被摄窗口是**替身**、非真版图；但比对链路是真算的确定性
        diff，verdict 由像素得出、`verified:false` 起步，与 M6 门一致。取不到帧/没 golden →
        管线照旧 fail-closed 成 no_frame/needs_golden，绝不假绿。
        """
        root = ctx._project_root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        from agent_runtime import eda_visual_regression as ev
        from agent_runtime import visual_regression as vr
        import screen_capture

        pid = ctx._request_project_id() if hasattr(ctx, "_request_project_id") else None

        def _source():
            # 归一成 (bgra, w, h, hwnd) 或 None——正是 FrameSource 契约，上层零改动。
            if req.source == "embedded":
                return screen_capture.grab_embedded(pid)
            if req.source == "hwnd":
                if not req.hwnd:
                    return None
                frame = screen_capture.capture_window(req.hwnd)
                if not frame:
                    return None
                raw, w, h = frame
                return raw, w, h, int(req.hwnd)
            return screen_capture.grab_foreground()

        store = vr.GoldenStore(root=os.path.join(root, ".docmind", "eda_golden"))
        common = dict(view=req.view, bbox_norm=req.bbox_norm, source=_source,
                      out_size=req.out_size)
        if req.mode == "save":
            r = await run_in_threadpool(
                ev.save_eda_golden, store, req.golden_key,
                meta={"source": req.source, "view": req.view, "bbox_norm": req.bbox_norm},
                overwrite=req.overwrite, **common)
            return {"ok": True, "mode": "save", **r}

        r = await run_in_threadpool(
            ev.check_eda_regression, store, req.golden_key,
            ssim_threshold=req.ssim_threshold, delta_threshold=req.delta_threshold,
            grid=req.grid, **common)
        return {"ok": True, "mode": "check", **r}

    return router
