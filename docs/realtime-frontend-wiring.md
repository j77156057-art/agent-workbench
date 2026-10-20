# 实时对话（P0）：把后端已有的实时流接上前端

> 2026-10-20。本文记录「实时视频/语音对话流」这条线从"后端齐备、前端为零"到
> **真机跑通**的过程：做了什么、协议怎么走、证据是什么、还差什么。

---

## 1. 起点：后端是产品级的，前端却一行都没有

后端从 2026-09 起就有完整的实时流能力，而且**没有功能开关，路由恒在**：

| 能力 | 位置 |
| --- | --- |
| `WS /api/vision/live-stream`（项目级实时网关） | `api.py:3206` |
| 版本化线协议（`v:1`，控制帧 + 二进制媒体包） | `agent_runtime/realtime_protocol.py` |
| 会话桥（模式上报 / 时间线 / 指标 / provider 生命周期） | `agent_runtime/realtime_bridge.py` |
| 原生实时 provider（DashScope Omni，音视频双向） | `agent_runtime/realtime_omni.py` |
| 有界串行媒体队列（control > audio > video） | `agent_runtime/realtime_media.py` |
| 语音三件套（STT / TTS / 对话） | `api.py:3972-4003` |
| 桌面取像（Windows GDI） | `api.py:3609` |

而前端这边：

- `frontend/src/workbench/` 下**已经写好了 7 个模块**（`realtimeProtocol.ts`、
  `liveStreamControl.ts`、`liveAudioControl.ts`、`liveFrameEncoding.ts`、
  `liveVisionSampling.ts`、`liveVisionFocus.ts`、`liveVisualWindow.ts`）
  + `RealtimeModeBadge.vue` + `useRealtimeMode.ts` + `realtimeModeDisplay.ts` +
  `realtimeModeTracker.ts`；
- **全部是孤儿**：生产引用 0 处。`RealtimeModeBadge.vue` 自己的注释写着
  「现阶段的接线范围只到 helper + 组件 + 单测。真正的挂载点等 WS 客户端接好」；
- `frontend/src` 全量搜不到 `new WebSocket` / `EventSource` / video 轨道；
  构建产物 `web/assets` 里连 `live-stream` 字符串都搜不到。

**所以缺的不是"再写一遍协议"，而是三样：transport、接线层、UI 挂载点。**

## 2. 这次补上的三样

### 2.1 `frontend/src/workbench/liveTransport.ts`（新）

连接生命周期：握手（hello）、心跳、事件分发、断线重连（1→2→4→8→10s 退避）、
主动关闭。

核心是**会话代数**（generation）：编码一帧 JPEG 是异步的，重连/关闭会让"旧会话的产物"
晚到一步。接线层每捕获一帧记下当时的代数，发包前再问 `isCurrent(gen)`，晚到的旧帧就地
丢弃 —— 否则会把上一条连接的画��推进新会话。

### 2.2 `frontend/src/workbench/composables/useLiveSession.ts`（新）

接线层：采集 → 编码 → 上行 → 事件归约 → 播放。

- 视频：`getUserMedia` → `<video>`（面板里那个，所见即所发）→ canvas → JPEG →
  `encodeVideoFrame` → `sendLiveVideoPacket`（背压自适应：帧率/分辨率/质量三档随压力升降）
- 音频：`getUserMedia` → AudioContext → 重采样 16k → 能量 VAD → 定长分片 → `encodeAudioPacket`
- 播放：`model.audio`（base64 PCM16 @24k）→ AudioBuffer → 无缝调度（`createAudioPlaybackClock`）
- 抢话：VAD 检测到"用户在模型说话时开口" → 发 `cancel`
- 相位：直接用既有的 `classifyLivePhase`（未开始/连接中/正在看/正在回答/已暂停/已断线）

### 2.3 `frontend/src/workbench/components/LiveStreamPanel.vue`（新）

面板：视频预览 + 对话字幕 + 画面观察 + 模式徽章 + 控制条（开始/开麦/打断/停止）。
入口挂在顶栏「更多 → 开发工具与运行 → 实时对话」。

顶栏徽章用既有的 `RealtimeModeBadge.vue`（**降级不隐形**：原生/降级/不可用三态都带底色，
降级原因直接显示在面板里）。

**推帧策略（原生模式与降级模式必须分开）**

供应商要求"一轮里音频在前、图像在后"。据此有两条互补规则：

