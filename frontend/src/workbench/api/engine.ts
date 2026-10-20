// 任务分支 / 引擎连接 / 运行时 / 场景编辑 / Web 试玩（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { postJson, rawJson, request } from './_base'
import { DesktopHost, EmbedRect, EngineStatus, TaskScope } from './fs'

export const taskApi = {
  list() { return request<{ ok: boolean; tasks: Record<string, unknown>[] }>('/api/tasks') },
  create(payload: Record<string, unknown>) { return postJson<{ ok: boolean; task?: Record<string, unknown>; error?: string }>('/api/tasks', payload) },
  validate(payload: Record<string, unknown>) { return postJson<{ ok: boolean; scope: TaskScope }>('/api/tasks/validate', payload) },
  impact(payload: Record<string, unknown>) { return postJson<{ ok: boolean; files: string[]; direct_files: string[]; related_files: string[] }>('/api/tasks/impact', payload) },
  snapshot(payload: Record<string, unknown>) { return postJson<{ ok: boolean; snapshot: Record<string, unknown> }>('/api/tasks/snapshot', payload) },
  verify(payload: Record<string, unknown>) { return postJson<{ ok: boolean; checks: { command: string; ok: boolean; output?: string; error?: string }[] }>('/api/tasks/verify', payload) },
  branch(payload: Record<string, unknown>) { return postJson<{ ok: boolean; branch?: string; error?: string }>('/api/tasks/branch', payload) },
}

export interface EngineCatalogEntry {
  id: string
  name: string
  executable: string
  download: string
  project_file: string
}

export interface EngineConfig {
  ok: boolean
  engine: string
  executable: string
  error?: string
}

