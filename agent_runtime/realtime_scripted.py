"""`scripted`：零网络的离线实时适配器——把整条实时回路跑通，而不碰任何厂商。

**它存在的唯一理由**：`tests/test_realtime_gateway_bridge.py` 已经用进程内的假 provider
验过网关那一半，但**没有任何自动测试跑过"真页面 + 真麦克风 + 真 ws 协议"**：那条链路
以前只能靠人对着麦克风说话（R12）。有了这个适配器，`dev_apptest` 就能用 Wave7 的合成
媒体夹具喂进真 Edge，让上行 PCM 真的穿过 `/api/vision/live-stream`，再断言页面上长出
本轮才可能出现的数字。全程零网络、零 key、零模型调用。

**它不是什么的边界（重要）**：
- 它**不**证明厂商事件与打断语义正确——那些仍归 R12 的人跑；它证明的是**我们自己的
  接线**（协议封包、桥、泵、前端 reducer、DOM）在真事件流下是通的。
- 它**不**产生任何"模型理解了你说的话"的证据：回合内容全部由**收到的字节算出来的统计量**
  拼成（片数 / 字节数 / 峰值 RMS）。这恰恰是它作为测试夹具的价值——回包里出现的数字
  只能来自真实到达的音频，缓存或写死的绿灯冒充不了。

**协议契约（照 `realtime_omni.py` 的真实适配器逐条对齐，不自己发明字段）**：
- 上行音频是 pcm16 mono 16 kHz，一片 100 ms = 3200 字节（`liveAudioControl.ts`）。
- `EVENT_TRANSCRIPT` 载荷 `{text, role:"user", final, language}`；`EVENT_TEXT_DELTA`
  载荷 `{text, role:"assistant"[, final]}`，最后一条带完整文本 + `final: True`
  （桥会按生产惯例把 final 当权威文本，见 `realtime_bridge.py:539`）。
- `EVENT_AUDIO_DELTA` 载荷里的 `audio` 是**裸 bytes**，由 `wire_events()` 负责 base64；
  `encoding` 沿用厂商用的 `"pcm24"`——它的含义是"24 kHz 的 pcm16"，不是 24-bit
  （`liveAudioControl.ts:19-21` 与 `realtime_omni.py:93`）。
- `EVENT_STATUS` 不上线但会让桥记 `native_ready`；`EVENT_DONE` 被桥换成"这一轮收口"标记。
"""
from __future__ import annotations

import math
import struct
import time
from typing import Any

from .realtime_provider import (
    CAP_AUDIO_IN,
    CAP_AUDIO_OUT,
    CAP_INTERRUPT,
    CAP_TEXT_OUT,
    CAP_VIDEO_IN,
    EVENT_AUDIO_DELTA,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_STATUS,
    EVENT_TEXT_DELTA,
    EVENT_TRANSCRIPT,
    RealtimeEvent,
    RealtimeProvider,
    register,
)

PROVIDER_NAME = "scripted"
#: 上行：pcm16 mono 16 kHz，一片 100 ms
INPUT_SAMPLE_RATE = 16_000
CHUNK_MS = 100
BYTES_PER_SAMPLE = 2
# 尾静音满这么多片才算"这一轮说完了"。**停止说话 ≠ 停止推流**是生产契约
# （见 tests/test_voice_loop.py 与 Wave8 的记录），夹具因此故意用同一条规则：
# 只差一片就不收口，测试能当场看出这条线断没断。
SILENCE_TAIL_CHUNKS = 10
#: 峰值 RMS 高于它算"有人在说话"。合成夹具是 0.4 振幅正弦（线上实测 RMS≈0.283），
#: 真静音是 0.0，两个数量级以上的间隔不会被环境噪声糊掉。
VOICED_RMS = 0.01
MAX_CHUNK_BYTES = 1_048_576
#: 一轮最多攒多少字节：上游媒体队列有背压（20 片 / 64 KB），这里再自保一次，
#: 免得一个从不间断的说话者把内存吃掉。
MAX_TURN_BYTES = 4_000_000
#: `"pcm24"` = 24 kHz 的 pcm16（厂商线上格式名，与 realtime_omni 保持一致）
OUTPUT_AUDIO_FORMAT = "pcm24"
OUTPUT_SAMPLE_RATE = 24_000
REPLY_PIECES = 3


