import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { advanceVisualActionLoop, beginVisualActionLoop, createVisualClickFlow, restoreVisualActionLoop } from '../src/workbench/visualActionLoop.ts'

const modelSource = readFileSync(new URL('../src/workbench/api/model.ts', import.meta.url), 'utf8')
const panelSource = readFileSync(new URL('../src/workbench/components/SceneRuntimePanel.vue', import.meta.url), 'utf8')

const step = (target, status, nextTarget = '') => ({ at: new Date().toISOString(), target, status,
  evidence: '可见状态', nextTarget })
const proposal = { proposal_id: 'proposal-1', image: 'before.png', bbox: [0.1, 0.1, 0.4, 0.4],
  point: { x: 20, y: 20 }, label: '保存', evidence: '可见保存文字', expires_in: 120 }
const pending = { ok: false, httpStatus: 202, state: 'pending', pending: true, executed: false,
  ticket_id: 'vision-proposal-1', action: 'desktop_action', target: 'vision:click:proposal-1' }
const completed = { ok: true, httpStatus: 200, state: 'completed', executed: true,
  before: 'before.png', after: 'after.png', label: '保存',
  verification: { status: 'met', evidence: '出现保存成功提示' } }
const approvalQueue = () => ({ items: [{ id: 'gate-1', action: 'desktop_action', target: 'vision:click:proposal-1' }] })

test('unmet action proposes a fresh next target but never executes it', () => {
  const started = beginVisualActionLoop('project', 'workflow', '看到完成提示', '保存')
  const next = advanceVisualActionLoop(started, step('保存', 'unmet', '确认'))
  assert.equal(next.phase, 'locating')
  assert.equal(next.lastTarget, '确认')
  assert.equal(next.steps.length, 1)
  assert.equal(advanceVisualActionLoop(next, step('确认', 'met')).phase, 'awaiting_user')
})

test('uncertain judgement and repeated suggestion stop the loop', () => {
  const started = beginVisualActionLoop('project', '', '完成', '保存')
  assert.equal(advanceVisualActionLoop(started, step('保存', 'uncertain', '确认')).phase, 'needs_review')
  const first = advanceVisualActionLoop(started, step('保存', 'unmet', '确认'))
  const second = advanceVisualActionLoop(first, step('确认', 'unmet', '保存'))
  assert.equal(second.phase, 'needs_review')
  assert.match(second.note, /重复点击/)
})

test('restored proposals require fresh localization and stay project scoped', () => {
  const started = beginVisualActionLoop('project', 'workflow', '完成', '保存')
  const raw = JSON.stringify({ ...started, phase: 'awaiting_confirmation' })
  assert.equal(restoreVisualActionLoop(raw, 'project', 'workflow').phase, 'needs_review')
  assert.equal(restoreVisualActionLoop(raw, 'other', 'workflow'), null)
  assert.equal(restoreVisualActionLoop(raw, 'project', 'other'), null)
})

test('execute-click request contract sends only proposal_id and preserves HTTP status', () => {
  const source = modelSource.slice(modelSource.indexOf('executeClick('), modelSource.indexOf('analyzeVideo('))
  assert.match(source, /JSON\.stringify\(\{ proposal_id: proposalId \}\)/)
  assert.doesNotMatch(source, /confirmed/)
  assert.match(source, /httpStatus: res\.status/)
})

test('202 renders pending with ticket details, never a successful click', async () => {
  const flow = createVisualClickFlow({ execute: async () => pending,
    listApprovals: approvalQueue, decide: async () => ({ ok: true }) })
  flow.propose(proposal)
  await flow.execute()
  const state = flow.snapshot()
  assert.equal(state.phase, 'pending')
  assert.equal(state.result, null)
  assert.equal(state.proposal.proposal_id, proposal.proposal_id)
  assert.equal(state.ticketId, 'vision-proposal-1')
  assert.equal(state.action, 'desktop_action')
  assert.equal(state.target, 'vision:click:proposal-1')
  assert.match(state.notice, /尚未点击/)
  assert.match(panelSource, /class="pb-click-pending" role="status"/)
  assert.match(panelSource, /待审批，尚未点击/)
  assert.match(panelSource, /clickState\.ticketId/)
})

