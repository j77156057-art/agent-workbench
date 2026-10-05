"""合成媒体夹具：把「对着麦克风说话」换成一个可以写进 CI 的确定性文件。

R12 一直卡在设备上——语音回路只能在真机、有人对着麦克风讲话时才验得到，于是
「静音尾」「音频先于视频」这类契约每次都要人手复现。本模块把输入换成交件：

1. 用 Chromium 自带的假设备开关（`--use-file-for-fake-audio-capture` /
   `--use-file-for-fake-video-capture`）把**我们自己生成的 WAV/Y4M**喂给真实浏览器
   页面——不是图像级模拟，页面走的是真的 `getUserMedia` + AudioContext。
2. 在回环地址上开一个懂 R0 协议线的探针服务（`MediaProbe`），收上来的每个二进制包
   都过 `realtime_protocol.parse_binary_packet`，所以验的是**线上契约**而不是私有回声。
3. 按片把内容分类成 speech/silence，用来断言：夹具内容确实到了线上、片形是 16k mono
   100ms（3200 字节）、说完之后仍有 ≥10 片静音尾、静音尾真的让服务端收口、音频包先于
   视频包。

不装 Playwright、不下载浏览器内核：复用 `visual_acceptance` 的真实 Edge + CDP 通道。

**这套夹具证明到哪一步**（越界宣称就是自欺）：
- ✅ 浏览器媒体采集 → WS 二进制包 → R0 协议解析这条**通路**，以及包形/内容/静音尾/
  顺序这些**线上可观测**的性质。
- ❌ 能量 VAD 状态机本身（`liveAudioControl.ts` 的 `createVoiceGate`）不在这个回路里跑
  ——探针页面刻意不接门限、逐帧无条件推流，这样内容对齐才是确定的。门限是纯函数，归
  前端单测。本模块只断言夹具的逐帧能量确实跨过 `startRms`/`endRms`，即「喂给真门的
  信号形状是对的」，不断言门本身。
- ❌ 真机听感、回声消除、蓝牙热插拔仍然只有人能判断。
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

from agent_runtime import dev_server, realtime_protocol
from agent_runtime import visual_acceptance as visual

TARGET_SAMPLE_RATE = 16_000
CHUNK_MS = 100
CHUNK_SAMPLES = TARGET_SAMPLE_RATE * CHUNK_MS // 1000   # 1600
CHUNK_BYTES = CHUNK_SAMPLES * 2                          # 3200：16k mono 100ms
TAIL_CHUNKS = 10                                         # 静音尾 ≥1s（服务端 VAD 收句要尾部静音）
# 与前端 GATE_DEFAULTS 对齐：只用来检查夹具形状，不是在这里重跑门限。
GATE_START_RMS = 0.02
GATE_END_RMS = 0.012

SINE_HZ = 440
SEGMENT_LEVELS = {"sil": 0.0, "quiet": 0.008, "talk": 0.40}
DEFAULT_SCRIPT = "sil:0.3 talk:0.7 sil:0.4 quiet:0.3 talk:0.7 sil:1.3"
MAX_SCRIPT_SECONDS = 30.0
MAX_VIDEO_FRAMES = 60
LOUD_RMS = SEGMENT_LEVELS["talk"] / math.sqrt(2)   # 理想正弦的解析 RMS ≈ 0.283

# 分类阈值：talk 实测 RMS≈0.283，sil 经假设备链路实测为精确 0.0，两者差两个量级，
# 阈值定得宽也不会误判；quiet（RMS≈0.0057）刻意落在中间。
LOUD_AT = 0.12
SILENT_AT = 0.002
RMS_TOLERANCE = 0.05
MAX_WAIT = 60.0


class MediaFixtureError(RuntimeError):
    """夹具无法构造或无法交付。"""


# ---- 夹具内容（纯函数，CI 必跑） ---------------------------------------------

def parse_script(text: str = "") -> list[dict[str, Any]]:
    """`sil:0.3 talk:0.7 ...` → 段列表。这是「用户说了什么」的机器可读写法。"""
    raw = str(text or "").strip() or DEFAULT_SCRIPT
    segments: list[dict[str, Any]] = []
    for token in raw.replace("，", ",").replace(";", ",").replace(",", " ").split():
        kind, _, seconds = token.partition(":")
        kind = kind.strip().lower()
        if kind not in SEGMENT_LEVELS:
            raise MediaFixtureError(
                "段类型只支持 %s，收到 %r。" % ("/".join(sorted(SEGMENT_LEVELS)), kind))
        try:
            value = float(seconds) if seconds else 1.0
        except ValueError:
            raise MediaFixtureError("段长度不是数字：%r。" % token) from None
        if not 0.05 <= value <= 10.0:
            raise MediaFixtureError("段长度必须在 0.05~10 秒之间，收到 %s。" % value)
        segments.append({"kind": kind, "seconds": round(value, 3)})
    if not segments:
        raise MediaFixtureError("脚本是空的。")
    if sum(item["seconds"] for item in segments) > MAX_SCRIPT_SECONDS:
        raise MediaFixtureError("脚本总长不能超过 %s 秒。" % MAX_SCRIPT_SECONDS)
    return segments


def cell_kinds(script: list[dict[str, Any]], chunk_ms: int = CHUNK_MS) -> list[str]:
    """把脚本铺成每格 `chunk_ms` 的段类型序列（取格中心落在哪一段）。"""
    total = int(round(sum(item["seconds"] for item in script) * 1000 / chunk_ms))
    grid: list[str] = []
    for index in range(total):
        center = (index * chunk_ms + chunk_ms / 2) / 1000.0
        walked = 0.0
        kind = "sil"
        for item in script:
            if center < walked + item["seconds"]:
                kind = item["kind"]
                break
            walked += item["seconds"]
        grid.append(kind)
    return grid


def build_pcm(script: list[dict[str, Any]], rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """16-bit LE 单声道 PCM：talk=440Hz 正弦，sil=精确零，quiet=低于门限的正弦。"""
    out = bytearray()
    for item in script:
        amplitude = SEGMENT_LEVELS[item["kind"]]
        count = int(round(item["seconds"] * rate))
        if amplitude <= 0.0:
            out += bytes(count * 2)
            continue
        for index in range(count):
            value = amplitude * math.sin(2 * math.pi * SINE_HZ * index / rate)
            out += struct.pack("<h", max(-32768, min(32767, round(value * 32767))))
    return bytes(out)


def frame_rms_profile(script: list[dict[str, Any]], rate: int = TARGET_SAMPLE_RATE,
                      frame: int = 128, *, include_boundaries: bool = False
                      ) -> list[tuple[str, float]]:
    """逐帧（生产 AudioWorklet 一片 128 样本）能量：确认夹具确实开关得了门。

    跨段的帧属于两种段（`(sil, rms)` 用 `""` 表示），默认剔除：真实门限在边界帧上本来
    就会看到混合能量，把它算进「静音段最大能量」会让任何夹具都过不了检查。
    """
    pcm = build_pcm(script, rate)
    count = len(pcm) // 2
    samples = struct.unpack("<%dh" % count, pcm)
    kinds: list[str] = []
    for item in script:
        kinds.extend([item["kind"]] * int(round(item["seconds"] * rate)))
    profile: list[tuple[str, float]] = []
    for start in range(0, max(0, count - frame), frame):
        first = kinds[min(start, len(kinds) - 1)]
        last = kinds[min(start + frame - 1, len(kinds) - 1)]
        window = samples[start:start + frame]
        rms = math.sqrt(sum(value * value for value in window) / len(window)) / 32767.0
        kind = first if first == last else ""
        if not kind and not include_boundaries:
            continue
        profile.append((kind, rms))
    return profile


def gate_shape(script: list[dict[str, Any]]) -> dict[str, Any]:
    """夹具与前端门限的关系：talk 帧必须能开门，sil/quiet 帧必须低于起说门限。"""
    frames = frame_rms_profile(script, include_boundaries=True)
    clean = [(kind, rms) for kind, rms in frames if kind]
    loud = [rms for kind, rms in clean if kind == "talk"]
    quiet = [rms for kind, rms in clean if kind != "talk"]
    return {
        "frames": len(clean),
        "mixed_frames_skipped": len(frames) - len(clean),
        "loud_max": round(max(loud), 4) if loud else 0.0,
        "loud_min": round(min(loud), 4) if loud else 0.0,
        "quiet_max": round(max(quiet), 4) if quiet else 0.0,
        "opens_gate": bool(loud) and min(loud) >= GATE_START_RMS,
        "closes_gate": bool(quiet) and max(quiet) < GATE_START_RMS,
    }


def write_wav(path: Path, pcm: bytes, rate: int = TARGET_SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


def build_y4m(*, width: int = 32, height: int = 32, frames: int = 25, fps: int = 25) -> bytes:
    """每帧亮度不同的 Y4M（420mpeg2）：亮度在 16~235 合法区间单调走，便于逐帧比对。"""
    if not 2 <= width <= 128 or not 2 <= height <= 128:
        raise MediaFixtureError("夹具视频尺寸为了可控，限制在 2~128 像素。")
    if width % 2 or height % 2:
        raise MediaFixtureError("夹具视频宽高必须都是偶数（420 色度子采样）。")
    if not 2 <= frames <= MAX_VIDEO_FRAMES:
        raise MediaFixtureError("夹具帧数必须在 2~%s 之间。" % MAX_VIDEO_FRAMES)
    luma = [16 + round(index * (235 - 16) / max(1, frames - 1)) for index in range(frames)]
    body = bytearray(b"YUV4MPEG2 W%d H%d F%d:1 Ip A1:1 C420mpeg2\n" % (width, height, fps))
    plane = width * height
    for value in luma:
        body += b"FRAME\n"
        body += bytes([value]) * plane
        body += b"\x80" * (plane // 4)      # 中性色度：亮度斜坡是唯一的每帧变量
        body += b"\x80" * (plane // 4)
    return bytes(body)


def fake_media_flags(audio: str | Path = "", video: str | Path = "",
                     *, permissions: bool = True) -> list[str]:
    """Chromium/Edge 的假设备开关：把文件变成麦克风/摄像头的【内容】。

    `--use-fake-ui-for-media-stream` 只解决授权弹窗，不给内容；真正喂内容的是
    `--use-file-for-fake-*-capture`。文件必须已经存在——否则 Edge 静默回落成内置假
    设备，现象是「跑通了但内容是假的」。
    """
    flags: list[str] = []
    if permissions:
        flags.append("--use-fake-ui-for-media-stream")
    if audio or video:
        flags.append("--use-fake-device-for-media-stream")
    for flag, value in (("--use-file-for-fake-audio-capture", audio),
                        ("--use-file-for-fake-video-capture", video)):
        if not str(value or ""):
            continue
        path = Path(str(value))
        if not path.is_file():
            raise MediaFixtureError("假设备文件不存在：%s" % path)
        flags.append("%s=%s" % (flag, path))
    flags.append("--autoplay-policy=no-user-gesture-required")
    return flags


# ---- 线上包分析（纯函数） ----------------------------------------------------

def chunk_rms(payload: bytes) -> float:
    count = len(payload) // 2
    if not count:
        return 0.0
    samples = struct.unpack("<%dh" % count, payload[: count * 2])
    return math.sqrt(sum(value * value for value in samples) / count) / 32767.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def classify(records: list[dict[str, Any]], *, kinds: tuple[str, ...] = ("audio",)) -> dict[str, Any]:
    """按片分类成 pattern，并给出最长静音连段与节奏统计。"""
    rows = [row for row in records if row.get("kind") in kinds]
    pattern = "".join(str(row.get("label") or "?") for row in rows)
    longest = run = 0
    for char in pattern:
        run = run + 1 if char == "." else 0
        longest = max(longest, run)
    loud = [float(row["rms"]) for row in rows if row["label"] == "T"]
    gaps = [round(float(rows[index]["at"]) - float(rows[index - 1]["at"]), 3)
            for index in range(1, len(rows))]
    return {
        "count": len(rows),
        "pattern": pattern,
        "speech_cells": pattern.count("T"),
        "quiet_cells": pattern.count("q"),
        "silent_cells": pattern.count("."),
        "longest_silence_run": longest,
        "speech_rms_median": round(_median(loud), 4),
        "gap_median": round(_median([float(g) for g in gaps]), 3),
        "gap_max": round(max(gaps), 3) if gaps else 0.0,
    }


def label_of(rms: float) -> str:
    if rms >= LOUD_AT:
        return "T"
    if rms <= SILENT_AT:
        return "."
    return "q"


def expected_labels(script: list[dict[str, Any]]) -> str:
    """脚本对应的理想 pattern：talk→T、quiet→q、sil→.。"""
    return "".join(label_of(SEGMENT_LEVELS[kind] / math.sqrt(2)) for kind in cell_kinds(script))


def pattern_match(observed: str, expected: str) -> dict[str, Any]:
    """在 observed 里找 expected 的最佳【循环】窗口：假设备会把夹具文件循环播放。"""
    if not observed or not expected:
        return {"start": -1, "score": 0.0, "cells": 0}
    span = min(len(expected), len(observed))
    best_start, best_score = -1, 0.0
    for offset in range(len(expected)):
        rotated = (expected[offset:] + expected[:offset])[:span]
        for start in range(len(observed) - span + 1):
            window = observed[start:start + span]
            score = sum(1 for a, b in zip(window, rotated) if a == b) / span
            if score > best_score:
                best_start, best_score = start, score
    return {"start": best_start, "score": round(best_score, 4), "cells": span}


# ---- 探针页面 ------------------------------------------------------------------

PROBE_PAGE_TEMPLATE = """/** 探针页面：真 getUserMedia → 重采样 16k → 100ms 一片 → R0 二进制包上 WS。
 *  刻意不接能量门（见 agent_runtime/media_fixture.py 模块文档）。 */