def pcm_rms(payload: bytes) -> float:
    """归一化到 0~1 的 RMS。奇数长度时丢掉最后半个样本而不是抛错——
    上游封包出错要在页面上以错误事件显现，不该把适配器线程炸掉。"""
    count = len(payload) // BYTES_PER_SAMPLE
    if count <= 0:
        return 0.0
    samples = struct.unpack("<%dh" % count, payload[: count * BYTES_PER_SAMPLE])
    total = sum(sample * sample for sample in samples) / count
    return math.sqrt(total) / 32768.0


def tone_bytes(ms: int = 120, hz: int = 440, amplitude: float = 0.25,
               sample_rate: int = OUTPUT_SAMPLE_RATE) -> bytes:
    """一小段确定性的正弦作为回答音频：能被逐字节复算，所以"回包到了页面"是有凭据的。"""
    total = int(sample_rate * ms / 1000)
    return b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * hz * i / sample_rate)))
        for i in range(total))


class ScriptedRealtimeProvider(RealtimeProvider):
    """按"有人说话 → 尾静音收轮 → 回报实测数字"的剧本行事的离线适配器。"""

    name = PROVIDER_NAME

    def __init__(self, *, session_id: str = "", project_id: str = "",
                 clock=time.monotonic) -> None:
        super().__init__(session_id=session_id, project_id=project_id)
        self._clock = clock
        self._audio_chunks = 0
        self._audio_bytes = 0
        self._frames = 0
        self._turns = 0
        self._interrupts = 0
        self._errors = 0
        self._turn = self._blank_turn()
        self._closed = False

    @staticmethod
    def _blank_turn() -> dict[str, Any]:
        return {"open": False, "bytes": 0, "chunks": 0, "voiced": 0, "peak": 0.0,
                "silence": 0}

    # -- capability / availability -------------------------------------------
    def capabilities(self) -> list[str]:
        return [CAP_AUDIO_IN, CAP_VIDEO_IN, CAP_TEXT_OUT, CAP_AUDIO_OUT, CAP_INTERRUPT]

    def availability(self) -> dict[str, Any]:
        return {"ok": True, "provider": PROVIDER_NAME,
                "reason": "离线剧本适配器：不需要任何 key，也不会联网"}

    def stats(self) -> dict[str, Any]:
        """给测试与状态端点看的自报账——回合数/字节数是**实际收到**的量。"""
        return {"provider": PROVIDER_NAME, "running": self.running,
                "audio_chunks": self._audio_chunks, "audio_bytes": self._audio_bytes,
                "video_frames": self._frames, "turns": self._turns,
                "interrupts": self._interrupts, "errors": self._errors,
                "turn_open": bool(self._turn["open"])}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> bool:
        if self._closed:
            return False
        with self._lock:
            self._running = True
        self._emit(EVENT_STATUS, state="scripted-ready", provider=PROVIDER_NAME)
        return True

    def close(self) -> None:
        with self._lock:
            self._running = False
            self._closed = True
            self._turn = self._blank_turn()

    # -- media input ----------------------------------------------------------
    def send_audio(self, pcm: bytes, captured_at: int = 0) -> bool:
        if self._closed or not self.running:
            return False
        payload = bytes(pcm or b"")
        if not payload:
            return True                     # 空片不算错误，也不算语音
        if len(payload) > MAX_CHUNK_BYTES:
            self._fail("audio_too_large", "剧本适配器拒收过大的音频分片")
            return False
        rms = pcm_rms(payload)
        with self._lock:
            self._audio_chunks += 1
            self._audio_bytes += len(payload)
            turn = self._turn
            if not turn["open"]:
                if rms < VOICED_RMS:
                    return True             # 还没开始说：静音片直接丢弃
                turn.update({"open": True, "bytes": 0, "chunks": 0, "voiced": 0,
                             "peak": 0.0, "silence": 0})
            turn["chunks"] += 1
            turn["bytes"] += len(payload)
            turn["peak"] = max(turn["peak"], rms)
            if rms >= VOICED_RMS:
                turn["voiced"] += 1
                turn["silence"] = 0
            else:
                turn["silence"] += 1
            if turn["bytes"] > MAX_TURN_BYTES:
                # `_fail` 里面还要拿一次锁：基类的是**非重入** Lock，在锁内调用就是死锁
                # （这条是被 `test_a_never_ending_turn_is_bounded_and_says_so` 当场钉住的）。
                over_cap = True
                self._turn = self._blank_turn()
            else:
                over_cap = False
            ready = turn["silence"] >= SILENCE_TAIL_CHUNKS
            snapshot = dict(turn)
            if ready:
                self._turn = self._blank_turn()
        if over_cap:
            self._fail("turn_too_long", "这一轮音频超过上限，剧本适配器已收口")
            return True
        if ready:
            self._answer(snapshot)
        return True

    def send_frame(self, image: bytes, captured_at: int = 0) -> bool:
        if self._closed or not self.running:
            return False
        with self._lock:
            self._frames += 1
        return True

    def commit(self) -> bool:
        """手动收轮（非 VAD 路径）：不等尾静音，立刻把这一轮结掉。"""
        with self._lock:
            if not self._turn["open"]:
                return False
            snapshot = dict(self._turn)
            self._turn = self._blank_turn()
        self._answer(snapshot)
        return True

    def interrupt(self) -> bool:
        with self._lock:
            self._interrupts += 1
            open_turn = bool(self._turn["open"])
            self._turn = self._blank_turn()
        # 收口标记只在**真有一轮被丢掉**时才发：否则前端会凭空关掉一条不存在的回合。
        # 发了之后由桥的 `_end_of_turn` 换成线上结束标记（见 realtime_bridge.py:589）。
        if open_turn:
            self._emit(EVENT_DONE, response_id="scripted-%d" % self._interrupts,
                       reason="interrupted", silenced_open_turn=True)
        return True

    def submit_agent_result(self, request_id: str, text: str) -> bool:
        """没实现就明确说"不"：网关会据此回 `agent_result_failed`，页面提示改看报告。

        真要做的是把报告念成一段回答（需要与厂商侧的 `response.create` 同一套语义），
        剧本适配器不装这个。
        """
        return False

    # -- 回合输出 -------------------------------------------------------------
    def _answer(self, turn: dict[str, Any]) -> None:
        with self._lock:
            self._turns += 1
            number = self._turns
        peak = round(float(turn["peak"]), 3)
        # 用户侧转写：内容完全由收到的字节算出，页面能显示它就证明上行通了。
        heard = "%d 片 · %d 字节 · 峰值 %.3f" % (turn["chunks"], turn["bytes"], peak)
        self._emit(EVENT_TRANSCRIPT, text=heard, role="user", final=True, language="zh")
        reply = "收到 %d 字节，峰值 %.3f，第 %d 轮剧本回复。" % (turn["bytes"], peak, number)
        # 向上取整切分：向下取整会把尾巴上的字符丢掉，于是"分片拼起来"永远不等于权威文本
        # （前端按分片累加渲染，掉字就是界面少字——被 test_the_streamed_pieces_... 钉住）。
        size = max(1, -(-len(reply) // REPLY_PIECES))
        for index in range(0, len(reply), size):
            self._emit(EVENT_TEXT_DELTA, text=reply[index:index + size], role="assistant")
        # 生产惯例：final 那条带**完整**文本，桥按权威处理（realtime_bridge.py:539）。
        self._emit(EVENT_TEXT_DELTA, text=reply, role="assistant", final=True)
        audio = tone_bytes()
        self._emit(EVENT_AUDIO_DELTA, audio=audio, encoding=OUTPUT_AUDIO_FORMAT,
                   chars=len(audio), sample_rate=OUTPUT_SAMPLE_RATE)
        self._emit(EVENT_DONE, response_id="scripted-%d" % number, reason="completed",
                   bytes=int(turn["bytes"]), chunks=int(turn["chunks"]), peak_rms=peak)

    def _fail(self, code: str, message: str) -> None:
        with self._lock:
            self._errors += 1
        self._emit(EVENT_ERROR, code=code, message=message, retryable=False)


#: 与真实适配器同一注册形式（`realtime_omni.py:579`）：注册表用**无参**工厂构造，
#: 会话身份由桥在 `bind_session` 之后自己带。
register(PROVIDER_NAME, ScriptedRealtimeProvider)
