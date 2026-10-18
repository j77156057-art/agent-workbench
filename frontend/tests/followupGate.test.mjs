// BUG-3 / BUG-4 前端管线守卫：后端在 final 事件上标的 clarification_required /
// degraded / consistency=warn 必须被 useChatStream 原样转发到回合对象上，
// 否则问答页拿不到这些信号，「猜你想问」chip 就会在澄清 / 降级 / 未核验回合上
// 照常弹出（把「？」「（没有返回内容）」当正常答案追问）。
//
// 这条锁的是**转发**这一环：判据本身（followups computed 的终态门）在 AskApp.vue
// 的 <script setup> 里，node 跑不动 SFC；但只要转发在，computed 的 gate 就有输入。
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

const newTurn = () => ({ id: 7, text: '', reasoning: '', notices: [], trace: [], plan: [] })
function feed(evs) {
  const turn = newTurn()
  const { dispatch } = useChatStream({ onFinal: (t, text) => { t.text = text } })
  for (const ev of evs) dispatch(ev, turn)
  return turn
}

test('final 的 clarification_required 转发到 turn.clarificationRequired', () => {
  const t = feed([{
    type: 'final', text: '这条消息看起来只有标点或符号。请补充具体想问的内容。',
    clarification_required: true, degraded: true,
  }])
  assert.equal(t.clarificationRequired, true, '澄清回合没被标出来，chip 就会误弹')
  assert.equal(t.degraded, true)
})

test('普通完成 final 不带澄清/降级标志（不误伤正常答案）', () => {
  const t = feed([{ type: 'final', text: '玩家移动在 _physics_process 里用 Input.get_vector 实现。' }])
  assert.equal(t.clarificationRequired, undefined)
  assert.equal(t.degraded, undefined)
})

test('consistency=warn 连同命中率转发，供未核验终态标记消费（BUG-4）', () => {
  const t = feed([{
    type: 'final', text: '一份可能漏了东西的答案。',
    consistency: 'warn', consistency_ratio: 0.19,
  }])
  assert.equal(t.consistency, 'warn')
  assert.equal(t.consistencyRatio, 0.19)
})

test('consistency=ok 正常记录，不触发未核验', () => {
  const t = feed([{ type: 'final', text: '扎实的答案。', consistency: 'ok', consistency_ratio: 0.9 }])
  assert.equal(t.consistency, 'ok')
  assert.equal(t.consistencyRatio, 0.9)
})
