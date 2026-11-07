<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { loadMotionProfile, motionRuntime } from '../motionRuntime'

const open = ref(false)
const chipRef = ref<HTMLButtonElement | null>(null)
const popStyle = ref<Record<string, string>>({})
let timer = 0

const tierLabel = computed(() => (
  motionRuntime.tier === 'low' ? '低配' : motionRuntime.tier === 'mid' ? '中配' : '高配'
))

const shortReasons = computed(() => motionRuntime.reasons
  .map(reason => shortReason(reason.kind, reason.value))
  .filter(Boolean))

const chipText = computed(() => {
  if (motionRuntime.degraded) {
    const pct = Math.round(motionRuntime.overrunPercent)
    // 例：GPU 软件渲染 · CPU 2 核 · 帧超支 62% · 已降级（最多保留两条画像原因）
    const head = shortReasons.value.slice(0, 2)
    return [...head, `帧超支 ${pct}%`, '已降级'].join(' · ')
  }
  if (motionRuntime.tier === 'low') return '低配 · 等待帧观测'
  return `资源正常 · ${tierLabel.value}`
})

const isProblem = computed(() => motionRuntime.degraded || motionRuntime.tier === 'low')

/** 把 kind + 实测值压成一句可读原因，供 chip 的 title / 弹层首行使用。 */
function shortReason(kind: string, value: string): string {
  switch (kind) {
    case 'cpu_physical': return `CPU ${value} 核`
    case 'cpu_logical': return `CPU ${value} 线程`
    case 'gpu_weak_model': return 'GPU 弱卡'
    case 'gpu_software_render': return 'GPU 软件渲染'
    case 'gpu_integrated_only': return '无独显'
    case 'device_memory': return `内存 ${value}`
    case 'js_heap': return 'JS 堆偏小'
    case 'os_old': return '系统过旧'
    case 'os_retired': return '系统停止支持'
    case 'reduced_motion': return '减少动效'
    default: return ''
  }
}

const reasonChain = computed(() => {
  const parts = motionRuntime.reasons
    .map(reason => shortReason(reason.kind, reason.value))
    .filter(Boolean)
  if (motionRuntime.degraded) {
    parts.push(`帧超支 ${Math.round(motionRuntime.overrunPercent)}%`, '已降级')
  } else {
    parts.push(`帧超支 ${Math.round(motionRuntime.overrunPercent)}%`)
  }
  return parts.join(' · ')
})

const chipTitle = computed(() => {
  const base = reasonChain.value || '系统资源采样中'
  return `${base}（点击查看 CPU / GPU / 内存 / 磁盘）`
})

function fmtPercent(value: number | null | undefined): string {
  return typeof value === 'number' ? `${value}%` : '—'
}

function fmtGb(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number') return '—'
  return `${(bytes / (1024 ** 3)).toFixed(1)}GB`
}

function fmtMb(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—'
  if (value >= 1024) return `${(value / 1024).toFixed(1)}GB`
  return `${Math.round(value)}MB`
}

const cpuLine = computed(() => {
  const cpu = motionRuntime.resources?.cpu
  if (!cpu) return motionRuntime.loading ? '读取中…' : '—'
  const cores = cpu.physical_cores && cpu.logical_cores
    ? `${cpu.physical_cores} 核 ${cpu.logical_cores} 线程`
    : '核数 unknown'
  const freq = cpu.freq_known && cpu.freq_mhz ? ` @${Math.round(cpu.freq_mhz)}MHz` : ' @unknown'
  const usage = cpu.overall_percent === null ? '采样预热中' : fmtPercent(cpu.overall_percent)
  const process = cpu.process_percent === null ? '—' : fmtPercent(cpu.process_percent)
  return `${usage} · 本进程 ${process} · ${cores}${freq}`
})

