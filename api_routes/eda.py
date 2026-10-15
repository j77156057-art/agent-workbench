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
    # 取像后端（平台可切换）：auto=按平台选（nt→gdi，其余→x11）；
    # gdi=Windows 真窗口截屏；x11=Linux/X11（M4 ImageMagick import）；vnc=远端 framebuffer。
    capture: str = "auto"
    source: str = "foreground"            # 仅 capture=gdi 生效："foreground"|"embedded"|"hwnd"
    hwnd: Optional[int] = None            # capture=gdi & source=hwnd：目标窗口句柄
    window_id: Optional[int] = None       # capture=x11：X11 window id（缺省回退用 hwnd）
    vnc_host: Optional[str] = None        # capture=vnc：VNC 服务地址（默认 127.0.0.1）
    vnc_port: Optional[int] = None        # capture=vnc：VNC 端口（默认 5900）
    overwrite: bool = True                # save 模式：是否覆盖已存在 golden
    out_size: int = 256
    ssim_threshold: float = 0.98
    delta_threshold: float = 8.0
    grid: int = 8


class ReportGateReq(BaseModel):
    # 在线 EDA 报告页 → M3 解析 → M6 门：取页 → 判定的整链入参。
    tool: str                             # vivado/klayout/verdi
    kind: str                             # timing/utilization/drc/simulation
    url: Optional[str] = None             # 在线报告/文档页地址（fetch=web_fetch 时取这里）
    content: Optional[str] = None         # 调用方已备好的正文（fetch=inline；浏览器抓回走这条）
    fetch: str = "auto"                   # auto/web_fetch=urllib 直抓；inline=直接用 content
    visual: Optional[dict] = None         # 可选视觉信号（含 verdict）；缺省 → 视觉 UNCERTAIN
    vlm_verified: Optional[bool] = None   # 仅上层明确"VLM 已核实通过"才置 True
    critical: Optional[list] = None       # 显式纳入门禁的判定量键；缺省 timing_met/drc_clean/lvs_clean
    # ---- 可选视觉回归通道（给了 golden_key + ROI + 取像后端就现算 M5 diff，喂进同一个 M6 门）----
    # 缺省不配 → 视觉退回 uncertain（纯文本单通道，行为同以前）。
    golden_key: Optional[str] = None      # 与 /api/eda/visual-regression 同一 golden 根，保证同源可比
    view: Optional[str] = None            # layout/waveform/schematic/heatmap（EDA_VIEWS）
    bbox_norm: Optional[list] = None      # 显式 [x1,y1,x2,y2] 归一框，覆盖 view
    capture: str = "auto"                 # auto/gdi/x11/vnc：平台可切换取像后端（复用 make_frame_source）
    source: str = "foreground"            # 仅 capture=gdi 生效："foreground"|"embedded"|"hwnd"
    hwnd: Optional[int] = None            # capture=gdi & source=hwnd：目标窗口句柄
    window_id: Optional[int] = None       # capture=x11：X11 window id（缺省回退用 hwnd）
    vnc_host: Optional[str] = None        # capture=vnc：VNC 服务地址（默认 127.0.0.1）
    vnc_port: Optional[int] = None        # capture=vnc：VNC 端口（默认 5900）
    out_size: int = 256
    ssim_threshold: float = 0.98
    delta_threshold: float = 8.0
    grid: int = 8
    changeset_id: Optional[str] = None    # 门判 rollback 时，登记"回滚此变更集"审批请求；缺省不登记（纯建议）


def resolve_capture(capture: Optional[str]) -> str:
    """把 'auto' 按平台解析成具体取像后端；显式值原样校验。未知值保守回落 'gdi'。

    - auto：nt（Windows）→ gdi；其余（Linux/…）→ x11（EDA 工具真跑起来的机器多在 X11/VNC）。
    - 显式 gdi/x11/vnc：直接用（跨平台调试/指定远端时用）。
    """
    cap = str(capture or "auto").strip().lower()
    if cap == "auto":
        return "gdi" if os.name == "nt" else "x11"
    return cap if cap in ("gdi", "x11", "vnc") else "gdi"


