# EDA 线（P2）：补上"连接 EDA 工具"这个出口

> 2026-10-20。承接 [realtime-frontend-wiring.md](realtime-frontend-wiring.md)（P0）与
> [engine-wiring-p1.md](engine-wiring-p1.md)（P1），本文记录 EDA 这条线的缺口与修法。

---

## 1. 起点：面板是有的，但"连上 EDA 工具"这件事不存在

先说清楚：**EDA 验收面板不是没有**。`EdaVerifyPanel.vue`（642 行）+ `api/eda.ts`（5 个客户端）
+ 顶栏「更多 → 工具与运行 → EDA 多模态验收」入口，齐全，且已打进产物。

真正缺的是三样：

| 缺口 | 后端 | 前端 |
| --- | --- | --- |
| **真跑 Vivado/KLayout/Verdi** | `agent_runtime/eda_tcl.py` 的 `run_tcl` / `run_and_parse` 早就能跑 | **既无 HTTP 端点、也无 agent 工具、无 UI** → "连接 EDA 工具"无从谈起 |
| **skill 说明书恒为空** | `eda_adapters.skill_for_tool()` 从 `.trae/skills/eda-adapters/SKILL.md` 读 | **该文件压根不存在** → `/api/eda/skills` 永远返回空串；前端 `edaApi.skills()` 也**零 UI 消费者** |
| **三个已支持的参数没暴露** | `verify` / `report-gate` 都支持 `critical`（自定义判定量键）、`bbox_norm`（显式 ROI）、`url` + `fetch='web_fetch'`（由后端抓报告页） | 面板固定 `fetch:'inline'` + 手贴正文，`critical` 不传，`bbox_norm` 不传 |

第三条尤其可惜：后端早就支持"把报告页地址交给 DocMind 自己抓"，这是最自然的产品用法，
而前端**根本没有这个输入框**。

## 2. 这次补上的

### 2.1 后端：新增 `POST /api/eda/run`（唯一执行类端点）

`api_routes/eda.py` 新增 `EdaRunReq` 与端点，跑脚本 → 解析成结构化事实。

**安全边界**（这是执行类接口，必须说清楚）：

| 约束 | 实现 |
| --- | --- |
| 必须显式确认 | `confirm` 不为 true 一律 **HTTP 400**，且**不执行任何东西**（与 `unreal-bridge/install` 的 confirm 门同一范式） |
| 只认登记在册的三个工具 | `vivado` / `klayout` / `verdi`，其余 400 |
| **不是任意 shell** | 执行面由 `eda_tcl._TOOL_COMMAND` 的固定 argv 模板限定（`vivado -mode batch -nojournal -source <tmp>.tcl` 等） |
| 工作目录锁在项目根 | `cwd=root` |
| 脚本长度 / 超时有上限 | 200KB / 600s（上限 900s） |
| 工具不可用 → fail-closed | `tool_unavailable:<tool>`，**绝不**返回空 stdout 让上层误判成"报告显示通过" |

### 2.2 后端：补上 SKILL.md（让 skill 接口不再空转）

新建 `.trae/skills/eda-adapters/SKILL.md`，按 `eda_adapters._TOOL_KEY` 认的段名分节：
`## Vivado` / `## KLayout` / `## Verdi` / `## Quartus` / `## Innovus` / `## Shared workflow`。

内容不是套话 —— 每节写的是**该工具的报告长什么样、解析器认哪几行**，例如：

- Vivado timing 解析器要的是 Design Summary 表头（同时含 `WNS(ns)` 与 `TNS(ns)`）
  后面那行六个数 `WNS TNS <Failing> <Total> WHS THS`；只写 `WNS = -0.123 ns` 也有兜底。
- KLayout 优先按 XML 报告数 `<check>` 下的 `<item>`，取不到再退回文本里的
  `Total violations: N`。
- 仿真没跑到断言**不算通过**；Quartus/Innovus 尚无解析器 → 结构化事实必然是
  `unsupported_parser:…`（uncertain），明说"按眼力翻译不算数"。

### 2.3 前端：面板补三个参数 + skill + 真跑脚本

`EdaVerifyPanel.vue`：

| 控件 | 对应参数 |
| --- | --- |
| 取正文（手贴正文 / **由后端抓取报告页**） + 报告地址 | `fetch` / `url` |
| 判定量键 critical（逗号分隔） | `critical` |
| ROI 归一框 x1,y1,x2,y2 | `bbox_norm`（覆盖 `view`） |
| 适配说明（可折叠） | `GET /api/eda/skills?tool=` |
| 连接 EDA 工具（真跑脚本，默认收起） | `POST /api/eda/run`，**先弹确认再执行** |

后端失败码翻成人话：`tool_unavailable:vivado` → "本机没找到 vivado 可执行文件 —— 先装好该工具再跑。"

