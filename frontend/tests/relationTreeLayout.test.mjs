import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildForest,
  computeDepths,
  layoutTree,
  DEFAULT_TREE_OPTIONS,
  ORPHAN_GROUP_ID,
  groupIdOf,
} from '../src/workbench/lib/relationTreeLayout.ts'

/**
 * 关系图分层树布局的几何门禁。
 *
 * ## 为什么这些断言是"几何"而不是"快照"
 *
 * 布局引擎是纯函数，输出坐标。这里锁的是**关系图必须成立的不变量**：
 *   - 子类必须在基类右边（列 = 关系深度），否则"谁继承谁"读不出来
 *   - 同一层的节点不能重叠（否则卡片叠在一起，等于没布局）
 *   - 大扇出必须折叠（否则 791 个子类把画布撑到 7 万像素）
 *   - 成环不能死循环（Python 的 `class A(B)` / `class B(A)` 真会出现）
 *
 * 每条断言都对应一次真实踩过的坑，不是为了凑覆盖率。
 */

/** 造一棵 n 个子类的扇出树。 */
function fanTree(base, childCount) {
  const nodes = [{ id: base, label: base }]
  const edges = []
  for (let i = 0; i < childCount; i++) {
    const id = 'child' + i
    nodes.push({ id, label: id })
    edges.push({ source: id, target: base, kind: 'inherits' })
  }
  return { nodes, edges }
}

test('子类排在基类右边：列 = 关系深度', () => {
  const { nodes, edges } = fanTree('Base', 3)
  const forest = buildForest(nodes, edges)
  const depths = computeDepths(forest)

  assert.equal(depths.get('Base'), 0, '基类深度应为 0')
  assert.equal(depths.get('child0'), 1, '直接子类深度应为 1')

  const res = layoutTree(nodes, forest)
  const base = res.placements.find((p) => p.id === 'Base')
  const child = res.placements.find((p) => p.id === 'child0')
  assert.ok(
    child.x > base.x,
    '子类必须在基类右侧（base.x=' + base.x + ' child.x=' + child.x + '）',
  )
})

test('多层继承逐层右移一列', () => {
  const nodes = [
    { id: 'A', label: 'A' },
    { id: 'B', label: 'B' },
    { id: 'C', label: 'C' },
  ]
  const edges = [
    { source: 'B', target: 'A', kind: 'inherits' },
    { source: 'C', target: 'B', kind: 'inherits' },
  ]
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)
  const px = (id) => res.placements.find((p) => p.id === id).x
  assert.ok(px('B') > px('A'), 'B 应在 A 右边')
  assert.ok(px('C') > px('B'), 'C 应在 B 右边')
})

test('同层节点不重叠：卡片间距 >= rowGap', () => {
  const { nodes, edges } = fanTree('Base', 6)
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)
  const opt = DEFAULT_TREE_OPTIONS

  const byCol = new Map()
  for (const p of res.placements) {
    if (p.absX !== undefined) continue
    const col = Math.round(p.x / (opt.nodeWidth + opt.colGap))
    const arr = byCol.get(col) || []
    arr.push(p.y)
    byCol.set(col, arr)
  }
  for (const [col, ys] of byCol) {
    ys.sort((a, b) => a - b)
    for (let i = 1; i < ys.length; i++) {
      assert.ok(
        ys[i] - ys[i - 1] >= opt.nodeHeight + opt.rowGap - 0.001,
        '列 ' + col + ' 的第 ' + i + ' 个节点与上一个重叠（' + ys[i - 1] + ' → ' + ys[i] + '）',
      )
    }
  }
})

test('大扇出折叠成分组卡：791 个子类不撑爆画布', () => {
  const { nodes, edges } = fanTree('TestCase', 791)
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)

  const groups = res.placements.filter((p) => p.kind === 'group')
  assert.equal(groups.length, 1, '应恰好有一张分组卡')
  assert.equal(groups[0].groupSize, 791, '分组卡应记录 791 个子类')
  assert.equal(groups[0].groupBase, 'TestCase', '分组卡应指回基类')
  assert.equal(groups[0].id, groupIdOf('TestCase'), '分组卡 id 应可反查基类')
  assert.equal(groups[0].members.length, 791, '分组卡应带着成员清单')

  const opt = DEFAULT_TREE_OPTIONS
  assert.ok(res.height < 500, '折叠后画布高度应很小，实际 ' + res.height)
  assert.ok(
    res.height < 791 * (opt.nodeHeight + opt.rowGap),
    '折叠后高度必须远小于不折叠',
  )
})

