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
    # 2026-10：默认改为 0（不限），与上面三项语义对齐 —— 长任务不再被纠偏次数掐断。
    # 此前是 2，理由是"防同一提示重复 8 遍"；但那条风险由 `_MAX_FORMAT_STALLS`
    # （格式连续失守即早退）、连续失败熔断、用户取消、截止时间各自兜住，
    # 不需要用"只给 2 次"这种粗粒度上限来兼任。用户可在设置页自己填正数收紧。
    "max_nudges": 0,               # 0 = 不限纠偏次数
    "max_final_continuations": 0,  # 0 = 不限续写次数
    "task_token_budget": 0,        # 0 = 不限 token
    "local_auto_limit": True,      # 本地模型显存自适应（不是步数上限，保留）
}

#: 各字段的上界。**这些数字同时是前端 input 的 `max`** ——
#: 两边一致性由 `tests/test_capacity_bounds_ui_backend_agree.py` 锁住。
#: 提成具名常量（而不是散在 `_clamp(x, 1000)` 里）的原因：调用点处看不出
#: 哪个数字对应哪个字段，而外部测试要能读到「后端声明的边界」才能与界面对照。
MAX_AGENTS_CAP = 64
MAX_STEPS_CAP = 1000
#: 上界 20 -> 1000：与 main_max_steps 对齐。原上界 20 会让"用户想设 50 次纠偏"
#: 被静默改回 DEFAULTS（现在是 0=不限），即"改了个大数反而变成不限"，行为不可预期。
MAX_NUDGES_CAP = 1000
MAX_TOKEN_BUDGET_CAP = 10_000_000


def normalize_settings(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化容量设置。

    ⚠⚠ **负值在这里 raise，不静默修正。** 但调用方要分两层看：

    * **请求边界**（`api.set_agent_capacity`）：负值 = 用户这次提交无效，
      应当 400 拒绝。这是 raise 的**预期用途**。
    * **读取持久化状态**（`config._apply_persisted_state` / `get_settings`）：
      状态文件里的负值会让 `normalize_settings` 抛异常，
      **开机恢复就会炸**。所以这两处必须 try 住并回落到 DEFAULTS。

    「越界」与「负值」也是两种处理：
    * 超过上界 -> **夹到上界**（用户想收紧，5000 -> 1000，语义仍是收紧）
    * 负值 -> **raise**（无法推断意图；夹到 0 反而变成「不限」，
      那是安全方向的反面 —— 手滑打 -1 的人拿到最宽松配置）
    * 非数字 / None -> 回落 DEFAULTS（无法解析，不是用户明确表达的值）
    """
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

    def _clamp(field: str, value: int, high: int) -> int:
        # 上界夹住；下界 raise（不夹到 0 —— 那等于「不限」）
        if value < 0:
            raise ValueError(
                f"{field} 不能为负（收到 {value}）；0 表示不限，正数表示上限")
        return min(int(value), high)

    max_agents = _clamp("max_agents", max_agents, MAX_AGENTS_CAP)
    main_max_steps = _clamp("main_max_steps", main_max_steps, MAX_STEPS_CAP)
    child_max_steps = _clamp("child_max_steps", child_max_steps, MAX_STEPS_CAP)
    max_nudges = _clamp("max_nudges", max_nudges, MAX_NUDGES_CAP)
    max_final_continuations = _clamp("max_final_continuations",
                                     max_final_continuations, MAX_NUDGES_CAP)
    task_token_budget = _clamp("task_token_budget", task_token_budget,
                               MAX_TOKEN_BUDGET_CAP)
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
