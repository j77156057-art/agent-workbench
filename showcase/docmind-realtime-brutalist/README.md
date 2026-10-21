# Agent 工作台 实时视频对话引擎 · 能力展示页（粗野原始版 / Brutalist）

与另外三个版本（复古霓虹控制台 / 极简纸面 / 工业任务控制）共用同一份经 **R12 真机验收** 的内容与指标，
但采用完全不同的视觉语言：**粗野主义（Brutalism）原始风**。

## 视觉方向
- **底色**：草图纸 `#e9e6df`，叠加方格坐标纸（graph-paper）背景——像随手铺开的设计草稿。
- **墨色**：近黑 `#0d0d0d` 作为唯一主墨，无任何渐变。
- **强调**：区域色 `audio #2ec4b6` 的浅化版 `#0c8c7e`，仅用于少量强调（保持 Agent 工作台 品牌可识别）。
- **字体**：`Archivo Black`（超粗展示标题）+ `Space Mono`（等宽正文/标签）+ `JetBrains Mono` + `Noto Sans SC`。
- **质感**：3px 纯黑实线边框、零圆角、`6px 6px 0 #0d0d0d` 硬投影（纯偏移、无模糊）。按钮按下感来自硬阴影，而非渐变。

## 设计取舍
这是四版里**最“反精致”**的一版，刻意放弃柔和与留白，用粗黑线、硬阴影和坐标纸底传达
“工程原型 / 现场草稿”的气质。它把技术能力以最不加修饰的方式摊开，适合想要强烈视觉反差、
或用于工程团队内部展示的场景。

## 复用内容（真实数据）
所有指标来自 `R12 真实设备联验报告`：首响应 3171ms、模型延迟 223ms、错误率 0.0%（75 轮）、验收 6/6。
六项能力均对应真实联验：语音即输入、画面即上下文、打断即响应、静音尾收句、断线自恢复、音频先行闸门。

## 运行
```bash
cd showcase/docmind-realtime-brutalist
python -m http.server 8014
# 浏览器打开 http://127.0.0.1:8014
```
或直接用浏览器打开 `index.html`（字体走 Google Fonts CDN，离线时回退系统字体）。

## 文件结构
```
docmind-realtime-brutalist/
├─ index.html
├─ styles/
│  ├─ tokens.css        # 草纸底 + 墨色 + 硬投影变量
│  ├─ base.css          # 坐标纸背景 + 入场 reveal + reduced-motion
│  └─ components.css     # 粗黑边、硬阴影、原始控制台、指标块、管线箭头、黑底跑马灯
├─ scripts/
│  └─ main.js           # IntersectionObserver 入场 + 指标 count-up + 降级
└─ README.md
```

## 可访问性
- 满足 `prefers-reduced-motion`：开启时直接显示终态、跳过 count-up 与入场动画。
- 对比度：墨色 `#0d0d0d` 配草纸底 `#e9e6df`，正文与标题均远高于 4.5:1。
- 焦点环、44px 触控目标在 v1 通用规范基础上沿用。
