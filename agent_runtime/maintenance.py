"""内部记忆定时检查：按用户配置的分钟数运行健康检查 + 保守维护。

用户把间隔设为 0 即关闭。检查内容：health.report（只读体检）+ health.maintain(apply=True)
（候选去重、共享去重、超过保留期的失效归档、重建索引）。所有维护都可从
archive/memory 追溯；不做 active 事实删除。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .memory import health, metrics, store

_STOP = threading.Event()
_THREAD = None
_LOCK = threading.RLock()
LAST_FILE = "maintenance-last.json"


def _last_path():
    return store.safe_path(LAST_FILE)


def last_status():
    try:
        with open(_last_path(), "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_last(value):
    try:
        store.atomic_write(_last_path(), json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass


def run_check(project_id=None):
    """执行一次内部检查；返回可展示的结构，任何异常都只记录不抛出。"""
    try:
        from config import get_memory_v2
        if not get_memory_v2():
            return {"ok": False, "skipped": "memory_v2_off"}
    except Exception:  # noqa: BLE001
        return {"ok": False, "skipped": "config_error"}
    try:
        import projects
        project = project_id or projects.current_project_id() or "default"
    except Exception:  # noqa: BLE001
        project = project_id or "default"
    started = time.perf_counter()
    result = {"ok": True, "ts": store.now(), "project": project}
    try:
        report = health.report(project)
        result["warnings"] = report.get("warnings") or []
        result["counts"] = {"shared": report.get("shared"), "project": report.get("project")}
        maintenance = health.maintain(project, apply=True)
        result["maintenance"] = {
            "candidates": maintenance.get("candidates"),
            "shared": maintenance.get("shared"),
            "gc": maintenance.get("gc"),
            "index": maintenance.get("index"),
        }
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        metrics.record("memory.check", project=project, latency_ms=result["latency_ms"],
                       warnings=len(result["warnings"]),
                       dedupe_candidates=(maintenance.get("candidates") or {}).get("merged"),
                       dedupe_shared=(maintenance.get("shared") or {}).get("duplicates"),
                       gc=(maintenance.get("gc") or {}).get("archived"))
    except Exception as exc:  # noqa: BLE001 ---- 定时任务失败不能影响服务
        result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "latency_ms": round((time.perf_counter() - started) * 1000)})
        metrics.record("memory.check.error", project=project, error=result["error"][:200])
    _write_last(result)
    return result


def _loop():
    while not _STOP.is_set():
        try:
            from config import get_memory_check_interval
            minutes = int(get_memory_check_interval() or 0)
        except Exception:  # noqa: BLE001
            minutes = 0
        if minutes <= 0:
            _STOP.wait(15)
            continue
        deadline = time.monotonic() + minutes * 60
        while not _STOP.is_set() and time.monotonic() < deadline:
            _STOP.wait(min(15, max(1, deadline - time.monotonic())))
        if _STOP.is_set():
            break
        run_check()


def start():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="docmind-memory-check", daemon=True)
        _THREAD.start()
        return True


def stop():
    global _THREAD
    with _LOCK:
        _STOP.set()
        _THREAD = None
