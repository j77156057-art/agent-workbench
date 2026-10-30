// 记忆 v2：final 事件里的交付标记要落到回合上，满意度卡据此显示。
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

function makeTurn() {
  return {
    id: 1, text: '', reasoning: '', notices: [], trace: [], flow: [],
    status: 'streaming',
  }
}

test('交付型 final 把 deliverable/task 写到回合上', () => {
  const { dispatch } = useChatStream({})
  const turn = makeTurn()
  dispatch({ type: 'final', text: '已完成', deliverable: true, task: 'tb-1' }, turn)
  assert.equal(turn.deliverable, true)
  assert.equal(turn.task, 'tb-1')
})

test('普通 final 不带交付标记', () => {
  const { dispatch } = useChatStream({})
  const turn = makeTurn()
  dispatch({ type: 'final', text: '只是一段回答' }, turn)
  assert.equal(turn.deliverable, undefined)
  assert.equal(turn.task, undefined)
})

test('降级回合即使带 deliverable，上层也会按 degraded 抑制（字段各自独立）', () => {
  const { dispatch } = useChatStream({})
  const turn = makeTurn()
  dispatch({ type: 'final', text: '降级', deliverable: true, degraded: true }, turn)
  assert.equal(turn.deliverable, true)
  assert.equal(turn.degraded, true)
})
