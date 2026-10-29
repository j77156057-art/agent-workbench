<script setup lang="ts">
// 工作台最右侧的只读预览栏（第三栏）：网页 / 文件 / 改动。
//
// 它不是"右侧常驻对话栏"——App.vue 里那条 420px 右栏的历史否决仍然有效。
// 这里只做**按需打开、可关、可拖宽**的只读预览：对话仍是主区底部抽屉。
// 三个页签共用一条外壳：标题栏（页签 + 关闭）与内容区；具体渲染在
// RailBrowser / RailFile / DiffBody 里，壳只负责选谁上场。
import { computed } from 'vue'
import { useWorkbench } from '../composables/workbench'
import type { RightRailTab } from '../composables/workbench-runtime'
import RailBrowser from './RailBrowser.vue'
import RailFile from './RailFile.vue'
import DiffBody from './DiffBody.vue'

const {
  rightRailTab, rightRailDiff, rightRailFile,
  closeRightRail, openRightRailFile,
} = useWorkbench()

const tabs = computed<Array<{ key: RightRailTab; label: string; title: string }>>(() => [
  { key: 'browser', label: '网页', title: '网页预览：真实嵌入，外部站点可切只读镜像' },
  { key: 'file', label: '文件', title: rightRailFile.value.path ? `文件预览：${rightRailFile.value.path}` : '文件预览：从引用 / 产物 / 改动进入' },
  { key: 'diff', label: '改动', title: rightRailDiff.value.length ? `改动预览：${rightRailDiff.value.length} 个文件` : '改动预览：从对话里的「查看改动」进入' },
])

function switchTab(t: RightRailTab) {
  // 页签是纯视图切换：内容状态都存在 workbench-runtime 里，切回来不丢。
  rightRailTab.value = t
}

/** 改动页签里的「只读查看」：把同一个文件交给文件页签，右栏内完成切换。 */
function viewDiffFile(path: string) {
  openRightRailFile(path, 1)
}
</script>

<template>
  <aside class="wb-rightrail" aria-label="预览侧栏">
    <header class="rr-head">
      <div class="rr-tabs" role="tablist" aria-label="预览内容">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          role="tab"
          class="rr-tab"
          :class="{ on: rightRailTab === t.key }"
          :aria-selected="rightRailTab === t.key"
          :title="t.title"
          @click="switchTab(t.key)"
        >{{ t.label }}</button>
      </div>
      <span class="rr-chip">只读</span>
      <button
        type="button"
        class="rr-close"
        title="关闭预览侧栏（可从顶栏「更多 → 预览侧栏」重新打开）"
        aria-label="关闭预览侧栏"
        @click="closeRightRail()"
      >
        <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
        </svg>
      </button>
    </header>

    <div class="rr-body">
      <RailBrowser v-if="rightRailTab === 'browser'" />
      <RailFile v-else-if="rightRailTab === 'file'" />
      <div v-else class="rr-diff">
        <DiffBody :entries="rightRailDiff" action-label="只读查看" @action="viewDiffFile($event.path)" />
      </div>
    </div>
  </aside>
</template>

<style scoped>
.wb-rightrail {
  flex: 0 0 var(--lay-rail-right, 460px);
  width: var(--lay-rail-right, 460px);
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border-left: 1px solid var(--border);
  overflow: hidden;
}
.rr-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.rr-tabs { display: flex; align-items: center; gap: 2px; min-width: 0; padding: 2px; border-radius: var(--r-sm); background: var(--bg); }
.rr-tab {
  padding: 5px 10px;
  font-size: var(--fs-sm);
  font-weight: 600;
  border: 1px solid transparent;
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  white-space: nowrap;
}
.rr-tab:hover { color: var(--text); background: var(--bg-hover); }
.rr-tab.on { color: var(--text); border-color: var(--border); background: var(--surface); box-shadow: 0 1px 2px rgba(17, 17, 22, .05); }
.rr-chip {
  margin-left: auto;
  flex: 0 0 auto;
  padding: 0 4px;
  color: var(--text-muted);
  font-size: var(--fs-cap);
}
.rr-close {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.rr-close:hover { background: var(--bg-hover); color: var(--text); }
.rr-close:focus-visible, .rr-tab:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 1px; }
.rr-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.rr-diff { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
</style>
