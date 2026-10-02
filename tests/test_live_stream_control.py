"""AI-A/AI-B：前端媒体模块 liveStreamControl.ts 的行为契约测试（只读实现）。

沿用仓库既有约定（`test_realtime_protocol_contract.py` 直接读前端源码）：用项目自己的
typescript 编译器把纯逻辑模块编译成 ESM，再由 node 执行断言脚本。只验证三件事：

1. R1 自适应：变慢/缓冲升高 → 逐级降频并压缩分辨率/码率；持续健康 → 逐级恢复；
   服务端 throttled/retry_after 抬底；档位区间有界。
2. R6 状态归类：正在看/正在回答/连接中/已断线/已暂停/未开始 的判定与标签齐备。
3. 在飞帧账本：端到端延迟计算、较新帧回帧即取代更旧帧、积压年龄检测。

node 或前端依赖缺失时整个文件跳过（与浏览器联验测试同样的降级方式）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "frontend" / "src" / "workbench" / "liveStreamControl.ts"
TSC = ROOT / "frontend" / "node_modules" / "typescript" / "bin" / "tsc"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not (MODULE.exists() and TSC.exists() and NODE),
    reason="node / 前端 typescript 工具链不可用，跳过前端逻辑行为测试",
)

_HARNESS = r"""
import assert from 'node:assert';
import { createAdaptiveSender, classifyLivePhase, LIVE_PHASE_LABELS, createInFlightLedger,
  appendCaptionTurn, closeUserTranscript, describeLiveCapabilities,
  shouldDispatchVoiceTurn, sameVisualFocus,
  isDismissedVisualAlert, pruneDismissedVisualAlerts,
  canQueueLiveVideo, sendLiveVideoPacket, liveVideoOverrun } from './liveStreamControl.js';

const now = () => Date.now();

// ---- R1 自适应发送 -----------------------------------------------------------
const a = createAdaptiveSender();
const p0 = a.plan();
assert.strictEqual(p0.intervalMs, 500, '默认档位应与接入前固定行为一致（500ms）');
assert.strictEqual(p0.maxEdge, 1280);
assert.ok(Math.abs(p0.quality - 0.8) < 1e-9);

// 单次高延迟即降一级
const p1 = a.feed({ bufferedAmount: 0, latencyMs: 9000 });
assert.ok(p1.intervalMs > 500 && p1.maxEdge < 1280, '模型变慢必须降频并压缩分辨率: ' + JSON.stringify(p1));

// 缓冲堆积也降压
a.reset();
assert.strictEqual(a.feed({ bufferedAmount: 400_000 }).intervalMs > 500, true);

// 连续健康样本逐级恢复，未攒够不回升
a.reset();
a.feed({ bufferedAmount: 500_000 });            // 降一级
const midPlan = a.plan();
assert.strictEqual(a.feed({ bufferedAmount: 1000, latencyMs: 900 }).intervalMs, midPlan.intervalMs, '单个健康样本不应立即回升');
assert.strictEqual(a.feed({ bufferedAmount: 1000, latencyMs: 900 }).intervalMs, midPlan.intervalMs);
assert.strictEqual(a.feed({ bufferedAmount: 1000, latencyMs: 900 }).intervalMs, midPlan.intervalMs);
const back = a.feed({ bufferedAmount: 1000, latencyMs: 900 });
assert.strictEqual(back.intervalMs, 500, '连续 4 个健康样本后应恢复一档');

// 极端压力一路降到最底档，且永不越界
const floor = createAdaptiveSender();
let last = floor.plan();
for (let i = 0; i < 12; i++) {
  last = floor.feed({ bufferedAmount: 3_000_000, latencyMs: 60_000, encodeOverrun: true });
  assert.ok(last.intervalMs >= 300 && last.intervalMs <= 8000, '间隔必须落在有界档位表: ' + last.intervalMs);
  assert.ok(last.maxEdge >= 480 && last.quality >= 0.5, '分辨率/码率必须保底: ' + JSON.stringify(last));
}
assert.strictEqual(last.intervalMs, 4000, '持续压力应收敛到最保守档 4s');