| 模式 | 规则 | 理由 |
| --- | --- | --- |
| 原生（native） | **只在用户说话的窗口内推帧**；静音期只补静音块 | 图像必须落在"有音频的那一轮"里。否则模型收到一个"只有图像的空轮次"，答出来的是 "I hear you." 这类无信息回复（实测：改之前 35 秒里反复出现） |
| 降级（抽帧） | **持续推帧** | 降级模式下画面是模型"看见屏幕"的唯一通路（后端旁路观察），限流等于什么都看不见 |

两条规则都用"静音块"兜底顺序：开麦时若最近 1.2s 没有音频上行，先补一块 100ms 静音再推帧，
让每一轮都满足"先音频、后图像"。否则供应商会回
`Error append image before append audio.`（实测：静音期推帧必踩）。

## 3. 顺带修掉的三处死代码

| 位置 | 问题 | 修法 |
| --- | --- | --- |
| `ChatDock.vue:59/61/116/159/1209` | 语音能力被 `scope==='cockpit'` 门控，而 cockpit scope 随 `AutonomousCockpit.vue` 一起删了 → **语音按钮永不渲染**；且 STT 状态探测也只在 cockpit 下跑，永远探测不到 | 门控改为按能力本身判断（`hasWebSpeech \|\| sttConfigured`），5 处一起改 |
| `vite.config.ts` | dev 代理没开 `ws: true` → dev 下 WS 握手必然失败；且 dev 页面在 5173、代理目标在 8012，后端的**同源校验**会判成跨源拒掉 | 代理加 `ws: true` + 改写 `Origin` |
| `liveStreamControl.appendCaptionTurn` | 增量片段逐条 `trim()` → 拼出 "Iamlistening."（丢空格）；供应商的 final 是"这一句"却会**整条替换**整轮回复（一句 "I" 吃掉一整段） | 片段保留前导空格；final 取"更长的一份" |

另外补了 `appendUserTranscriptDraft`：服务端的用户转写增量是**累计文本**
（"And" → "And so" → "And so, my"…），必须整条替换草稿，否则字幕碎成一屏
「你And」「你And so」「你And so my」。

## 4. 实机验证（真连真模型）

验证装置：Edge + `--use-file-for-fake-video-capture`（Y4M 彩条）+ `--use-file-for-fake-audio-capture`
（11 秒 JFK 演讲 WAV），对**真实后端**发真实帧与真实音频。

```
phase      正在看
badge      原生实时（tone=native）
已发帧     100（640×480 真实帧）
音频片     294
WS 事件    hello.ok 1 · audio.transcript 99 · model.delta 42 · cancel.ok 4 · heartbeat 2
页面异常   无
```

对话内容（面板字幕原文）：

```
你   And so, my fellow Americans.
模型 That's a famous line from John F. Kennedy's inaugural address in 1961.
你   What your country can do for you, ask what you can do for your country.
模型 It's a call to civic duty.
```

**模型真的听懂了音频并结合上下文作答**（不是回放固定文案）。

另外验了语音入口：对话台的工具条上现在能看到「◉语音」（之前因 cockpit 门控而永不渲染）：

```
▧图片 / ▹视频 / ◉语音 / DeepSeek · deepseek-flash / 联网搜索 / 深度思考
```

## 5. 途中排掉的三个"看起来是 bug 的东西"

| 现象 | 真因 |
| --- | --- |
| provider 报 `stream_broken`（"模型已下线"） | 是 **2×2 退化图像**喂给了云端模型 —— headless Chromium 的假摄像头不投递帧（`videoWidth` 停在 2）。用 Y4M 文件喂假摄像头即可拿到 640×480 真帧 |
| 旁路"画面观察"始终 0 条 | 旁路走**项目的 LLM**，而该项目配的 `deepseek-flash` 调视觉时返回 **HTTP 402 Insufficient Balance**（账户余额）。原生模式下旁路失败按设计被静默丢弃（"旁路探针失败绝不能打扰主链路"），所以是 0 条而不是报错。**充值后即恢复**（实测观察 6 条，且能跨帧对比"色块由左移到右"）。**注意：deepseek-flash 本身是有原生视觉的**（`config.py:346-348` 在 `_NATIVE_VISION_HINTS` 里），我一度用"DeepSeek 无视觉"的通识误判过这一点 |
| 供应商报 `Error append image before append audio.` | 该会话要求**一轮里音频在前、图像在后**；静音期只推帧就会踩到。客户端改为：开麦时若最近 1.2s 没有音频上行，先补一块静音再推帧 |

## 6. 续做（同日晚些时候）：圈选聚焦 + 通道指标 + 一个实测结论

