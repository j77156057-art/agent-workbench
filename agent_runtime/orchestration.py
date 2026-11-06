"""可恢复的 DAG 编排：JSON 任务图、审批后续跑、重启恢复与结果重合成。

每个计划独立保存为 .docmind/orchestrations/<plan_id>.json。单文件聚合配合
跨进程短锁和原子替换，足够覆盖本地单项目的恢复与审计，无需引入数据库迁移。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from file_locks import cross_process_lock

_ROOT = Path(".docmind") / "orchestrations"
_LOCK = threading.RLock()
_AGENTS = {}
_RUNNING = set()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(plan_id):
    safe = str(plan_id or "").replace("/", "_").replace("\\", "_")
    return _ROOT / (safe + ".json")


def _read(plan_id):
    path = _path(plan_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(record):
    path = _path(record["plan_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n"
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _update(plan_id, mutate):
    with cross_process_lock(_path(plan_id), timeout=10):
        record = _read(plan_id)
        if record is None:
            raise KeyError("未找到编排计划 " + str(plan_id))
        mutate(record)
        record["revision"] = int(record.get("revision") or 0) + 1
        record["updated_at"] = _now()
        _write(record)
    return record


def get(plan_id):
    """读取持久计划；不触发任何节点执行。"""
    with cross_process_lock(_path(plan_id), timeout=10):
        return _read(plan_id)


def _task_with_ids(task, plan_id):
    item = dict(task)
    item["_plan_id"] = plan_id
    return item


def _callbacks(record, agent, turn=None):
    try:
        import gpu_coordinator as _gpu
    except Exception:  # noqa: BLE001 - 无 GPU 探测不影响编排恢复
        _gpu = None

    plan_id = record["plan_id"]
    options = record.get("options") or {}
    kind = record["kind"]

    if kind == "orchestrate":
        runner = lambda task, context: agent._task_runner(_task_with_ids(task, plan_id), context, turn=turn)
        synth_runner = ((lambda tasks, results: agent._synth(tasks, results, turn=turn))
                        if options.get("synth", True) else None)
        replanner = ((lambda failed, results, attempt, ctx=None: agent._replanner(
            failed, results, attempt, turn=turn)) if options.get("replan", True) else None)
        max_replans = int(options.get("max_replans") or 0)
        max_tasks = options.get("task_limit")
    elif kind == "task_assist":
        from agent_runtime import task_assistant as assistant
        llm = agent._child_llm()
        goal = str(options.get("goal") or "")

        def ask(prompt):
            out = llm.chat([{"role": "user", "content": prompt}], stream=False, temperature=0.2)
            if turn is not None:
                turn.add_usage(getattr(llm, "last_usage", None))
            return out

        def runner(task, context):
            item = _task_with_ids(task, plan_id)
            out = agent._run_child(
                item.get("role"), assistant.render_spec(item), context=context, turn=turn,
                persona=item.get("persona", ""), tool_allowlist=(item.get("tools") or None),
                mcp_policy=item.get("mcp", "auto"), reflect=bool(item.get("reflection", True)),
                max_steps=item.get("max_steps"), _plan_id=plan_id, _task_id=item.get("id"))
            return assistant.envelope(item, out)

        def synth_runner(tasks, results):
            try:
                return ask(assistant.summary_prompt(goal, tasks, results))
            except Exception:  # noqa: BLE001 - 下方确定性人话兜底
                return ""

        def replanner(failed, results, attempt, ctx=None):
            try:
                proposal = assistant.parse_tasks(ask(assistant.recovery_prompt(failed, results, attempt)), goal)
                for task in failed:
                    results[task["id"]]["optional"] = True
                    results[task["id"]]["recovery_attempted"] = True
                return proposal
            except Exception:  # noqa: BLE001
                return []

        max_replans = int(options.get("max_replans") or 0)
        max_tasks = None
    else:
        raise ValueError("未知编排类型 " + str(kind))

    kwargs = {
        "synth_runner": synth_runner,
        "max_parallel": max(1, int(options.get("max_parallel") or 4)),
        "replanner": replanner,
        "max_replans": max_replans,
        "max_tasks": max_tasks,
        "budget_session": record.get("session_id") or "",
        "vram_provider": lambda: (_gpu.memory_info() or {}).get("free_mb") if _gpu else None,
        "cost_aware": True,
    }
    return runner, kwargs


def _event(plan_id, kind, payload):
    def mutate(record):
        if kind == "wave":
            for tid in payload.get("tasks") or []:
                current = (record["results"].get(tid) or {}).get("status")
                if current not in {"ok", "failed", "rejected", "skipped", "blocked"}:
                    record["results"][tid] = {"status": "running", "conclusion": "", "steps": 0, "error": ""}
        elif kind == "task":
            record["results"][payload["id"]] = payload["result"]
    return _update(plan_id, mutate)


def _execute(record, agent, turn=None):
    import orchestrator as _orchestrator

    plan_id = record["plan_id"]
    runner, kwargs = _callbacks(record, agent, turn=turn)
    resume = {
        "results": copy.deepcopy(record.get("results") or {}),
        "waves": copy.deepcopy(record.get("waves") or []),
        "replans": int(record.get("replans") or 0),
        "revisions": copy.deepcopy(record.get("revisions") or []),
        "dispatches": copy.deepcopy(record.get("dispatches") or []),
        "steps_used": int(record.get("steps_used") or 0),
    }
    report = _orchestrator.run_plan(
        record["tasks"], runner,
        checkpoint=lambda kind, payload: _event(plan_id, kind, payload),
        resume=resume, **kwargs)
    report["plan_id"] = plan_id
    if record["kind"] == "task_assist":
        from agent_runtime import task_assistant as assistant
        for task in report.get("tasks") or []:
            tid = task.get("id")
            if tid in report.get("results", {}):
                report["results"][tid] = assistant.envelope(task, report["results"][tid])
        if not report.get("merged"):
            report["merged"] = assistant.fallback_summary(report)
        report["assistant"] = {"orchestrator_only": True, "business_tools": [],
                               "max_replans": int(record["options"].get("max_replans") or 0)}
    statuses = [str(item.get("status") or "") for item in report.get("results", {}).values()]
    if "unknown" in statuses:
        status = "interrupted"
    elif "pending" in statuses or not report.get("final", True):
        status = "waiting_approval"
    else:
        status = "completed" if report.get("ok") else "failed"

    def mutate(saved):
        saved["tasks"] = report["tasks"]
        saved["results"] = report["results"]
        saved["waves"] = report["waves"]
        saved["replans"] = report["replans"]
        saved["revisions"] = report.get("revisions") or []
        saved["dispatches"] = report.get("dispatches") or []
        saved["steps_used"] = report.get("steps_used") or 0
        saved["status"] = status
        saved["final"] = bool(report.get("final"))
        saved["report"] = report
    saved = _update(plan_id, mutate)
    return saved


def run(agent, kind, tasks, options, turn=None):
    """创建并执行新计划；每个结果和状态迁移都同步落盘。"""
    plan_id = "plan-" + uuid.uuid4().hex[:16]
    now = _now()
    record = {
        "schema": 1, "plan_id": plan_id, "revision": 0,
        "session_id": str(agent.memory_session_id or agent.session_id or "default"),
        "project_id": str(agent.project_id or ""), "kind": str(kind),
        "options": copy.deepcopy(options), "tasks": copy.deepcopy(list(tasks)),
        "results": {}, "waves": [], "replans": 0, "revisions": [],
        "dispatches": [], "steps_used": 0, "status": "running", "final": False,
        "report": None, "created_at": now, "updated_at": now,
    }
    _write(record)
    with _LOCK:
        _AGENTS[plan_id] = agent
        _RUNNING.add(plan_id)
    try:
        return _execute(record, agent, turn=turn)
    finally:
        with _LOCK:
            _RUNNING.discard(plan_id)


def resume(plan_id, node, agent=None):
    """审批后恢复同一任务图；完成结果保留，running 崩溃结果转 unknown。"""
    with _LOCK:
        if plan_id in _RUNNING:
            current = get(plan_id)
            return {"ok": True, "already_running": True, "plan": current}
        _RUNNING.add(plan_id)
        if agent is None:
            agent = _AGENTS.get(plan_id)
    try:
        if agent is None:
            # 进程重启后由 API 注入同会话 Agent；独立调用也允许显式注入。
            raise ValueError("恢复计划需要同会话 Agent 实例")
        node_result = copy.deepcopy(node.get("result") or {})
        task_id = str(node.get("task_id") or "")
        if not task_id:
            raise ValueError("审批节点缺少 task_id")

        def prepare(record):
            record["results"][task_id] = node_result
            for result in (record.get("results") or {}).values():
                if result.get("status") == "running":
                    result["status"] = "unknown"
                    result["error"] = "进程在节点运行期间退出；结果未知，未自动重跑"
        record = _update(plan_id, prepare)
        saved = _execute(record, agent)
        return {"ok": True, "plan": saved, "report": saved["report"],
                "final": bool(saved.get("final"))}
    finally:
        with _LOCK:
            _RUNNING.discard(plan_id)