test('小组不折叠：子类数不超过阈值时逐个画出', () => {
  const { nodes, edges } = fanTree('Base', 4)
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)

  assert.equal(
    res.placements.filter((p) => p.kind === 'group').length,
    0,
    '4 个子类不该被折叠',
  )
  assert.equal(res.placements.filter((p) => p.kind === 'node').length, 5, '基类 + 4 子类')
})

test('折叠阈值可调：阈值调大就不折叠了', () => {
  const { nodes, edges } = fanTree('Base', 10)
  const forest = buildForest(nodes, edges)
  const folded = layoutTree(nodes, forest, { foldThreshold: 8 })
  const unfolded = layoutTree(nodes, forest, { foldThreshold: 20 })

  assert.equal(folded.placements.filter((p) => p.kind === 'group').length, 1)
  assert.equal(unfolded.placements.filter((p) => p.kind === 'group').length, 0)
})

test('展开分组卡：组内成员重新摊开成真实节点', () => {
  const { nodes, edges } = fanTree('TestCase', 20)
  const forest = buildForest(nodes, edges)

  const folded = layoutTree(nodes, forest)
  assert.equal(folded.placements.filter((p) => p.kind === 'group').length, 1)
  assert.equal(
    folded.placements.find((p) => p.id === 'child0'),
    undefined,
    '折叠时成员不该出现在画布上',
  )

  const opened = layoutTree(nodes, forest, { expandedGroups: [groupIdOf('TestCase')] })
  assert.equal(
    opened.placements.filter((p) => p.kind === 'group').length,
    0,
    '展开后不该再有分组卡',
  )
  assert.equal(
    opened.placements.filter((p) => p.kind === 'node').length,
    21,
    '展开后 20 个成员 + 基类都该在画布上',
  )
})

test('成环不炸：class A(B) / class B(A) 也能出图', () => {
  const nodes = [
    { id: 'A', label: 'A' },
    { id: 'B', label: 'B' },
  ]
  const edges = [
    { source: 'A', target: 'B', kind: 'inherits' },
    { source: 'B', target: 'A', kind: 'inherits' },
  ]
  const forest = buildForest(nodes, edges)
  assert.ok(forest.cycleEdges > 0, '应识别出成环边')

  const res = layoutTree(nodes, forest)
  assert.ok(res.placements.length > 0, '成环也要能出图')
  for (const p of res.placements) {
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y), '坐标不能是 NaN')
  }
})

test('零关系节点不进树，但被计数并收成一张卡', () => {
  const nodes = [
    { id: 'Base', label: 'Base' },
    { id: 'child', label: 'child' },
    { id: 'lonely', label: 'lonely' },
  ]
  const edges = [{ source: 'child', target: 'Base', kind: 'inherits' }]
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)

  assert.equal(res.orphanCount, 1, 'lonely 应算零关系节点')
  assert.equal(
    res.placements.find((p) => p.id === 'lonely'),
    undefined,
    '零关系节点不该占树里的位置',
  )
  const orphanCard = res.placements.find((p) => p.id === ORPHAN_GROUP_ID)
  assert.ok(orphanCard, '零关系节点应收成一张分组卡')
  assert.equal(orphanCard.groupSize, 1)
  assert.deepEqual(orphanCard.members, ['lonely'])
})

test('零关系节点展开成网格，排在树下方且不重叠', () => {
  const nodes = [
    { id: 'Base', label: 'Base' },
    { id: 'child', label: 'child' },
    { id: 'o1', label: 'o1' },
    { id: 'o2', label: 'o2' },
    { id: 'o3', label: 'o3' },
  ]
  const edges = [{ source: 'child', target: 'Base', kind: 'inherits' }]
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest, {
    expandedGroups: [ORPHAN_GROUP_ID],
    gridCols: 2,
  })

  assert.equal(
    res.placements.find((p) => p.id === ORPHAN_GROUP_ID),
    undefined,
    '展开后不该再有零关系分组卡',
  )
  const tree = res.placements.find((p) => p.id === 'Base')
  const o1 = res.placements.find((p) => p.id === 'o1')
  const o2 = res.placements.find((p) => p.id === 'o2')
  const o3 = res.placements.find((p) => p.id === 'o3')

  assert.ok(o1.y > tree.y, '零关系网格应排在树下方')
  assert.notEqual(o1.x, o2.x, '同一行的网格项应横向分开')
  assert.ok(o3.y > o1.y, '换行后的网格项应往下排')
})

