# DocMind · 实时视频对话引擎 · 能力展示页

独立、可运行的前端展示页（不依赖 `frontend/` 业务代码，规避并发写入）。用于对外演示 / 官网能力页。

## 设计定调（Direction Lock）

| 维度 | 决策 |
|---|---|
| **目的** | 向技术决策者 / 独立游戏·Mod 开发者展示 DocMind 的实时视频对话引擎能力 |
| **受众** | 技术负责人、独立开发者、内部团队 |
| **调性** | 复古未来 × 暗色精密仪表控制台（retro-futuristic instrument） |
| **展示字** | `Chakra Petch`（技术感，非禁用清单字体） |
| **数据/正文** | `JetBrains Mono`（与项目 `theme.ts` 的 `--font-mono` 同源）+ 中文 `Noto Sans SC` |
| **主色** | 近黑 `#0a0e14` |
| **主强调** | 音频青 `#2ec4b6`（直接抽取自项目 `theme.ts` 的 `region.audio`） |
| **结构青** | 数值蓝 `#58a6ff`（`region.values`，用于数据/网格） |
| **信号琥珀** | 资产金 `#e3a83a`（`region.assets`，用于告警闸门） |
| **记忆点** | 随语音脉冲的实时会话控制台 + 从项目真实 region 调色板抽出的霓虹，一眼可辨「这是 DocMind 的实时引擎」 |

调色板与产品本体同源（`frontend/src/workbench/theme.ts`），保证展示页不套通用模板、可被老用户一眼认出。

## 文件结构

```
showcase/docmind-realtime/
├── index.html            # 语义结构 / 内容（真实 R12 指标与能力点）
├── styles/
│   ├── tokens.css        # 设计令牌：颜色 / 字阶 / 间距 / 圆角 / 阴影 / 动效
│   ├── base.css          # 重置 / 排版 / 焦点 / 氛围层 / 降级动效
│   └── components.css    # 导航 / HERO / 控制台 / 指标 / 能力卡 / 管线 / 跑马灯 / CTA
├── scripts/main.js       # IntersectionObserver 入场 + 指标 count-up + 降级
└── README.md
```

## 内容来源（真实、可复核）

- 指标：首响应 3171ms / 模型延迟 223ms / 错误率 0.0%（75 轮）/ R12 验收 6/6 —— 来自 `docs/realtime-r12-acceptance-20260929.md`。
- 能力点：语音即输入、画面即上下文、打断即响应、静音尾收句、断线自恢复、音频先行闸门 —— 均为 R14 复审中实测修复的真实能力。
- 管线闸门：`audio-before-video` 是 DashScope 的硬约束，对应 `8cefac0` + `d3e376c` 双层修复。

## 设计令牌对照

| 令牌 | 值 | 来源 |
|---|---|---|
| `--accent` | `#2ec4b6` | `theme.ts` region.audio |
| `--cyan` | `#58a6ff` | `theme.ts` region.values |
| `--amber` | `#e3a83a` | `theme.ts` region.assets |
| `--bg` | `#0a0e14` | 暖调近黑，非纯黑 |
| `--font-mono` | JetBrains Mono | `style.css` `--font-mono` |

## 动效规格

- 入场序列：3–6 组，错峰 90–450ms（`reveal.d1–d5`）。
- 指标：进入视口后 1100ms ease-out count-up。
- 氛围：`atmos`（渐变网格 + CRT 扫描线）/ `grain`（颗粒）固定层。
- 悬停：卡片上浮 + 边框转青、导航下划线扫过、按钮辉光。
- 降级：`prefers-reduced-motion: reduce` 下关闭全部动效、隐藏氛围层。

## 反 AI 味自检（QA）

| # | 项 | 结果 |
|---|---|---|
| 1 | 展示字非禁用清单 | ✅ Chakra Petch（非 Inter/Roboto/…） |
| 2 | 配色非白底紫渐变 | ✅ 暗色仪表 + 青/蓝/琥珀 |
| 3 | 首屏非居中三卡 | ✅ 非对称 HERO + 实时控制台 |
| 4 | 卡片非千篇一律 | ✅ 能力卡带装饰括号 + 语义图标 |
| 5 | 图标非纯装饰 | ✅ 每项图标对应真实能力语义 |
| 6 | 文案无 AI 套话 | ✅ 用真实指标与能力命名 |
| 7 | 动效非齐步淡入 | ✅ 编排错峰 + 滚动触发 |
| 8 | 有氛围层 | ✅ 渐变网格 + 扫描线 + 颗粒 |
| 9 | 有记忆点 | ✅ 实时会话控制台 |
| 10 | 状态完整 | ✅ 连接/录制/实时 chip、光标闪烁 |
| 11 | 可访问性 | ✅ 对比度 ≥4.5:1、焦点圈、降级动效、44px 目标 |
| 12 | 响应式 | ✅ 980 / 620px 两档重排 |

## 运行

直接用浏览器打开 `index.html` 即可（字体走 Google Fonts CDN，`font-display: swap`，离线回退系统字体）。
