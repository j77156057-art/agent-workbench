"""视觉定位点击的护栏测试（离线：不真跑视觉模型、不真点屏幕）。

这些是**安全边界**，测的不是"能不能点"（那要真桌面 + 真模型），而是
"该被拦的**一定**被拦"。护栏的失效模式从来不是"拦错了"，而是"漏放了一个"。
"""
import json

import pytest

from agent_runtime import visual_click


WIDTH, HEIGHT = 40, 30
RAW = bytes([128, 128, 128, 255]) * (WIDTH * HEIGHT)


class _FakeScreen:
    """替身：抓帧与编码都不碰真桌面。"""

    def __init__(self, monkeypatch, *, hwnd=1234, raw=None):
        import screen_capture
        self.raw = raw if raw is not None else RAW
        self.frames = []

        def grab(pid, strict_project=True):
            self.frames.append((pid, strict_project))
            return (self.raw, WIDTH, HEIGHT, hwnd)

        monkeypatch.setattr(screen_capture, "grab_embedded", grab)
        monkeypatch.setattr(screen_capture, "encode_frame",
                            lambda raw, width, height: "ZmFrZS1pbWFnZQ==")


class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.answer


def _target_json(label="保存按钮", confidence=0.9):
    return json.dumps({"found": True, "label": label, "evidence": "右上角蓝色按钮",
                       "bbox": [0.5, 0.2, 0.7, 0.4], "confidence": confidence})


@pytest.fixture(autouse=True)
def _clean_proposals():
    visual_click.PROPOSALS.clear()
    yield
    visual_click.PROPOSALS.clear()


# --------------------------------------------------------------------------- 定位

def test_locate_without_a_visual_model_says_what_to_configure(monkeypatch):
    """没有视觉模型时要**说清去哪儿配**，不能拿只读文本的模型去猜坐标。"""
    _FakeScreen(monkeypatch)
    monkeypatch.setattr(visual_click, "resolve_vision_llm",
                        lambda pid: (None, "", "当前没有可用的视觉模型。"))
    result = visual_click.locate("prj-x", description="保存按钮")
    assert result["ok"] is False
    assert "视觉模型" in result["error"]


def test_locate_requires_a_description(monkeypatch):
    result = visual_click.locate("prj-x", description="   ")
    assert result["ok"] is False
    assert result["status"] == 400


def test_locate_without_an_embedded_window_says_so(monkeypatch):
    import screen_capture
    monkeypatch.setattr(screen_capture, "grab_embedded", lambda pid, strict_project=True: None)
    result = visual_click.locate("prj-x", description="保存按钮")
    assert result["ok"] is False
    assert "嵌入窗口" in result["error"]


def test_locate_registers_a_one_time_proposal(monkeypatch):
    """定位成功要产出可执行的提案：坐标、指纹、标签、证据。"""
    _FakeScreen(monkeypatch)
    llm = _FakeLLM(_target_json())
    monkeypatch.setattr(visual_click, "resolve_vision_llm", lambda pid: (llm, "harness", ""))
    result = visual_click.locate("prj-x", description="保存按钮", goal="弹窗消失")
    assert result["ok"] is True
    assert result["proposal_id"] in visual_click.PROPOSALS
    entry = visual_click.PROPOSALS[result["proposal_id"]]
    assert entry["used"] is False
    assert entry["label"] == "保存按钮"
    assert entry["goal"] == "弹窗消失"
    assert entry["fingerprint"]
    assert result["point"]["x"] > 0 and result["point"]["y"] > 0


def test_locate_rejects_an_untrustworthy_model_answer(monkeypatch):
    """把握度不够 / 没有依据 / 坐标非法 —— 一律不产出提案。"""
    _FakeScreen(monkeypatch)
    for bad in ('{"found": false, "label": "x", "evidence": "y", "bbox": [0,0,1,1], "confidence": 0.9}',
                '{"found": true, "label": "x", "evidence": "y", "bbox": [0,0,1,1], "confidence": 0.4}',
                '{"found": true, "label": "", "evidence": "y", "bbox": [0,0,1,1], "confidence": 0.9}',
                'not json at all'):
        monkeypatch.setattr(visual_click, "resolve_vision_llm",
                            lambda pid, a=bad: (_FakeLLM(a), "harness", ""))
        result = visual_click.locate("prj-x", description="保存按钮")
        assert result["ok"] is False, bad
    assert not visual_click.PROPOSALS


# --------------------------------------------------------------------------- 执行

def _prepare(monkeypatch, *, hwnd=1234):
    _FakeScreen(monkeypatch, hwnd=hwnd)
    llm = _FakeLLM(_target_json())
    monkeypatch.setattr(visual_click, "resolve_vision_llm", lambda pid: (llm, "harness", ""))
    located = visual_click.locate("prj-x", description="保存按钮")
    assert located["ok"], located
    return located["proposal_id"]


