// 拉丁字体的本地 @font-face —— 三个入口共用，必须在各自 main.ts 的第一行引入。
//
// 为什么自托管：DocMind 是桌面应用（.bat 启动、pywebview 内嵌），原先这四个字族
// 全部依赖 fonts.googleapis.com，实测本机访问会间歇性 ERR_CONNECTION_RESET ——
// 同一份 CSS 两次跑出两种观感，而 --font-display 的头号字族 Archivo Black 一旦
// 掉不到就退回 sans-serif，整套"粗野印刷"识别度直接消失。
//
// 用 latin-<weight>.css 而不是 <weight>.css：后者还带 latin-ext 与 vietnamese 子集，
// 本项目只有拉丁 + 中文，用不到。
//
// Noto Sans SC 仍走 CDN：fontsource 全套 71MB / 1905 文件，且 AI 回答与文件名都是
// 动态中文，不能按 UI 字符串裁剪子集 —— 取舍另议。
import '@fontsource/archivo-black/latin-400.css'
import '@fontsource/space-grotesk/latin-400.css'
import '@fontsource/space-grotesk/latin-500.css'
import '@fontsource/space-grotesk/latin-700.css'
import '@fontsource/space-mono/latin-400.css'
import '@fontsource/space-mono/latin-700.css'
