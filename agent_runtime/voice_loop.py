"""Wave8（选项3）：不装 vitest，用真浏览器 + 假麦克风验【语音接线】的行为。

vitest 那一步的代价量化完决定不走（见 HANDOFF：1 critical + 3 moderate 的 dev 公告，
且 vitest 3 要 vite 6/7 而本树是 vite 5）。这里改成验行为：

* 用前端**自带**的 esbuild（零新依赖）把生产模块 `liveAudioControl.ts` /
  `realtimeProtocol.ts` 编成浏览器可跑的 ESM——页面 import 的是编译后的生产模块本体，
  不是我手抄的逻辑副本；
* 页面里的胶水是照抄 `AutonomousCockpit.vue` 的 `sendLiveAudioChunk` /
  `sendNativeAudioReady` / `handleLiveAudioFrame` / `startLiveMic` 那几段（同样的调用、
  同样的顺序、同样的 tail 期零值替换与 `pump.pending()` 判定）；
* Wave7 的假设备开关把合成麦克风内容喂进真的 `getUserMedia`；
* Wave13 的 `dev_page_action` 真的点「连接 / 开麦 / AI 开始说话」。

**这条能验而 Wave7 验不到的那一半**：Wave7 的探针页面刻意不接能量门、逐帧无条件推流，
所以它只能证明"采集→线上"。这里页面接上了真门限，于是可以断言：
① 静音期不发流（只允许 hello.ok 之后生产必发的那 1 片 audio-ready 静音标记）；
② 说完之后还必须再推 ≥10 片静音（1 秒）
才停——就是 9/29 那次「转写完整却零回复」的契约；③ 推完必须**停下来**，不是无条件泵；
④ AI 说话时用户开口要发出 `cancel`（抢话）；⑤ 所有片都是 16k mono 100ms＝3200 字节
（顺带证明浏览器里那次 48k→16k 重采样真的做了）。

**边界（不许越界宣称）**：页面里那段胶水是我对照 `AutonomousCockpit.vue` 转写的，
不是从 `.vue` 里 import 出来的——`.vue` 的接线本身仍然没有组件级守卫，这里防的是
"生产函数被这样接起来时行为对不对"。真网关（`api.py` + DashScope）那条路仍归 R12 人跑。
另有一条实测事实：`quiet` 段在【生成的 WAV 里】RMS≈0.0057（低于 `startRms` 0.02），但经
Chromium 的 AEC/NS 链路后能量会被抬到足以开门——所以"低于门限"只对文件成立，不能拿它在
回路上断言"绝不开门"；要断言"门没开"就用纯 `sil`（实测只有 1 片 audio-ready 标记、
零片响亮音频，且永不收口）。
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from agent_runtime import media_fixture, page_action, visual_acceptance as visual

ESBUILD_ENTRY = ("node_modules", "esbuild", "bin", "esbuild")
PRODUCTION_MODULES = {
    "liveAudioControl.mjs": ("src", "workbench", "liveAudioControl.ts"),
    "realtimeProtocol.mjs": ("src", "workbench", "realtimeProtocol.ts"),
}
# 胶水必须调用到的生产符号：抄漏一个，行为就不再是那条真回路。
GLUE_CALLS = ("createVoiceGate", "createChunkPump", "encodeAudioPacket", "frameRms",
              "resample", "realtimeHello", "realtimeCancel", "parseRealtimeServerEvent",
              "TARGET_SAMPLE_RATE", "REALTIME_PROTOCOL_VERSION")
DEFAULT_SCRIPT = "sil:0.8 talk:1.0 sil:3.0"
TAIL_MIN = 10            # 静音尾下限：1 秒 @100ms
TAIL_MAX = 16            # 上限：明显超过就说明在无条件泵，而不是门限在收
RESUME_GAP_SECONDS = 0.4  # 尾后到下次开门的最小间隔
MAX_WAIT = 45.0
_WS_URL_RE = re.compile(r"^ws://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}(?:/[A-Za-z0-9._~-]*)?$")


class VoiceLoopError(RuntimeError):
    """语音回路无法构造或无法证实。"""


def frontend_dir(root: str | Path) -> Path:
    base = Path(root).resolve()
    for candidate in (base / "frontend", base):
        if (candidate / "src" / "workbench" / "liveAudioControl.ts").is_file():
            return candidate
    raise VoiceLoopError("找不到前端源码（需要 frontend/src/workbench/liveAudioControl.ts）。")


def bundle_command(node: str, frontend: Path, source: str, out: Path) -> list[str]:
    return [node, str(frontend.joinpath(*ESBUILD_ENTRY)), "--bundle", source,
            "--format=esm", f"--outfile={out}"]


def build_bundle(frontend: Path, target: Path, *, runner: Callable = subprocess.run) -> dict[str, Any]:
    """用前端自带的 esbuild 把生产模块编成 ESM；编不出来就是失败，绝不退回"抄一份"。"""
    node = shutil.which("node") or ""
    if not node:
        return {"ok": False, "error": "找不到 node，无法编译生产模块。"}
    if not frontend.joinpath(*ESBUILD_ENTRY).is_file():
        return {"ok": False, "error": "前端没有 esbuild（先在该目录 npm ci）。"}
    target.mkdir(parents=True, exist_ok=True)
    built: dict[str, str] = {}
    for name, parts in PRODUCTION_MODULES.items():
        source = "/".join(parts)
        out = (target / name).resolve()
        # 先删掉旧产物：否则上一次留下的文件会让"这次编译失败"看起来是成功的。
        with contextlib.suppress(FileNotFoundError):
            out.unlink()
        try:
            proc = runner(bundle_command(node, frontend, source, out), cwd=str(frontend),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "esbuild 编译 %s 超时（120s）。" % source}
        if proc.returncode != 0:
            return {"ok": False,
                    "error": "esbuild 编译 %s 失败：%s" % (source, (proc.stderr or proc.stdout or "")[-300:])}
        if not out.is_file() or out.stat().st_size == 0:
            return {"ok": False, "error": "esbuild 声称成功但没有产出 %s。" % name}
        built[name] = out.name
    return {"ok": True, "files": built, "node": node}


VOICE_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>语音回路行为页</title></head>
<body>
<h1>Agent 工作台 语音回路行为页</h1>
<button id="connect">连接</button><button id="mic">开麦</button><button id="stopmic">关麦</button>
<button id="aitalk">AI 开始说话</button><button id="aiidle">AI 停止说话</button>
<pre id="state">{}</pre>
<script type="module">
import { createVoiceGate, createChunkPump, encodeAudioPacket, frameRms, resample,
         TARGET_SAMPLE_RATE } from './liveAudioControl.mjs';
import { REALTIME_PROTOCOL_VERSION, realtimeHello, realtimeCancel,
         parseRealtimeServerEvent } from './realtimeProtocol.mjs';

const WS_URL = "__WS_URL__";
const state = window.__voice = { mic: 'off', connected: false, helloOk: false, speaking: false,
                                 sent: 0, bytes: 0, tailDone: 0, cancelled: 0, sampleRate: 0,
                                 errors: [], serverEvents: [] };
const show = () => { document.getElementById('state').textContent = JSON.stringify(state); };
let socket = null, stream = null, context = null, tap = null, gate = createVoiceGate(),
    pump = createChunkPump(), sequence = 0, nativeReady = false;

function sendLiveAudioChunk(payload) {                 // == 生产 sendLiveAudioChunk
  if (!socket || socket.readyState !== WebSocket.OPEN || socket.bufferedAmount > 500000) return false;
  sequence += 1;
  try {
    socket.send(encodeAudioPacket({ version: REALTIME_PROTOCOL_VERSION, sequence: sequence,
                                    capturedAt: Date.now(), payload }));
    state.sent += 1; state.bytes += payload.byteLength;
    return true;
  } catch (cause) { state.errors.push('send:' + cause.name); return false; }
}
function sendNativeAudioReady() {                      // == 生产：图像之前先推一片 100ms 静音
  if (nativeReady) return true;
  const sent = sendLiveAudioChunk(new Uint8Array(3200));
  if (sent) nativeReady = true;
  return sent;
}
function handleLiveAudioFrame(frame) {                 // == 生产 handleLiveAudioFrame
  const signal = gate.feed(frameRms(frame), Date.now(), state.speaking);
  if (signal.bargeIn) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify(realtimeCancel('用户抢话'))); state.cancelled += 1; }
      catch (cause) { state.errors.push('cancel:' + cause.name); }
    }
  }
  if (!signal.sending) {
    if (signal.tailDone) { state.tailDone += 1; if (pump.pending() > 0) pump.reset(); }
    return;
  }
  const tailLength = Math.round(frame.length * TARGET_SAMPLE_RATE / context.sampleRate);
  const samples = signal.state === 'tail'
    ? new Float32Array(tailLength)
    : resample(frame, context.sampleRate, TARGET_SAMPLE_RATE);
  for (const chunk of pump.push(samples)) sendLiveAudioChunk(chunk);
  show();
}
function connect() {
  if (socket && socket.readyState <= WebSocket.CONNECTING) return;
  socket = new WebSocket(WS_URL);
  // 生产在新建 socket 时把两个序号都归零（AutonomousCockpit.vue:1431-1434）
  sequence = 0; nativeReady = false;
  socket.onopen = () => { state.connected = true; socket.send(JSON.stringify(realtimeHello('voice-loop', 'docmind-voice-loop'))); show(); };
  socket.onmessage = (event) => {
    const row = parseRealtimeServerEvent(event.data);
    if (!row) return;
    state.serverEvents.push(row.type);
    if (row.type === 'hello.ok') {
      state.helloOk = true;
      sendNativeAudioReady();                    // 生产在 hello.ok 之后就发这片静音标记
    }
    show();
  };
  socket.onerror = () => { state.errors.push('ws-error'); show(); };
}
async function startMic() {
  if (state.mic === 'on') return;
  // 生产同款前置条件：没连上实时流就不开麦（否则一片音频都发不出去，还看不出为什么）
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    state.errors.push('mic-socket-not-open'); show(); return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia(
      { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    context = new AudioContext();
    await context.resume();
    state.sampleRate = context.sampleRate;
    const worklet = `class LiveMicTap extends AudioWorkletProcessor {
  process(inputs) {
    const frame = new Float32Array(inputs[0][0]);
    this.port.postMessage(frame.buffer, [frame.buffer]);
    return true;
  }
}
registerProcessor('live-mic-tap', LiveMicTap)`;
    const url = URL.createObjectURL(new Blob([worklet], { type: 'application/javascript' }));
    await context.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    tap = new AudioWorkletNode(context, 'live-mic-tap');
    tap.port.onmessage = (message) => handleLiveAudioFrame(new Float32Array(message.data));
    const sink = context.createGain();
    sink.gain.value = 0;                                 // 零增益回接：不把原声送回扬声器
    tap.connect(sink); sink.connect(context.destination);
    context.createMediaStreamSource(stream).connect(tap);
    // 生产 startLiveMic 只换门与泵；sequence 是在【新建 socket】时归零的（见 connect），
    // 开麦时重置会让 audio-ready 标记片与第一片语音撞同一个序号。
    gate = createVoiceGate(); pump = createChunkPump();
    state.mic = 'on';
  } catch (cause) {
    state.errors.push('mic:' + (cause && cause.name || cause));
    await stopMic();
  }
  show();
}
async function stopMic() {
  state.mic = 'off';
  if (tap) { try { tap.port.onmessage = null; tap.disconnect(); } catch (e) {} tap = null; }
  for (const track of (stream ? stream.getTracks() : [])) track.stop();
  stream = null;
  gate.reset(); pump.reset();        // 生产 stopLiveMic 也是这么收的
  show();
}
document.getElementById('connect').addEventListener('click', connect);
document.getElementById('mic').addEventListener('click', () => { void startMic(); });
document.getElementById('stopmic').addEventListener('click', () => { void stopMic(); });
document.getElementById('aitalk').addEventListener('click', () => { state.speaking = true;
  if (sendNativeAudioReady()) state.audioReady = true; show(); });
document.getElementById('aiidle').addEventListener('click', () => { state.speaking = false; show(); });
show();
window.__voiceReady = true;
</script></body></html>
"""


