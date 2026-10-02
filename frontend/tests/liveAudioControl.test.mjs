/**
 * Wave8：语音闭环前端纯逻辑的行为守卫（零新依赖，跑的是生产模块本体）。
 *
 * 这一文件补的是 Wave7 明确留给 Wave8 的那块：媒体夹具那条回路刻意【不接能量门】，
 * 所以 `createVoiceGate` 本身当时无人看守。而它正是 2026-09-29 那次「转写完整却零回复」
 * 事故的责任方——服务端 VAD 靠尾部静音收句，说完就掐流会让 turn 永不闭合。
 * 所以这里最要紧的一条不是「状态机走得到 tail」，而是 **tail 期间必须继续 sending**。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CHUNK_MS, OUTPUT_BITS_PER_SAMPLE, OUTPUT_SAMPLE_RATE, TARGET_SAMPLE_RATE,
  clampInt16, createAudioPlaybackClock, createChunkPump, createVoiceGate, encodeAudioPacket, floatToInt16Le,
  frameRms, resolveLiveModelAudioFormat, resample,
} from '../src/workbench/liveAudioControl.ts'

const LOUD = 0.5
const SILENT = 0.0

test('播放短暂预缓冲后逐片无缝衔接，保留原始时长', () => {
  const clock = createAudioPlaybackClock()
  let end = clock.schedule(0, .04) + .04
  assert.ok(Math.abs(end - .1) < 1e-9, '首片只预留 60ms，不等整句话')
  // 40ms PCM packets with uneven delivery still fit inside the jitter reserve.
  for (let i = 1; i < 20; i++) {
    const receivedAt = i * .04 + (i % 2 ? .03 : 0)
    const start = clock.schedule(receivedAt, .04)
    assert.ok(Math.abs(start - end) < 1e-9, '分片边界不插入额外停顿')
    end = start + .04
  }
  assert.ok(Math.abs(end - .86) < 1e-9, '800ms 的语音仍播放 800ms，不变速变调')
})

test('网络欠载时缓冲有上限，下一次回答恢复低延迟', () => {
  const clock = createAudioPlaybackClock()
  let now = 0
  let end = clock.schedule(now, .04) + .04
  for (let i = 0; i < 20; i++) {
    now = end + .01
    const start = clock.schedule(now, .04)
    assert.ok(start >= now + .06 && start <= now + .14 + 1e-9)
    end = start + .04
  }
  now = end + 2
  assert.ok(Math.abs(clock.schedule(now, .04) - now - .06) < 1e-9)
})

test('抢话重置清空上一轮排期，新片不等待旧语音结束', () => {
  const clock = createAudioPlaybackClock()
  clock.schedule(1, 10)
  clock.reset()
  assert.ok(Math.abs(clock.schedule(2, .1) - 2.06) < 1e-9)
  assert.throws(() => clock.schedule(NaN, .1))
  assert.throws(() => clock.schedule(1, 0))
})

test('pcm24 是 24kHz PCM16 的格式名，不是 24-bit 样本', () => {
  const format = resolveLiveModelAudioFormat('pcm24')
  assert.equal(format.sampleRate, OUTPUT_SAMPLE_RATE)
  assert.equal(format.bitsPerSample, OUTPUT_BITS_PER_SAMPLE)
  assert.equal(format.source, 'wire_encoding')
})

test('显式 sample_rate 优先于格式名；未知格式拒绝', () => {
  assert.deepEqual(resolveLiveModelAudioFormat('pcm24', 48000),
    { sampleRate: 48000, bitsPerSample: 16, source: 'wire_sample_rate' })
  assert.equal(resolveLiveModelAudioFormat('opus'), null, '未知格式必须拒，不能按错速播放')
  assert.equal(resolveLiveModelAudioFormat(undefined), null)
})

test('可疑现状（不是期望）：越界的外报采样率被静默丢弃并回落到 pcm24', () => {
  // 实测：resolveLiveModelAudioFormat('pcm24', 4000) 返回 24000/wire_encoding ——
  // 外报的 4000 因为越界被忽略，格式名照样生效。这正是模块文档声称要防的
  // 「按错速播放却看起来成功」。这里断言的是【当前行为】，把事实钉住；
  // 要不要改成拒绝，属于 R2 语音闭环 owner 的决定，我不代改运行时行为。
  assert.deepEqual(resolveLiveModelAudioFormat('pcm24', 4000),
    { sampleRate: 24_000, bitsPerSample: 16, source: 'wire_encoding' })
})

test('float→int16 是小端，且 -1 落到 -32767 而不是 -32768', () => {
  assert.deepEqual([...floatToInt16Le(new Float32Array([1]))], [0xff, 0x7f])
  // 映射用的是 round(v*32767)，所以负端只能到 -32767：断言成 0x80/0x00 就是在猜实现。
  assert.deepEqual([...floatToInt16Le(new Float32Array([-1]))], [0x01, 0x80])
  assert.equal(clampInt16(9), 32767, '超出 [-1,1] 的输入必须削顶而不是绕回')
  assert.equal(clampInt16(-9), -32768, '负端下限是 -32768')
  assert.equal(clampInt16(-1), -32767, '映射是 round(v*32767)，所以 -1 落 -32767')
})

test('重采样按目标率给长度，同率返回副本', () => {
  const input = new Float32Array(TARGET_SAMPLE_RATE)  // 1 秒 @16k
  assert.equal(resample(input, TARGET_SAMPLE_RATE, TARGET_SAMPLE_RATE).length, 16000)
  assert.equal(resample(new Float32Array(48000), 48000, TARGET_SAMPLE_RATE).length, 16000)
  assert.equal(resample(new Float32Array(10), 0, 16000).length, 0, '非法采样率不能瞎猜')
  const same = new Float32Array([1, 2, 3])
  const copy = resample(same, 16000, 16000)
  copy[0] = 99
  assert.equal(same[0], 1, '同率时必须返回副本而不是别名引用')
})

test('frameRms 对满幅方波是 1，对空帧是 0', () => {
  assert.equal(frameRms(new Float32Array([1, -1, 1, -1])), 1)
  assert.equal(frameRms(new Float32Array(0)), 0)
})

test('起说有去抖：一片响亮不足以开门', () => {
  const gate = createVoiceGate()
  const first = gate.feed(LOUD, 0)
  assert.equal(first.state, 'idle')
  assert.equal(first.started, false)
  assert.equal(first.sending, false)
  const second = gate.feed(LOUD, 100)
  assert.equal(second.state, 'speech')
  assert.equal(second.started, true)
  assert.equal(second.sending, true)
})

test('核心契约：停止说话 ≠ 停止推流，静音尾必须继续 sending 满 1 秒', () => {
  const gate = createVoiceGate()
  gate.feed(LOUD, 0); gate.feed(LOUD, 100)
  // 300ms 安静之后进入 tail
  let tail = null
  for (let at = 200; at <= 500 && !tail; at += 100) {
    const signal = gate.feed(SILENT, at)
    if (signal.ended) tail = { at, signal }
  }
  assert.ok(tail, '安静够久应判定说完并进入 tail')
  assert.equal(tail.signal.state, 'tail')
  assert.equal(tail.signal.sending, true, 'ended 那一刻仍必须推流')

  // tail 期间每秒都还在推，直到攒满 tailMs 才停。`ended` 那一片本身也在推流，
  // 所以要一起计入——漏算它就会把「恰好 1 秒」误读成「只有 0.9 秒」。
  let pushes = tail.signal.sending ? 1 : 0
  let done = null
  for (let at = tail.at + 100; at <= tail.at + 1500 && !done; at += 100) {
    const signal = gate.feed(SILENT, at)
    if (signal.sending) pushes += 1
    if (signal.tailDone) done = { at, signal }
  }
  assert.ok(done, '静音尾走完后必须给出 tailDone')
  assert.equal(done.signal.sending, false)
  assert.equal(done.signal.state, 'idle')
  assert.ok(pushes >= 10, `静音尾至少要多推 1 秒（10 片），实测 ${pushes} 片`)
})

test('响度中间断一下不算说完：silenceMs 会被重置', () => {
  const gate = createVoiceGate()
  gate.feed(LOUD, 0); gate.feed(LOUD, 100)
  gate.feed(SILENT, 200); gate.feed(SILENT, 300)
  gate.feed(LOUD, 400)                      // 又出声
  const after = gate.feed(SILENT, 500)
  assert.equal(after.ended, false, '累计静音被中途打断，不能判说完')
})

test('AI 说话时开口 = 抢话，要报 bargeIn 让接线层停播并 cancel', () => {
  const gate = createVoiceGate()
  const opened = gate.feed(LOUD, 0, true)
  assert.equal(opened.bargeIn, false, '一片还不算开口')
  const second = gate.feed(LOUD, 100, true)
  assert.equal(second.started, true)
  assert.equal(second.bargeIn, true)
})

test('静音尾期间再开口就是下一句，回到 speech 且继续推流', () => {
  const gate = createVoiceGate()
  gate.feed(LOUD, 0); gate.feed(LOUD, 100)
  for (const at of [200, 300, 400, 500]) gate.feed(SILENT, at)
  assert.equal(gate.state(), 'tail')
  const again = gate.feed(LOUD, 600)
  assert.equal(again.state, 'speech')
  assert.equal(again.started, true)
  assert.equal(again.sending, true)
})

test('reset 把状态机与时间基线一起清掉', () => {
  const gate = createVoiceGate()
  gate.feed(LOUD, 0); gate.feed(LOUD, 100)
  gate.reset()
  assert.equal(gate.state(), 'idle')
  assert.equal(gate.sending(), false)
  const restart = gate.feed(LOUD, 5000)
  assert.equal(restart.started, false, 'reset 后去抖要重新数，不能拿旧的时间差开门')
})

test('分片泵只产整片，不足的留在 pending 里', () => {
  const pump = createChunkPump()
  assert.equal(pump.chunkSamples(), TARGET_SAMPLE_RATE * CHUNK_MS / 1000)
  assert.deepEqual(pump.push(new Float32Array(pump.chunkSamples() - 1)), [], '不足一片不能产出')
  assert.equal(pump.pending(), pump.chunkSamples() - 1)
  const chunks = pump.push(new Float32Array(2))
  assert.equal(chunks.length, 1, '补齐后应产出恰好一片')
  assert.equal(pump.pending(), 1, '补齐用掉 1 个，剩下 1 个要留到下次')
  assert.equal(chunks[0].length, pump.chunkSamples() * 2)
})

test('分片泵对跨片输入给正确总字节数（与媒体夹具的 3200 字节/片一致）', () => {
  const pump = createChunkPump()
  const per = pump.chunkSamples()
  let bytes = 0
  for (const chunk of pump.push(new Float32Array(per * 3 + 7))) bytes += chunk.length
  assert.equal(bytes, 3 * per * 2, '多余的 7 个样本必须留下次的，不能凭空造一片')
  assert.equal(pump.pending(), 7)
})

test('静音尾片是全零的 3200 字节', () => {
  const pump = createChunkPump()
  const silence = pump.silenceChunk()
  assert.equal(silence.length, 3_200)
  assert.equal(silence.reduce((sum, value) => sum + value, 0), 0)
})

test('audio.chunk 包是 UTF-8 JSON 头 + 换行 + 裸 PCM 载荷', () => {
  const payload = new Uint8Array([1, 2, 3, 4])
  const packet = new Uint8Array(encodeAudioPacket({
    version: 1, sequence: 7, capturedAt: 1_700_000_000_000, payload,
  }))
  const newline = packet.indexOf(0x0a)
  const header = JSON.parse(new TextDecoder().decode(packet.subarray(0, newline)))
  assert.deepEqual(header, { v: 1, type: 'audio.chunk', sequence: 7,
    captured_at: 1_700_000_000_000 })
  assert.deepEqual([...packet.subarray(newline + 1)], [1, 2, 3, 4])
})

test('非法的 sequence / captured_at / 载荷大小必须在组包时就抛', () => {
  const base = { version: 1, sequence: 1, capturedAt: 1_700_000_000_000,
    payload: new Uint8Array([1]) }
  assert.throws(() => encodeAudioPacket({ ...base, sequence: -1 }), /sequence/)
  assert.throws(() => encodeAudioPacket({ ...base, sequence: 1.5 }), /sequence/)
  assert.throws(() => encodeAudioPacket({ ...base, capturedAt: 0 }), /captured_at/)
  assert.throws(() => encodeAudioPacket({ ...base, payload: new Uint8Array(0) }), /大小/)
  assert.throws(() => encodeAudioPacket({ ...base, payload: new Uint8Array(1_000_001) }), /大小/)
})
