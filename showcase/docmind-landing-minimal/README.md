# DocMind 产品落地草案（极简亮色版 / v2）

与 `docmind-landing-industrial/`（暗色工业控制台）并列的**产品首页草案**——不再是之前的「实时引擎能力展示页」，
而是 DocMind 作为**真实产品**的落地首页，可直接作为并入 `frontend/` 的视觉基线候选。

## 视觉方向
- **暖纸白**：底色 `#f3f1ea`、卡片 `#faf9f5`、墨色 `#16130d`（对齐用户选定的极简方向）。
- **单一主色 `#0c8c7e`**（品牌青浅色适配，region.audio `#2ec4b6` 加深满足 AA）+ 真实分区色板做点缀：audio `#2ec4b6` / values `#58a6ff` / assets `#e3a83a` / behaviors `#bc8cff` / bugs `#ff6b6b` / levels `#45c98c` / ui `#f072b6` / net `#f0883e`。
- **字体**：Bricolage Grotesque（展示标题）+ 系统 UI 字体 + JetBrains Mono（技术标签/数值）。
- **气质**：克制、靠留白与发丝线分层，无扫描线/颗粒/渐变光晕，无 emoji、无紫粉渐变（符合项目前端 P0 红线）。

## 内容结构（真实 DocMind）
1. 导航 + Hero（产品定位 + 工作台 UI 模拟）
2. 实测指标条（R12：首响 3171ms / 模型 223ms / 错误率 0.0% / 验收 6/6）
3. 两阶段架构（① 本地 RAG + ReAct Agent ② 仓库级工作台）
4. 实时多模态引擎高亮（6 项真机能力 + 控制台模拟）
5. 能力网格（按 8 分区色板着色）
6. CTA + 页脚

## 运行
```bash
cd showcase/docmind-landing-minimal
python -m http.server 8015
# 打开 http://127.0.0.1:8015
```

## 文件
```
docmind-landing-minimal/
├─ index.html            # 产品落地页（真实 DocMind 内容）
├─ styles/tokens.css     # 真实浅色主题 + 分区色板
├─ styles/base.css       # 重置 + 排版 + reveal + reduced-motion
├─ styles/components.css  # 导航/Hero/指标/阶段/引擎/能力/CTA/页脚
├─ scripts/main.js       # IntersectionObserver 入场 + count-up + 降级
└─ README.md
```
