"""自检沙箱租约的边界测试（离线，不碰真桌面）。

这些测试只验证"该被拦的一定被拦"：窗口范围、系统键、过期、换项目、
伪造边界摘要，以及工作流结束时的收回。真正的点击投递由 desktop_actions
的现有测试与端到端验收覆盖。
"""
import json
import os
import time

import game_workbench
import tools


def _project(tmp_path, name="game"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


# --------------------------------------------------------------------------- 边界

def test_scope_covers_only_the_projects_own_window_and_no_system_keys(tmp_path):
    root = _project(tmp_path)
    lease = game_workbench.grant_playtest_sandbox(root, "tester", ttl_seconds=300)
    status = game_workbench.playtest_sandbox_status(root)
    assert status["active"] is True
    assert status["lease"]["id"] == lease["id"]
    assert status["scope"]["targets"] == ["embedded", "engine"]
    assert status["scope"]["denied_targets"] == ["foreground"]

    for target in ("embedded", "engine"):
        allowed, _reason, _lease = game_workbench.playtest_sandbox_desktop_allowed(
            root, action="click", target=target)
        assert allowed is True
    allowed, reason, _lease = game_workbench.playtest_sandbox_desktop_allowed(
        root, action="click", target="foreground")
    assert allowed is False and "foreground" in reason
    allowed, reason, _lease = game_workbench.playtest_sandbox_desktop_allowed(
        root, action="key", target="engine", key="Win+r")
    assert allowed is False and "系统键" in reason
    allowed, reason, _lease = game_workbench.playtest_sandbox_desktop_allowed(
        root, action="exec", target="engine")
    assert allowed is False


def test_expired_lease_is_inactive_and_blocks(tmp_path):
    root = _project(tmp_path)
    lease = game_workbench.grant_playtest_sandbox(root, "tester", ttl_seconds=60)
    lease["expires_at_epoch"] = time.time() - 1
    game_workbench._write_playtest_sandbox(root, lease)
    status = game_workbench.playtest_sandbox_status(root)
    assert status["active"] is False
    assert "过期" in status["reason"]
    allowed, _reason, _lease = game_workbench.playtest_sandbox_desktop_allowed(
        root, action="click", target="engine")
    assert allowed is False


def test_lease_is_bound_to_its_project_and_revocable(tmp_path):
    root = _project(tmp_path)
    other = _project(tmp_path, "other")
    game_workbench.grant_playtest_sandbox(root, "tester")
    assert game_workbench.playtest_sandbox_status(other)["active"] is False
    game_workbench.revoke_playtest_sandbox(root, "done")
    status = game_workbench.playtest_sandbox_status(root)
    assert status["active"] is False
    assert "收回" in status["reason"]


def test_scope_change_invalidates_an_old_lease(tmp_path):
    root = _project(tmp_path)
    lease = game_workbench.grant_playtest_sandbox(root, "tester")
    lease["scope_digest"] = "deadbeefdeadbeef"
    game_workbench._write_playtest_sandbox(root, lease)
    status = game_workbench.playtest_sandbox_status(root)
    assert status["active"] is False
    assert "边界已变化" in status["reason"]


# --------------------------------------------------------------------------- 工具闸

_INFO = {"origin": {"x": 100, "y": 100}, "rect": {"width": 200, "height": 100}}


def _drive_desktop(monkeypatch, root, *, info=None):
    """把桌面层换成假窗口 + 假 SendInput，走**真实的** `perform` 主体。

    直接假造 `perform` 会把地板一起假造掉：坐标越界、危险键名这些判定都在
    `perform` 里面，假造了就什么都没验。这里只替换两个真 syscall 入口。
    """
    import desktop_actions
    resolved = []
    sent = []

    def fake_resolve(target, project_id=None, proj_root=None):
        resolved.append({"target": target, "root": proj_root})
        return 4242, (info or _INFO), ""

    monkeypatch.setattr(desktop_actions, "_resolve_target", fake_resolve)
    monkeypatch.setattr(desktop_actions, "_focus", lambda *a, **k: (True, ""))
    monkeypatch.setattr(desktop_actions, "_send",
                        lambda events: sent.append(len(events)) or (True, len(events)))
    monkeypatch.setattr(desktop_actions, "_user32",
                        lambda: type("_U", (), {"SetCursorPos": lambda *a: True})())
    return resolved, sent


def test_dev_desktop_action_executes_directly_but_only_on_its_own_window(monkeypatch, tmp_path):
    """审批门拆了，地板还在：绑当前项目根、只点客户区内、系统键不投递。

    61082a7「留地板、拆其余」把 `dev_desktop_action` 的逐次审批门恒置 None，
    于是那段"门没过时再看一次自检沙箱租约"的兜底分支成了死代码 —— 旧用例断言
    的 `result["sandbox"]` / `blocked` 来自那条分支，现在永远不会出现。沙箱租约
    本身仍然有效且仍在别处生效（`agent_runtime/visual_click.py`、`game_workbench`
    的边界判定，本文件上面那几条仍在验），这里只补齐**工具入口**这层的地板断言。
    """
    root = _project(tmp_path)
    game_workbench.grant_playtest_sandbox(root, "tester")
    monkeypatch.setattr(tools, "_get_code_root", lambda: root)
    resolved, sent = _drive_desktop(monkeypatch, root)

    ok = json.loads(tools.dev_desktop_action(
        "action: click\ntarget: engine\nx: 10\ny: 20"))
    assert ok["ok"] is True, "当场指定的桌面动作不再需要逐次审批"
    assert resolved[0]["root"] == root, "引擎窗口必须按当前项目根解析"
    assert sent == [2]

    # 客户区外的坐标：真点下去会打到别的应用，必须在投递前拦下。
    before = len(sent)
    out = json.loads(tools.dev_desktop_action(
        "action: click\ntarget: engine\nx: 999\ny: 999"))
    assert out["ok"] is False and len(sent) == before, "客户区外坐标不应投递"

    # Win+r 这类系统组合键：_VK 白名单不认，一个事件都不该发出去。
    combo = json.loads(tools.dev_desktop_action(
        "action: key\ntarget: engine\nkey: Win+r"))
    assert combo["ok"] is False and len(sent) == before, "白名单外的键名不应投递"


def test_dev_desktop_action_foreground_resolves_without_leaking_a_root(monkeypatch, tmp_path):
    """foreground 也直接执行（旧逐次审批门已拆），但它不能借到项目根的窗口。"""
    root = _project(tmp_path)
    monkeypatch.setattr(tools, "_get_code_root", lambda: root)
    resolved, sent = _drive_desktop(monkeypatch, root)

    result = json.loads(tools.dev_desktop_action(
        "action: click\ntarget: foreground\nx: 5\ny: 5"))
    assert result["ok"] is True
    assert [item["target"] for item in resolved] == ["foreground"]
    assert len(sent) == 1


# --------------------------------------------------------------------------- 工作流

def _plan_a_game(manager, project_root, *, kind="game"):
    started = manager.start("做一个能跑的小游戏", kind=kind,
                            project_root=project_root)
    workflow_id = started["workflow_id"]
    manager.choose(workflow_id, "recommended")
    manager.plan(workflow_id, [{"id": "a", "role": "tester",
                                "task": "运行游戏并检查引擎日志"}])
    waiting = manager.execute(workflow_id, lambda *_: {
        "status": "ok", "conclusion": "通过", "steps": 1})
    return workflow_id, waiting


def test_game_plan_approval_grants_one_boundary_and_interrupt_releases_it(tmp_path):
    from agent_runtime.game_workflow import GameWorkflowManager
    root = _project(tmp_path)
    manager = GameWorkflowManager(str(tmp_path / "state"))
    workflow_id, waiting = _plan_a_game(manager, root)
    assert "自检沙箱" in waiting["interrupt_reason"]

    approved = manager.approve(workflow_id, True)
    lease = approved.get("sandbox_lease") or {}
    assert lease.get("status") == "active"
    status = game_workbench.playtest_sandbox_status(root)
    assert status["active"] is True
    assert status["lease"]["id"] == lease["id"]

    interrupted = manager.interrupt(workflow_id, "测试结束")
    assert interrupted["sandbox_lease"]["status"] == "released"
    assert game_workbench.playtest_sandbox_status(root)["active"] is False


def test_generic_workflow_does_not_grant_a_sandbox(tmp_path):
    from agent_runtime.game_workflow import GameWorkflowManager
    root = _project(tmp_path)
    manager = GameWorkflowManager(str(tmp_path / "state"))
    workflow_id, waiting = _plan_a_game(manager, root, kind="generic")
    assert "自检沙箱" not in waiting["interrupt_reason"]
    approved = manager.approve(workflow_id, True)
    assert not approved.get("sandbox_lease")
    assert game_workbench.playtest_sandbox_status(root)["active"] is False


# --------------------------------------------------------------------------- 视觉点击

def test_visual_click_rides_the_sandbox_then_falls_back_after_expiry(monkeypatch, tmp_path):
    from agent_runtime import visual_click
    import desktop_actions
    import screen_capture

    root = _project(tmp_path)
    raw = bytes([128, 128, 128, 255]) * (40 * 30)
    monkeypatch.setattr(screen_capture, "grab_embedded",
                        lambda pid, strict_project=True: (raw, 40, 30, 1234))
    monkeypatch.setattr(screen_capture, "encode_frame", lambda *args: "ZmFrZQ==")

    class _LLM:
        def chat(self, *args, **kwargs):
            return json.dumps({"found": True, "label": "开始按钮",
                               "evidence": "画面中央的按钮",
                               "bbox": [0.4, 0.4, 0.6, 0.6], "confidence": 0.9})

    monkeypatch.setattr(visual_click, "resolve_vision_llm",
                        lambda pid: (_LLM(), "harness", ""))
    monkeypatch.setattr(game_workbench, "require_approval",
                        lambda *args: {"blocked": True, "approval_required": True,
                                       "action": "desktop_action", "target": "*"})
    monkeypatch.setattr(game_workbench, "approval",
                        lambda *args, **kwargs: {"approved": True})
    clicked = []
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *args, **kwargs: clicked.append(1) or {"ok": True})

    visual_click.PROPOSALS.clear()
    try:
        located = visual_click.locate("prj-x", description="开始按钮", root=root)
        assert located["ok"] is True
        game_workbench.grant_playtest_sandbox(root, "tester")

        result = visual_click.execute("prj-x", root,
                                      proposal_id=located["proposal_id"], gate=True)
        assert result["ok"] is True and result["executed"] is True
        assert result["sandbox"]["lease_id"]
        assert len(clicked) == 1

        lease = game_workbench._read_playtest_sandbox(root)
        lease["expires_at_epoch"] = time.time() - 1
        game_workbench._write_playtest_sandbox(root, lease)
        visual_click.PROPOSALS.clear()
        located = visual_click.locate("prj-x", description="开始按钮", root=root)
        result = visual_click.execute("prj-x", root,
                                      proposal_id=located["proposal_id"], gate=True)
        assert result.get("approval_required") is True
        assert result["sandbox"]["active"] is False
        assert len(clicked) == 1, "租约过期后不能再点"
    finally:
        visual_click.PROPOSALS.clear()


