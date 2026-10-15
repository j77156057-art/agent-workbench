"""R4/R5/R10 接线层：把 provider 解析、多模态时间线与性能指标接到实时网关上。

网关处理器本体是 `api.py` 的 `live_vision_stream`（R0/R3 所有者的文件，且工作区里
有多个并发写入者）。所以本模块的设计目标之一就是**把逻辑挪出 api.py**：
api.py 只留少量调用点（收帧/出观察/取消/关闭/握手），其余全部在这里，可独立测试。

## 接线后的行为契约

**1. 模式必须响亮上报（R4）**

`hello.ok` 追加四个字段（**不新增事件类型**，协议文件不动）：

| 字段 | 取值 | 含义 |
|---|---|---|
| `mode` | `"native-realtime"` / `"sampled-frames"` | 本次会话实际使用的通道 |
| `degraded_to` | `"sampled-frames"` / `null` | 降级目标；原生可用时为 null |
| `reason` | string | 降级原因（原生可用时为空串） |
| `provider_capabilities` | `string[]` | R4 能力标志（`audio.in`/`video.in`/`text.out`/`audio.out`/`interrupt`） |

这四字段对前端安全：`frontend/src/workbench/realtimeProtocol.ts` 的
`parseRealtimeServerEvent` 宽松透传（只校验 `v`/`type`/`sent_at`），`RealtimeServerEvent`
带 `[key: string]: unknown` 索引签名，`AutonomousCockpit.vue` 在 `hello.ok` 处整体提前
return。**唯一硬约束**：`v` 必须仍为 `1`、`sent_at` 必须仍为 number，否则整个事件被判非法。

*注意*：前端目前**不读** `hello.ok` 的任何字段，所以「界面上显示当前模式」需要另改
`AutonomousCockpit.vue`——那属于 R13（模式说明 UI），不在本模块职责内。本模块只负责
把事实送到线上。

**2. 原生通道（R4）**

`realtime_provider.resolve()` 给出 provider 时：帧走 `send_frame`、音频走 `send_audio`、
`cancel` 触发 `interrupt()`，provider 的 `RealtimeEvent` 由网关泵成线上事件。
**起不来就降级**：`start()` 失败不抛错、不伪装会话，直接回落抽帧并把原因写进 `reason`。

**2.1 网关自己拥有的事件不得转发（真机上已复现的坑）**

`WIRE_BY_KIND` 把适配器的**内部**事件也映射成了 R0 的服务端事件名：

* `EVENT_STATUS`（适配器收到 `session.created` 后发的"我好了"）→ `hello.ok`
* `EVENT_DONE`（模型一轮回答结束）→ `session.closed`

直接转发这两个是错的，而且第一个的危害已经在真机上复现：

* 网关在 hello 分支已经发过一次 `hello.ok`。适配器的 STATUS 再发一次，
  客户端就会收到**第二个 hello.ok，且 session_id 是适配器自己的**（实测：
  网关 `probe-s` vs 适配器 `rt-1a0ecc83e58`）。
* 前端 `AutonomousCockpit.vue` 收到 `hello.ok` 会**立刻改写模式显示**：它读
  `event.mode`，而适配器那条事件根本没有 `mode` 字段 → 落到默认值
  `'sampled-frames'` → **原生会话被显示成"兼容抽帧"**。
* `EVENT_DONE` 是"这一轮回答说完了"，不是"会话结束"。转发成 `session.closed`
  就是在会话仍然活着的时候告诉客户端会话已关闭。

所以 :data:`_GATEWAY_OWNED_WIRE_TYPES` 里这两类在 :meth:`SessionBridge.next_events`
被丢掉，**按线上事件名**记账（`suppressed`），不上线。

**2.2 `EVENT_DONE` 不能只是丢掉：要补一条结束标记**

`EVENT_STATUS` 丢掉就够了，`EVENT_DONE` 不行。一轮回答结束的信号通常由
`model.delta` 的 `final: true` 承载（`realtime_omni.py` 的
`response.audio_transcript.done` 分支），**但那条只在有转写时才发**：

* 用户抢话打断（`response.done` 的 reason 是 cancelled / 被 interrupt），
* 本轮只有音频、没有逐字转写，
* 本轮输出为空。

这几种情况下若把 `EVENT_DONE` 一并丢掉，前端那条 `done: false` 的助手回合就
永远不收口，界面停在"回答中"。所以 `EVENT_DONE` 被拦下时改发**一条结束标记**：
`model.delta` + `final: true` + 空文本（:meth:`SessionBridge._end_of_turn`）。

这一层仍复用 `model.delta` 的结束标记，不把 provider 的 `done` 映射成会话关闭；
R15 的发现提醒使用独立的 `model.observation` 事件，避免触发抽帧观察的失败关闭分支。
`frontend/src/workbench/liveStreamControl.ts` 的 `appendCaptionTurn` 对
`final + 空文本` 的处理恰好是"当前开着助手回合就地收口，没开着就什么都不做"，
天然幂等——重复的结束标记不会造出空气泡。
一轮已经由 `final: true` 收过口时不再补发（避免同一轮出现两个结束标记）。

**3. 音频 delta 必须 base64 上线（易踩的坑）**

`RealtimeEvent(EVENT_AUDIO_DELTA)` 的载荷里 `"audio"` 是**裸 `bytes`**
（见 `realtime_omni.py` 的 `response.audio.delta` 分支）。直接
`await websocket.send_json(event.to_wire())` 会抛
`TypeError: Object of type bytes is not JSON serializable`；而前端
`parseRealtimeServerEvent` 只接受**字符串**（`typeof raw !== 'string'` 即返回 null），
所以也不能改发二进制媒体包。**唯一可行解是 base64 编码后放进 JSON**，由
:func:`wire_events` 统一完成（`bytes` → base64 字符串，并补 `encoding` 标记）。

**4. 时间线（R5）**

每个项目一条 `RealtimeTimeline`，由网关喂入 `frame` 与 `observation` 两类条目，都带
**那一帧自己的** `captured_at`。时间线按项目共享、按活跃会话计数，最后一个会话断开时
释放，避免长期运行无界增长。

**5. 指标（R10）**

进程级 `MetricsRegistry`，网关写入：

* `frames_sent`（计数器，客户端被接受的帧）
* `frames_dropped`（计数器，被单槽背压覆盖掉的帧）
* `observation_latency_ms`（直方图，**网关收到该帧 → 该帧的观察就绪**，进程内单调时钟）
* `queue_depth`（仪表，出观察那一刻的待处理帧数，0/1）
* `connections`（计数器）、`model_rejections`（计数器）
* `first_token_ms` / `first_audio_ms`（直方图，**仅原生通道**：建连 → 首个文字/音频 delta）

语义要说清：`observation_latency_ms` 是**网关内部**延迟（收帧到出观察），不含客户端到
服务端的网络时间，也不含客户端渲染；所以它不会等于用户的端到端体感延迟。真实端到端
与丢帧率在 `realtime_bench.py` 里量，真机联验属 R12。
"""
from __future__ import annotations

