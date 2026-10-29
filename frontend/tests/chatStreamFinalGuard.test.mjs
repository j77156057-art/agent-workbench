// 首条 final 守卫：终态重复下发时只应用第一条。
// 此前只在问答页调用方有守卫，工作台对话台没有 —— 后到的 final 会覆盖答案，
// 或把已经成功的回合翻成 error。
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

function makeTurn() {
  return {
    id: 1, text: '', reasoning: '', notices: [], trace: [], flow: [],
    status: 'streaming',
  }
}

test('重复 final 只应用第一条，后到的文本不覆盖答案', () => {
  const applied = []
  const { dispatch } = useChatStream({
    onFinal: (turn, text) => { turn.text = text; applied.push(text) },
  })
  const turn = makeTurn()
  dispatch({ type: 'final', text: '第一条答案' }, turn)
  dispatch({ type: 'final', text: '第二条答案' }, turn)
  assert.equal(turn.text, '第一条答案')
  assert.deepEqual(applied, ['第一条答案'])
})

test('成功 final 之后的 error final 不能把已完成的回合翻成 error', () => {
  const { dispatch } = useChatStream({ onFinal: (turn, text) => { turn.text = text } })
  const turn = makeTurn()
  dispatch({ type: 'final', text: '完整答案' }, turn)
  turn.status = 'done'
  dispatch({ type: 'final', status: 'error', error_kind: 'permanent' }, turn)
  assert.equal(turn.status, 'done')
  assert.equal(turn.text, '完整答案')
})

test('error final 先到时，后到的成功 final 也不覆盖错误终态', () => {
  const { dispatch } = useChatStream({ onFinal: (turn, text) => { turn.text = text } })
  const turn = makeTurn()
  dispatch({ type: 'final', status: 'error', error_kind: 'permanent' }, turn)
  dispatch({ type: 'final', text: '迟到的答案' }, turn)
  assert.equal(turn.status, 'error')
  assert.equal(turn.text, '')
})
