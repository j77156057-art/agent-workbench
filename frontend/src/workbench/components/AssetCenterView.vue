<script setup lang="ts">
// 素材中心（阶段 2）：Poly Haven 单件素材 + Kenney 素材包真实下载入库 + 我的素材库。
import { computed, onMounted, reactive, ref } from 'vue'
import { assetsApi, FsApiError } from '../api'
import type {
  AssetItem, AssetKind, AssetOption, ExternalSource, KenneyPack,
  LibraryItem, LibraryResp, PackFile, PackPeek,
} from '../api'
import { useWorkbench } from '../composables/workbench'
import {
  demoMode, demoAssetResults, demoKenneyPacks, demoPackFiles, demoAssetLibrary,
} from '../composables/demo'
import ModelPreview from './ModelPreview.vue'
import AssetGeneratePanel from './AssetGeneratePanel.vue'
import SpritePlayer from './SpritePlayer.vue'

const { revealPath, loadTree } = useWorkbench()

type WebTab = 'web' | 'library' | 'generate'
type SourceKey = 'polyhaven' | 'kenney'
const tab = ref<WebTab>('web')
const source = ref<SourceKey>('polyhaven')
const external = ref<ExternalSource[]>([])
const moreOpen = ref(false)

// ---------------------------------------------------------------- 通用
function fmtSize(n: number): string {
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  if (n >= 1024) return Math.round(n / 1024) + ' KB'
  return n + ' B'
}
const toast = ref('')
let toastTimer: number | undefined
function showToast(msg: string) {
  toast.value = msg
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 3200)
}
function kindLabel(k: string): string {
  return ({ model: '3D 模型', texture: '贴图', hdri: 'HDRI', image: '图片', '2d': '2D 素材', audio: '音效', animation: '帧动画' } as Record<string, string>)[k] || '文件'
}
function kindBadgeClass(k: string): string {
  return 'ac-kind-' + k
}
const KIND_BADGE: Record<string, string> = {
  model: '3D', texture: '贴图', hdri: 'HDR', image: '图', '2d': '2D', audio: '音', animation: '动画', other: '',
}

// ---------------------------------------------------------------- Poly Haven
const POLY_KINDS: { key: AssetKind; name: string }[] = [
  { key: 'model', name: '3D 模型' },
  { key: 'texture', name: 'PBR 贴图' },
  { key: 'hdri', name: 'HDRI 环境' },
]
const polyKind = ref<AssetKind>('model')
const polyQuery = ref('')
const polyItems = ref<AssetItem[]>([])
const polyPage = ref(1)
const polyHasMore = ref(false)
const polyLoading = ref(false)
const polyError = ref('')

async function loadPoly(reset: boolean) {
  if (reset) { polyPage.value = 1; polyItems.value = [] }
  polyLoading.value = true
  polyError.value = ''
  try {
    if (demoMode.value) {
      const all = demoAssetResults.filter((it) => it.kind === polyKind.value)
      const q = polyQuery.value.trim().toLowerCase()
      const hit = q ? all.filter((it) => (it.name + it.summary + it.tags.join()).toLowerCase().includes(q)) : all
      polyItems.value = reset ? hit : [...polyItems.value, ...hit]
      polyHasMore.value = false
      return
    }
    const r = await assetsApi.polySearch(polyQuery.value.trim(), polyKind.value, polyPage.value)
    if (!r.ok) { polyError.value = r.error || '加载失败'; return }
    polyItems.value = reset ? r.items : [...polyItems.value, ...r.items]
    polyHasMore.value = r.has_more
  } catch (e) {
    polyError.value = (e as FsApiError).message
  } finally {
    polyLoading.value = false
  }
}
function searchPoly() { void loadPoly(true) }
function loadMorePoly() { polyPage.value += 1; void loadPoly(false) }
function switchPolyKind(k: AssetKind) {
  if (polyKind.value === k) return
  polyKind.value = k
  void loadPoly(true)
}

// 单件详情抽屉
interface DetailState {
  item: AssetItem
  options: AssetOption[]
  optIndex: number
  destDir: string
  importing: boolean
  importedPath: string
  error: string
}
const detail = ref<DetailState | null>(null)

function destDirsFor(kind: AssetKind): string[] {
  const sub = kind === 'model' ? 'models' : kind === 'texture' ? 'textures' : 'hdri'
  return [`assets/${sub}`, `values/assets/${sub}`, `assets`]
}
async function openDetail(item: AssetItem) {
  detail.value = {
    item, options: [], optIndex: 0, destDir: destDirsFor(item.kind)[0],
    importing: false, importedPath: '', error: '',
  }
  if (demoMode.value) {
    detail.value.options = [
      { label: 'glb · 2k · glb', ext: '.glb', size: 248320, url: item.thumb_url, md5: '' },
      { label: 'fbx · 2k · fbx', ext: '.fbx', size: 312000, url: '#', md5: '' },
    ]
    return
  }
  try {
    const r = await assetsApi.resolve(item.id, item.kind)
    if (!r.ok || !r.options.length) {
      detail.value.error = r.error || '该素材没有可导入的文件格式'
      return
    }
    detail.value.options = r.options
  } catch (e) {
    detail.value.error = (e as FsApiError).message
  }
}
function closeDetail() { if (!detail.value?.importing) detail.value = null }
const curOption = computed<AssetOption | null>(() => {
  const d = detail.value
  return d && d.options.length ? d.options[d.optIndex] ?? d.options[0] : null
})
/** model-viewer 只支持 glb；其他格式显示缩略图 + 说明。 */
const detailPreviewGlb = computed(() => curOption.value?.ext === '.glb')
function thumbUrl(u: string): string {
  if (!u) return ''
  if (u.startsWith('data:') || u.startsWith('#')) return u
  return assetsApi.proxyUrl(u)
}

async function doImport() {
  const d = detail.value
  const opt = curOption.value
  if (!d || !opt) return
  if (demoMode.value) {
    showToast('示例演示模式不会产生真实下载；连接本地项目后可真实导入 CC0 素材')
    return
  }
  d.importing = true
  d.error = ''
  try {
    const r = await assetsApi.importItem({
      source: 'polyhaven', item_id: d.item.id, option: opt, kind: d.item.kind,
      dest_dir: d.destDir, author: d.item.author, source_url: d.item.page_url,
    })
    if (!r.ok) { d.error = r.error || '导入失败'; return }
    d.importedPath = r.path || ''
    showToast(`已导入：${r.path}`)
    void loadTree()
    libLoaded.value = false
  } catch (e) {
    d.error = (e as FsApiError).message
  } finally {
    d.importing = false
  }
}