import base64
import difflib
import json
import threading
import time
from typing import Any, Callable

from agent_runtime import realtime_metrics as metrics
from agent_runtime import realtime_protocol
from agent_runtime import realtime_provider as provider_registry
from agent_runtime.realtime_provider import (
    DEGRADED_SAMPLED_FRAMES,
    EVENT_AUDIO_DELTA,
    EVENT_DONE,
    EVENT_STATUS,
    EVENT_TEXT_DELTA,
    RealtimeEvent,
    RealtimeProvider,
)
from agent_runtime.realtime_timeline import (
    KIND_FRAME,
    KIND_OBSERVATION,
    KIND_TEXT,
    KIND_TRANSCRIPT,
    RealtimeTimeline,
)

MODE_NATIVE = "native-realtime"
MODE_SAMPLED = "sampled-frames"
# 触发判据是「句首口语」，不是【括号标记】。为什么：实时模型在**同一通道**里既生成音频
# 又生成转写，标记里的话会被**念出来**（括号本身通常不发音，但「疑似异常」这几个字会），
# 用户会听到一句机器人腔。换成自然句子后，被念出来也不突兀，而且模型更容易把自然句子
# 放在句首（召回更稳，不必去记一个格式约定）。
DISCOVERY_OPENINGS = (
    "我看到画面这里有问题：",
    "我看到画面上有个问题：",
    "画面上有个问题：",
)
# 旧标记只作**兼容别名**接受：措辞刚换时，仍在跑的会话里模型可能还按老格式说，
# 不能出现「模型报了、网关不认」的静默漏报。
DISCOVERY_LEGACY_OPENING = "【疑似异常】"
_DISCOVERY_ALL_OPENINGS = (*DISCOVERY_OPENINGS, DISCOVERY_LEGACY_OPENING)
_MAX_OPENING_CHARS = max(len(item) for item in _DISCOVERY_ALL_OPENINGS)