初版面板能看、能听、能说话，但**没有两样东西**：

| 补的 | 后端 | 做法 |
| --- | --- | --- |
| **圈选聚焦** | 包头 `focused`（`api.py` 的 `focused_region="1" if header.get("focused")`） | 预览上拖拽画框 → 上行帧**就是**这块区域（所见即所发）+ 包头带 `focused=true`。鼠标坐标要按 `object-fit: contain` 的信箱留白换算，否则框的位置会偏 |
| **通道指标** | `GET /api/vision/realtime/status`（R5 时间线 + R10 指标） | 2 秒轮询，显示首字/首音/观察延迟（p50/p95）、队列深度、发帧/丢帧、模型拒绝、连接数。**这个端点此前前端零调用** —— R5/R10 的一等产物一直躺在后端 |

### 6.1 实测发现：音频是这条链路的**必需输入**，不是可选项

做圈选时顺带撞上一个怪现象：有时会话开几秒就 `stream_broken` 断流，有时稳跑 30 秒。
做了两轮对照实验（每轮 20 秒，Edge + Y4M 假摄像头 + JFK 语音）：

| 配置 | 结果 |
| --- | --- |
| 只发视频（不开麦） | **稳定断流**（stream_broken），转写 0~11 条 |
| 视频 + 音频（开麦） | **稳定正常**，转写 85~98 条，零错误 |

而且**音频一停**（先开麦跑几秒再关掉），会话过一会儿照样断 —— 供应商要求持续的音频输入，
纯图像会话会被服务端关掉。

后端原来那句提示恰好指向反了（"若音频一进流就被断开…"），实际是**音频没进流**才断。
于是两处都改：

- 后端 `realtime_omni.py` 的 stream_broken 文案改成**两个方向都讲**（含"实测：只发视频必断，
  带音频即稳"）。
- 前端「开始」改成**同时开麦**（麦克风从"可选项"变成这条链路的必需输入），关麦时明说代价；
  麦克风没开成时给可执行的提示 + 重试按钮。

### 6.2 圈选的实机证据

上行的是**二进制媒体包，包头是 JSON**，所以可以在探针里直接解出来看 `focused` 标记：

```
圈选前：上行视频帧 12，其中 focused=true 的 0
圈选后：上行视频帧 33，其中 focused=true 的 20
选区样式：left 24.9% top 24.9% width 45.1% height 50.0%（与拖拽的 25%→70% 一致）
```

## 7. 已知限制

1. **旁路"画面观察"依赖项目的视觉模型**：原生模式下画面由实时模型直接理解（结果体现在
   面板上方的回复里），那一栏只在降级（抽帧）模式下有意义。
2. **语音按钮的浏览器支持**：浏览器 Web Speech（`SpeechRecognition`）在 Edge/Chrome 可用，
   Firefox 没有；后端 STT 需配 `DOCMIND_STT_*`。
3. **抢话**：用户在模型说话时开口会打断本轮。连续说话会连续打断（实测 35s 内 3 次）——
   这是设计行为，不是缺陷。
4. **`model.audio` 播放**依赖 `AudioContext`，需要用户手势解锁（面板的「开始」即手势）。
5. **环形缓冲的转写去重**沿用既有 `closeUserTranscript` 语义：同文重放不重复记账。

## 7. 相关文件

| 文件 | 角色 |
| --- | --- |
| `frontend/src/workbench/liveTransport.ts` | **新**：WS 连接生命周期 + 会话代数 |
| `frontend/src/workbench/composables/useLiveSession.ts` | **新**：接线层（采集/编码/上行/归约/播放）+ 圈选聚焦 + 通道指标轮询 |
| `frontend/src/workbench/components/LiveStreamPanel.vue` | **新**：面板 UI（预览/圈选/字幕/观察/指标/控制） |
| `frontend/src/workbench/api/model.ts` | 改：新增 `realtimeChannelStatus()` 客户端 |
| `agent_runtime/realtime_omni.py` | 改：stream_broken 文案改成两个方向都讲（音频缺失也会断流） |
| `frontend/src/workbench/liveStreamControl.ts` | 改：字幕归约（草稿替换 / final 取长 / 保留空格） |
| `frontend/src/workbench/components/ChatDock.vue` | 改：解开语音的 cockpit 门控（5 处） |
| `frontend/vite.config.ts` | 改：dev 代理支持 WS + 同源 |
| `frontend/tests/liveStreamControl.test.mjs` | **新**：字幕归约的行为守卫（6 项） |
