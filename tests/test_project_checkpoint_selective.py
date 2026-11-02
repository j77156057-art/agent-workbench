
from agent_runtime.project_checkpoint import create_checkpoint, restore_checkpoint


def test_restore_checkpoint_can_limit_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("before-a", encoding="utf-8")
    (project / "b.txt").write_text("before-b", encoding="utf-8")
    manifest = create_checkpoint(str(project), str(tmp_path / "state"), "wf-selective")
    (project / "a.txt").write_text("after-a", encoding="utf-8")
    (project / "b.txt").write_text("after-b", encoding="utf-8")

    result = restore_checkpoint(str(project), str(tmp_path / "state"), "wf-selective",
                                manifest, selected_paths=["a.txt"])

    assert result["ok"]
    assert result["restored"] == ["a.txt"]
    assert (project / "a.txt").read_text(encoding="utf-8") == "before-a"
    assert (project / "b.txt").read_text(encoding="utf-8") == "after-b"


def test_restore_checkpoint_rejects_escape_in_selected_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("before", encoding="utf-8")
    manifest = create_checkpoint(str(project), str(tmp_path / "state"), "wf-safe")
    try:
        restore_checkpoint(str(project), str(tmp_path / "state"), "wf-safe", manifest,
                           selected_paths=["../outside.txt"])
    except ValueError as exc:
        assert "路径" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")