// ---------------------------------------------------------------- Kenney
const packs = ref<KenneyPack[]>([])
const packKind = ref('')
const packQuery = ref('')
const packThumbs = reactive<Record<string, string>>({})
const PACK_KINDS = [
  { key: '', name: '全部' }, { key: 'model', name: '3D 模型' },
  { key: 'texture', name: '贴图' }, { key: '2d', name: '2D' }, { key: 'audio', name: '音效' },
]
const filteredPacks = computed(() => {
  const q = packQuery.value.trim().toLowerCase()
  return packs.value.filter((p) => {
    if (packKind.value && !p.kinds.includes(packKind.value)) return false
    if (q && !(p.name + p.summary + p.slug).toLowerCase().includes(q)) return false
    return true
  })
})
async function loadPacks() {
  if (demoMode.value) { packs.value = demoKenneyPacks as KenneyPack[]; return }
  try {
    const r = await assetsApi.packs()
    packs.value = r.items
  } catch { /* 顶部列表失败保留空，错误由 peek 时再报 */ }
}

interface PeekState {
  pack: KenneyPack
  loading: boolean
  data: PackPeek | null
  selected: Set<string>
  activeFile: PackFile | null
  destRoot: string
  importing: boolean
  resultMsg: string
  error: string
}
const peek = ref<PeekState | null>(null)

async function openPack(pack: KenneyPack) {
  peek.value = {
    pack, loading: true, data: null, selected: new Set(), activeFile: null,
    destRoot: 'assets', importing: false, resultMsg: '', error: '',
  }
  if (demoMode.value) {
    window.setTimeout(() => {
      if (peek.value) {
        peek.value.loading = false
        peek.value.data = { ok: true, token: 'demo', name: pack.name, thumb: pack.thumb_url,
          page_url: pack.page_url, files: demoPackFiles as PackFile[], prefix: '', cached: false }
      }
    }, 500)
    return
  }
  try {
    const data = await assetsApi.packPeek(pack.slug)
    if (!data.ok) { peek.value.error = data.error || '素材包打开失败'; peek.value.loading = false; return }
    peek.value.data = data
    if (data.thumb) packThumbs[pack.slug] = assetsApi.proxyUrl(data.thumb)
  } catch (e) {
    peek.value.error = (e as FsApiError).message
  } finally {
    if (peek.value) peek.value.loading = false
  }
}
function closePeek() { if (!peek.value?.importing) peek.value = null }

interface DirGroup { dir: string; files: PackFile[] }
const peekGroups = computed<DirGroup[]>(() => {
  const files = peek.value?.data?.files ?? []
  const map = new Map<string, PackFile[]>()
  for (const f of files) {
    if (f.ext === '.txt' || f.ext === '.md') continue
    const dir = f.show_path.includes('/') ? f.show_path.split('/').slice(0, -1).join('/') : '(根目录)'
    if (!map.has(dir)) map.set(dir, [])
    map.get(dir)!.push(f)
  }
  return [...map.entries()].map(([dir, fs]) => ({ dir, files: fs }))
})

function toggleFile(path: string) {
  const sel = peek.value!.selected
  if (sel.has(path)) sel.delete(path); else sel.add(path)
}
function dirChecked(g: DirGroup): boolean {
  return g.files.every((f) => peek.value!.selected.has(f.path))
}
function toggleDir(g: DirGroup) {
  const sel = peek.value!.selected
  const on = !dirChecked(g)
  for (const f of g.files) {
    if (on && f.ext !== '.bin') sel.add(f.path)
    if (!on) sel.delete(f.path)
  }
}
const selectedCount = computed(() => peek.value?.selected.size ?? 0)

