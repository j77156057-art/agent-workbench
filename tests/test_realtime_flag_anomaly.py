# -*- coding: utf-8 -*-
"""M2·② flag_anomaly 严格 JSON 发现契约（不联网、不启 WS、直接喂 RealtimeEvent）。

锁四件事：
1. `_parse_anomaly_marker` 的 schema 边界：低置信/空 target/空 evidence/超长/半截 JSON
   一律拒（fail-closed——不合规就当没报，绝不用自由文本兜底）。
2. 句首前缀路径保留为 **兼容别名**（trigger="phrase-legacy"），旧会话切换窗不静默漏报。
3. `_discovery_from_delta` 里 JSON 优先于句首前缀；provider 复读整段 final 时以 final 为准。
4. 去重按 `target|evidence` 归一化，换措辞不再刷；节流/时间窗保持不变。
"""
from __future__ import annotations

from agent_runtime import realtime_bridge as br
from agent_runtime.realtime_provider import EVENT_TEXT_DELTA, RealtimeEvent


# ---- 纯函数：_parse_anomaly_marker -----------------------------------------
def test_marker_parses_flat_anomaly_and_strips_json():
    obj = br._parse_anomaly_marker(
        '画面里 {"anomaly": {"target":"右上角弹窗","evidence":"红色 Error: undefined",'
        '"confidence":0.9}} 你确认下')
    assert obj is not None
    assert obj["target"] == "右上角弹窗"
    assert obj["evidence"] == "红色 Error: undefined"
    assert obj["confidence"] == 0.9
    assert "anomaly" not in obj["text"]
    assert "确认下" in obj["text"]


def test_marker_rejects_low_confidence():
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"Y","confidence":0.79}}') is None


def test_marker_rejects_missing_or_empty_fields():
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"","evidence":"Y","confidence":0.9}}') is None
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"","confidence":0.9}}') is None
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","confidence":0.9}}') is None


def test_marker_rejects_oversized_fields():
    long_evidence = "错" * (br.ANOMALY_EVIDENCE_MAX_CHARS + 1)
    long_target = "T" * (br.ANOMALY_TARGET_MAX_CHARS + 1)
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"%s","confidence":0.9}}' % long_evidence) is None
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"%s","evidence":"Y","confidence":0.9}}' % long_target) is None


def test_marker_rejects_confidence_out_of_range_and_types():
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"Y","confidence":1.5}}') is None
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"Y","confidence":-0.1}}') is None
    assert br._parse_anomaly_marker(
        '{"anomaly": {"target":"X","evidence":"Y","confidence":"high"}}') is None


def test_marker_rejects_malformed_or_partial_json():
    assert br._parse_anomaly_marker('{"anomaly": {"target":"X",') is None
    assert br._parse_anomaly_marker('{"anomaly": not-json}') is None
    assert br._parse_anomaly_marker('random text no braces at all') is None


def test_marker_ignores_non_anomaly_json():
    assert br._parse_anomaly_marker('{"user":"alice","confidence":0.99}') is None


def test_marker_picks_last_valid_when_multiple():
    """模型自我修正时，后一段更可信；取最后一段而不是最后一段之前的。"""
    text = ('先说 {"anomaly": {"target":"A","evidence":"e1","confidence":0.9}} '
            '改口 {"anomaly": {"target":"B","evidence":"e2","confidence":0.95}}')
    obj = br._parse_anomaly_marker(text)
    assert obj is not None and obj["target"] == "B" and obj["confidence"] == 0.95


# ---- 主链：SessionBridge._discovery_from_delta ------------------------------
def _mk_bridge():
    """不起 provider，直接把桥当纯接线状态用——resolve_mode 走 sampled-frames 分支，
    discovery 逻辑与之无关。"""
    b = br.SessionBridge("flag-anomaly-test", clock=lambda: 0.0)
    b._clock = lambda: 10.0
    return b


def _ev(text, *, final=False):
    return RealtimeEvent(EVENT_TEXT_DELTA, captured_at=1, session_id="s",
                         payload={"text": text, "role": "assistant", "final": final})


def test_json_anomaly_emits_structured_observation():
    b = _mk_bridge()
    out = b._discovery_from_delta(_ev(
        '我看到右上角有个红色报错弹窗 {"anomaly": {"target":"右上角弹窗",'
        '"evidence":"红色 Error: undefined is not a function","confidence":0.9}}',
        final=True))
    assert out is not None
    assert out["type"] == "model.observation"
    assert out["verified"] is False
    assert out["source"] == "realtime-model"
    assert out["trigger"] == "json-marker"
    assert out["schema_version"] == br.ANOMALY_SCHEMA_VERSION
    assert out["target"] == "右上角弹窗"
    assert out["evidence"] == "红色 Error: undefined is not a function"
    assert out["confidence"] == 0.9
    assert "anomaly" not in out["text"]      # JSON 已剥
    assert "红色报错弹窗" in out["text"]      # 自然语言留下


