"""EDA 七段端到端样例 pipeline 的离线测试。

用合成版图 + 已 parse 好的 fact dict 把七段串起来跑，断言最终 verdict / decision
以及"unmet 且可回滚 / uncertain 绝不回滚 / 双绿放行 / no_frame fail-closed"。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eda_verify_pipeline as ep                              # noqa: E402
from agent_runtime import visual_regression as vr             # noqa: E402
from agent_runtime import verification_gate as vg             # noqa: E402


def _rgb_source(arr):
    h, w = arr.shape[:2]
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    bgra = np.dstack([arr[:, :, 2:3], arr[:, :, 1:2], arr[:, :, 0:1], alpha])
    raw = np.ascontiguousarray(bgra, dtype=np.uint8).tobytes()
    return lambda: (raw, w, h, 0x1)


def _fact(ok=True, **metrics):
    return {"ok": ok, "tool": "vivado", "kind": "timing",
            "metrics": metrics, "missing": [], "provenance": [], "error": None}


@pytest.fixture
def store(tmp_path):
    return vr.GoldenStore(root=str(tmp_path / "golden"))


def test_pipeline_full_green_met_accept(store):
    base = ep._synth_layout()
    out = ep.run_eda_case(
        store=store, golden_key="k1", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source(base),
        source_check=_rgb_source(base),
    )
    final = out["final"]
    assert final["merged"]["state"] == vg.MET
    assert final["decision"]["action"] == "accept"
    assert final["rollback_attempted"] is False
    # 七段 trace 齐全
    stage_ids = [s["stage"] for s in out["stages"]]
    assert stage_ids == ["0-domain-register", "1-structured-truth",
                         "2-3-capture-save-golden", "2-5-capture-roi-diff",
                         "6-7-merge-gate-rollback"]


def test_pipeline_structured_veto_visual_green_still_unmet_rollback(store):
    """结构化 timing_met=False + 视觉 identical → UNMET；rollback 被以正确 cs_id 调用一次。"""
    base = ep._synth_layout()
    calls = []
    def fake_rb(cs):
        calls.append(cs)
        return {"ok": True}
    out = ep.run_eda_case(
        store=store, golden_key="k2", view="layout",
        facts=_fact(timing_met=False),
        source_ok=_rgb_source(base),
        source_check=_rgb_source(base),   # 视觉 identical
        changeset_id="cs-1", rollback=fake_rb, auto_rollback=True,
    )
    final = out["final"]
    assert final["merged"]["state"] == vg.UNMET
    assert final["decision"]["action"] == "rollback"
    assert final["rollback_attempted"] is True
    assert calls == ["cs-1"]


def test_pipeline_gross_visual_regression_unmet(store):
    base = ep._synth_layout()
    gross = ep._synth_layout(gross=True)
    out = ep.run_eda_case(
        store=store, golden_key="k3", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source(base),
        source_check=_rgb_source(gross),
    )
    assert out["final"]["merged"]["state"] == vg.UNMET
    # gross → regression
    visual_stage = [s for s in out["stages"] if s["stage"] == "2-5-capture-roi-diff"][0]
    assert visual_stage["verdict"] == "regression"


def test_pipeline_via_missing_localized_uncertain_never_rolls_back(store):
    """§4 M6 铁律：UNCERTAIN 拿不准，即使 auto_rollback=True 也不回滚。"""
    base = ep._synth_layout()
    missing = ep._synth_layout(missing_via=True)
    def boom(cs):
        raise AssertionError("UNCERTAIN 绝不该触发回滚")
    out = ep.run_eda_case(
        store=store, golden_key="k4", view="layout",
        facts=_fact(timing_met=True),                     # 结构化绿
        source_ok=_rgb_source(base),
        source_check=_rgb_source(missing),                # 视觉 localized（未核实）
        changeset_id="cs-x", rollback=boom, auto_rollback=True,
    )
    final = out["final"]
    # 视觉 verdict 应是 localized（1px via 缺失被 region_hash 抓到）
    visual_stage = [s for s in out["stages"] if s["stage"] == "2-5-capture-roi-diff"][0]
    assert visual_stage["verdict"] == "localized"
    assert final["merged"]["state"] == vg.UNCERTAIN
    assert final["decision"]["action"] == "escalate"
    assert final["rollback_attempted"] is False


def test_pipeline_no_frame_fail_closed(store):
    """取像返回 None → no_frame → UNCERTAIN，绝不静默放行。"""
    base = ep._synth_layout()
    out = ep.run_eda_case(
        store=store, golden_key="k5", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source(base),
        source_check=lambda: None,
    )
    visual_stage = [s for s in out["stages"] if s["stage"] == "2-5-capture-roi-diff"][0]
    assert visual_stage["verdict"] == "no_frame"
    assert out["final"]["merged"]["state"] == vg.UNCERTAIN
    assert out["final"]["decision"]["action"] == "escalate"


def test_pipeline_roi_required_refused(store):
    """没给 view/bbox → 拒绝整屏，直接返回 uncertain，不进比对。"""
    out = ep.run_eda_case(
        store=store, golden_key="k6",
        facts=_fact(timing_met=True),
        source_ok=lambda: (b"", 0, 0, 0),
        source_check=lambda: (b"", 0, 0, 0),
    )
    assert out["final"]["state"] == vg.UNCERTAIN
    # 只有两阶段（域注册 fail + final），不进 golden/回归
    assert len(out["stages"]) == 1
    assert out["stages"][0]["ok"] is False


def test_pipeline_vlm_escalation_pkg_prepared_not_executed(store):
    """§5 VLM 升级包只准备、不执行：escalation 字段存在，但绝没跑模型。"""
    base = ep._synth_layout()
    missing = ep._synth_layout(missing_via=True)
    out = ep.run_eda_case(
        store=store, golden_key="k7", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source(base),
        source_check=_rgb_source(missing),
    )
    visual_stage = [s for s in out["stages"] if s["stage"] == "2-5-capture-roi-diff"][0]
    assert visual_stage["has_vlm_pkg"] is True


def test_render_trace_produces_human_readable(store):
    base = ep._synth_layout()
    out = ep.run_eda_case(
        store=store, golden_key="k8", view="layout",
        facts=_fact(timing_met=True),
        source_ok=_rgb_source(base), source_check=_rgb_source(base),
    )
    text = ep.render_trace(out)
    assert "6-7-merge-gate-rollback" in text
    assert "state" in text


def test_main_cli_runs(tmp_path):
    rc = ep.main(["--tmp", str(tmp_path)])
    assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
