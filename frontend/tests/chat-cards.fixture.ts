// 仅测试用：挂载真实的 ChatCardItem，复用工作台的字体/皮肤/令牌。
// 目的：不花模型额度也能看见「工具调用卡」的真实渲染（浏览器夹具）。
import { createApp, h } from 'vue'
import ChatCardItem from '../src/workbench/components/ChatCardItem.vue'
import '../src/fonts'
import '../src/workbench/skins.css'
import '../src/workbench/skins-seed.css'
import '../src/workbench/style.css'
import { applySkin, isSkinKey } from '../src/workbench/skins'
import { cardsOf } from '../src/workbench/composables/chatCards.ts'
import type { ChatTraceItem } from '../src/workbench/composables/chat-types.ts'

const skin = new URLSearchParams(location.search).get('skin') || 'zinc'
applySkin(isSkinKey(skin) ? skin : 'zinc')

function t(type: string, text: string, extra: Record<string, unknown> = {}): ChatTraceItem {
  return { type, text, at: 1, ...extra } as ChatTraceItem
}

const trace: ChatTraceItem[] = [
  t('thought', '先看入口在哪，再决定改哪里'),
  t('action', 'search_code(query: 提交接口)'),
  t('observation', '命中 3 处：api_routes/agent.py:120、api.py:88、frontend/src/api.ts:41', { ok: true, elapsedMs: 260 }),
  t('action', 'read_file(path: api_routes/agent.py, start: 110, end: 140)'),
  t('observation', 'def submit_turn(...)\n    ...\n    return {"ok": True}', { ok: true, elapsedMs: 95 }),
  t('action', 'run_command(cmd: pytest tests/test_turn.py -q)'),
  t('observation', 'FAILED tests/test_turn.py::test_submit_rejects_empty\n1 failed, 12 passed in 3.41s', { ok: false, elapsedMs: 3410 }),
  t('action', 'dev_headcheck(ref: HEAD, gate: 1)'),
  t('observation', '干净检出后 24 项门禁全绿', { ok: true, elapsedMs: 12400 }),
]

const cards = cardsOf({ trace, status: 'done' })

const App = {
  setup() {
    return () => h('div', { class: 'fixture-wrap' }, [
      h('div', { class: 'fixture-title' }, '工具调用卡（真实组件渲染）'),
      h('div', { class: 'fixture-list' },
        cards.map((card, i) => h(ChatCardItem, {
          key: card.key,
          card,
          defaultOpen: card.state === 'error' || i === cards.length - 1,
        }))),
      h('div', { class: 'fixture-title' }, '最终答案'),
      h('div', { class: 'fixture-answer' },
        '已定位到提交入口：`api_routes/agent.py:120`。测试里那一条失败与本次改动无关，是既有用例。'),
    ])
  },
}

createApp(App).mount('#app')
