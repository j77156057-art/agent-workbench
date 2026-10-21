// 视觉主题助手：分区配色（后端 regions.json 无颜色字段，前端按 key 稳定映射）、
// 文件类型 chip、时间/大小格式化。

/** 八个默认分区的固定色板；自定义分区走 hash 兜底，同一 key 永远同色 */
const REGION_COLORS: Record<string, string> = {
  values: '#58a6ff',     // 数值区 · 冷蓝
  behaviors: '#bc8cff',  // 角色行为区 · 紫
  assets: '#e3a83a',     // 素材区 · 琥珀金
  bugs: '#ff6b6b',       // bug 区 · 警戒红
  levels: '#45c98c',     // 关卡·场景区 · 翠绿
  ui: '#f072b6',         // UI·HUD 区 · 品红
  audio: '#2ec4b6',      // 音频区 · 青碧
  net: '#f0883e',        // 网络·存档区 · 橙
}

export function regionColor(key: string | null | undefined): string {
  if (!key) return '#7d8590'
  if (REGION_COLORS[key]) return REGION_COLORS[key]
  let h = 0
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0
  return `hsl(${h % 360} 62% 64%)`
}

/** 分区色**当文字用**时的版本：留 36% 色相、混向 --ink。
 *  皮肤决定 --ink 的明暗，所以同一个混合式在纸底与石墨底都自动达标；
 *  36% 是对 0–359 全色相扫出来的下限（亮黄最吃亏），再高就破 4.5:1。
 *  填色/描边仍用 regionColor —— 那是图形不是文本。 */
export function regionTextColor(key: string | null | undefined): string {
  return `color-mix(in srgb, ${regionColor(key)} 36%, var(--ink))`
}

/** 画布身份色的皮肤派生层：色相保留 55%，其余混向本皮肤 --ink。
 *  这些色相当年是照深色底调的（GitHub 深色系那批值），实测在 paper / zinc 上
 *  只有 2.0–2.5:1，而 graphite 反而全过 —— 也就是说缺的不是"新配色"，是明度跟皮肤。
 *  往 --ink 靠是双向的：浅色皮肤压暗、深色皮肤提亮，一个比例两边都变好。
 *  55% 是扫出来的（三套皮肤 × 两种画布底最差 3.47 / 4.22 / 3.65，过 1.4.11 的 3:1）。
 *  要当文字用请走 regionTextColor 那档 36%（4.5:1），别拿这个糊小字。 */
export const cat = (hex: string) => `color-mix(in srgb, ${hex} 55%, var(--ink))`

/** 同一个身份色的半透明版（光晕、淡底这类，允许透出后面的东西）。 */
export const catAlpha = (hex: string, pct: number) =>
  `color-mix(in srgb, ${cat(hex)} ${pct}%, transparent)`

/** 身份色压进**不透明**表面令牌：节点卡底板用它。
 *  为什么不用 catAlpha —— 半透明底的最终颜色取决于卡片后面是什么，
 *  实测同一张卡在 paper 上成像 rgb(221,222,211)，比 --bg-raised 暗了 29 个通道值，
 *  于是压在上面的 --text-muted 掉到 4.38:1 而没人看得见（画布底、点阵网格、
 *  卡片色底三层叠出来的）。改成不透明之后底是确定的，模型和现实必然一致。 */
export const catOn = (hex: string, pct: number) =>
  `color-mix(in srgb, ${cat(hex)} ${pct}%, var(--bg-raised))`

export type ChipKind = 'code' | 'data' | 'web' | 'doc' | 'media' | 'lib' | 'plain'

const CHIP_RULES: Array<{ exts: string[]; kind: ChipKind }> = [
  { exts: ['py', 'js', 'jsx', 'ts', 'tsx', 'gd', 'gdshader', 'lua', 'cs', 'go', 'rs', 'java', 'c', 'h', 'cpp', 'hpp', 'rb', 'php', 'kt', 'swift', 'scala', 'sh'], kind: 'code' },
  { exts: ['json', 'yaml', 'yml', 'toml', 'csv', 'ini', 'cfg', 'tscn', 'tres', 'res'], kind: 'data' },
  { exts: ['css', 'html'], kind: 'web' },
  { exts: ['md', 'txt'], kind: 'doc' },
  { exts: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'wav', 'mp3', 'ogg', 'mp4', 'webm', 'glb', 'gltf'], kind: 'media' },
]

