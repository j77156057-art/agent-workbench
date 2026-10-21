/**
 * 产物（artifact）的展示口径 —— 纯函数，无 DOM 依赖。
 *
 * ## 为什么抽出来
 *
 * 判定"这条产物是不是图片、该显示缩略图还是文件名、预览地址怎么拼"
 * 这几件事如果写在 .vue 里就只能靠眼睛看；而它们恰恰是**判错后果最直接**的：
 * 该显示缩略图的显示成了一个文件名，用户还是得自己去路径里翻 ——
 * 也就是这个功能本来要解决的问题。
 *
 * 所以口径放这里，组件只负责摆位置；node 单测直接跑这个模块。
 */

export interface ArtifactView {
  id?: string
  kind?: string
  path?: string
  label?: string
  summary?: string
  bytes?: number
  evidence?: string[]
  adapter?: string
  uri?: string
  mime?: string
  renderer?: string
  change_summary?: { added_lines?: number; removed_lines?: number }
}

const IMAGE_EXT = /\.(png|jpe?g|webp|gif|bmp|svg)$/i

/**
 * 是不是图片。
 *
 * 后端给的是 `kind`（preview_adapters.infer_artifact_kind：
 * image/code/text/diff/audio/video/model/structured/binary/interactive/unknown），
 * 但**不同产出来源的 kind 口径不完全一致**：
 * - `create_file` / `apply_edit` 那批压根不带 kind
 * - MCP 适配器那批是按扩展名推的
 * - `game_screenshot` 显式写 kind='image'
 * 所以再按扩展名兜一次。
 */
export function isImageArtifact(a: ArtifactView): boolean {
  if (a.kind === 'image') return true
  if (a.kind === 'code' || a.kind === 'text' || a.kind === 'diff') return false
  return !!a.path && IMAGE_EXT.test(a.path)
}

/**
 * 预览直链。
 *
 * 优先用后端给的 `uri` —— 但**只认 http/https**：
 * 后端 `_safe_uri` 已滤掉 javascript:/data:/file:，前端再兜一层，
 * 免得哪天后端放宽了口径，这儿就变成一个能点开的 XSS 入口。
 * 其余走项目相对路径 → `/api/assets/raw`（后端按项目根做越界校验）。
 */
export function artifactRawUrl(a: ArtifactView): string {
  const uri = (a.uri || '').trim()
  if (/^https?:\/\//i.test(uri)) return uri
  if (!a.path) return ''
  return `/api/assets/raw?path=${encodeURIComponent(a.path)}`
}

/** 改动的行数摘要（diff 类产物）。 */
export function artifactDiffLabel(a: ArtifactView): string {
  const cs = a.change_summary
  if (!cs) return ''
  const add = cs.added_lines ?? 0
  const del = cs.removed_lines ?? 0
  if (!add && !del) return ''
  return `+${add} / -${del} 行`
}

export function artifactSizeLabel(a: ArtifactView): string {
  if (typeof a.bytes !== 'number' || !Number.isFinite(a.bytes)) return ''
  if (a.bytes < 1024) return `${a.bytes} B`
  if (a.bytes < 1024 * 1024) return `${(a.bytes / 1024).toFixed(1)} KB`
  return `${(a.bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 卡片上的短名：文件名（去目录），没有路径就用后端给的 label。 */
export function artifactShortName(a: ArtifactView): string {
  const p = (a.path || '').split(/[\\/]/).filter(Boolean).pop()
  return p || a.label || '产物'
}

/** 一句话说明：后端给了就用，没给就按类型补一个（别让卡片空着标题）。 */
export function artifactCaption(a: ArtifactView): string {
  if (a.summary) return a.summary
  return artifactDiffLabel(a) || artifactSizeLabel(a)
}

/** 该显示的产物：至少有个能认出来的标识。 */
export function visibleArtifacts(list: ArtifactView[] | undefined): ArtifactView[] {
  return (list || []).filter((a) => a && (a.path || a.label || a.uri))
}
