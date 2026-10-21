import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  isImageArtifact,
  artifactRawUrl,
  artifactShortName,
  artifactCaption,
  artifactSizeLabel,
  artifactDiffLabel,
  visibleArtifacts,
} from '../src/workbench/lib/artifactView.ts'
import { useChatStream } from '../src/workbench/composables/useChatStream.ts'

/**
 * 产物（模型生成的文件 / 截取的图）在对话栏的展示门禁。
 *
 * ## 为什么这些断言是"判据"而不是"快照"
 *
 * 后端 observation 事件早就带 `artifacts`（工具落盘那一刻自报），
 * 但前端此前完全没用 —— 于是产物只躺在磁盘上，用户得自己去路径里搜。
 * 这个门禁锁的是"接上之后不许再退化"的几件事：
 *   - 判定图片/文件不能判错（判错 = 缩略图变文件名，用户还是得自己找）
 *   - 预览地址不能拼错，也不能拼出可点的 javascript: 之类
 *   - 流式管线必须真的把 artifacts 收进回合（收不到 = 界面永远空）
 */

// ---------------------------------------------------------------------------
// 判定：是不是图片
// ---------------------------------------------------------------------------

test('kind=image 判为图片', () => {
  assert.equal(isImageArtifact({ kind: 'image', path: '.docmind/screenshots/a.jpg' }), true)
})

test('kind 缺失时按扩展名兜底（create_file/apply_edit 那批不带 kind）', () => {
  assert.equal(isImageArtifact({ path: 'shots/a.png' }), true)
  assert.equal(isImageArtifact({ path: 'shots/a.JPEG' }), true)
  assert.equal(isImageArtifact({ path: 'shots/a.webp' }), true)
})

test('代码/文本/diff 明确不是图片（哪怕扩展名像）', () => {
  assert.equal(isImageArtifact({ kind: 'code', path: 'a/b.py' }), false)
  assert.equal(isImageArtifact({ kind: 'text', path: 'a/b.txt' }), false)
  assert.equal(isImageArtifact({ kind: 'diff', path: 'a/b.png' }), false)
})

test('没有路径也没有 kind 时不判成图片', () => {
  assert.equal(isImageArtifact({ label: '某个产物' }), false)
})

// ---------------------------------------------------------------------------
// 预览地址
// ---------------------------------------------------------------------------

test('项目相对路径走 /api/assets/raw', () => {
  const url = artifactRawUrl({ path: '.docmind/screenshots/a b.jpg' })
  assert.ok(url.startsWith('/api/assets/raw?path='), '应走素材直链，实际 ' + url)
  assert.ok(url.includes(encodeURIComponent('.docmind/screenshots/a b.jpg')), '路径要编码')
})

test('http(s) 的 uri 直接用', () => {
  assert.equal(artifactRawUrl({ uri: 'https://x.test/a.png' }), 'https://x.test/a.png')
  assert.equal(artifactRawUrl({ uri: 'http://x.test/a.png' }), 'http://x.test/a.png')
})

test('危险协议的 uri 不采纳（只认 http/https）', () => {
  // 前端再兜一层：后端 _safe_uri 已滤过，但前端不能只靠后端的口径
  for (const uri of ['javascript:alert(1)', 'data:text/html,<script>', 'file:///etc/passwd']) {
    assert.equal(artifactRawUrl({ uri }), '', '不该采纳 ' + uri)
  }
})

test('既没 uri 也没 path 时没有预览地址', () => {
  assert.equal(artifactRawUrl({ label: '产物' }), '')
})

// ---------------------------------------------------------------------------
// 文案
// ---------------------------------------------------------------------------

test('短名取文件名（去掉目录）', () => {
  assert.equal(artifactShortName({ path: 'a/b/c.py' }), 'c.py')
  assert.equal(artifactShortName({ path: 'a\\b\\c.py' }), 'c.py')
  assert.equal(artifactShortName({ label: '游戏运行画面' }), '游戏运行画面')
})

test('说明优先用后端给的 summary', () => {
  assert.equal(artifactCaption({ summary: '新建 a.py（12 字节）' }), '新建 a.py（12 字节）')
})

test('没有 summary 时退回 diff 行数 / 文件大小', () => {
  assert.equal(artifactCaption({ change_summary: { added_lines: 3, removed_lines: 1 } }), '+3 / -1 行')
  assert.equal(artifactCaption({ bytes: 2048 }), '2.0 KB')
  assert.equal(artifactCaption({}), '')
})

test('大小与 diff 文案', () => {
  assert.equal(artifactSizeLabel({ bytes: 512 }), '512 B')
  assert.equal(artifactSizeLabel({ bytes: 1536 }), '1.5 KB')
  assert.equal(artifactSizeLabel({ bytes: 3 * 1024 * 1024 }), '3.0 MB')
  assert.equal(artifactSizeLabel({}), '')
  assert.equal(artifactDiffLabel({}), '')
})

test('空标识的产物不显示', () => {
  assert.deepEqual(visibleArtifacts([{}, { path: 'a.py' }]), [{ path: 'a.py' }])
  assert.deepEqual(visibleArtifacts(undefined), [])
})

// ---------------------------------------------------------------------------
// 流式管线：observation 事件里的产物必须真的进回合
// ---------------------------------------------------------------------------

const newTurn = () => ({ id: 7, text: '', reasoning: '', notices: [], trace: [], plan: [] })

/** 按顺序喂事件，返回最终回合（useChatStream 的 turn 由调用方持有）。 */
function feed(evs) {
  const turn = newTurn()
  const { dispatch } = useChatStream({ onFinal: (t, text) => { t.text = text } })
  for (const ev of evs) dispatch(ev, turn)
  return turn
}

test('observation 事件的 artifacts 收进回合', () => {
  const t = feed([{
    type: 'observation',
    text: '已创建 a.py',
    artifacts: [{ path: 'a.py', label: 'a.py', summary: '新建 a.py' }],
  }])

  assert.equal(t.artifacts.length, 1, '产物应被收进回合')
  assert.equal(t.artifacts[0].path, 'a.py')
})

test('同一路径重复报只留一条（要的是"一共产出了什么"，不是"报了几次"）', () => {
  const art = [{ path: 'a.py', label: 'a.py' }]
  const t = feed([
    { type: 'observation', text: '第一次', artifacts: art },
    { type: 'observation', text: '第二次', artifacts: art },
  ])

  assert.equal(t.artifacts.length, 1, '重复路径应去重')
})

test('不同路径都留下（一次改多个文件就是多个产物）', () => {
  const t = feed([{
    type: 'observation',
    text: '已原子写入 2 个文件',
    artifacts: [{ path: 'a.py' }, { path: 'b.py' }],
  }])
  assert.equal(t.artifacts.length, 2)
})

test('非 observation 事件不登记产物', () => {
  // action 文本里也可能带 path，但**不能**从 action 登记：
  // 被安全拦截的调用同样带 path —— 照 action 反推会把没做的记成做了。
  const t = feed([{ type: 'action', text: 'apply_edit(path: a.py)', artifacts: [{ path: 'a.py' }] }])
  assert.equal(t.artifacts, undefined, 'action 不该登记产物')
})
