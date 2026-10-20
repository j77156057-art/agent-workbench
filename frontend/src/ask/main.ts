import { createApp } from 'vue'
import AskApp from './AskApp.vue'
import '../fonts'
import './ask.css'
import './ask-riso.css'
import './ask-typo.css'
import './ask-a11y.css'
// Markdown 答案正文：两页共享层（同一个 markdown.ts 渲染器，问答页与工作台对话台
// 此前只有问答页有样式，工作台一条都没有）。放在 a11y 之后，保证兜底规则优先。
import '../shared/markdown.css'
import '../workbench/skins.css'
// 种子皮肤：由 tools/design_seed.py 从随机种子确定性派生（换观感 = 重新生成这个文件）
import '../workbench/skins-seed.css'
// 玻璃材质：参数在 workbench/glass.css（单源），这里只引样式 + 初始化。
// 必须放在 skins.css **之后**：参数块 :root 兜底要早于 html[data-skin] 覆写才生效。
import '../workbench/glass.css'
import '../workbench/layout-modes.css'
import './ask-glass.css'
import { applyStoredSkin, applySkin, onSkinChange } from '../workbench/skins'
import { initStyle } from '../workbench/styleMode'
import { initLayout } from '../workbench/layoutMode'
import { initMaterial } from '../workbench/glassPointer'

// 与工作台共用同一份 localStorage key：两个入口必须显示同一套皮肤，
// 否则用户在设置里选了深色、切到问答页又变回纸色 = 风格分裂。
// 皮肤 / 造型 / 布局 / 材质四者都共用，同样必须在挂载前应用，否则首帧闪。
// （2026-10-07 并层：style-modes.css / hierarchy-fix.css 已删，造型令牌在
//   skins.css，问答页减边框规则在 ask.css 末尾。）
applyStoredSkin()
initStyle()
initLayout()
initMaterial()

// 跨窗口换肤实时跟随（2026-10-20）：此前问答页只在启动时读一次皮肤，
// 在工作台里换了皮肤，已经打开的问答页窗口不会变 —— 两页风格分裂。
// onSkinChange 内部对 storage 事件已经会 applySkin，这里再兜一次是幂等的。
onSkinChange((k) => applySkin(k))

window.DocMindSession.ready.then(() => createApp(AskApp).mount('#app')).catch((error: Error) => {
  document.getElementById('app')!.textContent = '会话初始化失败：' + error.message
})
