# Agent 工作台 集成文档：MCP 引擎桥 与 Web 试玩导出

> 适用对象：`mcp_client.py`（MCP 引擎桥）与 `web_export.py`（Godot Web 试玩导出）。
> 本文档是这两块已落地功能的**产品级使用说明 + 能力边界**，不是规划。

---

## 1. MCP 引擎桥（`mcp_client.py`）

### 它是什么
Agent 工作台 不内置任何引擎专用协议，而是通过标准 **MCP（Model Context Protocol）** 对接外部游戏引擎桥，让 Agent 能调用引擎侧工具（读场景、跑编辑器命令等）。

- **传输方式**：
  - `stdio`：长驻子进程，newline-delimited JSON-RPC 2.0。
  - `http`：Streamable HTTP JSON-RPC（无状态），用标准库 `urllib` 实现，兼容 `application/json` 与 `text/event-stream` 两种响应。
- **内置预设**（默认在代码库根 `.docmind_mcp.json` 中可覆盖）：

| key | 引擎 | 传输 | 默认地址 | 默认启用 |
|-----|------|------|----------|----------|
| `godot` | Godot 4（godot-ai） | stdio | `uvx godot-ai attach` | ✅ |
| `unity` | Unity（unity-mcp） | http | `http://127.0.0.1:8080/mcp` | ❌ |
| `unreal` | Unreal（UnrealMCP） | http | `http://127.0.0.1:3000/mcp` | ❌ |

仅用标准库实现，无第三方 MCP SDK 依赖。

### 配置模型
- **注册表位置**：代码库根目录 `.docmind_mcp.json`，结构 `{"servers": {...}}`。
- **合并规则**：默认预设 + 项目级覆盖做**深合并**；项目配置可禁用预设、改地址、或新增自定义服务器。
- **校验**：`transport` 仅允许 `stdio` / `http`；服务器 `key` 仅允许字母数字 / 下划线 / 连字符，长度 ≤ 40；非法配置会被自动标为 `enabled:false` 并附 `config_error`，不阻塞其它服务器。
- **持久化**：`save_server` / `remove_server` 原子写（写临时文件再 `os.replace`）；删除自定义服务器直接移除，删除预设仅写入 `enabled:false`（不删默认项）。

### API 表面
| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| GET | `/api/mcp/servers` | — | 当前配置列表（默认+覆盖，深合并） |
| POST | `/api/mcp/servers` | `{key, config}` | 新增/更新一个服务器（400 包裹非法配置） |
| POST | `/api/mcp/servers/remove` | `{key}` | 删除；预设仅置 `enabled:false` |
| POST | `/api/mcp/probe` | `{key}` | `initialize` + `tools/list`，返回工具数量（探测用） |
| POST | `/api/mcp/catalog/search` | `{query, web_enabled}` | 搜索 EDA 等领域的连接方式、使用步骤与可信来源 |
| POST | `/api/mcp/discover` | `{key, web_enabled}` | 根据真实 `tools/list` 生成待审批能力与前端表单候选 |
| GET | `/api/mcp/capabilities` | — | 读取已批准和待审批的项目级能力清单 |
| POST | `/api/mcp/capabilities/decision` | `{key, approved}` | 批准或拒绝能力路由候选 |
| GET | `/api/mcp/tools?key=` | query | 列出该服务器工具及 input schema |
| POST | `/api/mcp/call` | `{key, name, arguments}` | 调用工具，返回文本/结构化/其它块 |

> 注意：保存/删除类接口错误以 **HTTP 400** 返回；`probe` / `tools` / `call` 的调用错误统一以 **HTTP 200 + `{ok:false, error}`** 包裹，便于前端直接展示，不会走异常状态码。

### 能力搜索与受控学习

设置 → MCP 的搜索栏可直接搜索 `EDA`、`PCB`、`KiCad` 等需求。允许联网时，系统查询 MCP Registry 并展示通过校验的具体连接候选；领域目录会补查 KiCad、EasyEDA 等关键词，过滤掉名称碰巧包含 `eda` 的无关服务。每项标明社区或官方来源，Registry 收录仅表示有连接参数，不表示已经试连。关闭联网时只显示离线指引，不访问 Registry。搜索摘要不会变成可执行命令。

用户选择候选并点击“测试连接”后，本地 stdio 服务器可能通过 `npx -y` 等启动器首次下载并运行；界面先展示命令和参数。系统读取真实 `tools/list`，生成能力标签、工具映射和通用 schema 表单；只有确认添加、再点击“批准路由”后才参与 Agent 自动选择。EasyEDA MCP 进程可连接但编辑器扩展未就绪时，试连会额外显示扩展状态。拒绝或尚未批准的候选不会改变路由；本流程也不会自动生成专用前端源码。

Agent 的 `dev_mcp_search` 工具沿用同一领域目录和 Registry 查询；是否联网继承当前会话开关。它只返回候选，不写连接配置；模型仍需向用户展示候选，并通过添加连接器和能力审批两道门才能调用。

