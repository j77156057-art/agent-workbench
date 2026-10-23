"""R10 性能基准工具 `realtime_bench.py` 自身的测试（任务 R10，AI-F 槽位）。

基准工具最危险的失效方式是**悄悄测错东西**——产出一张漂亮的表，但数字根本不是它声称的
那个量。所以这里的断言重点不在"跑通了"，而在**归因是否正确**：

* 每一个观察都被对回它那一帧（序号精确匹配），而不是数了数事件个数；
* 延迟确实包含模型耗时（慢模型下延迟必须 ≥ 模型耗时）；
* 丢帧数必须与**另一条独立代码路径**（占位模型实际被调用次数）吻合——事件侧归因和
  调用侧计数互相印证，任一环节算错都会在这里露馅。

本文件只读 `api.py` 的网关，不改任何实现；与 `realtime_bench.py` 一起属 R10 交付。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

import api
import realtime_bench as bench
from agent_runtime import realtime_metrics as metrics

# 小帧数 + 小延时，保证整份文件在一秒级跑完。
FAST = 5
SLOW_MS = 60.0


def _run(*, mode: str, frames: int, model_ms: float = 0.0,
         interval_ms: float = 5.0) -> tuple[bench.RunResult, bench._StubAnalyzer, dict]:
    """跑一轮基准，返回 (结果, 占位模型, 指标快照)。"""
    registry = metrics.MetricsRegistry(window=max(frames, 8) + 1)
    analyzer = bench._StubAnalyzer(model_ms)
    with bench.gateway(analyzer) as client:
        if mode == "rtt":
            result = bench.run_rtt(client, frames=frames, model_ms=model_ms, registry=registry)
        else:
            result = bench.run_stream(client, frames=frames, model_ms=model_ms,
                                      interval_ms=interval_ms, registry=registry)
    return result, analyzer, registry.snapshot()


def _histogram(snapshot: dict, name: str) -> dict:
    return snapshot["sessions"][bench.SESSION_ID]["histograms"].get(name, {})


# --- 归因正确性 -----------------------------------------------------------

def test_rtt_mode_attributes_every_observation_to_its_frame():
    result, analyzer, _ = _run(mode="rtt", frames=FAST)
    assert result.observed == FAST
    assert result.dropped == 0
    assert len(result.latencies_ms) == FAST
    # 一帧一收，所以每一帧都真的进了模型：调用次数必须等于帧数。
    assert analyzer.calls == FAST


def test_stream_drop_count_agrees_with_the_independent_model_call_counter():
    """事件侧归因（dropped）与调用侧计数（analyzer.calls）必须吻合。

    这是本工具最关键的一致性检查：`dropped = frames - observed` 来自 websocket 事件
    回带的序号，`analyzer.calls` 来自占位模型自己的计数器，两条路径互不相干。
    """
    result, analyzer, _ = _run(mode="stream", frames=12, model_ms=SLOW_MS, interval_ms=10.0)
    assert result.dropped > 0, "模型比发帧间隔慢得多，网关应当合并掉中间帧"
    assert analyzer.calls == result.observed
    assert analyzer.calls + result.dropped == 12


def test_stream_with_a_fast_model_drops_nothing():
    result, analyzer, _ = _run(mode="stream", frames=8, model_ms=0.0, interval_ms=5.0)
    assert result.dropped == 0
    assert analyzer.calls == 8


# --- 延迟语义 -------------------------------------------------------------

def test_rtt_latency_actually_includes_the_simulated_model_time():
    """延迟必须把模型耗时算进去；只测到网关壳子的话这条会红。"""
    result, _, _ = _run(mode="rtt", frames=3, model_ms=SLOW_MS)
    assert min(result.latencies_ms) >= SLOW_MS * 0.8, (
        f"最短延迟 {min(result.latencies_ms):.1f}ms 明显小于模型耗时 {SLOW_MS}ms，"
        "说明计时起点晚于模型调用")
    assert max(result.latencies_ms) < SLOW_MS * 10, "延迟量级失控，计时单位可能错了"


def test_stream_latency_is_measured_at_arrival_not_after_the_send_loop():
    """读必须与发并发。

    若先发完再统一读，早期帧的观察会在客户端队列里空等，测出来的是"读取延迟"
    （修复前实测 p50 683ms，纯夹具伪影）。慢模型下真实端到端应当在模型耗时的量级，
    而不是"发帧总时长"的量级。
    """
    frames, interval = 12, 10.0
    result, _, _ = _run(mode="stream", frames=frames, model_ms=SLOW_MS, interval_ms=interval)
    send_window_ms = frames * interval
    assert send_window_ms > 100, "这条断言依赖发送窗口足够长，否则区分不出两种计时方式"
    p50 = sorted(result.latencies_ms)[len(result.latencies_ms) // 2]
    assert p50 < send_window_ms, (
        f"stream p50 {p50:.1f}ms 已达到整个发送窗口 {send_window_ms:.0f}ms 的量级，"
        "像是先发完再统一读的伪影")


def test_peak_backlog_tracks_frames_awaiting_an_observation():
    result, _, _ = _run(mode="stream", frames=12, model_ms=SLOW_MS, interval_ms=10.0)
    assert result.peak_backlog > 1, "慢模型下必然有帧在排队等观察"
    assert result.peak_backlog <= 12


# --- 指标注册表接线 -------------------------------------------------------

def test_registry_receives_the_r10_metric_names():
    registry = metrics.MetricsRegistry(window=16)
    analyzer = bench._StubAnalyzer(SLOW_MS)
    with bench.gateway(analyzer) as client:
        bench.run_rtt(client, frames=4, model_ms=SLOW_MS, registry=registry)
        bench.run_stream(client, frames=6, model_ms=SLOW_MS, interval_ms=8.0, registry=registry)
    snapshot = registry.snapshot()

    histograms = snapshot["sessions"][bench.SESSION_ID]["histograms"]
    assert metrics.OBSERVATION_LATENCY_MS in histograms
    assert metrics.END_TO_END_MS in histograms
    assert histograms[metrics.OBSERVATION_LATENCY_MS]["count"] == 4
    assert histograms[metrics.END_TO_END_MS]["p95"] is not None

    counters = snapshot["sessions"][bench.SESSION_ID]["counters"]
    assert counters[metrics.FRAMES_SENT] == 10
    assert counters[metrics.FRAMES_DROPPED] > 0

    gauges = snapshot["sessions"][bench.SESSION_ID]["gauges"]
    assert gauges[metrics.QUEUE_DEPTH] is not None
    # 全局作用域同样要收到，否则跨会话汇总会缺数。
    assert metrics.FRAMES_SENT in snapshot["global"]["counters"]


def test_report_is_json_serializable_and_carries_the_evidence():
    registry = metrics.MetricsRegistry(window=16)
    analyzer = bench._StubAnalyzer(SLOW_MS)
    with bench.gateway(analyzer) as client:
        results = [bench.run_rtt(client, frames=4, model_ms=SLOW_MS, registry=registry)]
    payload = bench.report(results, registry, analyzer.calls)
    text = json.dumps(payload, ensure_ascii=False)  # 不抛异常即可
    assert bench.SESSION_ID in text

    run = payload["runs"][0]
    for key in ("mode", "frames", "observed", "dropped", "drop_rate", "latency_ms",
                "peak_backlog", "frames_per_sec", "wall_ms", "side_events", "errors",
                "latency_metric"):
        assert key in run, f"证据里缺 {key}"
    assert payload["model_calls"] == 4
    assert run["errors"] == []
    assert run["latency_metric"] == metrics.OBSERVATION_LATENCY_MS


def test_gateway_overhead_is_reported_only_when_the_model_cost_is_known():
    """模拟模型耗时可精确扣除，所以 rtt 要额外报网关自身开销；没有模型耗时不报。"""
    registry = metrics.MetricsRegistry(window=16)
    analyzer = bench._StubAnalyzer(SLOW_MS)
    with bench.gateway(analyzer) as client:
        slow = [bench.run_rtt(client, frames=3, model_ms=SLOW_MS, registry=registry)]
    overhead = bench.report(slow, registry, analyzer.calls)["runs"][0]["gateway_overhead_ms"]
    assert overhead["p50"] < SLOW_MS, "扣掉模型耗时后，网关开销必须小于总延迟"

    free = metrics.MetricsRegistry(window=16)
    analyzer = bench._StubAnalyzer(0.0)
    with bench.gateway(analyzer) as client:
        fast = [bench.run_rtt(client, frames=3, model_ms=0.0, registry=free)]
    assert "gateway_overhead_ms" not in bench.report(fast, free, analyzer.calls)["runs"][0]


# --- 命令行与卫生 ---------------------------------------------------------

def test_frames_beyond_the_window_are_rejected_rather_than_silently_truncated():
    """样本多于环形窗口时早期样本会被挤掉，百分位不再代表整轮——宁可报错。"""
    with pytest.raises(SystemExit):
        bench.main(["--frames", "300", "--window", "256"])


def test_main_writes_json_evidence():
    """不用 pytest 的 `tmp_path`：本机用户名含撇号，pytest 扫描
    `D:\\Temp\\pytest-of-<local-user>` 会抛 PermissionError，与本工具无关。"""
    directory = tempfile.mkdtemp(prefix="realtime-bench-")
    try:
        out = os.path.join(directory, "bench.json")
        assert bench.main(["--mode", "rtt", "--frames", "3", "--json", out]) == 0
        with open(out, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["runs"][0]["mode"] == "rtt"
        assert payload["runs"][0]["observed"] == 3
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_bench_leaves_no_session_behind():
    """跑完必须能立刻为同一项目重开一条干净会话（槽位与处理器都已清理）。"""
    registry = metrics.MetricsRegistry(window=16)
    analyzer = bench._StubAnalyzer(0.0)
    with bench.gateway(analyzer) as client:
        bench.run_rtt(client, frames=3, model_ms=0.0, registry=registry)
        with client.websocket_connect(f"{bench.ENDPOINT}?project_id={bench.PROJECT_ID}") as socket:
            socket.send_text(bench._hello())
            hello = socket.receive_json()
            assert hello["type"] == "hello.ok"
            assert hello["project_id"] == bench.PROJECT_ID


def test_bench_records_side_events_instead_of_swallowing_them():
    """心跳/错误这类旁路事件要留痕，否则网关换了错误语义、工具会假装一切正常。"""
    analyzer = bench._StubAnalyzer(0.0)
    with bench.gateway(analyzer) as client:
        with bench._session(client) as socket:
            socket.send_text(json.dumps({"v": api.PROTOCOL_VERSION, "type": "heartbeat",
                                         "sent_at": int(time.time() * 1000)}))
            socket.send_bytes(bench._packet(0, bench._now_ms()))
            seen: list[str] = []
            event = bench._receive_observation(socket, seen)
            assert event["type"] == "video.observation"
            assert seen == ["heartbeat"]
