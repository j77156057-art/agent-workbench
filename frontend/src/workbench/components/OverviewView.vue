<script setup lang="ts">
// 概览驾驶舱（打开工作台、未进入代码编辑时的默认工作区）：
// 顶部项目状态条 + 左主区（分区卡片/未启用空状态）+ 右栏（上手清单 / 最近打开 / 试试问 AI）。
// 视觉遵循 Linear 式克制：平面表面、无装饰渐变、右栏为朴素分组（卡片只留给可点的分区）。
import { computed, onMounted, ref } from 'vue'
import RegionHomeView from './RegionHomeView.vue'
import { demoMode, demoRegionCards } from '../composables/demo'
import { useWorkbench } from '../composables/workbench'
import { appEvents } from '../eventBus'
import { getProjectId, projectApi, taskApi } from '../api'

const {
  tree, regionCards, recentFiles, openRecent, openSymbolMap,
  openRegionMap, tagMeta,
} = useWorkbench()

// 当前项目名（顶栏下拉之外的第二处项目上下文锚点）：自取项目列表解析，
// 不依赖 App.vue 的 local state；失败不影响概览渲染。
const projects = ref<{ project_id: string; name: string; root: string }[]>([])
const currentProjectName = computed(() => {
  const id = demoMode.value ? 'demo' : getProjectId()
  if (demoMode.value) return '示例游戏项目'
  return projects.value.find((p) => p.project_id === id)?.name || ''
})
onMounted(async () => {
  if (demoMode.value) return
  try {
    const r = await projectApi.list()
    projects.value = (r.projects || []).map((p) => ({ project_id: p.project_id, name: p.name, root: p.root }))
  } catch { /* 失败仅导致项目名不显示，不阻断概览 */ }
  try {
    const r = await taskApi.list()
    tasks.value = (r.tasks || []).slice(-5).reverse().map((t) => ({
      id: String(t.id ?? ''),
      title: String(t.title ?? '未命名任务'),
      status: String(t.status ?? ''),
    }))
  } catch { /* 失败仅导致不显示任务区，不阻断概览 */ }
})

/** 进行中的任务（首屏仪表盘）。取不到就当没有 —— 概览不该因它失败。 */
const tasks = ref<{ id: string; title: string; status: string }[]>([])

const regionsEnabled = computed(() => demoMode.value || !!tree.value?.regions_enabled)
// 真实态且未打开任何项目：概览首屏应引导「打开项目」，而非误导向「启用分区治理」
const noProject = computed(() => !demoMode.value && !tree.value)

// ------------------------------------------------------------ 项目状态条统计
const stats = computed(() => {
  const cards = demoMode.value ? demoRegionCards : regionCards.value
  const existing = cards.filter((c) => c.exists)
  const dirty = cards.filter((c) => c.dirty_count > 0)
  return {
    regions: existing.length,
    dirtyRegions: dirty.length,
    files: existing.reduce((n, c) => n + c.files, 0),
    dirtyFiles: dirty.reduce((n, c) => n + c.dirty_count, 0),
  }
})

// 语义标签覆盖（真实状态，替代营销提示语）；演示态不显示
const tagStatus = computed(() => {
  if (demoMode.value || !tagMeta.value.total) return ''
  const { tagged, total } = tagMeta.value
  return `语义标签 ${tagged}/${total}`
})

// ------------------------------------------------------------ 上手清单（可关闭/可恢复）
const ONBOARD_KEY = 'docmind:onboarding-dismissed'
const checklistHidden = ref(window.localStorage.getItem(ONBOARD_KEY) === '1')
function dismissChecklist() {
  checklistHidden.value = true
  try { window.localStorage.setItem(ONBOARD_KEY, '1') } catch { /* ignore */ }
}
function restoreChecklist() {
  checklistHidden.value = false
  try { window.localStorage.removeItem(ONBOARD_KEY) } catch { /* ignore */ }
}

function focusChat(q?: string) {
  appEvents.emit('docmind:focus-chat', { q })
}
function showSymbolMap() {
  if (demoMode.value) return
  openSymbolMap()
}

const ASK_CHIPS = [
  '玩家受伤扣多少血在哪算的？',
  '敌人的巡逻逻辑在哪个文件？',
  '这个函数被谁调用了？',
]
</script>

