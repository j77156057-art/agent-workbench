import { createApp } from 'vue'
import App from './App.vue'
import '../fonts'
// 2026-10-07 并层：brutalist-overrides.css / style-modes.css / hierarchy-fix.css
// 三个覆盖补丁已分别折进 style.css（组件级收口区段）与 skins.css（造型令牌）。
import './style.css'
import './skins.css'
// 种子皮肤：由 tools/design_seed.py 从随机种子确定性派生（换观感 = 重新生成这个文件）
import './skins-seed.css'
// 画布身份色令牌：关系图 / 代码地图 / Unity 图 / 分区图 / 场景画布 共用一套
// 55%（图形）/ 36%（文字）混合配方，混向 --ink 所以两套深浅皮肤都达标
import './viz-tokens.css'
import './layout-modes.css'
import './glass.css'
// Markdown 答案正文：两页共享层（2026-10-19）。此前工作台对话台的答案区一条
// .md-* 规则都没有 —— 同一个 markdown.ts 渲染器，问答页排版整齐、工作台"潦草"。
import '../shared/markdown.css'
import { applyStoredSkin, applySkin, onSkinChange } from './skins'
import { initStyle } from './styleMode'
import { initLayout } from './layoutMode'
import { initMaterial } from './glassPointer'

// 皮肤必须在挂载前落到 <html data-skin>，否则首帧会用默认皮渲染再切（闪一下纸色）
applyStoredSkin()
// 跨窗口换肤实时跟随（2026-10-20）：桌面端可能同时开着工作台与问答页两个窗口，
// 在一个窗口里换皮肤，另一个此前不会跟着变（只在启动时读一次）。
onSkinChange((k) => applySkin(k))
// 风格（造型语言）、布局（空间构成）、材质（实体/玻璃）同样要在挂载前应用，
// 否则先按默认骨架渲染再重排会闪一下（尤其 focus 模式是限宽居中，闪一下很明显）。
initStyle()
initLayout()
initMaterial()

window.DocMindSession.ready.then(() => createApp(App).mount('#app')).catch(error => {
  document.getElementById('app')!.textContent = '会话初始化失败：' + error.message
})
