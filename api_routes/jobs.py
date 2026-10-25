"""Background command job endpoints: list / detail / cancel.

The agent starts long commands with ``run_command(..., background: true)``; until
now the only way to watch one was to keep asking the model to call
``dev_job_logs``. These endpoints let the workbench render the same registry as a
standalone progress/log panel.

Security boundary: every endpoint is scoped to the current project root, and the
job registry itself filters by the job's ``cwd``. A job started for another
project is reported as "not found" rather than leaking its command or output.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent_runtime import process_runner

_RUNNING_STATES = ("starting", "running")


def _project_root(ctx) -> str:
    """Current project root, or "" in legacy mode (no project / invalid root)."""
    try:
        return str(ctx._project_root_or_error() or "")
    except Exception:  # noqa: BLE001 - a broken registry must not break the panel
        return ""


def _list_payload(scope: str, root: str) -> dict:
    scoped_root = "" if scope == "all" else root
    report = process_runner.job_logs("", root=scoped_root)
    jobs = report.get("jobs") or []
    running = sum(1 for job in jobs if str(job.get("state")) in _RUNNING_STATES)
    hidden = 0
    if scoped_root:
        visible = {job.get("job_id") for job in jobs}
        everything = process_runner.job_logs("").get("jobs") or []
        hidden = sum(1 for job in everything if job.get("job_id") not in visible)
    return {
        "ok": True,
        "scope": "all" if scope == "all" else "project",
        "root": root,
        "jobs": jobs,
        "running": running,
        "hidden": hidden,
        "capacity": process_runner.MAX_JOBS,
    }


def build_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/jobs", tags=["jobs"])

    @router.get("")
    async def list_jobs(scope: str = "project"):
        """Recent jobs for the current project (``scope=all`` for debugging)."""
        return await run_in_threadpool(_list_payload, scope, _project_root(ctx))

    @router.get("/{job_id}")
    async def job_detail(job_id: str, offset: int = 0):
        """One job's state plus the current log window (offset is a char offset)."""
        report = await run_in_threadpool(
            process_runner.job_logs, job_id, offset=offset, root=_project_root(ctx))
        if not report.get("ok"):
            return JSONResponse(
                {"ok": False, "error": report.get("error") or "后台任务不存在"},
                status_code=404)
        return report

    @router.post("/{job_id}/cancel")
    async def cancel_job(job_id: str):
        """Stop a job; collected output stays readable until the normal TTL cleanup."""
        report = await run_in_threadpool(
            process_runner.job_cancel, job_id, root=_project_root(ctx))
        if not report.get("ok"):
            return JSONResponse(
                {"ok": False, "error": report.get("error") or "后台任务不存在"},
                status_code=404)
        # Cancelling is not a log read: drop the (potentially 8k) logs from the response.
        return {k: v for k, v in report.items()
                if k not in ("logs", "offset", "next_offset")}

    return router


__all__ = ["build_router"]
