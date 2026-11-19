/**
 * 工具调用卡片的组装口径。
 *
 * 对话台改成 Qoder 式之后，轨迹不再逐条裸露：一个工具一张卡，返回结果收在
 * 卡里。这里钉住四件事——
 *  1) 一次工具调用 + 它的返回 = **一张**卡（不是两行）；
 *  2) 状态以工具的真实成败为准（observation.ok），不从返回正文里 grep 失败词；
 *  3) 思考（think / thought / reflection）各自成卡，不混进工具卡；
 *  4) 参数摘要不会被嵌套括号、逗号或换行切错。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { cardsOf, cardsHeadline, parseCall, paramsSummary, toolTitleOf } from '../src/workbench/composables/chatCards.ts'

function t(type, text, extra = {}) {
  return { type, text, at: 1, ...extra }
}

test('一次工具调用加它的返回折成一张卡', () => {
  const cards = cardsOf({
    status: 'done',
    trace: [
      t('action', 'read_file(path: README.md)'),
      t('observation', '文件内容第一行', { ok: true, elapsedMs: 120 }),
    ],
  })
  assert.equal(cards.length, 1)
  assert.equal(cards[0].kind, 'tool')
  assert.equal(cards[0].title, '读取文件')
  assert.equal(cards[0].params, 'path=README.md')
  assert.equal(cards[0].state, 'ok')
  assert.equal(cards[0].stateLabel, '完成')
  assert.equal(cards[0].elapsedMs, 120)
  assert.equal(cards[0].body, '文件内容第一行')
})

test('工具失败时卡片标红（以 observation.ok 为准）', () => {
  const cards = cardsOf({
    status: 'done',
    trace: [
      t('action', 'run_command(cmd: pytest -q)'),
      t('observation', 'FAILED tests/test_x.py::test_y', { ok: false }),
    ],
  })
  assert.equal(cards[0].state, 'error')
  assert.equal(cards[0].stateLabel, '失败')
  // 正文里含「失败」但 ok=true 的，不能被误判成失败
  const okCards = cardsOf({
    status: 'done',
    trace: [
      t('action', 'read_file(path: a.py)'),
      t('observation', '这段源码里有 except ValueError: 失败处理', { ok: true }),
    ],
  })
  assert.equal(okCards[0].state, 'ok')
})

test('没有 observation 的调用在流式中是进行中', () => {
  const cards = cardsOf({ status: 'streaming', trace: [t('action', 'read_file(path: a.py)')] })
  assert.equal(cards.length, 1)
  assert.equal(cards[0].state, 'running')
  assert.equal(cards[0].stateLabel, '进行中')
})

test('思考与工具各成一张卡：结果不与思考混在一起', () => {
  const cards = cardsOf({
    status: 'done',
    trace: [
      t('thought', '先读 README'),
      t('action', 'read_file(path: README.md)'),
      t('observation', '内容', { ok: true }),
      t('reflection', '结果看起来对不上'),
    ],
    flow: [
      { kind: 'thought', trace: 0 },
      { kind: 'action', trace: 1 },
      { kind: 'result', trace: 2 },
      { kind: 'thought', trace: 3 },
    ],
  })
  assert.equal(cards.length, 3)
  assert.equal(cards[0].kind, 'think')
  assert.equal(cards[0].title, '分析')
  assert.equal(cards[1].kind, 'tool')
  assert.equal(cards[1].title, '读取文件')
  assert.equal(cards[2].kind, 'think')
})

test('模型原生思考（think 节点）单独成卡', () => {
  const cards = cardsOf({
    status: 'done',
    trace: [],
    flow: [{ kind: 'think', text: '先想清楚再动手' }],
  })
  assert.equal(cards.length, 1)
  assert.equal(cards[0].kind, 'think')
  assert.equal(cards[0].title, '深度思考')
  assert.ok(cards[0].body.includes('先想清楚'))
})

test('参数摘要不被嵌套括号、逗号或换行切错', () => {
  const call = parseCall('run_command(cmd: pytest -q, timeout: 180, note: a, b (c))')
  assert.equal(call.tool, 'run_command')
  assert.equal(call.args.length, 3)
  assert.equal(call.args[0].value, 'pytest -q')
  assert.equal(call.args[1].value, '180')
  assert.equal(call.args[2].value, 'a, b (c)')
  const summary = paramsSummary(call)
  assert.ok(summary.includes('timeout=180'))
})

test('多行参数按行拆（工具的真实约定：一行一个 key: value）', () => {
  const call = parseCall('run_command(cmd: python main.py\nbackground: true)')
  assert.equal(call.args.length, 2)
  assert.equal(call.args[0].key, 'cmd')
  assert.equal(call.args[0].value, 'python main.py')
  assert.equal(call.args[1].key, 'background')
  assert.equal(call.args[1].value, 'true')
})

test('多参数时摘要只留前三个，其余折成 +N', () => {
  const call = parseCall('dev_mcp_call(server: a, tool: b, args: c, extra: d)')
  const summary = paramsSummary(call)
  assert.ok(summary.includes('+1'))
  assert.ok(!summary.includes('extra='))
})

test('未收录的工具用工具名本身，不编一个名字', () => {
  assert.equal(toolTitleOf('some_unknown_tool'), 'some_unknown_tool')
  const cards = cardsOf({
    status: 'done',
    trace: [
      t('action', 'some_unknown_tool(x: 1)'),
      t('observation', 'ok', { ok: true }),
    ],
  })
  assert.equal(cards[0].title, 'some_unknown_tool')
})

test('概览：工具步数与失败步数', () => {
  const cards = cardsOf({
    status: 'done',
    trace: [
      t('action', 'read_file(path: a.py)'),
      t('observation', 'ok', { ok: true }),
      t('action', 'run_command(cmd: x)'),
      t('observation', 'boom', { ok: false }),
    ],
  })
  assert.equal(cardsHeadline(cards), '2 步工具调用 · 1 步失败')
})
