"""在线 EDA 报告页 → M3 解析 → M6 门 的取页胶水链路测试（**离线，无网络**）。

覆盖三层：
1) `agent_runtime.eda_web_report` 单元：取页源归一（inline / 注入 / web_fetch 失败规整 None）、
   gate_online_report 的三态与 fail-closed（取不到正文不喂解析器、非报告正文由 M3 判 ok:false）。
2) **双通道**：给 golden + 帧来源时，gate 内部真跑 M5 ROI golden diff（复用合成 RGB 帧，离线）：
   双绿→met/accept、视觉 gross→unmet（只判不动手）、没 golden/取不到帧/取像抛错→uncertain，
   绝不假绿；没配视觉入参→退回纯文本单通道（visual uncertain）。
3) `/api/eda/report-gate` 路由：经 TestClient + stub ctx 验整链装配，重点断言**只判不执行回滚**
   （executes_rollback 恒 False），以及浏览器抓回的正文走 inline 这条路能打通 M3→M6。

复用 tests/test_eda_tcl.py 里的真实报告样本。web_fetch 后端一律 monkeypatch 掉 tools._fetch_page，
保证测试不触网。合成帧来自 eda_verify_pipeline（需 numpy，仅双通道那组用例用到）。
"""
import os
import sys

import pytest
from unittest import mock
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_runtime.eda_web_report as ewr  # noqa: E402
import api_routes.eda as eda_routes  # noqa: E402
from agent_runtime import visual_regression as vr  # noqa: E402
from agent_runtime import eda_visual_regression as ev  # noqa: E402

# 合成 RGB 帧工厂 + 版图生成器：直接借已提交的演示样例，避免测试里另写像素逻辑。
from eda_verify_pipeline import _rgb_source_factory, _synth_layout  # noqa: E402

# ---- 真实形状的报告样本（同 test_eda_tcl.py）----
TIMING_MET = """
Design Timing Summary
---------------------
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
      0.412        0.000                      0                52314        0.035        0.000
Clock  Waveform (ns)         Period(ns)      Frequency(MHz)
sys_clk  {0.000 2.500}        5.000           200.000
slow_clk  {0.000 8.333}       16.667          60.000
"""

TIMING_VIOLATED = """
Design Timing Summary
---------------------
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
     -0.123       -1.560                     42                52314        0.021        0.000
"""

EDA_NEWS_BODY = """
来源：https://example.com/eda-news
标题：华大九天参股芯行纪，补齐布局布线核心 EDA 工具
正文：某国内 EDA 厂商宣布战略投资，加速构建数字电路全流程工具系统……（这是一段行业新闻，
不含 report_timing_summary 表格，理应被判为不可解析的本类报告）
"""


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(eda_routes.build_router(object()))
    return TestClient(app)


# ---- make_page_source / default_page_source 归一 ----

def test_make_page_source_inline_returns_content():
    src = ewr.make_page_source("inline", content="hello body")
    assert src("http://ignored") == "hello body"


def test_make_page_source_inline_blank_is_none():
    src = ewr.make_page_source("inline", content="   ")
    assert src("http://x") is None


def test_make_page_source_injected_wins():
    src = ewr.make_page_source("web_fetch", page_source=lambda url: url.upper())
    assert src("abc") == "ABC"


def test_default_page_source_regularizes_fetch_fail_to_none(monkeypatch):
    # 模拟 tools._fetch_page 回传抓取失败串 → 取页源必须规整成 None（不把错误串当正文）。
    import tools
    monkeypatch.setattr(tools, "_fetch_page",
                        lambda url, *, image_budget=0, query_hint="": "网页读取失败：TimeoutError")
    src = ewr.default_page_source()
    assert src("http://unreachable") is None


def test_default_page_source_passes_real_text(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_fetch_page",
                        lambda url, *, image_budget=0, query_hint="": TIMING_MET)
    src = ewr.default_page_source()
    assert src("http://report") and "WNS" in src("http://report")


# ---- gate_online_report 单元：三态 + fail-closed ----

