/**
 * 关系图：继承树布局引擎（纯函数，无 DOM 依赖）。
 *
 * ## 为什么不用力导向
 *
 * 实测 Agent 工作台 自己的项目（`build_relation_graph` 全量）：
 * 1485 节点 / 1245 继承边 / 8 调用边 / 0 挂载边 —— 继承边占 99.4%。
 * 而且这些边本来是**树**（继承森林），不是网。
 * 用力导向画一棵树的结果是：1245 条弹簧互相拉扯，节点挤成一团毛球，
 * 看不出"谁继承谁"，也看不出哪块代码是核心。
 *
 * ## 这个模块做什么
 *
 * 把关系图画成**左→右的分层 tidy tree**：
 *   - 列 = 关系深度（基类在左，子类在右）
 *   - 行 = 兄弟顺序（tidy tree 排布，子树连续不交叉）
 *   - 大扇出折叠：子类数超过阈值的基类收成一张「N 个子类」分组卡，
 *     点开才展开 —— 否则 `unittest.TestCase` 的 791 个子类会把画布撑到 7 万像素高
 *   - 零关系节点（谁也不连、也没人连它）单独成一组，不混进树里
 *
 * ## 折叠的可见性规则（为什么必须自顶向下）
 *
 * 一个基类被折进分组卡时，它的**整棵子树**都看不见了 ——
 * 包括它自己那些"也很大扇出"的子树。所以"哪些分组卡存在"不能各算各的：
 * 必须从根往下走，遇到被折叠的基类就整枝剪掉，不再往下展开。
 * 否则会出现"悬空分组卡"：卡片在图上，却没有可见的父节点连着它。
 *
 * ## 边的优先级
 *
 * 一个节点可能同时有多种"父"（比如既被继承、又被场景挂载、又被调用）。
 * 树只有一根主父，按 `inherits > mounts > calls` 取第一个当主父，
 * 其余记为**交叉连线**（虚线，画在树上），这样既不丢信息、又不破坏树的形状。
 *
 * ## 纯函数
 *
 * 所有输入都是普通数据，输出是普通数据 —— 不碰 DOM、不读全局。
 * 这样才能用 node 单测直接验几何（见 `tests/relationTreeLayout.test.mjs`）。
 */

export interface RelationTreeNode {
  id: string
  label: string
}

export interface RelationTreeEdge {
  source: string
  target: string
  kind: string
  label: string
  line: number
  methods?: string[]
}

/** 布局产出：每个可见节点/分组卡的位置（以左上角为原点）。 */
export interface TreePlacement {
  /** 节点 id；分组卡用 `group:<baseId>` / `group:orphan` */
  id: string
  /** 分组卡标题（普通节点为空） */
  label: string
  /** 分组卡收纳的成员数（普通节点为 0） */
  groupSize: number
  /** 分组卡对应的基类 id（普通节点为空） */
  groupBase: string
  /** 分组卡成员（仅分组卡有值） */
  members: string[]
  x: number
  y: number
  /** 0 = 普通节点，1 = 分组卡 */
  kind: 'node' | 'group'
  /** 关系深度（列） */
  depth: number
  /** 行槽位（可能是小数 = 落在两行基线之间） */
  row: number
  /** 卡片宽（由调用方的 widthOf 决定，分组卡用固定宽） */
  width: number
  /** 卡片高 */
  height: number
  /**
   * 绝对坐标覆盖：展开的零关系节点按网格排在树下方，
   * 不走"列宽累加"那套（否则网格会被推到树的右侧，还把树撑宽）。
   */
  absX?: number
  absY?: number
}

/** 画线用的边：端点都是 placement id。 */
export interface TreeEdgeDraw {
  key: string
  /** 起点 placement id */
  from: string
  /** 终点 placement id */
  to: string
  kind: string
  label: string
  line: number
  methods?: string[]
  /** true = 交叉连线（虚线），false = 树主干 */
  cross: boolean
}

