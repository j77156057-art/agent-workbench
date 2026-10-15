"""在线 EDA 报告页 → M3 解析 → M6 门 的取页胶水链路测试（**离线，无网络**）。

覆盖两层：
1) `agent_runtime.eda_web_report` 单元：取页源归一（inline / 注入 / web_fetch 失败规整 None）、
   gate_online_report 的三态与 fail-closed（取不到正文不喂解析器、非报告正文由 M3 判 ok:false）。
2) `/api/eda/report-gate` 路由：经 TestClient + stub ctx 验整链装配，重点断言**只判不执行回滚**
   （executes_rollback 恒 False），以及浏览器抓回的正文走 inline 这条路能打通 M3→M6。

复用 tests/test_eda_tcl.py 里的真实报告样本。web_fetch 后端一律 monkeypatch 掉 tools._fetch_page，
保证测试不触网。
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_runtime.eda_web_report as ewr  # noqa: E402
import api_routes.eda as eda_routes  # noqa: E402

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