def test_gate_inline_met_accept():
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               fetch="inline", content=TIMING_MET,
                               visual={"verdict": "pass"})
    assert r["ok"] is True
    assert r["source"] == "inline"
    assert r["fact"]["ok"] is True
    assert r["fact"]["metrics"]["timing_met"] is True
    assert r["gate"]["merged"]["state"] == "met"
    assert r["gate"]["decision"]["action"] == "accept"
    assert r["executes_rollback"] is False


def test_gate_visual_regression_unmet_advises_rollback_but_not_executes():
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               fetch="inline", content=TIMING_MET,
                               visual={"verdict": "regression"})
    assert r["gate"]["merged"]["state"] == "unmet"
    assert r["gate"]["decision"]["action"] == "rollback"
    # 关键安全断言：建议回滚也绝不执行。
    assert r["executes_rollback"] is False
    assert r["gate"]["rollback_attempted"] is False


# ---- 门→审批队列桥线（gate 判 rollback 时登记人工审批请求，绝不自动 revert）----

def test_gate_rollback_with_changeset_enqueues_approval():
    """UNMET→rollback + changeset_id → record_gate_request 被调用，approval_requested=True。"""
    with mock.patch("agent_runtime.cockpit_policy.record_gate_request") as rg:
        r = ewr.gate_online_report(
            tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
            visual={"verdict": "regression"},
            changeset_id="cs-test-001", project_root="/tmp/fake")
    rg.assert_called_once_with("/tmp/fake", "rollback_changeset", "cs-test-001")
    assert r["approval_requested"] is True
    assert r["changeset_id"] == "cs-test-001"
    assert r["executes_rollback"] is False  # 铁律不破：登记≠执行


def test_gate_rollback_without_changeset_no_enqueue():
    """UNMET→rollback 但没给 changeset_id → 不登记（纯建议），approval_requested=False。"""
    with mock.patch("agent_runtime.cockpit_policy.record_gate_request") as rg:
        r = ewr.gate_online_report(
            tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
            visual={"verdict": "regression"})
    rg.assert_not_called()
    assert r["approval_requested"] is False
    assert r["executes_rollback"] is False


def test_gate_escalate_with_changeset_no_enqueue():
    """UNCERTAIN→escalate（非 rollback）即使有 changeset_id 也不登记。"""
    with mock.patch("agent_runtime.cockpit_policy.record_gate_request") as rg:
        r = ewr.gate_online_report(
            tool="vivado", kind="timing", fetch="inline", content=EDA_NEWS_BODY,
            visual={"verdict": "pass"},
            changeset_id="cs-test-002", project_root="/tmp/fake")
    assert r["gate"]["decision"]["action"] == "escalate"
    rg.assert_not_called()
    assert r["approval_requested"] is False


def test_gate_enqueue_failure_degrades_to_advisory():
    """record_gate_request 抛异常 → 门仍返回判定、approval_requested=False，不崩。"""
    with mock.patch("agent_runtime.cockpit_policy.record_gate_request",
                    side_effect=OSError("disk full")):
        r = ewr.gate_online_report(
            tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
            visual={"verdict": "regression"},
            changeset_id="cs-test-003", project_root="/tmp/fake")
    assert r["gate"]["decision"]["action"] == "rollback"  # 判定本身不受队列故障影响
    assert r["approval_requested"] is False
    assert r["executes_rollback"] is False


# ---- 原有测试继续 ----

def test_gate_violated_timing_is_unmet():
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               fetch="inline", content=TIMING_VIOLATED,
                               visual={"verdict": "pass"})
    assert r["fact"]["metrics"]["timing_met"] is False
    assert r["gate"]["merged"]["state"] == "unmet"


def test_gate_non_report_body_fail_closed_uncertain():
    # 取到正文但非本类报告 → M3 判 ok:false → M6 uncertain → escalate（不假绿）。
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               fetch="inline", content=EDA_NEWS_BODY,
                               visual={"verdict": "pass"})
    assert r["ok"] is True               # 链路本身跑通了
    assert r["fact"]["ok"] is False      # 但不是可信时序事实
    assert r["fact"]["error"] == "timing_metrics_not_found"
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"


