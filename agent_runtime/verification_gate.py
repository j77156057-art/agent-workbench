"""验收合并门（M6，流水线第 6~7 段）：结构化事实 × 视觉信号 → 三态，任一否决即否决 + 回滚接线。

信条（docs/multimodal-verification-engine-eda-kickoff.md §2 第6/7段、§4 M6、§0 全链 fail-closed）：
- **三态，不是二态。** 每个信号归一成 MET / UNMET / UNCERTAIN；合并规则
  **任一 UNMET 即 UNMET；否则有 UNCERTAIN 即 UNCERTAIN；全 MET 才 MET**。
  对应文档 §2 line112："结构化全绿但视觉明显不对 → unmet"。
- **只有 MET 才通过（fail-closed）。** UNCERTAIN 绝不静默放行——"拿不准就别动手"，
  一律升级给人 / 交下一段核实。`acceptance_decision` 只对 MET 返回 accept。
- **VLM 只能否决，不能自证通过。** 视觉结论 `verified:false` 起步：`regression` 是硬否决；
  `localized`（1px 缺失但全局达标）在没拿到已核实的 VLM 判定前算 UNCERTAIN，绝不当"通过"。
- **回滚接线但默认不擅自动手。** 第7段复用 `regions.rollback_changeset`（其内部有
  `require_approval` 人审批门）。本模块只在 **UNMET** 且显式 `auto_rollback=True` 且注入了
  `rollback` 回调时才执行回滚；UNCERTAIN 永不自动回滚（拿不准先问人）。测试用假 rollback
  证明"被判 unmet 且可回滚"这条验收，不伪造真实 git/分区状态。

依赖：纯标准库。不 import numpy/PIL/regions（回滚回调由调用方注入），
保证在无 git/无 X11 的机器上也能离线测合并逻辑。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

__all__ = [
    "MET", "UNMET", "UNCERTAIN",
    "structured_state", "visual_state", "merge_signals",
    "acceptance_decision", "gate_and_maybe_rollback",
]

MET = "met"
UNMET = "unmet"
UNCERTAIN = "uncertain"

# 结构化事实里被视为"一票否决"的布尔指标键（False=否决，None=证据不足/uncertain，True=绿）。
# 语义来自 M3 EdaFact.to_dict()["metrics"]：这些是"过没过"的判定量，不是普通数值。
_DEFAULT_CRITICAL_BOOLS = ("timing_met", "drc_clean", "lvs_clean")
# 视觉上判为硬否决的 verdict（明显回退 / 拿不到证据都不算通过）。
_VISUAL_UNMET_VERDICTS = ("regression",)
# 视觉上算"有变化但没核实 / 缺证据"→ UNCERTAIN 的 verdict。
_VISUAL_UNCERTAIN_VERDICTS = (
    "localized", "needs_golden", "no_frame", "capture_error",
    "roi_required", "no_source",
)
# 视觉上判为通过（仍需结构化佐证）。
_VISUAL_PASS_VERDICTS = ("pass", "identical")


def _is_true(v: Any) -> bool:
    return v is True


def _is_false(v: Any) -> bool:
    return v is False


def structured_state(fact, *,
                     critical: Optional[Iterable[str]] = None) -> tuple[str, list[str]]:
    """把 M3 `EdaFact.to_dict()`（或同形状 dict，或 dict 列表）归一成 (state, reasons)。

    多 fact 折叠规则：任一 UNMET → UNMET；否则任一 UNCERTAIN → UNCERTAIN；全 MET → MET。
    对应"综合后时序/面积/DRC"这类需要一起看门的情形。

    单 fact 规则（对齐文档 §0 fail-closed）：
    - fact 为空 / ok=False（解析失败、无可信证据）→ UNCERTAIN（绝不当绿）。
    - 任一 critical 布尔指标 = False → UNMET（结构化明确说没过，一票否决）。
    - critical 指标里有 None（证据不齐，M3 的 timing_met/drc_clean 不下结论）→ UNCERTAIN。
    - 无 critical 判定量（普通 utilization 数值不含判定量）→ UNCERTAIN（不猜其它键）。
    - 否则 → MET。
    `critical` 默认挑 fact.metrics 里的 timing_met / drc_clean / lvs_clean；调用方可显式传
    要纳入门禁的指标名，避免把普通数值当布尔误判。
    """
    # ---- 多 fact 折叠 ----
    if isinstance(fact, list):
        if not fact:
            return UNCERTAIN, ["结构化证据缺失：没有可信事实，不能判通过"]
        folded: list[tuple[str, list[str]]] = []
        for item in fact:
            folded.append(structured_state(item, critical=critical))
        combined: list[str] = []
        for _state, _reasons in folded:
            combined.extend(_reasons)
        if any(s == UNMET for s, _ in folded):
            return UNMET, combined or ["结构化否决"]
        if any(s == UNCERTAIN for s, _ in folded):
            return UNCERTAIN, combined or ["结构化证据不足"]
        return MET, combined or ["多份结构化事实均绿"]

    # ---- 单 fact ----
    reasons: list[str] = []
    if not fact or not isinstance(fact, dict):
        return UNCERTAIN, ["结构化证据缺失：没有可信事实，不能判通过"]
    if not fact.get("ok"):
        return UNCERTAIN, ["结构化解析未成功(ok=false)：证据不可信，交人工/下一段"]

    metrics = fact.get("metrics") or {}
    if critical is None:
        critical = [k for k in _DEFAULT_CRITICAL_BOOLS if k in metrics]
        # 没标注判定量时，保守：不猜别的键，缺关键判定 → uncertain。
        if not critical:
            return UNCERTAIN, ["结构化事实无门禁判定量(timing_met/drc_clean/...)：证据不足以裁决"]

    for key in critical:
        val = metrics.get(key)
        if _is_false(val):
            reasons.append(f"结构化否决：{key}=False")
        elif val is None:
            reasons.append(f"结构化证据不足：{key} 未知(None)")

    if any(_is_false(metrics.get(k)) for k in critical):
        return UNMET, reasons or ["结构化否决"]
    if any(metrics.get(k) is None for k in critical):
        return UNCERTAIN, reasons or ["结构化证据不足"]
    return MET, [f"结构化绿：{', '.join(critical)} 均 True"]


def visual_state(result: Optional[dict], *,
                 vlm_verified: Optional[bool] = None) -> tuple[str, list[str]]:
    """把视觉比对结果（M1/M2/M5 的 verdict dict）归一成 (state, reasons)。

    - result 缺失 / 无 verdict → UNCERTAIN。
    - verdict ∈ {regression} → UNMET（硬否决）。
    - verdict ∈ {localized, needs_golden, no_frame, capture_error, ...} → UNCERTAIN
      （有变化但没核实 / 拿不到证据；VLM 结论 verified:false 一律不当通过）。
      **例外**：若给了 `vlm_verified=True` 且 verdict=='pass'/'identical' → MET；
      只有当上层明确说"VLM 已核实为通过"才允许视觉佐证为绿。
    - verdict ∈ {pass, identical} → MET（结构达标、与 golden 同源）。
    """
    if not result or not isinstance(result, dict) or "verdict" not in result:
        return UNCERTAIN, ["视觉结果缺失：没有可判定的比对结论"]
    verdict = result.get("verdict")
    if verdict in _VISUAL_UNMET_VERDICTS:
        return UNMET, [f"视觉明显回退：verdict={verdict}"]
    if verdict in _VISUAL_UNCERTAIN_VERDICTS:
        # 拿不准：即使 VLM 口头说没事，verified:false 也不放行；除非显式已核实通过。
        if vlm_verified is True and verdict == "localized":
            return MET, ["视觉 localized 但 VLM 已核实通过(vlm_verified=True)"]
        return UNCERTAIN, [f"视觉未核实/证据不足：verdict={verdict}（结论 verified:false，不当通过）"]
    if verdict in _VISUAL_PASS_VERDICTS:
        return MET, [f"视觉通过：verdict={verdict}"]
    return UNCERTAIN, [f"未知视觉 verdict：{verdict}"]


def merge_signals(*states: tuple[str, list[str]]) -> dict[str, Any]:
    """合并任意多个 (state, reasons) → 三态结论。规则：UNMET 优先，其次 UNCERTAIN。"""
    all_reasons: dict[str, list[str]] = {}
    flat: list[str] = []
    for state, reasons in states:
        all_reasons[state] = all_reasons.get(state, []) + list(reasons)
        flat.extend(reasons)

    if UNMET in all_reasons:
        final = UNMET
    elif UNCERTAIN in all_reasons:
        final = UNCERTAIN
    else:
        final = MET
    return {
        "state": final,
        "met": final == MET,
        "escalate_to_vlm": final == UNCERTAIN,
        "needs_human": final != MET,
        "vetoed": UNMET in all_reasons,
        "reasons": all_reasons,
        "summary": "; ".join(flat) if flat else "全部信号一致通过",
    }


def acceptance_decision(merged: dict[str, Any]) -> dict[str, Any]:
    """验收门：只有 MET 才 accept；UNMET → rollback 候选；UNCERTAIN → 必须升级给人。"""
    state = merged.get("state")
    if state == MET:
        return {"accept": True, "action": "accept", "state": state,
                "reason": "结构化 + 视觉双绿，放行。"}
    if state == UNMET:
        return {"accept": False, "action": "rollback", "state": state,
                "reason": "有信号明确否决，拒绝放行并回滚到 golden/升级给人。"}
    return {"accept": False, "action": "escalate", "state": state,
            "reason": "信号拿不准（未核实/缺证据），绝不静默放行，升级人工/下一段核实。"}


def gate_and_maybe_rollback(*, structured: Optional[dict], visual: Optional[dict],
                            changeset_id: Optional[str] = None,
                            rollback: Optional[Callable[[str], Any]] = None,
                            auto_rollback: bool = False,
                            critical: Optional[Iterable[str]] = None,
                            vlm_verified: Optional[bool] = None) -> dict[str, Any]:
    """第6段合并 + 第7段验收/回滚接线的一站式入口。

    步骤：structured_state + visual_state → merge_signals → acceptance_decision。
    回滚只在 **UNMET** 且 `auto_rollback=True` 且给了 `rollback` 回调且有 `changeset_id` 时执行；
    UNCERTAIN 永不自动回滚（拿不准先问人，对齐"拿不准就别动手"）。

    `rollback(changeset_id)` 由调用方注入——生产环境里它应转调
    `regions.rollback_changeset(root, cs_id)`（该函数内部自带 require_approval 人审批门）；
    这里不伪造 git/分区状态，测试注入假回调即可验"unmet 且可回滚"。
    返回体附 `rollback_attempted` / `rollback_result` 供审计，默认 False。
    """
    st_state, st_reasons = structured_state(structured, critical=critical)
    vi_state, vi_reasons = visual_state(visual, vlm_verified=vlm_verified)
    merged = merge_signals((st_state, st_reasons), (vi_state, vi_reasons))
    decision = acceptance_decision(merged)

    out = {
        "structured_state": st_state,
        "visual_state": vi_state,
        "merged": merged,
        "decision": decision,
        "rollback_attempted": False,
        "rollback_result": None,
    }

    if decision["action"] == "rollback" and auto_rollback:
        if rollback is None or not changeset_id:
            out["rollback_result"] = {"ok": False,
                                      "reason": "UNMET 但没给 rollback 回调/changeset_id：需人工回滚或授权。"}
        else:
            out["rollback_attempted"] = True
            try:
                out["rollback_result"] = rollback(changeset_id)
            except Exception as exc:  # fail-closed：回滚执行异常也如实记，不吞
                out["rollback_result"] = {"ok": False, "reason": "rollback_error: %s" % str(exc)[:200]}
    return out