// 服务端明确限流：retry_after 直接抬到间隔下限
const throttled = createAdaptiveSender();
const tp = throttled.feed({ bufferedAmount: 0, throttled: true, retryAfterS: 3 });
assert.ok(tp.intervalMs >= 3000, 'retry_after 3s 期间不得发新帧: ' + tp.intervalMs);
const capped = createAdaptiveSender();
assert.ok(capped.feed({ bufferedAmount: 0, throttled: true, retryAfterS: 9999 }).intervalMs <= 8000, '退避上限必须封顶 8s');

// encodeOverrun（采集快于发送）单独构成压力
const overrun = createAdaptiveSender();
assert.ok(overrun.feed({ bufferedAmount: 0, encodeOverrun: true }).intervalMs > 500);

// ---- R6 状态归类 -------------------------------------------------------------
const base = { active: true, socketState: 'open', paused: false, reconnectScheduled: false, lastObservationAgeS: null };
assert.strictEqual(classifyLivePhase({ ...base, active: false }), 'idle');
assert.strictEqual(classifyLivePhase({ ...base, paused: true }), 'paused');
assert.strictEqual(classifyLivePhase({ ...base, socketState: 'closed' }), 'offline');
assert.strictEqual(classifyLivePhase({ ...base, socketState: 'closed', reconnectScheduled: true }), 'connecting');
assert.strictEqual(classifyLivePhase({ ...base, lastObservationAgeS: 12 }), 'viewing');
assert.strictEqual(classifyLivePhase({ ...base, lastObservationAgeS: 3 }), 'answering');
for (const key of ['idle', 'connecting', 'viewing', 'answering', 'paused', 'offline']) {
  assert.ok(LIVE_PHASE_LABELS[key] && LIVE_PHASE_LABELS[key].length > 0, '每个相位都要有中文标签: ' + key);
}
// 断线优先于「正在回答」：旧观察不能粉饰当前连接
assert.strictEqual(classifyLivePhase({ ...base, socketState: 'closed', lastObservationAgeS: 1 }), 'offline');

// ---- 在飞帧账本 ---------------------------------------------------------------
const ledger = createInFlightLedger();
const t0 = now();
ledger.onSent(1, t0 - 1000, t0);
ledger.onSent(2, t0 - 500, t0 + 100);
ledger.onSent(3, t0, t0 + 200);
assert.strictEqual(ledger.pendingCount(), 3);
assert.ok(ledger.oldestPendingAge(t0 + 1200) >= 1200, '积压年龄应能从最老在飞帧读出');
const latency = ledger.onObserved(3, t0 + 800);
assert.ok(latency && latency.e2eMs >= 800, '端到端延迟 = 观察返回 - 采集时刻: ' + JSON.stringify(latency));
assert.strictEqual(ledger.pendingCount(), 0, '背压只处理最新帧：3 号回帧后 1/2 号视为被取代');
assert.strictEqual(ledger.onObserved(9, t0), null, '未登记序号不得编造延迟');
ledger.onSent(4, now(), now()); ledger.clear();
assert.strictEqual(ledger.pendingCount(), 0, 'clear 后不得残留（会话重连卫生）');

// 无逐帧回执的原生模式长时间运行，账本仍有界；过期元数据不再制造永久积压。
for (let i = 1; i <= 1000; i++) ledger.onSent(i, t0, t0 + i);
assert.ok(ledger.pendingCount() <= 64);
assert.ok(ledger.prune(t0 + 31_001) > 0);
assert.strictEqual(ledger.pendingCount(), 0);
assert.strictEqual(ledger.oldestPendingAge(t0 + 31_001), 0);

assert.strictEqual(liveVideoOverrun('native-realtime', 10, 60_000, 500), false,
  '原生不逐帧回观察，不能据此持续降画质');
assert.strictEqual(liveVideoOverrun('sampled-frames', 10, 7000, 500), true);
assert.strictEqual(liveVideoOverrun('native-realtime', 1100, 0, 500), true,
  '真正的编码耗时仍触发降压');

