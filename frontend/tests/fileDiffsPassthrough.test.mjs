/**
 * 写文件工具的「完整前后改动」必须从 SSE 事件一路带到 trace 条目，供「查看改动」渲染。
 *
 * 背景：observation 正文只给模型看的一行摘要（如「已写入 a.py，+12 -3」），用户点进去
 * 看不到真正改了哪几行。修法是后端随 observation 下发结构化 `file_diffs`（含 old/new 全文），
 * 前端 appendTrace 原样挂到 trace 条目上，FileDiffDialog 据此渲染统一 diff。
 *
 * 这里钉住的是**接线**（与 observationOkFlag.test.mjs 同源，只锁最容易被无声改坏的一段）：
 * - appendTrace 把传入的 file_diffs 存进 trace 条目；
 * - 没带 file_diffs 时**不写**该字段（读类工具/旧后端不该冒出「查看改动」按钮）；
 * - dispatch 把 observation 事件上的 file_diffs 端到端透传。
 * 弹窗渲染本体（FileDiffDialog.vue）依赖 Vue，跑不进 node，故由工作台实跑截图核验。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

function newTurn() {
  return { id: 1, text: '', reasoning: '', notices: [], trace: [] }
}

const sampleDiffs = [
  { path: 'agent.py', kind: 'modify', old: 'a\nb\n', new: 'a\nc\n', diff: '-b\n+c' },
  { path: 'new.py', kind: 'create', old: '', new: 'x\n', diff: '+x', truncated: false },
]

test('appendTrace 把写文件 observation 的 file_diffs 落到 trace 条目', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', '已写入 agent.py', undefined, true, sampleDiffs)
  assert.equal(turn.trace.length, 1)
  assert.equal(turn.trace[0].type, 'observation')
  assert.ok(Array.isArray(turn.trace[0].file_diffs), 'file_diffs 必须原样挂上，供「查看改动」渲染')
  assert.equal(turn.trace[0].file_diffs.length, 2)
  assert.equal(turn.trace[0].file_diffs[0].path, 'agent.py')
  assert.equal(turn.trace[0].file_diffs[0].new, 'a\nc\n', '完整 new 全文得留住，不能只存摘要')
})

test('appendTrace 在未提供 file_diffs 时不写该字段（读类工具不冒改动按钮）', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', 'read_file 返回内容', undefined, true)
  assert.ok(!('file_diffs' in turn.trace[0]),
            '没有结构化改动时不得凭空写 file_diffs，否则前端会给只读步骤渲染「查看改动」')
})

test('appendTrace 空数组 file_diffs 也不写字段', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', 'noop', undefined, true, [])
  assert.ok(!('file_diffs' in turn.trace[0]), '空改动列表等价于没有改动，不该留空数组占位')
})

test('dispatch 把 observation 事件的 file_diffs 透传给 appendTrace（端到端接线）', () => {
  const { dispatch } = useChatStream({})
  const turn = newTurn()
  dispatch({ type: 'action', text: 'apply_edit(agent.py)' }, turn)
  dispatch({ type: 'observation', text: '已写入 agent.py', ok: true, file_diffs: sampleDiffs }, turn)
  const obs = turn.trace.find((t) => t.type === 'observation')
  assert.ok(obs, 'observation 应进 trace')
  assert.ok(obs.file_diffs && obs.file_diffs.length === 2,
            'SSE 事件上的 file_diffs 必须一路传到 trace 条目——这是「点击看改动」能拿到前后全文的那根线')
})