def make_frame_source(req: "VisualRegressionReq", pid: Any = None):
    """按 req.capture 归一成一个零参 FrameSource -> (bgra, w, h, hwnd) | None。

    正是 eda_visual_regression / engine_visual_regression 要的取像契约，于是"换后端其余
    零改动"。三条后端都 fail-closed：任何取不到帧的情况一律返回 None，上层照旧判
    no_frame，绝不拼一个看着像的帧。重模块（screen_capture / frame_grabber_linux /
    eda_visual_regression）全部惰性导入，保证无 PIL / 无 X11 / 无网络时 import 本路由也不炸。
    """
    backend = resolve_capture(req.capture)

    if backend == "x11":
        from agent_runtime import frame_grabber_linux as fgl
        from agent_runtime import eda_visual_regression as ev
        wid = req.window_id if req.window_id is not None else req.hwnd
        grabber = fgl.x11_grabber(window_id=wid)
        return ev.frame_source_from_grabber(grabber, wid)

    if backend == "vnc":
        from agent_runtime import frame_grabber_linux as fgl
        from agent_runtime import eda_visual_regression as ev
        grabber = fgl.vnc_frame_grabber(req.vnc_host or "127.0.0.1",
                                        int(req.vnc_port or 5900))
        return ev.frame_source_from_grabber(grabber, req.window_id)

    # gdi（Windows 域）：沿用原 source 语义——前台 / 嵌入引擎窗 / 指定 HWND。
    import screen_capture

    def _gdi_source():
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

    return _gdi_source


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

    @router.post("/api/eda/report-gate")
    async def report_gate(req: ReportGateReq):
        """在线 EDA 报告页 → M3 解析 →（可选 M4/M5 视觉回归）→ M6 合并门 的一整条链（**只判不执行回滚**）。

        取页源可切换、其余零改动（对齐平台可切换取像源同套路）：
        - `fetch="inline"`：直接用调用方已备好的 `content`——通用浏览器（agent 编排层的
          `builtin_browser`）抓回的正文就走这条，产品运行时不碰网络、不依赖扩展。
        - `fetch="web_fetch"|"auto"`：用仓库已有的 `tools.web_fetch`（urllib）抓 `url`。
        视觉通道同样双路径、可插拔：
        - 直接透传已算好的 `visual`（含 verdict）：由上层（例如已跑过 `/api/eda/visual-regression`
          或浏览器截图归一）拿 verdict 进来。
        - 或给 `golden_key` + ROI（`view`/`bbox_norm`）+ 取像后端（`capture`=auto/gdi/x11/vnc）：
          本路由用 `make_frame_source` 归一取像源、golden 根与 `/api/eda/visual-regression` 同源，
          在 `gate_online_report` 内部**现算**一次 M5 ROI golden diff，把 verdict 喂进同一个 M6 门。
        全程 fail-closed：取不到正文 → `no_report_text` + uncertain；取到但非本类报告 → M3 判 ok:false →
        uncertain；取不到帧/没 golden → M5 判 no_frame/needs_golden → uncertain。**不**执行回滚，
        也不在在线链里存 golden（存基线是写盘动作，留给 visual-regression mode=save）。
        诚实边界：本机通常没装 Vivado/KLayout，被摄窗口是**替身**、非真版图；但比对是**真算的**
        确定性 diff，verdict 由像素得出、`verified:false` 起步，与 M6 门一致。
        """
        from agent_runtime import eda_web_report as ewr

        root = ctx._project_root_or_error() if hasattr(ctx, "_project_root_or_error") else None

        kwargs: dict = {}
        visual_active = bool(req.golden_key and (req.view or req.bbox_norm))
        if visual_active:
            if not root:
                return {"ok": False, "error": "未配置代码库"}
            pid = ctx._request_project_id() if hasattr(ctx, "_request_project_id") else None
            # 复用平台可切换取像装配（capture=gdi/x11/vnc）；ReportGateReq 带齐 make_frame_source 所需字段。
            frame_source = make_frame_source(req, pid)
            kwargs = {
                "frame_source": frame_source,
                "golden_root": os.path.join(root, ".docmind", "eda_golden"),
                "golden_key": req.golden_key,
                "view": req.view,
                "bbox_norm": req.bbox_norm,
                "out_size": req.out_size,
                "ssim_threshold": req.ssim_threshold,
                "delta_threshold": req.delta_threshold,
                "grid": req.grid,
            }

        # web_fetch 是阻塞 urllib、视觉回归是阻塞取像/比对，都落线程池；inline/注入源同池也无副作用。
        return await run_in_threadpool(
            ewr.gate_online_report,
            tool=req.tool, kind=req.kind, url=req.url, content=req.content,
            fetch=req.fetch, visual=req.visual, vlm_verified=req.vlm_verified,
            critical=req.critical,
            changeset_id=req.changeset_id, project_root=root,
            **kwargs)

    @router.post("/api/eda/visual-regression")
    async def visual_regression(req: VisualRegressionReq):
        """第 3~5 段真机视觉回归：抓一个**真实窗口**的 ROI → SSIM + 8×8 块哈希 vs golden →
        算出 verdict（不再手选）。复用已提交的 M1/M2/M4/M5 零件，本层只做取像源装配。

        取像后端平台可切换（`capture`）：auto 按平台选（Windows→gdi，其余→x11）；
        gdi=真实 GDI 截屏（前台/嵌入窗/指定 HWND），x11=Linux/X11（M4 ImageMagick import），
        vnc=远端 framebuffer（M4 最小 RFB）。三条后端都归一成同一个 FrameSource 契约，
        **换后端其余零改动**，故交付物在别的机器上是"选个后端"而非"必须换平台/装某工具"。
        诚实边界：本机通常没装 Vivado/KLayout，被摄窗口是**替身**、非真版图；但比对链路是
        真算的确定性 diff，verdict 由像素得出、`verified:false` 起步，与 M6 门一致。取不到
        帧/没 golden → 管线照旧 fail-closed 成 no_frame/needs_golden，绝不假绿。
        """
        root = ctx._project_root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        from agent_runtime import eda_visual_regression as ev
        from agent_runtime import visual_regression as vr

        pid = ctx._request_project_id() if hasattr(ctx, "_request_project_id") else None
        backend = resolve_capture(req.capture)
        # 按平台/参数归一取像源（gdi/x11/vnc）；三条后端都 fail-closed，取不到帧→None。
        source = make_frame_source(req, pid)

        store = vr.GoldenStore(root=os.path.join(root, ".docmind", "eda_golden"))
        common = dict(view=req.view, bbox_norm=req.bbox_norm, source=source,
                      out_size=req.out_size)
        if req.mode == "save":
            r = await run_in_threadpool(
                ev.save_eda_golden, store, req.golden_key,
                meta={"capture": backend, "source": req.source, "view": req.view,
                      "bbox_norm": req.bbox_norm},
                overwrite=req.overwrite, **common)
            return {"ok": True, "mode": "save", "capture": backend, **r}

        r = await run_in_threadpool(
            ev.check_eda_regression, store, req.golden_key,
            ssim_threshold=req.ssim_threshold, delta_threshold=req.delta_threshold,
            grid=req.grid, **common)
        return {"ok": True, "mode": "check", "capture": backend, **r}

    return router
