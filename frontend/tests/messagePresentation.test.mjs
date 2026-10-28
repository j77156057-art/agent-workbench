import assert from 'node:assert/strict'
import { test } from 'node:test'
import { getMessagePresentation } from '../src/ask/messagePresentation.ts'

function msg(extra = {}) {
  return {
    id: 1,
    role: 'assistant',
    text: '',
    status: 'done',
    trace: [],
    reasoning: '',
    notices: [],
    plan: [],
    ...extra,
  }
}

test('失败与停止优先于其他终态', () => {
  assert.deepEqual(getMessagePresentation(msg({ status: 'error' })), { label: '回答失败', tone: 'danger' })
  assert.deepEqual(getMessagePresentation(msg({ status: 'stopped', degraded: true })), { label: '已停止', tone: 'neutral' })
})

test('流式阶段区分是否已有正文', () => {
  assert.deepEqual(getMessagePresentation(msg({ status: 'streaming' })), { label: '正在处理', tone: 'accent' })
  assert.deepEqual(getMessagePresentation(msg({ status: 'streaming', text: '部分回答' })), { label: '正在回答', tone: 'accent' })
})

test('等待授权/补充优先于普通完成，选择后不冒充任务完成', () => {
  const ask = { kind: 'permission', question: '允许吗？', options: [] }
  assert.deepEqual(getMessagePresentation(msg({ ask })), { label: '等待授权', tone: 'warning' })
  assert.deepEqual(getMessagePresentation(msg({ ask }), true), { label: '已提交选择', tone: 'neutral' })
  const clarify = { kind: 'clarify', question: '补充什么？', options: [] }
  assert.equal(getMessagePresentation(msg({ ask: clarify })).label, '等待补充')
})

test('降级与一致性提示不被标成完整完成', () => {
  assert.equal(getMessagePresentation(msg({ degraded: true })).label, '未完成')
  assert.equal(getMessagePresentation(msg({ consistencyReason: 'verbatim_dump' })).label, '未综合结论')
  assert.equal(getMessagePresentation(msg({ consistency: 'warn', consistencyRatio: 0.4 })).label, '需核对')
  assert.deepEqual(getMessagePresentation(msg({ consistency: 'ok' })), { label: '已回复', tone: 'neutral' })
})
