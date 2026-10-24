/**
 * observation 的「真实成败」标志必须从 SSE 事件一路带到 trace 条目上。
 *
 * 起因（2026-10-24 实测）：一次成功的 read_file 在工作台时间线里被标成「失败」。
 * 根因是渲染层用「返回正文里有没有『错误/失败/超时』」来染色，而读到的源码天然
 * 含这些字。修法是后端随 observation 下发结构化 `ok`，前端采信它、不再 grep 正文。
 *
 * 这条守卫钉住的是**接线**：appendTrace 必须把 `ev.ok` 存进 trace 条目，且
 * - ok=true / ok=false 原样落库（渲染层据此判成败）；
 * - 没带 ok（旧后端 / 未执行的安全护栏观察）时**不写**该字段，让渲染层回落到
 *   只认行首失败签名的逻辑，而不是误当成成功。
 * 渲染层的 traceState 本体在 useMessageRender.ts 里，依赖 Vue，跑不进 node
 * （node --experimental-strip-types 直接 import 生产模块），故染色判定由工作台
 * 实跑截图核验；这里只锁最容易被无声改坏的一段。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

function newTurn() {
  return { id: 1, text: '', reasoning: '', notices: [], trace: [] }
}

test('appendTrace 把 observation 的 ok=true 落到 trace 条目', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', 'agent.py 前 80 行内容里出现「错误处理」字样', undefined, true)
  assert.equal(turn.trace.length, 1)
  assert.equal(turn.trace[0].type, 'observation')
  assert.equal(turn.trace[0].ok, true, '成功的读取必须带 ok=true，渲染层据此不标红')
})

test('appendTrace 把 observation 的 ok=false 落到 trace 条目', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', '工具执行失败（FileNotFoundError）。', undefined, false)
  assert.equal(turn.trace[0].ok, false, '真实失败必须带 ok=false，别把红洗成绿')
})

test('appendTrace 在未提供 ok 时不写该字段（回落到行首签名判定）', () => {
  const { appendTrace } = useChatStream({})
  const turn = newTurn()
  appendTrace(turn, 'observation', '[安全拦截] write_file 未执行：用户没有要求创建或修改文件。')
  assert.ok(!('ok' in turn.trace[0]),
            '没有结构化标志时不得凭空写 ok=true/false，否则渲染层会跳过失败签名回落')
})

test('dispatch 把 observation 事件的 ok 透传给 appendTrace（端到端接线）', () => {
  const { dispatch } = useChatStream({})
  const turn = newTurn()
  dispatch({ type: 'action', text: 'read_file(agent.py)' }, turn)
  dispatch({ type: 'observation', text: '1: import os\n2: # 读取失败时降级', ok: true }, turn)
  const obs = turn.trace.find((t) => t.type === 'observation')
  assert.ok(obs, 'observation 应进 trace')
  assert.equal(obs.ok, true,
               'SSE 事件上的 ok 必须一路传到 trace 条目——这是把「成功读取不再标红」接上的那根线')
})