<template>
  <div class="ov">
    <div class="ov-inner">
      <!-- 项目状态条：平面、只放状态信息 -->
      <div class="ov-strip">
        <span class="ov-folder" aria-hidden="true">
          <svg width="15" height="15" viewBox="0 0 15 15">
            <path d="M1.2 3.6 Q1.2 2.9 1.9 2.9 H5.4 L6.5 4 H13.1 Q13.8 4 13.8 4.7 V11 Q13.8 11.7 13.1 11.7 H1.9 Q1.2 11.7 1.2 11 Z"
                  fill="none" stroke="currentColor" stroke-width="1.1"/>
          </svg>
        </span>
        <div class="ov-strip-main">
          <span v-if="currentProjectName" class="ov-project-name">{{ currentProjectName }}</span>
          <span class="ov-root" :title="tree?.code_root || ''">
            {{ demoMode ? '示例游戏项目（离线演示）' : (tree?.code_root || '未连接项目') }}
          </span>
          <span class="ov-strip-stats">
            <b>{{ stats.regions }}</b> 个分区
            <template v-if="!demoMode"> · <b>{{ stats.files }}</b> 个文件</template>
            <template v-if="stats.dirtyFiles">
              · <em><b>{{ stats.dirtyFiles }}</b> 处未提交</em>
            </template>
            <template v-if="tagStatus"> · <span>{{ tagStatus }}</span></template>
          </span>
        </div>
        <button v-if="checklistHidden" type="button" class="ov-restore" @click="restoreChecklist">
          新手引导
        </button>
      </div>

      <!-- 两栏：分区主区 + 右侧辅助栏 -->
      <div class="ov-columns">
        <div class="ov-main">
          <!-- 进行中的任务（2026-10-20 首屏仪表盘化）：真实任务列表。
               原来首屏主视觉是「这个项目还没启用分区治理」的空态大字框 ——
               一个空状态占了整个主栏，用户打开看不到任何"正在发生的事"。 -->
          <section v-if="tasks.length" class="ov-group ov-tasks">
            <header class="ov-group-head">
              <h4>进行中的任务</h4>
              <span class="ov-group-count">{{ tasks.length }}</span>
            </header>
            <div v-for="t in tasks" :key="t.id" class="ov-task">
              <span class="ov-task-dot" :class="t.status" aria-hidden="true" />
              <span class="ov-task-title">{{ t.title }}</span>
              <span class="ov-task-status">{{ t.status }}</span>
            </div>
          </section>

          <RegionHomeView v-if="regionsEnabled" />

          <!-- 未打开任何项目：首屏引导去顶栏打开/切换项目（而非误导向「启用分区治理」） -->
          <div v-else-if="noProject" class="ov-empty ov-noproj">
            <span class="ov-empty-ic" aria-hidden="true">
              <svg width="26" height="26" viewBox="0 0 30 30">
                <rect x="3.5" y="6.5" width="23" height="19" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M3.5 12.5 H26.5 M10.5 6.5 V4.5 M19.5 6.5 V4.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
              </svg>
            </span>
            <div class="ov-empty-body">
              <h3>还没有打开项目</h3>
              <p>
                代码工作台需要先绑定一个项目目录作为工作区。点击<b>顶栏左侧的项目名</b>，
                即可打开新项目或切换已有项目；打开后这里会显示分区与文件概览、最近改动与「试试问 AI」。
              </p>
              <p class="ov-noproj-hint">
                AI 问答页也能直接提问；但要在工作台里改代码、看分区、做 git 存档与回滚，需要先在这里打开项目。
              </p>
            </div>
          </div>

          <!-- 已打开项目但未启用分区治理的空状态 -->
          <div v-else class="ov-empty">
            <span class="ov-empty-ic" aria-hidden="true">
              <svg width="26" height="26" viewBox="0 0 30 30">
                <rect x="3.5" y="6.5" width="23" height="19" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M3.5 12.5 H26.5 M10.5 6.5 V4.5 M19.5 6.5 V4.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
              </svg>
            </span>
            <div class="ov-empty-body">
              <h3>这个项目还没启用分区治理</h3>
              <p>
                启用后，AI 只能在约定的分区文件夹里改代码——数值、行为、UI 各归其位，
                从源头防止代码堆砌；每个分区还能独立用 git 存档回滚。
              </p>
              <button type="button" class="ov-empty-btn" @click="openRegionMap">去启用分区治理</button>
            </div>
          </div>
        </div>

        <aside class="ov-rail">
          <!-- 上手四步：朴素分组，可关闭，localStorage 记忆 -->
          <section v-if="!checklistHidden" class="ov-group ov-guide">
            <header class="ov-group-head">
              <h4>上手四步</h4>
              <button class="ov-x" type="button" title="关闭（可在状态条重新打开）" @click="dismissChecklist">
                <svg width="10" height="10" viewBox="0 0 10 10"><path d="M1 1 L9 9 M9 1 L1 9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
              </button>
            </header>
            <ol class="ov-steps">
              <li>
                <a class="ov-step" href="/" target="_blank" rel="noopener" title="在新标签页打开 AI 问答首页">
                  <span class="ov-step-num">1</span>
                  <span class="ov-step-text"><b>让 AI「读完」项目</b><i>AI 问答首页选文件夹建索引（新标签页打开）</i></span>
                </a>
              </li>
              <li>
                <button class="ov-step" type="button" @click="focusChat()">
                  <span class="ov-step-num">2</span>
                  <span class="ov-step-text"><b>用大白话提问</b><i>右下角 AI 助手直接问，答案带代码位置</i></span>
                </button>
              </li>
              <li>
                <button class="ov-step" type="button" :disabled="demoMode" :title="demoMode ? '演示模式无真实项目数据' : '打开代码地图'" @click="showSymbolMap">
                  <span class="ov-step-num">3</span>
                  <span class="ov-step-text"><b>看图懂结构</b><i>代码地图画出函数定义与调用关系</i></span>
                </button>
              </li>
              <li>
                <div class="ov-step ov-step-static">
                  <span class="ov-step-num">4</span>
                  <span class="ov-step-text"><b>改坏了能「读档」</b><i>git 提交即存档点，历史版本一键回退</i></span>
                </div>
              </li>
            </ol>
            <footer class="ov-guide-foot">
              <span>Ctrl+S 保存</span><span>右键新建/重命名/删除</span><span>Alt+1/2 切工作区</span>
            </footer>
          </section>

          <!-- 最近打开 -->
          <section v-if="recentFiles.length" class="ov-group">
            <header class="ov-group-head"><h4>最近打开</h4></header>
            <ul class="ov-recent">
              <li v-for="f in recentFiles.slice(0, 6)" :key="f.path">
                <button type="button" class="ov-recent-item" :title="f.path" @click="openRecent(f.path)">
                  <span class="ov-recent-name">{{ f.name }}</span>
                  <span class="ov-recent-dir">{{ f.path.split('/').slice(0, -1).join('/') || '根目录' }}</span>
                </button>
              </li>
            </ul>
          </section>

          <!-- 试试问 AI -->
          <section class="ov-group">
            <header class="ov-group-head"><h4>试试问 AI</h4></header>
            <div class="ov-chips">
              <button
                v-for="q in ASK_CHIPS"
                :key="q"
                type="button"
                class="ov-chip"
                @click="focusChat(q)"
              >{{ q }}</button>
            </div>
          </section>
        </aside>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ov {
  flex: 1;
  overflow: auto;
  background: var(--bg-raised);
  animation: ov-in .14s ease both;
}
@keyframes ov-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.ov-inner {
  width: min(100%, 1480px);
  max-width: none;
  margin: 0 auto;
  padding: 22px 32px 34px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ---------- 项目状态条：平面、一根分隔线 ---------- */
.ov-strip {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid #dbe4f1;
  border-radius: 13px;
  background: linear-gradient(135deg, var(--bg-raised) 0%, color-mix(in srgb, var(--bg-raised) 90%, var(--bg)) 100%);
  box-shadow: 0 5px 18px rgba(35,52,84,.055);
}
.ov-folder { color: var(--accent); display: inline-flex; padding: 7px; border-radius: 9px; background: #eaf2ff; }
.ov-strip-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ov-project-name {
  font-family: var(--font-ui); font-size: var(--fs-body); font-weight: 700;
  color: var(--text); line-height: 1.2;
}
.ov-root {
  font-family: var(--font-mono);
  font-size: var(--fs-md);
  color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 560px;
}
.ov-strip-stats { font-size: var(--fs-sm); color: var(--text-faint); }
.ov-strip-stats b { color: var(--text-muted); font-weight: 700; }
.ov-strip-stats em { color: var(--amber); font-style: normal; }
.ov-restore {
  margin-left: auto; flex: 0 0 auto;
  border: none; background: transparent; cursor: pointer;
  color: var(--text-faint); font-size: var(--fs-sm); font-family: var(--font-ui);
  padding: 4px 8px; border-radius: 6px;
}
.ov-restore:hover { color: var(--accent); background: var(--bg-hover); }
.ov-restore:focus-visible, .ov-x:focus-visible, .ov-step:focus-visible, .ov-recent-item:focus-visible, .ov-chip:focus-visible, .ov-empty-btn:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }
.ov-restore, .ov-x, .ov-step, .ov-recent-item, .ov-chip, .ov-empty-btn { transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease; }

/* ---------- 两栏 ---------- */
.ov-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 312px;
  gap: 30px;
  align-items: start;
}
.ov-main { min-width: 0; }
/* 右栏：2026-10-19 去掉 position:sticky 与视口基准的 max-height。
   为什么：dock 默认态就占 62vh（折叠 33 / 中等 620 / 展开 876 三档，是产品决策），
   主区可视高度只剩 ~262px，而右栏内容 453px。原来 max-height 用 calc(100vh - 112px)
   = 视口高度算，**与主区真实可用高度无关** —— 于是右栏伸出可视区 297px，
   底部那部分正好压在 dock 底下；sticky 又让它顶在可视区上沿，于是出现
   「第 2 条被 dock 切一半、第 3/4 步完全看不见」的观感。
   改成静态流后：右栏与左栏同步随主区滚动，滚到底时右栏底边正好落在
   可视区底边（= dock 顶边），后两步能看到，且不再有"半截粘在顶上"的怪相。 */
