/**
 * 对话输出要像「发言 → 该段工具摘要 → 发言 → 摘要」，而不是把整轮推理压成一块。
 * 这里钉住切段口径：连续思考并段、动作/观察归到当前段、下一次发言开新段、
 * 复核算发言、动过文件的段用 ✎ 图标。两个界面（工作台 / 问答页）共用这份实现。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { traceSegments } from '../src/workbench/composables/traceDigest.ts'

function t(type, text) { return { type, text, at: 1 } }

test('发言夹着摘要：两轮思考各自带一段工具', () => {
  const trace = [
    t('thought', '先读 README'),
    t('action', 'read_file(path: README.md)'),
    t('observation', '文件不存在'),
    t('thought', '改用 grep 定位'),
    t('action', 'apply_edit(path: a.py)'),
    t('observation', '已写入'),
  ]
  const segs = traceSegments({ trace })
  assert.equal(segs.length, 2)
  assert.deepEqual(segs.map((s) => s.narration), [['先读 README'], ['改用 grep 定位']])
  assert.equal(segs[0].digest.text, '读取 1 个文件 · 思考 1 次')
  assert.equal(segs[0].glyph, '◷')
  assert.deepEqual(segs[0].rows, [1, 2])
  assert.equal(segs[1].digest.text, '编辑 1 个文件 · 思考 1 次')
  assert.equal(segs[1].glyph, '✎')
  assert.deepEqual(segs[1].rows, [4, 5])
})

test('reasoning 分段按动作切段，最后一跳只有发言没有工具', () => {
  const trace = [
    t('action', 'read_file(path: a.py)'),
    t('observation', 'ok'),
    t('action', 'grep(pattern: x)'),
    t('observation', 'done'),
  ]
  const flow = [
    { kind: 'think', text: '第一段思考' },
    { kind: 'action', trace: 0 },
    { kind: 'result', trace: 1 },
    { kind: 'think', text: '第二段思考' },
    { kind: 'action', trace: 2 },
    { kind: 'result', trace: 3 },
    { kind: 'think', text: '收尾思考' },
  ]
  const segs = traceSegments({ trace, flow })
  assert.equal(segs.length, 3)
  assert.deepEqual(segs.map((s) => s.narration), [['第一段思考'], ['第二段思考'], ['收尾思考']])
  assert.deepEqual(segs[0].rows, [0, 1])
  assert.deepEqual(segs[1].rows, [2, 3])
  assert.equal(segs[2].rows.length, 0)
  assert.equal(segs[2].digest.toolTotal, 0)
})

test('连续思考并进同一段，不造空摘要', () => {
  const trace = [
    t('thought', 'ReAct 思考'),
    t('action', 'run_command(cmd: pytest)'),
    t('observation', 'ok'),
  ]
  const flow = [
    { kind: 'think', text: 'reasoning 段' },
    { kind: 'thought', trace: 0 },
    { kind: 'action', trace: 1 },
    { kind: 'result', trace: 2 },
  ]
  const segs = traceSegments({ trace, flow })
  assert.equal(segs.length, 1)
  assert.deepEqual(segs[0].narration, ['reasoning 段', 'ReAct 思考'])
  assert.equal(segs[0].digest.text, '执行 1 条命令 · 思考 2 次')
})

test('复核当发言，不作为工具返回行', () => {
  const trace = [
    t('thought', '先看一眼'),
    t('action', 'read_file(path: a.py)'),
    t('observation', 'ok'),
    t('reflection', '结果看起来对不上，需要再核对'),
  ]
  const segs = traceSegments({ trace })
  assert.equal(segs.length, 2)
  assert.deepEqual(segs[1].narration, ['结果看起来对不上，需要再核对'])
  assert.equal(segs[1].rows.length, 0)
})
