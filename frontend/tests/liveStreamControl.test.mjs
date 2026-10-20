/**
 * 实时字幕归约的行为守卫（零新依赖，跑的是生产模块本体）。
 *
 * 起因是 2026-10-20 第一次把实时流真正接上线时抓到的两个真缺陷：
 *
 * 1. **用户转写的增量是「累计文本」，不是片段**。服务端的
 *    `conversation.item.input_audio_transcription.delta` 给的是
 *    "And" → "And so" → "And so, my" → "And so, my fellow Americans."，
 *    每一条都包含之前的内容。按片段追加，字幕会碎成一屏
 *    「你And」「你And so」「你And so my」—— 所以草稿必须**整条替换**。
 *
 * 2. **供应商的 final 可能是「这一句」而不是「整轮」**。
 *    `response.audio_transcript.done` 带的是本句完整文本（"I"），
 *    而草稿里累积的是整轮增量之和。一句短的 final 若整条替换，
 *    会把前面一整段回复吃掉（实测：整段回复变成一个 "I"）。
 *
 * 这两条都是"看起来能跑、实际把内容弄坏"的类型，所以要有守卫。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  appendCaptionTurn,
  appendUserTranscriptDraft,
  classifyLivePhase,
  closeUserTranscript,
} from '../src/workbench/liveStreamControl.ts'

test('用户转写增量是累计文本：草稿整条替换，不碎屏', () => {
  let captions = []
  for (const text of ['And', 'And so', 'And so, my', 'And so, my fellow Americans.']) {
    captions = appendUserTranscriptDraft(captions, text)
  }
  assert.equal(captions.length, 1, '整个句子只占一条字幕，不是一条增量一条')
  assert.equal(captions[0].text, 'And so, my fellow Americans.')
  assert.equal(captions[0].role, 'user')
  assert.equal(captions[0].done, false, '草稿还没收口')
})

test('final 整句替换草稿，而不是新起一条', () => {
  let captions = appendUserTranscriptDraft([], 'And so, my fellow')
  captions = closeUserTranscript(captions, 'And so, my fellow Americans.', true)
  assert.equal(captions.length, 1)
  assert.equal(captions[0].text, 'And so, my fellow Americans.')
  assert.equal(captions[0].done, true, 'final 之后收口')
})

test('final 与上一条同文且无草稿 = 重放，不重复记账', () => {
  const captions = [{ role: 'user', text: '你好', done: true }]
  assert.equal(closeUserTranscript(captions, '你好', false), null)
})

test('助手短句 final 不得把整轮回复吃掉', () => {
  let captions = []
  for (const text of ['That', ' sounds', ' like the', ' opening of a speech.']) {
    captions = appendCaptionTurn(captions, { type: 'model.delta', text })
  }
  assert.equal(captions[0].text, 'That sounds like the opening of a speech.')
  // 供应商这一句只给了 "I"（本句完整文本），整轮草稿更长 → 取更长的那份
  captions = appendCaptionTurn(captions, { type: 'model.delta', text: 'I', final: true })
  assert.equal(captions[0].text, 'That sounds like the opening of a speech.', '短句 final 不覆盖整轮')
  assert.equal(captions[0].done, true)
})

test('助手 final 文本比草稿更长时取 final（整条替换）', () => {
  let captions = appendCaptionTurn([], { type: 'model.delta', text: 'I am' })
  captions = appendCaptionTurn(captions, { type: 'model.delta', text: 'I am ready to listen.', final: true })
  assert.equal(captions[0].text, 'I am ready to listen.')
  assert.equal(captions.length, 1)
})

test('助手增量片段的前导空格必须保留（否则拼成 "Iamlistening."）', () => {
  let captions = []
  for (const text of ['I', ' am', ' listening.']) {
    captions = appendCaptionTurn(captions, { type: 'model.delta', text })
  }
  assert.equal(captions[0].text, 'I am listening.', '片段之间的空格是语义的一部分')
})

test('相位：断线优先于观察时间，暂停优先于一切', () => {
  assert.equal(classifyLivePhase({ active: false, socketState: 'open', paused: false, reconnectScheduled: false, lastObservationAgeS: 0 }), 'idle')
  assert.equal(classifyLivePhase({ active: true, socketState: 'open', paused: true, reconnectScheduled: false, lastObservationAgeS: 0 }), 'paused')
  assert.equal(classifyLivePhase({ active: true, socketState: 'closed', paused: false, reconnectScheduled: true, lastObservationAgeS: 0 }), 'connecting')
  assert.equal(classifyLivePhase({ active: true, socketState: 'closed', paused: false, reconnectScheduled: false, lastObservationAgeS: 0 }), 'offline')
  assert.equal(classifyLivePhase({ active: true, socketState: 'open', paused: false, reconnectScheduled: false, lastObservationAgeS: 1 }), 'answering')
  assert.equal(classifyLivePhase({ active: true, socketState: 'open', paused: false, reconnectScheduled: false, lastObservationAgeS: 30 }), 'viewing')
})
