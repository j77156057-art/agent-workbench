import { createApp } from 'vue'
import App from './App.vue'
import '../fonts'
// 2026-10-07 并层：brutalist-overrides.css / style-modes.css / hierarchy-fix.css
// 三个覆盖补丁已分别折进 style.css（组件级收口区段）与 skins.css（造型令牌）。
import './style.css'
// 画布身份色令牌：关系图 / 代码地图 / Unity 图 / 分区图 / 场景画布 共用一套
// 55%（图形）/ 36%（文字）混合配方，混向 --ink 所以两套深浅皮肤都达标
import './viz-tokens.css'
import './layout-modes.css'
import './glass.css'
// Markdown 答案正文：两页共享层（2026-10-19）。此前工作台对话台的答案区一条
// .md-* 规则都没有 —— 同一个 markdown.ts 渲染器，问答页排版整齐、工作台"潦草"。
import '../shared/markdown.css'
import '../shared/ui-v2.css'
import './memory-inspector-tokens.css'
// 「墨屿」质感层：阅读体排版与形态（chat-ink）+ 两套新皮肤（skins-moyu）。
// 放在最后 import：同名规则下后者胜（要盖过 shared/markdown.css 的答案排版）。
import './chat-ink.css'
import './skins-moyu.css'
import { applyPalette, loadPalette } from './palette'
// 用户自备字体（2026-11-20）：偏好同步落到 :root（首帧就对），字体文件异步注册。
import { applyFontPrefs, restoreUserFonts } from './userFonts'
import { initStyle } from './styleMode'
import { initLayout } from './layoutMode'
import { initMaterial } from './glassPointer'

// 配色：默认浅色；深色观测舱由设置页切换（docmind.workbenchPalette）。
// 2026-11-09 之前这里写死 observatory + dark，导致首次打开就是黑、旧偏好也无效。
applyPalette(loadPalette())
applyFontPrefs()
restoreUserFonts()
// 风格（造型语言）、布局（空间构成）、材质（实体/玻璃）同样要在挂载前应用，
// 否则先按默认骨架渲染再重排会闪一下（尤其 focus 模式是限宽居中，闪一下很明显）。
initStyle()
initLayout()
initMaterial()

window.DocMindSession.ready.then(() => createApp(App).mount('#app')).catch(error => {
  document.getElementById('app')!.textContent = '会话初始化失败：' + error.message
})
