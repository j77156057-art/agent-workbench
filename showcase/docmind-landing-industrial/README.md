# DocMind 产品落地草案（工业控制台版 / v3）

与 `docmind-landing-minimal/`（极简亮色）并列的**产品首页草案**——DocMind 作为**真实产品**的落地首页，
可直接作为并入 `frontend/` 的视觉基线候选。

## 视觉方向
- **暗色 cockpit**：底色 `#0b0e13`、面板 `#11161f`、墨色 `#e7edf5`。
- **顶部遥测条**：ENGINE / MODE / UPLINK / SESSIONS / LATENCY / R12 状态，LED 状态点（绿/青发光）。
- **主色 `#2ec4b6`**（品牌青，audio 区）+ 真实分区色板点缀；LED 状态色：绿 `#45c98c` / 琥珀 `#e3a83a` / 红 `#ff6b6b` / 青 `#58a6ff` / 紫 `#bc8cff`。
- **字体**：Archivo（展示标题）+ 系统 UI + JetBrains Mono（技术标签/数值）。
- **气质**：密排网格（能力区 1px 间隙）、硬边线分层、LED 芯片、无渐变光晕噪音。最接近 Devin Desktop / dsh-dashboard 的 cockpit 血统，也最贴 DocMind「深色精密仪表」身份。

## 内容结构（真实 DocMind）
1. 顶部遥测条 + 导航 + Hero（产品定位 + 工作台 UI 模拟）
2. 实测指标条（R12：首响 3171ms / 模型 223ms / 错误率 0.0% / 验收 6/6）
3. 两阶段架构（① 本地 RAG + ReAct Agent ② 仓库级工作台）
4. 实时多模态引擎高亮（6 项真机能力 + 控制台模拟）
5. 能力网格（按 8 分区色板着色，1px 间隙密排）
6. CTA + 页脚

## 运行
```bash
cd showcase/docmind-landing-industrial
python -m http.server 8016
# 打开 http://127.0.0.1:8016
```

## 文件
```
docmind-landing-industrial/
├─ index.html            # 产品落地页（真实 DocMind 内容 + 顶部遥测条）
├─ styles/tokens.css     # 暗色 cockpit + LED 色 + 分区色板
├─ styles/base.css       # 重置 + 排版 + reveal + reduced-motion
├─ styles/components.css  # 遥测条/导航/Hero/指标/阶段/引擎/能力/CTA/页脚
├─ scripts/main.js       # IntersectionObserver 入场 + count-up + 降级
└─ README.md
```
