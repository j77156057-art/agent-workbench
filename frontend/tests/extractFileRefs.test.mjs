/**
 * `extractFileRefs` 的**行为守卫**：钉住"文件引用提取"的两类边界。
 *
 * 修的是钻测现场坐实的引用噪声（X：`_flash.color` 被当成 C 源码文件 `_flash.c`）：
 * `REF_TOKEN` 早先没有尾部边界，扩展名只要是更长单词的前缀就会误命中——
 * `_flash.c`olor、`file.js`on 都被抠成"文件"。加 `(?![A-Za-z0-9_])` 后，
 * 扩展名后面紧跟字母/数字/下划线就不算文件。
 *
 * 同时锁住**别改坏正当用法**：`a/b.gd:12`、`a/b.gd 第 12 行`、`res://` 前缀、
 * 句末点号、去重，都得照常。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { extractFileRefs } from '../src/workbench/markdown.ts'

test('点号片段不再被当文件：_flash.color / take_damage.foo 这类成员访问', () => {
  const paths = extractFileRefs('设 `_flash.color` 为红，并调用 `behaviors.hit.power` 取值。')
  // 这些都不是文件：既不能冒出 `_flash.c`，也不能冒出 `.foo` 截断
  assert.deepEqual(paths.map((r) => r.path), [])
})

test('json 不被截成 js：完整扩展名才算', () => {
  const paths = extractFileRefs('读 `project.json` 与 `player.js` 两份配置。')
  const got = paths.map((r) => r.path).sort()
  assert.deepEqual(got, ['player.js', 'project.json'])
})

test('正当引用不受影响：路径+行号、中文行号、res://、句末点号', () => {
  const refs = extractFileRefs(
    '见 `scenes/player.gd:12`，另见 `scripts/enemy.gd 第 20 行`，' +
    '以及 res://ui/hud.gd。整句结束就一个点：hud.gd.')
  const keys = refs.map((r) => `${r.path}:${r.line}`)
  assert.ok(keys.includes('scenes/player.gd:12'), keys.join('|'))
  assert.ok(keys.includes('scripts/enemy.gd:20'), keys.join('|'))
  assert.ok(keys.includes('ui/hud.gd:0'), keys.join('|'))
  // `hud.gd.`（句末点号）仍是同一个 hud.gd，不该因后面那个点而丢掉
  assert.ok(keys.filter((k) => k.startsWith('ui/hud.gd:')).length >= 1, keys.join('|'))
})

test('按 path+line 去重，重复引用只留一条', () => {
  const refs = extractFileRefs('`player.gd:5` 又 `player.gd:5` 还 `player.gd:5`')
  assert.equal(refs.length, 1)
  assert.equal(refs[0].line, 5)
})

test('v/pr/issue 号前缀照旧被过滤（版本号不是文件）', () => {
  const paths = extractFileRefs('对齐 v2.gd 与 issue3.py 的说法')
  assert.deepEqual(paths.map((r) => r.path), [])
})

test('常见行号写法都要认：:12 / 第12行 / #L12 / :L12-L20 / (12 行)', () => {
  const cases = [
    ['见 `a.py:12`。', 12],
    ['见 `a.py 第 12 行`。', 12],
    ['见 `a.py#L12`。', 12],
    ['见 `a.py:L12`。', 12],
    ['见 `a.py:L12-L20`。', 12],
    ['见 a.py (12 行)。', 12],
    ['见 a.py（第 12 行）。', 12],
  ]
  for (const [text, line] of cases) {
    const refs = extractFileRefs(text)
    assert.deepEqual(refs, [{ path: 'a.py', line }], text)
  }
  // 脚注式 `(1)` 不带「行」，不能被当成行号
  assert.deepEqual(extractFileRefs('见 a.py (1)。'), [{ path: 'a.py', line: 0 }])
})

test('同一文件的无行号引用被带行号引用吸收，不同行号仍各自保留', () => {
  // 先不带行号、后带行号：只留一条，位置停在首次出现处
  assert.deepEqual(extractFileRefs('先看 `a.py`，再看 `a.py:130`。'), [{ path: 'a.py', line: 130 }])
  // 反过来也一样
  assert.deepEqual(extractFileRefs('先看 `a.py:130`，再说 `a.py`。'), [{ path: 'a.py', line: 130 }])
  // 两个不同行号指向不同位置，不合并
  assert.deepEqual(
    extractFileRefs('`a.py:10` 与 `a.py:40`'),
    [{ path: 'a.py', line: 10 }, { path: 'a.py', line: 40 }],
  )
  // 只有无行号时保持原样
  assert.deepEqual(extractFileRefs('`a.py`'), [{ path: 'a.py', line: 0 }])
})

test('绝对路径：按项目根剥成相对路径，剥不掉的保留原样交给后端判否', () => {
  // 用中性的示例根，不把本机工作区路径写进仓库（隐私门也会拦）
  const root = 'D:\\proj\\demo'
  assert.deepEqual(
    extractFileRefs('见 D:/proj/demo/agent_trace.py:130。', root),
    [{ path: 'agent_trace.py', line: 130 }],
  )
  assert.deepEqual(
    extractFileRefs('见 d:/PROJ/DEMO/tests/a.py#L7。', root),
    [{ path: 'tests/a.py', line: 7 }],
  )
  assert.deepEqual(
    extractFileRefs('见 D:/other/proj/a.py:3。', root),
    [{ path: 'other/proj/a.py', line: 3 }],
  )
})