### 使用前提（务必先满足）
- **godot-ai**：必须通过 **stdio `attach`** 方式连接（`uvx godot-ai attach`）。裸 HTTP 无法完成 capability 轮换认证；且需 Godot 编辑器处于打开状态、并已安装启用 godot-ai 插件。
- **unity / unreal**：需在各自编辑器内运行对应 MCP 插件（unity-mcp / UnrealMCP），默认地址分别为 `:8080` / `:3000`，需在配置中启用。
- **EasyEDA Pro**：需安装目标 MCP 项目的 EasyEDA 扩展、允许外部交互并打开设计文件。MCP 握手成功只说明服务进程可连接；扩展未连接时仍无法读取或修改图纸。
- 首次 `uvx` 冷启动会构建约 67 个包，可能超过常规 30s 超时——本模块已把 `INIT_TIMEOUT` 放宽到 **180s**；单次调用 `CALL_TIMEOUT=120s`，HTTP `HTTP_TIMEOUT=30s`。
- HTTP 服务器为**无状态**：`initialize` 与后续请求相互独立，不保持会话。

### 已知边界 / 注意事项
- **工具结果只抽取文本**：图片 / 二进制等 content 块仅标注其类型（`other_blocks` 字段），不做二进制处理。
- **stdio 会话长驻**：按 `root + key` 缓存；若子进程异常退出，下一次调用会报 `MCPError`（含 stderr 尾部用于排错）。
- **无沙箱**：MCP 服务器作为子进程启动，继承 Agent 工作台 进程环境（含配置注入的 `env`），权限等同本服务本身——**只适合可信的本地代码**，不要接不可信远端。
- **不提供跨服务器聚合**：没有"一次列出所有服务器全部工具"的接口，需按 `key` 逐个 `list_tools` / `call_tool`。
- `pin_godot_server(version)` 可在安装 godot-ai 插件后把 stdio 参数 pin 到与插件相同的发布版本。

---

## 2. Godot Web 试玩导出（`web_export.py`）

### 它是什么
把 Godot 项目导出为 **Web（HTML + WASM）** 并在工作台 iframe 内试玩。导出产物固定在 `<root>/.docmind/web/`（通过 `.gdignore` 排除 Godot 扫描）。

### 它做了什么（全部幂等）
1. **注入 `DocmindBridge` autoload**（`addons/docmind_bridge/docmind_bridge.gd`）：
   - Web：与父页面 `window.postMessage` 双向通信；
   - 桌面：打印 `DOCMIND_EVENT <json>` 供运行时时间线抓取。
   - 游戏代码可调用 `DocmindBridge.emit("player_damaged", {"hp": 90})` 上报事件；支持 `ping` / `reload` / `quit` 控制命令。
2. **生成/合并 `export_presets.cfg` 的 `Web` preset**（已存在则原样保留）；预设含 `runnable=true`、`html/canvas_resize_policy=2`、`focus_canvas_on_start` 等。
3. **Web 导出模板检测与按需安装**：从官方 GitHub `.tpz` 仅提取 `web_*` 几个变体 zip（约 90MB，省去整包 1GB+）；支持镜像回退（`DOCMIND_TPZ_MIRRORS` 环境变量）。
4. **导出后处理** `index.html`：注入父页面消息转发脚本，打通 iframe ↔ 游戏命令通道。

### API 表面
| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| GET | `/api/engine/web/templates` | — | 模板状态：引擎版本、模板目录、`web_debug`/`web_release`、已存在变体、安装进度 |
| POST | `/api/engine/web/templates/install` | `{confirm:true}` | 后台下载安装（需**明确确认**，约数百 MB） |
| POST | `/api/engine/web/export` | — | 幂等注入 bridge+preset → 执行 `--export-release Web` → 后处理；返回 `{ok, token, url:"/play/<token>/index.html", files}` |
| GET | `/play/{token}/{file_path}` | path | 静态服务试玩产物；`token` 绑定代码库绝对路径（跨项目隔离），带 COOP/COEP 以启用 SharedArrayBuffer，`no-cache` + ETag |

> 试玩 `token` = `sha1(normcase(代码库绝对路径))[:16]`，与当前代码库一一对应，不同项目互不可见。

### 使用前提（务必先满足）
- **仅支持 Godot**：需先配置代码库（`CODE_ROOT`）并能被识别到 Godot 可执行文件。
- 需安装**对应版本**的 Web 导出模板（`web_debug.zip` / `web_release.zip` 及 nothreads 变体）；未检测到会返回 `missing_templates` 错误。
- 自动安装**仅支持 stable 正式版**模板（beta/dev 请在编辑器内手动安装）。
- `--export-release` 耗时随工程规模变化，默认 `timeout=300s`。

### 已知边界 / 注意事项
- 模板下载来自官方 GitHub（约数百 MB），需用户显式确认，且依赖外网可达（GitHub 直连或镜像）。
- 产物文件名固定（`index.pck` / `index.js` 无哈希），重新导出后浏览器需重新验证；`/play` 用 `no-cache` + ETag 处理（未变走 304，变了立即拉新）。
- bridge 脚本**幂等**：已存在且内容一致则不重写；`project.godot` 的 `[autoload]` 已含则跳过。
- 试玩 iframe 与游戏的双向通信依赖 `window.postMessage`，需在**同一父页面内嵌**。
- 本模块只覆盖 **Web** 平台导出；其它平台导出不在范围内。

---

## 3. 二者关系：何时用哪个
- **MCP 引擎桥**：接"活的"引擎编辑器/进程，让 Agent 调用引擎侧工具——偏**开发期集成**。
- **Web 试玩导出**：把工程导出成可玩网页——偏**交付/预览**。
- 两者**正交**，可独立使用，也可组合（例如用 MCP 桥在编辑器里改完场景，再导出 Web 试玩）。
