<script setup lang="ts">
// 概览驾驶舱主区：按分区展示职责、文件数、未提交改动、契约状态、最近一次提交。
// 数据由 workbench 共享状态统一加载（loadTree 后拉取 / 演示态由 App 注入示例卡）。
// 真实存在的分区以卡片网格呈现（卡片即导航交互）；契约里约定但尚未创建的分区
// 默认折叠成一行，避免首页被"还没有的东西"稀释。
import { computed, ref } from 'vue'
import { demoMode } from '../composables/demo'
import { regionColor } from '../theme'
import { useWorkbench } from '../composables/workbench'
import type { RegionCard } from '../api'

const { tree, revealPath, regionCards, regionCardsLoading, loadRegionCards, openRegionMap } = useWorkbench()

const cards = regionCards
const liveCards = computed(() => cards.value.filter((c) => c.exists))
const missingCards = computed(() => cards.value.filter((c) => !c.exists))
const showMissing = ref(false)

function openDir(dir: string) {
  if (demoMode.value) return
  revealPath(dir.replace(/\\/g, '/').replace(/\/+$/, ''))
}

function ago(ts: number | null): string {
  if (!ts) return ''
  const h = (Date.now() / 1000 - ts) / 3600
  if (h < 1) return '刚刚'
  if (h < 24) return `${Math.floor(h)} 小时前`
  const d = Math.floor(h / 24)
  return d < 30 ? `${d} 天前` : `${Math.floor(d / 30)} 个月前`
}

function contractText(c: RegionCard): string {
  if (!c.exists) return '目录未创建'
  if (c.missing_exports.length) return `契约缺 ${c.missing_exports.length} 项`
  return c.exports.length ? '契约完整' : '无契约文件'
}
</script>

