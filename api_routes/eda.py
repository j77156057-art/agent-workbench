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

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel


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

    return router