test('多继承：第一个基类是主父，其余记为额外基类', () => {
  const nodes = [
    { id: 'A', label: 'A' },
    { id: 'B', label: 'B' },
    { id: 'C', label: 'C' },
  ]
  const edges = [
    { source: 'C', target: 'A', kind: 'inherits' },
    { source: 'C', target: 'B', kind: 'inherits' },
  ]
  const forest = buildForest(nodes, edges)
  assert.equal(forest.parentOf.get('C'), 'A', '主父取第一个')
  const extra = forest.extraParents.get('C')
  assert.equal(extra.length, 1, '第二个基类记为额外基类')
  assert.equal(extra[0].parent, 'B', '额外基类应指向第二个基类')
  assert.equal(extra[0].kind, 'inherits', '额外基类要记住边的种类')
})

test('边优先级：继承 > 挂载 > 调用', () => {
  const nodes = [
    { id: 'A', label: 'A' },
    { id: 'B', label: 'B' },
    { id: 'C', label: 'C' },
  ]
  // C 既被 B 调用（calls: source=B target=C），又继承 A
  const edges = [
    { source: 'B', target: 'C', kind: 'calls' },
    { source: 'C', target: 'A', kind: 'inherits' },
  ]
  const forest = buildForest(nodes, edges)
  assert.equal(forest.parentOf.get('C'), 'A', '继承优先于调用当主父')
  const extra = forest.extraParents.get('C')
  assert.equal(extra.length, 1, '调用边降级成交叉连线')
  assert.equal(extra[0].parent, 'B', '交叉连线指向调用方')
})

test('调用边方向：调用方是父，被调方是子', () => {
  const nodes = [
    { id: 'caller', label: 'caller' },
    { id: 'callee', label: 'callee' },
  ]
  const edges = [{ source: 'caller', target: 'callee', kind: 'calls' }]
  const forest = buildForest(nodes, edges)
  assert.equal(forest.parentOf.get('callee'), 'caller', '调用方应是被调方的父')
  assert.equal(forest.parentOf.get('caller'), undefined)
})

test('非继承边也参与建树（否则调用关系整批消失）', () => {
  const nodes = [
    { id: 'A', label: 'A' },
    { id: 'B', label: 'B' },
  ]
  const edges = [{ source: 'A', target: 'B', kind: 'calls' }]
  const forest = buildForest(nodes, edges)
  assert.equal(forest.members.size, 2, '调用边也要产生关系')
  assert.equal(forest.roots.length, 1)
})

test('布局不修改入参（纯函数）', () => {
  const { nodes, edges } = fanTree('Base', 5)
  const nodesCopy = JSON.parse(JSON.stringify(nodes))
  const edgesCopy = JSON.parse(JSON.stringify(edges))
  const forest = buildForest(nodes, edges)
  layoutTree(nodes, forest)
  assert.deepEqual(nodes, nodesCopy, 'nodes 不该被改')
  assert.deepEqual(edges, edgesCopy, 'edges 不该被改')
})

test('画布尺寸能覆盖所有节点', () => {
  const { nodes, edges } = fanTree('Base', 5)
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)
  const opt = DEFAULT_TREE_OPTIONS
  for (const p of res.placements) {
    const w = p.kind === 'group' ? opt.groupWidth : opt.nodeWidth
    const h = p.kind === 'group' ? opt.groupHeight : opt.nodeHeight
    assert.ok(p.x + w <= res.width + 0.001, p.id + ' 超出画布宽度')
    assert.ok(p.y + h <= res.height + 0.001, p.id + ' 超出画布高度')
  }
})

test('子节点在父节点的垂直范围内（子树连续，不交叉）', () => {
  const nodes = [
    { id: 'Root', label: 'Root' },
    { id: 'a1', label: 'a1' },
    { id: 'a2', label: 'a2' },
    { id: 'b1', label: 'b1' },
    { id: 'b2', label: 'b2' },
  ]
  const edges = [
    { source: 'a1', target: 'Root', kind: 'inherits' },
    { source: 'a2', target: 'a1', kind: 'inherits' },
    { source: 'b1', target: 'Root', kind: 'inherits' },
    { source: 'b2', target: 'b1', kind: 'inherits' },
  ]
  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)
  const y = (id) => res.placements.find((p) => p.id === id).y

  assert.ok(y('a2') < y('b1'), '第一棵子树应完全排在第二棵子树之上')
  assert.ok(y('a2') >= y('a1'), '孙节点不该跑到父节点上方')
})

