// 全站字体的本地 @font-face —— 三个入口共用，必须在各自 main.ts 的第一行引入。
//
// 拉丁与中文的理由不同，分开记，别混成一条。
//
// 【拉丁：修本机真实故障】DocMind 是桌面应用（.bat 启动、pywebview 内嵌），原先
// Archivo Black / Space Grotesk / Space Mono 靠 fonts.googleapis.com，而本机访问它会
// 间歇性 ERR_CONNECTION_RESET（同一份构建一次拿到 16 个 woff2、一次一个都没有）。
// CDN 不可达时 @font-face 数为 0、三个字族的宽度度量全部退化成 monospace —— 界面真的
// 在换字形，且本机没有这些字族兜底。必须自托管。
//
// 【中文：买跨机器一致性与版本钉死，不是修本机】Noto Sans SC 本机装着（about:blank 上
// 零 @font-face 时按名字就能取到，15010 墨像素），而 --font-ui / --font-display 的栈里
// 本来就点名了它，所以在这台机器上撤掉自托管仍会渲染 Noto，看不出差别。真正的收益是：
// ① 要发给别人用，不能假设用户装了 Noto —— 没装时中文掉到系统默认，实测墨量比 Noto 重
// 13.5%，且没有真 900 字重（700 与 900 墨像素完全相同，只是合成加粗）；② 钉死版本，
// 本机装的 Noto 与 fontsource 那份不同版（15010 vs 15374，差 2.4%）。
//
// 中文必须用 chinese-simplified-<weight>.css，不要用 <weight>.css：后者还带 latin-ext、
// cyrillic、vietnamese 子集，三档字重会把产物从 +7.8MB/6 文件撑到 +18.2MB/588 文件。
//
// 代价（已知并接受）：每字重一个整块，浏览器无法再按 unicode-range 懒加载，首屏约
// 2.3MB（之后走缓存），产物 +7.8MB。
//
// 另注：artifacts/fetch_fonts.py 是更早为静态 mockup 写的同类脚本（只下拉丁子集，
// 并明确不下 CJK 因为本机装了 Noto）。它与本文件是两套并存的机制，改动时两边都看。
import '@fontsource/archivo-black/latin-400.css'
import '@fontsource/space-grotesk/latin-400.css'
import '@fontsource/space-grotesk/latin-500.css'
import '@fontsource/space-grotesk/latin-700.css'
import '@fontsource/space-mono/latin-400.css'
import '@fontsource/space-mono/latin-700.css'
import '@fontsource/noto-sans-sc/chinese-simplified-400.css'
import '@fontsource/noto-sans-sc/chinese-simplified-700.css'
import '@fontsource/noto-sans-sc/chinese-simplified-900.css'