.ov-rail { display: flex; flex-direction: column; gap: 12px; overflow: visible; padding-right: 2px; }

/* ---------- 右栏：朴素分组，无卡片边框 ---------- */
.ov-group-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.ov-group-head h4 {
  margin: 0; font-size: var(--fs-cap); font-weight: 700;
  color: var(--text-faint); letter-spacing: .06em;
  text-transform: uppercase;
}
.ov-x {
  border: none; background: transparent; color: var(--text-faint);
  cursor: pointer; padding: 3px; border-radius: 5px; line-height: 0;
}
.ov-x:hover { color: var(--text); background: var(--bg-hover); }

/* 上手清单 */
.ov-steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.ov-step {
  width: 100%; display: flex; align-items: flex-start; gap: 9px;
  text-align: left; border: none; background: transparent;
  padding: 6px 7px; border-radius: 8px; cursor: pointer;
  font-family: var(--font-ui); text-decoration: none;
  transition: background .12s;
}
button.ov-step:hover, a.ov-step:hover { background: var(--bg-hover); }
.ov-step:disabled { cursor: default; opacity: .55; }
.ov-step:disabled:hover { background: transparent; }
.ov-step-static { cursor: default; }
.ov-step-num {
  flex: 0 0 19px; height: 19px; margin-top: 1px;
  display: grid; place-items: center;
  border-radius: 6px; font-size: var(--fs-cap); font-weight: 800; color: var(--accent-ink);
  background: var(--accent);
}
.ov-step-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.ov-step-text b { font-size: var(--fs-sm); color: var(--text); font-weight: 600; }
.ov-step-text i {
  font-style: normal; font-size: var(--fs-cap); line-height: 1.5;
  color: var(--text-faint);
}
.ov-guide-foot {
  margin-top: 9px; padding-top: 9px;
  border-top: 1px solid var(--border);
  display: flex; gap: 12px; flex-wrap: wrap;
  font-size: var(--fs-cap); color: var(--text-faint);
}