<template>
  <section v-if="demoMode || tree?.regions_enabled" class="rh">
    <header class="rh-head">
      <div>
        <h3>项目分区</h3>
        <p>每个分区是一个带名称的文件夹，AI 只能在约定的分区里改代码；点卡片在左侧文件树定位。</p>
      </div>
      <button v-if="!demoMode" class="rh-refresh" :disabled="regionCardsLoading" title="重新统计文件与提交状态" @click="loadRegionCards(true)">
        {{ regionCardsLoading ? '统计中…' : '刷新' }}
      </button>
    </header>

    <p v-if="regionCardsLoading && !cards.length" class="rh-loading">正在统计分区状态…</p>

    <div class="rh-grid">
      <button
        v-for="c in liveCards"
        :key="c.key"
        class="rh-card"
        :disabled="demoMode"
        :title="demoMode ? '演示数据' : `在左侧文件树定位 ${c.dir}/`"
        @click="openDir(c.dir)"
      >
        <span class="rh-rail" :style="{ background: regionColor(c.key) }" />
        <span class="rh-body">
          <span class="rh-title">
            <span class="rh-name">{{ c.name }}</span>
            <span class="rh-dir">{{ c.dir }}/</span>
            <span v-if="c.branch" class="rh-branch">{{ c.branch }}</span>
            <span class="rh-locator" aria-hidden="true">
              <svg width="11" height="11" viewBox="0 0 11 11">
                <circle cx="5" cy="5" r="3.1" fill="none" stroke="currentColor" stroke-width="1.1"/>
                <path d="M7.4 7.4 L9.6 9.6" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>
              </svg>
            </span>
          </span>
          <span class="rh-desc">{{ c.desc || '暂无分区说明' }}</span>

          <!-- 一行朴素文本统计：文件数 + 未提交（仅在有改动时出现，琥珀色） -->
          <span class="rh-line">
            <span class="rh-line-item"><b>{{ c.files }}</b> 个文件</span>
            <span v-if="c.dirty_count > 0" class="rh-line-warn"><b>{{ c.dirty_count }}</b> 个未提交</span>
            <span class="rh-line-faint" :class="{ 'rh-line-bad': c.missing_exports.length }">
              {{ contractText(c) }}
            </span>
          </span>

          <!-- 最近提交：收成一行弱化信息 -->
          <span v-if="c.last_commit" class="rh-commit">
            <span class="rh-hash">{{ c.last_commit.hash }}</span>
            <span class="rh-msg">{{ c.last_commit.message }}</span>
            <span class="rh-time">{{ ago(c.last_commit.time) }}</span>
          </span>
        </span>
      </button>
    </div>

    <!-- 已约定但尚未创建的分区：默认折叠 -->
    <div v-if="missingCards.length" class="rh-missing">
      <button type="button" class="rh-missing-toggle" @click="showMissing = !showMissing">
        <svg class="rh-caret" :class="{ 'rh-caret-open': showMissing }" width="9" height="9" viewBox="0 0 9 9">
          <path d="M2 3 L4.5 5.5 L7 3" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        还有 <b>{{ missingCards.length }}</b> 个约定分区未创建（{{ missingCards.map(c => c.name).join('、') }}）
      </button>
      <button v-if="!demoMode" type="button" class="rh-missing-go" @click="openRegionMap">去分区治理创建 →</button>

      <ul v-if="showMissing" class="rh-missing-list">
        <li v-for="c in missingCards" :key="c.key">
          <span class="rh-dot" :style="{ background: regionColor(c.key) }" />
          <b>{{ c.name }}</b>
          <code>{{ c.dir }}/</code>
          <span class="rh-line-faint">{{ c.desc }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.rh { text-align: left; min-width: 0; }
.rh-loading { font-size: var(--fs-sm); color: var(--text-faint); margin: 8px 0; }
.rh-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 12px; margin-bottom: 14px;
}
.rh-head h3 { margin: 0; font-size: var(--fs-h2); color: var(--text); font-weight: 750; letter-spacing: -.01em; }
.rh-head p { margin: 3px 0 0; font-size: var(--fs-sm); color: var(--text-faint); }
.rh-refresh {
  flex: 0 0 auto; border: 1px solid var(--border); background: var(--bg-raised);
  color: var(--text-muted); font-size: var(--fs-cap); border-radius: 6px;
  padding: 4px 11px; cursor: pointer; font-family: var(--font-ui);
}
.rh-refresh:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); color: var(--accent); }
.rh-refresh:focus-visible, .rh-card:focus-visible, .rh-missing-toggle:focus-visible, .rh-missing-go:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }
.rh-refresh, .rh-card, .rh-missing-toggle, .rh-missing-go { transition: border-color .16s ease, box-shadow .16s ease, background .16s ease, transform .16s ease, color .16s ease; }

.rh-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.rh-card {
  position: relative; display: flex; align-items: stretch;
  text-align: left; border: 1px solid var(--border);
  border-radius: 10px; background: var(--bg-raised);
  padding: 0; cursor: pointer; overflow: hidden;
  font-family: var(--font-ui);
  min-height: 142px;
  transition: border-color .15s, box-shadow .15s, transform .16s var(--ease-spring), background .16s ease;
  /* 进入时轻微淡入（分区多时按位置错峰，封顶前 8 个） */
  animation: rh-in .22s ease both;
}
.rh-card:nth-child(1) { animation-delay: .02s }
.rh-card:nth-child(2) { animation-delay: .05s }
.rh-card:nth-child(3) { animation-delay: .08s }
.rh-card:nth-child(4) { animation-delay: .11s }
.rh-card:nth-child(5) { animation-delay: .14s }
.rh-card:nth-child(6) { animation-delay: .17s }
.rh-card:nth-child(7) { animation-delay: .20s }
.rh-card:nth-child(8) { animation-delay: .23s }
@keyframes rh-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
.rh-card:hover:not(:disabled) { border-color: #a9c5ef; background: var(--bg-raised); box-shadow: 0 8px 22px rgba(35,52,84,.11); transform: translateY(-2px); }
.rh-card:disabled { cursor: default; }
.rh-card:disabled .rh-locator { display: none; }
.rh-rail { width: 3px; flex: 0 0 3px; }
.rh-body {
  flex: 1; min-width: 0;
  display: flex; flex-direction: column; gap: 5px;
  padding: 14px 15px 12px;
}
.rh-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
.rh-name { font-size: var(--fs-body); font-weight: 750; color: var(--text); }
.rh-dir { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint); }
.rh-branch {
  margin-left: auto; flex: 0 0 auto;
  font-size: var(--fs-cap); color: var(--text-muted);
  background: var(--bg-hover); border-radius: 4px; padding: 0 6px; line-height: 16px;
}
/* 定位图标：默认隐入，hover 卡片才出现，表达"点击=在树里定位" */
.rh-locator {
  flex: 0 0 auto; color: var(--text-faint);
  display: inline-flex; opacity: 0; transform: translateX(-3px);
  transition: opacity .15s, transform .15s, color .15s;
}
.rh-card:hover .rh-locator { opacity: 1; transform: none; color: var(--accent); }
.rh-desc {
  font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.6;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}

