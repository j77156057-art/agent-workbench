<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import RegionHomeView from './RegionHomeView.vue'
import { demoMode } from '../composables/demo'
import { useWorkbench } from '../composables/workbench'
import { appEvents } from '../eventBus'
import { getProjectId, projectApi, taskApi } from '../api'
import type { TreeNode } from '../api'

const {
  tree, regionCards, recentFiles, openRecent, openSymbolMap,
  openRegionMap, tagMeta,
} = useWorkbench()

const projects = ref<{ project_id: string; name: string; root: string }[]>([])
const tasks = ref<{ id: string; title: string; status: string }[]>([])
const projectRoot = computed(() => tree.value?.code_root || '')
const regionsEnabled = computed(() => demoMode.value || !!tree.value?.regions_enabled)
const noProject = computed(() => !demoMode.value && !projectRoot.value)

function normalizePath(path: string) {
  return path.replace(/\\/g, '/').replace(/\/+$/, '')
}

const currentProjectName = computed(() => {
  if (demoMode.value) return '示例游戏项目'
  const root = normalizePath(projectRoot.value)
  if (!root) return '尚未打开项目'
  const project = projects.value.find((p) => p.project_id === getProjectId() && normalizePath(p.root) === root)
  return project?.name || root.split('/').pop() || projectRoot.value
})

// 切换项目后清空旧任务；忽略已失效的请求，避免把上一项目的信息带到新概览。
watch([projectRoot, demoMode], async ([root, isDemo], _previous, onCleanup) => {
  let active = true
  onCleanup(() => { active = false })
  tasks.value = []
  projects.value = []
  if (!root || isDemo) return
  const projectId = getProjectId()
  await Promise.all([
    projectApi.list().then((r) => {
      if (active && projectId === getProjectId()) projects.value = r.projects || []
    }).catch(() => { /* 项目名回退为真实目录名。 */ }),
    taskApi.list().then((r) => {
      if (!active || projectId !== getProjectId()) return
      tasks.value = (r.tasks || []).slice(-5).reverse().map((t) => ({
        id: String(t.id ?? ''),
        title: String(t.title ?? '未命名任务'),
        status: String(t.status ?? ''),
      }))
    }).catch(() => { /* 任务不可用时不展示空的统计或示例任务。 */ }),
  ])
}, { immediate: true })

function countFiles(nodes: TreeNode[]): number {
  return nodes.reduce((count, node) => count + (node.type === 'file' ? 1 : countFiles(node.children || [])), 0)
}

// 文件树可能被截断，所以只承诺“已载入文件”，不把分区文件之和当作全项目总数。
const stats = computed(() => {
  if (demoMode.value || !tree.value?.ok) return []
  const items = [{ label: '已载入文件', value: String(countFiles(tree.value.nodes)) }]
  if (regionsEnabled.value && regionCards.value.length) {
    items.push({ label: '已创建分区', value: String(regionCards.value.filter((c) => c.exists).length) })
  }
  if (tagMeta.value.total > 0) {
    items.push({ label: '语义标签覆盖', value: `${tagMeta.value.tagged}/${tagMeta.value.total}` })
  }
  return items
})

const ONBOARD_KEY = 'docmind:onboarding-dismissed'
const checklistHidden = ref(false)
try { checklistHidden.value = window.localStorage.getItem(ONBOARD_KEY) === '1' } catch { /* 隐私模式仍展示清单。 */ }
function dismissChecklist() {
  checklistHidden.value = true
  try { window.localStorage.setItem(ONBOARD_KEY, '1') } catch { /* 忽略偏好存储失败。 */ }
}
function restoreChecklist() {
  checklistHidden.value = false
  try { window.localStorage.removeItem(ONBOARD_KEY) } catch { /* 忽略偏好存储失败。 */ }
}

// 仅展开助手并预填输入，发送仍由用户确认。
function focusChat(q?: string) {
  appEvents.emit('docmind:focus-chat', { q })
}
function showSymbolMap() {
  if (demoMode.value || noProject.value) return
  openSymbolMap()
}
</script>

