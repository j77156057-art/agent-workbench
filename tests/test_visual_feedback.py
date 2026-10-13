"""Raster feedback evidence must survive restarts and stay inside its project."""
import base64
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowError
from agent_runtime.visual_feedback import decode_snapshot, evidence_path


def png(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (40, 24), color).save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def workflow(tmp_path):
    manager = GameWorkflowManager(str(tmp_path))
    wid = manager.start("调整项目画面", project_root=str(tmp_path / "project"))["workflow_id"]
    manager.record_visual_feedback(wid, {"id": "feedback-1", "note": "调整按钮", "screenshot": True})
    yield manager, wid
    manager.close()


def test_before_after_survive_restart_without_inline_image_data(workflow):
    manager, wid = workflow
    assert not manager.get(wid)["visual_feedback"][0]["screenshot"]
    manager.save_visual_snapshot(wid, "feedback-1", "before", png())
    manager.save_visual_snapshot(wid, "feedback-1", "after", png("blue"))
    manager.save_visual_snapshot(wid, "feedback-1", "after", png("green"))
    manager.update_visual_feedback(wid, "feedback-1", "accepted", "效果满意")
    restarted = GameWorkflowManager(str(manager.state_root.parents[1]))
    try:
        item = restarted.get(wid)["visual_feedback"][0]
        assert item["screenshot"] and item["status"] == "accepted"
        assert item["snapshots"]["before"]["width"] == 40
        assert item["snapshots"]["after"]["height"] == 24
        with Image.open(restarted.visual_snapshot_path(wid, "feedback-1", "before")) as image:
            assert image.getpixel((0, 0)) == (255, 0, 0)
        with Image.open(restarted.visual_snapshot_path(wid, "feedback-1", "after")) as image:
            assert image.getpixel((0, 0)) == (0, 128, 0)
        raw = manager._path(wid).read_text(encoding="utf-8")
        assert "base64" not in raw and "image_base64" not in raw
    finally:
        restarted.close()


def test_retry_is_idempotent_and_before_snapshot_is_immutable(workflow):
    manager, wid = workflow
    original = manager.save_visual_snapshot(wid, "feedback-1", "before", png())["feedback"]
    saved = manager.record_visual_feedback(wid, {"id": "feedback-1", "note": "重复发送"})
    assert saved["feedback"]["snapshots"] == original["snapshots"]
    assert saved["feedback"]["note"] == "调整按钮"
    with pytest.raises(WorkflowError, match="不能覆盖"):
        manager.save_visual_snapshot(wid, "feedback-1", "before", png("blue"))
    with Image.open(manager.visual_snapshot_path(wid, "feedback-1", "before")) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("encoded", ["", "%%%", "aGVsbG8=", "data:text/html;base64,aGVsbG8=", "data:image/png;base64,???"])
def test_invalid_images_rejected(encoded):
    with pytest.raises(ValueError):
        decode_snapshot(encoded)


def test_image_dimensions_and_metadata_are_checked():
    from PIL.PngImagePlugin import PngInfo
    output = io.BytesIO()
    metadata = PngInfo()
    metadata.add_text("private", "not part of evidence")
    Image.new("RGB", (4, 4)).save(output, "PNG", pnginfo=metadata)
    image, _ = decode_snapshot(base64.b64encode(output.getvalue()).decode())
    with Image.open(io.BytesIO(image)) as clean:
        assert "private" not in clean.info
    with patch("agent_runtime.visual_feedback.MAX_IMAGE_PIXELS", 8):
        with pytest.raises(ValueError, match="尺寸过大"):
            decode_snapshot(png())


@pytest.mark.parametrize("wid,fid,phase", [("../other", "f", "before"), ("w", "../other", "before"), ("w", "f", "../../outside")])
def test_evidence_path_rejects_escape(tmp_path, wid, fid, phase):
    with pytest.raises(ValueError):
        evidence_path(tmp_path, wid, fid, phase)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "oops"])
def test_region_rejects_nonfinite_values(workflow, value):
    manager, wid = workflow
    with pytest.raises(WorkflowError, match="坐标无效"):
        manager.record_visual_feedback(wid, {"note": "bad", "region": {"x": value}})


def test_region_bounds_and_feedback_status_do_not_change_workflow(workflow):
    manager, wid = workflow
    status = manager.get(wid)["status"]
    result = manager.record_visual_feedback(wid, {"note": "检查边缘", "region": {"x": 95, "y": -4, "width": 40, "height": 150}})
    assert result["feedback"]["region"] == {"x": 95, "y": 0, "width": 5, "height": 100}
    manager.update_visual_feedback(wid, "feedback-1", "processing")
    state = manager.get(wid)
    assert state["status"] == status
    assert "status" not in state["events"][-1]
    with pytest.raises(WorkflowError):
        manager.update_visual_feedback(wid, "feedback-1", "completed")
    with pytest.raises(WorkflowError):
        manager.visual_snapshot_path(wid, "feedback-1", "before")
    with pytest.raises(WorkflowError):
        manager.save_visual_snapshot(wid, "missing", "before", png())


def test_historical_feedback_does_not_rearm_project_mutex(workflow):
    manager, wid = workflow
    root = manager.get(wid)["project_root"]
    restarted = GameWorkflowManager(str(manager.state_root.parents[1]))
    try:
        restarted.record_visual_feedback(wid, {"id": "historical", "note": "历史意见"})
        restarted.update_visual_feedback(wid, "historical", "pending")
        restarted.save_visual_snapshot(wid, "historical", "before", png())
        assert wid not in restarted._states
        assert restarted.start("新的任务", project_root=root)["workflow_id"] != wid
    finally:
        restarted.close()


def test_disk_failure_is_reported_instead_of_acknowledged(workflow):
    manager, wid = workflow
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        with pytest.raises(WorkflowError, match="记录保存失败"):
            manager.update_visual_feedback(wid, "feedback-1", "failed")


def test_api_roundtrip_and_project_isolation(workflow, tmp_path):
    from api_routes.agent import build_router
    manager, wid = workflow
    root = manager.get(wid)["project_root"]
    current = {"root": root}
    ctx = SimpleNamespace(
        _project_root_or_error=lambda: current["root"],
        _request_project_id=lambda: "",
    )
    app = FastAPI()
    with patch("api_routes.agent.WORKFLOWS", manager):
        app.include_router(build_router(ctx))
        with TestClient(app) as client:
            url = f"/api/agent/workflow/{wid}/visual-feedback/feedback-1"
            assert client.post(url + "/snapshot/before", json={"image_base64": png()}).json()["ok"]
            response = client.get(url + "/snapshot/before")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.content.startswith(b"\x89PNG")
            assert response.headers["cache-control"] == "no-store"
            assert client.post(url + "/status", json={"status": "awaiting_review"}).json()["ok"]
            current["root"] = str(tmp_path / "another-project")
            assert client.get(url + "/snapshot/before").status_code == 404
            assert not client.post(url + "/status", json={"status": "accepted"}).json()["ok"]
            assert not client.post(url + "/snapshot/after", json={"image_base64": png()}).json()["ok"]
            assert not client.post(f"/api/agent/workflow/{wid}/visual-feedback", json={"note": "wrong project"}).json()["ok"]