def test_gate_missing_text_fail_closed_never_parses(monkeypatch):
    # 取不到正文 → no_report_text，且绝不喂解析器（用哨兵断 parse_report 未被调用）。
    called = {"n": 0}
    from agent_runtime import eda_tcl

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("parse_report must NOT be called when there is no text")

    monkeypatch.setattr(eda_tcl, "parse_report", _boom)
    r = ewr.gate_online_report(tool="vivado", kind="timing", fetch="inline", content="   ")
    assert r["ok"] is False
    assert r["error"] == "no_report_text"
    assert r["fetch_failed"] is True
    assert r["fact"] is None
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"
    assert called["n"] == 0


def test_gate_injected_source_none_is_fetch_failed():
    # 注入源（模拟浏览器/web_fetch 抓失败）恒返回 None → fetch_failed 路径。
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               page_source=lambda url: None)
    assert r["error"] == "no_report_text"
    assert r["source"] == "injected"
    assert r["gate"]["decision"]["action"] == "escalate"


def test_gate_web_fetch_backend_parses_fetched_report(monkeypatch):
    # fetch=web_fetch 后端：monkeypatch tools._fetch_page 返回真实报告文本，走 urllib 归一路径。
    import tools
    monkeypatch.setattr(tools, "_fetch_page",
                        lambda url, *, image_budget=0, query_hint="": TIMING_MET)
    r = ewr.gate_online_report(tool="vivado", kind="timing",
                               url="http://host/report", fetch="web_fetch",
                               visual={"verdict": "pass"})
    assert r["source"] == "web_fetch"
    assert r["fact"]["metrics"]["timing_met"] is True
    assert r["gate"]["merged"]["state"] == "met"


# ---- 双通道：gate 内部真跑 M5 ROI golden diff（注入合成 RGB 帧，离线）----

def _save_green_golden(root, key, *, out_size=256):
    """先把"通过态"版图 ROI 存成 golden，之后 check 用同一 ROI，保证同源可比。"""
    store = vr.GoldenStore(root=root)
    saved = ev.save_eda_golden(store, key, view="layout",
                               source=_rgb_source_factory(_synth_layout()),
                               out_size=out_size, meta={"why": "gate dual-channel baseline"})
    assert saved.get("saved"), saved
    return store


def test_gate_dual_channel_both_green_met(tmp_path):
    root = str(tmp_path / "golden")
    _save_green_golden(root, "blkA")
    r = ewr.gate_online_report(
        tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
        golden_key="blkA", golden_root=root, view="layout",
        frame_source=_rgb_source_factory(_synth_layout()))   # 同源绿帧 → identical/pass
    assert r["fact"]["metrics"]["timing_met"] is True
    assert r["visual_source"] == "regression"
    assert r["gate"]["visual_state"] == "met"
    assert r["gate"]["merged"]["state"] == "met"
    assert r["gate"]["decision"]["action"] == "accept"
    assert r["executes_rollback"] is False


def test_gate_dual_channel_visual_gross_unmet_not_executed(tmp_path):
    root = str(tmp_path / "golden")
    _save_green_golden(root, "blkB")
    r = ewr.gate_online_report(
        tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,   # 结构化绿
        golden_key="blkB", golden_root=root, view="layout",
        frame_source=_rgb_source_factory(_synth_layout(gross=True)))        # 视觉 gross → regression
    assert r["visual_source"] == "regression"
    assert r["gate"]["visual_state"] == "unmet"
    assert r["gate"]["merged"]["state"] == "unmet"
    assert r["gate"]["decision"]["action"] == "rollback"
    # 关键安全断言：视觉明确否决、建议回滚，但绝不执行、也绝不自动动手。
    assert r["gate"]["rollback_attempted"] is False
    assert r["executes_rollback"] is False