def _discovery_remainder(candidate: str) -> str | None:
    """以任一触发句开头 → 返回去掉触发句后的正文；否则 None。"""
    for opening in _DISCOVERY_ALL_OPENINGS:
        if candidate.startswith(opening):
            return candidate[len(opening):]
    return None


def _could_be_discovery_opening(candidate: str) -> bool:
    """candidate 仍是某个触发句的前缀（分块时只拿到半句）→ 还判断不了。"""
    return any(opening.startswith(candidate) for opening in _DISCOVERY_ALL_OPENINGS)


# ---- M2·②：严格 `flag_anomaly` JSON 事件（替代句首前缀匹配） --------------------
# 为什么要改成 tool-call 式结构：句首前缀是"换说法就漏报"的脆弱契约（开工文档 §4 M2 明确
# 列为验收不可接受）。模型必须吐一段扁平 JSON：`{"anomaly": {"target": "...", "evidence":
# "...", "confidence": 0.0~1.0}}`，网关用严格 schema 校验；不合规的一律**不**当发现，
# 也就不会出现"模型换了措辞 → 明明报了却静默漏掉"或反过来"随便一句话被误判异常"。
# 旧的句首前缀路径保留为 **兼容别名**（`trigger="phrase-legacy"`），只作旧会话切换窗口用。
ANOMALY_SCHEMA_VERSION = "flag_anomaly.v1"
ANOMALY_MIN_CONFIDENCE = 0.8          # 对齐 visual_targeting.parse_visual_target 的门槛
ANOMALY_TARGET_MAX_CHARS = 80
ANOMALY_EVIDENCE_MAX_CHARS = 200


def _parse_anomaly_marker(text: str) -> dict[str, Any] | None:
    """在流式缓冲里找一个合规的 `{"anomaly": {...}}` JSON 块并返回结构化字段。

    用 `json.JSONDecoder.raw_decode` 从每个 '{' 位置起试解析——能吃到嵌套花括号，
    不像 `\\{[^{}]*\\}` 那种正则在嵌套结构上会截半。多个候选时取**最后一个
    通过校验的**（模型自我修正时后说的更可信）。confidence<门槛/target 空/evidence 空
    /字段超长一律拒；这就是 fail-closed——不合规就当没报，绝不用自由文本兜底。
    """
    if not text or "anomaly" not in text:
        return None
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        pos = max(end, start + 1)
        if not isinstance(obj, dict):
            continue
        inner = obj.get("anomaly")
        if not isinstance(inner, dict):
            continue
        target = str(inner.get("target") or "").strip()
        evidence = str(inner.get("evidence") or "").strip()
        raw_conf = inner.get("confidence")
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= conf <= 1.0):
            continue
        if conf < ANOMALY_MIN_CONFIDENCE:
            continue
        if not target or len(target) > ANOMALY_TARGET_MAX_CHARS:
            continue
        if not evidence or len(evidence) > ANOMALY_EVIDENCE_MAX_CHARS:
            continue
        stripped = (text[:start] + text[end:]).strip()
        found = {
            "target": target, "evidence": evidence, "confidence": round(conf, 3),
            "text": stripped,
        }
    return found


MAX_DISCOVERY_CHARS = 800
DISCOVERY_DEDUPE_WINDOW_SECONDS = 45.0
DISCOVERY_THROTTLE_SECONDS = 2.0

# 事件类型 → 时间线条目类型（落进时间线的那部分证据）。
_TIMELINE_KIND_BY_EVENT = {
    EVENT_TEXT_DELTA: KIND_TEXT,
    EVENT_AUDIO_DELTA: KIND_TEXT,   # 音频 delta 归入回答侧证据，不单独建类
}

# 网关自己拥有的事件（见模块文档 2.1）：适配器的内部事件被 WIRE_BY_KIND 映射成了
# 这些 R0 事件名，但握手与"会话结束"的语义只属于网关，转发即撒谎。
_GATEWAY_OWNED_WIRE_TYPES = frozenset({"hello.ok", "session.closed"})

METRICS = metrics.MetricsRegistry()

_timelines: dict[str, RealtimeTimeline] = {}
_timeline_users: dict[str, int] = {}
_state_lock = threading.Lock()


# ---- 项目时间线（R5） -------------------------------------------------------