export interface TreeLayoutResult {
  placements: TreePlacement[]
  /** 要画的边（树主干 + 交叉连线） */
  edges: TreeEdgeDraw[]
  /** 当前处于展开状态的分组卡对应的基类 id（用来在基类上画"折叠"角标） */
  expandedBases: string[]
  /** 被折叠掉的成员数（不含分组卡自身） */
  foldedCount: number
  /** 零关系节点数量 */
  orphanCount: number
  /** 参与树的根数量 */
  rootCount: number
  /** 继承边里构成环的数量（被忽略） */
  cycleEdges: number
  /** 画布尺寸（未含边距） */
  width: number
  height: number
}

export interface TreeLayoutOptions {
  /** 子类数超过这个值就折叠成分组卡 */
  foldThreshold: number
  /** 强制展开的分组卡 id（`group:<base>` / `group:orphan`） */
  expandedGroups: string[]
  /** 零关系节点是否也收成分组卡 */
  foldOrphans: boolean
  /** 零关系节点展开时的网格列数 */
  gridCols: number
  /** 水平间距 */
  colGap: number
  /** 垂直间距 */
  rowGap: number
  /** 分组卡宽度 */
  groupWidth: number
  /** 分组卡高度 */
  groupHeight: number
  /** 普通节点默认宽度（没有实测宽度时用） */
  nodeWidth: number
  /** 普通节点默认高度 */
  nodeHeight: number
}

export const DEFAULT_TREE_OPTIONS: TreeLayoutOptions = {
  foldThreshold: 8,
  expandedGroups: [],
  foldOrphans: true,
  gridCols: 12,
  colGap: 40,
  rowGap: 14,
  groupWidth: 168,
  groupHeight: 34,
  nodeWidth: 150,
  nodeHeight: 34,
}

/** 孤儿分组卡的 id（零关系节点收成一张卡时用）。 */
export const ORPHAN_GROUP_ID = 'group:orphan'

/** 分组卡 id：group:<基类 id> */
export function groupIdOf(base: string): string {
  return `group:${base}`
}

/** 分组卡 id 反查基类 id；不是分组卡则返回 null。 */
export function baseOfGroup(id: string): string | null {
  if (!id.startsWith('group:')) return null
  if (id === ORPHAN_GROUP_ID) return ''
  return id.slice('group:'.length)
}

/** 边的分类优先级：继承 > 挂载 > 调用。 */
const KIND_PRIORITY = ['inherits', 'mounts', 'calls']

export interface Forest {
  /** 子节点 id → 主父 id */
  parentOf: Map<string, string>
  /** 子节点 id → 主父边的种类（继承/挂载/调用） */
  parentKindOf: Map<string, string>
  /** 子节点 id → 主父边本身（画线要用它的 label/line） */
  primaryEdgeOf: Map<string, RelationTreeEdge>
  /** 主父 id → 子节点 id 列表 */
  childrenOf: Map<string, string[]>
  /** 参与关系的所有节点 id */
  members: Set<string>
  /** 额外的父（多继承 / 又被挂载 / 又被调用）：画成交叉连线 */
  extraParents: Map<string, ExtraParent[]>
  /** 关系森林的根（没有主父的节点） */
  roots: string[]
  /** 被忽略的成环边数量 */
  cycleEdges: number
  /** 参与任何关系边的节点 */
  touched: Set<string>
}

/** 交叉连线：节点 child 除了主父，还连着 parent。 */
export interface ExtraParent {
  parent: string
  kind: string
  edge: RelationTreeEdge
}

/**
 * 建关系森林。
 *
 * 方向约定：
 *   - `inherits`（子 → 父）、`mounts`（场景 → 脚本）：source 是子，target 是父
 *   - `calls`（调用方 → 被调方）：source 是父（调用方在上），target 是子
 *
 * 成环保护：Python 的 `class A(B)` / `class B(A)` 在项目里真会出现，
 * 顺着父链上溯时若撞回自己，这条边会断掉（记进 `cycleEdges`）。
 */