def voice_page_script(ws_url: str) -> str:
    """把探针地址塞进页面；顺带检查"生产符号真的被调用到了"，抄漏就当场失败。"""
    text = str(ws_url or "")
    if not _WS_URL_RE.match(text):
        raise VoiceLoopError("探针地址必须是回环 ws 地址，收到 %r。" % text[:120])
    page = VOICE_PAGE_TEMPLATE.replace("__WS_URL__", text)
    if "__WS_URL__" in page:
        raise VoiceLoopError("探针地址没有替换掉。")
    # 只看"名字出现过"是废话：import 块本身就写着这十个名字。必须在 import 之后的
    # 函数体里出现，才算胶水真的调了生产实现。
    body = page.split("parseRealtimeServerEvent } from", 1)
    if len(body) != 2:
        raise VoiceLoopError("认不出胶水页的 import 结尾，无法校验调用点。")
    missing = [name for name in GLUE_CALLS if name not in body[1]]
    if missing:
        raise VoiceLoopError("胶水页缺少生产调用：%s" % ", ".join(missing))
    return page


def tail_stats(labels: list[str], gaps: list[float], *, trailing_observation: float = 0.0
               ) -> dict[str, Any]:
    """第一段语音之后的静音连段有多长、尾后隔多久才再来声音。

    `gaps[i]` 是第 i 片的到达间隔，所以"尾后到下一段声音"的间隔是 `gaps[after]`；
    取 `gaps[after-1]` 会量到静音尾【内部】的 100ms，永远看不出流有没有真的停。

    "尾后没再发包"只有在我们**继续观察够了时间**时才是证据：采集按包数提前收工的话，
    尾巴撞到列表末尾会变成假的 `stopped_then_nothing`（实测：一个完全没接门限、逐帧
    无条件推流的页面，只要截断在尾巴中间就能全项通过）。所以这里要求
    `trailing_observation >= RESUME_GAP_SECONDS` 才算停，观察不够就明确标未观测。
    """
    first_speech = next((index for index, label in enumerate(labels) if label == "T"), -1)
    if first_speech < 0:
        return {"first_is_speech": False, "tail_after_speech": 0, "gap_after_tail": 0.0,
                "leading_quiet": 0, "stopped": "not_observed",
                "trailing_observation": round(float(trailing_observation), 3)}
    leading_quiet = sum(1 for label in labels[:first_speech] if label != "T")
    index = first_speech
    while index < len(labels) and labels[index] == "T":
        index += 1
    tail = 0
    while index + tail < len(labels) and labels[index + tail] in (".", "q"):
        tail += 1
    after = index + tail
    if after >= len(labels):
        stopped = ("yes" if trailing_observation >= RESUME_GAP_SECONDS else "not_observed")
    else:
        gap = round(float(gaps[after]) if after < len(gaps) else 0.0, 3)
        stopped = "yes" if gap >= RESUME_GAP_SECONDS else "no"
    return {"first_is_speech": first_speech == 0, "tail_after_speech": tail,
            "gap_after_tail": (gap if after < len(labels) else 0.0),
            "leading_quiet": leading_quiet, "stopped": stopped,
            "trailing_observation": round(float(trailing_observation), 3)}