# --------------------------------------------------------------------------- 试做区

def test_stage_scope_allows_engine_and_window_tools(tmp_path):
    """默认开启试做区时，自检需要的引擎/窗口工具不能被白名单一刀切拦掉。"""
    from agent_runtime.tools import (SideEffect, execute_tool,
                                     project_stage_scope, stage_origin_root,
                                     stage_scope_active)

    origin = _project(tmp_path, "origin")
    seen = []

    def fake(_arg):
        seen.append(stage_origin_root())
        return "ok"

    with project_stage_scope(origin_root=origin):
        for name in ("engine_start", "engine_stop", "engine_reload", "engine_logs",
                     "dev_desktop_action", "vision_locate_click", "vision_click"):
            result = execute_tool(fake, "", lambda _text: False,
                                  tool_name=name, side_effect=SideEffect.PURE)
            assert result.ok, (name, result.text)
        blocked = execute_tool(fake, "", lambda _text: False,
                               tool_name="start_workflow",
                               side_effect=SideEffect.PURE)
        assert blocked.ok is False
        assert blocked.error_kind == "project_stage_blocked"

    assert seen and all(item == origin for item in seen)
    assert stage_scope_active() is False
    assert stage_origin_root() == "", "离开作用域后 origin 必须清空，不能漏给下一个任务"