export function buildForest(
  nodes: { id: string }[],
  edges: RelationTreeEdge[],
): Forest {
  const known = new Set(nodes.map((n) => n.id))
  const parentOf = new Map<string, string>()
  const parentKindOf = new Map<string, string>()
  const primaryEdgeOf = new Map<string, RelationTreeEdge>()
  const childrenOf = new Map<string, string[]>()
  const extraParents = new Map<string, ExtraParent[]>()
  const members = new Set<string>()
  const touched = new Set<string>()

  /** 待处理：按优先级排序的边 */
  const pending: { child: string; parent: string; kind: string; edge: RelationTreeEdge }[] = []
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue
    if (e.source === e.target) continue
    touched.add(e.source)
    touched.add(e.target)
    const kind = KIND_PRIORITY.includes(e.kind) ? e.kind : 'calls'
    if (kind === 'calls') {
      // 调用层级：调用方是父，被调方是子
      pending.push({ child: e.target, parent: e.source, kind, edge: e })
    } else {
      pending.push({ child: e.source, parent: e.target, kind, edge: e })
    }
  }
  pending.sort((a, b) => KIND_PRIORITY.indexOf(a.kind) - KIND_PRIORITY.indexOf(b.kind))

  for (const { child, parent, kind, edge } of pending) {
    if (parentOf.has(child)) {
      const list = extraParents.get(child) ?? []
      list.push({ parent, kind, edge })
      extraParents.set(child, list)
      members.add(parent)
      continue
    }
    parentOf.set(child, parent)
    parentKindOf.set(child, kind)
    primaryEdgeOf.set(child, edge)
    members.add(child)
    members.add(parent)
    const list = childrenOf.get(parent) ?? []
    list.push(child)
    childrenOf.set(parent, list)
  }

  // 成环保护：沿主父链上溯，撞回在栈上的节点就断这条边
  let cycleEdges = 0
  const state = new Map<string, 0 | 1 | 2>() // 0=未访问 1=在栈上 2=完成
  for (const child of [...parentOf.keys()]) {
    if (state.get(child) === 2) continue
    const path: string[] = []
    let cur: string | undefined = child
    while (cur !== undefined && state.get(cur) !== 2) {
      if (state.get(cur) === 1) {
        // 环：断开 cur 的父边
        const p = parentOf.get(cur)
        if (p !== undefined) {
          parentOf.delete(cur)
          const sibs = childrenOf.get(p)
          if (sibs) {
            const i = sibs.indexOf(cur)
            if (i >= 0) sibs.splice(i, 1)
          }
          members.delete(cur)
          cycleEdges++
        }
        break
      }
      state.set(cur, 1)
      path.push(cur)
      cur = parentOf.get(cur)
    }
    for (const p of path) state.set(p, 2)
  }

  const roots: string[] = []
  for (const id of members) {
    if (!parentOf.has(id)) roots.push(id)
  }
  roots.sort()

  return {
    parentOf,
    parentKindOf,
    primaryEdgeOf,
    childrenOf,
    members,
    extraParents,
    roots,
    cycleEdges,
    touched,
  }
}

/**
 * 关系深度：0 = 根（没有主父），子类 = 父深度 + 1。
 * 成环时按已算出的深度兜底，不会死循环。
 */
export function computeDepths(forest: Forest): Map<string, number> {
  const depth = new Map<string, number>()
  const visiting = new Set<string>()
  const walk = (id: string): number => {
    const hit = depth.get(id)
    if (hit !== undefined) return hit
    if (visiting.has(id)) return 0
    visiting.add(id)
    const parent = forest.parentOf.get(id)
    const d = parent === undefined ? 0 : walk(parent) + 1
    visiting.delete(id)
    depth.set(id, d)
    return d
  }
  for (const id of forest.members) walk(id)
  return depth
}

