"""Conservative scheduling policy for local LLM providers.

Local OpenAI-compatible servers usually share one loaded model and one GPU/CPU
memory pool.  Creating several cloned clients does not create useful capacity;
it mostly increases queueing, context memory, and the chance of timeout/OOM.
This module keeps the policy in one place so orchestration and delegation make
the same decision.
"""
from __future__ import annotations

import os
import contextvars
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LocalResourceProfile:
    provider: str
    model: str
    local: bool
    tier: str
    max_parallel: int
    max_subagents: int
    recommended_roles: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "local": self.local,
            "tier": self.tier,
            "max_parallel": self.max_parallel,
            "max_subagents": self.max_subagents,
            "recommended_roles": list(self.recommended_roles),
            "reason": self.reason,
        }


_LOCAL_PROVIDERS = {"ollama", "llamacpp"}
_UNBOUNDED = 2**31 - 1
# 等待本地推理槽的上限（秒）。>0 为有界等待；0 = 无限等待（旧行为，失衡时会永久阻塞）。
# 背景：本地 provider 全局只有一个 BoundedSemaphore(1) 槽位，任何一次 acquire/release
# 失衡或上一个生成长期不返回，都会让后续每一个请求在"首次 LLM 调用之前"无限阻塞，
# 表现为「0 次 LLM 调用、静默挂死到客户端断开」（实测有过 844s 的一轮）。
_SLOT_WAIT_SECONDS = float(os.getenv("DOCMIND_LOCAL_SLOT_TIMEOUT", "180") or 0)
_SEMAPHORES: dict[tuple[str, str, int], BoundedSemaphore] = {}
# 诊断用：key -> {"tag": 用途@线程名, "since": 开始持有时刻}。仅用于"卡住时点名持有者"。
_HOLDERS: dict[tuple[str, str, int], dict] = {}
# 诊断计数：等槽超时被放行的次数（>0 即说明确实发生过"槽位被长期占用"）。
_STATS = {"wait_exceeded": 0}
_SEMAPHORE_LOCK = Lock()
_HELD_SLOTS: contextvars.ContextVar[set[tuple[str, str, int]]] = contextvars.ContextVar(
    "docmind_held_local_slots", default=set())


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 16) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _tier(model: str) -> str:
    name = (model or "").lower()
    if any(hint in name for hint in ("35b", "35b-a3b", "36b")):
        return "large"
    if any(hint in name for hint in ("14b", "13b", "16b")):
        return "medium"
    if any(hint in name for hint in ("7b", "8b", "9b", "deepseek-r1")):
        return "small"
    if any(hint in name for hint in ("1b", "2b", "3b", "4b")):
        return "tiny"
    return "unknown"


