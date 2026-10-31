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
from datetime import datetime

from .memory import health, metrics, store

_STOP = threading.Event()
_THREAD = None
_LOCK = threading.RLock()
LAST_FILE = "maintenance-last.json"
#: 到点后先等这一小段再真的开跑：健康检查会开 SQLite、可能触发重嵌入，
#: 不该压在服务启动与首批请求中间。
STARTUP_GRACE_SECONDS = 20


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


def _seconds_to_next(minutes, now_value=None):
    """距下一次检查还有多少秒；`<= 0` 表示现在就该跑（含「从没跑过」）。

    为什么不能只按 monotonic 睡一个间隔：服务每次重启都会从零开始计时，间隔设成
    一天就意味着「只要这台机器每天重启一次，这个检查永远不发生」。实测正是这样 ——
    功能交付了，`maintenance-last.json` 一直是空的。
    """
    if now_value is None:
        now_value = time.time()
    last = last_status().get("ts")
    if not last:
        return 0
    try:
        then = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0
    return minutes * 60 - (now_value - then)


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
        wait = _seconds_to_next(minutes)
        if wait > STARTUP_GRACE_SECONDS:
            # 还没到点：分片睡，停服时能立刻退出；间隔被用户改小时下一轮自然跟上。
            deadline = time.monotonic() + wait - STARTUP_GRACE_SECONDS
            while not _STOP.is_set() and time.monotonic() < deadline:
                _STOP.wait(min(15, max(1, deadline - time.monotonic())))
            if _STOP.is_set():
                break
        # 到点或从没跑过：留一小段宽限让服务先起来（健康检查会开 SQLite、可能重嵌向量）。
        if _STOP.wait(STARTUP_GRACE_SECONDS):
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
