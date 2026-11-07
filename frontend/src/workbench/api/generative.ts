// ComfyUI / 素材库 / 资产生成 / 云端生成（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { FsApiError, postJson, request, withProject } from './_base'

export interface ComfyWatchJob {
  prompt_id: string
  running: boolean
  done: boolean
  result?: Record<string, unknown> | null
  started_at?: string
  finished_at?: string
  cancel_requested?: boolean
  cancel_requested_at?: string
  cancel_state?: 'requesting' | 'requested' | 'terminated' | 'failed'
  status?: 'queued' | 'running' | 'completed' | 'failed' | 'timeout' | 'recovered'
  recovery_note?: string
  recovered_at?: string
  progress?: { executed_nodes: number; total_nodes: number; percent: number }
}

export interface ComfyModelCheckTemplate {
  id: string
  name: string
  present: boolean
  missing: string[]
  pending: boolean
}
export interface ComfyModelCheckResult {
  ok: boolean
  root?: string
  found?: string[]
  templates?: ComfyModelCheckTemplate[]
  error?: string
}

export const comfyApi = {
  start(root?: string, port = 8188) { return postJson<{ok:boolean;running?:boolean;pid?:number;error?:string}>('/api/comfy/start', { root, port }) },
  stop() { return postJson<{ok:boolean;running?:boolean;stopped?:boolean;pid?:number;error?:string}>('/api/comfy/stop', {}) },
  templates() { return request<{ ok: boolean; templates: { id:string; name:string; model:string; kind:string; group?:'image'|'control'|'character'|'video'; vram_gb?:number; models?:string[]; pending?:boolean; hint?:string; source_url?:string; workflow?:string; schema?:Record<string,string> }[] }>('/api/comfy/templates') },
  /** 只读检测各模板依赖的模型文件是否存在（不下载）。 */
  modelCheck() { return request<ComfyModelCheckResult>('/api/comfy/model-check') },
  template(id: string) { return request<{ ok:boolean; workflow?:Record<string, unknown>; format?:string; error?:string }>(`/api/comfy/templates/${encodeURIComponent(id)}`) },
  apply(workflow: Record<string, unknown>, parameters: Record<string, unknown>) { return postJson<{ok:boolean; workflow?:Record<string, unknown>; error?:string}>('/api/comfy/templates/apply', {workflow, parameters}) },
  jobs(page=1, pageSize=20) { return request<{ok:boolean; total:number; items:Record<string, unknown>[]}>(`/api/comfy/jobs?page=${page}&page_size=${pageSize}`) },
  retry(promptId: string, url = 'http://127.0.0.1:8188') { return postJson<{ok:boolean; response?:Record<string, unknown>; retry_count?:number; error?:string}>(`/api/comfy/retry/${encodeURIComponent(promptId)}?url=${encodeURIComponent(url)}`, {}) },
  status(url = 'http://127.0.0.1:8188') { return request<{ ok: boolean; available: boolean; url: string; error?: string }>(`/api/comfy/status?url=${encodeURIComponent(url)}`) },
  /** 提交成功后租约 reown 为 comfyui:{prompt_id}，整作业周期持有；watch 仅轮询不持租约 */
  queue(workflow: Record<string, unknown>, url = 'http://127.0.0.1:8188') { return postJson<{ ok: boolean; response?: Record<string, unknown>; workflow_sha256?: string; watch?: { ok: boolean; job?: ComfyWatchJob }; lease?: { owner: string; ttl: number; gpu: number | null; evicted: boolean }; error?: string }>('/api/comfy/queue', { url, workflow }) },
  history(promptId: string, url = 'http://127.0.0.1:8188') { return request<{ ok: boolean; done?: boolean; finished?: boolean; failed?: boolean; lease_released?: boolean; progress?: { executed_nodes: number; total_nodes: number; percent: number }; outputs?: { filename?: string; subfolder?: string; type?: string; preview_url?: string; mime?: string; asset_kind?: 'media'|'3d'|'unknown'; preview_supported?: boolean }[]; status?: Record<string, unknown>; error?: string }>(`/api/comfy/history/${encodeURIComponent(promptId)}?url=${encodeURIComponent(url)}`) },
  wait(promptId: string, url = 'http://127.0.0.1:8188', timeout = 120, interval = 1.0) { return request<{ ok: boolean; timeout?: boolean; error?: string }>(`/api/comfy/wait/${encodeURIComponent(promptId)}?url=${encodeURIComponent(url)}&timeout=${timeout}&interval=${interval}`) },
  watch(promptId: string, url = 'http://127.0.0.1:8188', timeout = 900, interval = 1.0) { return postJson<{ ok: boolean; job?: ComfyWatchJob; error?: string }>(`/api/comfy/watch/${encodeURIComponent(promptId)}?url=${encodeURIComponent(url)}&timeout=${timeout}&interval=${interval}`, {}) },
  watchStatus(promptId: string) { return request<{ ok: boolean; job?: ComfyWatchJob | null }>(`/api/comfy/watch/${encodeURIComponent(promptId)}`) },
  cancel(promptId: string, url = 'http://127.0.0.1:8188') { return postJson<{ ok: boolean; prompt_id: string; deleted: boolean; interrupted: boolean; lease_released: boolean; error?: string }>('/api/comfy/cancel', { url, prompt_id: promptId }) },
  duplicates(directory = 'assets/generated') { return request<{ ok: boolean; directory: string; groups: { sha256: string; paths: string[]; duplicate_count: number }[]; duplicate_files: number; files: number }>(`/api/comfy/resources/duplicates?directory=${encodeURIComponent(directory)}`) },
  unused(directory = 'assets/generated') { return request<{ ok: boolean; directory: string; unused: string[]; files: number; unused_count: number }>(`/api/comfy/resources/unused?directory=${encodeURIComponent(directory)}`) },
  import(promptId: string, image: Record<string, unknown>, url = 'http://127.0.0.1:8188', destDir = 'assets/generated') { return postJson<{ ok: boolean; path?: string; error?: string }>('/api/comfy/import', { prompt_id: promptId, image, url, dest_dir: destDir }) },
  importAll(promptId: string, images: Record<string, unknown>[], url = 'http://127.0.0.1:8188', destDir = 'assets/generated') { return postJson<{ ok: boolean; imported: number; results: { ok: boolean; path?: string; error?: string }[] }>('/api/comfy/import-all', { prompt_id: promptId, images, url, dest_dir: destDir }) },
  validateProvenance(meta: Record<string, unknown>) { return postJson<{ok:boolean;errors:string[];review_required:boolean}>('/api/comfy/provenance/validate', meta) },
}