def test_json_wins_over_legacy_phrase_when_both_present():
    """模型既说了旧开头又补了新 marker → 按新 schema 出，trigger 标 json-marker。"""
    b = _mk_bridge()
    out = b._discovery_from_delta(_ev(
        '我看到画面上有个问题：{"anomaly": {"target":"右下按钮","evidence":"被遮挡",'
        '"confidence":0.95}}', final=True))
    assert out and out["trigger"] == "json-marker"
    assert out["target"] == "右下按钮"


def test_low_confidence_json_is_rejected_not_silently_escalated():
    b = _mk_bridge()
    assert b._discovery_from_delta(_ev(
        '{"anomaly": {"target":"X","evidence":"Y","confidence":0.5}}', final=True)) is None


def test_legacy_phrase_still_emits_as_deprecated_alias():
    b = _mk_bridge()
    out = b._discovery_from_delta(_ev(
        '我看到画面上有个问题：右下角弹了个红框', final=True))
    assert out is not None
    assert out["trigger"] == "phrase-legacy"
    assert out["verified"] is False
    # 兼容别名不带结构化字段（旧 prompt 会话里模型压根没吐 JSON）
    assert "target" not in out
    assert "confidence" not in out
    assert out["text"] == "右下角弹了个红框"


def test_mid_sentence_phrase_not_a_discovery():
    """开工文档 §5.3 强调：自然聊天里出现旧开头不算新异常。"""
    b = _mk_bridge()
    out = b._discovery_from_delta(_ev(
        '好的，我先把当前画面看一遍。我看到画面上有个问题：右下角有红字', final=True))
    assert out is None


def test_partial_deltas_only_emit_on_final_and_accumulate_marker():
    """半截 JSON 不猜——只有拼出合法结构才认。增量分三段：前缀 + anomaly key 半截 + 收尾。"""
    b = _mk_bridge()
    assert b._discovery_from_delta(_ev('我看到一个 {"anomal', final=False)) is None
    assert b._discovery_from_delta(_ev('y": {"target":"X","evid', final=False)) is None
    out = b._discovery_from_delta(_ev(
        'ence":"Y","confidence":0.9}}', final=True))
    assert out is not None
    assert out["trigger"] == "json-marker"
    assert out["target"] == "X"


def test_final_transcript_is_authoritative_not_appended_duplicate():
    """provider 常见套路：增量吐一半 + final 复读整段。JSON 走 final 权威分支不重。"""
    b = _mk_bridge()
    b._discovery_from_delta(_ev('partial {"anomal', final=False))
    full = ('我看到右下角 {"anomaly": {"target":"右下按钮","evidence":"红字 Error",'
            '"confidence":0.9}}')
    out = b._discovery_from_delta(_ev(full, final=True))
    assert out is not None
    assert out["target"] == "右下按钮"
    # 自然语言里不应包含"partial"这种增量残片，说明 final delta 覆盖了累积。
    assert "partial" not in out["text"]


def test_dedupe_by_target_and_evidence_across_paraphrase():
    b = _mk_bridge()
    ev1 = b._discovery_from_delta(_ev(
        '有个错 {"anomaly": {"target":"右上弹窗","evidence":"Error: X 未定义",'
        '"confidence":0.9}}', final=True))
    assert ev1 is not None
    # 换个说法再报同一个 target+evidence → 命中去重，不刷第二条
    b._clock = lambda: 15.0        # 过 2s throttle，仍在 45s dedupe 窗内
    ev2 = b._discovery_from_delta(_ev(
        '同一个错 {"anomaly": {"target":"右上弹窗","evidence":"Error: X 未定义",'
        '"confidence":0.9}}', final=True))
    assert ev2 is None


def test_dedup_window_expires():
    b = _mk_bridge()
    b._discovery_from_delta(_ev(
        '{"anomaly": {"target":"T","evidence":"E","confidence":0.9}}', final=True))
    # 过了 dedupe 窗（45s），同样内容可以再报
    b._clock = lambda: 10.0 + br.DISCOVERY_DEDUPE_WINDOW_SECONDS + 1.0
    out = b._discovery_from_delta(_ev(
        '{"anomaly": {"target":"T","evidence":"E","confidence":0.9}}', final=True))
    assert out is not None


def test_throttle_blocks_rapid_burst_of_distinct_anomalies():
    """不同 target/evidence 逃过内容去重，但 2s 内仍被节流拦住——避免刷屏。"""
    b = _mk_bridge()
    first = b._discovery_from_delta(_ev(
        '{"anomaly": {"target":"A","evidence":"e1","confidence":0.9}}', final=True))
    assert first is not None
    b._clock = lambda: 10.5        # 0.5s 后另一个不同异常
    second = b._discovery_from_delta(_ev(
        '{"anomaly": {"target":"B","evidence":"e2","confidence":0.9}}', final=True))
    assert second is None


def test_non_assistant_role_is_ignored():
    b = _mk_bridge()
    ev = RealtimeEvent(EVENT_TEXT_DELTA, captured_at=1, session_id="s",
                       payload={"text": '{"anomaly": {"target":"X","evidence":"Y","confidence":0.9}}',
                                "role": "user", "final": True})
    assert b._discovery_from_delta(ev) is None
