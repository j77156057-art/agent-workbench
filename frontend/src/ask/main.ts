import { createApp } from 'vue'
import AskApp from './AskApp.vue'
import './ask.css'
import './ask-riso.css'
import './ask-typo.css'
import './ask-a11y.css'
import '../workbench/skins.css'
// 玻璃材质：参数在 workbench/glass.css（单源），这里只引样式 + 初始化。
// 必须放在 skins.css **之后**：参数块 :root 兜底要早于 html[data-skin] 覆写才生效。
import '../workbench/glass.css'
import '../workbench/style-modes.css'
import '../workbench/layout-modes.css'
import '../workbench/hierarchy-fix.css'
import './ask-glass.css'
import { applyStoredSkin } from '../workbench/skins'
import { initStyle } from '../workbench/styleMode'
import { initLayout } from '../workbench/layoutMode'
import { initMaterial } from '../workbench/glassPointer'

// 与工作台共用同一份 localStorage key：两个入口必须显示同一套皮肤，
// 否则用户在设置里选了深色、切到问答页又变回纸色 = 风格分裂。
// 皮肤 / 造型 / 布局 / 材质四者都共用，同样必须在挂载前应用，否则首帧闪。
applyStoredSkin()
initStyle()
initLayout()
initMaterial()

window.DocMindSession.ready.then(() => createApp(AskApp).mount('#app')).catch((error: Error) => {
  document.getElementById('app')!.textContent = '会话初始化失败：' + error.message
})