/** 素材中心（asset_sources.py / /api/assets/*，阶段 2） */
export type AssetKind = 'model' | 'texture' | 'hdri' | 'image' | 'audio' | '2d' | 'animation' | 'other'
export interface AssetSourceInfo {
  key: string; name: string; mode: 'remote' | 'pack'
  kinds: string[]; license: string; home: string
}
export interface ExternalSource { key: string; name: string; url: string }
export interface AssetItem {
  id: string; source: string; kind: AssetKind; name: string
  author: string; license: string; page_url: string; thumb_url: string
  tags: string[]; summary: string
}
export interface AssetOption { label: string; ext: string; size: number; url: string; md5: string }
export interface KenneyPack {
  slug: string; name: string; kinds: string[]; summary: string
  source: string; license: string; page_url: string; thumb_url: string
}
export interface PackFile {
  path: string; show_path: string; ext: string; kind: AssetKind
  size: number; dep: boolean
}
export interface PackPeek {
  ok: boolean; token: string; name: string; thumb: string; page_url: string
  files: PackFile[]; prefix: string; cached: boolean; error?: string
}
export interface ImportResult {
  ok: boolean; path?: string; kind?: string; size?: number; sha256?: string
  license?: string; author?: string; error?: string
}
export interface PackImportResp {
  ok: boolean; imported: ImportResult[]; skipped: ImportResult[]
  imported_count: number; skipped_count: number; error?: string
}
export interface LibraryItem {
  path: string; name: string; kind: AssetKind; size: number; mtime: number
  source: string; author: string; license: string; imported_at: string; duplicate: boolean
  /** 仅离线演示数据使用：内联占位缩略图（真实接口不返回） */
  thumb?: string
  /** kind === 'animation' 时的精灵播放元数据 */
  fps?: number; frame_count?: number; cols?: number; rows?: number
  frame_width?: number; frame_height?: number
  frames_dir?: string; first_frame?: string; prompt?: string; manifest?: string
}
export interface LibraryResp {
  ok: boolean; total: number
  dirs: { dir: string; items: LibraryItem[] }[]; error?: string
}

export const assetsApi = {
  sources() {
    return request<{ ok: boolean; sources: AssetSourceInfo[]; external: ExternalSource[] }>('/api/assets/sources')
  },
  polySearch(q: string, kind: string, page: number) {
    return request<{ ok: boolean; page: number; has_more: boolean; items: AssetItem[]; error?: string }>(
      `/api/assets/search?source=polyhaven&kind=${encodeURIComponent(kind)}&page=${page}&q=${encodeURIComponent(q)}`)
  },
  resolve(id: string, kind: string) {
    return request<{ ok: boolean; options: AssetOption[]; error?: string }>(
      `/api/assets/resolve?id=${encodeURIComponent(id)}&kind=${encodeURIComponent(kind)}`)
  },
  importItem(payload: { source: string; item_id: string; option: AssetOption; kind: string;
                        dest_dir: string; author: string; source_url: string }) {
    return postJson<ImportResult>('/api/assets/import', payload)
  },
  packs(q = '', kinds = '') {
    return request<{ ok: boolean; items: KenneyPack[] }>(
      `/api/assets/packs?q=${encodeURIComponent(q)}&kinds=${encodeURIComponent(kinds)}`)
  },
  packPeek(slug: string) {
    return postJson<PackPeek>('/api/assets/packs/peek', { slug })
  },
  packImport(token: string, selected: string[], destRoot: string) {
    return postJson<PackImportResp>('/api/assets/packs/import',
      { token, selected, dest_root: destRoot })
  },
  packPreviewUrl(token: string, file: string) {
    return `/api/assets/packs/preview?token=${encodeURIComponent(token)}&file=${encodeURIComponent(file)}`
  },
  createPlaceholder(path: string, kind: 'text' | 'json' | 'svg') {
    return postJson<{ ok: boolean; resource?: {
      path: string; mime: string; size: number; extension: string
    }; error?: string }>('/api/create_placeholder', { path, kind })
  },
  library() {
    return request<LibraryResp>('/api/assets/library')
  },
  rawUrl(path: string) {
    return `/api/assets/raw?path=${encodeURIComponent(path)}`
  },
  proxyUrl(url: string) {
    return `/api/assets/proxy?url=${encodeURIComponent(url)}`
  },
}

