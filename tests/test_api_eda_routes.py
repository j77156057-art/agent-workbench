"""EDA 验证路由离线测试：/api/eda/parse-report / /skills / /verify。

这三条路由只用 agent_runtime.* 零件，不依赖真实 project root，故用 stub ctx 挂到
一个最小 FastAPI app 上、经 TestClient 验。（真实 api.py 传 sys.modules['api'] 作 ctx。）
重点断言：**verify 只判不执行回滚**（executes_rollback 恒 False）。
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_routes.eda as eda_routes  # noqa: E402


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(eda_routes.build_router(object()))
    return TestClient(app)


def _fact(ok=True, **metrics):
    return {"ok": ok, "tool": "vivado", "kind": "timing",
            "metrics": metrics, "missing": [], "provenance": [], "error": None}


# ---- parse-report ----

def test_parse_report_unknown_combo_fail_closed(client):
    r = client.post("/api/eda/parse-report",
                    json={"tool": "nope", "kind": "nope", "text": "junk"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "unsupported_parser" in (body.get("error") or "")


def test_parse_report_known_combo_returns_edafact_shape(client):
    # 已知组合 + 任意文本 → 解析器 fail-closed，但仍是合法的 EdaFact.to_dict 形状。
    r = client.post("/api/eda/parse-report",
                    json={"tool": "vivado", "kind": "timing", "text": "no metrics here"})
    body = r.json()
    for k in ("ok", "tool", "kind", "metrics", "missing", "provenance", "error"):
        assert k in body


# ---- skills ----

def test_skills_endpoint_degrades_gracefully(client):
    # .trae/ gitignore，SKILL.md 可能不在 → 优雅降级空串，但仍 ok。
    r = client.get("/api/eda/skills?tool=vivado")
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["skill"], str)


# ---- verify（合并门，只判不动手）----

def test_verify_all_green_accept(client):
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=True),
        "visual": {"verdict": "pass"},
    })
    body = r.json()
    assert body["merged"]["state"] == "met"
    assert body["decision"]["action"] == "accept"
    assert body["executes_rollback"] is False


def test_verify_structured_veto_rollback_advised(client):
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=False),
        "visual": {"verdict": "identical"},
    })
    body = r.json()
    assert body["merged"]["state"] == "unmet"
    assert body["decision"]["action"] == "rollback"
    # 关键安全断言：即便建议 rollback，本接口也不执行。
    assert body["executes_rollback"] is False


def test_verify_visual_regression_unmet(client):
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=True),
        "visual": {"verdict": "regression"},
    })
    assert r.json()["merged"]["state"] == "unmet"


def test_verify_localized_uncertain_escalate(client):
    # VLM 未核实（不传 vlm_verified）→ localized → UNCERTAIN → escalate
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=True),
        "visual": {"verdict": "localized"},
    })
    body = r.json()
    assert body["merged"]["state"] == "uncertain"
    assert body["decision"]["action"] == "escalate"
    assert body["executes_rollback"] is False


def test_verify_localized_upgraded_when_vlm_verified(client):
    # 唯一提级通道：显式 vlm_verified=true 才把 localized 抬到 MET（结构化也绿）。
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=True),
        "visual": {"verdict": "localized"},
        "vlm_verified": True,
    })
    assert r.json()["merged"]["state"] == "met"


def test_verify_missing_visual_is_uncertain(client):
    r = client.post("/api/eda/verify", json={
        "structured": _fact(timing_met=True),
    })
    assert r.json()["merged"]["state"] == "uncertain"


def test_verify_multi_fact_folding(client):
    # 结构化给 fact 列表：任一 UNMET → 整体 UNMET。
    r = client.post("/api/eda/verify", json={
        "structured": [_fact(timing_met=True), _fact(timing_met=True, drc_clean=False)],
        "visual": {"verdict": "pass"},
    })
    body = r.json()
    assert body["structured_state"] == "unmet"
    assert body["merged"]["state"] == "unmet"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