def test_execute_without_a_proposal_is_not_found(monkeypatch):
    result = visual_click.execute("prj-x", ".", proposal_id="nope")
    assert result["ok"] is False
    assert result["status"] == 404


def test_execute_without_any_confirmation_does_not_click(monkeypatch):
    """既没有 UI 确认、也没走审批门 → 不点。模型的自称确认不算。"""
    proposal_id = _prepare(monkeypatch)
    clicked = []
    import desktop_actions
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *a, **k: clicked.append((a, k)) or {"ok": True})
    result = visual_click.execute("prj-x", ".", proposal_id=proposal_id)
    assert result["ok"] is False
    assert result["status"] == 400
    assert not clicked, "没确认就点了"


def test_execute_gated_by_approval_and_not_clicked_until_approved(monkeypatch, tmp_path):
    """agent 出口：没有审批记录 → 被拦下并进审核队列，且**没有点**。"""
    proposal_id = _prepare(monkeypatch)
    clicked = []
    import desktop_actions
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *a, **k: clicked.append((a, k)) or {"ok": True})
    monkeypatch.setattr(visual_click, "resolve_vision_llm",
                        lambda pid: (_FakeLLM(_target_json()), "harness", ""))

    def _blocked(root, action, target):
        return {"blocked": True, "approval_required": True, "action": action, "target": target,
                "message": "需要审批"}

    import game_workbench
    monkeypatch.setattr(game_workbench, "require_approval", _blocked)
    result = visual_click.execute("prj-x", str(tmp_path), proposal_id=proposal_id, gate=True)
    assert result["ok"] is False
    assert result.get("approval_required") is True
    assert "审核队列" in result.get("hint", "")
    assert not clicked, "没批准就点了"


def test_execute_clicks_once_after_approval(monkeypatch, tmp_path):
    """批准之后：复核通过 → 真点一次；同一个提案再调 → 拒绝（防连点）。"""
    proposal_id = _prepare(monkeypatch)
    clicked = []
    import desktop_actions
    import game_workbench

    def _perform(*args, **kwargs):
        clicked.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(desktop_actions, "perform", _perform)
    monkeypatch.setattr(game_workbench, "require_approval", lambda root, a, t: None)
    monkeypatch.setattr(game_workbench, "approval",
                        lambda root, action, user, approved=False, target="":
                        {"approved": bool(approved), "action": action, "target": target})
    result = visual_click.execute("prj-x", str(tmp_path), proposal_id=proposal_id, gate=True)
    assert result["ok"] is True
    assert result["executed"] is True
    assert len(clicked) == 1
    again = visual_click.execute("prj-x", str(tmp_path), proposal_id=proposal_id, gate=True)
    assert again["ok"] is False
    assert again["status"] == 409
    assert len(clicked) == 1, "同一个提案点了两次"


def test_execute_refuses_when_the_target_pixels_changed(monkeypatch, tmp_path):
    """定位时看到的是这个按钮、点的时候已经变了 —— 必须不点。"""
    proposal_id = _prepare(monkeypatch)
    # 画面变成另一种颜色：指纹不再匹配。
    _FakeScreen(monkeypatch, raw=bytes([10, 20, 30, 255]) * (WIDTH * HEIGHT))
    clicked = []
    import desktop_actions
    import game_workbench
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *a, **k: clicked.append((a, k)) or {"ok": True})
    monkeypatch.setattr(game_workbench, "require_approval", lambda root, a, t: None)
    monkeypatch.setattr(game_workbench, "approval",
                        lambda root, action, user, approved=False, target="":
                        {"approved": True, "action": action, "target": target})
    result = visual_click.execute("prj-x", str(tmp_path), proposal_id=proposal_id, gate=True)
    assert result["ok"] is False
    assert result["status"] == 409
    assert "画面已变化" in result["error"]
    assert not clicked


def test_execute_refuses_when_the_window_switched(monkeypatch, tmp_path):
    """窗口换了（不是同一个嵌入窗口）→ 不点。"""
    proposal_id = _prepare(monkeypatch)
    _FakeScreen(monkeypatch, hwnd=9999)
    import desktop_actions
    import game_workbench
    clicked = []
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *a, **k: clicked.append((a, k)) or {"ok": True})
    monkeypatch.setattr(game_workbench, "require_approval", lambda root, a, t: None)
    monkeypatch.setattr(game_workbench, "approval",
                        lambda root, action, user, approved=False, target="":
                        {"approved": True, "action": action, "target": target})
    result = visual_click.execute("prj-x", str(tmp_path), proposal_id=proposal_id, gate=True)
    assert result["ok"] is False
    assert "窗口已切换" in result["error"]
    assert not clicked


def test_another_project_cannot_execute_the_proposal(monkeypatch):
    """别的项目的提案在这里「不存在」，且不回 403 —— 不泄露它是否存在。"""
    proposal_id = _prepare(monkeypatch)
    result = visual_click.execute("prj-other", ".", proposal_id=proposal_id, confirmed=True)
    assert result["ok"] is False
    assert result["status"] == 404


