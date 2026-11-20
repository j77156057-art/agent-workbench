/**
 * 原始输出缓冲的规矩。
 *
 * 右侧浮层的「原始输出」页签显示的就是这份缓冲。它必须守住三条：
 *   1. 连续 token 增量**合并成一条**（逐 token 一行会把面板刷爆）；
 *   2. 只在内存里，刷新即清空 —— 不写文件、不记提问与代码原文；
 *   3. 条数有上限，长会话不能把内存吃满。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  clearRawOutput,
  rawOutputEvents,
  recordRawEvent,
} from '../src/workbench/composables/rawOutput.ts'

test.beforeEach(() => clearRawOutput())

test('连续 token 合并成一条，文本与摘要同步更新', () => {
  recordRawEvent({ type: 'token', text: '你' }, 's1')
  recordRawEvent({ type: 'token', text: '好' }, 's1')
  recordRawEvent({ type: 'token', text: '！' }, 's1')
  const events = rawOutputEvents()
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'token')
  assert.equal(events[0].data.text, '你好！')
  assert.equal(events[0].summary, '3 字')
  assert.equal(events[0].session, 's1')
})

test('非 token 事件各占一条，顺序保持', () => {
  recordRawEvent({ type: 'trace', text: 'read_file(path: a.py)' }, 's1')
  recordRawEvent({ type: 'token', text: 'x' }, 's1')
  recordRawEvent({ type: 'final', text: '答案' }, 's1')
  const types = rawOutputEvents().map((e) => e.type)
  assert.deepEqual(types, ['trace', 'token', 'final'])
})

test('token 之后再来 token 仍是新的一条（不跨事件合并）', () => {
  recordRawEvent({ type: 'token', text: 'a' }, 's1')
  recordRawEvent({ type: 'trace', text: 'note' }, 's1')
  recordRawEvent({ type: 'token', text: 'b' }, 's1')
  assert.equal(rawOutputEvents().length, 3)
})

test('摘要按事件类型给：final 给字数，长文本截断', () => {
  recordRawEvent({ type: 'final', text: '一二三四五' }, 's1')
  assert.equal(rawOutputEvents()[0].summary, '最终答案 5 字')
  clearRawOutput()
  const long = 'x'.repeat(200)
  recordRawEvent({ type: 'trace', text: long }, 's1')
  assert.ok(rawOutputEvents()[0].summary.length <= 91)
  assert.ok(rawOutputEvents()[0].summary.endsWith('…'))
})

test('条数有上限：只留最近的', () => {
  for (let i = 0; i < 460; i++) recordRawEvent({ type: 'note', text: String(i) }, 's1')
  const events = rawOutputEvents()
  assert.ok(events.length <= 400, `缓冲不该超过 400 条，实际 ${events.length}`)
  assert.equal(events[events.length - 1].summary, '459')
})

test('清空后没有残留', () => {
  recordRawEvent({ type: 'token', text: 'a' }, 's1')
  clearRawOutput()
  assert.equal(rawOutputEvents().length, 0)
})