const gpuLine = computed(() => {
  const gpu = motionRuntime.resources?.gpu
  if (!gpu) return motionRuntime.loading ? '读取中…' : '—'
  if (!gpu.available) return `不可用 · ${gpu.reason || '未提供原因'}`
  const mem = `${fmtMb(gpu.memory_used_mb)} / ${fmtMb(gpu.memory_total_mb)}`
  return `${fmtPercent(gpu.utilization_percent)} · 显存 ${mem} · ${gpu.name || 'unknown'}`
})

const memoryLine = computed(() => {
  const memory = motionRuntime.resources?.memory
  if (!memory) return motionRuntime.loading ? '读取中…' : '—'
  if (memory.error) return `不可用 · ${memory.error}`
  return `${fmtPercent(memory.percent)} · ${fmtGb(memory.used_bytes)} / ${fmtGb(memory.total_bytes)}`
})

const diskLine = computed(() => {
  const disk = motionRuntime.resources?.disk
  if (!disk) return motionRuntime.loading ? '读取中…' : '—'
  if (disk.error) return `不可用 · ${disk.error}`
  return `${fmtPercent(disk.percent)} · 剩余 ${fmtGb(disk.free_bytes)} · ${disk.path}`
})

function refreshLive() {
  const svg = document.querySelector<SVGSVGElement>('.mo-stable-graph')
  if (!svg) return
  motionRuntime.degraded = svg.dataset.motionDegraded === '1'
  const overrun = Number(svg.dataset.motionOverrunPct || '0')
  const refresh = Number(svg.dataset.motionRefreshMs || '16.7')
  motionRuntime.overrunPercent = Number.isFinite(overrun) ? overrun : 0
  motionRuntime.refreshMs = Number.isFinite(refresh) ? refresh : 16.7
}

const POPOVER_W = 360
function place() {
  const rect = chipRef.value?.getBoundingClientRect()
  if (!rect) return
  // 贴着胶囊右侧展开：旧版按 right 对齐会从按钮向左跨到文件树上（x=116–476），
  // 一打开就把资源管理器/项目卡盖住。右侧不够时才回退到左侧，并夹在视口内。
  const gap = 8
  const preferred = Math.round(rect.right + gap)
  const maxLeft = Math.max(gap, window.innerWidth - POPOVER_W - gap)
  const left = preferred <= maxLeft ? preferred : Math.max(gap, Math.round(rect.left - POPOVER_W - gap))
  popStyle.value = {
    top: `${Math.round(rect.bottom + 6)}px`,
    left: `${left}px`,
  }
}

function toggle() {
  open.value = !open.value
  if (open.value) {
    refreshLive()
    place()
  }
}

function onResize() {
  if (open.value) place()
}

