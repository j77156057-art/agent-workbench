import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createVoiceVerificationGate } from '../src/workbench/voiceVerification.ts'

test('a clue waits for clear consent; ambiguous speech never starts checking', () => {
  const gate = createVoiceVerificationGate()
  gate.offer('clue-1', '保存按钮被遮住了', 1000)
  assert.equal(gate.decide('可能吧', 1100), 'clarify')
  assert.equal(gate.matches('clue-1'), false)
  assert.equal(gate.decide('可以核查一下。', 1200), 'approve')
  assert.equal(gate.matches('clue-1'), true)
  assert.equal(gate.decide('可以', 1300), 'checking')
  assert.equal(gate.offer('clue-2', '另一个问题', 1400), false)
  assert.equal(gate.complete('clue-2'), false)
  assert.equal(gate.complete('clue-1'), true)
  assert.equal(gate.complete('clue-1'), false)
})

test('refusal, stale consent and reconnect cannot authorize or complete a task', () => {
  const gate = createVoiceVerificationGate()
  gate.offer('refused', '报错', 1000)
  assert.equal(gate.decide('不用核查', 1100), 'decline')
  assert.equal(gate.approve('refused', 1200), false)
  gate.offer('expired', '报错', 2000)
  assert.equal(gate.decide('好的', 123000), 'expired')
  gate.offer('old-session', '报错', 124000)
  assert.equal(gate.approve('old-session', 124001), true)
  gate.reset()
  assert.equal(gate.complete('old-session'), false)
})