/** 本地 AI 生成（asset_gen.py / /api/assets/generate/*，阶段 5） */
export interface GenStatus {
  ok: boolean
  online: boolean
  online_error: string
  comfy_root: string
  models_dir: string
  image: { ready: boolean; missing: string[] }
  video: { ready: boolean; missing: string[] }
  workflows: { i2v: string; t2v: string }
}
export interface GenJob {
  id: string
  type: 'image' | 'animation'
  status: 'queued' | 'running' | 'completed' | 'failed' | 'canceling'
  phase: string
  progress: number
  prompt_id: string
  prompt: string
  result: Record<string, unknown> | null
  error: string
  created_at: string
  cancel_requested?: boolean
  /** 云端任务自带：engine='cloud'，并带服务商与模型 */
  engine?: 'local' | 'cloud'
  provider?: string
  model?: string
}
export interface GenSubmitResp { ok: boolean; job_id?: string; error?: string }

export const genApi = {
  status() {
    return request<GenStatus>('/api/assets/generate/status')
  },
  image(p: { prompt: string; negative_prompt: string; width: number; height: number;
             steps: number; seed: number; batch_size: number }) {
    return postJson<GenSubmitResp>('/api/assets/generate/image', p)
  },
  animation(p: { prompt: string; duration: number; seed: number; fps: number;
                 max_frames: number; turbo: boolean;
                 first_frame_path?: string; first_frame_name?: string }) {
    return postJson<GenSubmitResp>('/api/assets/generate/animation', p)
  },
  async uploadFrame(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    const r = await fetch('/api/assets/generate/upload-frame', withProject({ method: 'POST', body: fd }))
    const body = await r.json().catch(() => ({})) as { ok?: boolean; name?: string; error?: string }
    if (!r.ok || !body.ok) throw new FsApiError(r.status, body.error || '首帧上传失败')
    return body.name as string
  },
  jobs() {
    return request<{ ok: boolean; jobs: GenJob[] }>('/api/assets/generate/jobs')
  },
  job(id: string) {
    return request<{ ok: boolean; job: GenJob; error?: string }>(
      `/api/assets/generate/jobs/${encodeURIComponent(id)}`)
  },
  cancel(id: string) {
    return postJson<{ ok: boolean; error?: string }>(
      `/api/assets/generate/jobs/${encodeURIComponent(id)}/cancel`, {})
  },
}

/** 云端 AI 生成（cloud_gen.py / /api/assets/cloud/*，用户自带 Key） */
export interface CloudModelInfo { id: string; label: string; i2v?: boolean }
export interface CloudProvider {
  id: string
  name: string
  base_url: string
  adapter: string
  key_url: string
  doc_url: string
  note: string
  image_models: CloudModelInfo[]
  video_models: CloudModelInfo[]
}
export interface CloudKeyState { saved: boolean; mask: string }

export const cloudGenApi = {
  providers() {
    return request<{ ok: boolean; providers: CloudProvider[] }>('/api/assets/cloud/providers')
  },
  keys() {
    return request<{ ok: boolean; keys: Record<string, CloudKeyState> }>('/api/assets/cloud/keys')
  },
  saveKey(provider: string, key: string) {
    return postJson<{ ok: boolean; error?: string }>('/api/assets/cloud/key', { provider, key })
  },
  deleteKey(provider: string) {
    return postJson<{ ok: boolean; error?: string }>('/api/assets/cloud/key/delete', { provider, key: '' })
  },
  image(p: { provider: string; model: string; prompt: string; negative_prompt?: string;
              width: number; height: number; seed?: number; batch?: number;
              api_key?: string; base_url?: string }) {
    return postJson<GenSubmitResp>('/api/assets/cloud/image', p)
  },
  animation(p: { provider: string; model: string; prompt: string; first_frame_path?: string;
                  duration: number; seed?: number; fps: number; max_frames: number;
                  api_key?: string; base_url?: string }) {
    return postJson<GenSubmitResp>('/api/assets/cloud/animation', p)
  },
  async uploadFrame(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    const r = await fetch('/api/assets/cloud/upload-frame', withProject({ method: 'POST', body: fd }))
    const body = await r.json().catch(() => ({})) as { ok?: boolean; path?: string; error?: string }
    if (!r.ok || !body.ok) throw new FsApiError(r.status, body.error || '首帧上传失败')
    return body.path as string
  },
}

/** GPU 协调器（gpu_coordinator.py / /api/gpu/*） */
