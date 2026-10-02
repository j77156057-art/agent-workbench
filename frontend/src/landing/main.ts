import { createApp } from 'vue'
import LandingApp from './App.vue'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'

// 粗野原始落地草案：独立的 MPA 入口，不直接依赖 window.DocMindSession，
// 因此这里直接挂载（与 ask/workbench 的 session.ready 守卫不同）。
createApp(LandingApp).mount('#app')
