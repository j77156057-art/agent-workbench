"""Pluggable contract for native realtime multimodal providers (plan R4).

The cockpit must never hard-wire one vendor. Adapters register a factory here
and the gateway resolves one by name, so swapping DashScope Qwen-Omni for any
other realtime endpoint is a config change (``DOCMIND_REALTIME_PROVIDER``),
not a rewrite.

Design rules that came out of the plan review:

* **One vocabulary.** Adapters emit :class:`RealtimeEvent` only. Mapping to the
  wire protocol (``frontend/src/workbench/realtimeProtocol.ts`` +
  ``agent_runtime/realtime_protocol.py``, both owned by the R0 owner) happens in
  :meth:`RealtimeEvent.to_wire`, so a protocol bump touches exactly one place.
* **Degrade loudly.** :func:`resolve` returns ``ok=False`` with a reason and a
  ``degraded_to`` marker instead of pretending a realtime session exists. The
  caller falls back to the existing sampled-frame path and tells the user.
* **No vendor SDK.** Adapters speak WebSocket directly; a missing SDK or an
  unconfigured key is a reported state, never an import-time crash.
"""
from __future__ import annotations

import importlib
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ---- normalised event kinds -------------------------------------------------
# Keep in sync with WIRE_BY_KIND; the wire names are the R0 server event types.
EVENT_STATUS = "status"
EVENT_OBSERVATION = "observation"
EVENT_TRANSCRIPT = "transcript"
EVENT_TEXT_DELTA = "text_delta"
EVENT_AUDIO_DELTA = "audio_delta"
EVENT_DONE = "done"
EVENT_ERROR = "error"

EVENT_KINDS = (EVENT_STATUS, EVENT_OBSERVATION, EVENT_TRANSCRIPT,
               EVENT_TEXT_DELTA, EVENT_AUDIO_DELTA, EVENT_DONE, EVENT_ERROR)

WIRE_BY_KIND = {
    EVENT_STATUS: "hello.ok",
    EVENT_OBSERVATION: "video.observation",
    EVENT_TRANSCRIPT: "audio.transcript",
    EVENT_TEXT_DELTA: "model.delta",
    EVENT_AUDIO_DELTA: "model.audio",
    EVENT_DONE: "session.closed",
    EVENT_ERROR: "error",
}

# Capability flags a provider can advertise. The gateway uses them to decide
# whether interrupt / audio-out / video-in are offered in the UI at all.
CAP_AUDIO_IN = "audio.in"
CAP_VIDEO_IN = "video.in"
CAP_TEXT_OUT = "text.out"
CAP_AUDIO_OUT = "audio.out"
CAP_INTERRUPT = "interrupt"

DEGRADED_SAMPLED_FRAMES = "sampled-frames"
DEFAULT_PROVIDER_ENV = "DOCMIND_REALTIME_PROVIDER"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RealtimeEvent:
    """Vendor-neutral event flowing from a provider back to the gateway."""

    kind: str
    captured_at: int = 0
    sequence: int = 0
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown realtime event kind: {self.kind}")
        if self.captured_at <= 0:
            self.captured_at = _now_ms()

    def to_wire(self, *, version: int = 1, sent_at: int | None = None) -> dict[str, Any]:
        """Render as an R0 server event. Audio bytes stay in ``payload``."""
        event: dict[str, Any] = {
            "v": version,
            "type": WIRE_BY_KIND.get(self.kind, "error"),
            "sent_at": sent_at if sent_at is not None else _now_ms(),
        }
        if self.sequence:
            event["sequence"] = self.sequence
        if self.captured_at:
            event["captured_at"] = self.captured_at
        if self.session_id:
            event["session_id"] = self.session_id
        for key, value in self.payload.items():
            if key not in {"v", "type", "sent_at", "sequence", "captured_at", "session_id"}:
                event[key] = value
        return event


