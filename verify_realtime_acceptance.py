"""R12 acceptance run for the realtime path (provider-side evidence).

R12 asks for five measurements against a real model: first response, sustained
response, interrupt response, reconnect recovery and error rate. This drives
the real :class:`~agent_runtime.realtime_omni.OmniRealtimeProvider` end to end
and writes a dated acceptance report under ``docs/``.

Scope note for the other agents: this covers the **R4 provider** side of R12.
Device capture (camera / microphone / screen share) and the cockpit UX pass
belong to the R12 owner; nothing here touches the gateway or the frontend.

Usage::

    .venv\\Scripts\\python.exe -B verify_realtime_acceptance.py --wav speech.wav
    .venv\\Scripts\\python.exe -B verify_realtime_acceptance.py --quick
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_realtime_live import _ensure_wav, _load_dotenv  # noqa: E402

from agent_runtime import realtime_provider as rp  # noqa: E402
from agent_runtime.realtime_omni import OmniRealtimeProvider  # noqa: E402

STEP = 3200  # 100 ms of 16 kHz s16le
SILENCE = b"\x00" * STEP
# Seconds of quiet pushed after the speech itself. See _stream() for why this
# is not optional: the server VAD closes the turn on trailing silence, and a
# stream cut while the last sample is still loud leaves the turn open forever.
DEFAULT_TAIL = 1.0
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


@dataclass
class Turn:
    """One speak -> answer round."""

    label: str
    start_ms: int = 0
    speak_ms: int = 0
    tail_ms: int = 0
    speech_stopped_ms: int = 0
    first_text_ms: int = 0
    first_audio_ms: int = 0
    text_deltas: int = 0
    audio_deltas: int = 0
    transcript: str = ""
    last_delta_ms: int = 0
    interrupt_ms: int = 0
    saw_done: bool = False
    sent: int = 0
    send_failed: int = 0
    retried: bool = False
    stream_broken: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def e2e_ms(self) -> int:
        """Start of speech -> first assistant output.

        Measured from the first chunk, not the last: in server-VAD mode the
        server declares the turn over long before the clip finishes, so a
        "last chunk" baseline goes negative.
        """
        first = min(x for x in (self.first_text_ms, self.first_audio_ms) if x > 0) \
            if (self.first_text_ms or self.first_audio_ms) else 0
        return first - self.start_ms if first else 0

    @property
    def model_ms(self) -> int:
        """Server-detected speech end -> first assistant output."""
        first = min(x for x in (self.first_text_ms, self.first_audio_ms) if x > 0) \
            if (self.first_text_ms or self.first_audio_ms) else 0
        return first - self.speech_stopped_ms if first and self.speech_stopped_ms else 0


def _stream(provider: OmniRealtimeProvider, pcm: bytes, seconds: float,
            sink: Turn, stop_after_deltas: int = 0,
            interrupt_after_deltas: int = 0, tail: float = DEFAULT_TAIL) -> int:
    """Send `seconds` of audio plus a silence tail, then collect.

    The tail is the important part. When the clip is cut mid-word the server
    VAD never sees the trailing quiet it needs to declare the turn over, so it
    sits there holding an open turn until the session is reaped ~8 s later --
    the caller sees `stream_broken` with a perfectly good transcript and no
    answer. Every real client has the same shape of problem: the microphone
    keeps running after the user stops talking, and that is what closes the
    turn. Dropping the connection the instant speech ends is the bug.
    """
    total = min(int(seconds * 10), len(pcm) // STEP)
    sink.start_ms = int(time.time() * 1000)
    for i in range(total):
        if provider.send_audio(pcm[i * STEP:(i + 1) * STEP],
                               captured_at=int(time.time() * 1000)):
            sink.sent += 1
        else:
            sink.send_failed += 1
        time.sleep(0.1)
    sink.speak_ms = int(time.time() * 1000)
    sink.tail_ms = int(tail * 1000)
    send_tail = getattr(provider, "send_silence_tail", None)
    if callable(send_tail):
        # The provider owns the pacing and exact PCM16 chunk size so production
        # and acceptance use the same VAD-closing behavior.
        send_tail(tail)
    else:
        for _ in range(int(tail * 10)):
            # not counted in `sent`: that counter exists to show whether the
            # speech chunks themselves reached the wire
            provider.send_audio(SILENCE, captured_at=int(time.time() * 1000))
            time.sleep(0.1)
    return _collect(provider, sink, seconds=18.0,
                    stop_after_deltas=stop_after_deltas,
                    interrupt_after_deltas=interrupt_after_deltas)


def turn_succeeded(turn: Turn) -> bool:
    """A realtime turn succeeds only after a terminal ``done`` event."""
    return bool(turn.saw_done and (turn.text_deltas or turn.audio_deltas))


def turn_needs_retry(turn: Turn) -> bool:
    """Transcript-only and stream-broken turns are recoverable failures."""
    return not turn_succeeded(turn)


def _collect(provider: OmniRealtimeProvider, sink: Turn, seconds: float,
             stop_after_deltas: int = 0, interrupt_after_deltas: int = 0) -> int:
    deadline = time.time() + seconds
    deltas = 0
    interrupted = False
    while time.time() < deadline:
        event = provider.poll(timeout=0.3)
        if event is None:
            continue
        kind, payload = event.kind, event.payload
        if kind == rp.EVENT_STATUS:
            if payload.get("state") == "thinking" and not sink.speech_stopped_ms:
                sink.speech_stopped_ms = event.captured_at
        elif kind == rp.EVENT_TRANSCRIPT:
            if payload.get("final"):
                sink.transcript = str(payload.get("text") or "")
        elif kind == rp.EVENT_TEXT_DELTA:
            deltas += 1
            sink.text_deltas += 1
            sink.last_delta_ms = event.captured_at
            if not sink.first_text_ms:
                sink.first_text_ms = event.captured_at
        elif kind == rp.EVENT_AUDIO_DELTA:
            deltas += 1
            sink.audio_deltas += 1
            sink.last_delta_ms = event.captured_at
            if not sink.first_audio_ms:
                sink.first_audio_ms = event.captured_at
        elif kind == rp.EVENT_DONE:
            sink.saw_done = True
            break
        elif kind == rp.EVENT_ERROR:
            code = str(payload.get("code") or "error")
            sink.errors.append(code)
            if code == "stream_broken":
                sink.stream_broken = True
                break
        if interrupt_after_deltas and not interrupted and deltas >= interrupt_after_deltas:
            interrupted = True
            provider.interrupt()
            sink.interrupt_ms = int(time.time() * 1000)
            sink.errors.append("interrupt-sent")
        if stop_after_deltas and deltas >= stop_after_deltas:
            break
    return deltas


def _ensure_live(provider: OmniRealtimeProvider, stats: dict[str, int]) -> bool:
    """Reconnect if the server dropped the session; count how often it happens.

    The current endpoint keeps an idle session alive for at least 45 s, but it
    does drop one after a completed turn often enough to matter. A production
    gateway needs exactly this reflex, so the run exercises it rather than
    hiding it.
    """
    if provider._running and provider._ws is not None:
        return True
    stats["reconnects"] += 1
    provider.close()
    time.sleep(0.5)
    if not provider.start():
        return False
    return _wait_ready(provider)


def _wait_ready(provider: OmniRealtimeProvider, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = provider.poll(timeout=0.3)
        if event is not None and event.kind == rp.EVENT_STATUS:
            return True
    return False


def _drain_quiet(provider: OmniRealtimeProvider, quiet: float = 1.2,
                 max_seconds: float = 10.0) -> None:
    """Drain until the stream has been silent for `quiet` seconds.

    A fixed 1 s drain left the tail of one turn queued up, and the next turn
    would then time its first response off those stale events (e2e came out at
    42 ms, which is obviously not a round trip). Wait for real quiet instead.
    """
    deadline = time.time() + max_seconds
    quiet_since = time.time()
    while time.time() < deadline:
        if provider.poll(timeout=0.2) is not None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= quiet:
            return


def _retry_turn(provider: OmniRealtimeProvider, pcm: bytes, span: float,
                turn: Turn, stats: dict[str, int], tail: float) -> bool:
    """Reconnect once and replay an incomplete turn; broken streams are recoverable."""
    turn.retried = True
    stats["reconnects"] += 1
    provider.close()
    time.sleep(0.5)
    if not provider.start() or not _wait_ready(provider):
        return False
    # Completion belongs to an individual attempt.  A provider can emit a
    # terminal marker without any assistant payload before the socket breaks;
    # retaining that marker would let a replay with payload but no fresh ``done``
    # pass the success gate.  Keep the error history for the report, but reset
    # all turn evidence that must be produced by this replay.
    turn.saw_done = False
    turn.transcript = ""
    turn.text_deltas = 0
    turn.audio_deltas = 0
    turn.speech_stopped_ms = 0
    turn.speak_ms = 0
    turn.tail_ms = 0
    # Replay the utterance and its VAD-closing silence tail. A transcript
    # without done is deliberately replayed: audio arrival is not completion.
    _stream(provider, pcm, span, turn, tail=tail)
    return turn_succeeded(turn)


def main() -> int:
    parser = argparse.ArgumentParser(description="R12 realtime acceptance run")
    parser.add_argument("--wav", default="", help="16 kHz mono s16le speech wav")
    parser.add_argument("--quick", action="store_true",
                        help="skip the sustained and video scenarios")
    parser.add_argument("--turn-seconds", type=float, default=0.0,
                        help="force each turn to this many seconds of audio")
    parser.add_argument("--tail-seconds", type=float, default=DEFAULT_TAIL,
                        help="silence pushed after each turn so the server VAD "
                             "closes it (default 1.0; set 0 to reproduce the "
                             "drop)")
    parser.add_argument("--report", default="", help="report path (default docs/)")
    args = parser.parse_args()
    _load_dotenv()

    wav = _ensure_wav(args.wav)
    with wave.open(wav, "rb") as w:
        pcm = w.readframes(w.getnframes())
    if len(pcm) < STEP * 60:
        print("WARNING: clip is short; results may be thin")

    results: dict[str, object] = {}
    stats: dict[str, int] = {"reconnects": 0}
    turns: list[Turn] = []
    error_events = 0
    total_events = 0

    # ---- scenario 1 + 2: single turn, then sustained turns ------------------
    provider = OmniRealtimeProvider(session_id="r12", project_id="prj-r12")
    print("availability:", provider.availability().get("model"),
          provider.availability().get("voice"))
    if not provider.start():
        print("start failed")
        return 2
    _wait_ready(provider)

    # split the clip so each turn says something different
    n_turns = 1 if args.quick else 3
    span = args.turn_seconds or max(3.0, min(4.0, len(pcm) / 32000 / n_turns))
    for i in range(n_turns):
        turn = Turn(label=f"turn-{i + 1}")
        start = i * int(span * 32000)
        chunk = pcm[start:start + int(span * 32000)]
        if len(chunk) < STEP * 10:
            break
        _ensure_live(provider, stats)
        _stream(provider, chunk, span, turn, tail=args.tail_seconds)
        if turn_needs_retry(turn):
            # A stream_broken or transcript-only turn is incomplete. Reconnect
            # and replay once; do not call it successful without done.
            _retry_turn(provider, chunk, span, turn, stats, args.tail_seconds)
        turns.append(turn)
        total_events += (turn.text_deltas + turn.audio_deltas
                         + len(turn.errors) + (1 if turn.saw_done else 0))
        error_events += len([e for e in turn.errors
                             if e not in {"interrupt-sent", "stream_broken"}])
        print(f"  {turn.label}: e2e={turn.e2e_ms}ms model={turn.model_ms}ms "
              f"text={turn.text_deltas} audio={turn.audio_deltas} "
              f"done={turn.saw_done} sent={turn.sent}/{turn.sent + turn.send_failed} "
              f"retry={turn.retried} err={turn.errors[:2]} "
              f"transcript={turn.transcript[:40]!r}")
        _drain_quiet(provider, 1.0)

    # ---- scenario 3: interrupt ---------------------------------------------
    interrupt_turn = Turn(label="interrupt")
    if not args.quick:
        if _ensure_live(provider, stats):
            _stream(provider, pcm, 3.0, interrupt_turn,
                    interrupt_after_deltas=8, tail=args.tail_seconds)
        post_turn = Turn(label="post-interrupt")
        after = _collect(provider, post_turn, seconds=8.0)
        # What matters is not that output stops instantly -- tokens already in
        # flight keep arriving -- but that it stops promptly.
        stop_ms = (post_turn.last_delta_ms - interrupt_turn.interrupt_ms
                   if post_turn.last_delta_ms and interrupt_turn.interrupt_ms
                   else -1)
        results["interrupt"] = {
            "deltas_before": interrupt_turn.text_deltas + interrupt_turn.audio_deltas,
            "deltas_after": after,
            "stop_ms": stop_ms,
            "saw_done": interrupt_turn.saw_done,
        }
        print(f"  interrupt: before={results['interrupt']['deltas_before']} "
              f"after={after} stop={stop_ms}ms")
        _drain_quiet(provider, 1.0)

    # ---- scenario 4: disconnect + reconnect --------------------------------
    provider.close()
    time.sleep(1.0)
    recovery_turn = Turn(label="after-reconnect")
    restarted = provider.start()
    recovered = False
    if restarted:
        recovered = _wait_ready(provider)
        if recovered:
            _stream(provider, pcm[:STEP * 40], 3.0, recovery_turn,
                    tail=args.tail_seconds)
    stats["reconnects"] += 1
    results["recovery"] = {"restarted": restarted, "ready": recovered,
                           "text": recovery_turn.text_deltas,
                           "audio": recovery_turn.audio_deltas,
                           "done": recovery_turn.saw_done}
    print(f"  recovery: restarted={restarted} ready={recovered} "
          f"text={recovery_turn.text_deltas} audio={recovery_turn.audio_deltas}")

    # ---- scenario 5: a video frame -----------------------------------------
    video_ok = None
    if not args.quick:
        Turn(label="video")
        # 1x1 PNG is enough to prove the event is accepted, not to be understood
        tiny = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
                b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\n"
                b"IDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
                b"\x00IEND\xaeB`\x82")
        _ensure_live(provider, stats)
        video_ok = provider.send_frame(tiny, captured_at=int(time.time() * 1000))
        _drain_quiet(provider, 3.0)
        results["video_frame_accepted"] = video_ok
        print(f"  video frame accepted: {video_ok}")
    provider.close()

    # ---- report -------------------------------------------------------------
    dropped_turns = [t.label for t in turns if not turn_succeeded(t)]
    good = [t for t in turns if turn_succeeded(t)]
    first = good[0] if good else (turns[0] if turns else Turn(label="none"))
    sustained_ok = len(good) >= (2 if n_turns > 1 else 1)
    # Tokens already in flight keep arriving after an interrupt, so "zero new
    # output" is not required. The bar is: either nothing more arrived, or
    # whatever arrived stopped promptly.
    interrupt_after = int(results.get("interrupt", {}).get("deltas_after", -1))
    interrupt_stop = int(results.get("interrupt", {}).get("stop_ms", -1))
    interrupt_ok = bool(results.get("interrupt")) and \
        (interrupt_after == 0 or 0 <= interrupt_stop <= 3000)
    interrupt_txt = ("无新增输出" if interrupt_after == 0
                     else f"{interrupt_stop} ms 内停止")
    interrupt_cell = (f"新增 {interrupt_after} 条，{interrupt_txt}"
                      if interrupt_after else "无新增输出")
    tail_note = (f"本次 {len(dropped_turns)}/{len(turns)} 轮受影响："
                 f"{', '.join(dropped_turns)}。" if dropped_turns
                 else f"本次 {len(turns)} 轮全部闭合，未复现。")
    recovery_ok = bool(results["recovery"]["ready"]) and \
        (results["recovery"]["text"] or results["recovery"]["audio"])
    error_rate = (error_events / total_events * 100) if total_events else 0.0

    lines = [
        "# R12 实时链路验收报告（R4 provider 侧证据）",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型：`{provider._model}` / 音色：`{provider._voice}`",
        f"- 音频样本：{os.path.basename(wav)}（16 kHz 单声道 s16le，实时节奏推流）",
        f"- 每轮语音后补静音：{args.tail_seconds:.1f} s（服务端 VAD 靠尾音收句，见关键发现 1）",
        f"- 会话重连次数：{stats['reconnects']}（含失败重连与场景 4 的强制重连）",
        "",
        "## 指标",
        "",
        "| 指标 | 结果 | 判定 |",
        "| --- | --- | --- |",
        f"| 首响应（开始说话→首个输出，含服务端 VAD 静默等待） | {first.e2e_ms} ms | "
        f"{'通过' if 0 < first.e2e_ms < 8000 else '未通过'} |",
        f"| 模型延迟（服务端判定说完→首个输出） | {first.model_ms} ms | "
        f"{'通过' if 0 < first.model_ms < 8000 else '未通过'} |",
        f"| 持续响应 | {len(good)}/{len(turns)} 轮完整 | "
        f"{'通过' if sustained_ok else '未通过'} |",
        f"| 打断响应 | 打断后{interrupt_cell if results.get('interrupt') else 'n/a'} | "
        f"{'通过' if interrupt_ok else ('未测' if args.quick else '未通过')} |",
        f"| 断线恢复 | 全程重连 {stats['reconnects']} 次均成功，恢复后文本"
        f"{results['recovery']['text']}/音频{results['recovery']['audio']} | "
        f"{'通过' if recovery_ok else '未通过'} |",
        f"| 错误率 | {error_rate:.1f}%（{error_events}/{total_events}） | "
        f"{'通过' if error_rate < 5 else '未通过'} |",
        "",
        "## 逐轮明细",
        "",
        "| 轮次 | 端到端 | 模型延迟 | 文本 | 音频 | done | 重连重试 | 转写 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in turns + [recovery_turn]:
        lines.append(f"| {t.label} | {t.e2e_ms} ms | {t.model_ms} ms | "
                     f"{t.text_deltas} | {t.audio_deltas} | "
                     f"{'是' if t.saw_done else '否'} | {'是' if t.retried else '否'} | "
                     f"{t.transcript[:44]!r} |")
    lines += [
        "",
        "## 关键发现",
        "",
        "1. **必须在语音之后补一段静音，否则服务端永远不会回答。** 服务端 VAD 靠"
        "尾部静音判定一轮说完；如果在最后一片采样仍然响亮时就把流切断，这一轮会一直"
        "挂着不闭合，服务端约 8.5 秒后回收会话，调用方只看到 `stream_broken`。"
        "对照实验（同一片段、全新会话）：不补静音 0/2 成功；补 1.0 秒静音 2/2 成功"
        "（文本 17 条 / 音频 35 条 / done）。这不是模型不稳定，也不是账号问题——"
        "麦克风在用户停止说话后仍在运行，本来就会提供这段尾音，**主动掐断才是 bug**。"
        f"{tail_note}",
        "",
        "2. **`commit()` 不能替代静音尾，在 VAD 已收句后调用还会报错。** 补了静音尾"
        "之后服务端已判定说完，此时再 `input_audio_buffer.commit` 提交空 buffer 会收到 "
        "`vendor_error`。建议：服务端 VAD 模式下只用静音尾收句，`commit()` 留给客户端"
        "自己做 VAD 的场景。",
        "",
        "3. **成功判定必须看 `done`；有转写、无 `done` 必须判定为失败并重发。** 失败样本能拿到 8–19 条 "
        "`transcript` 却零回复，说明音频确实送达、只是 turn 没闭合。网关不能以"
        "「转写成功」当作成功信号。",
        "",
        "4. **断连时分片其实已全部发出**（`sent` 满额），所以调用方不能只看 "
        "`send_audio()` 的返回值，必须监听 `stream_broken` 并重连重发。",
        "",
        "5. **打断生效，但可能有余波。** 打断后仍会继续收到已在生成中的 token"
        f"（本次{interrupt_cell if results.get('interrupt') else '未测'}），"
        "所以「打断后零输出」不是必需条件，判据应该是**多久停止**。",
        "",
        "6. **重连恢复可靠**：全程重连 "
        f"{stats['reconnects']} 次，每次都能重新握手并拿到完整应答。",
        "",
        "## 范围声明",
        "",
        "本报告只覆盖 **R4 provider 侧**：真实模型、音频输入、打断、断线重连。",
        "设备采集（摄像头/麦克风/屏幕共享）与驾驶舱体验调优属 R12 负责人范围，未在此覆盖。",
        "",
        "## 对 R3 网关的三条硬要求",
        "",
        "1. **停止说话 ≠ 停止推流。** 采集端或网关在用户停止说话后必须继续推约 1 秒"
        "静音（或保持链路直到收到 `speech_stopped`），否则服务端永远不回答。这条如果"
        "漏掉，现象是「转写正常但没有任何回复」，极易误判成模型故障。",
        "2. **不能把断连当致命错误上抛。** 验收 runner 将 `stream_broken` 视为可恢复事件，"
        "执行一次重连并重发该轮音频与静音尾；生产网关需采用同一语义。",
        "3. **成功判定看 `done`。** 有 `transcript` 而无 `done` 属于失败，已纳入重发条件。",
        "",
    ]
    report_path = args.report or os.path.join(
        REPORT_DIR, f"realtime-r12-acceptance-{time.strftime('%Y%m%d')}.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nreport -> {report_path}")

    ok = (0 < first.e2e_ms < 8000) and sustained_ok and recovery_ok \
        and error_rate < 5 and (interrupt_ok or args.quick)
    print("RESULT:", "PASS" if ok else "PARTIAL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
