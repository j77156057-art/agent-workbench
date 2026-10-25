# -*- coding: utf-8 -*-
"""后台任务面板 HTTP 接口：项目隔离 / 详情日志 / 取消。

路由挂在一个最小 FastAPI app 上，用真实 ``process_runner`` 起子进程验证——
这些接口的价值就是"看得见真实任务"，用 mock 掉注册表等于什么都没验。
每个用例结束都从注册表里摘掉自己起的任务，避免污染同进程的其它测试。
"""
import os
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_routes.jobs as jobs_routes  # noqa: E402
from agent_runtime import process_runner as pr  # noqa: E402


class _Ctx:
    def __init__(self, root):
        self._root = root

    def _project_root_or_error(self):
        return self._root


def _client(root):
    app = FastAPI()
    app.include_router(jobs_routes.build_router(_Ctx(root)))
    return TestClient(app)


def _start(root, code, timeout=30):
    started = pr.start_job([sys.executable, "-u", "-c", code],
                           cwd=str(root), timeout=timeout)
    assert started.get("ok"), started
    return started["job_id"]


def _forget(*job_ids):
    for job_id in job_ids:
        try:
            pr.job_cancel(job_id)
        except Exception:  # noqa: BLE001 - 清理失败不能让用例结果变红
            pass
        with pr._LOCK:
            pr._JOBS.pop(job_id, None)


def _wait_detail(client, job_id, *, contains="", deadline=8.0):
    """轮询详情直到任务终态（若给了 contains，还要等输出刷进缓冲）。"""
    end = time.monotonic() + deadline
    body = {}
    while time.monotonic() < end:
        body = client.get(f"/api/jobs/{job_id}").json()
        terminal = body.get("state") not in ("starting", "running")
        if terminal and (not contains or contains in str(body.get("logs") or "")):
            return body
        time.sleep(0.05)
    return body


def test_list_scopes_by_project_and_hides_other_jobs(tmp_path):
    root_a, root_b = tmp_path / "alpha", tmp_path / "beta"
    root_a.mkdir()
    root_b.mkdir()
    job_a = _start(root_a, "import time; time.sleep(30)")
    job_b = _start(root_b, "import time; time.sleep(30)")
    try:
        client = _client(root_a)
        body = client.get("/api/jobs").json()
        ids = [job["job_id"] for job in body["jobs"]]
        assert body["ok"] is True
        assert body["scope"] == "project"
        assert job_a in ids and job_b not in ids
        assert body["running"] >= 1
        assert body["hidden"] >= 1
        assert body["capacity"] == pr.MAX_JOBS

        row = next(job for job in body["jobs"] if job["job_id"] == job_a)
        assert row["cwd"] == os.path.abspath(str(root_a))
        assert "logs" not in row, "列表不该顺带把 8k 日志全塞回来"

        # 另一个项目的任务：详情/取消都按"不存在"处理，不泄漏命令与输出。
        assert client.get(f"/api/jobs/{job_b}").status_code == 404
        assert client.post(f"/api/jobs/{job_b}/cancel").status_code == 404

        everything = client.get("/api/jobs?scope=all").json()
        all_ids = [job["job_id"] for job in everything["jobs"]]
        assert job_a in all_ids and job_b in all_ids
        assert everything["scope"] == "all"
    finally:
        _forget(job_a, job_b)


def test_detail_returns_terminal_state_and_log_window(tmp_path):
    job_id = _start(tmp_path, "print('后台任务输出中文')")
    try:
        client = _client(tmp_path)
        body = _wait_detail(client, job_id, contains="后台任务输出中文")
        assert body["state"] == "done"
        assert body["exit_code"] == 0
        assert body["cwd"] == os.path.abspath(str(tmp_path))
        assert "后台任务输出中文" in body["logs"]
        assert body["next_offset"] == len(body["logs"])
        assert body["offset"] == 0

        tail = client.get(f"/api/jobs/{job_id}?offset=2").json()
        assert tail["offset"] == 2
        assert tail["logs"] == body["logs"][2:]
        assert tail["next_offset"] == len(body["logs"])
    finally:
        _forget(job_id)


def test_cancel_marks_job_cancelled_and_keeps_logs_readable(tmp_path):
    job_id = _start(tmp_path, "import time; time.sleep(60)", timeout=120)
    try:
        client = _client(tmp_path)
        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        body = cancelled.json()
        assert body["ok"] is True
        assert body["state"] == "cancelled"
        assert "logs" not in body, "取消不是读日志，不该回 8k 文本"

        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["state"] == "cancelled"
        assert "logs" in detail, "取消后已采集的输出必须还能看"
    finally:
        _forget(job_id)


def test_unknown_and_out_of_scope_jobs_are_404(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    client = _client(tmp_path)
    assert client.get("/api/jobs/deadbeef00").status_code == 404
    assert client.post("/api/jobs/deadbeef00/cancel").status_code == 404

    # 同名前缀的兄弟目录不能被当成"在项目内"。
    assert pr._is_within(str(tmp_path / "project2"), str(tmp_path / "project")) is False
    assert pr._is_within(str(tmp_path / "project" / "sub"), str(tmp_path / "project")) is True


def test_empty_legacy_root_does_not_scope(tmp_path):
    class _NoRoot:
        def _project_root_or_error(self):
            return None

    app = FastAPI()
    app.include_router(jobs_routes.build_router(_NoRoot()))
    job_id = _start(tmp_path, "import time; time.sleep(30)")
    try:
        body = TestClient(app).get("/api/jobs").json()
        assert job_id in [job["job_id"] for job in body["jobs"]]
        assert body["scope"] == "project"
    finally:
        _forget(job_id)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