test('approval retries the same proposal once and exposes the completed result', async () => {
  const calls = []
  const responses = [pending, completed]
  const decisions = []
  const flow = createVisualClickFlow({
    execute: async id => { calls.push(id); return responses.shift() },
    listApprovals: approvalQueue,
    decide: async (id, approved) => { decisions.push({ id, approved }); return { ok: true } },
  })
  flow.propose(proposal)
  await flow.execute()
  await flow.approve()
  const state = flow.snapshot()
  assert.deepEqual(calls, [proposal.proposal_id, proposal.proposal_id])
  assert.deepEqual(decisions, [{ id: 'gate-1', approved: true }])
  assert.equal(state.phase, 'completed')
  assert.equal(state.result.after, 'after.png')
  assert.equal(state.result.verification.status, 'met')
})

test('duplicate approval cannot issue another execution request or success notice', async () => {
  const calls = []
  const responses = [pending, { ...completed, idempotent: true }]
  let decisions = 0
  const flow = createVisualClickFlow({
    execute: async id => { calls.push(id); return responses.shift() },
    listApprovals: approvalQueue,
    decide: async () => { decisions++; return { ok: true } },
  })
  flow.propose(proposal)
  await flow.execute()
  await Promise.all([flow.approve(), flow.approve()])
  await flow.approve()
  assert.equal(calls.length, 2)
  assert.equal(decisions, 1)
  assert.equal(flow.snapshot().phase, 'completed')
  assert.equal(flow.snapshot().notice, '')
})

test('409 is an explicit consumed-proposal error, not a completed click', async () => {
  const flow = createVisualClickFlow({
    execute: async () => ({ ok: false, httpStatus: 409, state: 'error', error: '提案已经执行过一次' }),
    listApprovals: approvalQueue, decide: async () => ({ ok: true }),
  })
  flow.propose(proposal)
  await flow.execute()
  const state = flow.snapshot()
  assert.equal(state.phase, 'error')
  assert.equal(state.result, null)
  assert.equal(state.proposal, null)
  assert.match(state.error, /HTTP 409/)
  assert.match(state.error, /不能重复点击/)
})

test('rejection returns to a fresh-proposal state without another execution', async () => {
  let executions = 0
  const decisions = []
  const flow = createVisualClickFlow({
    execute: async () => { executions++; return pending },
    listApprovals: approvalQueue,
    decide: async (id, approved) => { decisions.push({ id, approved }); return { ok: true } },
  })
  flow.propose(proposal)
  await flow.execute()
  await flow.reject()
  assert.equal(executions, 1)
  assert.deepEqual(decisions, [{ id: 'gate-1', approved: false }])
  assert.equal(flow.snapshot().phase, 'idle')
  assert.equal(flow.snapshot().proposal, null)
  assert.match(flow.snapshot().notice, /可以重新定位/)
  flow.propose({ ...proposal, proposal_id: 'fresh-proposal' })
  assert.equal(flow.snapshot().phase, 'ready')
  assert.equal(flow.snapshot().proposal.proposal_id, 'fresh-proposal')
})

test('renewed expiry is pending again, not a forced re-localization', async () => {
  const flow = createVisualClickFlow({
    execute: async () => ({ ...pending, renewed: true, renewal: 1 }),
    listApprovals: approvalQueue, decide: async () => ({ ok: true }),
  })
  flow.propose(proposal)
  await flow.execute()
  const state = flow.snapshot()
  assert.equal(state.phase, 'pending')
  assert.equal(state.renewed, true)
  assert.equal(state.renewal, 1)
  assert.match(state.notice, /已重新签票/)
  assert.match(state.notice, /无需重新定位/)
  assert.match(panelSource, /过期已续签，等待再次批准/)
})

test('invalid proposal has a distinct re-localization error', async () => {
  const flow = createVisualClickFlow({
    execute: async () => ({ ok: false, httpStatus: 404, state: 'error', error: '提案已失效或不再可用' }),
    listApprovals: approvalQueue, decide: async () => ({ ok: true }),
  })
  flow.propose(proposal)
  await flow.execute()
  const state = flow.snapshot()
  assert.equal(state.phase, 'error')
  assert.equal(state.result, null)
  assert.match(state.error, /必须重新定位/)
  assert.doesNotMatch(state.error, /过期/)
})
