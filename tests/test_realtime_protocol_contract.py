"""R0 实时协议契约测试（任务 R11）。

本文件**只读** `agent_runtime/realtime_protocol.py`、`frontend/src/workbench/realtimeProtocol.ts`
和 R4 的 `agent_runtime/realtime_provider.py`，不修改任何实现。它把线上契约钉死在三个
互相独立的地方，任何一处漂移都会让这里变红：

1. 服务端事件词汇表 == 前端 `RealtimeServerEvent` 联合类型；
2. 客户端控制词汇表 == 前端 `RealtimeClientControl` 联合类型；
3. R4 的 `RealtimeEvent.to_wire()` 产物必须是 R0 声明过的合法服务端事件。

"已知缺口"一节的测试断言的是**当前**行为，不是期望行为：它们记录 R0 实现里已经上报、
但按分工不由 R11 修改的问题。R0 修好之后这些测试会失败，届时应当把它们翻转成严格断言。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_runtime import realtime_protocol as proto
from agent_runtime import realtime_provider as provider

_TS_PROTOCOL = (Path(__file__).resolve().parents[1] / "frontend" / "src"
                / "workbench" / "realtimeProtocol.ts")

# 冻结时钟，让 captured_at 窗口边界可复现。
_FROZEN_MS = 1_800_000_000_000
_FROZEN_SECONDS = _FROZEN_MS / 1000


def _frozen_clock():
    return patch.object(proto.time, "time", return_value=_FROZEN_SECONDS)


def _ts_source() -> str:
    if not _TS_PROTOCOL.exists():  # pragma: no cover - 仓库布局被破坏时才走到
        pytest.fail(f"找不到前端协议文件：{_TS_PROTOCOL}")
    return _TS_PROTOCOL.read_text(encoding="utf-8")


def _ts_segment(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"前端协议缺少 {start_marker!r}；协议改动后请同步本契约测试"
    end = source.find(end_marker, start + len(start_marker))
    assert end > start, f"前端协议缺少 {end_marker!r}；协议改动后请同步本契约测试"
    return source[start:end]


def _ts_literals(segment: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", segment))


def _server_event_union_segment(source: str) -> str:
    """只截 `RealtimeServerEvent` 声明本体（到它自己的 `sent_at: number` 信封字段为止）。

    早先这里用 `end_marker="export function realtimeHello"` 圈整段，会把紧随其后、
    夹在类型定义与该函数之间的 `HelloOkEvent` 接口一起扫进来——它的
    `mode: 'native-realtime' | 'sampled-frames' | 'unavailable'` 是【实时模式取值】、
    不是服务端事件类型。严格相等断言因此被这三个假阳性撞红，看着像前后端词汇表漂移，
    其实是测试把窗口开太宽。真正要钉的是 `type:` 那一行的联合类型，
    所以收到紧邻其下的 `sent_at: number` 为止。
    """
    return _ts_segment(source, "export type RealtimeServerEvent", "sent_at: number")


def _binary_packet(payload: bytes = b"jpeg-frame", *, version: int = 1,
                   kind: str = "video.frame", sequence: object = 1,
                   captured_at: object = None, **extra: object) -> bytes:
    """按前端 `encodeVideoFrame` 的写法构造二进制包：JSON 头 + \\n + 载荷。"""
    header = {"v": version, "type": kind, "sequence": sequence,
              "captured_at": _FROZEN_MS if captured_at is None else captured_at}
    header.update(extra)
    return json.dumps(header).encode("utf-8") + b"\n" + payload


# ---- 词汇表一致性：Python 协议 ↔ 前端 TypeScript -----------------------------

def test_protocol_version_matches_the_frontend_constant():
    source = _ts_source()
    match = re.search(r"REALTIME_PROTOCOL_VERSION\s*=\s*(\d+)", source)
    assert match, "前端协议缺少 REALTIME_PROTOCOL_VERSION；协议改动后请同步本契约测试"
    assert proto.PROTOCOL_VERSION == int(match.group(1)), (
        "Python 与前端协议版本号不一致：网关会静默拒绝前端全部控制包")


def test_server_event_vocabulary_matches_the_frontend_union():
    segment = _server_event_union_segment(_ts_source())
    assert _ts_literals(segment) == proto.SERVER_TYPES, (
        "服务端事件词汇表与前端 RealtimeServerEvent 联合类型不一致")


def test_client_control_vocabulary_matches_the_frontend_union():
    segment = _ts_segment(_ts_source(), "export type RealtimeClientControl",
                          "export type RealtimeServerEvent")
    assert _ts_literals(segment) == proto.CONTROL_TYPES, (
        "客户端控制词汇表与前端 RealtimeClientControl 联合类型不一致")


def test_media_types_match_the_frontend_capability_handshake():
    source = _ts_source()
    hello = _ts_segment(source, "export function realtimeHello", "export function realtimeHeartbeat")
    advertised = set(re.findall(r"'([^']+)'", _ts_segment(hello, "capabilities: [", "]")))
    assert proto.MEDIA_TYPES <= advertised, (
        f"前端 hello 声明的能力 {sorted(advertised)} 没有覆盖协议媒体类型 "
        f"{sorted(proto.MEDIA_TYPES)}")
    assert "video.frame" in _ts_segment(source, "export function encodeVideoFrame",
                                        "export function parseRealtimeServerEvent"), (
        "前端 encodeVideoFrame 写出的 type 必须仍是协议里的 video.frame")


# ---- R4 → R0 的适配器契约 ----------------------------------------------------

def test_every_provider_event_kind_maps_onto_a_declared_server_event():
    assert set(provider.WIRE_BY_KIND) == set(provider.EVENT_KINDS), (
        "R4 事件种类与线上映射表脱节：适配器会发出网关不认的事件")
    assert set(provider.WIRE_BY_KIND.values()) <= proto.SERVER_TYPES, (
        "R4 映射出了 R0 未声明的服务端事件")


def test_model_observation_is_a_declared_server_event():
    assert "model.observation" in proto.SERVER_TYPES
    assert "model.observation" in _ts_literals(_server_event_union_segment(_ts_source()))


@pytest.mark.parametrize("kind", provider.EVENT_KINDS)
def test_to_wire_output_is_a_valid_r0_server_event(kind):
    with _frozen_clock():
        event = provider.RealtimeEvent(kind=kind, captured_at=_FROZEN_MS, sequence=7,
                                       session_id="sess-1", payload={"text": "你好"})
        wire = event.to_wire(sent_at=_FROZEN_MS)

        # server_event 自己盖 sent_at，所以要冻结时钟才能逐字段比对。
        rebuilt = proto.server_event(
            wire["type"], sequence=wire.get("sequence"), captured_at=wire.get("captured_at"),
            session_id=wire.get("session_id"),
            **{k: v for k, v in wire.items()
               if k not in ("v", "type", "sent_at", "sequence", "captured_at", "session_id")})
    assert rebuilt == wire, "to_wire 的产物与 server_event 不能互相还原：适配器会写出网关不认的包"


def test_to_wire_never_lets_a_payload_clobber_the_envelope():
    event = provider.RealtimeEvent(kind=provider.EVENT_ERROR,
                                   sequence=7, captured_at=_FROZEN_MS,
                                   session_id="trusted",
                                   payload={"v": 99, "type": "evil", "sent_at": 1,
                                            "sequence": 999, "captured_at": 1,
                                            "session_id": "evil"})
    wire = event.to_wire(sent_at=_FROZEN_MS)
    assert wire["v"] == 1 and wire["type"] == "error" and wire["sent_at"] == _FROZEN_MS
    assert wire["sequence"] == 7
    assert wire["captured_at"] == _FROZEN_MS
    assert wire["session_id"] == "trusted"


# ---- 二进制媒体包 ------------------------------------------------------------

def test_accepts_a_canonical_frontend_video_packet():
    with _frozen_clock():
        header, payload = proto.parse_binary_packet(_binary_packet(b"jpeg", focused=True))
    assert payload == b"jpeg"
    assert header == {"v": 1, "type": "video.frame", "sequence": 1,
                      "captured_at": _FROZEN_MS, "focused": True}


@pytest.mark.parametrize("raw, reason", [
    (b"", "空包"),
    (b"ab", "长度不足 3 字节"),
    (b"no-newline-separator", "缺少头部/载荷分隔符"),
    (b'{"v":1,"type":"video.frame","sequence":1,"captured_at":1}\n', "载荷为空"),
    (b"\xff\xfe\nx", "头部不是合法 UTF-8"),
    (b"{not json\nx", "头部不是合法 JSON"),
    (b'["v",1]\nx', "头部不是对象"),
    (b'{"v":2,"type":"video.frame","sequence":1,"captured_at":1}\n' + b"x", "协议版本漂移"),
    (b'{"v":1,"type":"video.unknown","sequence":1,"captured_at":1}\n' + b"x", "未声明的媒体类型"),
    (b'{"v":1,"type":"hello","sequence":1,"captured_at":1}\n' + b"x", "控制类型走二进制通道"),
    (b'{"v":1,"type":"video.frame","captured_at":1}\n' + b"x", "缺少 sequence"),
    (b'{"v":1,"type":"video.frame","sequence":1}\n' + b"x", "缺少 captured_at"),
    (b'{"v":1,"type":"video.frame","sequence":-1,"captured_at":1}\n' + b"x", "sequence 为负"),
    (b'{"v":1,"type":"video.frame","sequence":1.5,"captured_at":1}\n' + b"x", "sequence 非整数"),
    (b'{"v":1,"type":"video.frame","sequence":1,"captured_at":"now"}\n' + b"x", "captured_at 非数值"),
])
def test_rejects_malformed_media_packets(raw, reason):
    with _frozen_clock():
        assert proto.parse_binary_packet(raw) is None, f"应当拒绝：{reason}"


def test_rejects_an_oversized_header():
    filler = b"x" * (proto.MAX_PACKET_HEADER + 1)
    raw = b"{" + filler + b'"v":1}\n' + b"payload"
    with _frozen_clock():
        assert proto.parse_binary_packet(raw) is None


@pytest.mark.parametrize("delta, accepted", [
    (-120_001, False),   # 早于窗口下界 1ms
    (-120_000, True),    # 恰好落在下界
    (0, True),
    (60_000, True),      # 恰好落在上界
    (60_001, False),     # 晚于窗口上界 1ms
])
def test_captured_at_window_is_inclusive_on_both_edges(delta, accepted):
    with _frozen_clock():
        packets = {delta: _binary_packet(captured_at=_FROZEN_MS + delta)}
        assert (proto.parse_binary_packet(packets[delta]) is not None) is accepted


def test_rejects_a_clock_that_is_absurdly_far_in_the_future():
    """前端时钟跑飞时网关必须拒绝，而不是把未来帧当作可信证据排进时间线。"""
    with _frozen_clock():
        assert proto.parse_binary_packet(_binary_packet(captured_at=_FROZEN_MS * 2)) is None


# ---- 控制包 ------------------------------------------------------------------

@pytest.mark.parametrize("packet", [
    {"v": 1, "type": "hello", "project_id": "p", "capabilities": []},
    {"v": 1, "type": "heartbeat", "sent_at": _FROZEN_MS},
    {"v": 1, "type": "cancel"},
    {"v": 1, "type": "session.close"},
])
def test_accepts_declared_control_packets(packet):
    assert proto.parse_control_packet(packet) == packet


@pytest.mark.parametrize("packet, reason", [
    ({"type": "hello"}, "缺少版本号"),
    ({"v": 2, "type": "hello"}, "协议版本漂移"),
    ({"v": 1, "type": "video.frame"}, "媒体类型走控制通道"),
    ({"v": 1, "type": "nope"}, "未声明的控制类型"),
    ("hello", "控制包不是对象"),
    (None, "控制包为空"),
])
def test_rejects_malformed_control_packets(packet, reason):
    assert proto.parse_control_packet(packet) is None, f"应当拒绝：{reason}"


# ---- 服务端事件 --------------------------------------------------------------

def test_unknown_server_event_is_a_programming_error():
    """拼错事件名必须当场炸，而不是悄悄发一个前端不认的 type 出去。"""
    with pytest.raises(ValueError):
        proto.server_event("video.frames")


def test_optional_envelope_fields_only_appear_when_set():
    bare = proto.server_event("hello.ok")
    assert set(bare) == {"v", "type", "sent_at"}
    assert bare["v"] == proto.PROTOCOL_VERSION

    full = proto.server_event("video.observation", sequence=0, captured_at=_FROZEN_MS,
                              session_id="sess-1", ok=True)
    assert full["sequence"] == 0, "sequence=0 是合法序号，不能因为是假值就被丢掉"
    assert full["captured_at"] == _FROZEN_MS
    assert full["session_id"] == "sess-1"
    assert full["ok"] is True


def test_compact_error_bounds_its_payload_and_defaults_to_non_retryable():
    event = proto.compact_error("x" * 200, "y" * 900)
    assert event["type"] == "error"
    assert len(event["code"]) == 80
    assert len(event["message"]) == 400
    assert event["retryable"] is False
    assert proto.compact_error("rate_limited", "稍后重试", retryable=True)["retryable"] is True


# ---- R0 信封与字段类型约束 ----------------------------------------------------

def test_bool_sequence_is_rejected_even_though_bool_is_an_int_subclass():
    with _frozen_clock():
        parsed = proto.parse_binary_packet(_binary_packet(sequence=True))
    assert parsed is None


def test_server_event_payload_cannot_clobber_the_envelope():
    event = proto.server_event("hello.ok", type="evil", v=2)
    assert event["type"] == "hello.ok"
    assert event["v"] == proto.PROTOCOL_VERSION
    assert isinstance(event["sent_at"], int)