export const engineApi = {
  unrealBridgeStatus() { return request<{ok:boolean;available:boolean;error?:string}>('/api/engine/unreal-bridge/status') },
  unrealAssets() { return request<{ok:boolean;available:boolean;assets?:string[];error?:string}>('/api/engine/unreal-bridge/assets') },
  unrealActors() { return request<{ok:boolean;available:boolean;actors?:{name:string;class:string}[];error?:string}>('/api/engine/unreal-bridge/actors') },
  unrealBlueprint(path: string) { return request<{ok:boolean;available:boolean;blueprint?:string;nodes?:unknown[];variables?:unknown[];links?:unknown[];error?:string}>(`/api/engine/unreal-bridge/blueprint/${encodeURIComponent(path)}`) },
  unrealActor(name: string) { return request<{ok:boolean;available:boolean;actor?:string;components?:unknown[];properties?:unknown[];error?:string}>(`/api/engine/unreal-bridge/actor/${encodeURIComponent(name)}`) },
  unrealWrite(body: {task_id:string;target_path:string;property:string;value:unknown;confirm:boolean}) { return postJson<{ok:boolean;available?:boolean;error?:string}>('/api/engine/unreal-bridge/write', body) },
  catalog() { return request<{ ok:boolean; engines:EngineCatalogEntry[] }>('/api/engine/catalog') },
  config(engine?: string, executable?: string) { return engine ? postJson<EngineConfig>('/api/engine/config',{engine,executable}) : request<EngineConfig>('/api/engine/config') },
  /** 深度读取 Unity/Unreal 项目的文本资产，建立轻量索引（清单/场景/预制体/资产/符号/蓝图/关卡）。 */
  inspect(engine = '') { return request<{ ok:boolean; engine?:string; manifests?:Record<string,unknown>[]; scenes?:string[]; prefabs?:string[]; assets?:string[]; symbols?:Record<string,unknown>[]; blueprints?:string[]; levels?:string[]; error?:string }>(`/api/engine/inspect?engine=${encodeURIComponent(engine)}`) },
  /** 单文件 GDScript 校验（godot --headless --check-only，不开编辑器）。只支持 .gd。 */
  check(path: string, executable = '', timeout = 120) { return postJson<{ ok:boolean; error?:string; diagnostics?:{severity:string;message:string;line?:number}[]; returncode?:number; output?:string }>('/api/engine/check', { path, executable, timeout }) },
  /** Unreal/MSVC 编译输出 → 统一诊断结构。 */
  diagnostics(text: string, engine = 'unreal') { return postJson<{ ok:boolean; engine?:string; diagnostics?:{path:string;line:number;kind:string;message:string}[]; error?:string }>('/api/engine/diagnostics', { engine, text }) },
  /** 安装 Unreal 桥接脚本（写文件，需用户明确确认）。 */
  installUnrealBridge(force = false) { return postJson<{ ok:boolean; path?:string; created?:boolean; note?:string; error?:string }>('/api/engine/unreal-bridge/install', { confirm: true, force }) },
  /** 装运行时探针（addons/docmind_runtime/probe.gd）。 */
  installRuntimeProbe() { return postJson<{ ok:boolean; path?:string; created?:boolean; error?:string }>('/api/runtime/probe', {}) },
  /** 跑一次玩测（自动探测测试框架或自定义命令）。 */
  playtest(command = '', timeout = 30) { return postJson<{ ok:boolean; error?:string; output?:string; exit_code?:number; timed_out?:boolean }>('/api/playtest', { command, timeout }) },
  /** 扫描项目里有没有引擎工程文件（project.godot / ProjectVersion.txt / *.uproject）。 */
  scan() { return request<{ ok: boolean; projects: { engine: string; path: string }[] }>('/api/engine/scan') },
  /** 探测可执行文件并写入项目配置；ready=false 时带下载链接。
   *  用 rawJson 而不是 postJson：失败体的 `download`（引擎官方下载地址）就在
   *  `ok:false` 那一支里，而 postJson 遇到 ok:false 会直接抛异常 —— 链接就丢了，
   *  用户只看到"未找到引擎可执行文件"却不知道去哪装。 */
  prepare(engine: string, executable = '') { return rawJson<{ ok: boolean; ready?: boolean; engine?: string; download?: string; error?: string; config?: EngineConfig; projects?: { engine: string; path: string }[] }>('/api/engine/prepare', { engine, executable }) },
  /** 该引擎的适配 Skill 文本（给 AI 读的项目约定）。 */
  skill(engine: string) { return request<{ ok: boolean; engine: string; skill: string }>(`/api/engine/skill?engine=${encodeURIComponent(engine)}`) },
  status() { return request<EngineStatus>('/api/engine/status') },
  start(executable = 'godot', embed = true, rect?: EmbedRect | null) {
    return postJson<EngineStatus>('/api/engine/start', rect ? { executable, embed, rect } : { executable, embed })
  },
  host() { return request<DesktopHost>('/api/desktop/host') },
  /** 把已运行的引擎窗口嵌进桌面宿主。rect 省略时按宿主客户区铺满。 */
  embed(rect?: EmbedRect | null) { return postJson<{ ok: boolean; error?: string; width?: number; height?: number; embedded?: boolean }>('/api/engine/embed', rect || {}) },
  /** 引擎视窗随前端布局变化重新定位（弹窗移动、窗口缩放时调用）。 */
  place(rect: EmbedRect) { return postJson<{ ok: boolean; error?: string }>('/api/engine/place', rect) },
  detach() { return postJson<{ ok: boolean; was_embedded?: boolean; error?: string }>('/api/engine/detach', {}) },
  focusEngine(keepAttached = false) { return postJson<{ ok: boolean; focused?: number; error?: string }>('/api/engine/focus', { keep_attached: !!keepAttached }) },
  /** 按宿主当前客户区重排"铺满模式"的嵌入窗口 */
  resizeEngine() { return postJson<{ ok: boolean; error?: string }>('/api/engine/resize', {}) },
  stop() { return postJson<{ ok: boolean; stopped: boolean }>('/api/engine/stop', {}) },
  /** 保存启动参数并快速重启运行中的 Godot，恢复原嵌入位置。 */
  reload() { return postJson<{ ok: boolean; running?: boolean; embedded?: boolean; reloaded?: boolean; reload_mode?: string; error?: string }>('/api/engine/reload', {}) },
  /** 检查引擎启动后项目脚本/场景/资源是否被外部修改，不会自动重载。 */
  changes() { return request<{ ok: boolean; running?: boolean; changed?: string[]; added?: string[]; deleted?: string[]; files?: string[]; count?: number; error?: string }>('/api/engine/changes') },
  logs(limit = 200) { return request<{ ok: boolean; lines: string[]; errors: { path: string; line: number; message: string }[] }>(`/api/engine/logs?limit=${limit}`) },
  verify(executable = 'godot') { return postJson<{ ok: boolean; output?: string; error?: string }>('/api/engine/verify', { executable }) },
}

export const runtimeApi = {
  events() { return request<{ ok: boolean; events: Record<string, unknown>[] }>('/api/runtime/events') },
  append(events: Record<string, unknown>[]) { return postJson<{ ok: boolean; events: Record<string, unknown>[] }>('/api/runtime/events', { events }) },
  /** 会话切分（时间线按会话分组） */
  sessions(gap = 120) { return request<{ ok: boolean; total: number; sessions: RuntimeSession[] }>(`/api/runtime/sessions?gap=${gap}`) },
  clear(scope: 'stored' | 'all' = 'stored') { return postJson<{ ok: boolean; scope: string; removed: number }>('/api/runtime/clear', { scope }) },
}