def _local_endpoint(provider: str, base_url: str | None = None) -> bool:
    try:
        if base_url:
            url = base_url
        else:
            from config import LLM_PROVIDER, PROVIDERS, get_runtime
            selected = str(get_runtime("llm_provider") or LLM_PROVIDER).lower()
            if selected != provider:
                return False
            url = str(get_runtime("llm_base_url") or PROVIDERS.get(provider, {}).get("base_url", ""))
        return (urlsplit(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    except Exception:
        return True


def _detected_parallel_capacity(tier: str) -> int | None:
    """Estimate additional local inference slots from current NVIDIA free VRAM.

    This is a scheduling estimate, not a guarantee: context/KV-cache sizes and
    remote cluster nodes are not observable through the local NVIDIA probe.
    """
    try:
        from agent_runtime.capacity import gpu_snapshot
        snapshot = gpu_snapshot()
        if not snapshot.get("available"):
            return None
        reserve_mb = {"tiny": 2048, "small": 3072, "medium": 5120,
                      "large": 8192, "unknown": 4096}.get(tier, 4096)
        free_mb = max(0, int(snapshot.get("free_mb", 0)))
        return max(1, min(16, 1 + free_mb // reserve_mb))
    except Exception:
        return None


def resource_profile(provider: str, model: str, base_url: str | None = None) -> LocalResourceProfile:
    """Return a conservative model resource profile.

    Local hardware readings provide a best-effort concurrency estimate. The
    persisted user policy caps task count; remote local-model endpoints cannot
    be inspected and therefore use the user's cap without a local VRAM limit.
    """
    p, m = (provider or "").strip().lower(), (model or "").strip()
    if p not in _LOCAL_PROVIDERS:
        return LocalResourceProfile(
            p, m, False, "cloud", 16, 16,
            ("researcher", "coder", "reviewer", "tester"),
            "非本地 provider，不应用本地推理槽位限制",
        )

    tier = _tier(m)
    # ``max_parallel`` is the memory/concurrency guard.  ``max_subagents``
    # is the total number of bounded DAG tasks, which may still run one after
    # another.  A tiny model therefore keeps one active generation but can
    # complete a small design -> implementation -> verification workflow.
    defaults = {
        "tiny": (1, 3, ("dispatcher", "designer", "coder", "tester", "reviewer")),
        "small": (1, 4, ("researcher", "coder", "tester", "reviewer")),
        "medium": (1, 4, ("researcher", "coder", "reviewer", "tester")),
        "large": (1, 4, ("dispatcher", "designer", "coder", "reviewer", "tester")),
        "unknown": (1, 3, ("dispatcher", "designer", "tester", "reviewer")),
    }
    default_parallel, default_subagents, roles = defaults[tier]
    try:
        from agent_runtime.capacity import get_settings
        settings = get_settings()
    except Exception:
        settings = {"max_agents": default_subagents, "local_auto_limit": True}
    endpoint_local = _local_endpoint(p, base_url)
    env_parallel = os.getenv("DOCMIND_LOCAL_LLM_MAX_CONCURRENCY")
    if env_parallel is not None:
        parallel = _env_int("DOCMIND_LOCAL_LLM_MAX_CONCURRENCY", default_parallel)
    elif settings.get("local_auto_limit") and endpoint_local:
        parallel = _detected_parallel_capacity(tier) or default_parallel
    else:
        parallel = max(1, int(settings.get("max_agents") or _UNBOUNDED))

    env_agents = os.getenv("DOCMIND_LOCAL_SUBAGENT_MAX")
    if env_agents is not None:
        subagents = _env_int("DOCMIND_LOCAL_SUBAGENT_MAX", default_subagents)
    else:
        configured = int(settings.get("max_agents", 0))
        # 本地模型**不**跟随"默认不限"：它们共用同一个推理服务（上面 max_parallel 已被
        # 钳到 1），不限子代理数会让任务无上限地排进同一个队列——显存不会被并发撑爆，
        # 但队列会越积越长、用户看到的是"卡住"而不是"在跑"。
        # 所以本地档位保留按模型体量推出的默认值；用户显式填正数仍然生效。
        # 这条边界要写在这里而不是藏起来：它与云端 provider 的"0=不限"不同。
        subagents = configured if configured > 0 else default_subagents
    return LocalResourceProfile(
        p, m, True, tier, parallel, subagents, roles,
        "本地模型共享同一推理服务，默认串行生成以避免显存/上下文争抢",
    )


def effective_parallelism(provider: str, model: str, requested: int | None,
                           base_url: str | None = None) -> int:
    """Clamp an orchestration request to the provider/model resource profile."""
    requested_value = max(1, int(requested or _UNBOUNDED))
    profile = resource_profile(provider, model, base_url)
    return min(requested_value, profile.max_parallel) if profile.local else requested_value


def effective_subagent_limit(provider: str, model: str, requested: int | None,
                              base_url: str | None = None) -> int:
    requested_value = max(1, int(requested or _UNBOUNDED))
    profile = resource_profile(provider, model, base_url)
    return min(requested_value, profile.max_subagents) if profile.local else requested_value


@contextmanager
def local_llm_slot(provider: str, model: str, purpose: str = "", base_url: str | None = None):
    """Serialize local generations even when callers use independent clients.

    ``purpose`` 只用于诊断：卡住时可点名"谁"占着槽（用途@线程名）。
    """
    profile = resource_profile(provider, model, base_url)
    if not profile.local:
        yield
        return
    key = (profile.provider, profile.model, profile.max_parallel)
    held = _HELD_SLOTS.get()
    if key in held:
        # _run_child holds the slot around the whole child loop.  LLMClient
        # also uses this context manager for direct requests; make that nesting
        # re-entrant so the two protection layers cannot deadlock at capacity 1.
        yield
        return
    tag = f"{purpose or 'llm'}@{threading.current_thread().name}"
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.setdefault(key, BoundedSemaphore(profile.max_parallel))
    if _SLOT_WAIT_SECONDS > 0:
        acquired = semaphore.acquire(timeout=_SLOT_WAIT_SECONDS)
    else:
        semaphore.acquire()          # 旧行为：无限等待
        acquired = True
    if not acquired:
        # 槽位被上一个本地生成长期占用（失衡/卡死）。超时后**放行本次**而不是继续
        # 无限等待：本地推理服务端自身会排队，宁可轻微过订阅，也绝不能让整轮请求
        # 在第一次 LLM 调用前静默挂死到客户端断开。
        _STATS["wait_exceeded"] = int(_STATS.get("wait_exceeded", 0)) + 1
        with _SEMAPHORE_LOCK:
            holder = dict(_HOLDERS.get(key) or {})
        who = holder.get("tag") or "未知持有者"
        wait_note = (f"（已持有 {time.time() - holder['since']:.0f}s）"
                     if holder.get("since") else "")
        warnings.warn(
            f"[local_llm_slot] 等待 {profile.provider}/{profile.model} 本地推理槽超过 "
            f"{_SLOT_WAIT_SECONDS:.0f}s，已放行本次以免请求永久阻塞。"
            f"当前持有者：{who}{wait_note}。"
        )
        # 仍要登记 _HELD_SLOTS（但不 acquire、因此也不 release），否则嵌套的
        # local_llm_slot（子代理/非流式）会在同一 key 上再等待一次、把超时叠加。
        token = _HELD_SLOTS.set(set(held) | {key})
        try:
            yield
        finally:
            _HELD_SLOTS.reset(token)
        return
    with _SEMAPHORE_LOCK:
        _HOLDERS[key] = {"tag": tag, "since": time.time()}
    token = _HELD_SLOTS.set(set(held) | {key})
    try:
        yield
    finally:
        _HELD_SLOTS.reset(token)
        with _SEMAPHORE_LOCK:
            if (_HOLDERS.get(key) or {}).get("tag") == tag:
                _HOLDERS.pop(key, None)
        semaphore.release()


def slot_holders() -> dict:
    """诊断：当前每个本地推理槽的持有者（tag / 已持有秒数）。供 workbench 排障。"""
    now = time.time()
    with _SEMAPHORE_LOCK:
        return {f"{key[0]}/{key[1]}": {
            "tag": value.get("tag"),
            "held_seconds": round(now - value.get("since", now), 1),
        } for key, value in _HOLDERS.items()}


def set_slot_wait_seconds(seconds) -> float:
    """运行时调整"等本地推理槽"的上限（秒）；<=0 表示无限等待（旧行为）。"""
    global _SLOT_WAIT_SECONDS
    _SLOT_WAIT_SECONDS = max(0.0, float(seconds or 0))
    return _SLOT_WAIT_SECONDS


def slot_status() -> dict:
    """本地推理槽诊断快照：等待上限、超时放行次数、当前持有者。

    供 workbench 的 GPU 面板展示：卡住时能直接看到"谁占着槽、占了多久"。
    """
    return {
        "wait_timeout_seconds": _SLOT_WAIT_SECONDS,
        "wait_exceeded": int(_STATS.get("wait_exceeded", 0)),
        "holders": slot_holders(),
    }


__all__ = [
    "LocalResourceProfile",
    "resource_profile",
    "effective_parallelism",
    "effective_subagent_limit",
    "local_llm_slot",
    "slot_holders",
    "slot_status",
    "set_slot_wait_seconds",
]