test('实测形状：1485 节点 / 1245 继承边能压进可读画布', () => {
  // 复刻 Agent 工作台 真实数据的关键形状：
  // 一个 791 子类的巨扇出 + 一个 121 子类的扇出 + 一批小组 + 若干零关系节点。
  const nodes = []
  const edges = []
  const push = (id, label) => nodes.push({ id, label })
  push('TestCase', 'TestCase')
  for (let i = 0; i < 791; i++) {
    push('tc' + i, 'tc' + i)
    edges.push({ source: 'tc' + i, target: 'TestCase', kind: 'inherits' })
  }
  push('BaseModel', 'BaseModel')
  for (let i = 0; i < 121; i++) {
    push('bm' + i, 'bm' + i)
    edges.push({ source: 'bm' + i, target: 'BaseModel', kind: 'inherits' })
  }
  for (let i = 0; i < 40; i++) {
    push('lone' + i, 'lone' + i)
  }

  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)

  assert.equal(res.orphanCount, 40, '40 个零关系节点')
  assert.ok(res.height < 2000, '画布高度必须可读，实际 ' + res.height)
  assert.ok(res.width < 2000, '画布宽度必须可读，实际 ' + res.width)
  assert.ok(res.foldedCount >= 900, '大扇出应被折叠，实际折叠 ' + res.foldedCount)
})

// ---------------------------------------------------------------------------
// 下面两条是实机踩出来的 bug，各自都有专门的回归断言
// ---------------------------------------------------------------------------

test('坐标永远有限：父节点行号是小数（子区间中点）也不能算出 NaN', () => {
  // 回归：父节点的 row 是 (firstRow + lastRow) / 2，可能是 36.5 这种小数。
  // 早期实现用 rowY[row] 直接索引整数数组 → undefined → 整张图高度变 NaN。
  const nodes = []
  const edges = []
  // 造一棵深度不均的树，让父节点落在两行之间
  for (let i = 0; i < 20; i++) {
    nodes.push({ id: 'a' + i, label: 'a' + i })
    edges.push({ source: 'a' + i, target: 'root', kind: 'inherits' })
  }
  nodes.push({ id: 'root', label: 'root' })
  edges.push({ source: 'b', target: 'root', kind: 'inherits' })
  nodes.push({ id: 'b', label: 'b' })
  edges.push({ source: 'c', target: 'b', kind: 'inherits' })
  nodes.push({ id: 'c', label: 'c' })

  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)
  for (const p of res.placements) {
    assert.ok(
      Number.isFinite(p.x) && Number.isFinite(p.y),
      p.id + ' 坐标不是有限数（x=' + p.x + ' y=' + p.y + '）',
    )
  }
  assert.ok(Number.isFinite(res.height) && res.height > 0, '画布高度必须是正有限数')
  assert.ok(Number.isFinite(res.width) && res.width > 0, '画布宽度必须是正有限数')
})

test('折叠的基类不产生悬空分组卡：整枝剪掉', () => {
  // 回归：Base 有 20 个子类（> 阈值 8 → 折成一张卡），
  // 而其中某个子类自己又有 20 个子类。
  // 早期实现各算各的 → Base 的子树里浮出一张没有可见父节点的分组卡。
  const nodes = [{ id: 'Base', label: 'Base' }]
  const edges = []
  for (let i = 0; i < 20; i++) {
    nodes.push({ id: 'mid' + i, label: 'mid' + i })
    edges.push({ source: 'mid' + i, target: 'Base', kind: 'inherits' })
  }
  // mid0 自己也是个大扇出
  for (let i = 0; i < 20; i++) {
    nodes.push({ id: 'leaf' + i, label: 'leaf' + i })
    edges.push({ source: 'leaf' + i, target: 'mid0', kind: 'inherits' })
  }

  const forest = buildForest(nodes, edges)
  const res = layoutTree(nodes, forest)

  const groups = res.placements.filter((p) => p.kind === 'group')
  assert.equal(groups.length, 1, 'Base 折成一张卡后，mid0 的卡不该再出现')
  assert.equal(groups[0].groupBase, 'Base')

  // 每张分组卡都必须有可见的父节点连着
  const placed = new Set(res.placements.map((p) => p.id))
  for (const g of groups) {
    const connected = res.edges.some((e) => e.to === g.id && placed.has(e.from))
    assert.ok(connected, '分组卡 ' + g.id + ' 没有可见父节点（悬空卡）')
  }
  // 被折掉的节点不该出现在画布上
  for (const mid of ['mid0', 'mid1']) {
    assert.equal(
      res.placements.find((p) => p.id === mid),
      undefined,
      mid + ' 被折进分组卡了，不该单独出现在画布上',
    )
  }
})