def click_labels(barge_in: bool) -> tuple[str, ...]:
    """真回路上依次要点的按钮文案。必须是【字符串】：写成元组时定位会静默失配，
    整条回路退化成"只点了连接"，现场测试仍然能跑出个看似合理的 pattern。"""
    return ("连接", "开麦", "AI 开始说话") if barge_in else ("连接", "开麦")


def verdict(packets: list[dict[str, Any]], control: list[dict[str, Any]], sent: list[dict[str, Any]],
            *, state: dict[str, Any], violations: list[str],
            tail_min: int = TAIL_MIN, tail_max: int = TAIL_MAX,
            expect_barge_in: bool = True, trailing_observation: float = 0.0,
            console_errors: list | None = None, failed_requests: list | None = None,
            connections: int = 1) -> dict[str, Any]:
    audio = [row for row in packets if row.get("kind") == "audio"]
    labels = [str(row.get("label") or "?") for row in audio]
    gaps = [float(row.get("gap") or 0.0) for row in audio]
    stats = tail_stats(labels, gaps, trailing_observation=trailing_observation)
    raw_sequences = [row.get("sequence") for row in audio]
    sequences = [int(value) for value in raw_sequences if isinstance(value, int)]
    types = [str(row.get("type")) for row in control]
    # 页面里 `state.serverEvents` 存的是【事件名字符串】（push(row.type)），不是对象。
    server_events = [row.get("type") if isinstance(row, dict) else str(row)
                     for row in (state.get("serverEvents") or [])]
    errors = list(state.get("errors") or []) + [str(item.get("text") or "")
                                                for item in (console_errors or [])]
    page_failed = [str(item.get("url") or "") for item in (failed_requests or [])]
    # 生产在 hello.ok 之后就发一片 100ms 静音标记（`sendNativeAudioReady`），
    # 与有没有点"AI 开始说话"无关，所以前导静默允许恰好 1 片；多于 1 片就是门在漏流。
    allowed_leading = 1
    checks = {
        "hello_ok_returned": bool(state.get("helloOk")) and any(
            row.get("type") == "hello.ok" for row in sent),
        "mic_started": str(state.get("mic") or "") == "on",
        "no_page_errors": not errors and not page_failed,
        "protocol_valid": not violations,
        "chunks_arrived": len(audio) > 0,
        "chunk_is_16k_100ms": bool(audio) and all(row.get("bytes") == media_fixture.CHUNK_BYTES
                                                  for row in audio),
        # 单包、或 sequence 根本不是整数时不能算"单调"——那等于没判。
        "sequence_monotonic": len(sequences) >= 2 and len(sequences) == len(audio)
                              and all(left < right for left, right in zip(sequences, sequences[1:])),
        "gate_closed_during_silence": stats["leading_quiet"] <= allowed_leading,
        "silence_tail_at_least_1s": stats["tail_after_speech"] >= tail_min,
        "tail_is_bounded_not_always_pumping": 0 < stats["tail_after_speech"] <= tail_max,
        "stream_really_stops_after_tail": stats["stopped"] == "yes",
        # 探针自己数的收口不算证据：必须页面也真的收到了那条 final 事件。
        "tail_closed_the_turn": any(row.get("type") == "model.delta" and row.get("final") is True
                                    for row in sent) and "model.delta" in server_events,
        "single_connection": int(connections or 0) == 1,
    }
    if expect_barge_in:
        checks["barge_in_sent_cancel"] = "cancel" in types
    return {"checks": checks, "passed": all(checks.values()), "packets": len(audio),
            "pattern": "".join(labels), "tail": stats,
            "thresholds": {"tail_min": tail_min, "tail_max": tail_max},
            "page_state": {key: state.get(key) for key in
                           ("mic", "connected", "helloOk", "sent", "bytes", "tailDone",
                            "cancelled", "sampleRate", "speaking")},
            "page_errors": errors[:5], "violations": list(violations[:5]),
            "server_events": [row.get("type") for row in sent], "control_types": types}