def test_stage_dev_desktop_action_resolves_the_stage_window(monkeypatch, tmp_path):
    """试做区里窗口按副本解析；租约/台账仍记在真实项目名下，不落到 stage 目录。

    审批门拆掉后 `dev_desktop_action` 这层已经不查租约了（原来那两条断言
    `result["sandbox"]` / `gate_roots == [origin]` 测的是死代码），所以这里只保留
    仍然成立的两件事：窗口解析落在 stage 副本、stage 目录里不长出租约文件。
    「租约按 origin 查」这条由上面的边界用例与 `agent_runtime/visual_click.py`
    那条仍在生效的入口负责。
    """
    from agent_runtime.tools import project_stage_scope

    origin = _project(tmp_path, "origin")
    stage = _project(tmp_path, "stage")
    game_workbench.grant_playtest_sandbox(origin, "tester")
    monkeypatch.setattr(tools, "_get_code_root", lambda: stage)
    resolved, _sent = _drive_desktop(monkeypatch, stage)

    with project_stage_scope(origin_root=origin):
        result = json.loads(tools.dev_desktop_action(
            "action: click\ntarget: engine\nx: 10\ny: 20"))

    assert result["ok"] is True
    assert resolved and resolved[-1]["root"] == stage, "窗口解析必须留在试做区副本"
    assert not os.path.exists(game_workbench.playtest_sandbox_path(stage)), \
        "试做区目录下不应出现租约副本"


