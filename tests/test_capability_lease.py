import time

from agent import Agent
from agent_runtime.game_workflow import GameWorkflowManager


class _LLM:
    provider = "mock"
    model = "mock"


def _tool(value):
    return lambda _arg: value


def test_agent_lease_allows_declared_capability_and_blocks_other_tools():
    agent = Agent(
        llm=_LLM(),
        tool_registry={
            "read_file": {"description": "read", "func": _tool("read ok")},
            "web_search": {"description": "web", "func": _tool("web ok")},
        },
        capability_lease={
            "capabilities": ["read_local"],
            "expires_at_epoch": time.time() + 60,
        },
    )
    assert agent._lease_blocked("read_file")[0] is False
    blocked, reason = agent._lease_blocked("web_search")
    assert blocked is True
    assert "network" in reason
    result = agent._run_batch([("web_search", "q")], None)[0]
    assert result[3] is False
    assert "权限租约" in result[2]
    assert result[5] == "lease_blocked"


def test_expired_lease_blocks_even_when_capability_is_declared():
    agent = Agent(
        llm=_LLM(),
        tool_registry={"read_file": {"description": "read", "func": _tool("ok")}},
        capability_lease={"capabilities": ["read_local"], "expires_at_epoch": time.time() - 1},
    )
    blocked, reason = agent._lease_blocked("read_file")
    assert blocked is True
    assert "过期" in reason


def test_released_or_incomplete_lease_cannot_execute_tools():
    agent = Agent(
        llm=_LLM(),
        tool_registry={"read_file": {"description": "read", "func": _tool("ok")}},
        capability_lease={"status": "released", "capabilities": ["read_local"],
                          "expires_at_epoch": time.time() + 60},
    )
    assert agent._lease_blocked("read_file")[0] is True
    agent.capability_lease = {"status": "invalid"}
    assert agent._lease_blocked("read_file")[0] is True
    agent.capability_lease = {"status": "active", "expires_at_epoch": time.time() + 60}
    assert agent._lease_blocked("read_file")[0] is True


def test_workflow_capability_lease_persists_and_releases_on_terminal_state(tmp_path):
    manager = GameWorkflowManager(str(tmp_path))
    workflow_id = manager.start("临时工具权限测试")['workflow_id']
    lease = manager.create_capability_lease(
        workflow_id, ttl_seconds=300, capabilities=["read_local", "network"])
    assert lease["status"] == "active"
    assert lease["capabilities"] == ["read_local", "network"]
    reloaded = GameWorkflowManager(str(tmp_path)).get(workflow_id)
    assert reloaded["capability_lease"]["id"] == lease["id"]
    assert reloaded["capability_lease"]["status"] == "active"
    interrupted = manager.interrupt(workflow_id, "测试结束")
    assert interrupted["capability_lease"]["status"] == "released"
    assert "released_at" in interrupted["capability_lease"]


# --------------------------------------------------------------------------
# _UNSET sentinel：显式空 capabilities 不得被扩成全能力
# --------------------------------------------------------------------------


def _make_manager(tmp_path):
    from agent_runtime.game_workflow import GameWorkflowManager
    return GameWorkflowManager(state_root=str(tmp_path))


def test_capability_lease_sentinel_distinguishes_omitted_from_empty(tmp_path):
    """`_ensure_capability_lease` 必须区分「未传」与「显式空集合」。

    之前 `capabilities=[]` 会被 `capabilities or ()` 折叠到默认全能力；
    调用方传空集合本来想表达「零权限」，却被扩成全能力。
    """
    from agent_runtime.game_workflow import _UNSET, WorkflowState

    mgr = _make_manager(tmp_path)
    state = WorkflowState(workflow_id="wf-sentinel")
    state.status = "planning"

    # 未传 → 默认全能力
    lease = mgr._ensure_capability_lease(state, capabilities=_UNSET)
    assert set(lease["capabilities"]) >= {"read_local", "write_local", "network", "admin"}

    # 显式空列表 → 零权限（**不能**扩成全能力）
    state2 = WorkflowState(workflow_id="wf-empty")
    state2.status = "planning"
    lease = mgr._ensure_capability_lease(state2, capabilities=[])
    assert lease["capabilities"] == []

    # 显式空集合 → 同样是零权限
    state3 = WorkflowState(workflow_id="wf-empty-set")
    state3.status = "planning"
    lease = mgr._ensure_capability_lease(state3, capabilities=set())
    assert lease["capabilities"] == []


def test_create_capability_lease_omitted_vs_empty(tmp_path):
    """`create_capability_lease` 公共入口同样区分。"""
    from agent_runtime.game_workflow import GameWorkflowManager
    mgr = GameWorkflowManager(state_root=str(tmp_path))
    wid_a = mgr.start("测试A", kind="generic")["workflow_id"]
    wid_b = mgr.start("测试B", kind="generic")["workflow_id"]
    # 未传 capabilities → 默认全能力
    lease = mgr.create_capability_lease(wid_a)
    assert set(lease["capabilities"]) >= {"read_local", "write_local"}
    # 显式空列表 → 零权限（不同 workflow，避免已有 lease 缓存干扰）
    lease_empty = mgr.create_capability_lease(wid_b, capabilities=[])
    assert lease_empty["capabilities"] == []
