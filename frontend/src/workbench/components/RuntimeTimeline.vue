<script setup lang="ts">
// P1-1 运行时时间线：把 .docmind_runtime.jsonl / 引擎 stdout 事件画成多轨道时间轴。
//
// 相比「一个列表」，这里解决的问题是：事件在同一秒里成批出现时看不出因果关系，
// 也无法回答"血量随时间怎么走"。所以提供：按类型分轨道、时间缩放、类型/来源筛选、
// 会话分组、数值指标曲线、导出 JSON，以及点击事件跳到对应代码行。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { runtimeApi } from '../api'
import { createPoller } from '../composables/polling'
import type { RuntimeSession } from '../api'

const emit = defineEmits<{ (e: 'open-file', rel: string, line?: number): void }>()

interface Ev {
  key: string
  type: string
  source: string
  timestamp: string
  session: string
  data: Record<string, unknown>
  raw: Record<string, unknown>
  /** 真实毫秒时间戳；缺失为 NaN，不虚构时间。 */
  t: number
}

const events = ref<Ev[]>([])
const sessions = ref<RuntimeSession[]>([])
const loading = ref(false)
const message = ref('')
const autoRefresh = ref(false)

const activeTypes = ref<string[]>([])
const activeSources = ref<string[]>([])
const sessionFilter = ref<number | 0>(0)
const keyword = ref('')
const metric = ref('')
const pxPerSec = ref(60)
const selected = ref<Ev | null>(null)
const wrapEl = ref<HTMLElement | null>(null)
const seen = new Set<string>()

/* ------------------------------------------------------------------ 采集 */
function ingest(list: Record<string, unknown>[]) {
  for (const e of list) {
    const key = String(e.id ?? e.eid ?? '')
    if (!key || seen.has(key)) continue
    seen.add(key)
    const stamp = String(e.timestamp ?? '')
    const parsed = stamp ? Date.parse(stamp) : NaN
    events.value.push({
      key,
      type: String(e.type ?? 'event'),
      source: String(e.source ?? ''),
      timestamp: stamp,
      session: String(e.session ?? ''),
      data: (e.data && typeof e.data === 'object' ? e.data : {}) as Record<string, unknown>,
      raw: e,
      t: parsed,
    })
  }
  events.value.sort((a, b) => (Number.isFinite(a.t) ? a.t : Infinity) - (Number.isFinite(b.t) ? b.t : Infinity))
  if (events.value.length > 4000) {
    const dropped = events.value.splice(0, events.value.length - 4000)
    dropped.forEach(d => seen.delete(d.key))
  }
}

