import { createApp } from 'vue'
import App from './App.vue'
import '../fonts'
// 2026-10-07 并层：brutalist-overrides.css / style-modes.css / hierarchy-fix.css
// 三个覆盖补丁已分别折进 style.css（组件级收口区段）与 skins.css（造型令牌）。
import './style.css'
import './skins.css'
import './layout-modes.css'
import './glass.css'
import { applyStoredSkin } from './skins'
import { initStyle } from './styleMode'
import { initLayout } from './layoutMode'
import { initMaterial } from './glassPointer'

// 皮肤必须在挂载前落到 <html data-skin>，否则首帧会用默认皮渲染再切（闪一下纸色）
applyStoredSkin()
// 风格（造型语言）、布局（空间构成）、材质（实体/玻璃）同样要在挂载前应用，
// 否则先按默认骨架渲染再重排会闪一下（尤其 focus 模式是限宽居中，闪一下很明显）。
initStyle()
initLayout()
initMaterial()

window.DocMindSession.ready.then(() => createApp(App).mount('#app')).catch(error => {
  document.getElementById('app')!.textContent = '会话初始化失败：' + error.message
})