// 网速慢时连编码都不执行；编码期间暂停/重连则丢包，不允许旧帧进入新会话。
let encodes = 0, sends = 0, buffered = 1_000_000, current = true;
const input = { encode: async () => { encodes++; return new ArrayBuffer(100); },
  isCurrent: () => current, bufferedAmount: () => buffered, send: () => sends++ };
assert.strictEqual(await sendLiveVideoPacket(input), 'backpressure');
assert.strictEqual(encodes, 0);
buffered = 0;
assert.strictEqual(await sendLiveVideoPacket({ ...input,
  encode: async () => { current = false; return new ArrayBuffer(100); } }), 'stale');
assert.strictEqual(sends, 0);
current = true;
assert.strictEqual(await sendLiveVideoPacket({ ...input,
  encode: async () => { buffered = 999_950; return new ArrayBuffer(100); } }), 'backpressure');
assert.strictEqual(sends, 0, '必须把整包大小计入缓冲预算并在编码后复查');
buffered = 0;
assert.strictEqual(await sendLiveVideoPacket(input), 'sent');
assert.strictEqual(sends, 1);
assert.strictEqual(canQueueLiveVideo(0, 1_000_000), false);
assert.strictEqual(canQueueLiveVideo(120_000, 10_000, 128_000), false,
  '开麦时视频必须服从较小缓冲预算，给语音留空间');
assert.strictEqual(await sendLiveVideoPacket({ ...input, bufferLimit: 80,
  encode: async () => new ArrayBuffer(100) }), 'backpressure');
assert.strictEqual(sends, 1, '语音优先预算必须在真正发送整包之前执行');

const nativeSender = createAdaptiveSender();
nativeSender.feed({ bufferedAmount: 500_000 });
for (let i = 0; i < 4; i++) nativeSender.feed({ bufferedAmount: 0, transportHealthy: true });
assert.strictEqual(nativeSender.plan().intervalMs, 500,
  '原生通道在网络恢复后也必须能恢复画质，无需等待不存在的逐帧观察');
nativeSender.feed({ bufferedAmount: 500_000 });
nativeSender.feed({ bufferedAmount: 0, transportHealthy: true });
nativeSender.feed({ bufferedAmount: 100_000 });
for (let i = 0; i < 3; i++) nativeSender.feed({ bufferedAmount: 0, transportHealthy: true });
assert.strictEqual(nativeSender.plan().intervalMs, 900, '健康样本必须连续，不能跨过中等压力累计');

const negotiated = createAdaptiveSender();
negotiated.setMinimumInterval(1400);
for (let i = 0; i < 12; i++) negotiated.feed({ bufferedAmount: 0, latencyMs: 900 });
assert.ok(negotiated.plan().intervalMs >= 1400, '恢复后也不能超过服务端允许的分析频率');
negotiated.setMinimumInterval(12_000);
assert.strictEqual(negotiated.plan().intervalMs, 12_000, '用户配置的服务端间隔不应被普通退避上限截短');
negotiated.reset();
assert.strictEqual(negotiated.plan().intervalMs, 500, '重连到原生通道时不沿用抽帧限速');

// ---- 字幕归约（R13：model.delta / audio.transcript） -------------------------
let caps = appendCaptionTurn([], { type: 'audio.transcript', text: '这里的按钮不对' });
assert.strictEqual(caps.length, 1);
assert.strictEqual(caps[0].role, 'user');
assert.strictEqual(caps[0].done, true, '用户转写天然是完整句');

caps = appendCaptionTurn(caps, { type: 'model.delta', text: '我看' });
caps = appendCaptionTurn(caps, { type: 'model.delta', text: '一下' });
assert.strictEqual(caps.length, 2, '增量应并入同一助手回合');
assert.strictEqual(caps[1].text, '我看一下');
assert.strictEqual(caps[1].done, false);

// 适配器先发增量 .delta、再发一条全量 .done：final 必须整条替换，拼起来会重复一遍
caps = appendCaptionTurn(caps, { type: 'model.delta', text: '我看一下，按钮确实越界了', final: true });
assert.strictEqual(caps[1].text, '我看一下，按钮确实越界了', 'final 全量替换失败，回答会被拼成两遍');
assert.strictEqual(caps[1].done, true);

