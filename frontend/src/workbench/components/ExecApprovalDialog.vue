<script setup lang="ts">
// python_exec 三档分级同意的前端确认轨。
// 后端把「执行前分类命中」或「运行期安全钩升级」的高危代码登记为待确认（_PENDING_EXEC），
// 本组件轮询 /api/pending_execs 把它呈给用户：
//   · 第二档（橙）：有一定风险、大致可逆 → 点 1 次「同意执行」即真正落子进程。
//   · 第三档（红）：不可逆危害 → 需连续点 3 次确认（每次 confirm_exec 递增计数，未达阈值
//     后端返回「还需 N 次」且仍不执行）。计数进度直接读后端 confirms/need，前端不自造。
// 拒绝则丢弃该待执行代码。真正执行后后端以该档为天花板运行，凭据/系统目录/格式化仍被
// 运行期硬底线拦死——即便用户点满次数也毁不了的那撮，界面无需特判。
import { ref, computed } from 'vue'
import { usePolling } from '../composables/polling'
import { pendingApi, type PendingExec } from '../api'

const pending = ref<PendingExec[]>([])
const msg = ref<Record<string, string>>({})
const busy = ref<Record<string, boolean>>({})

async function load() {
  try {
    const resp = await pendingApi.execs()
    pending.value = resp.pending || []
  } catch { /* 服务不可达时静默保留上次列表，不打断工作台 */ }
}
usePolling(load, 3000)

const count = computed(() => pending.value.length)

function tierOf(item: PendingExec): 2 | 3 {
  return item.tier >= 3 ? 3 : 2
}

async function confirm(item: PendingExec) {
  if (busy.value[item.id]) return
  busy.value[item.id] = true
  try {
    const res = await pendingApi.confirmExec(item.id)
    // ok=true 表示已达阈值并真正执行：message 即执行输出/摘要；该条会从待确认列表消失。
    // ok=false 表示三档还没点够：message 是「还需 N 次」，稍后 load 刷新进度。
    msg.value[item.id] = res.message || (res.ok ? '已执行。' : '尚未达到确认次数。')
    await load()
  } catch (error) {
    msg.value[item.id] = '确认失败：' + (error as Error).message
  } finally {
    busy.value[item.id] = false
  }
}

async function reject(item: PendingExec) {
  if (busy.value[item.id]) return
  busy.value[item.id] = true
  try {
    const res = await pendingApi.rejectExec(item.id)
    if (!res.ok) msg.value[item.id] = res.message || '未找到该待执行项（可能已失效）。'
    await load()
  } catch (error) {
    msg.value[item.id] = '拒绝失败：' + (error as Error).message
  } finally {
    busy.value[item.id] = false
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="ea-pop">
      <div v-if="count" class="ea-root" role="region" aria-label="待确认的高危代码执行">
        <div v-for="item in pending" :key="item.id" class="ea-card" :class="tierOf(item) === 3 ? 'ea-danger' : 'ea-warn'">
          <div class="ea-head">
            <span class="ea-badge">{{ tierOf(item) === 3 ? '高危 · 需连续确认 3 次' : '需确认 · 1 次即可执行' }}</span>
            <span v-if="item.source === 'runtime'" class="ea-src">运行期拦截</span>
            <span v-else class="ea-src">执行前拦截</span>
          </div>
          <p class="ea-reason">{{ item.reason }}</p>
          <details class="ea-code">
            <summary>查看将要执行的代码</summary>
            <pre>{{ item.code }}</pre>
          </details>
          <div class="ea-progress">已确认 {{ item.confirms }}/{{ item.need }} 次</div>
          <div class="ea-actions">
            <button
              class="ea-confirm"
              :disabled="busy[item.id]"
              @click="confirm(item)">
              {{ item.confirms + 1 >= item.need ? '同意执行' : `继续确认（还差 ${item.need - item.confirms - 1 > 0 ? item.need - item.confirms - 1 : 1} 次）` }}
            </button>
            <button class="ea-reject" :disabled="busy[item.id]" @click="reject(item)">拒绝</button>
          </div>
          <small v-if="msg[item.id]" class="ea-msg">{{ msg[item.id] }}</small>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ea-root{position:fixed;right:16px;bottom:16px;z-index:420;display:flex;flex-direction:column;gap:10px;width:min(360px,calc(100vw - 32px));max-height:calc(100vh - 120px);overflow:auto;font-size:12px;line-height:1.6}
.ea-card{padding:12px 13px;border-radius:10px;background:var(--bg-raised);border:1px solid var(--border-strong);box-shadow:0 16px 42px rgba(35,52,84,.22)}
.ea-warn{border-color:var(--amber)}
.ea-danger{border-color:var(--danger)}
.ea-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.ea-badge{font-weight:600}
.ea-warn .ea-badge{color:var(--amber)}
.ea-danger .ea-badge{color:var(--danger)}
.ea-src{font-size:10px;color:var(--text-faint);border:1px solid var(--border);border-radius:4px;padding:1px 6px;white-space:nowrap}
.ea-reason{margin:0 0 6px;color:var(--text)}
.ea-code{margin:0 0 6px}
.ea-code summary{cursor:pointer;color:var(--text-muted);outline:none}
.ea-code pre{max-height:160px;overflow:auto;margin:6px 0 0;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font:10px/1.5 var(--font-mono);white-space:pre-wrap;word-break:break-word}
.ea-progress{color:var(--text-muted);margin-bottom:8px}
.ea-danger .ea-progress{color:var(--danger)}
.ea-actions{display:flex;gap:8px}
.ea-actions button{border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;border:1px solid transparent;transition:filter .16s ease,transform .16s ease,opacity .16s ease}
.ea-actions button:active:not(:disabled){transform:translateY(1px)}
.ea-actions button:disabled{opacity:.6;cursor:default}
.ea-confirm{background:linear-gradient(180deg,#e8a13a,#d98a1f);border-color:#c47a15;color:#fff;font-weight:600}
.ea-danger .ea-confirm{background:linear-gradient(180deg,#e2565d,#c93a42);border-color:#b52f37}
.ea-confirm:hover:not(:disabled){filter:brightness(1.06)}
.ea-reject{background:transparent;border-color:var(--border-strong);color:var(--text-muted)}
.ea-reject:hover:not(:disabled){border-color:var(--danger);color:var(--danger)}
.ea-msg{display:block;margin-top:6px;color:var(--text-muted)}
.ea-card:focus-within .ea-actions button:focus-visible{outline:2px solid #2f6fed88;outline-offset:2px}
.ea-pop-enter-active,.ea-pop-leave-active{transition:opacity .18s ease,transform .18s ease}
.ea-pop-enter-from,.ea-pop-leave-to{opacity:0;transform:translateY(8px)}
@media (prefers-reduced-motion:reduce){.ea-pop-enter-active,.ea-pop-leave-active,.ea-actions button{transition:none}}
</style>