const WS_URL = "__WS_URL__";
const NEED_VIDEO = __NEED_VIDEO__;
const CHUNK_MS = __CHUNK_MS__;
const TARGET_SAMPLES = __TARGET_SAMPLES__;
const VIDEO_FRAMES = __VIDEO_FRAMES__;
const out = document.getElementById('out');
const state = window.__docmindMedia = {sent: 0, bytes: 0, video: 0, tracks: 0, rate: 0,
                                       error: '', serverEvents: [], helloOk: false, closed: false};
const log = (line) => { out.textContent += '\\n' + line; };
(async () => {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('当前浏览器没有 mediaDevices');
    }
    const stream = await navigator.mediaDevices.getUserMedia(
      {audio: {channelCount: 1, echoCancellation: false, noiseSuppression: false},
       video: NEED_VIDEO});
    state.tracks = stream.getAudioTracks().length;
    const context = new AudioContext();
    await context.resume();
    state.rate = context.sampleRate;
    const socket = new WebSocket(WS_URL);
    socket.binaryType = 'arraybuffer';
    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') return;
      state.serverEvents.push(event.data.slice(0, 300));
      if (event.data.indexOf('hello.ok') >= 0) state.helloOk = true;
      log('srv ' + event.data.slice(0, 140));
    };
    socket.onclose = () => { state.closed = true; };
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = () => reject(new Error('探针地址连不上：' + WS_URL));
      setTimeout(() => reject(new Error('探针地址连接超时')), 5000);
    });
    socket.send(JSON.stringify({v: 1, type: 'hello', session_id: 'docmind-media-probe'}));
    let audioSequence = 0;
    let videoSequence = 0;
    const send = (type, sequence, body) => {
      const header = new TextEncoder().encode(JSON.stringify(
        {v: 1, type: type, sequence: sequence, captured_at: Date.now()}) + '\\n');
      const packet = new Uint8Array(header.length + body.byteLength);
      packet.set(header, 0);
      packet.set(body, header.length);
      socket.send(packet.buffer);
      state.sent += 1;
      state.bytes += body.byteLength;
    };
    // 生产的 sendNativeAudioReady 规则：provider 在收到第一片音频之前会拒绝图像，
    // 所以任何视频帧出门前必须先把一片 100ms 静音推上去。
    audioSequence += 1;
    send('audio.chunk', audioSequence, new Uint8Array(TARGET_SAMPLES * 2));
    const source = context.createMediaStreamSource(stream);
    const tap = context.createScriptProcessor(2048, 1, 1);
    const sink = context.createGain();
    sink.gain.value = 0;                 // 零增益回接：绝不把夹具原声送回扬声器
    let carry = new Float32Array(0);
    const slice = Math.max(1, Math.round(context.sampleRate * CHUNK_MS / 1000));
    tap.onaudioprocess = (event) => {
      const frame = new Float32Array(event.inputBuffer.getChannelData(0));
      const merged = new Float32Array(carry.length + frame.length);
      merged.set(carry); merged.set(frame, carry.length); carry = merged;
      while (carry.length >= slice && socket.readyState === 1) {
        const window = carry.subarray(0, slice);
        carry = carry.subarray(slice);
        const step = window.length / TARGET_SAMPLES;
        const pcm = new Int16Array(TARGET_SAMPLES);
        for (let i = 0; i < TARGET_SAMPLES; i++) {
          const at = i * step;
          const low = Math.floor(at);
          const high = Math.min(low + 1, window.length - 1);
          const value = window[low] + (window[high] - window[low]) * (at - low);
          pcm[i] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
        }
        audioSequence += 1;
        send('audio.chunk', audioSequence, new Uint8Array(pcm.buffer));
      }
    };
    source.connect(tap); tap.connect(sink); sink.connect(context.destination);
    if (NEED_VIDEO) {
      const track = stream.getVideoTracks()[0];
      if (track) {
        const video = document.createElement('video');
        video.muted = true;
        video.srcObject = new MediaStream([track]);
        await video.play().catch(() => {});
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth || 32;
        canvas.height = video.videoHeight || 32;
        const painter = canvas.getContext('2d');
        for (let n = 0; n < VIDEO_FRAMES; n++) {
          await new Promise((resolve) => setTimeout(resolve, 120));
          painter.drawImage(video, 0, 0, canvas.width, canvas.height);
          const base64 = (canvas.toDataURL('image/jpeg', 0.7).split(',')[1]) || '';
          const binary = atob(base64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          videoSequence += 1;
          send('video.frame', videoSequence, bytes);
          state.video += 1;
        }
      }
    }
    log('sent=' + state.sent + ' video=' + state.video + ' rate=' + state.rate);
  } catch (cause) {
    state.error = String((cause && cause.message) || cause);
    log('ERR ' + state.error);
  }
})();
"""

PROBE_PAGE_HTML = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                   "<title>DocMind 媒体夹具探针</title></head><body>"
                   "<pre id=\"out\">boot</pre><script>\n%s</script></body></html>\n")


_WS_URL_RE = re.compile(r"^ws://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}(?:/[A-Za-z0-9._~-]*)?$")


def probe_page_script(ws_url: str, *, video: bool = False, frames: int = 6,
                      chunk_ms: int = CHUNK_MS) -> str:
    """把夹具页面里的占位符换成实际参数；留一个未替换的占位符就是静默失效。"""
    # 这个值会被塞进 JS 字符串字面量，所以只认回环 ws 地址的形，不做「引号转义」那套。
    if not _WS_URL_RE.match(str(ws_url or "")):
        raise MediaFixtureError("探针地址必须是回环 ws 地址，收到 %r。" % str(ws_url)[:120])
    tokens = {"__WS_URL__": str(ws_url), "__NEED_VIDEO__": "true" if video else "false",
              "__CHUNK_MS__": str(int(chunk_ms)), "__TARGET_SAMPLES__": str(CHUNK_SAMPLES),
              "__VIDEO_FRAMES__": str(int(frames))}
    script = PROBE_PAGE_TEMPLATE
    for token, value in tokens.items():
        if token not in script:
            raise MediaFixtureError("探针页面缺少占位符 %s。" % token)
        script = script.replace(token, value)
    if any(token in script for token in tokens):
        raise MediaFixtureError("探针页面还有未替换的占位符。")
    return script


# ---- 探针服务：懂 R0 协议线的回环 WS ------------------------------------------

class MediaProbe:
    """回环 WebSocket 探针：按 R0 线上协议收包并记账，不接 provider、不碰模型。

    只做三件事：`hello` → 回 `hello.ok`；`cancel` → 回 `cancel.ok`；媒体包 → 解析记账。
    静音尾凑齐后补一条 `model.delta final:true`，模拟「服务端因为用户说完而收口」，
    于是页面侧的收口路径也第一次跑得到了。
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0,
                 tail_chunks: int = TAIL_CHUNKS, auto_finalize: bool = True) -> None:
        if str(host) not in dev_server.LOOPBACK and not str(host).startswith("127."):
            raise MediaFixtureError("探针只允许监听回环地址，收到 %r。" % host)
        self.host = str(host)
        self.requested_port = int(port or 0)
        self.tail_chunks = max(1, int(tail_chunks))
        self.auto_finalize = bool(auto_finalize)
        self.port = 0
        self.packets: list[dict[str, Any]] = []
        self.control: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.violations: list[str] = []
        self.connections = 0
        self.finalized = False
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        return "ws://%s:%d/live" % (self.host, self.port)

    def start(self, timeout: float = 6.0) -> str:
        try:
            import websockets  # noqa: F401  依赖检查，真正的 serve 在线程里
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise MediaFixtureError("探针需要 websockets 依赖。") from exc
        self._thread = threading.Thread(target=self._run, name="docmind-media-probe",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(max(0.5, float(timeout))):
            raise MediaFixtureError(self.error or "探针服务未能在期限内监听。")
        if not self.port:
            raise MediaFixtureError(self.error or "探针服务没有回环端口。")
        return self.url

    def stop(self, timeout: float = 6.0) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        return self.report()

    def report(self) -> dict[str, Any]:
        return {"url": self.url if self.port else "", "packets": list(self.packets),
                "control": list(self.control), "sent": list(self.sent),
                "violations": list(self.violations), "connections": self.connections,
                "finalized": self.finalized, "error": self.error}

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - 线程边界，原因交回主线程判定
            self.error = "探针服务异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
            self._ready.set()

    async def _serve(self) -> None:
        import websockets

        self.port = self.requested_port or dev_server.free_port(self.host)
        async with websockets.serve(self._handle, self.host, self.port, max_size=8_000_000):
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)

    async def _push(self, connection: Any, event: dict[str, Any]) -> None:
        try:
            await connection.send(json.dumps(event, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 客户端已走：不影响已收到的证据
            return
        self.sent.append(event)

    async def _handle(self, connection: Any) -> None:
        self.connections += 1
        opened = time.monotonic()
        silent_run = 0
        saw_speech = False
        try:
            while not self._stop.is_set():
                try:
                    message = await asyncio.wait_for(connection.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 - 连接结束
                    break
                if isinstance(message, (bytes, bytearray, memoryview)):
                    row = self._note_binary(bytes(message), opened)
                    if row and row["kind"] == "audio":
                        if row["label"] == "T":
                            saw_speech = True
                            silent_run = 0
                        elif row["label"] == "." and saw_speech:
                            silent_run += 1
                            if (self.auto_finalize and not self.finalized
                                    and silent_run >= self.tail_chunks):
                                self.finalized = True
                                await self._push(connection, realtime_protocol.server_event(
                                    "model.delta", session_id="docmind-media-probe",
                                    text="", role="assistant", final=True))
                else:
                    await self._note_text(str(message), connection)
        finally:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 - 收尾路径
                pass

    def _note_binary(self, data: bytes, opened: float) -> dict[str, Any] | None:
        parsed = realtime_protocol.parse_binary_packet(data)
        if not parsed:
            head, _, _ = data.partition(b"\n")
            self.violations.append("包不符合 R0 媒体协议：%s"
                                   % head[:120].decode("utf-8", "replace"))
            return None
        header, payload = parsed
        kind = "audio" if header.get("type") == "audio.chunk" else "video"
        rms = round(chunk_rms(payload), 4) if kind == "audio" else 0.0
        row = {"kind": kind, "type": header.get("type"), "sequence": header.get("sequence"),
               "captured_at": header.get("captured_at"), "bytes": len(payload), "rms": rms,
               "at": round(time.monotonic() - opened, 3),
               "label": label_of(rms) if kind == "audio" else "v",
               "jpeg": kind == "video" and payload[:2] == b"\xff\xd8"}
        self.packets.append(row)
        return row

    async def _note_text(self, message: str, connection: Any) -> None:
        try:
            value = json.loads(message)
        except ValueError:
            self.violations.append("控制包不是 JSON：%s" % message[:120])
            return
        if not realtime_protocol.parse_control_packet(value):
            self.violations.append("控制包类型非法：%s" % str(value.get("type"))[:60])
            return
        self.control.append(value)
        event_type = str(value.get("type"))
        if event_type == "hello":
            await self._push(connection, realtime_protocol.server_event(
                "hello.ok", session_id="docmind-media-probe", mode="native-realtime",
                degraded_to=None, reason="", provider="media-probe",
                provider_capabilities=["audio.in", "video.in", "text.out"]))
        elif event_type == "cancel":
            await self._push(connection, realtime_protocol.server_event(
                "cancel.ok", session_id="docmind-media-probe"))
        elif event_type == "session.close":
            await self._push(connection, realtime_protocol.server_event(
                "session.closed", session_id="docmind-media-probe"))


# ---- 交付：夹具构建 + 真实浏览器回环 ------------------------------------------

def fixture_dir(root: str | Path, out: str = "", stamp: str = "") -> Path:
    """默认写进 `.docmind/media-fixture/`（已 gitignore）；显式 `out` 才落在项目可见处。

    多人同仓时把临时产物丢进工作树就是给别人埋雷，所以默认位置必须是忽略目录，并且
    `out` 要过一遍越界检查。
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise MediaFixtureError("当前项目目录不存在。")
    requested = str(out or "").strip().replace("\\", "/")
    if requested:
        if requested.startswith("/") or requested.startswith("-") \
                or ".." in [part for part in requested.split("/")]:
            raise MediaFixtureError("夹具输出必须是项目内的相对路径，收到 %r。" % requested)
        target = (base / requested).resolve()
        if base not in target.parents:
            raise MediaFixtureError("夹具输出越出了当前项目：%s" % requested)
    else:
        stamp = str(stamp or "").strip() or (time.strftime("%Y%m%d-%H%M%S") + "-"
                                             + os.urandom(2).hex())
        target = base / ".docmind" / "media-fixture" / stamp
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_fixtures(root: str | Path, *, script: str = "", video_frames: int = 0,
                   out: str = "", stamp: str = "") -> dict[str, Any]:
    """生成 WAV（+ 可选 Y4M）并交回可直接使用的假设备启动参数。"""
    base = Path(root).resolve()
    segments = parse_script(script)
    target = fixture_dir(base, out, stamp)
    pcm = build_pcm(segments)
    wav = write_wav(target / "fixture.wav", pcm)
    frames = max(0, int(video_frames or 0))
    y4m: Path | None = None
    if frames:
        y4m = target / "fixture.y4m"
        y4m.write_bytes(build_y4m(frames=min(MAX_VIDEO_FRAMES, max(2, frames))))
    flags = fake_media_flags(wav, y4m or "")
    duration = sum(item["seconds"] for item in segments)
    return {
        "ok": True, "dir": target, "audio": wav, "video": y4m,
        "script": segments, "duration": round(duration, 3), "pcm_bytes": len(pcm),
        "cells": cell_kinds(segments), "expected_pattern": expected_labels(segments),
        "gate_shape": gate_shape(segments), "flags": flags,
        "expected_packets": int(round(duration * 1000 / CHUNK_MS)),
    }


def _strictly_increasing(values) -> bool:
    ordered = [int(value) for value in values if isinstance(value, int)]
    return all(left < right for left, right in zip(ordered, ordered[1:]))


def verdict(report: dict[str, Any], fixtures: dict[str, Any], state: dict[str, Any],
            *, video: bool = False, tail_chunks: int = TAIL_CHUNKS) -> dict[str, Any]:
    """把「页面上确实发生了采集」与「线上契约成立」拆成一条条可复核的判定。"""
    packets = report.get("packets") or []
    audio = classify(packets)
    video_rows = [row for row in packets if row.get("kind") == "video"]
    violations = report.get("violations") or []
    expected = str(fixtures.get("expected_pattern") or "")
    match = pattern_match(audio["pattern"], expected)
    # 音频与视频各自有独立计数器（生产 `liveAudioSequence` 就是这样），所以单调性必须
    # 按种类分别判，跨种类比大小会把正常的一轮判成乱序。
    monotonic = (_strictly_increasing(row["sequence"] for row in packets
                                      if row.get("kind") == "audio")
                 and _strictly_increasing(row["sequence"] for row in packets
                                          if row.get("kind") == "video"))
    first_audio = next((row["at"] for row in packets if row.get("kind") == "audio"), None)
    first_video = next((row["at"] for row in video_rows), None)
    error = str(state.get("error") or "")
    checks = {
        "page_captured_media": bool(int(state.get("tracks") or 0)) and not error,
        "no_page_errors": not error,
        "hello_ok_returned": bool(state.get("helloOk")),
        "packets_arrived": audio["count"] > 0,
        "protocol_valid": not violations,
        "sequence_monotonic": monotonic,
        "chunk_shape": bool(audio["count"]) and all(
            row.get("bytes") == CHUNK_BYTES for row in packets if row.get("kind") == "audio"),
        "content_matches_fixture": bool(expected) and match["score"] >= 0.8,
        "speech_amplitude_ok": bool(audio["speech_cells"]) and abs(
            float(audio["speech_rms_median"]) - LOUD_RMS) <= RMS_TOLERANCE,
        "silence_tail_sent": audio["longest_silence_run"] >= int(tail_chunks),
        "tail_closed_the_turn": bool(report.get("finalized")),
    }
    if video:
        checks["video_frames_sent"] = len(video_rows) > 0
        checks["video_frames_are_jpeg"] = bool(video_rows) and all(row.get("jpeg") for row in video_rows)
        checks["audio_before_video"] = (first_audio is not None and first_video is not None
                                        and first_audio <= first_video)
    return {"checks": checks, "passed": all(checks.values()), "audio": audio,
            "video_packets": len(video_rows), "pattern_match": match,
            "page_state": {key: state.get(key) for key in
                           ("tracks", "rate", "sent", "bytes", "video", "error", "closed")},
            "violations": violations[:5], "page_errors": [error] if error else [],
            "sent_events": report.get("sent") or [], "connections": report.get("connections") or 0,
            "probe_error": report.get("error") or ""}


def run_media_loop(root: str | Path, *, script: str = "", video_frames: int = 0,
                   wait: float = 0.0, tail_chunks: int = TAIL_CHUNKS,
                   out: str = "", timeout: float = 40.0) -> dict[str, Any]:
    """跑一次完整回环：合成媒体 → 真实浏览器采集 → R0 协议包 → 探针记账 → 判定。"""
    base = Path(root).resolve()
    if not base.is_dir():
        raise MediaFixtureError("当前项目目录不存在。")
    frames = max(0, int(video_frames or 0))
    fixtures = build_fixtures(base, script=script, video_frames=frames, out=out)
    duration = float(fixtures["duration"])
    budget = float(wait or 0.0) or min(MAX_WAIT, max(4.0, duration + 3.0))
    probe = MediaProbe(tail_chunks=tail_chunks)
    url = probe.start()
    page = fixtures["dir"] / "probe.html"
    page.write_text(PROBE_PAGE_HTML % probe_page_script(
        url, video=bool(frames), frames=max(2, min(frames or 6, 24))),
        encoding="utf-8", newline="\n")
    server, thread = visual.serve_static(fixtures["dir"])
    page_url = ("http://127.0.0.1:%d/%s?docmind=%d"
                % (server.server_port, page.name, time.time_ns()))
    state: dict[str, Any] = {}
    console_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    screenshot = ""
    browser_error = ""
    try:
        with visual.browser_session(timeout, fixtures["flags"]) as devtools:
            devtools.call("Page.navigate", {"url": page_url})
            # 预算跟着 `timeout` 走（有上限），不再写死 120 次 × 50ms = 6 秒。
            # 预算只有一份，在 visual_acceptance.page_ready_js。
            devtools.evaluate(visual.page_ready_js(timeout))
            deadline = time.monotonic() + budget
            expected_packets = int(fixtures["expected_packets"]) + (frames or 0)
            while time.monotonic() < deadline:
                raw = devtools.evaluate("JSON.stringify(window.__docmindMedia||{})")
                try:
                    state = json.loads(str(raw or "{}"))
                except ValueError:
                    state = {}
                if str(state.get("error") or ""):
                    break
                if int(state.get("sent") or 0) >= expected_packets:
                    break
                time.sleep(0.25)
            devtools.drain(0.6)
            console_errors = [item for item in devtools.console if item["level"] == "error"]
            failed_requests = list(devtools.failed_requests)
            shot = devtools.call("Page.captureScreenshot", {"format": "png"})
            encoded = str(shot.get("data") or "")
            if encoded:
                (fixtures["dir"] / "probe.png").write_bytes(base64.b64decode(encoded))
                screenshot = str((fixtures["dir"] / "probe.png").relative_to(base).as_posix())
    except visual.VisualAcceptanceError as exc:
        browser_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 回环失败要报原因，不能报通过
        browser_error = "浏览器回环失败：%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        visual.stop_static(server, thread)
        report = probe.stop()
    result = verdict(report, fixtures, state, video=bool(frames), tail_chunks=tail_chunks)
    result.update({
        "ok": bool(result["passed"]) and not browser_error and not console_errors
                and not failed_requests,
        "browser_error": browser_error, "console_errors": console_errors,
        "failed_requests": failed_requests, "screenshot": screenshot,
        "page_url": page_url, "probe_url": url, "duration": duration,
        "expected_pattern": fixtures["expected_pattern"], "flags": fixtures["flags"],
        "artifacts": {key: str(fixtures[key]) for key in ("dir", "audio") if fixtures.get(key)},
        "video_artifact": str(fixtures.get("video") or ""),
    })
    if browser_error:
        result["checks"]["browser_available"] = False
        result["passed"] = False
    (fixtures["dir"] / "run.json").write_text(
        json.dumps({key: value for key, value in result.items() if key != "flags"},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8", newline="\n")
    return result


def render_fixtures(fixtures: dict[str, Any]) -> str:
    """build 的文字结果：路径 + 内容算式 + 可直接粘进 `preview_project` 的 flags。"""
    shape = fixtures.get("gate_shape") or {}
    lines = [
        "夹具已生成（%s 秒，%s 片）。" % (fixtures["duration"], fixtures["expected_packets"]),
        "音频：%s" % fixtures["audio"],
    ]
    if fixtures.get("video"):
        lines.append("视频：%s" % fixtures["video"])
    lines.append("理想 pattern：%s（T=说话 q=低于门限 .=静音）" % fixtures["expected_pattern"])
    lines.append("门限形状：talk 帧 RMS %s~%s（≥%s 才开门），非 talk 最大 %s，跨界帧剔除 %s 片。"
                 % (shape.get("loud_min"), shape.get("loud_max"), GATE_START_RMS,
                    shape.get("quiet_max"), shape.get("mixed_frames_skipped")))
    if not (shape.get("opens_gate") and shape.get("closes_gate")):
        lines.append("注意：这份夹具开关不了前端的能量门，别拿它验语音回路。")
    lines.append("假设备启动参数（可直接放进 preview_project 的 flags: 里，每行一条）：")
    lines.extend("  " + flag for flag in fixtures["flags"])
    lines.append("要跑完整回环并用 R0 协议逐包判定，用 dev_media action: run。")
    return "\n".join(lines)


def render(result: dict[str, Any]) -> str:
    """给人/模型读的判定文本：先说结论，再给可复核的数字证据。"""
    checks = result.get("checks") or {}
    audio = result.get("audio") or {}
    lines = ["媒体回环【%s】：%d/%d 项通过。" % (
        "通过" if result.get("ok", result.get("passed")) else "未通过",
        sum(1 for value in checks.values() if value), len(checks))]
    if result.get("browser_error"):
        lines.append("浏览器不可用：%s" % result["browser_error"])
    if result.get("page_errors"):
        lines.append("页面错误：%s" % str(result["page_errors"][0])[:200])
    for name in sorted(checks):
        if not checks[name]:
            lines.append("未通过项：%s" % name)
    lines.append("收到音频片 %s（speech %s / quiet %s / silence %s），最长静音连段 %s。"
                 % (audio.get("count"), audio.get("speech_cells"), audio.get("quiet_cells"),
                    audio.get("silent_cells"), audio.get("longest_silence_run")))
    lines.append("pattern %s（夹具 %s，匹配 %s%%）" % (
        audio.get("pattern"), result.get("expected_pattern"),
        round(float((result.get("pattern_match") or {}).get("score") or 0) * 100)))
    lines.append("speech 中位 RMS %s（理想 %s），片间隔中位 %sms / 最大 %sms。" % (
        audio.get("speech_rms_median"), round(LOUD_RMS, 4),
        round(float(audio.get("gap_median") or 0) * 1000),
        round(float(audio.get("gap_max") or 0) * 1000)))
    if result.get("video_packets"):
        lines.append("视频帧 %s 片。" % result.get("video_packets"))
    if result.get("violations"):
        lines.append("协议违规 %s 条：%s" % (len(result["violations"]),
                                          " / ".join(result["violations"])))
    if result.get("screenshot"):
        lines.append("截图：%s" % result["screenshot"])
    if result.get("artifacts"):
        lines.append("夹具产物：%s" % json.dumps(result["artifacts"], ensure_ascii=False))
    lines.append("说明：静音尾/包形/顺序是线上可观测的；能量门限本身不在这条回路里跑。")
    return "\n".join(lines)


__all__ = ["DEFAULT_SCRIPT", "MediaFixtureError", "MediaProbe", "build_fixtures",
           "build_pcm", "build_y4m", "cell_kinds", "chunk_rms", "classify",
           "expected_labels", "fake_media_flags", "fixture_dir", "frame_rms_profile",
           "gate_shape", "parse_script", "pattern_match", "probe_page_script",
           "render", "render_fixtures", "run_media_loop", "verdict", "write_wav"]
