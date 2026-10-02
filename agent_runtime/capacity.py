"""Persisted user policy for multi-agent task capacity."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

SETTING_KEY = "agent_capacity_settings"

#: 默认**全部为 0 = 不设上限**（2026-10-02 起）。
#:
#: 为什么从 8/24/50000 改成 0：这些默认值是「防失控」而不是「防出错」——它们压的是
#: **正常**的长任务，而不是危险行为。用户反馈的正是这一点：一句话问答被 8 步截断、
#: 一轮审查被 50000 token 掐断，看起来像 agent 变笨了。
#:
#: 放开之后**仍然在**的护栏（这些不是"上限"，是正确性/安全性约束，一个都不动）：
#:   · 重复调用同一 (工具, 参数) 被拒；
#:   · 连续失败熔断、用户取消、截止时间；
#:   · 写操作的人工审批门、分区越界拦截、命令黑名单；
#:   · 观察截断与轨迹裁剪（防上下文撑爆，不是防"多跑几步"）；
#:   · `_MAX_FORMAT_STALLS`（我刚加的格式失守早退）——它防的是"同一句话重复 8 遍"，
#:     正是用户当初看到那个现象的根因，与步数上限无关。
#:
#: 代价（说清楚而不是藏起来）：真出现死循环时，没有步数兜底了。所以
#: `remaining_agent_capacity` / `RunBudget` / `normalize_settings` 三处的 0 语义必须
#: 保持"不限"，任何一处把 0 当成"用默认"就会静默退回旧行为——那比不改更坏。
DEFAULTS = {
    "max_agents": 0,               # 0 = 不限并发子代理
    "main_max_steps": 0,           # 0 = 不限工具步数
    "child_max_steps": 0,          # 0 = 不限子代理步数
    "max_nudges": 2,               # 纠偏次数仍保留：它防的是"同一提示重复 8 遍"
    "max_final_continuations": 2,  # 续写仍保留：防输出被长度截断后答一半
    "task_token_budget": 0,        # 0 = 不限 token
    "local_auto_limit": True,      # 本地模型显存自适应（不是步数上限，保留）
}


def normalize_settings(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    try:
        max_agents = int(raw.get("max_agents", DEFAULTS["max_agents"]))
    except (TypeError, ValueError):
        max_agents = DEFAULTS["max_agents"]
    try:
        main_max_steps = int(raw.get("main_max_steps", DEFAULTS["main_max_steps"]))
    except (TypeError, ValueError):
        main_max_steps = DEFAULTS["main_max_steps"]
    try:
        child_max_steps = int(raw.get("child_max_steps", DEFAULTS["child_max_steps"]))
    except (TypeError, ValueError):
        child_max_steps = DEFAULTS["child_max_steps"]
    try:
        max_nudges = int(raw.get("max_nudges", DEFAULTS["max_nudges"]))
    except (TypeError, ValueError):
        max_nudges = DEFAULTS["max_nudges"]
    try:
        max_final_continuations = int(raw.get("max_final_continuations", DEFAULTS["max_final_continuations"]))
    except (TypeError, ValueError):
        max_final_continuations = DEFAULTS["max_final_continuations"]
    try:
        task_token_budget = int(raw.get("task_token_budget", DEFAULTS["task_token_budget"]))
    except (TypeError, ValueError):
        task_token_budget = DEFAULTS["task_token_budget"]
    # 0 是**合法的"不限"**，必须原样保留。实测踩过：这几行原本写成
    # `if x < 0 or x > 1000: x = DEFAULTS[...]`，而当时的 DEFAULTS 是 8/24——
    # 于是用户填 0 会被"修正"回 8，看起来像改了却没生效，比不改更难查。
    if max_agents < 0 or max_agents > 64:
        max_agents = DEFAULTS["max_agents"]
    if main_max_steps < 0 or main_max_steps > 1000:
        main_max_steps = DEFAULTS["main_max_steps"]
    if child_max_steps < 0 or child_max_steps > 1000:
        child_max_steps = DEFAULTS["child_max_steps"]
    if max_nudges < 0 or max_nudges > 20:
        max_nudges = DEFAULTS["max_nudges"]
    if max_final_continuations < 0 or max_final_continuations > 20:
        max_final_continuations = DEFAULTS["max_final_continuations"]
    if task_token_budget < 0 or task_token_budget > 10_000_000:
        task_token_budget = DEFAULTS["task_token_budget"]
    return {
        "max_agents": max_agents,
        "main_max_steps": main_max_steps,
        "child_max_steps": child_max_steps,
        "max_nudges": max_nudges,
        "max_final_continuations": max_final_continuations,
        "task_token_budget": task_token_budget,
        "local_auto_limit": raw.get("local_auto_limit", True) is not False,
    }


def remaining_agent_capacity(max_agents: int, dispatched: int) -> int | None:
    """Return remaining dispatch slots; ``None`` means the user chose unlimited."""
    limit = max(0, int(max_agents))
    used = max(0, int(dispatched))
    return None if limit == 0 else max(0, limit - used)


def get_settings() -> dict[str, Any]:
    try:
        from config import get_runtime, load_state
        value = get_runtime(SETTING_KEY)
        if value is None:
            value = load_state(SETTING_KEY, DEFAULTS)
        return normalize_settings(value)
    except Exception:
        return dict(DEFAULTS)


def gpu_snapshot() -> dict[str, Any]:
    """Best-effort local NVIDIA usage; remote inference hosts are not visible here."""
    try:
        import gpu_coordinator
        memory = gpu_coordinator.memory_info()
        processes = gpu_coordinator.process_status()
    except Exception:
        memory, processes = None, {"available": False, "compute_apps": []}
    if not memory:
        return {
            "available": False,
            "scope": "local_host",
            "gpus": [],
            "compute_apps": [],
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
        }
    return {
        "available": True,
        "scope": "local_host",
        "gpus": [
            {key: gpu.get(key) for key in
             ("index", "name", "used_mb", "total_mb", "utilization")}
            | {"free_mb": max(0, int(gpu.get("total_mb", 0)) - int(gpu.get("used_mb", 0)))}
            for gpu in memory.get("gpus", [])
        ],
        "compute_apps": [
            {key: app.get(key) for key in ("pid", "process_name", "used_mb")}
            for app in processes.get("compute_apps", [])
        ],
        "total_mb": int(memory.get("total_mb", 0)),
        "used_mb": int(memory.get("used_mb", 0)),
        "free_mb": int(memory.get("free_mb", 0)),
    }


def local_model_memory_snapshot(provider: str, base_url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Read model-level VRAM from local Ollama when its native API is reachable."""
    if str(provider or "").lower() != "ollama":
        return {"available": False, "provider": str(provider or ""), "models": []}
    try:
        parts = urlsplit(base_url)
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        endpoint = urlunsplit((parts.scheme, parts.netloc, path + "/api/ps", "", ""))
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return {"available": False, "provider": "ollama", "models": []}
        return {
            "available": True,
            "provider": "ollama",
            "models": [
                {"name": str(item.get("name") or item.get("model") or ""),
                 "size_vram_bytes": max(0, int(item.get("size_vram") or 0)),
                 "size_bytes": max(0, int(item.get("size") or 0))}
                for item in models if isinstance(item, dict)
            ],
        }
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return {"available": False, "provider": "ollama", "models": []}


__all__ = ["DEFAULTS", "SETTING_KEY", "get_settings", "gpu_snapshot",
           "local_model_memory_snapshot", "normalize_settings", "remaining_agent_capacity"]
