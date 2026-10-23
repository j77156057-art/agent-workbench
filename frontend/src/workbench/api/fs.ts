// 文件树 / 读写 / Git 历史 / 符号与关系图谱类型 + fsApi（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { postJson, request } from './_base'

// 工作台文件系统接口 client（对应后端 workbench_fs.py，前缀 /api/fs）
// dev 由 vite 代理到 127.0.0.1:8000；打包后与后端同源，全部用相对路径。

export interface RegionMeta {
  key: string
  name: string
  dir: string
  desc: string
}

export interface TreeNode {
  path: string
  name: string
  type: 'dir' | 'file'
  region: string | null
  region_name: string | null
  lang: string | null
  writable: boolean
  children: TreeNode[]
  size?: number
  /** true=已纳入 git 且干净；false=未跟踪；null=不在任何 git 仓库 */
  tracked?: boolean | null
  /** true=有未提交改动（含新增已 add） */
  dirty?: boolean | null
}

export interface TreeResp {
  ok: boolean
  code_root: string
  regions_enabled: boolean
  regions: RegionMeta[]
  nodes: TreeNode[]
  truncated: boolean
}

export interface TaskScope {
  ok: boolean
  errors: { path: string; error: string }[]
  region: string
  allowed_paths: string[]
}

export interface EngineStatus {
  ok: boolean
  running: boolean
  pid?: number | null
  error?: string
  /** 引擎窗口是否已嵌进桌面宿主（浏览器模式下永远为 false） */
  embedded?: boolean
  child_hwnd?: number | null
  host_hwnd?: number | null
  embed_title?: string
  embed_offset_y?: number
  embed_dpi?: number
  host_dpi?: number
  embed_mode?: 'rect' | 'fill'
  embed_size?: { width: number; height: number }
  embed_error?: string
  /** engine/start 成功嵌入时回传的实际落点与 DPI */
  embed?: { width: number; height: number; offset_y: number; dpi: number; host_dpi: number; title: string }
}

/** 桌面宿主信息。浏览器模式下 desktop=false，前端据此降级（不显示"嵌入工作台"）。 */
export interface DesktopHost {
  ok: boolean
  host_hwnd: number | null
  desktop?: boolean
  dpi_awareness?: string
  /** 宿主客户区尺寸（物理像素）——把页面 CSS 坐标换算成引擎窗口坐标就靠它 */
  client?: { width: number; height: number }
  dpi?: number
  embedded?: {
    hwnd: number
    host: number
    mode?: 'rect' | 'fill'
    placed?: { x: number; y: number; width: number; height: number }
    alive: boolean
  }[]
  bridge_error?: string
}

/** 引擎视窗矩形：宿主客户区物理像素坐标 */
export interface EmbedRect { x: number; y: number; width: number; height: number }

export interface FileResp {
  ok: boolean
  path: string
  content: string
  lang: string
  size: number
  mtime: number
  region: string | null
  region_name: string | null
  writable: boolean
  preview_kind?: 'text' | 'binary' | 'image' | null
  preview_note?: string | null
  preview_url?: string | null
  tracked: boolean | null
  dirty: boolean | null
}

export interface SaveResp {
  ok: boolean
  path: string
  mtime: number
  changed: boolean
  region: string | null
  region_name: string | null
  reindexed: { code_chunks: number } | null
  reindex_warnings: string[]
}

export interface DeleteResp {
  ok: boolean
  deleted: string
  recoverable: boolean
}

export interface RenameResp {
  ok: boolean
  from: string
  to: string
  git_renamed: boolean
  reindex_warnings: string[]
}

/** P3：git 提交记录（/gitlog） */
export interface GitCommit {
  hash: string
  full_hash: string
  time: number | null
  time_raw: string
  author: string
  message: string
}
export interface GitLogResp {
  ok: boolean
  path: string
  commits: GitCommit[]
}

/** P3：文件级回滚响应；reverted=false 且 reason='clean' 表示本就无改动 */
export interface RevertResp {
  ok: boolean
  path: string
  reverted: boolean
  reason?: string
  mtime: number
}

/** P3：某文件某次提交时的历史内容 */
export interface GitShowResp {
  ok: boolean
  path: string
  ref: string
  content: string
  size: number
}

/** P3：恢复到历史版本（响应体同 save，另带 ref/restored） */
export interface RestoreResp extends SaveResp {
  ref: string
  restored: boolean
}

/** P4：改动总览里的 6 种语义（后端 _classify_change 的取值） */
export type GitChangeKind =
  | 'conflict' | 'modified' | 'added' | 'deleted' | 'renamed' | 'untracked'

export interface GitChangeFile {
  /** 仓库内相对路径；改名时为**源（旧名）** */
  path: string
  /** 改名/复制时的目标路径（新名） */
  path_to?: string
  kind: GitChangeKind
  /** 后端给的中文标签（冲突/修改/新增/删除/改名/未跟踪） */
  label: string
  /** porcelain 原始两列状态码，供排查用 */
  xy: string
}

export interface GitChangeRepo {
  name: string
  /** 相对 code_root 的正斜杠路径；根仓库为 '' */
  dir: string
  branch: string
  files: GitChangeFile[]
  /** 单个仓库读取失败时的原因（不影响其他仓库） */
  error?: string
}

export interface GitChangesResp {
  ok: boolean
  repos: GitChangeRepo[]
  total: number
  truncated: boolean
  empty: boolean
}

/** P1：符号（func/class/const/var/signal/enum/node ...） */
export interface SymbolInfo {
  name: string
  kind: string
  start: number
  end: number
  parent: string
  signature: string
  doc: string
  detail: string
}

