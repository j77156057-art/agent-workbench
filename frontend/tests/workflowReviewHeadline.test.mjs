import assert from 'node:assert/strict'
import { test } from 'node:test'
import { reviewHeadlineOf, statusLabelOf, displayStatusOf } from '../src/workbench/composables/workflow-format.ts'

/**
 * 复核区标题必须**分两维度说**，不能读被压扁的 `review.ok`。
 *
 * ## 实测（`wf-af6a2e4a9b2b` 真跑）
 *
 * 四任务全 ok、十项检查全 True、evidence_verified、引用 50/30/28/2 条不过 0 条、
 * change_count=0（纯只读）。后端分开存了 `review.agent_ok`（内容+证据）与
 * `review.project.ok`（独立验证），`review.ok` 是两者的与。
 *
 * 复核区原来渲染 `state.review.ok` -> 「! 复核未通过」，而同一张卡片的头部
 * 已经按 `24c845c` 显示「已完成 · 未做独立验证」。**两句互相打架。**
 * 且那一轮 `failures` 是空数组，于是复核区只剩一句指控，没有任何解释。
 */

const unverifiedRun = {
  status: 'failed',
  review: {
    ok: false,
    agent_ok: true,
    failures: [],
    checks: {
      has_result: true, no_replacement_chars: true, credentials_absent: true,
      sensitive_terms_without_value: true, tool_failures_below_limit: true,
      status_ok: true, file_evidence_bounded: true, test_evidence_bounded: true,
      tests_ok: true, evidence_verified: true,
    },
    project: {
      ok: false, status: 'unverified', independent: true, change_count: 0,
      changes: [], diff: '', message: '试做区没有容器级命令隔离，不能作为独立测试通过的证据',
    },
  },
}

test('实测那一轮：内容复核通过 + 未做独立验证 —— 不能说「复核未通过」', () => {
  const h = reviewHeadlineOf(unverifiedRun)
  assert.equal(h.tone, 'unverified')
  assert.ok(!h.text.includes('复核未通过'), `实际拿到：${h.text}`)
  assert.ok(h.text.includes('内容复核通过'), `实际拿到：${h.text}`)
  assert.ok(h.text.includes('unverified'), `应说明独立复核的实际状态：${h.text}`)
})

test('与头部状态标签不再自相矛盾（同一份数据，两处说法一致）', () => {
  const head = statusLabelOf(unverifiedRun)
  const body = reviewHeadlineOf(unverifiedRun)
  assert.equal(head, '已完成 · 未做独立验证')
  assert.equal(displayStatusOf(unverifiedRun), 'completed_unverified')
  assert.equal(body.tone, 'unverified')
})

test('反向守卫：内容复核真失败时，仍然红色「复核未通过」', () => {
  const failed = {
    status: 'failed',
    review: { ok: false, agent_ok: false, failures: [{ message: '缺少可核验的文件原文' }] },
  }
  const h = reviewHeadlineOf(failed)
  assert.equal(h.tone, 'bad', '真失败不能被降级成琥珀色')
  assert.equal(h.text, '! 复核未通过')
})

test('反向守卫：独立复核通过时不该出现「未做独立验证」', () => {
  const passed = { status: 'completed', review: { ok: true, agent_ok: true, project: { ok: true, status: 'passed' } } }
  assert.deepEqual(reviewHeadlineOf(passed), { text: '✓ 复核通过', tone: 'ok' })
})

test('反向守卫：agent_ok 为真但 project 字段整个缺失时也不该说「复核未通过」', () => {
  const h = reviewHeadlineOf({ review: { ok: false, agent_ok: true, failures: [] } })
  assert.equal(h.tone, 'unverified')
  assert.ok(!h.text.includes('（'), 'project 缺失时不该凭空造一个状态名')
})

test('数据还没到（review 缺失）时按真失败处理，不假装通过', () => {
  assert.equal(reviewHeadlineOf({}).tone, 'bad')
  assert.equal(reviewHeadlineOf(null).tone, 'bad')
})