caps = appendCaptionTurn(caps, { type: 'model.delta', text: '下一轮' });
assert.strictEqual(caps.length, 3, 'final 之后的新 delta 必须开新回合');

let c2 = appendCaptionTurn([], { type: 'model.delta', text: '半句' });
c2 = appendCaptionTurn(c2, { type: 'model.delta', final: true });
assert.strictEqual(c2[0].text, '半句', 'final 无全量文本时用已累积文本收尾');
assert.strictEqual(c2[0].done, true);

assert.strictEqual(appendCaptionTurn(caps, { type: 'model.delta', text: '   ' }), caps, '空增量不记账');
assert.strictEqual(appendCaptionTurn(caps, { type: 'video.observation', text: 'x' }), caps, '无关事件不得进入字幕流');

let many = [];
for (let i = 0; i < 20; i++) many = appendCaptionTurn(many, { type: 'audio.transcript', text: 'u' + i });
assert.strictEqual(many.length, 8, '字幕窗口必须有界');
assert.strictEqual(many[many.length - 1].text, 'u19');

// ---- 用户语音收尾：记账与「该不该转交」共用同一条判定（A 档：全部转交）---------
// 返回值 null 是契约：代表这条 final 是**重放**。调用方据此同时不上字幕、不转交，
// 所以这里钉住 null 的出现条件，就等于钉住「语音指令不会被重复派给主 Agent」。
const u0 = [{ role: 'user', text: '把按钮往右挪一点', done: true }];

let cu = closeUserTranscript(u0, '把按钮往右挪一点', false);
assert.strictEqual(cu, null, '同上一条同文的 final 是重放：必须返回 null（不记账也不转交）');

cu = closeUserTranscript(u0, '把按钮往右挪一点。', false);
assert.strictEqual(cu.length, 2, '新内容要新起一条用户回合');
assert.strictEqual(cu[1].text, '把按钮往右挪一点。');
assert.strictEqual(cu[1].done, true);

cu = closeUserTranscript(u0, '把按钮往右挪一点', true);
assert.strictEqual(cu.length, 1, '有草稿时整条替换草稿，不得新增一条');
assert.strictEqual(cu[0].text, '把按钮往右挪一点');
assert.strictEqual(cu[0].done, true);

assert.strictEqual(closeUserTranscript(u0, '   ', false), null, '空 final 不记账也不转交');
assert.strictEqual(closeUserTranscript([], '第一句', false).length, 1, '首条语音要能开回合');

let bounded = [];
for (let i = 0; i < 12; i++) bounded = closeUserTranscript(bounded, 'u' + i, false);
assert.strictEqual(bounded.length, 8, '用户字幕窗口同样有界');

// ---- 语音轮次的噪音门控：误识别/旁音不许起 Agent 轮（全是硬条件，不猜意图）--------
const T = 1_000_000;
assert.strictEqual(shouldDispatchVoiceTurn('帮我把按钮往右挪一点', {}, T), true, '正常指令必须转交');

// 语气词与空串：去掉标点和语气词后什么都不剩
assert.strictEqual(shouldDispatchVoiceTurn('嗯', {}, T), false, '单字语气词不转交');
assert.strictEqual(shouldDispatchVoiceTurn('啊？', {}, T), false, '语气词加问号不转交');
assert.strictEqual(shouldDispatchVoiceTurn('。。。', {}, T), false, '纯标点不转交');
assert.strictEqual(shouldDispatchVoiceTurn('   ', {}, T), false, '空白不转交');
assert.strictEqual(shouldDispatchVoiceTurn('嗯嗯啊啊', {}, T), false, '纯语气词串不转交');

// 但语气词**开头的正常句子**必须照常转交（别把「嗯，帮我改一下」误杀）
assert.strictEqual(shouldDispatchVoiceTurn('嗯，帮我改一下这个按钮', {}, T), true, '带内容的句子要转交');
assert.strictEqual(shouldDispatchVoiceTurn('改', {}, T), true, '单字实义指令（改/停/继续）不能误杀');