/* 朴素文本统计行（替代原来的三个灰 pill） */
.rh-line { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.rh-line-item { font-size: var(--fs-cap); color: var(--text-faint); }
.rh-line-item b { color: var(--text-muted); font-weight: 700; }
.rh-line-warn { font-size: var(--fs-cap); color: var(--amber); }
.rh-line-warn b { font-weight: 700; }
.rh-line-faint { font-size: var(--fs-cap); color: var(--text-faint); }
.rh-line-bad { color: var(--amber); }

/* 最近提交一行：小字 + 省略号 */
.rh-commit {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--fs-cap); color: var(--text-faint);
  min-width: 0;
}
.rh-hash {
  flex: 0 0 auto; font-family: var(--font-mono);
  color: var(--text-muted);
}
.rh-msg {
  min-width: 0; flex: 1;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rh-time { flex: 0 0 auto; }

/* 未创建分区折叠条 */
.rh-missing {
  margin-top: 10px;
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px;
  padding: 8px 11px;
  border: 1px dashed var(--border);
  border-radius: 9px;
  background: color-mix(in srgb, var(--bg-raised) 66%, transparent);
}
.rh-missing-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  border: none; background: transparent; cursor: pointer;
  font-family: var(--font-ui); font-size: var(--fs-sm); color: var(--text-faint);
  padding: 2px 0;
}
.rh-missing-toggle:hover { color: var(--text-muted); }
.rh-missing-toggle b { color: var(--text-muted); font-weight: 700; }
.rh-caret { transition: transform .15s; }
.rh-caret-open { transform: rotate(180deg); }
.rh-missing-go {
  border: none; background: transparent; cursor: pointer;
  font-family: var(--font-ui); font-size: var(--fs-sm); color: var(--accent);
  padding: 2px 0;
}
.rh-missing-go:hover { text-decoration: underline; }
.rh-missing-list {
  flex-basis: 100%;
  list-style: none; margin: 4px 0 0; padding: 6px 0 2px;
  border-top: 1px dashed var(--border);
  display: flex; flex-direction: column; gap: 5px;
}
.rh-missing-list li {
  display: flex; align-items: center; gap: 8px;
  font-size: var(--fs-sm); color: var(--text-muted);
}
.rh-missing-list b { font-weight: 600; color: var(--text); }
.rh-missing-list code {
  font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint);
}
.rh-missing-list .rh-line-faint { margin-left: auto; }
.rh-dot { width: 7px; height: 7px; border-radius: 2px; flex: 0 0 7px; }

@media (max-width: 620px) {
  .rh-grid { grid-template-columns: minmax(0, 1fr); gap: 9px; }
  .rh-card { min-height: 0; }
  .rh-missing-list .rh-line-faint { margin-left: 0; flex-basis: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .rh-card, .rh-refresh, .rh-missing-toggle, .rh-missing-go, .rh-locator, .rh-caret { animation: none; transition: none; }
}
</style>
