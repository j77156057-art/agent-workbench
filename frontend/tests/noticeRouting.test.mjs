// notice 的分流判据：由后端标的 transient 决定，不由前端比文案猜。
//
// 为什么单独一条测试：旧版实现是一份四条文案白名单（已进入模型处理 / 模型仍在处理 /
// 正在准备本轮上下文 / 正在等待模型输出），与后端**零交集** —— 那 4 条后端一条都不发，
// 而它实际发的 3 条一条都没被过滤，于是「模型输出尚未结束，正在继续执行。」在任务
// 完成后还挂在「已完成 · 40s」下面（docs/known-defect-notice-whitelist-detached.md）。
// 那条白名单失效了很久却全绿，因为**没有任何测试锁过 notice 的过滤行为**。
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

const newTurn = () => ({ id: 7, text: '', reasoning: '', notices: [], trace: [], plan: [] })
/** 按顺序喂事件，返回回合最终状态。 */
function feed(evs) {
  const turn = newTurn()
  // useChatStream 故意**不**把 final 正文写进 turn.text —— 覆盖/守卫策略归调用方
  // （见该文件头）。这里补一个最朴素的钩子，好让下面的断言读得到真实形态。
  const { dispatch } = useChatStream({ onFinal: (t, text) => { t.text = text } })
  for (const ev of evs) dispatch(ev, turn)
  return turn
}

test('标了 transient 的 notice 进运行指示，不进历史通知', () => {
  const t = feed([{ type: 'notice', text: '模型输出尚未结束，正在继续执行。', transient: true }])
  assert.equal(t.progress, '模型输出尚未结束，正在继续执行。')
  assert.deepEqual(t.notices, [], '运行期进度不该在历史里留一行')
})

test('没标 transient 的 notice 留在历史里，重复只记一次', () => {
  const t = feed([
    { type: 'notice', text: '模型单次输出已满，正在接续完整回答。' },
    { type: 'notice', text: '模型单次输出已满，正在接续完整回答。' },
  ])
  assert.deepEqual(t.notices, ['模型单次输出已满，正在接续完整回答。'],
    '答案真被截断过是**事实**，回合结束后用户仍需要知道')
  assert.equal(t.progress, undefined, '没标记就不该被当成运行期进度')
})

// 这条是防「又回到比文案」的关键：旧白名单会**静默丢掉**这四条，而当前后端一条都不发，
// 所以行为测试只有在这里才照得出判据是不是仍然按文案走。
test('判据不再看文案：白名单里的老句子现在照常进历史', () => {
  for (const text of ['已进入模型处理', '模型仍在处理', '正在准备本轮上下文', '正在等待模型输出']) {
    const t = feed([{ type: 'notice', text }])
    assert.deepEqual(t.notices, [text], `${text}：按文案特判就是判据又漂移回去了`)
    assert.equal(t.progress, undefined)
  }
})

test('final 清掉运行期提示', () => {
  const t = feed([
    { type: 'notice', text: '模型输出尚未结束，正在继续执行。', transient: true },
    { type: 'final', text: '这是完整答案。' },
  ])
  assert.equal(t.text, '这是完整答案。')
  assert.equal(t.progress, '', '回合结束后还挂着「正在继续执行」就是假陈述')
})

test('done 也清：出错收尾时 final 可能没有正文', () => {
  const t = feed([
    { type: 'notice', text: '模型输出尚未结束，正在继续执行。', transient: true },
    { type: 'done' },
  ])
  assert.equal(t.progress, '')
})

test('transient 只影响这一条，后续事实通知照常留', () => {
  const t = feed([
    { type: 'notice', text: '模型输出尚未结束，正在继续执行。', transient: true },
    { type: 'notice', text: '早期 3 轮对话已自动压缩为摘要，新问答不受影响。' },
  ])
  assert.deepEqual(t.notices, ['早期 3 轮对话已自动压缩为摘要，新问答不受影响。'])
  assert.equal(t.progress, '模型输出尚未结束，正在继续执行。',
    '没标记的那条不该覆盖运行指示 —— 它进的是历史')
})
