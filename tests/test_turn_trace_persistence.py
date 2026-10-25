"""逐轮「推理轨迹」旁路持久化测试。

覆盖：turn_id 稳定继承、轨迹全量落盘（不截断）、孤儿回收、静默降级。
用 tmp 目录重定向 SESSIONS_DIR / TURN_TRACE_DIR，绝不碰真实会话数据。
"""
import json
import os

import pytest

import sessions


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    sess_dir = tmp_path / "sessions"
    trace_dir = tmp_path / "turn_traces"
    sess_dir.mkdir()
    monkeypatch.setattr(sessions, "SESSIONS_DIR", str(sess_dir))
    monkeypatch.setattr(sessions, "TURN_TRACE_DIR", str(trace_dir))
    yield


def _turn_id_of(session_id, idx=0):
    data = sessions.load(session_id)
    return data["turns"][idx]["turn_id"]


def test_save_assigns_stable_turn_id_across_rewrites():
    sid = "s-trace-1"
    # 第一次落盘：agent 内存 history 不带 turn_id
    sessions.save(sid, [{"user": "问题A", "assistant": "回答A"}])
    tid1 = _turn_id_of(sid)
    assert tid1 and isinstance(tid1, str)

    # 第二次 save：模拟 agent 用「不含 turn_id 的整段内存 history」整体重写，
    # 追加一轮新问题。同一轮（问题A/回答A）的 turn_id 必须保持不变。
    sessions.save(sid, [
        {"user": "问题A", "assistant": "回答A"},   # 无 turn_id，应按签名继承
        {"user": "问题B", "assistant": "回答B"},
    ])
    data = sessions.load(sid)
    assert data["turns"][0]["turn_id"] == tid1, "同一轮 turn_id 在二次 save 后漂移了"
    assert data["turns"][1]["turn_id"] != tid1, "新轮应 mint 不同的 turn_id"


def test_attach_turn_trace_roundtrips_full_untruncated_text():
    sid = "s-trace-2"
    long_obs = "X" * 50000  # 全量旁路：正文不得被截断
    steps = [
        {"type": "thought", "text": "先看结构"},
        {"type": "action", "text": "run_command(python -c 'print(1)')"},
        {"type": "observation", "text": long_obs, "ok": True},
        {"type": "reflection", "text": "结果符合预期"},
    ]
    sessions.save(sid, [{"user": "跑个命令", "assistant": "好的，结果是 1"}])
    assert sessions.attach_turn_trace(sid, "跑个命令", "好的，结果是 1", steps) is True

    store = sessions.load_turn_traces(sid)
    tid = _turn_id_of(sid)
    assert tid in store
    got = store[tid]
    assert [s["type"] for s in got] == ["thought", "action", "observation", "reflection"]
    assert len(got[2]["text"]) == 50000, "全量旁路不应截断 observation 正文"
    assert got[2]["ok"] is True


def test_attach_matches_last_turn_by_user():
    sid = "s-trace-3"
    sessions.save(sid, [
        {"user": "重复问题", "assistant": "第一次答"},
        {"user": "别的", "assistant": "中间"},
        {"user": "重复问题", "assistant": "第三次答"},
    ])
    steps = [{"type": "action", "text": "grep foo"}]
    assert sessions.attach_turn_trace(sid, "重复问题", "", steps) is True
    store = sessions.load_turn_traces(sid)
    # 命中的应是最后一条「重复问题」轮（第三次答）
    last_tid = sessions.load(sid)["turns"][2]["turn_id"]
    assert last_tid in store and store[last_tid][0]["text"] == "grep foo"


def test_orphan_trace_pruned_when_turn_removed():
    sid = "s-trace-4"
    sessions.save(sid, [
        {"user": "保留", "assistant": "r1"},
        {"user": "将删", "assistant": "r2"},
    ])
    tid_keep = sessions.load(sid)["turns"][0]["turn_id"]
    tid_del = sessions.load(sid)["turns"][1]["turn_id"]
    sessions.save_turn_trace(sid, tid_keep, [{"type": "action", "text": "a"}])
    sessions.save_turn_trace(sid, tid_del, [{"type": "action", "text": "b"}])
    assert set(sessions.load_turn_traces(sid)) == {tid_keep, tid_del}

    # 删掉第二轮后再写轨迹，应顺带回收已不存在的 tid_del
    sessions.save(sid, [{"user": "保留", "assistant": "r1"}])
    sessions.save_turn_trace(sid, tid_keep, [{"type": "action", "text": "a2"}])
    store = sessions.load_turn_traces(sid)
    assert tid_keep in store
    assert tid_del not in store, "被删轮的孤儿轨迹应被回收"


def test_attach_missing_turn_is_noop_and_never_raises():
    sid = "s-trace-5"
    sessions.save(sid, [{"user": "只有这一轮", "assistant": "答"}])
    # 匹配不到 user → 返回 False，不抛
    assert sessions.attach_turn_trace(sid, "根本不存在的问题", "", [{"type": "action", "text": "x"}]) is False
    # 空 steps → False
    assert sessions.attach_turn_trace(sid, "只有这一轮", "答", []) is False


def test_load_turn_traces_missing_returns_empty():
    assert sessions.load_turn_traces("no-such-session-xyz") == {}