function previewable(f: PackFile): boolean {
  return ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.glb', '.wav', '.ogg', '.mp3'].includes(f.ext)
}
function pickPreview(f: PackFile) {
  if (previewable(f)) peek.value!.activeFile = f
}
function packFileUrl(f: PackFile): string {
  if (demoMode.value) return f.path.endsWith('.glb') ? '' : thumbPlaceholder(f)
  return assetsApi.packPreviewUrl(peek.value!.data!.token, f.path)
}
function thumbPlaceholder(f: PackFile): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="140"><rect width="200" height="140" fill="#eef2f8"/>`
    + `<text x="100" y="76" text-anchor="middle" font-family="sans-serif" font-size="15" fill="#8ba0c0">${f.ext.slice(1).toUpperCase()}</text></svg>`
  return 'data:image/svg+xml,' + encodeURIComponent(svg)
}

async function doPackImport() {
  const st = peek.value
  if (!st || !st.data || st.importing) return
  const selected = [...st.selected]
  if (!selected.length) { showToast('请先勾选要导入的文件'); return }
  if (demoMode.value) {
    st.resultMsg = `示例演示：已选择 ${selected.length} 个文件。连接本地项目后将真实下载并写入 ${st.destRoot}/${st.pack.name}/`
    showToast('示例演示模式不会产生真实下载')
    return
  }
  st.importing = true
  st.resultMsg = ''
  try {
    const r = await assetsApi.packImport(st.data.token, selected, st.destRoot)
    if (r.ok && !r.skipped_count) {
      st.resultMsg = `成功导入 ${r.imported_count} 个文件到 ${st.destRoot}/${st.pack.name}/`
    } else if (r.imported_count) {
      st.resultMsg = `导入 ${r.imported_count} 个，跳过 ${r.skipped_count} 个（多为内容重复）`
    } else {
      st.resultMsg = r.skipped[0]?.error || '没有文件被导入（可能已全部存在）'
    }
    showToast(st.resultMsg)
    void loadTree()
    libLoaded.value = false
  } catch (e) {
    st.resultMsg = '导入失败：' + (e as FsApiError).message
  } finally {
    st.importing = false
  }
}

// ---------------------------------------------------------------- 我的素材库
const libDirs = ref<LibraryResp['dirs']>([])
const libTotal = ref(0)
const libLoading = ref(false)
const libLoaded = ref(false)
const libPreview = ref<LibraryItem | null>(null)

async function loadLibrary(force = false) {
  if (libLoading.value || (libLoaded.value && !force)) return
  libLoading.value = true
  try {
    if (demoMode.value) {
      const map = new Map<string, LibraryItem[]>()
      for (const it of demoAssetLibrary as LibraryItem[]) {
        const d = it.path.split('/').slice(0, -1).join('/')
        if (!map.has(d)) map.set(d, [])
        map.get(d)!.push(it)
      }
      libDirs.value = [...map.entries()].map(([dir, items]) => ({ dir, items }))
      libTotal.value = demoAssetLibrary.length
      libLoaded.value = true
      return
    }
    const r = await assetsApi.library()
    if (!r.ok) throw new Error(r.error)
    libDirs.value = r.dirs
    libTotal.value = r.total
    libLoaded.value = true
  } catch (e) {
    showToast('素材库加载失败：' + (e as FsApiError).message)
  } finally {
    libLoading.value = false
  }
}
function libThumb(it: LibraryItem): string {
  if (it.thumb) return it.thumb
  return assetsApi.rawUrl(it.path)
}
function isImageKind(k: string): boolean { return k === 'image' || k === 'texture' }
/** 卡片可直接显示缩略图的类型（帧动画用 SpriteSheet 当封面） */
function hasThumb(it: LibraryItem): boolean { return isImageKind(it.kind) || it.kind === 'animation' }
function sourceLabel(s: string): string {
  return ({ polyhaven: 'Poly Haven', kenney: 'Kenney', comfyui: 'ComfyUI', 'comfyui-h3': 'ComfyUI H3' } as Record<string, string>)[s] || '本地'
}
function locateInTree(it: LibraryItem) {
  revealPath(it.path)
  showToast(`已在左侧文件树定位：${it.path}`)
}
async function switchTab(t: WebTab) {
  tab.value = t
  moreOpen.value = false
  if (t === 'library') await loadLibrary()
}
/** AI 生成面板完成新图/新动画后，让素材库下次展示最新内容 */
function onGenDone() {
  libLoaded.value = false
  void loadTree()
  if (tab.value === 'library') void loadLibrary()
}

function openExternal(e: ExternalSource) {
  window.open(e.url, '_blank', 'noopener')
  moreOpen.value = false
}

onMounted(() => {
  void loadPacks()
  void loadPoly(true)
  assetsApi.sources().then((r) => { external.value = r.external }).catch(() => undefined)
})
</script>

<template>
  <div class="ac-view" @click.self="moreOpen = false">
    <!-- 顶部：来源 + 子页签 -->
    <div class="ac-head">
      <div class="ac-source-seg">
        <button
          type="button" class="ac-seg" :class="{ on: tab === 'web' && source === 'polyhaven' }"
          @click="tab = 'web'; source = 'polyhaven'"
        >Poly Haven</button>
        <button
          type="button" class="ac-seg" :class="{ on: tab === 'web' && source === 'kenney' }"
          @click="tab = 'web'; source = 'kenney'"
        >Kenney 素材包</button>
        <div class="ac-more-wrap">
          <button type="button" class="ac-seg ac-more-btn" @click.stop="moreOpen = !moreOpen">
            更多<svg width="9" height="9" viewBox="0 0 9 9" aria-hidden="true"><path d="M1.5 3 L4.5 6 L7.5 3" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
          </button>
          <div v-if="moreOpen" class="ac-more-menu" @click.stop>
            <p class="ac-more-title">其他 CC0 素材站（浏览器打开）</p>
            <button v-for="e in external" :key="e.key" type="button" class="ac-more-item" @click="openExternal(e)">
              {{ e.name }}
            </button>
          </div>
        </div>
      </div>
      <div class="ac-sub-seg">
        <button type="button" class="ac-sub" :class="{ on: tab === 'web' }" @click="switchTab('web')">网络素材</button>
        <button type="button" class="ac-sub" :class="{ on: tab === 'library' }" @click="switchTab('library')">
          我的素材库<span v-if="libTotal" class="ac-sub-count">{{ libTotal }}</span>
        </button>
        <button type="button" class="ac-sub" :class="{ on: tab === 'generate' }" @click="switchTab('generate')">
          AI 生成
        </button>
      </div>
    </div>

    <!-- ============================ 网络素材 ============================ -->
    <template v-if="tab === 'web'">
      <!-- Poly Haven 工具条 -->
      <div v-if="source === 'polyhaven'" class="ac-filterbar">
        <div class="ac-kind-seg">
          <button
            v-for="k in POLY_KINDS" :key="k.key" type="button"
            class="ac-kind" :class="{ on: polyKind === k.key }"
            @click="switchPolyKind(k.key)"
          >{{ k.name }}</button>
        </div>
        <div class="ac-search">
          <input
            v-model="polyQuery" type="search" placeholder="搜索素材（英文关键词，如 chair / wood / studio）"
            @keydown.enter="searchPoly"
          >
          <button type="button" class="ac-search-btn" :disabled="polyLoading" @click="searchPoly">搜索</button>
        </div>
        <span class="ac-license-hint">Poly Haven · 全部 CC0 免费可商用 · 免注册</span>
      </div>

      <!-- Kenney 工具条 -->
      <div v-else class="ac-filterbar">
        <div class="ac-kind-seg">
          <button
            v-for="k in PACK_KINDS" :key="k.key" type="button"
            class="ac-kind" :class="{ on: packKind === k.key }"
            @click="packKind = k.key"
          >{{ k.name }}</button>
        </div>
        <div class="ac-search">
          <input v-model="packQuery" type="search" placeholder="筛选素材包（原型 / 城市 / UI…）">
        </div>
        <span class="ac-license-hint">Kenney · 全部 CC0 · 整包下载后按需勾选入库（本地缓存最近 3 个包）</span>
      </div>

      <div class="ac-scroll">
        <!-- Poly 卡片网格 -->
        <div v-if="source === 'polyhaven'" class="ac-grid">
          <button
            v-for="it in polyItems" :key="it.id" type="button" class="ac-card"
            @click="openDetail(it)"
          >
            <div class="ac-thumb">
              <img v-if="it.thumb_url" :src="thumbUrl(it.thumb_url)" :alt="it.name" loading="lazy">
              <span class="ac-thumb-badge" :class="kindBadgeClass(it.kind)">{{ KIND_BADGE[it.kind] }}</span>
            </div>
            <div class="ac-card-body">
              <p class="ac-card-name" :title="it.name">{{ it.name }}</p>
              <p class="ac-card-meta">{{ it.author || '佚名' }} · CC0</p>
            </div>
          </button>
        </div>

        <!-- Kenney 包卡片 -->
        <div v-else class="ac-grid">
          <div v-for="p in filteredPacks" :key="p.slug" class="ac-card ac-pack-card">
            <div class="ac-thumb">
              <img v-if="packThumbs[p.slug] || p.thumb_url" :src="packThumbs[p.slug] || p.thumb_url" :alt="p.name" loading="lazy">
              <span v-else class="ac-thumb-ph">{{ p.name.slice(0, 1) }}</span>
              <span class="ac-thumb-badge ac-kind-2d">{{ p.kinds.map((k) => KIND_BADGE[k]).filter(Boolean).join('/') }}</span>
            </div>
            <div class="ac-card-body">
              <p class="ac-card-name" :title="p.name">{{ p.name }}</p>
              <p class="ac-card-meta ac-pack-summary">{{ p.summary }}</p>
              <button type="button" class="ac-pack-open" @click="openPack(p)">打开素材包</button>
            </div>
          </div>
          <p v-if="!filteredPacks.length" class="ac-empty">没有匹配的素材包。</p>
        </div>

        <!-- 状态行 -->
        <div v-if="source === 'polyhaven'" class="ac-grid-foot">
          <span v-if="polyLoading" class="ac-loading">加载中…</span>
          <span v-else-if="polyError" class="ac-err">{{ polyError }}</span>
          <span v-else-if="!polyItems.length" class="ac-empty">没有找到素材，换个关键词试试。</span>
          <button v-if="polyHasMore && !polyLoading" type="button" class="ac-more-load" @click="loadMorePoly">加载更多</button>
        </div>
      </div>
    </template>

    <!-- ============================ AI 生成（本地 ComfyUI） ============================ -->
    <AssetGeneratePanel v-else-if="tab === 'generate'" @done="onGenDone" />

    <!-- ============================ 我的素材库 ============================ -->
    <div v-else class="ac-scroll">
      <div class="ac-lib-head">
        <span v-if="!libLoading" class="ac-lib-count">共 {{ libTotal }} 个素材（含 ComfyUI 生成物）</span>
        <button v-if="libLoaded" type="button" class="ac-refresh" @click="loadLibrary(true)">刷新</button>
      </div>
      <p v-if="libLoading" class="ac-loading">正在扫描素材库…</p>
      <p v-else-if="!libDirs.length" class="ac-empty">
        素材库还是空的。到「网络素材」挑 CC0 模型/贴图，或用 ComfyUI 生成图片，都会出现在这里。
      </p>
      <section v-for="g in libDirs" :key="g.dir" class="ac-lib-group">
        <h3 class="ac-lib-dir">
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M1 3 Q1 2.2 1.8 2.2 H4.6 L5.6 3.2 H10.2 Q11 3.2 11 4 V9 Q11 9.8 10.2 9.8 H1.8 Q1 9.8 1 9 Z" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>
          {{ g.dir }} <i>{{ g.items.length }}</i>
        </h3>
        <div class="ac-grid">
          <div v-for="it in g.items" :key="it.path" class="ac-card ac-lib-card">
            <div class="ac-thumb">
              <template v-if="hasThumb(it)">
                <img :src="libThumb(it)" :alt="it.name" loading="lazy">
              </template>
              <span v-else class="ac-thumb-ph ac-thumb-type">{{ KIND_BADGE[it.kind] || '·' }}</span>
              <span class="ac-thumb-badge" :class="kindBadgeClass(it.kind)">{{ KIND_BADGE[it.kind] }}</span>
              <span v-if="it.duplicate" class="ac-dup-badge" title="素材库中存在内容完全相同的文件">重复</span>
            </div>
            <div class="ac-card-body">
              <p class="ac-card-name" :title="it.path">{{ it.name }}</p>
              <p class="ac-card-meta">
                <template v-if="it.kind === 'animation'">
                  {{ it.fps }}fps · {{ it.frame_count }}帧 · {{ sourceLabel(it.source) }}
                </template>
                <template v-else>{{ fmtSize(it.size) }} · {{ sourceLabel(it.source) }}</template>
              </p>
              <div class="ac-lib-actions">
                <button v-if="it.kind === 'model' || it.kind === 'audio' || it.kind === 'animation'" type="button" class="ac-mini-btn" @click="libPreview = it">预览</button>
                <button type="button" class="ac-mini-btn" @click="locateInTree(it)">文件树定位</button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- ============================ Poly 详情抽屉 ============================ -->
    <div v-if="detail" class="ac-drawer-mask wb-modal-backdrop" @click.self="closeDetail">
      <aside class="ac-drawer wb-modal-shell">
        <header class="ac-drawer-head">
          <p class="ac-drawer-title">{{ detail.item.name }}</p>
          <button type="button" class="ac-x" @click="closeDetail" aria-label="关闭">×</button>
        </header>
        <div class="ac-drawer-body">
          <div class="ac-drawer-preview">
            <ModelPreview
              v-if="detailPreviewGlb && curOption && !demoMode"
              :src="curOption.url"
              :poster="thumbUrl(detail.item.thumb_url)"
            />
            <img v-else-if="detail.item.thumb_url" class="ac-drawer-thumb" :src="thumbUrl(detail.item.thumb_url)" :alt="detail.item.name">
            <p v-else class="ac-loading">无预览图</p>
            <p v-if="demoMode" class="ac-preview-note">
              连接本地项目后，这里会加载真实 GLB 的可旋转 3D 预览
            </p>
            <p v-else-if="curOption && curOption.ext !== '.glb'" class="ac-preview-note">
              {{ curOption.ext }} 格式不支持网页 3D 预览，导入后可在 Godot 中打开
            </p>
          </div>

          <dl class="ac-meta-grid">
            <div><dt>来源</dt><dd>Poly Haven（<a :href="detail.item.page_url" target="_blank" rel="noopener">素材页 ↗</a>）</dd></div>
            <div><dt>作者</dt><dd>{{ detail.item.author || '佚名' }}</dd></div>
            <div><dt>许可</dt><dd class="ac-cc0">CC0 公有领域</dd></div>
            <div><dt>类型</dt><dd>{{ kindLabel(detail.item.kind) }}</dd></div>
          </dl>
          <p v-if="detail.item.summary" class="ac-summary">{{ detail.item.summary }}</p>
          <div v-if="detail.item.tags.length" class="ac-tags">
            <span v-for="t in detail.item.tags" :key="t" class="ac-tag">{{ t }}</span>
          </div>

          <p v-if="detail.error" class="ac-err ac-detail-err">{{ detail.error }}</p>

          <div class="ac-import-row">
            <label class="ac-field">
              <span>文件档位</span>
              <select v-if="detail.options.length" v-model.number="detail.optIndex">
                <option v-for="(o, i) in detail.options" :key="i" :value="i">
                  {{ o.label }}（{{ fmtSize(o.size) }}）
                </option>
              </select>
              <span v-else class="ac-loading">读取可下载档位…</span>
            </label>
            <label class="ac-field">
              <span>放入目录</span>
              <select v-model="detail.destDir">
                <option v-for="d in destDirsFor(detail.item.kind)" :key="d" :value="d">{{ d }}/</option>
              </select>
            </label>
          </div>
        </div>
        <footer class="ac-drawer-foot">
          <p v-if="detail.importedPath" class="ac-imported-ok">已导入：{{ detail.importedPath }}</p>
          <button type="button" class="ac-primary" :disabled="detail.importing || !detail.options.length" @click="doImport">
            {{ detail.importing ? '下载并入库中…' : detail.importedPath ? '再次导入（自动避让重名）' : '下载并导入项目' }}
          </button>
        </footer>
      </aside>
    </div>

    <!-- ============================ Kenney 包勾选对话框 ============================ -->
    <div v-if="peek" class="ac-modal-mask wb-modal-backdrop" @click.self="closePeek">
      <div class="ac-modal wb-modal-shell">
        <header class="ac-drawer-head">
          <div>
            <p class="ac-drawer-title">{{ peek.pack.name }}</p>
            <p class="ac-modal-sub">Kenney · CC0 · 勾选需要的文件导入，.gltf 会自动带上贴图与 .bin 依赖</p>
          </div>
          <button type="button" class="ac-x" @click="closePeek" aria-label="关闭">×</button>
        </header>

        <div v-if="peek.loading" class="ac-peek-loading">
          <span class="ac-spinner" aria-hidden="true" />
          <p>正在从 kenney.nl 下载并解压素材包，大包可能需要一两分钟…</p>
        </div>
        <p v-else-if="peek.error" class="ac-err ac-peek-loading">{{ peek.error }}</p>

        <div v-else-if="peek.data" class="ac-peek-body">
          <div class="ac-peek-files">
            <div v-for="g in peekGroups" :key="g.dir" class="ac-dir-group">
              <label class="ac-dir-head">
                <input type="checkbox" :checked="dirChecked(g)" @change="toggleDir(g)">
                <span class="ac-dir-name">{{ g.dir }}</span>
                <i>{{ g.files.length }}</i>
              </label>
              <ul class="ac-file-list">
                <li v-for="f in g.files" :key="f.path">
                  <label class="ac-file-row" :class="{ active: peek.activeFile === f }">
                    <input type="checkbox" :checked="peek.selected.has(f.path)" @change="toggleFile(f.path)">
                    <button type="button" class="ac-file-name" :disabled="!previewable(f)" @click="pickPreview(f)">
                      {{ f.show_path.split('/').pop() }}
                    </button>
                    <span class="ac-file-size">{{ fmtSize(f.size) }}</span>
                  </label>
                </li>
              </ul>
            </div>
          </div>
          <div class="ac-peek-preview">
            <template v-if="peek.activeFile">
              <img
                v-if="['.png','.jpg','.jpeg','.webp','.gif','.svg'].includes(peek.activeFile.ext)"
                :src="packFileUrl(peek.activeFile)" :alt="peek.activeFile.show_path"
              >
              <ModelPreview
                v-else-if="peek.activeFile.ext === '.glb' && !demoMode"
                :src="packFileUrl(peek.activeFile)"
              />
              <audio v-else-if="['.wav','.ogg','.mp3'].includes(peek.activeFile.ext)" controls
                :src="packFileUrl(peek.activeFile)" />
              <p v-else class="ac-loading">该文件类型不支持预览</p>
              <p class="ac-peek-preview-name">{{ peek.activeFile.show_path }}</p>
            </template>
            <p v-else class="ac-preview-tip">点左侧文件名可预览图片 / 模型 / 音效</p>
          </div>
        </div>

        <footer v-if="peek.data && !peek.loading" class="ac-modal-foot">
          <label class="ac-dest-root">
            导入到
            <select v-model="peek.destRoot">
              <option value="assets">assets/（推荐）</option>
              <option value="values/assets">values/assets/</option>
            </select>
            /{{ peek.pack.name }}/
          </label>
          <span class="ac-selected-n">已选 {{ selectedCount }} 个文件</span>
          <span v-if="peek.resultMsg" class="ac-peek-result">{{ peek.resultMsg }}</span>
          <span class="ac-foot-spacer" />
          <button type="button" class="ac-primary" :disabled="peek.importing || !selectedCount" @click="doPackImport">
            {{ peek.importing ? '导入中…' : '导入选中文件' }}
          </button>
        </footer>
      </div>
    </div>

    <!-- ============================ 素材库模型/音效预览 ============================ -->
    <div v-if="libPreview" class="ac-drawer-mask wb-modal-backdrop" @click.self="libPreview = null">
      <aside class="ac-drawer wb-modal-shell">
        <header class="ac-drawer-head">
          <p class="ac-drawer-title">{{ libPreview.name }}</p>
          <button type="button" class="ac-x" @click="libPreview = null" aria-label="关闭">×</button>
        </header>
        <div class="ac-drawer-body">
          <div class="ac-drawer-preview ac-lib-preview-box">
            <ModelPreview v-if="libPreview.kind === 'model'" :src="assetsApi.rawUrl(libPreview.path)" />
            <audio v-else-if="libPreview.kind === 'audio'" controls class="ac-audio-full" :src="assetsApi.rawUrl(libPreview.path)" />
            <SpritePlayer
              v-else-if="libPreview.kind === 'animation' && libPreview.cols && libPreview.rows"
              :src="libThumb(libPreview)" :cols="libPreview.cols" :rows="libPreview.rows"
              :frame-w="libPreview.frame_width || 0" :frame-h="libPreview.frame_height || 0"
              :fps="libPreview.fps || 12" :frames="libPreview.frame_count || 0"
            />
          </div>
          <dl class="ac-meta-grid">
            <div><dt>路径</dt><dd>{{ libPreview.path }}</dd></div>
            <div><dt>大小</dt><dd>{{ fmtSize(libPreview.size) }}</dd></div>
            <div><dt>来源</dt><dd>{{ sourceLabel(libPreview.source) }}</dd></div>
            <template v-if="libPreview.kind === 'animation'">
              <div><dt>帧率</dt><dd>{{ libPreview.fps }} fps</dd></div>
              <div><dt>帧数</dt><dd>{{ libPreview.frame_count }}（{{ libPreview.cols }}×{{ libPreview.rows }} 图集）</dd></div>
              <div><dt>单帧</dt><dd>{{ libPreview.frame_width }}×{{ libPreview.frame_height }} px</dd></div>
              <div v-if="libPreview.frames_dir"><dt>序列帧</dt><dd>{{ libPreview.frames_dir }}（frame_NNNN.png）</dd></div>
              <div v-if="libPreview.prompt"><dt>动作提示</dt><dd>{{ libPreview.prompt }}</dd></div>
            </template>
            <div v-if="libPreview.license"><dt>许可</dt><dd class="ac-cc0">{{ libPreview.license }}</dd></div>
          </dl>
        </div>
      </aside>
    </div>

    <!-- toast -->
    <Transition name="ac-toast">
      <p v-if="toast" class="ac-toast">{{ toast }}</p>
    </Transition>
  </div>
</template>

<style scoped>
.ac-view {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}

/* 头部来源/子页签 */
.ac-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 16px 8px;
}
.ac-source-seg { display: flex; align-items: center; gap: 4px; }
.ac-sub-seg { display: flex; gap: 2px; background: var(--bg-hover); border-radius: 8px; padding: 2px; }
.ac-seg, .ac-sub {
  font-family: var(--font-ui);
  cursor: pointer;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  /* 轨道压的是 --bg-hover（paper 上比页面底深一档），非选中项再用 --text-muted
     就只剩 4.34。这里让位给对比度：非选中项走 --text，选中态仍靠底色 + 描边 +
     强调字区分，层次没丢。 */
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  padding: 6px 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: background .12s, color .12s;
}
.ac-seg:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); color: var(--text); }
.ac-seg.on { background: var(--bg-raised); border-color: var(--border); color: var(--accent-text); box-shadow: 0 1px 2px rgba(35,52,84,.07); }
.ac-sub.on { background: var(--bg-raised); color: var(--accent-text); box-shadow: 0 1px 2px rgba(35,52,84,.08); }
.ac-seg:focus-visible, .ac-sub:focus-visible, .ac-kind:focus-visible,
.ac-search-btn:focus-visible, .ac-more-item:focus-visible, .ac-pack-open:focus-visible,
.ac-card:focus-visible, .ac-refresh:focus-visible, .ac-mini-btn:focus-visible,
.ac-primary:focus-visible, .ac-x:focus-visible, .ac-file-name:focus-visible,
.ac-dest-root select:focus-visible, .ac-field select:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 60%, transparent);
  outline-offset: 2px;
}
.ac-sub-count {
  display: inline-block; margin-left: 4px; min-width: 17px; padding: 0 4px;
  border-radius: 9px; background: color-mix(in srgb, var(--accent) 12%, transparent); color: var(--accent-text);
  font-size: 10px; line-height: 16px; text-align: center;
}
.ac-more-wrap { position: relative; }
.ac-more-menu {
  position: absolute; top: calc(100% + 4px); left: 0; z-index: 30;
  min-width: 220px; background: var(--bg-raised); border: 1px solid var(--border);
  border-radius: 9px; box-shadow: 0 8px 28px rgba(35,52,84,.16); padding: 6px;
  animation: ac-pop-in .16s ease-out both;
}
.ac-more-title { margin: 2px 6px 6px; font-size: 11px; color: var(--text-faint); }
.ac-more-item {
  display: block; width: 100%; text-align: left;
  border: none; background: none; cursor: pointer;
  font-family: var(--font-ui); font-size: 12px; color: var(--text);
  padding: 7px 8px; border-radius: 6px;
}
.ac-more-item:hover { background: var(--bg-hover); }

/* 过滤栏 */
.ac-filterbar {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 2px 16px 10px;
}
.ac-kind-seg { display: flex; gap: 2px; }
.ac-kind {
  border: 1px solid var(--border); background: var(--bg-raised); color: var(--text-muted);
  font-family: var(--font-ui); font-size: 12px; font-weight: 500;
  padding: 4px 11px; border-radius: 14px; cursor: pointer; transition: all .12s;
}
.ac-kind:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
.ac-kind.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.ac-search { display: flex; gap: 6px; flex: 1 1 260px; max-width: 460px; }
.ac-search input {
  flex: 1 1 auto; height: 30px; padding: 0 10px;
  border: 1px solid var(--border); border-radius: 7px; background: var(--bg-raised);
  font-family: var(--font-ui); font-size: 12px; color: var(--text);
  min-width: 0;
}
.ac-search input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent); }
.ac-search-btn {
  height: 30px; padding: 0 14px; border: none; border-radius: 7px;
  background: var(--accent); color: var(--accent-ink); font-size: 12px; font-weight: 600;
  font-family: var(--font-ui); cursor: pointer;
}
.ac-search-btn:disabled { opacity: .6; cursor: default; }
.ac-license-hint { font-size: 11px; color: var(--text-faint); margin-left: auto; white-space: nowrap; }

/* 网格 */
.ac-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 2px 16px 18px; }
.ac-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 12px;
}
.ac-card {
  display: flex; flex-direction: column; text-align: left;
  border: 1px solid var(--border); border-radius: 10px; background: var(--bg-raised);
  overflow: hidden; cursor: pointer; padding: 0;
  transition: box-shadow .14s, border-color .14s, transform .14s;
}
button.ac-card { font: inherit; color: inherit; }
.ac-card:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  box-shadow: 0 5px 18px rgba(35,52,84,.1);
  transform: translateY(-1px);
}
.ac-pack-card { cursor: default; }
.ac-pack-card:hover { transform: none; }
.ac-thumb {
  position: relative;
  aspect-ratio: 16 / 10;
  background: var(--bg-hover);
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
.ac-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ac-thumb-ph {
  font-size: 30px; font-weight: 700; color: color-mix(in srgb, var(--accent) 55%, transparent);
  font-family: var(--font-ui); user-select: none;
}
.ac-thumb-type { font-size: 18px; }
.ac-thumb-badge {
  position: absolute; left: 6px; top: 6px;
  font-size: 10px; font-weight: 700; line-height: 1;
  padding: 3px 6px; border-radius: 5px;
  background: color-mix(in srgb, var(--bg-raised) 92%, transparent); color: var(--text);
  border: 1px solid var(--border);
}
.ac-kind-model { color: #2f6fed; }
.ac-kind-texture { color: #b0742a; }
.ac-kind-hdri { color: #6d4bbf; }
.ac-kind-audio { color: #2b9a6a; }
.ac-kind-2d { color: #c0467a; }
.ac-kind-animation { color: #0f8f8f; }
.ac-dup-badge {
  position: absolute; right: 6px; bottom: 6px;
  font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 10px;
  background: color-mix(in srgb, var(--amber) 12%, transparent); color: var(--amber);
  border: 1px solid color-mix(in srgb, var(--amber) 30%, transparent);
}
.ac-card-body { padding: 8px 10px 10px; display: flex; flex-direction: column; gap: 3px; }
.ac-card-name {
  margin: 0; font-size: 12px; font-weight: 600; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ac-card-meta { margin: 0; font-size: 11px; color: var(--text-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ac-pack-summary { white-space: normal; color: var(--text-muted); line-height: 1.45; min-height: 31px; }
.ac-pack-open {
  margin-top: 6px; align-self: flex-start;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); background: color-mix(in srgb, var(--accent) 7%, transparent);
  color: var(--accent-text); border-radius: 6px; padding: 4px 12px;
  font-family: var(--font-ui); font-size: 12px; font-weight: 600; cursor: pointer;
}
.ac-pack-open:hover { background: color-mix(in srgb, var(--accent) 14%, transparent); }
.ac-grid-foot { display: flex; justify-content: center; align-items: center; gap: 14px; padding: 16px 0 4px; }
.ac-more-load {
  border: 1px solid var(--border); background: var(--bg-raised); border-radius: 7px;
  padding: 6px 18px; font-size: 12px; color: var(--accent-text); font-weight: 600;
  font-family: var(--font-ui); cursor: pointer;
}
.ac-more-load:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
.ac-loading { color: var(--text-muted); font-size: 12px; }
.ac-err { color: var(--danger); font-size: 12px; }
.ac-empty { color: var(--text-faint); font-size: 12px; padding: 30px 0; text-align: center; grid-column: 1 / -1; }

/* 素材库 */
.ac-lib-head { display: flex; align-items: center; gap: 12px; padding: 4px 0 12px; }
.ac-lib-count { font-size: 12px; color: var(--text-muted); }
.ac-refresh {
  margin-left: auto; border: 1px solid var(--border); background: var(--bg-raised);
  border-radius: 6px; padding: 4px 12px; font-size: 12px; cursor: pointer;
  font-family: var(--font-ui); color: var(--text-muted);
}
.ac-refresh:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
.ac-lib-group { margin-bottom: 18px; }
.ac-lib-dir {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 9px; font-size: 12px; font-weight: 600; color: var(--text-muted);
}
.ac-lib-dir i { font-style: normal; font-size: 11px; color: var(--text-faint); }
.ac-lib-card { cursor: default; }
.ac-lib-card:hover { transform: none; }
.ac-lib-actions { display: flex; gap: 6px; margin-top: 6px; }
.ac-mini-btn {
  border: 1px solid var(--border); background: var(--bg-raised); border-radius: 5px;
  padding: 3px 9px; font-size: 11px; cursor: pointer;
  font-family: var(--font-ui); color: var(--text-muted);
}
.ac-mini-btn:hover { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }

/* 抽屉 */
.ac-drawer-mask {
  position: absolute; inset: 0; z-index: 40;
  background: rgba(35,52,84,.28);
  display: flex; justify-content: flex-end;
}
.ac-drawer {
  width: 440px; max-width: 92%; height: 100%;
  background: var(--bg-raised); border-left: 1px solid var(--border);
  display: flex; flex-direction: column;
  box-shadow: -8px 0 30px rgba(35,52,84,.14);
  animation: ac-drawer-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.ac-drawer-head {
  flex: 0 0 auto;
  display: flex; align-items: flex-start; justify-content: space-between; gap: 10px;
  padding: 13px 16px; border-bottom: 1px solid var(--border);
}
.ac-drawer-title { margin: 0; font-size: 14px; font-weight: 700; color: var(--text); }
.ac-x {
  border: none; background: none; font-size: 20px; line-height: 1;
  color: var(--text-faint); cursor: pointer; padding: 0 2px;
}
.ac-x:hover { color: var(--text); }
.ac-drawer-body { flex: 1 1 auto; overflow-y: auto; padding: 14px 16px; }
.ac-drawer-preview {
  width: 100%; height: 240px; border: 1px solid var(--border); border-radius: 9px; overflow: hidden;
  background: var(--bg-hover); margin-bottom: 12px;
}
.ac-drawer-thumb { width: 100%; height: 100%; object-fit: contain; display: block; background: #fff; }
.ac-preview-note {
  position: absolute;
}
.ac-drawer-preview { position: relative; }
.ac-drawer-preview .ac-preview-note {
  position: static; display: block; text-align: center; padding-top: 8px;
  font-size: 11px; color: var(--text-faint); background: var(--bg-hover); margin: 0;
}
.ac-meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 0 0 10px; }
.ac-meta-grid div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.ac-meta-grid dt { font-size: 10px; color: var(--text-faint); margin: 0; }
.ac-meta-grid dd { margin: 0; font-size: 12px; color: var(--text); overflow-wrap: anywhere; }
.ac-meta-grid a { color: var(--accent); text-decoration: none; }
.ac-cc0 { color: var(--green); font-weight: 600; }
.ac-summary { margin: 4px 0 10px; font-size: 12px; line-height: 1.6; color: var(--text-muted); }
.ac-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 12px; }
.ac-tag {
  font-size: 10px; padding: 2px 8px; border-radius: 10px;
  background: var(--bg-hover); color: var(--text-muted);
}
.ac-import-row { display: flex; gap: 10px; margin-top: 6px; }
.ac-field { flex: 1 1 0; display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text-faint); }
.ac-field select {
  height: 30px; border: 1px solid var(--border); border-radius: 7px; padding: 0 8px;
  font-family: var(--font-ui); font-size: 12px; color: var(--text); background: var(--bg-raised);
}
.ac-detail-err { margin-top: 10px; }
.ac-drawer-foot {
  flex: 0 0 auto; border-top: 1px solid var(--border); padding: 12px 16px;
  display: flex; align-items: center; gap: 10px;
}
.ac-imported-ok { margin: 0; font-size: 12px; color: var(--green); flex: 1 1 auto; overflow-wrap: anywhere; }
.ac-primary {
  margin-left: auto;
  border: none; border-radius: 8px; background: var(--accent); color: var(--accent-ink);
  font-family: var(--font-ui); font-size: 12px; font-weight: 600;
  padding: 8px 18px; cursor: pointer;
}
.ac-primary:hover:not(:disabled) { background: var(--accent-deep); }
.ac-primary:disabled { opacity: .55; cursor: default; }
.ac-lib-preview-box { height: 300px; }
.ac-audio-full { width: 100%; margin: auto 20px; }

/* Kenney modal */
.ac-modal-mask {
  position: absolute; inset: 0; z-index: 42;
  background: rgba(35,52,84,.32);
  display: flex; align-items: center; justify-content: center; padding: 28px;
}
.ac-modal {
  width: 900px; max-width: 100%; height: min(640px, 88%);
  background: var(--bg-raised); border-radius: 12px; border: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 0 18px 60px rgba(35,52,84,.25);
  animation: ac-modal-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.ac-modal-sub { margin: 3px 0 0; font-size: 11px; color: var(--text-faint); }
.ac-peek-loading { flex: 1 1 auto; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 30px; text-align: center; }
.ac-spinner {
  width: 22px; height: 22px;
  border: 2.5px solid color-mix(in srgb, var(--accent) 20%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: ac-spin .8s linear infinite;
}
@keyframes ac-spin { to { transform: rotate(360deg); } }
.ac-peek-body { flex: 1 1 auto; min-height: 0; display: flex; }
.ac-peek-files { flex: 1 1 55%; overflow-y: auto; border-right: 1px solid var(--border); padding: 10px 12px; }
.ac-dir-group { margin-bottom: 12px; }
.ac-dir-head { display: flex; align-items: center; gap: 7px; cursor: pointer; font-size: 12px; font-weight: 600; color: var(--text); }
.ac-dir-head i { font-style: normal; font-size: 10px; color: var(--text-faint); font-weight: 400; }
.ac-file-list { list-style: none; margin: 5px 0 0; padding: 0 0 0 24px; }
.ac-file-row {
  display: flex; align-items: center; gap: 7px; padding: 2px 4px;
  border-radius: 5px; font-size: 12px;
}
.ac-file-row.active { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.ac-file-name {
  flex: 1 1 auto; text-align: left; border: none; background: none; padding: 0;
  font-family: var(--font-ui); font-size: 12px; color: var(--text); cursor: pointer;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ac-file-name:disabled { cursor: default; color: var(--text); }
.ac-file-name:not(:disabled):hover { color: var(--accent); text-decoration: underline; }
.ac-file-size { font-size: 10px; color: var(--text-faint); flex: 0 0 auto; }
.ac-peek-preview {
  flex: 1 1 45%; min-width: 0; padding: 14px;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  background: var(--bg);
}
.ac-peek-preview img { max-width: 100%; max-height: 100%; border: 1px solid var(--border); border-radius: 8px; object-fit: contain; }
.ac-peek-preview > :deep(.m3d-wrap) { flex: 1 1 auto; width: 100%; }
.ac-peek-preview audio { width: 100%; }
.ac-peek-preview-name { font-size: 11px; color: var(--text-muted); align-self: flex-start; word-break: break-all; }
.ac-preview-tip { font-size: 12px; color: var(--text-faint); text-align: center; }
.ac-modal-foot {
  flex: 0 0 auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  border-top: 1px solid var(--border); padding: 11px 16px;
}
.ac-dest-root { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 5px; }
.ac-dest-root select {
  border: 1px solid var(--border); border-radius: 6px; padding: 3px 6px; height: 26px;
  font-family: var(--font-ui); font-size: 12px; background: var(--bg-raised);
}
.ac-selected-n { font-size: 12px; color: var(--text-muted); }
.ac-peek-result { font-size: 12px; color: var(--green); }
.ac-foot-spacer { flex: 1 1 auto; }

/* toast */
.ac-toast {
  position: absolute; left: 50%; bottom: 26px; transform: translateX(-50%);
  z-index: 60; max-width: 70%;
  background: var(--ink); color: var(--bg); font-size: 12px; line-height: 1.5;
  padding: 9px 16px; border-radius: 9px; box-shadow: 0 8px 30px rgba(20,32,56,.3);
  margin: 0;
}
.ac-toast-enter-active, .ac-toast-leave-active { transition: opacity .18s, transform .18s; }
.ac-toast-enter-from, .ac-toast-leave-to { opacity: 0; transform: translate(-50%, 8px); }

@keyframes ac-pop-in { from { opacity: 0; transform: translateY(-4px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes ac-drawer-in { from { opacity: 0; transform: translateX(18px); } to { opacity: 1; transform: translateX(0); } }
@keyframes ac-modal-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

/* 窄屏：过滤栏换行、详情抽屉拉宽占比 */
@media (max-width: 1024px) {
  .ac-license-hint { margin-left: 0; flex-basis: 100%; order: 3; }
  .ac-peek-body { flex-direction: column; }
  .ac-peek-files { border-right: none; border-bottom: 1px solid var(--border); flex-basis: 55%; }
  .ac-peek-preview { flex-basis: 45%; }
}
@media (max-width: 720px) {
  .ac-grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
  .ac-drawer { width: 100%; max-width: 100%; }
  .ac-modal-mask { padding: 0; }
  .ac-modal { width: 100%; height: 100%; border-radius: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .ac-seg, .ac-sub, .ac-kind, .ac-card, .ac-more-menu, .ac-drawer, .ac-modal,
  .ac-spinner, .ac-toast-enter-active, .ac-toast-leave-active {
    animation: none !important;
    transition: none !important;
  }
}
</style>