class RealtimeProvider:
    """Base class every realtime adapter implements.

    Subclasses own a background receive loop and push normalised events through
    :meth:`_emit`; ``poll``/``drain`` are the gateway's read side. Every method
    is expected to fail closed: return ``False`` / emit an error event rather
    than raising into the caller's stream.
    """

    name = "base"

    def __init__(self, *, session_id: str = "", project_id: str = "") -> None:
        self.session_id = session_id or f"rt-{int(time.time() * 1000):x}"
        self.project_id = project_id
        self._events: "queue.Queue[RealtimeEvent]" = queue.Queue()
        self._lock = threading.Lock()
        self._sequence = 0
        self._running = False

    # -- capability / availability -------------------------------------------
    def capabilities(self) -> list[str]:
        return []

    def availability(self) -> dict[str, Any]:
        """Why this adapter can or cannot run right now."""
        return {"ok": False, "provider": self.name,
                "reason": "未配置实时模型服务", "degraded_to": DEGRADED_SAMPLED_FRAMES}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # -- media input ----------------------------------------------------------
    def send_audio(self, pcm: bytes, captured_at: int = 0) -> bool:
        return False

    def send_frame(self, image: bytes, captured_at: int = 0) -> bool:
        return False

    def commit(self) -> bool:
        """Manual (non-VAD) mode: flush buffered audio and request a response."""
        return False

    def interrupt(self) -> bool:
        return False

    # -- read side ------------------------------------------------------------
    def poll(self, timeout: float = 0.0) -> RealtimeEvent | None:
        try:
            return self._events.get(timeout=timeout) if timeout else self._events.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit: int = 64) -> list[RealtimeEvent]:
        events: list[RealtimeEvent] = []
        while len(events) < limit:
            event = self.poll()
            if event is None:
                break
            events.append(event)
        return events

    # -- internal -------------------------------------------------------------
    def _emit(self, kind: str, *, captured_at: int = 0, **payload: Any) -> RealtimeEvent:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        event = RealtimeEvent(kind=kind, captured_at=captured_at or _now_ms(),
                              sequence=sequence, session_id=self.session_id, payload=payload)
        self._events.put(event)
        return event


# ---- registry ---------------------------------------------------------------
_FACTORIES: dict[str, Callable[[], RealtimeProvider]] = {}
# Adapters self-register on import; importing them here keeps callers from
# having to know which modules exist. A broken adapter is skipped, never fatal.
# `realtime_scripted` 是零网络的离线剧本适配器（选它需要显式设
# ``DOCMIND_REALTIME_PROVIDER=scripted``，不影响默认路径），它存在的目的是让
# "真页面 + 真麦克风 + 真 ws 协议"这一段能被自动测试跑到。
_BUILTIN_MODULES = ("agent_runtime.realtime_omni", "agent_runtime.realtime_scripted")
_AUTLOADED = False


def _autoload() -> None:
    global _AUTLOADED
    if _AUTLOADED:
        return
    _AUTLOADED = True
    for module in _BUILTIN_MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            pass


def register(name: str, factory: Callable[[], RealtimeProvider]) -> None:
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("provider name is required")
    _FACTORIES[key] = factory


def unregister(name: str) -> None:
    _FACTORIES.pop(str(name or "").strip().lower(), None)


def provider_names() -> list[str]:
    _autoload()
    return sorted(_FACTORIES)


def configured_name() -> str:
    return os.getenv(DEFAULT_PROVIDER_ENV, "").strip().lower()


def create(name: str = "") -> RealtimeProvider | None:
    """Instantiate a registered provider. Empty name falls back to env."""
    key = (name or configured_name()).strip().lower()
    if not key:
        return None
    _autoload()
    factory = _FACTORIES.get(key)
    return factory() if factory else None


def resolve(name: str = "") -> dict[str, Any]:
    """Pick a usable provider or explain the degradation.

    Returns ``{"ok": True, "provider": <instance>, ...}`` or
    ``{"ok": False, "reason": ..., "degraded_to": "sampled-frames"}``. Callers
    must surface ``reason`` to the user and must never fake a session.
    """
    requested = (name or configured_name()).strip().lower()
    if not requested:
        return {"ok": False, "provider": None, "name": "",
                "reason": f"未设置 {DEFAULT_PROVIDER_ENV}，未启用原生实时模型",
                "degraded_to": DEGRADED_SAMPLED_FRAMES,
                "available": provider_names()}
    provider = create(requested)
    if provider is None:
        return {"ok": False, "provider": None, "name": requested,
                "reason": f"未注册实时模型适配器：{requested}",
                "degraded_to": DEGRADED_SAMPLED_FRAMES,
                "available": provider_names()}
    state = provider.availability() or {}
    if not state.get("ok"):
        result = {"ok": False, "provider": None, "name": requested,
                  "reason": str(state.get("reason") or "实时模型不可用"),
                  "degraded_to": state.get("degraded_to") or DEGRADED_SAMPLED_FRAMES,
                  "available": provider_names()}
        return result
    return {"ok": True, "provider": provider, "name": requested,
            "capabilities": provider.capabilities(),
            "degraded_to": None}


def describe() -> dict[str, Any]:
    """Static view used by status endpoints and the cockpit model bar."""
    return {"configured": configured_name(), "available": provider_names(),
            "degraded_to": DEGRADED_SAMPLED_FRAMES if not configured_name() else None}