// 同一句话重复（含标点差异）：误识别常吐两遍
assert.strictEqual(shouldDispatchVoiceTurn('你好。', { text: '你好', at: T - 5000 }, T), false,
  '同句（仅标点不同）不得重复转交');
assert.strictEqual(shouldDispatchVoiceTurn('你好', { text: '你好', at: T - 5000 }, T), false);
assert.strictEqual(shouldDispatchVoiceTurn('换个说法', { text: '你好', at: T - 5000 }, T), true,
  '换了内容就要转交');

// 连发保护：同一句被切成两段会连着来
assert.strictEqual(shouldDispatchVoiceTurn('先看左侧栏', { text: '上一句', at: T - 300 }, T), false,
  '距上次转交太近的连发只发一次');
assert.strictEqual(shouldDispatchVoiceTurn('先看左侧栏', { text: '上一句', at: T - 5000 }, T), true,
  '隔开足够久就照常转交');

// ---- 跨来源合并判据：抽帧告警 vs 实时模型自述是否在说同一处 -------------------
// 两条观测链各自弹横幅时，同一个问题会弹两次；这个判据只回答"是不是同一处"。
assert.strictEqual(sameVisualFocus('右上角有报错弹窗', '右上角出现报错弹窗，遮住了保存按钮'), true,
  '同一处、不同说法要判为同一处');
assert.strictEqual(sameVisualFocus('右上角有报错弹窗', '左下角按钮点不动'), false,
  '不同位置的问题不得合并');
assert.strictEqual(sameVisualFocus('报错', '报错弹窗'), false,
  '字数太少不判（避免两个字随便撞上就合并）');
assert.strictEqual(sameVisualFocus('', '右上角报错'), false, '空文本不判');

// ---- 忽略期内不再弹（后端相似度去重只有 45s 窗口，过了窗口同一处会再弹） ---------
const T2 = 5_000_000;
const ignored = [{ text: '右上角有报错弹窗', at: T2 - 1000 }];
assert.strictEqual(isDismissedVisualAlert('右上角有报错弹窗，遮住了保存按钮', ignored, T2), true,
  '同处换说法也要认（否则用户会觉得忽略按钮没生效）');
assert.strictEqual(isDismissedVisualAlert('左下角按钮点不动', ignored, T2), false, '不同处不得抑制');
assert.strictEqual(isDismissedVisualAlert('右上角有报错弹窗', ignored, T2 + 11 * 60_000), false,
  '超过 10 分钟窗口后不再抑制（那时同一位置又出问题仍应提醒）');

let pruned = [];
for (let i = 0; i < 25; i++) pruned = pruneDismissedVisualAlerts([...pruned, { text: 'x' + i, at: T2 }], T2);
assert.strictEqual(pruned.length, 20, '忽略记录必须有界');
assert.strictEqual(pruned[pruned.length - 1].text, 'x24');
assert.strictEqual(pruneDismissedVisualAlerts([{ text: 'old', at: T2 - 11 * 60_000 }], T2).length, 0,
  '超窗记录要被裁掉');

assert.match(describeLiveCapabilities(['audio.in', 'text.out']), /麦克风音频输入.*文字回复/);
assert.strictEqual(describeLiveCapabilities(undefined), '', '抽帧模式没有能力表，返回空串而不是报错');
assert.strictEqual(describeLiveCapabilities([]), '');
assert.match(describeLiveCapabilities(['weird.cap']), /weird\.cap/, '未知能力原样透出，不谎报');