<template>
  <div class="ov">
    <div class="ov-inner">
      <header class="ov-page-head">
        <div>
          <h1>项目概览</h1>
          <p>了解当前项目，从这里继续你的工作。</p>
        </div>
        <button v-if="checklistHidden" type="button" class="ov-text-button" @click="restoreChecklist">
          显示上手清单
        </button>
      </header>

      <section class="ov-project" aria-label="当前项目信息">
        <div class="ov-project-context">
          <span class="ov-folder" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
              <path d="M2 5a1 1 0 0 1 1-1h5l2 2h7a1 1 0 0 1 1 1v9H2V5Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
            </svg>
          </span>
          <div class="ov-project-info">
            <h2 class="ov-project-name">{{ currentProjectName }}</h2>
            <p class="ov-root" :title="demoMode ? '离线演示' : projectRoot">
              {{ demoMode ? '离线演示 · 示例数据' : (projectRoot || '通过顶栏项目菜单选择本地目录') }}
            </p>
          </div>
        </div>
        <dl v-if="stats.length" class="ov-stats">
          <div v-for="stat in stats" :key="stat.label" class="ov-stat">
            <dt>{{ stat.label }}</dt>
            <dd>{{ stat.value }}</dd>
          </div>
        </dl>
        <p v-if="!demoMode && tree?.truncated" class="ov-stats-note">文件树仅加载了部分内容，统计不代表项目文件总数。</p>
      </section>

      <div class="ov-columns">
        <div class="ov-main">
          <RegionHomeView v-if="regionsEnabled" />

          <section v-else-if="noProject" class="ov-start">
            <span class="ov-eyebrow">开始工作</span>
            <h2>先打开一个项目</h2>
            <p>从顶栏项目菜单选择「打开项目」，绑定本地目录后，即可浏览文件、理解代码并继续开发。</p>
            <span class="ov-start-note">也可以按 Alt+P 打开项目菜单。</span>
          </section>

          <template v-else>
            <section class="ov-start">
              <span class="ov-eyebrow">项目探索</span>
              <h2>从理解项目开始</h2>
              <p>先梳理目录、主要模块和入口，再带着具体问题深入代码。让下一步工作更有方向。</p>
              <div class="ov-actions">
                <button type="button" class="ov-action ov-action-primary" @click="focusChat('梳理当前项目的目录结构、主要模块和入口文件。')">
                  梳理项目结构
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
                <button type="button" class="ov-action" @click="focusChat('请帮我定位当前项目中的问题。我遇到的现象是：')">定位问题</button>
              </div>
              <span class="ov-start-note">问题将填入底部 AI 助手，由你确认后发送。</span>
            </section>
            <section class="ov-region-settings" aria-label="分区管理">
              <div>
                <h3>按分区组织项目</h3>
                <p>需要约定 AI 的修改范围时，再设置分区。</p>
              </div>
              <button type="button" class="ov-text-button" @click="openRegionMap">管理分区 <span aria-hidden="true">→</span></button>
            </section>
          </template>

          <section v-if="tasks.length" class="ov-tasks" aria-label="最近任务">
            <header class="ov-group-head">
              <h2>最近任务</h2>
              <span class="ov-group-count">{{ tasks.length }} 条</span>
            </header>
            <ul class="ov-task-list">
              <li v-for="(task, index) in tasks" :key="task.id || index" class="ov-task">
                <span class="ov-task-dot" aria-hidden="true" />
                <span class="ov-task-title" :title="task.title">{{ task.title }}</span>
                <span v-if="task.status" class="ov-task-status">{{ task.status }}</span>
              </li>
            </ul>
          </section>
        </div>

        <aside class="ov-rail" aria-label="项目快捷入口">
          <section v-if="!checklistHidden" class="ov-group">
            <header class="ov-group-head">
              <h2>上手清单</h2>
              <button class="ov-dismiss" type="button" title="隐藏上手清单，可在页首恢复" aria-label="隐藏上手清单" @click="dismissChecklist">
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="m3 3 6 6m0-6-6 6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
              </button>
            </header>
            <ol class="ov-steps">
              <li class="ov-step-static">
                <span class="ov-step-num">1</span>
                <span class="ov-step-text"><b>打开项目</b><span>在顶栏项目菜单选择本地目录</span></span>
              </li>
              <li>
                <button class="ov-step" type="button" @click="focusChat()">
                  <span class="ov-step-num">2</span>
                  <span class="ov-step-text"><b>与 AI 讨论代码</b><span>在底部 AI 助手描述目标或问题</span></span>
                </button>
              </li>
              <li>
                <button class="ov-step" type="button" :disabled="demoMode || noProject" :title="demoMode ? '演示模式无真实代码地图' : noProject ? '请先打开项目' : '打开代码地图'" @click="showSymbolMap">
                  <span class="ov-step-num">3</span>
                  <span class="ov-step-text"><b>查看代码地图</b><span>探索函数定义与调用关系</span></span>
                </button>
              </li>
            </ol>
            <footer class="ov-guide-foot"><span>Ctrl+S 保存</span><span>Alt+1 / 2 / 3 切换工作区</span></footer>
          </section>

          <section class="ov-group">
            <header class="ov-group-head"><h2>最近打开</h2></header>
            <ul v-if="recentFiles.length" class="ov-recent">
              <li v-for="file in recentFiles.slice(0, 5)" :key="file.path">
                <button type="button" class="ov-recent-item" :title="file.path" @click="openRecent(file.path)">
                  <span class="ov-recent-name">{{ file.name }}</span>
                  <span class="ov-recent-dir">{{ normalizePath(file.path).split('/').slice(0, -1).join('/') || '根目录' }}</span>
                </button>
              </li>
            </ul>
            <p v-else class="ov-recent-empty">打开文件后，可在这里快速继续。</p>
          </section>
        </aside>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ov {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  background: var(--bg);
  color: var(--text);
  container-type: inline-size;
}
.ov-inner {
  width: min(100%, 1280px);
  margin: 0 auto;
  padding: 28px 32px 40px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.ov-page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.ov-page-head h1 { margin: 0; font-size: calc(var(--fs-h1) + 2px); font-weight: 650; line-height: 1.4; letter-spacing: -.02em; }
.ov-page-head p { margin: 6px 0 0; color: var(--text-muted); font-size: var(--fs-body); line-height: 1.6; }
.ov-project {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 20px 28px;
  padding: 20px 24px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-raised);
}
.ov-project-context { flex: 1 1 260px; display: flex; align-items: center; gap: 14px; min-width: 0; }
.ov-folder { flex: none; display: grid; place-items: center; width: 40px; height: 40px; color: var(--text-muted); background: var(--bg); border-radius: 8px; }
.ov-project-info { min-width: 0; }
.ov-project-name { margin: 0; font-size: var(--fs-h2); font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
.ov-root { margin: 4px 0 0; color: var(--text-muted); font: var(--fs-sm)/1.6 var(--font-mono); overflow-wrap: anywhere; }
.ov-stats { display: flex; flex-wrap: wrap; gap: 16px 24px; margin: 0; }
.ov-stat { display: flex; flex-direction: column-reverse; gap: 4px; }
.ov-stat dt { font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.5; }
.ov-stat dd { margin: 0; font-size: var(--fs-xl); font-weight: 600; line-height: 1.4; font-variant-numeric: tabular-nums; }
.ov-stats-note { flex-basis: 100%; margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.6; }
.ov-columns { display: grid; grid-template-columns: minmax(0, 1fr) 280px; align-items: start; gap: 28px; }
.ov-main { min-width: 0; display: flex; flex-direction: column; gap: 20px; }
.ov-start { padding: 28px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-raised); }
.ov-eyebrow { display: block; margin-bottom: 12px; font-size: var(--fs-sm); font-weight: 600; color: var(--accent-text); }
.ov-start h2 { margin: 0; font-size: var(--fs-h1); font-weight: 600; line-height: 1.5; letter-spacing: -.02em; }
.ov-start p { max-width: 520px; margin: 12px 0 24px; font-size: var(--fs-body); line-height: 1.8; color: var(--text-muted); }
.ov-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
.ov-action {
  min-height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 9px 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-raised);
  color: var(--text);
  font-size: var(--fs-body);
  font-weight: 500;
  line-height: 1.5;
  cursor: pointer;
}
.ov-action-primary { color: var(--accent-text); background: var(--accent-soft); border-color: var(--border-2); font-weight: 600; }
.ov-action:hover { background: var(--bg-hover); }
.ov-start-note { display: block; font-size: var(--fs-sm); line-height: 1.7; color: var(--text-muted); }
.ov-region-settings { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 0 4px 20px; border-bottom: 1px solid var(--border); }
.ov-region-settings h3 { margin: 0; font-size: var(--fs-body); font-weight: 500; line-height: 1.5; }
.ov-region-settings p { margin: 5px 0 0; font-size: var(--fs-md); line-height: 1.6; color: var(--text-muted); }
.ov-text-button { flex: none; min-height: 32px; padding: 6px 8px; border: 0; border-radius: 5px; background: none; color: var(--text-muted); font-size: var(--fs-md); line-height: 1.5; cursor: pointer; }
.ov-text-button:hover { color: var(--accent-text); background: var(--bg-hover); }
.ov-rail { min-width: 0; display: flex; flex-direction: column; gap: 24px; }
.ov-group { min-width: 0; }
.ov-group + .ov-group { padding-top: 20px; border-top: 1px solid var(--border); }
.ov-group-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 28px; margin-bottom: 12px; }
.ov-group-head h2 { margin: 0; font-size: var(--fs-body); line-height: 1.5; font-weight: 600; }
.ov-dismiss { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: none; border-radius: 5px; background: none; color: var(--text-muted); cursor: pointer; }
.ov-dismiss:hover { background: var(--bg-hover); color: var(--text); }
.ov-steps, .ov-recent, .ov-task-list { list-style: none; margin: 0; padding: 0; }
.ov-steps { display: flex; flex-direction: column; gap: 4px; }
.ov-step, .ov-step-static { display: flex; align-items: flex-start; gap: 10px; width: 100%; padding: 10px 8px; text-align: left; border: 0; border-radius: 6px; background: none; color: var(--text); }
.ov-step { cursor: pointer; }
.ov-step:hover:not(:disabled) { background: var(--bg-hover); }
.ov-step:disabled { opacity: .55; cursor: not-allowed; }
.ov-step-num { flex: none; display: grid; place-items: center; width: 22px; height: 22px; border: 1px solid var(--border); border-radius: 50%; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1; font-variant-numeric: tabular-nums; }
.ov-step-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ov-step-text b { font-size: var(--fs-md); font-weight: 500; line-height: 1.6; }
.ov-step-text > span { font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.6; }
.ov-guide-foot { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 12px; padding: 0 8px; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.6; }
.ov-recent-item { width: 100%; display: flex; flex-direction: column; gap: 3px; text-align: left; padding: 10px 8px; border: 0; border-radius: 6px; background: none; color: var(--text); cursor: pointer; }
.ov-recent-item:hover { background: var(--bg-hover); }
.ov-recent-name { max-width: 100%; font-size: var(--fs-md); line-height: 1.5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-recent-dir { max-width: 100%; color: var(--text-muted); font: var(--fs-cap)/1.6 var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-recent-empty { margin: 0; font-size: var(--fs-md); line-height: 1.7; color: var(--text-muted); }
.ov-tasks { min-width: 0; }
.ov-group-count { font-size: var(--fs-sm); color: var(--text-muted); }
.ov-task { display: flex; align-items: center; gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--border); font-size: var(--fs-md); line-height: 1.6; }
.ov-task:last-child { border-bottom: 0; }
.ov-task-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); }
.ov-task-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-task-status { max-width: 35%; font-size: var(--fs-sm); color: var(--text-muted); overflow-wrap: anywhere; }
.ov-action, .ov-text-button, .ov-dismiss, .ov-step, .ov-recent-item { transition: background .15s ease, color .15s ease; }
.ov button:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
@container (max-width: 880px) {
  .ov-columns { grid-template-columns: minmax(0, 1fr); gap: 28px; }
  .ov-rail { padding-top: 24px; border-top: 1px solid var(--border); }
}
@container (max-width: 540px) {
  .ov-inner { padding: 20px 16px 28px; gap: 20px; }
  .ov-page-head { align-items: flex-start; flex-wrap: wrap; gap: 8px; }
  .ov-project { padding: 16px; gap: 20px; }
  .ov-start { padding: 22px 20px; }
  .ov-actions { flex-direction: column; align-items: stretch; }
}
@media (prefers-reduced-motion: reduce) {
  .ov-action, .ov-text-button, .ov-dismiss, .ov-step, .ov-recent-item { transition: none; }
}
</style>
