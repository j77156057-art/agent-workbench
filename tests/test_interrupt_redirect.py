"""④ interrupt-and-redirect：后端串行化 + 重定向注入的单元测试。

覆盖：
1. 同会话两条并发 run 经 _session_run_scope 严格串行（不重叠），且注册表自清理；
2. prev 指向自身线程时不会自 join 死锁；
3. 同会话已有活跃 run 时，chat() 注入重定向系统提示（确定性，无需真实并发）；
4. 空闲会话不注入重定向提示（对照）。
"""
import threading
import time
import uuid

import api
from api import (
    _ACTIVE_RUN,
    _session_run_scope,
)


def _unique_key():
    return "test-redir-" + uuid.uuid4().hex


def test_session_run_scope_serializes_same_session():
    """两条并发 produce 模拟线程进入同一 key 的 scope：第二条必须等第一条退到安全边界。"""
    key = _unique_key()
    log = []

    def produce(idx, hold_s):
        ev_stop = threading.Event()
        me = threading.current_thread()
        with _session_run_scope(key, ev_stop, me):
            t = time.monotonic()
            log.append(("enter", idx, t))
            time.sleep(hold_s)
            log.append(("exit", idx, time.monotonic()))

    t1 = threading.Thread(target=produce, args=(1, 0.20))
    t2 = threading.Thread(target=produce, args=(2, 0.05))
    t1.start()
    time.sleep(0.03)          # 让 t1 先进入临界区
    t2.start()
    t1.join()
    t2.join()

    enters = {i: t for (k, i, t) in log if k == "enter"}
    exits = {i: t for (k, i, t) in log if k == "exit"}
    # 关键不变量：两条 run 不重叠 —— 第二条进入前第一条必须已退出。
    assert enters[2] >= exits[1], f"两条 run 重叠：{log}"
    # 注册表自清理：退出后该 key 不应残留（除非有遗漏的活跃 run）。
    assert key not in _ACTIVE_RUN, f"注册表未清理：{list(_ACTIVE_RUN)}"


def test_session_run_scope_self_join_no_deadlock():
    """prev 指向自身线程时，scope 必须跳过 join（否则自锁死）。"""
    key = _unique_key()
    ev = threading.Event()
    # 模拟“活跃 run”的线程就是当前线程自己。
    _ACTIVE_RUN[key] = {"stop": ev, "thread": threading.current_thread()}
    # 若 guard 失效，这里会死锁（join 自身）→ 测试超时失败。
    with _session_run_scope(key, ev, threading.current_thread()):
        pass
    assert key not in _ACTIVE_RUN


def test_redirect_hint_injected_when_active_run():
    """同会话已有活跃 run 时，chat() 必须把重定向提示注入 system_context。"""
    import os
    import tempfile
    import config
    from unittest.mock import patch
    from starlette.testclient import TestClient

    key = _unique_key()
    captured = []
    dummy = threading.Event()

    def fake_run(self, *args, **kwargs):
        captured.append(kwargs.get("system_context"))
        yield {"type": "final", "text": "ok"}

    # 注册一个仍“活着”的线程，模拟正在跑的旧 run（scope 会 join 它）。
    holder = threading.Thread(target=lambda: time.sleep(2.0), daemon=True)
    holder.start()
    _ACTIVE_RUN[key] = {"stop": dummy, "thread": holder}

    prev_root = config.get_runtime("code_root")
    old_provider = config.get_runtime("llm_provider")
    config.set_runtime("llm_provider", "ollama")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config.set_runtime("code_root", tmp)
            with patch.object(api, "check_ollama", return_value={"reachable": True, "guidance": ""}), \
                 patch.object(api.Agent, "run", side_effect=fake_run):
                client = TestClient(api.app)
                resp = client.post("/api/chat", data={"question": "改一下", "session_id": key})
        assert resp.status_code == 200, resp.text
        assert captured, "fake_run 未被调用"
        joined = " ".join(str(c) for c in captured)
        assert "【用户中途重定向】" in joined, f"重定向提示未注入：{joined}"
    finally:
        config.set_runtime("code_root", prev_root or "")
        config.set_runtime("llm_provider", old_provider or "")
        _ACTIVE_RUN.pop(key, None)


def test_redirect_hint_absent_when_idle():
    """空闲会话（无活跃 run）不应注入重定向提示。"""
    import tempfile
    import config
    from unittest.mock import patch
    from starlette.testclient import TestClient

    key = _unique_key()
    captured = []

    def fake_run(self, *args, **kwargs):
        captured.append(kwargs.get("system_context"))
        yield {"type": "final", "text": "ok"}

    prev_root = config.get_runtime("code_root")
    old_provider = config.get_runtime("llm_provider")
    config.set_runtime("llm_provider", "ollama")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config.set_runtime("code_root", tmp)
            with patch.object(api, "check_ollama", return_value={"reachable": True, "guidance": ""}), \
                 patch.object(api.Agent, "run", side_effect=fake_run):
                client = TestClient(api.app)
                resp = client.post("/api/chat", data={"question": "你好", "session_id": key})
        assert resp.status_code == 200, resp.text
        joined = " ".join(str(c) for c in captured)
        assert "【用户中途重定向】" not in joined, f"空闲会话误注入重定向：{joined}"
    finally:
        config.set_runtime("code_root", prev_root or "")
        config.set_runtime("llm_provider", old_provider or "")
