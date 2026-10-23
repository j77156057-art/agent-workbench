// 工作台跨域共享的纯类型（无运行时代码，供各域 composable 互相引用，避免环依赖）。

export interface EditorTab {
  id: number
  path: string
  name: string
  lang: string
  region: string | null
  regionName: string | null
  mtime: number
  writable: boolean
  previewKind?: 'text' | 'binary' | 'image' | null
  previewNote?: string | null
  previewUrl?: string | null
  /** 只读快照：建编辑器时作为初始 doc，保存成功后更新 */
  savedContent: string
  draftContent?: string
  dirty: boolean
  loading: boolean
  error: string | null
  errorStatus: number | null
  saving: boolean
  savedAt: number | null
  tracked: boolean | null
  gitDirty: boolean | null
  reindexWarn: string | null
}