onMounted(() => {
  void loadMotionProfile()
  refreshLive()
  timer = window.setInterval(refreshLive, 1000)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <span class="msb-wrap">
    <button
      ref="chipRef"
      type="button"
      class="msb-chip"
      :class="{ 'is-problem': isProblem }"
      :title="chipTitle"
      :aria-expanded="open"
      @click="toggle"
    >
      <i class="msb-dot" aria-hidden="true" />
      <span class="msb-text">{{ chipText }}</span>
    </button>

    <Teleport to="body">
      <div v-if="open" class="msb-backdrop" @click="open = false" />
      <div
        v-if="open"
        class="msb-pop"
        :style="popStyle"
        role="dialog"
        aria-label="系统资源与动效降级原因"
        @click.stop
      >
        <header class="msb-pop-head">
          <strong>系统资源 · {{ tierLabel }}</strong>
          <span class="msb-pop-state" :class="{ bad: isProblem }">
            {{ motionRuntime.degraded ? '已降级' : '正常' }}
          </span>
        </header>

        <p v-if="reasonChain" class="msb-reason">{{ reasonChain }}</p>

        <dl class="msb-rows">
          <div><dt>CPU</dt><dd>{{ cpuLine }}</dd></div>
          <div><dt>GPU</dt><dd :class="{ 'is-muted': !motionRuntime.resources?.gpu.available }">{{ gpuLine }}</dd></div>
          <div><dt>内存</dt><dd>{{ memoryLine }}</dd></div>
          <div><dt>磁盘</dt><dd>{{ diskLine }}</dd></div>
        </dl>

        <div class="msb-detail">
          <p v-for="reason in motionRuntime.reasons" :key="reason.kind + reason.value" class="msb-detail-row">
            <span>{{ reason.detail }}</span>
            <small>{{ reason.value }} · 阈值 {{ reason.threshold }}</small>
          </p>
          <p v-if="!motionRuntime.reasons.length" class="msb-detail-row">
            <span>{{ motionRuntime.loaded ? '没有触发降级的证据' : '正在读取能力画像…' }}</span>
          </p>
        </div>

        <footer class="msb-pop-foot">
          <span>采样 {{ motionRuntime.resources?.sampled_at || '—' }}</span>
          <span>刷新 {{ motionRuntime.refreshMs.toFixed(1) }}ms</span>
        </footer>
      </div>
    </Teleport>
  </span>
</template>

<style scoped>
.msb-wrap { display: inline-flex; align-items: center; }
.msb-chip {
  display: inline-flex; align-items: center; gap: 6px;
  height: 24px; padding: 0 8px; border-radius: 12px;
  border: 1px solid var(--line, rgba(255, 255, 255, 0.12));
  background: transparent; color: var(--text-dim); cursor: pointer;
  font-size: var(--fs-cap); line-height: 1; white-space: nowrap;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.msb-chip:hover { color: var(--text); border-color: var(--accent); }
.msb-chip.is-problem { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.msb-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); flex: none; }
.msb-chip.is-problem .msb-dot { background: var(--accent); }
.msb-text {
  font-size: var(--fs-cap); letter-spacing: 0.01em;
  max-width: 260px; overflow: hidden; text-overflow: ellipsis;
}

.msb-backdrop { position: fixed; inset: 0; z-index: 2190; background: transparent; }
.msb-pop {
  position: fixed; z-index: 2200; width: 360px; max-width: calc(100vw - 24px);
  padding: 12px 14px; border-radius: 10px;
  border: 1px solid var(--line, rgba(255, 255, 255, 0.14));
  background: var(--surface, #1c1c1f); color: var(--text);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.38);
}
.msb-pop-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.msb-pop-head strong { font-size: var(--fs-body); font-weight: 600; }
.msb-pop-state { font-size: var(--fs-cap); color: var(--text-dim); }
.msb-pop-state.bad { color: var(--accent); }
.msb-reason {
  margin: 8px 0 10px; padding: 6px 8px; border-radius: 6px;
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  color: var(--text-dim); font-size: var(--fs-cap); line-height: 1.5;
  word-break: break-word;
}
.msb-rows { margin: 0; display: grid; gap: 6px; }
.msb-rows > div { display: grid; grid-template-columns: 40px 1fr; gap: 8px; }
.msb-rows dt { color: var(--text-faint); font-size: var(--fs-cap); }
.msb-rows dd {
  margin: 0; font-size: var(--fs-cap); line-height: 1.45; color: var(--text-dim);
  word-break: break-word;
}
.msb-rows dd.is-muted { color: var(--text-faint); }
.msb-detail { margin-top: 10px; border-top: 1px solid var(--line, rgba(255, 255, 255, 0.1)); padding-top: 8px; }
.msb-detail-row { margin: 0 0 5px; display: grid; gap: 2px; }
.msb-detail-row span { font-size: var(--fs-cap); color: var(--text-dim); }
.msb-detail-row small { font-size: var(--mi-fs-caption, var(--fs-cap)); color: var(--text-faint); }
.msb-pop-foot {
  display: flex; justify-content: space-between; gap: 8px; margin-top: 4px;
  color: var(--text-faint); font-size: var(--mi-fs-caption, var(--fs-cap));
}
</style>