.ov-group {
  padding: 13px 14px;
  border: 1px solid #e0e7f1;
  border-radius: 12px;
  background: color-mix(in srgb, var(--bg-raised) 75%, transparent);
  box-shadow: 0 4px 16px rgba(35,52,84,.045);
}
.ov-guide { border-color: var(--border-2); background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 100%, transparent), color-mix(in srgb, var(--bg-raised) 90%, transparent)); }

/* 最近打开 */
.ov-recent { list-style: none; margin: 0; padding: 0; }
.ov-recent-item {
  width: 100%; display: flex; flex-direction: column; gap: 1px;
  border: none; background: transparent; cursor: pointer;
  padding: 5px 7px; border-radius: 7px; text-align: left;
  font-family: var(--font-ui);
}
.ov-recent-item:hover { background: var(--bg-hover); }
.ov-recent-name { font-size: var(--fs-sm); color: var(--text); }
.ov-recent-dir {
  font-family: var(--font-mono); font-size: var(--fs-cap);
  color: var(--text-faint);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* 试试问 AI */
.ov-chips { display: flex; flex-direction: column; gap: 3px; }
.ov-chip {
  text-align: left; border: none;
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-sm); line-height: 1.5;
  padding: 6px 8px; border-radius: 7px; cursor: pointer;
  font-family: var(--font-ui);
  transition: color .12s, background .12s;
}
.ov-chip:hover { color: var(--accent); background: var(--bg-hover); }
.ov-chip::before {
  content: '→'; margin-right: 7px; color: var(--text-faint);
}
.ov-chip:hover::before { color: var(--accent); }