export function fileChip(name: string): { label: string; kind: ChipKind } {
  const dot = name.lastIndexOf('.')
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
  if (!ext) return { label: '•', kind: 'plain' }
  const rule = CHIP_RULES.find((r) => r.exts.includes(ext))
  return { label: ext.slice(0, 4).toUpperCase(), kind: rule ? rule.kind : 'lib' }
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

export function formatMtime(mtime: number): string {
  const d = new Date(mtime * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** git 状态语义：untracked=绿点；dirty=琥珀点；clean/无仓库=不显示 */
export function gitState(tracked: boolean | null | undefined, dirty: boolean | null | undefined) {
  if (tracked === false) return { dot: 'untracked', title: '未纳入 git，删除不可恢复' }
  if (tracked && dirty) return { dot: 'dirty', title: '有未提交的改动' }
  if (tracked) return { dot: 'clean', title: '已纳入 git，无改动' }
  return { dot: 'none', title: '该项目未启用 git 版本管理' }
}

/** P1 符号种类的中文标签 / 角标 / 颜色（大纲面板与符号地图共用） */
const SYMBOL_KINDS: Record<string, { label: string; mark: string; color: string }> = {
  function: { label: '函数', mark: 'ƒ', color: '#bc8cff' },
  class: { label: '类', mark: 'C', color: '#58a6ff' },
  signal: { label: '信号', mark: '~', color: '#f072b6' },
  enum: { label: '枚举', mark: 'E', color: '#e3a83a' },
  const: { label: '常量', mark: 'K', color: '#45c98c' },
  var: { label: '变量', mark: 'x', color: '#2ec4b6' },
  node: { label: '节点', mark: '▣', color: '#f0883e' },
  resource: { label: '资源', mark: 'R', color: '#8b97a7' },
  section: { label: '配置段', mark: '§', color: '#8b97a7' },
  group: { label: '导出分组', mark: '▼', color: '#7d8590' },
}

export function symbolKind(kind: string): { label: string; mark: string; color: string } {
  return SYMBOL_KINDS[kind] ?? { label: kind, mark: '·', color: '#7d8590' }
}

/** P1 关系图节点外观（类 / 匿名脚本 / 场景 / 引擎与第三方基类） */
export interface GraphNodeStyle {
  fill: string
  stroke: string
  text: string
  mark: string
  dashed: boolean
  label: string
}

const GRAPH_NODE_STYLES: Record<string, GraphNodeStyle> = {
  class: { fill: 'rgba(88,166,255,0.14)', stroke: '#58a6ff', text: '#d6e7ff', mark: 'C', dashed: false, label: '命名类' },
  script: { fill: 'rgba(46,196,182,0.12)', stroke: '#2ec4b6', text: '#c9f3ef', mark: 'S', dashed: false, label: '脚本（无 class_name）' },
  scene: { fill: 'rgba(240,136,62,0.13)', stroke: '#f0883e', text: '#ffe0c4', mark: '▣', dashed: false, label: '场景' },
  engine: { fill: 'rgba(139,151,167,0.06)', stroke: '#6b7686', text: '#a6b1bf', mark: '⌂', dashed: true, label: '引擎基类' },
  external: { fill: 'rgba(139,151,167,0.06)', stroke: '#6b7686', text: '#a6b1bf', mark: '{}', dashed: true, label: '第三方基类' },
  // 分组卡：大扇出折叠后的替代物（"这一坨 791 个子类"）。
  // 描边走中性灰 —— 和 external 同一套 cat() 派生，理由是它必须扛得住
  // 画布对比度门禁：SVG 的 fill 是绘制属性、门禁不算它，描边是跟**画布点阵底**
  // 比的（zinc 下 = rgba(125,133,144,.10) 叠画布色）。实测边框令牌
  // --border-graphic(#8f8f95) 对那个底只有 2.82:1（差 0.18 不达标），
  // 而 cat() 派生出来的这档在三套皮肤下都过。
  group: { fill: 'rgba(139,151,167,0.06)', stroke: '#6b7686', text: '#a6b1bf', mark: '⋯', dashed: true, label: '分组（折叠的子类）' },
}

export function graphNodeStyle(kind: string): GraphNodeStyle {
  const s = GRAPH_NODE_STYLES[kind] ?? GRAPH_NODE_STYLES.external
  // 表里那几个值是"身份"，出口一律派生：描边过 3:1、底板只压 8% 同色相。
  // 8% 是量出来的：14% 时卡片标签在 paper 上掉到 4.38:1（底被色相染暗了，字还是中性色），
  // 8% 既留得住"按类型分组"的那层淡色，又把标签推回 4.5 以上。
  // text 不再回那批调死的浅色（#d6e7ff 在纸底上等于隐形）—— 标签走中性色，
  // 色相由卡片描边和底色承担。
  return { ...s, stroke: cat(s.stroke), fill: catOn(s.stroke, 8), text: 'var(--text)' }
}