def acquire_timeline(project_id: str) -> RealtimeTimeline:
    """取（或建）某项目的时间线，并把该项目的活跃会话计数 +1。"""
    key = project_id or ""
    with _state_lock:
        timeline = _timelines.get(key)
        if timeline is None:
            timeline = RealtimeTimeline(key)
            _timelines[key] = timeline
        _timeline_users[key] = _timeline_users.get(key, 0) + 1
        return timeline


def release_timeline(project_id: str) -> None:
    """活跃会话计数 -1；归零时丢弃该项目的时间线，避免无界增长。"""
    key = project_id or ""
    with _state_lock:
        remaining = _timeline_users.get(key, 0) - 1
        if remaining > 0:
            _timeline_users[key] = remaining
            return
        _timeline_users.pop(key, None)
        _timelines.pop(key, None)


def timeline_snapshot(project_id: str, limit: int = 50) -> dict[str, Any]:
    key = project_id or ""
    with _state_lock:
        timeline = _timelines.get(key)
        users = _timeline_users.get(key, 0)
    if timeline is None:
        return {"project_id": key, "count": 0, "dropped_other_project": 0,
                "entries": [], "active_sessions": 0}
    snapshot = timeline.snapshot(limit=limit)
    snapshot["active_sessions"] = users
    return snapshot


def metrics_snapshot() -> dict[str, Any]:
    return METRICS.snapshot()


def active_timeline_projects() -> list[str]:
    with _state_lock:
        return sorted(_timelines)


def reset_state() -> None:
    """丢弃所有时间线并清空指标（测试用；不影响 provider 注册表）。"""
    global METRICS
    with _state_lock:
        _timelines.clear()
        _timeline_users.clear()
    METRICS.reset()


# ---- 模式解析（R4） ---------------------------------------------------------

def resolve_mode(provider_name: str = "") -> dict[str, Any]:
    """`realtime_provider.resolve()` 的薄包装，把结果归一成可直接上报的形状。"""
    result = provider_registry.resolve(provider_name)
    if result.get("ok"):
        return {"ok": True, "provider": result.get("provider"), "name": result.get("name") or "",
                "degraded_to": None, "reason": "",
                "capabilities": list(result.get("capabilities") or [])}
    return {"ok": False, "provider": None, "name": result.get("name") or "",
            "degraded_to": result.get("degraded_to") or DEGRADED_SAMPLED_FRAMES,
            "reason": str(result.get("reason") or "实时模型不可用"),
            "capabilities": []}


