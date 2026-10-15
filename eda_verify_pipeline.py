"""EDA 七段端到端验收样例（开工文档 §2 全流程 · 演示 / 活文档 / 无副作用）。

把 M1→M6 已建的零件按 §2 的七段串起来跑一次，喂合成 fixture 演示：
"结构化事实 × 视觉回归 → 合并门 → 有回退则回滚"。

**这段脚本只做编排与打印，不新增任何比较/解析/取像逻辑。** 所有零件都来自已提交的模块：
- §1 结构化真值：`agent_runtime.eda_tcl.parse_report` 或已 parse 好的 fact dict。
- §2 取像：M4 `frame_grabber_linux.x11_grabber` + 注入 runner，或调用方直接给 source。
- §3 ROI + §4 确定性 diff + §5 VLM 升级包：M5 `eda_visual_regression.save_eda_golden` /
  `check_eda_regression`（内部走 M2 `engine.check_visual_regression` → M1 `visual_regression.compare`）。
- §6 合并门 + §7 验收/回滚：M6 `verification_gate`。

信条（§0）：
- 本机没有 X11/VNC/ImageMagick/EDA 工具 → 全部走注入 source / fact dict；
  真实取像路径不在本机执行，样例只做离线编排。
- **默认绝不自动回滚**：`auto_rollback=True` 且注入 `rollback` 回调时，也只在 UNMET 才触发；
  UNCERTAIN 一律升级给人，不擅自动手。真实生产里 `rollback(cs_id)` 应转调
  `regions.rollback_changeset`（内含 `require_approval` 人审批门）。
- VLM 只解释不裁决：本样例**不**真跑 VLM；若 M5 打了 `escalation` 包，仅在 trace 里
  记录"已备好、交下一段/人工"，符合结论必须 `verified:false` 起步的信条。

CLI：`python eda_verify_pipeline.py` 打印两条演示：
1) 结构化时序违反 + 版图 via 缺失 → UNMET → 建议回滚；
2) 全绿（结构化 + 视觉 identical）→ MET → 放行。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Any, Callable, Iterable, Optional

# 允许脚本独立运行（工作目录 = 仓库根）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_runtime import visual_regression as vr            # noqa: E402
from agent_runtime import eda_visual_regression as eda_vr    # noqa: E402
from agent_runtime import verification_gate as vg            # noqa: E402


__all__ = ["run_eda_case", "render_trace", "main"]


def run_eda_case(*,
                 store: vr.GoldenStore,
                 golden_key: str,
                 facts: Any,                                    # dict 或 dict 列表（M3 EdaFact.to_dict 形状）
                 source_ok: Callable[[], Optional[tuple]],      # 取像 FrameSource（通过态）
                 source_check: Callable[[], Optional[tuple]],   # 取像 FrameSource（待验态）
                 view: Optional[str] = None,
                 bbox_norm: Optional[Iterable[float]] = None,
                 changeset_id: Optional[str] = None,
                 rollback: Optional[Callable[[str], Any]] = None,
                 auto_rollback: bool = False,
                 critical: Optional[Iterable[str]] = None,
                 vlm_verified: Optional[bool] = None,
                 out_size: int = vr.DEFAULT_ROI_SIZE,
                 ) -> dict[str, Any]:
    """按 §2 七段依次调用既有零件，返回 `{stages:[...], final:{...}}`。

    段与段之间是"上一段产物即下一段输入"的数据流，不引入任何新比较逻辑。
    每段结果都是纯 dict，可 JSON dump；不抛异常穿透（fail-closed：任一段拿不到证据
    → 归到 UNCERTAIN，最后交人工）。
    """
    stages: list[dict[str, Any]] = []

    # §0 域注册：把 view / bbox / golden_key 记进 trace（对应 eda_adapters 该声明的四件事之一）。
    bbox = eda_vr.resolve_roi(view, bbox_norm)
    stages.append({
        "stage": "0-domain-register",
        "golden_key": golden_key,
        "view": view,
        "bbox_norm": bbox,
        "ok": bbox is not None,
        "note": "ROI 定义来自 §0 域注册；本样例用 view/bbox 显式指定。",
    })
    if bbox is None:
        final = {"state": vg.UNCERTAIN, "reason": "未给 view / bbox_norm：拒绝整屏，交人工。"}
        return {"stages": stages, "final": final}

    # §1 结构化真值：直接调 M6 的 structured_state 归一（不重新解析）。
    st_state, st_reasons = vg.structured_state(facts, critical=critical)
    stages.append({
        "stage": "1-structured-truth",
        "facts": facts if isinstance(facts, list) else [facts] if facts else [],
        "state": st_state,
        "reasons": st_reasons,
    })

    # §2 取像（golden 侧） + §3 ROI + 存 golden。
    saved = eda_vr.save_eda_golden(store, golden_key, bbox_norm=bbox,
                                    source=source_ok, out_size=out_size,
                                    meta={"stage": "sample-baseline"})
    stages.append({
        "stage": "2-3-capture-save-golden",
        "result": saved,
        "ok": bool(saved.get("saved")),
    })

    # §2 取像（check 侧） + §3 ROI + §4 确定性 diff + §5 VLM 升级包（不跑 VLM）。
    visual = eda_vr.check_eda_regression(store, golden_key, bbox_norm=bbox,
                                          source=source_check, out_size=out_size,
                                          vlm_escalate=None)  # 绝不真跑 VLM
    stages.append({
        "stage": "2-5-capture-roi-diff",
        "verdict": visual.get("verdict"),
        "passed": visual.get("passed"),
        "escalate_to_vlm": visual.get("escalate_to_vlm"),
        "blocks": visual.get("blocks"),
        "ssim": visual.get("ssim"),
        "mean_abs_delta": visual.get("mean_abs_delta"),
        "has_vlm_pkg": "escalation" in visual,
        "note": "VLM 升级包只准备、不执行——结论必须 verified:false 起步。",
    })

    # §6 合并 + §7 验收/回滚：M6 一站式，回滚只在 UNMET + auto + 注入回调 + cs_id 时才动。
    gate = vg.gate_and_maybe_rollback(
        structured=facts, visual=visual,
        changeset_id=changeset_id, rollback=rollback,
        auto_rollback=auto_rollback, critical=critical,
        vlm_verified=vlm_verified,
    )
    stages.append({"stage": "6-7-merge-gate-rollback", "result": gate})
    return {"stages": stages, "final": gate}


# ---- CLI 演示 ---------------------------------------------------------------

def _rgb_source_factory(arr_rgb):
    """把 (H,W,3) uint8 RGB 数组包成 source()→(bgra,w,h,hwnd)。numpy 只在需要时 import。"""
    import numpy as np
    h, w = arr_rgb.shape[:2]
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    bgra = np.dstack([arr_rgb[:, :, 2:3], arr_rgb[:, :, 1:2], arr_rgb[:, :, 0:1], alpha])
    raw = np.ascontiguousarray(bgra, dtype=np.uint8).tobytes()
    return lambda: (raw, w, h, 0x1)


def _synth_layout(missing_via=False, gross=False):
    import numpy as np
    arr = np.full((300, 300, 3), 30, dtype=np.uint8)
    if gross:
        arr[:, :] = (200, 200, 200)          # 整片换色 → 视觉 regression
        return arr
    arr[:, :] = (30, 40, 50)
    for i in range(0, 300, 20):
        arr[i, :, :] = (90, 100, 110)
        arr[:, i, :] = (90, 100, 110)
    for (cy, cx) in [(60, 60), (120, 120), (180, 60), (60, 180)]:
        if missing_via and (cy, cx) == (120, 120):
            continue
        arr[cy:cy + 12, cx:cx + 12] = (240, 220, 40)
    return arr


def _fact(ok=True, **metrics):
    return {"ok": ok, "tool": "vivado", "kind": "timing",
            "metrics": metrics, "missing": [], "provenance": [], "error": None}


def _demo(tmp_root: str) -> int:
    store = vr.GoldenStore(root=os.path.join(tmp_root, "golden"))

    def banner(t): print("\n==== " + t + " ====")

    banner("演示 A：结构化时序违反 + 版图 gross 回退 → 应 UNMET，rollback 被触发")
    calls = []
    def fake_rb(cs):
        calls.append(cs)
        return {"ok": True, "reason": "reverted %s" % cs}
    out = run_eda_case(
        store=store, golden_key="blkA", view="layout",
        facts=_fact(timing_met=False),                     # 结构化红线：时序违反
        source_ok=_rgb_source_factory(_synth_layout()),
        source_check=_rgb_source_factory(_synth_layout(gross=True)),  # 视觉 gross
        changeset_id="cs-demo-A", rollback=fake_rb, auto_rollback=True,
    )
    print(json.dumps(out["final"]["merged"], ensure_ascii=False, indent=2))
    print("decision:", out["final"]["decision"])
    print("rollback_attempted:", out["final"]["rollback_attempted"], "calls:", calls)

    banner("演示 B：结构化 + 视觉都绿 → 应 MET，accept")
    out2 = run_eda_case(
        store=store, golden_key="blkB", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source_factory(_synth_layout()),
        source_check=_rgb_source_factory(_synth_layout()),
    )
    print(json.dumps(out2["final"]["merged"], ensure_ascii=False, indent=2))
    print("decision:", out2["final"]["decision"])

    banner("演示 C：结构化 ok 但视觉 localized（1px via 缺失，未核实）→ UNCERTAIN，交人工")
    out3 = run_eda_case(
        store=store, golden_key="blkC", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source_factory(_synth_layout()),
        source_check=_rgb_source_factory(_synth_layout(missing_via=True)),
    )
    print(json.dumps(out3["final"]["merged"], ensure_ascii=False, indent=2))
    print("decision:", out3["final"]["decision"],
          " rollback_attempted:", out3["final"]["rollback_attempted"])

    banner("演示 D：取像失败（source_check 返回 None）→ no_frame → UNCERTAIN，绝不静默放行")
    out4 = run_eda_case(
        store=store, golden_key="blkD", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source_factory(_synth_layout()),
        source_check=lambda: None,
    )
    print(json.dumps(out4["final"]["merged"], ensure_ascii=False, indent=2))
    print("decision:", out4["final"]["decision"])

    banner("trace（演示 A 完整段序，用于审计）")
    print(json.dumps(out["stages"], ensure_ascii=False, indent=2)[:1400] + " ...")
    return 0


def render_trace(run_out: dict[str, Any]) -> str:
    """把 run_eda_case 输出渲染成人类可读的分段文本（审计/日志用）。"""
    lines: list[str] = []
    for s in run_out.get("stages", []):
        lines.append("── %s" % s.get("stage"))
        for k, v in s.items():
            if k == "stage":
                continue
            lines.append("   %s: %s" % (k, json.dumps(v, ensure_ascii=False)[:160]))
    lines.append("── final")
    lines.append(json.dumps(run_out.get("final", {}), ensure_ascii=False, indent=2))
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="eda_verify_pipeline",
                                description="EDA 七段端到端验收样例（离线演示）")
    p.add_argument("--demo", action="store_true", default=True,
                   help="跑内置 4 条演示（默认）")
    p.add_argument("--tmp", default=None, help="golden 落盘根目录（默认系统临时目录）")
    args = p.parse_args(argv)
    if args.tmp:
        tmp_root = args.tmp
    else:
        import tempfile
        tmp_root = tempfile.mkdtemp(prefix="eda-verify-demo-")
    return _demo(tmp_root)


if __name__ == "__main__":
    raise SystemExit(main())
