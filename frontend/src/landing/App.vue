<script setup lang="ts">
import { ref } from 'vue'
import CapabilityCard from './CapabilityCard.vue'
import { useReveal, useCountUp } from './useReveal'

const root = ref<HTMLElement | null>(null)
useReveal(root)
useCountUp(root)

// R12 真机联验数据（与 showcase 同源，非 PPT 数字）
const metrics = [
  { count: 3171, suffix: 'ms', cap: '实时引擎首响应' },
  { count: 223, suffix: 'ms', cap: '模型延迟' },
  { count: 0, suffix: '%', cap: '对话错误率 / 75 轮' },
  { count: 6, suffix: '/6', cap: 'R12 真机验收通过' },
]

const phases = [
  {
    step: 'PHASE 01',
    title: '本地 RAG + ReAct Agent',
    items: [
      '离线索引工程文档、代码与配置',
      'ReAct 推理循环：检索 → 决策 → 执行工具',
      '弱模型友好的工具调用归一与失败护栏',
      '所有上下文留在本地，不外发',
    ],
  },
  {
    step: 'PHASE 02',
    title: '仓库级工作台',
    items: [
      '分区管理：audio / values / assets / behaviors 隔离',
      '每区独立 git + 契约治理，改动可回滚',
      '引擎嵌入（Godot 等）+ 场景画布',
      '多项目体系，并行而不串味',
    ],
  },
]

// 引擎六硬能力（顺序即真机验证顺序）
const engineCaps = [
  '语音即输入：16kHz PCM16 麦克风上行',
  '画面即上下文：摄像头 / 屏幕共享进模型视野',
  '打断即响应：说一半就停、自动取消在播音频',
  '静音尾收句：停说后补约 1s 静音让 VAD 干净收句',
  '断线自恢复：stream_broken 自动重连重放',
  '音频先行闸门：先音频后图像，绕过供应商硬约束',
]

