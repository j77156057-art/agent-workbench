"""model 工具 `eda_report_gate` 的注册与行为守卫（离线，注入正文，不触网）。

覆盖：
- 注册完整性：进 TOOLS、带 input_schema(tool/kind required)、被归类为 NETWORK（受联网开关约束）、
  声明了 domain/facet（过 test_tool_domains 的完整性守卫）。
- 行为：inline 喂真报告文本 → met/accept 且带 provenance；非报告正文 → fact_ok=false → uncertain/escalate；
  缺 tool/kind 或缺 url+content → needs 提示；取页失败 → fetch_failed 且诚实判不确定。
- 安全：任何情况 executes_rollback 恒 false；正常(含 uncertain)输出不含失败标记串，
  免得被 runner 误判成"工具崩溃"触发无限重试。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
from agent_runtime.tools import coerce_tool_spec, Capability  # noqa: E402
from agent_runtime import tool_domains as D  # noqa: E402

TIMING_MET = """
Design Timing Summary
---------------------
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
      0.412        0.000                      0                52314        0.035        0.000
Clock  Waveform (ns)         Period(ns)      Frequency(MHz)
sys_clk  {0.000 2.500}        5.000           200.000
"""

EDA_NEWS = ("来源：https://x/eda-news 标题：华大九天参股芯行纪补齐布局布线核心 EDA 工具 "
            "正文：某厂商宣布战略投资，加速构建数字电路全流程工具系统……（无 report_timing_summary）")

# agent.py 里判定"工具失败"的标记串（本工具的正常/不确定输出都不该命中，否则会被当崩溃重试）。
FAILURE_MARKERS = (
    "计算失败", "搜索失败", "搜索未返回结果", "交通票价查询失败", "天气查询失败",
    "字幕提取失败", "读取失败", "文件不存在", "拒绝访问", "未提供", "安全限制",
    "拒绝写入", "参数缺失",
)


def _no_marker(text):
    return not any(m in text for m in FAILURE_MARKERS)


# ---- 注册完整性 ----

def test_tool_is_registered_with_schema():
    entry = tools.TOOLS["eda_report_gate"]
    assert callable(entry["func"])
    schema = entry["input_schema"]
    assert set(schema["required"]) == {"tool", "kind"}
    assert {"tool", "kind", "url", "content", "fetch"} <= set(schema["properties"])


def test_tool_classified_as_network():
    spec = coerce_tool_spec("eda_report_gate", tools.TOOLS["eda_report_gate"])
    # 归为 NETWORK → 与 web_fetch 同受联网开关(_web_blocked)约束、走网络超时钩子。
    assert spec.capability == Capability.NETWORK
    assert spec.side_effect.value != "irreversible"


def test_tool_declares_a_domain():
    assert D.TOOL_DOMAINS.get("eda_report_gate"), "eda_report_gate 未声明 domain/facet"


# ---- 行为 ----

def test_inline_report_met_accept():
    out = json.loads(tools.eda_report_gate(json.dumps({
        "tool": "vivado", "kind": "timing", "fetch": "inline",
        "content": TIMING_MET, "visual": {"verdict": "pass"}})))
    assert out["structured"]["fact_ok"] is True
    assert out["structured"]["metrics"]["timing_met"] is True
    assert out["structured"]["provenance_count"] > 0
    assert out["gate"]["state"] == "met"
    assert out["gate"]["decision"] == "accept"
    assert out["executes_rollback"] is False
    assert _no_marker(json.dumps(out, ensure_ascii=False))


def test_inline_non_report_escalates():
    out = json.loads(tools.eda_report_gate(json.dumps({
        "tool": "vivado", "kind": "timing", "fetch": "inline",
        "content": EDA_NEWS, "visual": {"verdict": "pass"}})))
    assert out["structured"]["fact_ok"] is False
    assert out["structured"]["error"] == "timing_metrics_not_found"
    assert out["gate"]["state"] == "uncertain"
    assert out["gate"]["decision"] == "escalate"
    assert _no_marker(json.dumps(out, ensure_ascii=False))


def test_visual_regression_makes_unmet_but_no_rollback():
    out = json.loads(tools.eda_report_gate(json.dumps({
        "tool": "vivado", "kind": "timing", "fetch": "inline",
        "content": TIMING_MET, "visual": {"verdict": "regression"}})))
    assert out["gate"]["state"] == "unmet"
    assert out["gate"]["decision"] == "rollback"   # 仅建议
    assert out["executes_rollback"] is False


def test_missing_tool_kind_returns_needs():
    out = json.loads(tools.eda_report_gate(json.dumps({"url": "http://x"})))
    assert out["ok"] is False
    assert set(out["needs"]) == {"tool", "kind"}


def test_missing_source_returns_needs():
    out = json.loads(tools.eda_report_gate(json.dumps({"tool": "vivado", "kind": "timing"})))
    assert out["ok"] is False
    assert "url 或 content" in out["needs"][0]


def test_fetch_failure_is_honest_uncertain(monkeypatch):
    # 走 web_fetch 后端但抓取失败 → fetch_failed 且判不确定，绝不假绿。
    monkeypatch.setattr(tools, "_fetch_page",
                        lambda url, *, image_budget=0, query_hint="": "网页读取失败：URLError")
    out = json.loads(tools.eda_report_gate(json.dumps({
        "tool": "vivado", "kind": "timing", "url": "http://unreachable",
        "fetch": "web_fetch", "visual": {"verdict": "pass"}})))
    assert out["fetch_failed"] is True
    assert out["structured"] is None
    assert out["gate"]["state"] == "uncertain"
    assert out["gate"]["decision"] == "escalate"
    # 取页失败仍是"合法结论"，不该被当工具崩溃 → 不含失败标记串
    assert _no_marker(json.dumps(out, ensure_ascii=False))


def test_func_returns_json_string():
    raw = tools.eda_report_gate(json.dumps({"tool": "vivado", "kind": "timing",
                                            "fetch": "inline", "content": TIMING_MET}))
    assert isinstance(raw, str)
    json.loads(raw)  # 必须是合法 JSON 串（execute_tool 要求 str）


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