## 3. 途中挖出并修掉的一个**系统级**缺陷：确认框点不动

跑真机验证时发现：点「运行并解析」后确认框**弹出来了，但按钮点不动** ——
点击位置命中的是面板里的 `TEXTAREA`，不是确认按钮。

用 `document.elementsFromPoint()` 取真实绘制顺序，根因清楚了：

```
maskChain: DIV.dg-mask → pos:fixed z:1100  ←  外层 DIV.wb-shell → isolation
popChain:  DIV.ev-pop  → pos:fixed z:300
```

**`.wb-shell` 带 `isolation: isolate`（自建层叠上下文）**，于是：

- AppDialog 的确认框在**壳内**排序（z:1100 只在壳内有效）；
- EDA 面板 teleport 到 **body**（根上下文，z:300）；
- **根上下文里的元素一律压过壳内的一切** → 面板盖住确认框。

所以这是个**类问题**：凡是 teleport 到 body 的面板（EDA 面板 z:300 / 实时面板 z:120 /
顶栏菜单 z:61）都会盖住壳内的任何对话框。反过来也成立：设置弹窗（壳内 z:2100）里弹出的
确认框，**原本被设置弹窗自己盖住**。

修法：把 `AppDialog` 整体 `<Teleport to="body">`。样式全是全局的（`.dg-mask` / `.dg-box` /
`.dg-btn`），没有任何选择器依赖它待在壳内 → 改动零风险。teleport 后它在根上下文排序，
z:1100 高于所有 body 级面板，且**同时修好了设置弹窗里确认框被自己盖住**的那处。

## 4. 实机验证

| 验证项 | 结果 |
| --- | --- |
| `/api/eda/skills?tool=klayout` | 2159 字符，含 `## KLayout` 与 `## Shared workflow` ✅ |
| `/api/eda/skills?tool=vivado` | 2388 字符，含 `## Vivado` ✅ |
| `/api/eda/run` 无 confirm | HTTP 400「运行 EDA 脚本会真的调起工具并执行这段脚本，需要显式确认。」✅ |
| `/api/eda/run` 非法工具（rm） | HTTP 400「不支持的 EDA 工具（只认 vivado/klayout/verdi）。」✅ |
| `/api/eda/run` 有 confirm（本机没装 Vivado） | 200 + `ok:false` + `tool_unavailable:vivado`（fail-closed）✅ |
| 面板「适配说明」 | 展开 2388 字符 skill 文本 ✅ |
| 面板「取正文」切到「由后端抓取」 | 报告地址输入框出现、正文框隐藏 ✅ |
| 面板「运行并解析」 | 先弹确认框（文案含"以项目根为工作目录"/"fail-closed"）→ 确认后如实报「未拿到事实：本机没找到 vivado 可执行文件」✅ |
| 确认按钮命中 | `BUTTON.dg-btn dg-primary`（修前是 `TEXTAREA.ev-text`）✅ |
| 页面异常 | 无 ✅ |

后端测试：`tests/test_api_eda_routes.py` 16 项全过（新增 7 项：confirm 门 / 工具白名单 /
空脚本 / 缺项目根 / fail-closed / skill 分段）；EDA 全套 69 项全过。

## 5. EDA 线还剩什么

| 缺口 | 性质 |
| --- | --- |
| `eda_verify_pipeline.py` 的七段编排仍只作 CLI/活文档，未被任何路由或工具调用 | 后端内部编排，无出口 |
| `run_tcl` 只接了 HTTP，**没接 agent 工具** —— 模型目前不能自己跑 Vivado | 缺 agent 工具（要进 `tools.py` 注册表并过护栏） |
| Quartus / Innovus 尚无报告解析器 | 缺解析器（skill 里已明说这两家是"指导级"，结构化事实必然 uncertain） |

## 6. 相关文件

| 文件 | 角色 |
| --- | --- |
| `api_routes/eda.py` | 改：新增 `EdaRunReq` + `POST /api/eda/run`（confirm 门 + 工具白名单 + 上限） |
| `.trae/skills/eda-adapters/SKILL.md` | **新**：按工具分段的适配说明（后端缺的那块素材） |
| `frontend/src/workbench/api/eda.ts` | 改：新增 `run()` 客户端；`EdaFact` 补 `reason` |
| `frontend/src/workbench/components/EdaVerifyPanel.vue` | 改：三个参数 + skill 展示 + 真跑脚本（含确认） |
| `frontend/src/workbench/components/AppDialog.vue` | 改：teleport 到 body（修"确认框被面板盖住"这类问题） |
| `tests/test_api_eda_routes.py` | 改：新增 7 项（confirm 门 / 白名单 / fail-closed / skill 分段） |