def run_voice_loop(root: str | Path, *, script: str = "", wait: float = 0.0,
                   tail_min: int = TAIL_MIN, tail_max: int = TAIL_MAX,
                   barge_in: bool = True, timeout: float = 40.0) -> dict[str, Any]:
    """编译生产模块 → 假麦克风 → 真点击 → 探针收包 → 逐条判定。"""
    base = Path(root).resolve()
    if not base.is_dir():
        return {"ok": False, "passed": False, "checks": {}, "error": "当前项目目录不存在。"}
    try:
        frontend = frontend_dir(base)
    except VoiceLoopError as exc:
        # 失败要有统一的形状：调用方（工具/CI）只看 ok/error，不会拿到半截结果。
        return {"ok": False, "passed": False, "checks": {}, "error": str(exc)}
    segments = media_fixture.parse_script(script or DEFAULT_SCRIPT)
    duration = sum(item["seconds"] for item in segments)
    fixtures = media_fixture.build_fixtures(base, script=script or DEFAULT_SCRIPT)
    bundle = build_bundle(frontend, fixtures["dir"])
    if not bundle["ok"]:
        return {"ok": False, "passed": False, "error": bundle["error"], "checks": {},
                "artifacts": {"dir": str(fixtures["dir"])}}
    probe = media_fixture.MediaProbe(tail_chunks=max(1, int(tail_min)))
    server = thread = None
    page_id = ""
    state: dict[str, Any] = {}
    console_errors: list[Any] = []
    failed_requests: list[Any] = []
    started_error = ""
    session: dict[str, Any] = {}
    target = ""
    report: dict[str, Any] = {"packets": [], "control": [], "sent": [], "violations": [],
                              "connections": 0}
    trailing = 0.0
    try:
        try:
            url = probe.start()
        except media_fixture.MediaFixtureError as exc:
            raise VoiceLoopError(str(exc)) from exc
        page_file = fixtures["dir"] / "voice.html"
        page_file.write_text(voice_page_script(url), encoding="utf-8", newline="\n")
        server, thread = visual.serve_static(fixtures["dir"])
        target = "http://127.0.0.1:%d/%s" % (server.server_port, page_file.name)  # noqa: E501
        session = page_action.open_page(base, url=target,
                                        flags=media_fixture.fake_media_flags(fixtures["audio"]),
                                        timeout=timeout)
        page_id = session["page"]
        console_errors = list(session.get("console_errors") or [])
        failed_requests = list(session.get("failed_requests") or [])
        for label in click_labels(barge_in):
            click = page_action.click(base, page=page_id, text=label)
            console_errors += list(click.get("console_errors") or [])
            failed_requests += list(click.get("failed_requests") or [])
        budget = float(wait or 0.0) or min(MAX_WAIT, max(6.0, duration + 4.0))
        deadline = time.monotonic() + budget
        want = int(round((duration - segments[-1]["seconds"]) * 10)) + tail_min + 2
        seen = 0
        last_packet_wall = time.monotonic()
        while time.monotonic() < deadline:
            rows = probe.report()["packets"]
            if len(rows) > seen:
                seen = len(rows)
                last_packet_wall = time.monotonic()
            # 采够包之后还要再观察一段"确实没有新包"，否则"停下来"就没有证据：
            # 提前收工会让【完全没接门限、逐帧无条件推流】的页面在截断处判成全过。
            if seen >= want and time.monotonic() - last_packet_wall >= RESUME_GAP_SECONDS + 0.3:
                break
            time.sleep(0.2)
        trailing = round(time.monotonic() - last_packet_wall, 3)
        read = page_action.read_page(base, page=page_id, selector="#state", mode="text")
        try:
            state = json.loads(str(read.get("text") or "{}"))
        except ValueError:
            state = {"errors": ["页面状态读不回来：%s" % str(read.get("text"))[:120]]}
        click = page_action.click(base, page=page_id, text="关麦")
        console_errors += list(click.get("console_errors") or [])
        failed_requests += list(click.get("failed_requests") or [])
        report = probe.stop()
    except (VoiceLoopError, media_fixture.MediaFixtureError, page_action.PageActionError,
            visual.VisualAcceptanceError) as exc:
        started_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 回环失败要报原因，不能报通过
        started_error = "语音行为回路异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        with contextlib.suppress(Exception):
            probe.stop()
        if page_id:
            with contextlib.suppress(Exception):
                page_action.close_page(page_id)
        visual.stop_static(server, thread)
    report.setdefault("packets", [])
    audio = [row for row in report["packets"] if row.get("kind") == "audio"]
    for index, row in enumerate(audio):
        previous = float(audio[index - 1]["at"]) if index else None
        row["gap"] = round(float(row["at"]) - previous, 3) if previous is not None \
            else round(float(row["at"]), 3)
    if started_error:
        return {"ok": False, "passed": False, "error": started_error,
                "checks": {"loop_ran": False}, "packets": len(audio),
                "trailing_observation": trailing,
                "artifacts": {"dir": str(fixtures["dir"])}, "page_url": target}
    result = verdict(report["packets"], report["control"], report["sent"], state=state,
                     violations=report["violations"], tail_min=tail_min, tail_max=tail_max,
                     expect_barge_in=barge_in, trailing_observation=trailing,
                     console_errors=console_errors, failed_requests=failed_requests,
                     connections=report.get("connections", 0))
    result.update({"ok": result["passed"], "trailing_observation": trailing,
                   "artifacts": {"dir": str(fixtures["dir"]), "page": page_file.name,
                                 "audio": str(fixtures["audio"])},
                   "expected_pattern": fixtures["expected_pattern"], "duration": duration,
                   "opened": {key: session.get(key) for key in ("page", "url", "title")}})
    (fixtures["dir"] / "run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        newline="\n")
    return result


def render(result: dict[str, Any]) -> str:
    if not result.get("checks"):
        return "语音行为回路未能开始：%s" % result.get("error", "未知原因")
    checks = result.get("checks") or {}
    failed = [name for name in sorted(checks) if not checks[name]]
    head = "语音行为回路【%s】：%d/%d 项通过。" % (
        "通过" if result.get("ok") else "未通过", len(checks) - len(failed), len(checks))
    lines = [head]
    if result.get("error"):
        lines.append("未能开始的原因：%s" % result["error"])
    state = result.get("page_state") or {}
    lines.append("页面：mic=%s 采样率=%s 发出片=%s 字节=%s tailDone=%s 抢话 cancel=%s" % (
        state.get("mic"), state.get("sampleRate"), state.get("sent"), state.get("bytes"),
        state.get("tailDone"), state.get("cancelled")))
    tail = result.get("tail") or {}
    thresholds = result.get("thresholds") or {}
    lines.append("静音尾：第一段语音后连推 %s 片（要求 %s~%s），尾后间隔 %s 秒再来声音，"
                 "尾后继续观察 %s 秒，流是否真的停：%s；语音前静默片数 %s。" % (
                     tail.get("tail_after_speech"),
                     thresholds.get("tail_min", TAIL_MIN),
                     thresholds.get("tail_max", TAIL_MAX),
                     tail.get("gap_after_tail"), tail.get("trailing_observation", 0.0),
                     tail.get("stopped"), tail.get("leading_quiet")))
    lines.append("pattern：%s" % str(result.get("pattern") or "")[:80])
    for name in failed:
        lines.append("未通过项：%s" % name)
    for item in (result.get("page_errors") or [])[:3]:
        lines.append("页面错误：%s" % str(item)[:160])
    for item in (result.get("violations") or [])[:3]:
        lines.append("协议违规：%s" % str(item)[:160])
    if result.get("artifacts"):
        lines.append("产物：%s" % json.dumps(result["artifacts"], ensure_ascii=False))
    lines.append("边界：验的是【生产函数这样接起来的行为】；.vue 里的接线本身不是被 import 出来的，"
                 "真网关与听感仍归 R12。")
    return "\n".join(lines)


__all__ = ["DEFAULT_SCRIPT", "GLUE_CALLS", "TAIL_MAX", "TAIL_MIN", "VoiceLoopError",
           "build_bundle", "bundle_command", "click_labels", "frontend_dir", "render",
           "run_voice_loop", "tail_stats", "verdict", "voice_page_script"]