def remove_bytes(value: Any) -> Any:
    """把不可 JSON 序列化的 bytes 转成 base64 字符串（递归）。

    见模块文档第 3 条：音频 delta 是裸 bytes，而前端只能解析 JSON 文本事件。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {key: remove_bytes(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [remove_bytes(item) for item in value]
    return value


def wire_events(events: list[Any], *, version: int = 1) -> list[dict[str, Any]]:
    """把 provider 事件渲染成可以直接 `send_json` 的线上事件。"""
    wire: list[dict[str, Any]] = []
    for event in events:
        rendered = event.to_wire(version=version)
        if "audio" in rendered and isinstance(rendered["audio"], (bytes, bytearray, memoryview)):
            rendered["audio"] = base64.b64encode(bytes(rendered["audio"])).decode("ascii")
            rendered.setdefault("encoding", "pcm16")
        wire.append(remove_bytes(rendered))
    return wire


# ---- 会话桥（R4 + R5 + R10） ------------------------------------------------

class SessionBridge:
    """一条实时连接的接线状态：provider + 时间线 + 指标。

    网关只调用这里的少量入口；所有簿记都在本类内，便于脱离 WebSocket 单测。
    """

    def __init__(self, project_id: str, *, provider_name: str = "",
                 clock: Callable[[], float] = time.perf_counter, auto_start: bool = False) -> None:
        self.project_id = project_id or ""
        self.session_id = ""
        self.timeline = acquire_timeline(self.project_id)
        self.metrics = METRICS
        self._clock = clock
        self._started_at = clock()
        self._first_token_at: float | None = None
        self._first_audio_at: float | None = None
        self._frame_received_at: float | None = None
        self._pending_frames = 0
        self._closed = False
        # 适配器自报"会话已建立"（收到 vendor 的 session.created）。
        # 注意与 `native` 的区别：`native` 只表示网关已经连上并按原生通道转发，
        # `native_ready` 才是服务端确认可用。
        self.native_ready = False
        # 适配器自报的会话号，仅用于诊断（与网关的 `session_id` 不是一回事）。
        self.provider_session_id = ""
        # 被 `_GATEWAY_OWNED_WIRE_TYPES` 拦下的事件计数（按线上事件名）。
        self.suppressed: dict[str, int] = {}
        # 当前这一轮回答是否已经发过 `final: true` 的结束标记（见模块文档 2.2）。
        # 跨 `next_events` 调用存活：结束标记与被拦下的 `EVENT_DONE` 可能落在两次
        # poll 里，用局部变量会把同一轮收口两次。
        self._turn_closed = False
        self._discovery_text = ""
        self._discovery_prefix_state: bool | None = None
        self._discovery_last_sent_at = 0.0
        self._recent_discoveries: list[tuple[float, str]] = []
        self._agent_reports: list[str] = []

        mode = resolve_mode(provider_name)
        self.mode = MODE_NATIVE if mode["ok"] else MODE_SAMPLED
        self.degraded_to = mode["degraded_to"]
        self.reason = mode["reason"]
        self.provider_name = mode["name"]
        self.capabilities = mode["capabilities"]
        self.provider: RealtimeProvider | None = mode["provider"]
        if self.provider is not None:
            self.provider.project_id = self.project_id
        if auto_start and self.provider is not None:
            self.start()

    def bind_session(self, session_id: str) -> None:
        """把网关侧定的 `session_id` 绑进本桥，并记一次连接。

        **必须走这个方法，不要直接赋值 `session_id`**：`CONNECTIONS` 与后面的
        `MODEL_REJECTIONS` 都要记在**会话作用域**里。若按项目 id 记，`drop_session()`
        只清会话作用域，这些条目就永远不会被回收——进程长期运行时无界增长。
        """
        self.session_id = str(session_id or "")
        METRICS.incr(metrics.CONNECTIONS, 1, self.session_id or None)

    # -- 模式上报 -------------------------------------------------------------
    @property
    def native(self) -> bool:
        return self.provider is not None and self.mode == MODE_NATIVE

    def hello_fields(self) -> dict[str, Any]:
        """要合并进 `hello.ok` 的模式字段（不新增事件类型）。"""
        return {
            "mode": self.mode,
            "degraded_to": self.degraded_to,
            "reason": self.reason,
            "provider": self.provider_name,
            "provider_capabilities": list(self.capabilities),
        }

    # -- provider 生命周期 ----------------------------------------------------
    def start(self) -> bool:
        """尝试起原生会话；失败即降级，绝不伪装。"""
        if self.provider is None:
            return False
        try:
            started = bool(self.provider.start())
        except Exception as exc:  # noqa: BLE001 - 适配器故障不得冒泡进网关循环
            started = False
            self.reason = f"实时模型启动失败：{type(exc).__name__}: {str(exc)[:200]}"
        if not started:
            if not self.reason:
                self.reason = "实时模型未能建立会话"
            self.mode = MODE_SAMPLED
            self.degraded_to = DEGRADED_SAMPLED_FRAMES
            METRICS.incr(metrics.MODEL_REJECTIONS, 1, self.session_id or None)
            self.capabilities = []
            self.provider = None
            return False
        self.mode = MODE_NATIVE
        self.degraded_to = None
        # **不要把 provider 的 session_id 赋给 self.session_id**（踩过）：
        # 适配器会自己造一个 `rt-...`，覆盖掉客户端在 hello 里给的会话号之后，
        # 指标就记进了以 `rt-...` 为键的作用域，而 `drop_session(客户端会话号)`
        # 永远清不掉它——进程长期运行无界增长（= HANDOFF 里记的那颗雷）。
        # 两者必须分开：`session_id` 是网关的（客户端可见、指标与时间线的作用域），
        # `provider_session_id` 只用于诊断。
        self.provider_session_id = str(self.provider.session_id or "")
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.provider is not None:
            try:
                self.provider.close()
            except Exception:  # noqa: BLE001 - 收尾路径
                pass
        release_timeline(self.project_id)
        if self.session_id:
            METRICS.drop_session(self.session_id)

    # -- 媒体入站（R10 记账 + R5 时间线 + R4 转发） ---------------------------
    def note_frame(self, header: dict[str, Any], *, queued: bool = True) -> None:
        """网关收到一帧：记指标、落时间线。

        ``queued=True``（抽帧路径）：帧进单槽等待处理，所以上一帧还没被处理就来了新帧，
        那一帧就是被背压丢掉的，在这里结算。
        ``queued=False``（原生路径）：帧已经即时转给 provider，没有排队，也就不存在合并。
        """
        session = self.session_id or None
        if queued:
            if self._pending_frames:
                self.metrics.incr(metrics.FRAMES_DROPPED, 1, session)
            self._pending_frames = 1
            self.metrics.gauge(metrics.QUEUE_DEPTH, 1.0, session)
        self.metrics.incr(metrics.FRAMES_SENT, 1, session)
        self._frame_received_at = self._clock()
        self.timeline.add(KIND_FRAME, header.get("captured_at") or 0, project_id=self.project_id,
                          session_id=self.session_id, sequence=header.get("sequence") or 0)

    def send_frame(self, payload: bytes, header: dict[str, Any]) -> bool:
        """原生通道：把帧体交给 provider。返回是否真的走了原生路径。"""
        if not self.native or self.provider is None:
            return False
        return bool(self.provider.send_frame(payload, header.get("captured_at") or 0))

    def take_audio(self, payload: bytes, captured_at: int = 0) -> bool:
        """原生通道接管音频分片；返回 False 表示该走 audio_not_ready 降级回复。"""
        if not self.native or self.provider is None:
            return False
        self.timeline.add(KIND_TRANSCRIPT, captured_at or 0, project_id=self.project_id,
                          session_id=self.session_id, bytes=len(payload))
        return bool(self.provider.send_audio(payload, captured_at))

    def interrupt(self) -> bool:
        """用户抢话/cancel：中断原生回答。非原生通道无操作。"""
        if not self.native or self.provider is None:
            return False
        return bool(self.provider.interrupt())

    def submit_agent_result(self, request_id: str, text: str) -> bool:
        if not self.native or self.provider is None or request_id in self._agent_reports:
            return False
        if not self.provider.submit_agent_result(request_id, text):
            return False
        self._agent_reports = (self._agent_reports + [request_id])[-64:]
        return True

    def recover(self) -> bool:
        """Reconnect a provider after a retryable stream break, when supported."""
        if not self.native or self.provider is None:
            return False
        recover = getattr(self.provider, "recover", None)
        return bool(recover()) if callable(recover) else False

    # -- 出站 ----------------------------------------------------------------
    def observation_elapsed_ms(self) -> float:
        """网关收帧 → 现在。用于 `observation_latency_ms`。"""
        if self._frame_received_at is None:
            return 0.0
        return (self._clock() - self._frame_received_at) * 1000.0

    def note_observation(self, header: dict[str, Any], observations: list[str],
                         *, elapsed_ms: float | None = None) -> None:
        session = self.session_id or None
        value = self.observation_elapsed_ms() if elapsed_ms is None else elapsed_ms
        self._pending_frames = 0
        self._frame_received_at = None
        self.metrics.gauge(metrics.QUEUE_DEPTH, 0.0, session)
        self.metrics.observe(metrics.OBSERVATION_LATENCY_MS, value, session)
        text = str(observations[-1])[:900] if observations else ""
        if text:
            self.timeline.add(KIND_OBSERVATION, header.get("captured_at") or 0,
                              project_id=self.project_id, session_id=self.session_id,
                              sequence=header.get("sequence") or 0, text=text)

    def note_model_failure(self) -> None:
        self.metrics.incr(metrics.MODEL_REJECTIONS, 1, self.session_id or None)

    def next_events(self, *, limit: int = 32, timeout: float = 0.0) -> list[dict[str, Any]]:
        """泵出 provider 事件（已转成可直接 send_json 的线上事件）并喂时间线。

        网关自己拥有的事件（`hello.ok` / `session.closed`）在这里被拦下，理由见模块
        文档 2.1；拦下的事件仍然记账，便于状态端点与测试复核。
        """
        if not self.native or self.provider is None:
            return []
        events: list[RealtimeEvent] = []
        first = self.provider.poll(timeout) if timeout else self.provider.poll()
        if first is not None:
            events.append(first)
        if len(events) < limit:
            events.extend(self.provider.drain(limit - len(events)))
        for event in events:
            self._note_provider_event(event)
        relayed: list[dict[str, Any]] = []
        # `wire_events` 是逐条一对一渲染，两张表按下标对齐，这样才能同时看到
        # "适配器事件种类"（决定 2.2 的补发）和"线上事件名"（决定 2.1 的拦下）。
        for event, rendered in zip(events, wire_events(events)):
            kind = str(rendered.get("type") or "")
            if kind in _GATEWAY_OWNED_WIRE_TYPES:
                self.suppressed[kind] = self.suppressed.get(kind, 0) + 1
                if event.kind == EVENT_DONE:
                    # Some providers close a response with ``done`` without
                    # repeating a final transcript delta. Flush a marked
                    # discovery accumulated from earlier deltas before reset.
                    observation = self._discovery_from_delta(RealtimeEvent(
                        EVENT_TEXT_DELTA, captured_at=event.captured_at,
                        session_id=self.session_id or "",
                        payload={"text": "", "role": "assistant", "final": True},
                    ))
                    if observation:
                        relayed.append(observation)
                    relayed.extend(self._end_of_turn())
                    self._reset_discovery_turn()
                continue
            # 会话身份以网关为准：客户端只知道 hello 里那个会话号，若事件里带的是适配器
            # 自造的 `rt-...`，按会话号做校验的客户端会把事件丢掉（R0 的
            # `server_event` 也声明"信封是权威，provider 载荷不得改写协议字段"）。
            self._normalize_session_id(rendered)
            if event.kind == EVENT_TEXT_DELTA:
                observation = self._discovery_from_delta(event)
                if observation:
                    relayed.append(observation)
            if kind == "model.delta" and rendered.get("final") is True:
                self._turn_closed = True
                self._reset_discovery_turn()
            relayed.append(rendered)
        return relayed

    def _discovery_from_delta(self, event: RealtimeEvent) -> dict[str, Any] | None:
        """Relay explicitly marked visual claims as unverified, bounded notices.

        M2·②：优先走严格 `flag_anomaly` JSON 结构（tool-call 契约），句首前缀降为兼容别名。
        provider 常见行为是先增量、final 时复读整段——所以如果 final delta 本身就带合法
        marker 或以旧句首开头，直接以它为准，避免"增量+复述"两份拼到一起把 marker 挤掉。
        没拿到 final 一律不发（半截 JSON 不猜、半截句首也不猜，fail-closed）。
        """
        if str(event.payload.get("role") or "assistant") != "assistant":
            return None
        delta = str(event.payload.get("text") or "")
        is_final = event.payload.get("final") is True
        authoritative_final = False
        if is_final and delta:
            if _discovery_remainder(delta.lstrip()) is not None:
                self._discovery_text = delta.strip()[:MAX_DISCOVERY_CHARS + _MAX_OPENING_CHARS]
                authoritative_final = True
            elif _parse_anomaly_marker(delta) is not None:
                self._discovery_text = delta.strip()[-(MAX_DISCOVERY_CHARS + _MAX_OPENING_CHARS):]
                authoritative_final = True
        if delta and not authoritative_final:
            self._discovery_text = (self._discovery_text + delta)[
                -MAX_DISCOVERY_CHARS - _MAX_OPENING_CHARS:]
        if not is_final:
            return None
        text_raw = self._discovery_text.strip()
        marker = _parse_anomaly_marker(text_raw)
        if marker is not None:
            nl_text = (marker["text"] or marker["evidence"])[:MAX_DISCOVERY_CHARS]
            return self._emit_discovery(
                event, text=nl_text, target=marker["target"],
                evidence=marker["evidence"], confidence=marker["confidence"],
                trigger="json-marker")
        remainder = _discovery_remainder(text_raw.lstrip())
        if remainder is None:
            return None
        legacy_text = remainder.strip()[:MAX_DISCOVERY_CHARS]
        if not legacy_text:
            return None
        return self._emit_discovery(event, text=legacy_text, trigger="phrase-legacy")

    def _emit_discovery(self, event: RealtimeEvent, *, text: str,
                        target: str | None = None, evidence: str | None = None,
                        confidence: float | None = None,
                        trigger: str = "phrase-legacy") -> dict[str, Any] | None:
        """JSON 与旧句首两条路径共用的收口：去重 + 节流 + verified:false 落账。

        去重归一化 key：JSON 路径按 `target|evidence`——换个说法再报同一个位置/证据，
        不该被当成新异常刷给用户；旧句首路径按整句（原本也没结构化字段可用）。
        """
        now = self._clock()
        key_src = ("%s|%s" % (target, evidence)) if target is not None else text
        normalized = " ".join(str(key_src).casefold().split())
        if not normalized:
            return None
        self._recent_discoveries = [(stamp, previous) for stamp, previous in self._recent_discoveries
                                    if now - stamp <= DISCOVERY_DEDUPE_WINDOW_SECONDS]
        if any(difflib.SequenceMatcher(None, normalized, previous).ratio() >= 0.82
               for _, previous in self._recent_discoveries):
            return None
        if now - self._discovery_last_sent_at < DISCOVERY_THROTTLE_SECONDS:
            return None
        self._discovery_last_sent_at = now
        self._recent_discoveries.append((now, normalized))
        self._recent_discoveries = self._recent_discoveries[-64:]
        kwargs: dict[str, Any] = {
            "captured_at": event.captured_at,
            "session_id": self.session_id or None,
            "source": "realtime-model",
            "verified": False,
            "text": text,
            "trigger": trigger,
        }
        if target is not None:
            kwargs["schema_version"] = ANOMALY_SCHEMA_VERSION
            kwargs["target"] = target
            kwargs["evidence"] = evidence
            kwargs["confidence"] = confidence
        return realtime_protocol.server_event("model.observation", **kwargs)

    def _reset_discovery_turn(self) -> None:
        self._discovery_text = ""
        self._discovery_prefix_state = None

    def _end_of_turn(self) -> list[dict[str, Any]]:
        """被拦下的 `EVENT_DONE` 的替身：一条"这一轮说完了"的线上标记。

        见模块文档 2.2：不补这一条，被抢话打断 / 无转写的轮次在前端就永远不收口。
        本轮已经由 `final: true` 收过口时不补发，避免同一轮出现两个结束标记。
        """
        already_closed = self._turn_closed
        self._turn_closed = False   # 这一轮到此为止，下一轮重新计
        if already_closed:
            return []
        return wire_events([RealtimeEvent(
            EVENT_TEXT_DELTA,
            session_id=self.session_id or "",
            payload={"text": "", "role": "assistant", "final": True},
        )])

    def _normalize_session_id(self, rendered: dict[str, Any]) -> None:
        """把转发事件里的会话号改成网关的；适配器自己的号只留作诊断。"""
        provider_id = str(rendered.get("session_id") or "")
        if provider_id and provider_id == self.provider_session_id:
            if self.session_id:
                rendered["session_id"] = self.session_id
            else:
                rendered.pop("session_id", None)
            rendered["provider_session_id"] = provider_id

    def _note_provider_event(self, event: Any) -> None:
        """首个文字/音频 delta 的延迟（`first_token_ms` / `first_audio_ms`）在这里落地。"""
        if event.kind == EVENT_STATUS:
            # 服务端确认会话已建立；这条不上线（见 2.1），但要留痕。
            self.native_ready = True
        if event.kind == EVENT_TEXT_DELTA and self._first_token_at is None:
            self._first_token_at = self._clock()
            self.metrics.observe(metrics.FIRST_TOKEN_MS,
                                 (self._first_token_at - self._started_at) * 1000.0,
                                 self.session_id or None)
        elif event.kind == EVENT_AUDIO_DELTA and self._first_audio_at is None:
            self._first_audio_at = self._clock()
            self.metrics.observe(metrics.FIRST_AUDIO_MS,
                                 (self._first_audio_at - self._started_at) * 1000.0,
                                 self.session_id or None)
        kind = _TIMELINE_KIND_BY_EVENT.get(event.kind)
        if kind:
            payload = {key: value for key, value in event.payload.items()
                       if isinstance(value, (str, int, float, bool, type(None)))}
            self.timeline.add(kind, event.captured_at, project_id=self.project_id,
                              session_id=self.session_id, sequence=event.sequence, **payload)


def status_snapshot(project_id: str = "", limit: int = 50) -> dict[str, Any]:
    """R5/R10 的只读状态视图：多模态时间线 + 性能指标。

    刻意**不报模式**。模式的唯一来源是 `/api/vision/realtime-status`（R13）和每次会话的
    `hello.ok.mode`；这里再报一份就会出现两个可能互相矛盾的 mode——本模块按"是否配置了
    provider"判断，R13 按 `resolve().ok` 判断，**配置了但不可用**时两者不一致，排查时
    会误事。
    """
    return {
        "timeline": timeline_snapshot(project_id, limit=limit) if project_id else None,
        "timeline_projects": active_timeline_projects(),
        "metrics": metrics_snapshot(),
    }