export interface TreeSizeFn {
  /** 节点宽度；缺省用 options.nodeWidth */
  widthOf?: (id: string) => number
  /** 节点高度；缺省用 options.nodeHeight */
  heightOf?: (id: string) => number
}

/**
 * 算分层树的坐标。
 *
 * 折叠规则：某个节点的直接子类数 > `foldThreshold` 且没被强制展开时，
 * 这批子类收成一张分组卡（占 1 个槽位），点开才展开。
 * 这样最大的那坨（`unittest.TestCase` 的 791 个子类）不会把画布撑成一条线。
 *
 * 零关系节点（不在 forest.members 里）不画进树里 —— 它们在关系图上
 * 本来就是孤立点，混进树里只会让树变形。要么收成一张卡，要么直接不画。
 */
export function layoutTree(
  nodes: { id: string; label: string }[],
  forest: Forest,
  options: Partial<TreeLayoutOptions> = {},
  sizes: TreeSizeFn = {},
): TreeLayoutResult {
  const opt = { ...DEFAULT_TREE_OPTIONS, ...options }
  const depth = computeDepths(forest)
  const labelOf = new Map(nodes.map((n) => [n.id, n.label]))
  const expanded = new Set(opt.expandedGroups)

  const widthOf = (id: string) => sizes.widthOf?.(id) ?? opt.nodeWidth
  const heightOf = (id: string) => sizes.heightOf?.(id) ?? opt.nodeHeight
  const groupW = opt.groupWidth
  const groupH = opt.groupHeight

  // ------------------------------------------------------------
  // 阶段一：自顶向下定可见性
  //
  // 被折进分组卡的节点，其整棵子树都不可见。所以必须从根往下走，
  // 遇到被折叠的基类就整枝剪掉 —— 各算各的会出现"悬空分组卡"。
  // ------------------------------------------------------------
  const visible = new Set<string>()
  const groupBases = new Set<string>() // 有分组卡的基类
  const hidden = new Set<string>() // 被折起来的节点
  const expandedBases: string[] = []

  const walk = (base: string) => {
    visible.add(base)
    const kids = forest.childrenOf.get(base) ?? []
    if (!kids.length) return
    if (kids.length > opt.foldThreshold && !expanded.has(groupIdOf(base))) {
      groupBases.add(base)
      for (const k of kids) hidden.add(k)
      return // 整枝剪掉，不再往下展开
    }
    if (kids.length > opt.foldThreshold) expandedBases.push(base)
    for (const k of kids) walk(k)
  }
  for (const root of forest.roots) {
    if (!forest.members.has(root)) continue
    walk(root)
  }

  // ------------------------------------------------------------
  // 阶段二：tidy tree 排行
  // ------------------------------------------------------------
  interface Slot {
    id: string
    kind: 'node' | 'group'
    base: string
    size: number
  }

  const childrenSlots = new Map<string, Slot[]>()
  let foldedCount = 0
  const groupMembers = new Map<string, string[]>()

  for (const base of groupBases) {
    const kids = forest.childrenOf.get(base) ?? []
    foldedCount += kids.length
    groupMembers.set(groupIdOf(base), kids)
    childrenSlots.set(base, [{ id: groupIdOf(base), kind: 'group', base, size: kids.length }])
  }
  for (const base of visible) {
    if (groupBases.has(base)) continue
    const kids = forest.childrenOf.get(base) ?? []
    if (!kids.length) continue
    childrenSlots.set(base, kids.map((k) => ({ id: k, kind: 'node' as const, base, size: 0 })))
  }

  const rowOf = new Map<string, number>()
  const place = (id: string, start: number): number => {
    const slots = childrenSlots.get(id) ?? []
    if (!slots.length) {
      rowOf.set(id, start)
      return 1
    }
    let cursor = start
    for (const s of slots) {
      if (s.kind === 'group') {
        rowOf.set(s.id, cursor)
        cursor += 1
      } else {
        cursor += place(s.id, cursor)
      }
    }
    // 父节点落在自己子树的正中
    const first = slots[0]
    const last = slots[slots.length - 1]
    rowOf.set(id, (rowOf.get(first.id)! + rowOf.get(last.id)!) / 2)
    return cursor - start
  }

  let rowCursor = 0
  for (const root of forest.roots) {
    if (!visible.has(root)) continue
    rowCursor += place(root, rowCursor)
  }

  // ------------------------------------------------------------
  // 阶段三：产出 placement
  // ------------------------------------------------------------
  const placements: TreePlacement[] = []
  for (const id of visible) {
    placements.push({
      id,
      label: '',
      groupSize: 0,
      groupBase: '',
      members: [],
      x: 0,
      y: 0,
      kind: 'node',
      depth: depth.get(id) ?? 0,
      row: rowOf.get(id) ?? 0,
      width: widthOf(id),
      height: heightOf(id),
    })
  }
  for (const base of groupBases) {
    const members = groupMembers.get(groupIdOf(base)) ?? []
    placements.push({
      id: groupIdOf(base),
      label: labelOf.get(base) ?? base,
      groupSize: members.length,
      groupBase: base,
      members,
      x: 0,
      y: 0,
      kind: 'group',
      depth: (depth.get(base) ?? 0) + 1,
      row: rowOf.get(groupIdOf(base)) ?? 0,
      width: groupW,
      height: groupH,
    })
  }

  // ---- 零关系节点：收成一张卡（可选），或干脆不画
  const orphanIds = nodes.filter((n) => !forest.members.has(n.id)).map((n) => n.id)
  if (orphanIds.length && opt.foldOrphans) {
    if (expanded.has(ORPHAN_GROUP_ID)) {
      // 展开成网格，排在树下面（绝对坐标，不参与列宽累加）
      const cols = Math.max(1, Math.min(opt.gridCols, orphanIds.length))
      orphanIds.forEach((id, i) => {
        placements.push({
          id,
          label: '',
          groupSize: 0,
          groupBase: '',
          members: [],
          x: 0,
          y: 0,
          kind: 'node',
          depth: 0,
          row: 0,
          width: widthOf(id),
          height: heightOf(id),
          absX: (i % cols) * (opt.nodeWidth + opt.colGap),
          // 第 0 行留给"无关系节点"分组卡本身，网格从第 1 行开始
          absY: (Math.floor(i / cols) + 1) * (opt.nodeHeight + opt.rowGap),
        })
      })
    } else {
      placements.push({
        id: ORPHAN_GROUP_ID,
        label: '无关系节点',
        groupSize: orphanIds.length,
        groupBase: '',
        members: orphanIds,
        x: 0,
        y: 0,
        kind: 'group',
        depth: 0,
        row: 0,
        width: groupW,
        height: groupH,
        absX: 0,
        absY: 0,
      })
    }
  }

  // ------------------------------------------------------------
  // 阶段四：像素坐标
  // 列宽按该列最宽的节点算，行高按该行最高的节点算。
  // 绝对坐标覆盖的（展开的零关系网格）不参与这两项统计。
  // ------------------------------------------------------------
  const colWidth = new Map<number, number>()
  const rowHeight = new Map<number, number>()
  for (const p of placements) {
    if (p.absX !== undefined) continue
    colWidth.set(p.depth, Math.max(colWidth.get(p.depth) ?? 0, p.width))
    // 父节点的行号是子区间中点，可能是小数（36.5）。行高按**向下取整**的那一行记，
    // 位置再插值 —— 否则父节点会跟"半行"以外的邻居抢同一条基线。
    const row = Math.floor(p.row)
    rowHeight.set(row, Math.max(rowHeight.get(row) ?? 0, p.height))
  }
  const maxDepth = placements.length
    ? Math.max(...placements.filter((p) => p.absX === undefined).map((p) => p.depth), 0)
    : 0
  const colX: number[] = []
  let xAcc = 0
  for (let d = 0; d <= maxDepth; d++) {
    colX[d] = xAcc
    xAcc += (colWidth.get(d) ?? opt.nodeWidth) + opt.colGap
  }
  // 行号可能是小数，所以行高表要一直建到 ceil(最大行号)
  const maxRow = placements.length
    ? Math.ceil(Math.max(...placements.filter((p) => p.absX === undefined).map((p) => p.row), 0))
    : 0
  const rowY: number[] = []
  let yAcc = 0
  for (let r = 0; r <= maxRow; r++) {
    rowY[r] = yAcc
    yAcc += (rowHeight.get(r) ?? opt.nodeHeight) + opt.rowGap
  }
  // 小数行号在相邻两行基线之间插值
  const rowYAt = (r: number): number => {
    const lo = Math.floor(r)
    const hi = Math.ceil(r)
    if (hi === lo || rowY[hi] === undefined) return rowY[lo] ?? 0
    return rowY[lo] + (rowY[hi] - rowY[lo]) * (r - lo)
  }
  // 网格摆在树下方：树的总高度 + 一段留白
  const orphanGridTop = yAcc + opt.rowGap * 2

  let width = 0
  let height = 0
  for (const p of placements) {
    if (p.absX !== undefined) {
      p.x = p.absX
      p.y = orphanGridTop + (p.absY ?? 0)
    } else {
      p.x = colX[p.depth]
      p.y = rowYAt(p.row)
    }
    width = Math.max(width, p.x + p.width)
    height = Math.max(height, p.y + p.height)
  }

  // ------------------------------------------------------------
  // 阶段五：边
  // 树主干 = 主父边；折叠掉的成员不画边（分组卡代表了它们）。
  // ------------------------------------------------------------
  const edges: TreeEdgeDraw[] = []
  const placed = new Set(placements.map((p) => p.id))
  for (const [child, parent] of forest.parentOf) {
    if (!visible.has(child) || !visible.has(parent)) continue
    const e = forest.primaryEdgeOf.get(child)
    edges.push({
      key: `${child}>${parent}`,
      from: parent,
      to: child,
      kind: forest.parentKindOf.get(child) ?? e?.kind ?? 'inherits',
      label: e?.label ?? '',
      line: e?.line ?? 0,
      methods: e?.methods,
      cross: false,
    })
  }
  // 分组卡：由它的基类连出来（成员已被折叠，主干边不再重复画）
  for (const base of groupBases) {
    const gid = groupIdOf(base)
    if (!placed.has(gid) || !placed.has(base)) continue
    const kids = groupMembers.get(gid) ?? []
    const firstEdge = kids.length ? forest.primaryEdgeOf.get(kids[0]) : undefined
    edges.push({
      key: `${base}>${gid}`,
      from: base,
      to: gid,
      kind: firstEdge?.kind ?? 'inherits',
      label: firstEdge?.label ?? '',
      line: firstEdge?.line ?? 0,
      cross: false,
    })
  }
  // 交叉连线：既不是主父、又确实存在的关系
  for (const [child, parents] of forest.extraParents) {
    if (!visible.has(child)) continue
    for (const p of parents) {
      if (!visible.has(p.parent)) continue
      edges.push({
        key: `${child}>${p.parent}#cross`,
        from: p.parent,
        to: child,
        kind: p.kind,
        label: p.edge.label ?? '',
        line: p.edge.line ?? 0,
        methods: p.edge.methods,
        cross: true,
      })
    }
  }

  return {
    placements,
    edges,
    expandedBases,
    foldedCount,
    orphanCount: orphanIds.length,
    rootCount: forest.roots.length,
    cycleEdges: forest.cycleEdges,
    width,
    height,
  }
}
