// 工作台「语义增强」域：文件业务标签、大白话定位、概览分区卡。
// 全部是增强层：加载失败不打扰编辑主流程。树/跳转依赖由 workbench 主模块
// 显式注入（initSemanticDeps），避免 composable 之间循环引用。
import { ref, shallowRef, type ShallowRef } from 'vue'
import { semanticApi } from '../api'
import type { SemanticTagRecord, LocateResp, RegionCard, TreeResp } from '../api'

interface SemanticDeps {
  tree: ShallowRef<TreeResp | null>
  revealPath(path: string): void
  jumpToLine(path: string, line: number): Promise<void>
}
let deps!: SemanticDeps
export function initSemanticDeps(d: SemanticDeps) { deps = d }

// ---------------------------------------------------------------- 语义标签
/** 路径 → 业务标签记录（供文件树徽章与定位结果展示）。 */
const tagMap = shallowRef<Record<string, SemanticTagRecord>>({})
const tagLoading = ref(false)
const tagError = ref('')
const tagMeta = ref({ total: 0, tagged: 0, pending: 0, stale: 0 })

async function loadTags() {
  try {
    const r = await semanticApi.tags()
    if (r.error) {
      tagError.value = r.error
      return
    }
    tagMap.value = r.files || {}
    tagMeta.value = { total: r.total_files, tagged: r.tagged_files, pending: r.pending, stale: r.stale }
    tagError.value = ''
  } catch {
    /* 标签是增强层：加载失败不打扰主流程 */
  }
}

/** 调模型增量打标签；剩余文件可再次点击直到 pending=0。 */
async function refreshTags(limit = 60) {
  tagLoading.value = true
  tagError.value = ''
  try {
    const r = await semanticApi.refreshTags(limit)
    if (r.error) { tagError.value = r.error; return r }
    tagMap.value = r.files || {}
    tagMeta.value = { total: r.total_files, tagged: r.tagged_files, pending: r.pending, stale: r.stale }
    return r
  } finally {
    tagLoading.value = false
  }
}

// ---------------------------------------------------------------- 大白话定位
/** 定位态：locatePaths 中的文件在树里持续高亮，直到清空查询。 */
const locateQuery = ref('')
const locateLoading = ref(false)
const locatePaths = ref<Set<string>>(new Set())
const locateResult = shallowRef<LocateResp | null>(null)
/** 定位请求失败时的错误信息（finally 已保证 loading 复位，但 UI
之前没有错误状态 → 失败时旧结果仍占位、用户不知道已过期）。 */
const locateError = ref<string>('')

async function runLocate(q: string) {
  const query = q.trim()
  locateQuery.value = query
  if (!query) {
    clearLocate()
    return
  }
  locateLoading.value = true
  locateError.value = ''
  try {
    const r = await semanticApi.locate(query)
    locateResult.value = r
    locatePaths.value = new Set((r.files || []).map((f) => f.path))
  } catch (e) {
    // 失败时清掉旧结果（避免过期数据误导）+ 暴露错误给 UI
    locateResult.value = null
    locatePaths.value = new Set()
    locateError.value = (e as { message?: string })?.message
      || '定位失败，请检查后端服务是否可用'
  } finally {
    locateLoading.value = false
  }
}

function clearLocate() {
  locateQuery.value = ''
  locateResult.value = null
  locatePaths.value = new Set()
  locateError.value = ''
}

/** 命中某文件：打开并跳到符号所在行，同时让文件树展开/闪烁，定位高亮保留。 */
async function openLocateFile(hit: { path: string; line: number | null }) {
  deps.revealPath(hit.path)
  await deps.jumpToLine(hit.path, hit.line || 1)
}

/** 命中某分区：展开并闪烁该分区文件夹。 */
function openLocateRegion(dir: string) {
  deps.revealPath(dir.replace(/\\/g, '/').replace(/\/+$/, ''))
}

// ---------------------------------------------------------------- 概览分区卡
const regionCards = shallowRef<RegionCard[]>([])
const regionCardsLoading = ref(false)

/** 概览页分区卡：统计文件数/未提交/最近提交；增强层，失败静默。 */
async function loadRegionCards(force = false) {
  if (!force && regionCardsLoading.value) return
  if (!deps.tree.value?.regions_enabled) {
    regionCards.value = []
    return
  }
  regionCardsLoading.value = true
  try {
    const r = await semanticApi.regionCards()
    regionCards.value = r.regions || []
  } catch {
    /* 分区卡是导航增强层：读取失败保留旧数据，不弹错打扰 */
  } finally {
    regionCardsLoading.value = false
  }
}

/** 离线演示态由 App 注入示例卡片（composable 不反向依赖 demo 模块）。 */
function seedDemoRegionCards(cards: RegionCard[]) {
  regionCards.value = cards
}

export function useSemantic() {
  return {
    tagMap, tagLoading, tagError, tagMeta, loadTags, refreshTags,
    locateQuery, locateLoading, locatePaths, locateResult, locateError,
    runLocate, clearLocate, openLocateFile, openLocateRegion,
    regionCards, regionCardsLoading, loadRegionCards, seedDemoRegionCards,
  }
}
