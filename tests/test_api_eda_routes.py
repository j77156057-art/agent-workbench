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


@pytest.fixture
def client_with_root(tmp_path):
    """带项目根的 ctx：/api/eda/run 需要它（工作目录锁在项目根）。"""
    class _Ctx:
        def _project_root_or_error(self):
            return str(tmp_path)

    app = FastAPI()
    app.include_router(eda_routes.build_router(_Ctx()))
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


# ---- run（2026-10-20 新增：真跑 EDA 脚本的唯一出口）----

def test_run_requires_explicit_confirm(client):
    # 没有 confirm 一律拒（HTTP 400），并且**不执行**任何东西。
    r = client.post("/api/eda/run", json={
        "tool": "vivado", "kind": "timing", "script": "report_timing_summary",
    })
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "确认" in (body.get("error") or "")


def test_run_rejects_unregistered_tool(client):
    # 执行面只认登记在册的三个二进制：不是任意 shell。
    r = client.post("/api/eda/run", json={
        "tool": "rm", "kind": "timing", "script": "-rf /", "confirm": True,
    })
    assert r.status_code == 400
    assert "vivado/klayout/verdi" in (r.json().get("error") or "")


def test_run_rejects_empty_script(client):
    r = client.post("/api/eda/run", json={
        "tool": "vivado", "kind": "timing", "script": "   ", "confirm": True,
    })
    assert r.status_code == 400


def test_run_fails_closed_when_tool_missing(client_with_root):
    # 工具不可用 → fail-closed：ok=false + tool_unavailable，**绝不**返回空 stdout
    # 让上层误当成"报告显示通过"。
    r = client_with_root.post("/api/eda/run", json={
        "tool": "vivado", "kind": "timing", "script": "report_timing_summary",
        "confirm": True, "timeout": 20,
    })
    body = r.json()
    assert body["ok"] is False
    assert "tool_unavailable" in (body.get("error") or "") or "run_failed" in (body.get("error") or "")


def test_run_requires_project_root(client):
    # 没有代码库 → 明确拒绝，不执行。
    r = client.post("/api/eda/run", json={
        "tool": "vivado", "kind": "timing", "script": "x", "confirm": True,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "代码库" in (r.json().get("error") or "")


def test_skills_now_returns_real_sections(client):
    # SKILL.md 已入库（.trae/skills/eda-adapters/SKILL.md），按工具切段 + Shared workflow。
    r = client.get("/api/eda/skills?tool=klayout")
    body = r.json()
    assert body["ok"] is True
    assert "## KLayout" in body["skill"]
    assert "## Shared workflow" in body["skill"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
