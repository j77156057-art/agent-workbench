"""M6 合并门/回滚接线 离线单测。

纪律：本机不真动 git/分区，回滚用**注入的假 rollback 回调**验"unmet 且可回滚"
（开工文档 §4 M6 验收口径）。合并逻辑纯函数，喂 M3 EdaFact.to_dict() 形状 +
M1/M2/M5 的 verdict 形状即可。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime.verification_gate import (  # noqa: E402
    MET, UNMET, UNCERTAIN,
    structured_state, visual_state, merge_signals,
    acceptance_decision, gate_and_maybe_rollback,
)


def _fact(ok=True, **metrics):
    return {"ok": ok, "tool": "vivado", "kind": "timing",
            "metrics": metrics, "missing": [], "provenance": [], "error": None}


# ---- structured_state ------------------------------------------------------

def test_structured_green():
    state, _ = structured_state(_fact(timing_met=True))
    assert state == MET


def test_structured_veto():
    state, reasons = structured_state(_fact(timing_met=False))
    assert state == UNMET
    assert any("timing_met=False" in r for r in reasons)


def test_structured_uncertain_on_none_metric():
    # M3 铁律：证据不齐时 timing_met=None，绝不当绿
    state, _ = structured_state(_fact(timing_met=None))
    assert state == UNCERTAIN


def test_structured_uncertain_when_ok_false():
    state, _ = structured_state(_fact(ok=False, timing_met=True))
    assert state == UNCERTAIN


def test_structured_uncertain_when_no_gate_metric():
    # 只有普通数值（利用率），没判定量 → 不猜 → uncertain
    state, _ = structured_state(_fact(lut=1234, ff=567))
    assert state == UNCERTAIN


def test_structured_uncertain_when_empty():
    assert structured_state(None)[0] == UNCERTAIN
    assert structured_state([])[0] == UNCERTAIN


def test_structured_multi_fact_any_veto():
    # [时序 MET, DRC UNMET] → 折叠 UNMET
    facts = [_fact(timing_met=True), _fact(timing_met=True, drc_clean=False)]
    state, _ = structured_state(facts)
    assert state == UNMET


def test_structured_multi_fact_all_green():
    facts = [_fact(timing_met=True), _fact(timing_met=True, drc_clean=True)]
    state, _ = structured_state(facts)
    assert state == MET


def test_structured_multi_fact_uncertain_bubbles_over_met():
    facts = [_fact(timing_met=True), _fact(ok=False, timing_met=True)]
    state, _ = structured_state(facts)
    assert state == UNCERTAIN


def test_structured_explicit_critical_overrides():
    # 显式指定 critical，避免猜别的键
    state, _ = structured_state(_fact(our_gate=False, other=True), critical=["our_gate"])
    assert state == UNMET


# ---- visual_state ----------------------------------------------------------

def test_visual_regression_is_unmet():
    state, _ = visual_state({"verdict": "regression"})
    assert state == UNMET


@pytest.mark.parametrize("verdict", ["localized", "needs_golden", "no_frame", "capture_error", "roi_required", "no_source"])
def test_visual_uncertain_for_unverified(verdict):
    state, _ = visual_state({"verdict": verdict})
    assert state == UNCERTAIN


def test_visual_pass_and_identical_are_met():
    assert visual_state({"verdict": "pass"})[0] == MET
    assert visual_state({"verdict": "identical"})[0] == MET


def test_visual_missing_result_is_uncertain():
    assert visual_state(None)[0] == UNCERTAIN
    assert visual_state({})[0] == UNCERTAIN


def test_visual_localized_can_be_upgraded_by_verified_vlm():
    # 唯一提级通道：上层显式说 VLM 已核实通过，才把 localized 抬到 MET
    state, _ = visual_state({"verdict": "localized"}, vlm_verified=True)
    assert state == MET


def test_visual_localized_stays_uncertain_when_vlm_not_verified():
    # VLM 结论 verified:false 一律不放行
    state, _ = visual_state({"verdict": "localized"}, vlm_verified=False)
    assert state == UNCERTAIN


# ---- merge_signals + acceptance_decision ----------------------------------

def test_merge_any_veto_wins():
    merged = merge_signals((MET, ["绿"]), (UNMET, ["红"]))
    assert merged["state"] == UNMET
    assert merged["vetoed"] is True
    assert merged["met"] is False


def test_merge_uncertain_bubbles_when_no_veto():
    merged = merge_signals((MET, ["绿"]), (UNCERTAIN, ["拿不准"]))
    assert merged["state"] == UNCERTAIN
    assert merged["needs_human"] is True
    assert merged["escalate_to_vlm"] is True


def test_merge_all_met_is_met():
    merged = merge_signals((MET, ["绿1"]), (MET, ["绿2"]))
    assert merged["state"] == MET
    assert merged["met"] is True
    assert merged["needs_human"] is False


def test_acceptance_only_met_passes():
    assert acceptance_decision({"state": MET})["accept"] is True
    assert acceptance_decision({"state": UNMET})["accept"] is False
    assert acceptance_decision({"state": UNCERTAIN})["accept"] is False


def test_acceptance_action_mapping():
    assert acceptance_decision({"state": MET})["action"] == "accept"
    assert acceptance_decision({"state": UNMET})["action"] == "rollback"
    assert acceptance_decision({"state": UNCERTAIN})["action"] == "escalate"


# ---- 端到端 gate_and_maybe_rollback ---------------------------------------

def test_m6_acceptance_structured_green_visual_unmet_is_rollbackable():
    """开工文档 §4 M6 验收口径：结构化全绿 + 视觉 unmet → 判 unmet 且可回滚。"""
    calls = []

    def fake_rollback(cs_id):
        calls.append(cs_id)
        return {"ok": True, "reason": "reverted %s" % cs_id}

    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=True),
        visual={"verdict": "regression"},
        changeset_id="abc123",
        rollback=fake_rollback,
        auto_rollback=True,
    )
    assert out["structured_state"] == MET
    assert out["visual_state"] == UNMET
    assert out["merged"]["state"] == UNMET
    assert out["decision"]["action"] == "rollback"
    assert out["decision"]["accept"] is False
    assert out["rollback_attempted"] is True
    assert calls == ["abc123"]   # 以正确 cs_id 调用一次
    assert out["rollback_result"]["ok"] is True


def test_gate_default_no_auto_rollback():
    """默认不擅自动手：即便 UNMET，auto_rollback=False 时也不回滚。"""
    def fake_rollback(cs_id):
        raise AssertionError("绝不该被调用")
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=True),
        visual={"verdict": "regression"},
        changeset_id="x", rollback=fake_rollback, auto_rollback=False,
    )
    assert out["decision"]["action"] == "rollback"
    assert out["rollback_attempted"] is False


def test_uncertain_never_auto_rolls_back():
    """UNCERTAIN 拿不准，即使 auto_rollback=True 也不回滚（对齐 §0 '拿不准别动手'）。"""
    def fake_rollback(cs_id):
        raise AssertionError("UNCERTAIN 绝不该触发回滚")
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=True),
        visual={"verdict": "localized"},   # 未核实 → UNCERTAIN
        changeset_id="x", rollback=fake_rollback, auto_rollback=True,
    )
    assert out["merged"]["state"] == UNCERTAIN
    assert out["decision"]["action"] == "escalate"
    assert out["rollback_attempted"] is False


def test_unmet_no_rollback_callable_records_need_for_human():
    """UNMET 但没注入 rollback/cs_id → 记 rollback_result 需要人工回滚，不吞不崩。"""
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=False),
        visual={"verdict": "identical"},
        auto_rollback=True,   # 显式想 auto 但缺 rollback/cs_id
    )
    assert out["merged"]["state"] == UNMET
    assert out["rollback_attempted"] is False
    assert out["rollback_result"]["ok"] is False
    assert "没给" in out["rollback_result"]["reason"]


def test_rollback_exception_is_recorded_not_swallowed():
    """回滚回调抛异常 → fail-closed 记录 rollback_error，不崩不吞。"""
    def boom(cs_id):
        raise RuntimeError("git not available")
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=True),
        visual={"verdict": "regression"},
        changeset_id="y", rollback=boom, auto_rollback=True,
    )
    assert out["rollback_attempted"] is True
    assert out["rollback_result"]["ok"] is False
    assert "rollback_error" in out["rollback_result"]["reason"]


def test_dual_green_is_met_and_no_action():
    """结构化 + 视觉都绿 → MET，decision=accept，绝不触发回滚。"""
    def fake_rollback(cs_id):
        raise AssertionError("MET 不该回滚")
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=True, drc_clean=True),
        visual={"verdict": "pass"},
        changeset_id="z", rollback=fake_rollback, auto_rollback=True,
    )
    assert out["merged"]["state"] == MET
    assert out["decision"]["action"] == "accept"
    assert out["rollback_attempted"] is False


def test_structured_veto_visual_green_still_unmet():
    """任一否决即否决：结构化 False + 视觉 identical → UNMET（结构化一票否决）。"""
    out = gate_and_maybe_rollback(
        structured=_fact(timing_met=False),
        visual={"verdict": "identical"},
    )
    assert out["structured_state"] == UNMET
    assert out["visual_state"] == MET
    assert out["merged"]["state"] == UNMET


# ---- 与真实 EdaFact 形状对齐（不依赖 numpy，用手写 dict 模拟 to_dict）------

def test_shape_matches_edafact_to_dict_and_visual_check_output():
    """用 M3 EdaFact.to_dict() 的键形状 + M2 check_visual_regression 的键形状，
    证明合并门消费的正是这两层的产出，字段名不错配。"""
    # EdaFact.to_dict 形状（见 tests/test_eda_tcl.py）
    fact_dict = {"ok": True, "tool": "vivado", "kind": "timing",
                 "metrics": {"wns": 0.412, "tns": 0.0, "timing_met": True, "fmax": 60.0},
                 "missing": [], "provenance": [], "error": None}
    # check_visual_regression 三态（见 visual_regression.compare）
    visual_result = {"verdict": "pass", "passed": True, "escalate_to_vlm": False,
                     "ssim": 0.995, "mean_abs_delta": 1.2,
                     "blocks": {"count": 0, "total": 64, "ratio": 0.0, "indices": [], "grid": 8}}
    out = gate_and_maybe_rollback(structured=fact_dict, visual=visual_result)
    assert out["merged"]["state"] == MET


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