// 八项能力：rc 为 theme.ts 同源分区色，icon 为内联 SVG path
const caps = [
  { rc: 'var(--r-assets)', icon: '<path d="M4 7h16M4 12h16M4 17h10"/>', title: '分区知识库', desc: '按 audio / values / assets / behaviors / levels 隔离索引与上下文。' },
  { rc: 'var(--r-values)', icon: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>', title: '本地 RAG 检索', desc: '离线语义检索工程文档与代码，不外发上下文。' },
  { rc: 'var(--r-bugs)', icon: '<path d="M12 3v18M5 8l7-5 7 5"/><circle cx="12" cy="14" r="2"/>', title: '契约治理', desc: '改动先过契约校验，区与区之间不越界写入。' },
  { rc: 'var(--r-levels)', icon: '<rect x="3" y="4" width="18" height="14"/><path d="M3 9h18M8 18v3M16 18v3"/>', title: '引擎嵌入', desc: '把 Godot 等引擎窗口嵌入工作台，所见即所改。' },
  { rc: 'var(--r-audio)', icon: '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>', title: '实时对话', desc: '语音入、画面入、打断出，R12 验收 6/6。' },
  { rc: 'var(--r-behaviors)', icon: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 12a9 9 0 0 1-15 6.7L3 16"/>', title: '自验证闭环', desc: '改动写完后自动跑校验门，失败即收尾并标记。' },
  { rc: 'var(--r-net)', icon: '<path d="M3 12h4l3 8 4-16 3 8h4"/>', title: '经验记忆', desc: '跨会话沉淀排错经验，下次同类问题直接召回。' },
  { rc: 'var(--r-ui)', icon: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>', title: '多项目体系', desc: '一个项目一个 ID、独立状态，并行推进不串味。' },
]
</script>

<template>
  <div ref="root" class="landing-root">
    <header class="nav">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <span><b>DocMind</b> <span class="tag">LOCAL SCAFFOLD</span></span>
      </div>
      <nav class="nav-links" aria-label="主导航">
        <a href="#product">产品</a>
        <a href="#phases">工作台</a>
        <a href="#engine">引擎</a>
        <a href="#caps">能力</a>
      </nav>
      <span class="status-pill"><span class="dot" aria-hidden="true"></span>LOCAL · 本地运行</span>
    </header>

    <main>
      <section class="hero">
        <div class="container">
          <div class="hero-grid">
            <div>
              <p class="eyebrow reveal">Local AI Dev Scaffold · 游戏 / Mod 工程</p>
              <h1 class="reveal d1">
                <span class="line">给你的游戏工程，</span>
                <span class="line">配一个<span class="hl">看得懂场景</span></span>
                <span class="line">的 AI 搭档</span>
              </h1>
              <p class="hero-sub reveal d2">本地 RAG + ReAct Agent 起步，叠加仓库级工作台（分区 + 独立 git + 契约 + 引擎嵌入 + 场景画布），再接一层实时多模态对话引擎。所有数据留在你机器上。</p>
              <div class="hero-cta reveal d3">
                <a class="btn btn-primary" href="#engine">看实时引擎实测</a>
                <a class="btn btn-ghost" href="#product">了解产品</a>
              </div>
            </div>

            <div class="product-mock reveal d2" role="img" aria-label="DocMind 工作台预览：左侧分区列表，右侧 Agent 对话与改动回写">
              <div class="pm-bar"><span class="dots"><i></i><i></i><i></i></span><span>docmind · prj-3da07fde89a5</span></div>
              <div class="pm-body">
                <div class="pm-side">
                  <span class="zone"><span class="sw" style="background:var(--r-audio)"></span>audio</span>
                  <span class="zone"><span class="sw" style="background:var(--r-values)"></span>values</span>
                  <span class="zone"><span class="sw" style="background:var(--r-assets)"></span>assets</span>
                  <span class="zone"><span class="sw" style="background:var(--r-behaviors)"></span>behaviors</span>
                  <span class="zone"><span class="sw" style="background:var(--r-levels)"></span>levels</span>
                </div>
                <div class="pm-main">
                  <div class="pm-row"><span class="who">你 ›</span><br />把 boss 血量调低 20%，加一个狂暴阶段。</div>
                  <div class="pm-row model"><span class="who">DocMind ›</span><br />已改 values/boss.tres，血量 100→80；新增 phase_rage 节点。要我同步对话触发配置吗？</div>
                  <div class="pm-row"><span class="who">你 ›</span><br />同步，并提交到本仓库 git。</div>
                </div>
              </div>
            </div>
          </div>

          <div class="metrics">
            <div v-for="(m, i) in metrics" :key="i" class="metric reveal" :class="'d' + i">
              <div class="num" :data-count="m.count" :data-suffix="m.suffix">0<span class="u">{{ m.suffix }}</span></div>
              <div class="cap">{{ m.cap }}</div>
            </div>
          </div>
        </div>
      </section>

      <section class="section" id="product">
        <div class="container">
          <div class="section-head reveal">
            <p class="eyebrow">Two-Phase Architecture</p>
            <h2>两阶段，从「会查文档」到「改得动仓库」</h2>
            <p>不是把大模型塞进编辑器，而是先给工程一本本地知识库，再给它一套能安全落盘的仓库级工作台。</p>
          </div>
          <div class="phases" id="phases">
            <div v-for="(p, i) in phases" :key="p.step" class="phase reveal" :class="'d' + i">
              <span class="step">{{ p.step }}</span>
              <h3>{{ p.title }}</h3>
              <ul>
                <li v-for="(it, j) in p.items" :key="j">{{ it }}</li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      <section class="section" id="engine">
        <div class="container">
          <div class="section-head reveal">
            <p class="eyebrow">Realtime Multimodal Engine</p>
            <h2>实时多模态对话引擎</h2>
            <p>语音即输入、画面即上下文、打断即响应、断线自恢复。R12 真机联验实测，非 PPT 数字。</p>
          </div>
          <div class="engine">
            <div>
              <h3 :style="{ marginBottom: 'var(--s-4)' }">六项被真机验证的硬能力</h3>
              <ul class="engine-list">
                <li v-for="(c, i) in engineCaps" :key="i">{{ c }}</li>
              </ul>
            </div>
            <div class="console reveal d2" role="img" aria-label="实时会话控制台：用户与模型字幕、首响与延迟读数">
              <div class="c-bar"><span>SESSION · prj-3da07fde89a5</span><span>native-realtime</span></div>
              <div class="c-body">
                <span class="c-chip">CONNECTED · VAD server · audio.in + video.in</span>
                <div class="bubble"><b>你 ›</b> 把 boss 的血量调低 20%，再加一个阶段。</div>
                <div class="bubble model"><b>模型 ›</b> 已设血量 80%，新增「狂暴阶段」。同步改对话触发配置吗</div>
                <div class="latency">
                  <span>首响应 <b>3171ms</b></span>
                  <span>模型延迟 <b>223ms</b></span>
                  <span>错误率 <b>0.0%</b></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="section" id="caps">
        <div class="container">
          <div class="section-head reveal">
            <p class="eyebrow">Capabilities</p>
            <h2>围绕分区色板组织的能力</h2>
            <p>每一项能力对应一个真实工程分区，颜色与工作台完全一致。</p>
          </div>
          <div class="caps">
            <CapabilityCard
              v-for="(c, i) in caps"
              :key="c.title"
              :rc="c.rc"
              :icon="c.icon"
              :title="c.title"
              :desc="c.desc"
              :index="i"
            />
          </div>
        </div>
      </section>

      <section class="section">
        <div class="container">
          <div class="cta-band reveal">
            <h2>把实时 AI 搭档，装进你的开发舱</h2>
            <p>本地运行，数据不出机。先从一个分区的知识库开始，再逐步打开工作台与实时引擎。</p>
            <div class="hero-cta">
              <a class="btn btn-primary" href="#product">本地运行 DocMind</a>
              <a class="btn btn-ghost" href="#engine">阅读接入文档</a>
            </div>
          </div>
        </div>
      </section>
    </main>

    <footer class="footer">
      <div class="container">
        <span>DOCUMENT · DocMind 本地 AI 研发脚手架 · 粗野原始落地草案</span>
        <span>数据来源 · R12 真实设备联验报告 · 2026-09-29</span>
      </div>
    </footer>
  </div>
</template>