/* ---------- 未启用空状态 ---------- */
.ov-empty {
  border: 1px dashed var(--border);
  border-radius: 10px;
  /* 2026-10-20：从「居中大字 + 42px 内边距」改成左对齐的紧凑提示条。
     原来这个空态框占满整个主栏（892px 宽）还居中摆一行大字，
     成了首屏主视觉 —— 打开工作台先看到一句"你还没启用分区治理"，
     观感上像工程样机而不是产品。 */
  padding: 18px 20px;
  display: flex;
  align-items: flex-start;
  gap: 14px;
  text-align: left;
  color: var(--text-muted);
}
.ov-empty-ic { color: var(--text-faint); display: inline-flex; margin-bottom: 0; flex: 0 0 auto; }
.ov-empty-body { min-width: 0; flex: 1; }
.ov-empty h3 { margin: 0 0 6px; font-size: var(--fs-h3); color: var(--text); font-weight: 700; }
.ov-empty p {
  margin: 0 0 14px; max-width: 560px;
  font-size: var(--fs-sm); line-height: 1.7; color: var(--text-muted);
}
.ov-empty-btn {
  /* 原为 color: var(--accent)：朱红当正文色，实测压在这颗淡蓝底上只有
     2.51:1（paper 皮肤，12px）——不达 AA。按仓库约定「纸底强调字走 --accent-text」修。
     边同理：color-mix(in srgb, var(--accent) 32%, var(--bg-raised)) 是 --border-2 的字面值，写死就不会跟皮肤。 */
  /* 这颗按钮已经有强调色底纹 + 强调字，框是第三遍表达同一件事。
     宽度留着（transparent）以免布局宽度跳一下。 */
  border: var(--edge-w) solid transparent; background: color-mix(in srgb, var(--accent) 6%, transparent);
  color: var(--accent-text); font-family: var(--font-ui); font-size: var(--fs-sm); font-weight: 600;
  padding: 8px 20px; border-radius: 8px; cursor: pointer;
  transition: background .12s;
}
.ov-empty-btn:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); }
.ov-empty-btn:active, .ov-step:active, .ov-recent-item:active, .ov-chip:active { transform: translateY(1px); }
/* 修：原值末尾多出一段 "f9fd"（像是 hex 尾巴误粘），整条 background 声明因此
   被浏览器判为无效而丢弃 —— 这颗卡一直只有 border 没有渐变底。 */
.ov-noproj { border-style: solid; border-color: var(--border-2); background: linear-gradient(180deg, var(--bg-raised), color-mix(in srgb, var(--bg-raised) 90%, var(--bg))); }
.ov-noproj .ov-empty-ic { color: var(--accent); }
.ov-noproj-hint {
  margin: 12px 0 0; max-width: 560px;
  font-size: var(--fs-sm); line-height: 1.7; color: var(--text-faint);
  padding-top: 12px; border-top: 1px dashed var(--border);
}

/* ---------- 进行中的任务（首屏仪表盘，2026-10-20）---------- */
.ov-tasks { margin-bottom: 14px; }
.ov-group-count {
  margin-left: auto;
  font-family: var(--font-mono); font-size: var(--fs-cap);
  color: var(--text-faint);
}
.ov-task {
  display: flex; align-items: center; gap: 9px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}
.ov-task:last-child { border-bottom: none; }
.ov-task-dot {
  flex: 0 0 auto;
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-faint);
}
.ov-task-dot.open { background: var(--accent); }
.ov-task-dot.done { background: var(--green); }
.ov-task-dot.closed { background: var(--text-faint); }
.ov-task-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.ov-task-status { flex: none; font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint); }

/* ---------- 窄屏：右栏落到底部 ---------- */
@media (max-width: 1080px) {
  .ov-inner { width: 100%; }
  .ov-columns { grid-template-columns: minmax(0, 1fr); gap: 18px; }
  .ov-rail {
    position: static; max-height: none; overflow: visible; padding-right: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px 24px;
  }
}
@media (max-width: 620px) {
  .ov-inner { padding: 14px 14px 22px; gap: 14px; }
  .ov-strip { align-items: flex-start; padding: 12px; }
  .ov-root { max-width: calc(100vw - 90px); }
  .ov-rail { grid-template-columns: 1fr; }
  .ov-empty { padding: 32px 18px; }
}
@media (prefers-reduced-motion: reduce) {
  .ov, .ov-restore, .ov-x, .ov-step, .ov-recent-item, .ov-chip, .ov-empty-btn { animation: none; transition: none; }
}
</style>