export interface SymbolsResp {
  ok: boolean
  path: string
  lang: string
  class_name: string
  extends: string
  doc: string
  symbols: SymbolInfo[]
}

export interface SymbolMapFile {
  rel: string
  lang: string
  region: string
  region_name: string
  class_name: string
  extends: string
  doc: string
  symbols: SymbolInfo[]
}

export interface SymbolMapResp {
  ok: boolean
  code_root: string
  regions_enabled: boolean
  files: SymbolMapFile[]
  stats: {
    files: number
    symbols: number
    by_kind: Record<string, number>
    skipped: number
  }
}

/** P1：关系图节点（class/script/scene/external） */
export interface RelationNode {
  id: string
  label: string
  sub: string
  kind: 'class' | 'script' | 'scene' | 'engine' | 'external' | string
  rel: string
  line: number
  region: string
  region_name: string
  external: boolean
  doc: string
}

export interface RelationEdge {
  source: string
  target: string
  /** inherits=继承；mounts=场景挂载脚本；calls=项目内调用 */
  kind: 'inherits' | 'mounts' | 'calls' | string
  label: string
  line: number
  methods?: string[]
}

export interface RelationGraphResp {
  ok: boolean
  code_root: string
  regions_enabled: boolean
  nodes: RelationNode[]
  edges: RelationEdge[]
  stats: {
    files: number
    nodes: number
    user_nodes: number
    external_nodes: number
    edges: number
    edges_by_kind: Record<string, number>
    skipped: number
  }
}

/** P1-2：Unity GUID 引用图节点（资产 / 缺失外部引用） */
export interface UnityNode {
  id: string
  guid: string
  label: string
  sub: string
  /** scene/prefab/script/material/texture/.../missing */
  kind: string
  rel: string
  line: number
  external: boolean
  doc: string
}

export interface UnityEdge {
  source: string
  target: string
  kind: 'guid-ref' | string
  label: string
  line: number
  /** 同一 (源,目标) 的引用处数（已聚合） */
  count: number
}

export interface UnityGraphResp {
  ok: boolean
  root?: string
  nodes: UnityNode[]
  edges: UnityEdge[]
  stats: {
    unity_project: boolean
    scanned_root: string
    metas: number
    serialized_files: number
    assets_total: number
    nodes: number
    asset_nodes: number
    missing_nodes: number
    orphan_meta: number
    duplicate_guids: number
    edges: number
    resolved_edges: number
    missing_edges: number
    skipped: number
    by_kind: Record<string, number>
  }
  error?: string
}


export const fsApi = {
  tree(depth = 4): Promise<TreeResp> {
    return request<TreeResp>(`/api/fs/tree?depth=${depth}`)
  },
  read(path: string): Promise<FileResp> {
    return request<FileResp>(`/api/fs/file?path=${encodeURIComponent(path)}`)
  },
  save(path: string, content: string, ifMtime: number | null, reindex = true, taskId = ''): Promise<SaveResp> {
    return postJson<SaveResp>('/api/fs/save', {
      path,
      content,
      if_mtime: ifMtime,
      reindex,
      task_id: taskId,
    })
  },
  create(path: string, type: 'file' | 'folder', content = '') {
    return postJson<{ ok: boolean; path: string; node: TreeNode }>('/api/fs/create', {
      path, type, content,
    })
  },
  rename(path: string, newPath: string, reindex = true): Promise<RenameResp> {
    return postJson<RenameResp>('/api/fs/rename', {
      path, new_path: newPath, reindex,
    })
  },
  delete(path: string, recursive = false, force = false): Promise<DeleteResp> {
    return postJson<DeleteResp>('/api/fs/delete', { path, recursive, force })
  },
  symbols(path: string): Promise<SymbolsResp> {
    return request<SymbolsResp>(`/api/fs/symbols?path=${encodeURIComponent(path)}`)
  },
  symbolMap(): Promise<SymbolMapResp> {
    return request<SymbolMapResp>('/api/fs/symbol-map')
  },
  relationGraph(): Promise<RelationGraphResp> {
    return request<RelationGraphResp>('/api/fs/relation-graph')
  },
  unityGuidGraph(): Promise<UnityGraphResp> {
    return request<UnityGraphResp>('/api/unity/guid-graph')
  },
  sceneTree(path: string) { return request<{ ok: boolean; nodes: { name: string; type: string; parent: string; line: number; node_path?: string; properties?: {name:string;value:string;line:number}[] }[] }>(`/api/fs/scene-tree?path=${encodeURIComponent(path)}`) },
  setSceneProperty(path: string, node: string, property: string, value: string) { return postJson<{ ok: boolean; error?: string }>('/api/fs/scene-property', { path, node, property, value }) },
  gitlog(path: string, limit = 20): Promise<GitLogResp> {
    return request<GitLogResp>(
      `/api/fs/gitlog?path=${encodeURIComponent(path)}&limit=${limit}`,
    )
  },
  /** P4：仓库级未提交改动总览（只读）。 */
  gitChanges(): Promise<GitChangesResp> {
    return request<GitChangesResp>('/api/fs/git-changes')
  },
  revert(path: string): Promise<RevertResp> {
    return postJson<RevertResp>('/api/fs/revert', { path })
  },
  gitShow(path: string, ref: string): Promise<GitShowResp> {
    return request<GitShowResp>(
      `/api/fs/git-show?path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`,
    )
  },
  restoreAt(path: string, ref: string, reindex = true): Promise<RestoreResp> {
    return postJson<RestoreResp>('/api/fs/restore-at', { path, ref, reindex })
  },
}