export interface RuntimeSession {
  index: number
  id: string
  start: string
  end: string
  count: number
  types: Record<string, number>
  sources: Record<string, number>
}

/* ---------------- 场景画布（P0-2） ---------------- */

export interface SceneProperty {
  name: string
  value: string
  line: number
  kind: 'ref' | 'transform' | 'vector' | 'array' | 'string' | 'bool' | 'number' | 'other'
}

export interface SceneNode {
  id: string
  name: string
  type: string
  parent: string | null
  parent_attr: string
  line: number
  space: '2d' | '3d'
  depth: number
  instance_id: string
  script_id: string
  instance: string
  script: string
  groups: string[]
  index: string
  position: number[] | null
  position_from: string
  rotation: number | null
  scale: number[] | null
  children: string[]
  /** 引用的其它外部资源卡 id（'ext:<rid>'），贴图/材质/字体等 */
  resource_ids: string[]
  /** true = 位于实例（instance）子树内，改动属于引擎侧覆写 */
  overridden: boolean
  properties: SceneProperty[]
}

export interface SceneFile {
  id: string
  rel: string
  raw: string
  kind: 'scene' | 'script' | 'resource'
  chip: string
  resolved: boolean
  type: string
  uid: string
  nodes: string[]
  used: number
  orphan?: boolean
}

export interface SceneEdge {
  id: string
  source: string
  target: string
  kind: 'hierarchy' | 'script' | 'instance' | 'reference'
}

export interface SceneGuard {
  writable: boolean
  reason: string
  region: string | null
  region_name: string | null
  tracked: boolean | null
  dirty: boolean | null
}

export interface SceneGraph {
  ok: boolean
  error?: string
  path: string
  root_id: string
  header: Record<string, string>
  space: '2d' | '3d'
  nodes: SceneNode[]
  files: SceneFile[]
  edges: SceneEdge[]
  sub_resources: { id: string; type: string; line: number }[]
  structure_errors: string[]
  warnings: string[]
  guard: SceneGuard
  stats: {
    nodes: number
    edges: number
    files: number
    max_depth: number
    instances: number
    scripts: number
    lines: number
    size: number
    /** 零引用的外部资源数（可清理提示） */
    orphans: number
    revision: string
    mtime: number
    editable: boolean
  }
  read_only: boolean
}

/**
 * 一条可反向执行的编辑描述。
 * 字段名与 POST /api/scene/op 的请求体完全同形，所以 `sceneApi.op(undo)` 就能撤销，
 * 前端不需要任何字段映射（这个"同形"是刻意的：曾经用过 props/properties 两套名字，
 * 结果撤销请求被后端当成空 payload 拒掉，所以定死一套）。
 */
export interface SceneUndo {
  op: string
  node?: string
  name?: string
  parent?: string
  properties?: Record<string, string>
  remove?: string[]
  lines?: string[]
  at?: number
}

export interface SceneOpResult {
  ok: boolean
  error?: string
  status?: number
  /** 文件已被外部修改，需重新加载 */
  stale?: boolean
  /** 编辑导致结构非法且已自动回滚 */
  rolled_back?: boolean
  errors?: string[]
  path?: string
  op?: string
  node?: string
  undo?: SceneUndo
  warnings?: string[]
  revision?: string
  mtime?: number
}

export const sceneApi = {
  graph(path: string) { return rawJson<SceneGraph>(`/api/scene/graph?path=${encodeURIComponent(path)}`) },
  op(payload: Record<string, unknown>) { return rawJson<SceneOpResult>('/api/scene/op', payload) },
  /** 项目主场景（project.godot 的 run/main_scene）——场景画布打开时自动加载用 */
  main() { return rawJson<{ ok: boolean; scene?: string; exists?: boolean; error?: string; note?: string }>('/api/scene/main') },
}

/** P1：Web 导出 / iframe 试玩 */
export interface WebTemplates {
  ok: boolean
  version?: string
  template_dir?: string
  installed: boolean
  web_debug: boolean
  web_release: boolean
  install?: { state: string; downloaded: number; total: number; error: string; started_at: number; mirror?: string }
  error?: string
}
export interface WebExportResult {
  ok: boolean
  token?: string
  url?: string
  elapsed?: number
  files?: Record<string, number>
  html_injected?: boolean
  bridge_changed?: string[]
  preset_added?: boolean
  output?: string
  error?: string
  message?: string
  templates?: WebTemplates
}
export const playApi = {
  templates() { return request<WebTemplates>('/api/engine/web/templates') },
  installTemplates() { return postJson<{ ok: boolean; started: boolean }>('/api/engine/web/templates/install', { confirm: true }) },
  exportWeb() { return postJson<WebExportResult>('/api/engine/web/export', {}) },
}