async function refresh() {
  loading.value = true
  try {
    const r = await runtimeApi.events()
    ingest(r.events ?? [])
    try { sessions.value = (await runtimeApi.sessions()).sessions ?? [] } catch { /* 会话可选 */ }
    message.value = ''
  } catch (e) {
    message.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

// 自动刷新：勾选后才轮询，页面切后台自动暂停（旧实现切后台仍恒定 2.5s 请求）
const timelinePoller = createPoller(refresh, 2500, { active: autoRefresh })

async function clearAll() {
  if (!window.confirm('清空全部运行时事件（含已抓取的引擎日志）？此操作不可撤销。')) return
  try {
    await runtimeApi.clear('all')
    events.value = []
    sessions.value = []
    seen.clear()
    selected.value = null
    message.value = '已清空。'
  } catch (e) {
    message.value = (e as Error).message
  }
}

onMounted(() => { void refresh(); timelinePoller.start() })
onUnmounted(() => { timelinePoller.stop() })

/* ------------------------------------------------------------------ 派生 */
const typeCounts = computed(() => {
  const map = new Map<string, number>()
  for (const ev of events.value) map.set(ev.type, (map.get(ev.type) ?? 0) + 1)
  return [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
})
const sourceCounts = computed(() => {
  const map = new Map<string, number>()
  for (const ev of events.value) map.set(ev.source, (map.get(ev.source) ?? 0) + 1)
  return [...map.entries()].sort((a, b) => b[1] - a[1])
})

const filtered = computed(() => events.value.filter(ev => {
  if (activeTypes.value.length && !activeTypes.value.includes(ev.type)) return false
  if (activeSources.value.length && !activeSources.value.includes(ev.source)) return false
  if (sessionFilter.value) {
    const session = sessions.value.find(s => s.index === sessionFilter.value)
    if (session && ev.timestamp) {
      if (ev.timestamp < session.start || ev.timestamp > session.end) return false
    } else if (session && !session.id) return false
  }
  if (keyword.value.trim()) {
    const kw = keyword.value.trim().toLowerCase()
    const blob = `${ev.type} ${ev.source} ${JSON.stringify(ev.data)}`.toLowerCase()
    if (!blob.includes(kw)) return false
  }
  return true
}))

const timed = computed(() => filtered.value.filter(ev => Number.isFinite(ev.t)))
const undatedCount = computed(() => filtered.value.length - timed.value.length)

/** 指标候选：所有事件 data 里出现过的数值字段 */
const metrics = computed(() => {
  const map = new Map<string, number>()
  for (const ev of events.value) {
    for (const [k, v] of Object.entries(ev.data)) if (typeof v === 'number' && k !== 'frame') map.set(k, (map.get(k) ?? 0) + 1)
  }
  return [...map.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => ({ key: k, count: n }))
})

const tracks = computed(() => {
  const map = new Map<string, Ev[]>()
  for (const ev of timed.value) {
    if (!map.has(ev.type)) map.set(ev.type, [])
    map.get(ev.type)!.push(ev)
  }
  return [...map.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([type, list]) => ({ type, list }))
})

const domain = computed(() => {
  const list = timed.value
  if (!list.length) return { t0: 0, t1: 1, span: 1 }
  const t0 = list[0].t
  const t1 = list[list.length - 1].t
  return { t0, t1, span: Math.max(1, t1 - t0) }
})

const trackWidth = computed(() => Math.max(320, Math.round((domain.value.span / 1000) * pxPerSec.value) + 64))

function xOf(t: number) {
  return 24 + ((t - domain.value.t0) / 1000) * pxPerSec.value
}

const ROW_H = 30
const CURVE_H = 76

const curvePoints = computed(() => {
  const key = metric.value
  if (!key) return ''
  const pts: string[] = []
  let min = Infinity
  let max = -Infinity
  const list = timed.value.filter(ev => typeof ev.data[key] === 'number')
  for (const ev of list) {
    const v = ev.data[key] as number
    if (v < min) min = v
    if (v > max) max = v
  }
  if (!list.length || !Number.isFinite(min)) return ''
  const span = max - min || 1
  for (const ev of list) {
    const v = ev.data[key] as number
    const y = CURVE_H - 12 - ((v - min) / span) * (CURVE_H - 26)
    pts.push(`${xOf(ev.t).toFixed(1)},${y.toFixed(1)}`)
  }
  return pts.join(' ')
})

const curveRange = computed(() => {
  const key = metric.value
  if (!key) return null
  const vals = timed.value.filter(ev => typeof ev.data[key] === 'number').map(ev => ev.data[key] as number)
  if (!vals.length) return null
  return { min: Math.min(...vals), max: Math.max(...vals) }
})

function evClass(type: string) {
  if (type.startsWith('__')) return 'sys'
  if (/damage|hit|defeat|die|death|error|fail/i.test(type)) return 'bad'
  if (/ready|spawn|start|heal|level/i.test(type)) return 'good'
  return 'mid'
}
function evTime(e: Ev) {
  if (!e.timestamp) return ''
  const d = new Date(e.timestamp)
  return isNaN(d.getTime()) ? e.timestamp.slice(11, 19) : d.toLocaleTimeString('zh-CN', { hour12: false })
}
function evData(e: Ev) {
  const keys = Object.keys(e.data || {})
  return keys.length ? keys.map(k => `${k}=${String(e.data[k])}`).join('  ') : ''
}
function asEntries(e: Ev) {
  return Object.entries(e.raw).filter(([k]) => k !== 'data')
}

/** 事件里可能带代码位置（约定 file/path + line），有就允许跳转 */
function codeTarget(e: Ev) {
  const file = (e.data.file ?? e.data.path ?? e.raw.file ?? e.raw.path) as string | undefined
  const line = Number(e.data.line ?? e.raw.line ?? 0)
  return file && typeof file === 'string' ? { file, line } : null
}
function jump(e: Ev) {
  const target = codeTarget(e)
  if (target) emit('open-file', target.file, target.line || undefined)
}

function fitWidth() {
  if (!wrapEl.value) return
  const width = wrapEl.value.clientWidth - 200
  const seconds = domain.value.span / 1000 || 1
  pxPerSec.value = Math.max(2, Math.min(600, Math.round(width / seconds)))
}

function exportJson() {
  const payload = {
    exported_at: new Date().toISOString(),
    total: events.value.length,
    filtered: filtered.value.length,
    sessions: sessions.value,
    events: filtered.value.map(ev => ev.raw),
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `docmind-runtime-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="rt-wrap">
    <div class="rt-bar">
      <label class="rt-check"><input v-model="autoRefresh" type="checkbox" />自动刷新</label>
      <button class="rt-btn" :disabled="loading" @click="refresh">{{ loading ? '刷新中…' : '刷新' }}</button>
      <span class="rt-sep" />
      <input v-model="keyword" class="rt-input" placeholder="关键字（类型/数据）" />
      <select v-model="sessionFilter" class="rt-sel">
        <option :value="0">全部会话（{{ sessions.length }}）</option>
        <option v-for="s in sessions" :key="s.index" :value="s.index">
          #{{ s.index }} · {{ s.count }} 条{{ s.id ? ` · ${s.id}` : '' }}
        </option>
      </select>
      <span class="rt-sep" />
      <label class="rt-zoom">
        缩放
        <input v-model.number="pxPerSec" type="range" min="2" max="600" step="2" />
        <b>{{ pxPerSec }}px/s</b>
      </label>
      <button class="rt-btn" @click="fitWidth">适应宽度</button>
      <span class="rt-spacer" />
      <label class="rt-zoom">
        指标曲线
        <select v-model="metric" class="rt-sel">
          <option value="">无</option>
          <option v-for="m in metrics" :key="m.key" :value="m.key">{{ m.key }}（{{ m.count }}）</option>
        </select>
      </label>
      <button class="rt-btn" :disabled="!filtered.length" @click="exportJson">导出 JSON</button>
      <button class="rt-btn danger" @click="clearAll">清空</button>
    </div>

    <p v-if="undatedCount" class="rt-dim">{{ undatedCount }} 条事件缺少有效时间戳，仅保留原始导出，不绘制虚构时间。</p>
    <div class="rt-filters">
      <span class="rt-flabel">类型</span>
      <button
        v-for="[type, count] in typeCounts"
        :key="type"
        class="rt-pill"
        :class="[evClass(type), { on: activeTypes.includes(type) }]"
        @click="activeTypes.includes(type) ? activeTypes = activeTypes.filter(t => t !== type) : activeTypes.push(type)"
      >{{ type }} <i>{{ count }}</i></button>
      <span v-if="!typeCounts.length" class="rt-dim">无事件</span>
      <span class="rt-sep" />
      <span class="rt-flabel">来源</span>
      <button
        v-for="[source, count] in sourceCounts"
        :key="source"
        class="rt-pill"
        :class="{ on: activeSources.includes(source) }"
        @click="activeSources.includes(source) ? activeSources = activeSources.filter(s => s !== source) : activeSources.push(source)"
      >{{ source }} <i>{{ count }}</i></button>
      <button v-if="activeTypes.length || activeSources.length || keyword" class="rt-btn ghost" @click="activeTypes = []; activeSources = []; keyword = ''">清除筛选</button>
    </div>

    <div v-if="message" class="rt-msg">{{ message }}</div>

    <div class="rt-legend">
      <b>怎么读：</b>左边每一行是一个<b>事件类型</b>（方块数量 = 该类事件条数）；方块的位置 = 事件发生的<b>时间点</b>（横轴是时间）；
      点任意方块，右侧「事件详情」显示它的完整字段。<b>来源</b>指事件由谁上报：<code>api</code> = 前端/接口上报，<code>godot</code> = 引擎日志抓取。
      想只看某几类，点上面的类型/来源胶囊筛选；「指标曲线」可把某个数值字段（如 hp）画成折线。
    </div>

    <div class="rt-body">
      <div class="rt-timeline" ref="wrapEl">
        <div class="rt-inner" :style="{ width: `${trackWidth}px` }">
          <!-- 指标曲线轨道 -->
          <div v-if="metric" class="rt-track">
            <div class="rt-label curve">
              <b>{{ metric }}</b>
              <small v-if="curveRange">{{ curveRange.min }} → {{ curveRange.max }}</small>
            </div>
            <div class="rt-lane" :style="{ height: `${CURVE_H}px` }">
              <svg class="rt-svg" :width="trackWidth" :height="CURVE_H">
                <polyline :points="curvePoints" fill="none" stroke="#58a6ff" stroke-width="1.6" />
                <circle
                  v-for="ev in timed.filter(e => typeof e.data[metric] === 'number')"
                  :key="`m-${ev.key}`"
                  :cx="xOf(ev.t)"
                  :cy="CURVE_H - 12 - (((ev.data[metric] as number) - (curveRange?.min ?? 0)) / ((curveRange?.max ?? 1) - (curveRange?.min ?? 0) || 1)) * (CURVE_H - 26)"
                  r="2.4"
                  fill="#58a6ff"
                />
              </svg>
            </div>
          </div>

          <div v-for="track in tracks" :key="track.type" class="rt-track">
            <div class="rt-label" :class="evClass(track.type)">
              <b>{{ track.type }}</b>
              <small>{{ track.list.length }}</small>
            </div>
            <div class="rt-lane" :style="{ height: `${ROW_H}px` }">
              <span
                v-for="ev in track.list"
                :key="ev.key"
                class="rt-mark"
                :class="[evClass(ev.type), { sel: selected?.key === ev.key }]"
                :style="{ left: `${xOf(ev.t)}px` }"
                :title="`${evTime(ev)} ${ev.type} ${evData(ev)}`"
                @click="selected = ev"
              />
            </div>
          </div>

          <div v-if="!tracks.length" class="rt-empty">
            暂无事件。启动游戏（Web 试玩或桌面窗口）后，掉血 / 死亡 / 生成等事件会实时出现在这里。
            <template v-if="events.length && !filtered.length"><br />当前筛选条件下没有匹配事件。</template>
          </div>
        </div>
      </div>

      <aside class="rt-detail">
        <div class="rt-detail-head">
          <b>事件详情</b>
          <span class="rt-dim">{{ filtered.length }} / {{ events.length }}</span>
        </div>
        <template v-if="selected">
          <div class="rt-kv"><span>类型</span><b :class="evClass(selected.type)">{{ selected.type }}</b></div>
          <div class="rt-kv"><span>来源</span><b>{{ selected.source }}</b></div>
          <div class="rt-kv"><span>时间</span><b>{{ selected.timestamp || evTime(selected) }}</b></div>
          <div v-if="selected.session" class="rt-kv"><span>会话</span><b>{{ selected.session }}</b></div>
          <div v-for="[k, v] in asEntries(selected)" :key="k" class="rt-kv"><span>{{ k }}</span><b>{{ v }}</b></div>
          <div class="rt-json">
            <div v-for="[k, v] in Object.entries(selected.data)" :key="k" class="rt-kv">
              <span>{{ k }}</span><b>{{ v }}</b>
            </div>
            <pre v-if="!Object.keys(selected.data).length">{{ JSON.stringify(selected.raw, null, 2) }}</pre>
          </div>
          <button v-if="codeTarget(selected)" class="rt-btn" @click="jump(selected)">
            跳转到 {{ codeTarget(selected)!.file }}<template v-if="codeTarget(selected)!.line">:{{ codeTarget(selected)!.line }}</template>
          </button>
        </template>
        <div v-else class="rt-dim pad">点时间轴上的任一点查看事件详情。</div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.rt-wrap { display: flex; flex-direction: column; min-height: 0; flex: 1; }
.rt-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding-bottom: 7px; }
.rt-spacer { flex: 1; }
.rt-sep { width: 1px; height: 18px; background: var(--border); margin: 0 3px; }
.rt-btn { border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted); border-radius: 5px; padding: 5px 9px; font-size: var(--fs-cap); cursor: pointer; white-space: nowrap; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease; }
.rt-btn:hover:not(:disabled) { color: var(--text); border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); background: var(--bg-selected); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }.rt-btn:focus-visible, .rt-pill:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.rt-btn:disabled { opacity: .38; cursor: default; }
.rt-btn.danger { border-color: color-mix(in srgb, var(--danger) 38%, transparent); color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.rt-btn.ghost { border-style: dashed; }
.rt-check { display: flex; align-items: center; gap: 4px; font-size: var(--fs-cap); color: var(--text-muted); cursor: pointer; }
.rt-input { background: var(--bg); border: 1px solid var(--border-graphic); color: var(--text); font-size: var(--fs-cap); padding: 5px 8px; border-radius: 5px; min-width: 150px; transition: border-color .15s ease, box-shadow .15s ease; }.rt-input:focus, .rt-sel:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent); }
.rt-sel { background: var(--bg); border: 1px solid var(--border-graphic); color: var(--text); font-size: var(--fs-cap); padding: 5px 6px; border-radius: 5px; max-width: 190px; transition: border-color .15s ease, box-shadow .15s ease; }
.rt-zoom { display: flex; align-items: center; gap: 5px; font-size: var(--fs-cap); color: var(--text-muted); }
.rt-zoom input[type=range] { width: 92px; }
.rt-zoom b { font-family: var(--font-mono); font-weight: 400; color: var(--text-muted); }

.rt-filters { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; padding-bottom: 7px; }
.rt-flabel { font-size: var(--fs-cap); color: var(--text-faint); }
.rt-pill { border: 1px solid var(--border); background: transparent; color: var(--text-muted); border-radius: 10px; padding: 2px 8px; font-size: var(--fs-cap); cursor: pointer; font-family: var(--font-mono); transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease; }.rt-pill:hover { border-color: var(--accent); background: var(--bg-selected); transform: translateY(-1px); }
.rt-pill i { font-style: normal; color: var(--text-faint); margin-left: 3px; }
.rt-pill.on { background: linear-gradient(180deg, var(--accent-loud), var(--accent)); border-color: var(--accent-deep); color: var(--accent-ink); }
.rt-pill.good.on { background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--green) 38%, transparent); color: var(--green); }
.rt-pill.bad.on { background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--danger) 38%, transparent); color: var(--danger); }
.rt-msg { font-size: var(--fs-cap); color: var(--amber); padding-bottom: 7px; }
.rt-legend { font-size: var(--fs-cap); color: var(--text-faint); line-height: 1.7; padding-bottom: 7px; }
.rt-legend b { color: var(--text-muted); }
.rt-legend code { font-family: var(--font-mono); color: var(--text-muted); background: var(--bg); border: 1px solid var(--border); border-radius: 3px; padding: 0 3px; }
.rt-dim { color: var(--text-faint); font-size: var(--fs-cap); }
.rt-dim.pad { padding: 12px 4px; line-height: 1.7; }

.rt-body { flex: 1; display: flex; min-height: 0; gap: 10px; }
.rt-timeline { flex: 1; min-width: 0; overflow: auto; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-raised); box-shadow: 0 6px 18px rgba(35,52,84,.05); }
.rt-inner { min-height: 100%; padding: 8px 0; }
.rt-track { display: flex; align-items: stretch; }
.rt-track + .rt-track { border-top: 1px solid rgba(35,52,84,.08); }
.rt-label { flex: 0 0 150px; position: sticky; left: 0; z-index: 2; background: color-mix(in srgb, var(--bg-raised) 92%, transparent); border-right: 1px solid var(--border); padding: 0 9px; display: flex; flex-direction: column; justify-content: center; gap: 1px; backdrop-filter: blur(4px); }
.rt-label b { font-size: var(--fs-cap); font-weight: 600; color: var(--text); font-family: var(--font-mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rt-label small { font-size: var(--fs-cap); color: var(--text-faint); }
.rt-label.good b { color: var(--green); }
.rt-label.bad b { color: var(--danger); }
.rt-label.mid b { color: var(--accent); }
.rt-label.curve b { color: var(--accent); }
.rt-lane { position: relative; flex: 1; background: repeating-linear-gradient(90deg, transparent 0 59px, rgba(35,52,84,.06) 59px 60px); }
.rt-svg { position: absolute; left: 0; top: 0; }
.rt-mark { position: absolute; top: 50%; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 2px; background: var(--accent); cursor: pointer; border: 1px solid #fff; transition: transform .15s ease, box-shadow .15s ease; }
.rt-mark:hover { transform: scale(1.35); box-shadow: 0 2px 6px rgba(35,52,84,.22); }
.rt-mark.sel { box-shadow: 0 0 0 2px color-mix(in srgb, var(--amber) 50%, transparent); }
.rt-mark.good { background: var(--green); }
.rt-mark.bad { background: var(--danger); }
.rt-mark.sys { background: var(--text-muted); }
.rt-empty { padding: 22px 16px; color: var(--text-faint); font-size: var(--fs-sm); line-height: 1.8; }

.rt-detail { width: 268px; flex: 0 0 268px; border: 1px solid var(--border); border-radius: 8px; padding: 9px 10px; overflow: auto; display: flex; flex-direction: column; gap: 4px; background: linear-gradient(180deg, var(--bg-hover), var(--bg-raised)); box-shadow: 0 5px 16px rgba(35,52,84,.04); }
.rt-detail-head { display: flex; align-items: baseline; justify-content: space-between; }
.rt-detail-head b { font-size: var(--fs-sm); }
.rt-kv { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-cap); border-bottom: 1px dashed rgba(35,52,84,.08); padding: 2px 0; }
.rt-kv span { color: var(--text-faint); font-family: var(--font-mono); }
.rt-kv b { color: var(--text); font-weight: 500; text-align: right; overflow: hidden; text-overflow: ellipsis; }
.rt-kv b.good { color: var(--green); }
.rt-kv b.bad { color: var(--danger); }
.rt-kv b.mid { color: var(--accent); }
.rt-json { margin-top: 5px; }
.rt-json pre { font-size: var(--fs-cap); color: var(--text-muted); white-space: pre-wrap; word-break: break-all; margin: 0; padding: 6px; border: 1px solid var(--border); border-radius: 5px; background: var(--bg); }
@media (max-width: 800px) { .rt-body { flex-direction: column; overflow: auto; }.rt-timeline { flex: 1 1 360px; min-height: 300px; }.rt-detail { width: auto; flex: 0 0 auto; max-height: 38vh; }.rt-input { min-width: 120px; }.rt-zoom input[type=range] { width: 72px; } }
@media (max-width: 560px) { .rt-bar { align-items: stretch; }.rt-bar .rt-sep { display: none; }.rt-btn, .rt-input, .rt-sel { flex: 1 1 auto; }.rt-zoom { flex: 1 1 100%; justify-content: space-between; }.rt-filters { max-height: 120px; overflow: auto; align-content: flex-start; }.rt-label { flex-basis: 108px; }.rt-detail { max-height: none; } }
@media (prefers-reduced-motion: reduce) { .rt-btn, .rt-input, .rt-sel, .rt-pill, .rt-mark { transition: none; } }
</style>
