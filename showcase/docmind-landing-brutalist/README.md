# Agent 工作台 产品落地草案（粗野原始版 / v4）

与 `docmind-landing-minimal/`（极简暖纸）并列的**产品首页草案**，采用用户选定的**粗野原始**方向。
视觉系统与 `showcase/docmind-realtime-brutalist/` 完全同源。

## 视觉方向
- **素描纸 + 方格坐标纸**：底色 `#e9e6df`，22px 细格 + 110px 粗格网格背景。
- **墨 `#0d0d0d`** 同时用作 3px 粗描边与 `6px 6px 0` 硬偏移投影（纯偏移、零模糊）。
- **零圆角**；强调色 `#2ec4b6`（品牌青浅色版）作巨型色块；文字型强调用 `#0c8c7e`。
- **字体**：Archivo Black（巨型重字标题）+ Space Mono（等宽正文/技术标签）+ Noto Sans SC。
- **气质**：粗粝外露、结构外翻，与极简版同浅色但气质相反（粗野 vs 纤细）。

## 内容结构（真实 Agent 工作台）
1. 导航 + Hero（巨型标题 + 青色高亮块 + 工作台 UI 模拟）
2. 实测指标条（R12：首响 3171ms / 模型 223ms / 错误率 0.0% / 验收 6/6）
3. 两阶段架构（① 本地 RAG + ReAct Agent ② 仓库级工作台）
4. 实时多模态引擎高亮（6 项真机能力 + 控制台模拟）
5. 能力网格（8 项，顶部 6px 分区色条，按压反色）
6. CTA + 页脚

## 运行
```bash
cd showcase/docmind-landing-brutalist
python -m http.server 8017
# 打开 http://127.0.0.1:8017
```

## 文件
```
docmind-landing-brutalist/
├─ index.html            # 产品落地页（真实 Agent 工作台 内容）
├─ styles/tokens.css     # 素描纸 + 粗描边 + 硬阴影 + 分区色板
├─ styles/base.css       # 方格坐标纸背景 + reveal + reduced-motion
├─ styles/components.css  # 导航/Hero/工作台模拟/指标/阶段/引擎/能力/CTA/页脚
├─ scripts/main.js       # IntersectionObserver 入场 + count-up + 降级
└─ README.md
```