def test_gate_dual_channel_missing_golden_uncertain(tmp_path):
    # 全新根、没存过 golden → check 判 needs_golden → 视觉 uncertain → 不假绿。
    root = str(tmp_path / "empty_golden")
    r = ewr.gate_online_report(
        tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,   # 结构化绿
        golden_key="never_saved", golden_root=root, view="layout",
        frame_source=_rgb_source_factory(_synth_layout()))
    assert r["visual_source"] == "regression"
    assert r["gate"]["visual_state"] == "uncertain"
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"
    assert r["executes_rollback"] is False


def test_gate_dual_channel_no_frame_uncertain(tmp_path):
    root = str(tmp_path / "golden")
    _save_green_golden(root, "blkD")
    r = ewr.gate_online_report(
        tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
        golden_key="blkD", golden_root=root, view="layout",
        frame_source=lambda: None)          # 取不到帧 → no_frame → uncertain
    assert r["gate"]["visual_state"] == "uncertain"
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"
    assert r["executes_rollback"] is False


def test_gate_dual_channel_capture_raises_fail_closed(tmp_path):
    root = str(tmp_path / "golden")
    _save_green_golden(root, "blkE")

    def _boom():
        raise RuntimeError("capture exploded")

    r = ewr.gate_online_report(
        tool="vivado", kind="timing", fetch="inline", content=TIMING_MET,
        golden_key="blkE", golden_root=root, view="layout",
        frame_source=_boom)                 # 取像抛错 → 收敛 uncertain，不 500、不假绿
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"
    # 比对里任何异常都记 visual_error 供审计；结论仍不放过。
    assert r.get("visual_error") or r["gate"]["visual_state"] == "uncertain"
    assert r["executes_rollback"] is False


def test_gate_no_visual_params_is_text_only_single_channel():
    # 只给文本、没配任何视觉入参 → 视觉 uncertain（退回单通道），visual_source=="none"。
    r = ewr.gate_online_report(tool="vivado", kind="timing", fetch="inline", content=TIMING_MET)
    assert r["fact"]["metrics"]["timing_met"] is True     # 结构化绿
    assert r["visual_source"] == "none"
    assert r["gate"]["visual_state"] == "uncertain"        # 但没视觉证据 → 不静默放行
    assert r["gate"]["merged"]["state"] == "uncertain"
    assert r["gate"]["decision"]["action"] == "escalate"
    assert r["executes_rollback"] is False


# ---- /api/eda/report-gate 路由装配 ----

def test_route_report_gate_inline_met(client):
    r = client.post("/api/eda/report-gate", json={
        "tool": "vivado", "kind": "timing", "fetch": "inline",
        "content": TIMING_MET, "visual": {"verdict": "pass"}})
    assert r.status_code == 200
    body = r.json()
    assert body["fact"]["ok"] is True
    assert body["gate"]["merged"]["state"] == "met"
    assert body["executes_rollback"] is False


def test_route_report_gate_non_report_escalates(client):
    r = client.post("/api/eda/report-gate", json={
        "tool": "vivado", "kind": "timing", "fetch": "inline",
        "content": EDA_NEWS_BODY, "visual": {"verdict": "pass"}})
    body = r.json()
    assert body["fact"]["ok"] is False
    assert body["gate"]["decision"]["action"] == "escalate"


def test_route_report_gate_missing_text_fail_closed(client):
    r = client.post("/api/eda/report-gate", json={
        "tool": "vivado", "kind": "timing", "fetch": "inline"})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "no_report_text"
    assert body["fact"] is None
    assert body["gate"]["decision"]["action"] == "escalate"


def test_route_report_gate_unknown_tool_kind_fail_closed(client):
    # 未知 (tool,kind) → M3 fail-closed；链路仍返回合法结构、只判不动手。
    r = client.post("/api/eda/report-gate", json={
        "tool": "quartus", "kind": "power", "fetch": "inline", "content": "x"})
    body = r.json()
    assert body["fact"]["ok"] is False
    assert "unsupported_parser" in (body["fact"].get("error") or "")
    assert body["executes_rollback"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