def test_stage_visual_click_checks_origin_ledger(monkeypatch, tmp_path):
    """视觉点击在试做区里同样要按真实项目查审批/租约，否则永远被判未批准。"""
    from agent_runtime import visual_click
    from agent_runtime.tools import project_stage_scope

    origin = _project(tmp_path, "origin")
    stage = _project(tmp_path, "stage")
    visual_click.PROPOSALS.clear()
    try:
        visual_click.PROPOSALS["proposal-1"] = {
            "project_id": "prj-x", "used": False, "created_at": time.time(),
            "point": {"x": 10, "y": 20}, "bbox": [0.1, 0.1, 0.2, 0.2],
            "capture_target": "embedded", "hwnd": 1234, "label": "开始按钮",
            "fingerprint": "deadbeef",
        }
        lookups = []
        monkeypatch.setattr(
            game_workbench, "require_approval",
            lambda root, *args: lookups.append(("gate", root)) or {
                "blocked": True, "approval_required": True})
        monkeypatch.setattr(
            game_workbench, "playtest_sandbox_desktop_allowed",
            lambda root, **kwargs: lookups.append(("lease", root)) or (
                False, "尚未开启自检沙箱。", {}))

        with project_stage_scope(origin_root=origin):
            result = visual_click.execute("prj-x", stage,
                                          proposal_id="proposal-1", gate=True)

        assert result.get("approval_required") is True
        assert lookups == [("gate", origin), ("lease", origin)]
    finally:
        visual_click.PROPOSALS.clear()


def test_workflow_stage_runner_receives_origin_root(tmp_path):
    """工作流真的把真实项目根带进试做区作用域，而不是只改了函数签名。"""
    from agent_runtime.game_workflow import GameWorkflowManager
    from agent_runtime.tools import stage_origin_root, stage_scope_active

    root = _project(tmp_path)
    manager = GameWorkflowManager(str(tmp_path / "state"))
    started = manager.start("做一个能跑的小游戏", kind="game",
                            project_root=root, stage_enabled=True)
    workflow_id = started["workflow_id"]
    manager.choose(workflow_id, "recommended")
    manager.plan(workflow_id, [{"id": "a", "role": "tester",
                                "task": "运行游戏并检查引擎日志"}])
    seen = {}

    def runner(_task, _context):
        seen["active"] = stage_scope_active()
        seen["origin"] = stage_origin_root()
        return {"status": "ok", "conclusion": "通过", "steps": 1}

    waiting = manager.execute(workflow_id, runner)
    assert waiting["status"] == "awaiting_approval"
    manager.approve(workflow_id, True)
    # 批准后状态回到 planned，再执行一次才是真正开跑（同步）。
    result = manager.execute(workflow_id, runner)
    assert result.get("status") in {"completed", "failed", "reviewing"}
    assert seen.get("active") is True, "runner 没有进入试做区作用域"
    assert seen.get("origin") == root

