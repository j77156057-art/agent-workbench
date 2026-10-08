// 全站字体的本地 @font-face —— 三个入口共用，必须在各自 main.ts 的第一行引入。
//
// 为什么全部自托管：DocMind 是桌面应用（.bat 启动、pywebview 内嵌），原先四个字族
// 都依赖 fonts.googleapis.com，而本机访问它会间歇性 ERR_CONNECTION_RESET —— 同一份
// 构建，两次跑一次拿到 16 个 woff2、一个都拿不到，整页中文就在 Noto Sans SC 与
// Microsoft YaHei 之间来回跳。实测两者中文墨量差 9.6%（正文 400）到 13.5%（标题 700），
// 而 --font-display 依赖的 900 字重雅黑根本没有（700 与 900 墨像素完全相同，只是合成加粗）。
//
// 中文必须用 chinese-simplified-<weight>.css，不要用 <weight>.css：后者还带 latin-ext、
// cyrillic、vietnamese 子集，三档字重会把产物从 +7.8MB/6 文件撑到 +18.2MB/588 文件。
//
// 代价（已知并接受）：自托管是每字重一个整块，浏览器无法再按 unicode-range 懒加载，
// 首屏传输约 2.3MB（之后走缓存）。换来的是渲染确定、离线可用。
import '@fontsource/archivo-black/latin-400.css'
import '@fontsource/space-grotesk/latin-400.css'
import '@fontsource/space-grotesk/latin-500.css'
import '@fontsource/space-grotesk/latin-700.css'
import '@fontsource/space-mono/latin-400.css'
import '@fontsource/space-mono/latin-700.css'
import '@fontsource/noto-sans-sc/chinese-simplified-400.css'
import '@fontsource/noto-sans-sc/chinese-simplified-700.css'
import '@fontsource/noto-sans-sc/chinese-simplified-900.css'
