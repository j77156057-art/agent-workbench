"""Versioned wire contract for the autonomous cockpit realtime session.

Binary media packets use one UTF-8 JSON header followed by a newline and the
encoded payload. JSON control packets use the same ``v`` and ``type`` fields.
The gateway owns validation so provider adapters cannot silently invent a
second event vocabulary.
"""
from __future__ import annotations

import json
import time
from typing import Any

PROTOCOL_VERSION = 1
MAX_PACKET_HEADER = 512
MEDIA_TYPES = {"video.frame", "audio.chunk"}
CONTROL_TYPES = {"hello", "heartbeat", "cancel", "session.close", "agent.result"}
SERVER_TYPES = {"hello.ok", "heartbeat", "video.observation", "model.observation", "audio.transcript", "model.delta", "model.audio", "error", "cancel.ok", "session.closed", "agent.result.ok"}
_ENVELOPE_KEYS = {"v", "type", "sent_at", "sequence", "captured_at", "session_id"}


def _finite_timestamp(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    now = int(time.time() * 1000)
    return number if now - 120_000 <= number <= now + 60_000 else None


def parse_binary_packet(data: bytes) -> tuple[dict[str, Any], bytes] | None:
    if not isinstance(data, (bytes, bytearray)) or len(data) < 3:
        return None
    header, separator, payload = bytes(data).partition(b"\n")
    if not separator or len(header) > MAX_PACKET_HEADER or not payload:
        return None
    try:
        value = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("v") != PROTOCOL_VERSION:
        return None
    packet_type = value.get("type")
    if packet_type not in MEDIA_TYPES:
        return None
    captured_at = _finite_timestamp(value.get("captured_at"))
    sequence = value.get("sequence")
    if captured_at is None or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return None
    value["captured_at"] = captured_at
    return value, payload


def parse_control_packet(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("v") != PROTOCOL_VERSION:
        return None
    if value.get("type") not in CONTROL_TYPES:
        return None
    if value.get("type") == "agent.result":
        request_id, text = value.get("request_id"), value.get("text")
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 160
                or not isinstance(text, str) or not text.strip() or len(text) > 4000):
            return None
    return value


def server_event(event_type: str, *, sequence: int | None = None, captured_at: int | None = None,
                 session_id: str | None = None, **payload: Any) -> dict[str, Any]:
    if event_type not in SERVER_TYPES:
        raise ValueError(f"unsupported realtime server event: {event_type}")
    event: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": event_type,
                             "sent_at": int(time.time() * 1000)}
    if sequence is not None:
        event["sequence"] = sequence
    if captured_at is not None:
        event["captured_at"] = captured_at
    if session_id:
        event["session_id"] = session_id
    # The wire envelope is authoritative.  Provider payloads are untrusted and
    # must not be able to rewrite the protocol version, event type, or clock.
    event.update({key: value for key, value in payload.items()
                  if key not in _ENVELOPE_KEYS})
    return event


def compact_error(code: str, message: str, *, retryable: bool = False,
                  session_id: str | None = None) -> dict[str, Any]:
    return server_event("error", session_id=session_id, code=str(code)[:80],
                        message=str(message)[:400], retryable=retryable)