# --------------------------------------------------------------------------- 审批 target

def test_approval_target_is_stable_for_the_same_point():
    """同一坐标 → 同一 target：用户批准之后，agent 用同样参数重试才对得上。"""
    first = visual_click.approval_target({"x": 12, "y": 34})
    second = visual_click.approval_target({"x": 12, "y": 34})
    assert first == second
    assert first.startswith("desktop:click:embedded:")


def test_approval_target_changes_with_the_point():
    """批准之后换坐标 → 另一个 target → 必须重新批准。"""
    assert visual_click.approval_target({"x": 12, "y": 34}) != \
        visual_click.approval_target({"x": 13, "y": 34})


def test_approval_target_matches_the_desktop_action_format():
    """与 dev_desktop_action 的 target 格式一致 —— 两类点击在同一个审核面板批准。"""
    import hashlib
    payload = {"action": "click", "target": "embedded", "x": 7, "y": 9,
               "to_x": None, "to_y": None, "key": "",
               "text_sha256": hashlib.sha256("".encode("utf-8")).hexdigest()[:16]}
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    assert visual_click.approval_target({"x": 7, "y": 9}) == "desktop:click:embedded:%s" % digest


# ------------------------------------------------------------------- 引擎独立窗口（浏览器模式）

def test_locate_falls_back_to_project_engine_window(monkeypatch):
    """浏览器模式无桌面宿主：嵌入抓不到 → 回退本项目自己启动的引擎独立窗口，并记住来源。"""
    import screen_capture
    import game_workbench
    monkeypatch.setattr(screen_capture, "grab_embedded",
                        lambda pid, strict_project=True: None)
    monkeypatch.setattr(game_workbench, "engine_window_frame",
                        lambda root, hint="": ((RAW, WIDTH, HEIGHT, 7777), "StarVoyager (DEBUG)"))
    monkeypatch.setattr(screen_capture, "encode_frame",
                        lambda raw, width, height: "ZmFrZS1pbWFnZQ==")
    llm = _FakeLLM(_target_json())
    monkeypatch.setattr(visual_click, "resolve_vision_llm", lambda pid: (llm, "harness", ""))

    result = visual_click.locate("prj-x", description="开始按钮", root="D:/proj")

    assert result["ok"] is True
    entry = visual_click.PROPOSALS[result["proposal_id"]]
    assert entry["capture_target"] == "engine"
    assert entry["hwnd"] == 7777


def test_locate_without_root_does_not_fabricate_engine_window(monkeypatch):
    """没有项目根目录时不凭空兜底：嵌入抓不到就明确报错，绝不枚举/抓取任意窗口。"""
    import screen_capture
    monkeypatch.setattr(screen_capture, "grab_embedded",
                        lambda pid, strict_project=True: None)
    monkeypatch.setattr(visual_click, "_root_for_pid", lambda pid: "")
    result = visual_click.locate("prj-x", description="开始按钮")
    assert result["ok"] is False
    assert "嵌入窗口" in result["error"]


def test_execute_engine_proposal_rechecks_and_clicks_via_engine_target(monkeypatch, tmp_path):
    """引擎来源的提案：点击前复核与点击投递都走 engine 目标，句柄与 root 一并交给 desktop_actions。"""
    import screen_capture
    import game_workbench
    import desktop_actions
    monkeypatch.setattr(screen_capture, "grab_embedded",
                        lambda pid, strict_project=True: None)
    monkeypatch.setattr(game_workbench, "engine_window_frame",
                        lambda root, hint="": ((RAW, WIDTH, HEIGHT, 7777), "StarVoyager (DEBUG)"))
    monkeypatch.setattr(screen_capture, "encode_frame",
                        lambda raw, width, height: "ZmFrZS1pbWFnZQ==")
    llm = _FakeLLM(_target_json())
    monkeypatch.setattr(visual_click, "resolve_vision_llm", lambda pid: (llm, "harness", ""))
    located = visual_click.locate("prj-x", description="开始按钮", root=str(tmp_path))
    assert located["ok"]

    clicked = []
    monkeypatch.setattr(desktop_actions, "perform",
                        lambda *a, **k: clicked.append(k) or {"ok": True})
    monkeypatch.setattr(game_workbench, "require_approval", lambda root, a, t: None)
    monkeypatch.setattr(game_workbench, "approval",
                        lambda root, action, user, approved=False, target="":
                        {"approved": bool(approved), "action": action, "target": target})

    result = visual_click.execute("prj-x", str(tmp_path),
                                  proposal_id=located["proposal_id"], gate=True)

    assert result["ok"] is True
    assert clicked and clicked[0]["target"] == "engine"
    assert clicked[0]["root"] == str(tmp_path)
    assert clicked[0]["expected_hwnd"] == 7777