console.log('OK ' + JSON.stringify({ checks: 44 }));
"""


@pytest.fixture(scope="module")
def compiled_module(tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("live_stream_control")
    compile = subprocess.run(
        [NODE, str(TSC), str(MODULE), "--target", "es2020", "--module", "esnext",
         "--moduleResolution", "node", "--strict", "--outDir", str(out_dir)],
        capture_output=True, text=True,
    )
    if compile.returncode != 0:
        pytest.fail(f"liveStreamControl.ts 编译失败：\n{compile.stdout}\n{compile.stderr}")
    return out_dir


def _write_package(dir_path: Path) -> None:
    (dir_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")


def test_adaptive_sender_and_phase_logic_behave_as_contracted(compiled_module: Path):
    _write_package(compiled_module)
    harness = compiled_module / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    run = subprocess.run([NODE, str(harness)], capture_output=True, text=True)
    assert run.returncode == 0, (
        f"前端自适应/相位/账本行为契约失败：\n{run.stdout}\n{run.stderr}")
    assert run.stdout.startswith("OK"), run.stdout


def test_module_exports_stay_pure_for_browserless_execution(compiled_module: Path):
    """模块必须保持无浏览器依赖的纯逻辑，否则本文件的执行式契约会退化成正则。"""
    source = MODULE.read_text(encoding="utf-8")
    for forbidden in ("import Vue", "from 'vue'", "WebSocket(", "document.", "window."):
        assert forbidden not in source, f"liveStreamControl.ts 混入了浏览器/Vue 依赖：{forbidden}"


def test_cockpit_wiring_uses_the_control_module():
    """接线回归：AutonomousCockpit 必须真的消费自适应计划与相位，而不是留着魔法数字。"""
    cockpit = (ROOT / "frontend" / "src" / "workbench" / "components" / "AutonomousCockpit.vue")
    source = cockpit.read_text(encoding="utf-8")
    assert "createAdaptiveSender" in source and "liveAdaptive.plan()" in source
    assert "classifyLivePhase" in source and "LIVE_PHASE_LABELS" in source
    assert "realtimeCancel" in source, "打断按钮必须通过 R0 的 cancel 控制包实现"
    assert "appendCaptionTurn" in source and "model.delta" in source and "audio.transcript" in source, \
        "cockpit 必须消费 R4 的回答流/转写事件，否则字幕区是死组件"
    assert "appendLiveCaption" in source and "liveTranscriptDraft" in source, \
        "连续多轮转写必须把 delta/final 归并为一条用户回合，不能逐条重复显示"
    assert "stream_broken" in source and "正在恢复并重放未完成语音" in source, \
        "可恢复的 stream_broken 必须给出明确的恢复中反馈"
    assert "closeOpenLiveCaption" in source and "stopAiPlayback()" in source, \
        "抢话或断线时必须收口未完成字幕并清空旧的播放队列"
    assert "describeLiveCapabilities" in source and "provider_capabilities" in source, \
        "hello.ok 送到的模型能力必须在 UI 呈现（R13 验收：界面说明当前模式和限制）"
    assert "liveAudioControl" in source and "createVoiceGate" in source and "encodeAudioPacket" in source, \
        "麦克风上行必须复用 liveAudioControl 的 VAD/分片/封包，不得在组件里另写一套数学"
    assert "model.audio" in source and "playLiveModelAudio" in source, \
        "原生语音回复必须接播放管线"
    assert "resolveLiveModelAudioFormat" in source and "格式无法识别" in source, \
        "model.audio 必须按 wire 格式解码，未知编码不得静默播放"
    assert "sendNativeAudioReady" in source and "liveStreamReadyForVideo" in source, \
        "native provider 要求先有音频：视频发送必须等待音频就绪闸门"
    assert "sendVoiceTurnToAgent" in source and "shouldDispatchVoiceTurn" in source, \
        "语音转写必须转交主 Agent，且先过噪音门控：实时模型没有工具通道，改文件只能由主 Agent 执行"
    assert "cockpit_voice_turn" in source, \
        "语音轮次要带自己的 uiContext（框架说明走系统上下文，不作为用户消息展示）"
    assert "verify_realtime_alert" in source and "dismissRealtimeAlert" in source, \
        "「交给 AI 排查」要用专用上下文，忽略要记住这一处（否则 45s 后同一处又弹）"
    assert "closeUserTranscript" in source and "if (!next) return" in source, \
        "转交必须与字幕记账走同一条判定：重放（null）时既不上字幕也不转交"
    assert "1280 / video.videoWidth, 720" not in source, "固定 1280/720 采集应已被自适应档位取代"
