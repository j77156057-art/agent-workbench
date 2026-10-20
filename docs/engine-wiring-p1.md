# 引擎线（P1）：让"选引擎"这件事在前端真正存在

> 2026-10-20。承接 [realtime-frontend-wiring.md](realtime-frontend-wiring.md)，
> 本文记录游戏引擎这条线的缺口与修法。

---

## 1. 起点：后端有整条链，前端一个调用点都没有

审计时量到的差集（后端 271 端点 / 前端实调 140）里，引擎线是最大的一块：

| 后端端点 | 用途 | 前端 |
| --- | --- | --- |
| `GET /api/engine/scan` | 扫描项目文件识别引擎（`project.godot` / `ProjectVersion.txt` / `*.uproject`） | **无客户端** |
| `GET /api/engine/catalog` | 三引擎目录（id / 名称 / 可执行文件 / 官方下载页 / 工程标志文件） | 客户端有，**零调用点** |
| `GET/POST /api/engine/config` | 读/写项目当前引擎与可执行文件（落在项目状态 `engine.json`） | 客户端有，**零调用点** |
| `POST /api/engine/prepare` | 探测可执行文件并写入项目配置 | **无客户端** |
| `GET /api/engine/skill` | 该引擎的适配 Skill 文本（给 AI 读的项目约定） | **无客户端** |

后果很具体：**引擎在前端被写死成 Godot**。

```ts
// SceneRuntimePanel.vue:307
const r = await engineApi.start('godot', !!rect, rect)      // 写死
// TaskEnginePanel.vue:22/24
await engineApi.start()                                        // 默认值 'godot'
await engineApi.verify()                                       // 默认值 'godot'
// TaskEnginePanel 的文案更是直接写着「Godot 已热重载」「Godot 校验通过」
```

所以：Unity / Unreal **无法选择、无法启动、无法校验**；连"这个项目到底是哪个引擎"
前端都不问，用户看到的是"引擎按钮"和一份永远写死 Godot 的界面。

## 2. 这次补上的

### 2.1 设置页新增「引擎与项目」分区（`EngineSetupPanel.vue`）

放在设置页「运行与引擎」分区里，与「游戏引擎连接」并列 —— 两者都是**配置类**能力，
而"启动/嵌入/热重载"是运行类能力，仍归运行槽。分区里四件事：

1. **项目探测**：调 `scan`，列出识别到的引擎工程文件；命中的引擎打上「本项目」标记
2. **引擎选择**：从 `catalog` 渲染 Godot / Unity / Unreal 三选一
3. **可执行文件**：随引擎自动填默认值，可改；`保存` 写项目配置
4. **准备引擎**：`prepare` 探测可执行文件并落配置；找不到时**给出官方下载链接**
5. **适配说明**：`skill` 端点的文本（可折叠）

### 2.2 引擎不再写死

新增 `composables/useEngineConfig.ts`：把"项目当前用哪个引擎"读出来给运行类界面用。

- `SceneRuntimePanel` 启动前 `await engineCfg.load()`，再 `start(engineCfg.engine.value, …)`
- `TaskEnginePanel` 的 `start()` / `verify()` 同理；文案从写死的「Godot」改成跟随引擎名

### 2.3 入口可深链

`App.vue` 的 `settingsSection` 类型原本是 `'usage' | 'search' | 'mcp' | 'agent' | 'model'`，
**不含 `'runtime'`** → 设置页的「运行与引擎」分区无法深链直达，只能打开设置面板再点导航。

现在：`settingsSection` 加上 `'runtime'`，并在顶栏「更多」菜单加一项「运行与引擎」直达。

### 2.4 清掉一个死事件

`docmind:open-engine` 事件**有类型声明、有监听者（EngineConnectPopover）、无人派发** ——
顶栏入口在 2026-10-19 撤走后就一直是死的。现在连接器弹层内嵌在设置页（常驻可见），
事件通路没有必要，声明与监听一并删除。

## 3. 途中修掉的一个真缺陷：下载链接被吞了

「准备引擎」在找不到可执行文件时，后端返回的是**失败体**：

```json
{"ok": false, "ready": false, "engine": "unity",
 "download": "https://unity.com/download", "error": "未找到引擎可执行文件。"}
```

而 `postJson` 遇到 `ok:false` 会直接抛异常 → 前端拿到的只有 `error`，
**`download` 字段整个丢失** → 用户只知道"没找到"，不知道去哪装。
改用 `rawJson`（只在网络/JSON 层面抛错，不按业务失败抛）后，链接回来了。

## 4. 实机验证

装置：真实后端（8012）+ 真实项目 `godot_sample`（含 `project.godot`）。

| 步骤 | 结果 |
| --- | --- |
| 顶栏「更多 → 运行与引擎」 | 直接落到设置页「运行与引擎」分区（`activeNav=运行与引擎`）✅ |
| 项目探测 | `Godot  project.godot`，Godot 一项标「本项目」✅ |
| 切到 Unity | 可执行文件自动跟到 `Unity.exe` ✅ |
| 保存 | 后端读回 `{"engine":"unity","executable":"Unity.exe"}` ✅ |
| 准备引擎（Unity，本机未装） | `未找到引擎可执行文件。 请先安装：https://unity.com/download` ✅ |
| 改回 Godot 并保存 | 后端读回 `{"engine":"godot","executable":"godot"}` ✅ |
| `prepare(godot)`（后端直调） | `ready: true`，自动解析出 `D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe` ✅ |
| `skill(godot)` | 792 字符适配说明 ✅ |
| 页面异常 | 无 ✅ |

## 5. 还没做的（引擎线剩下的缺口）

| 缺口 | 性质 |
| --- | --- |
| `GET /api/engine/inspect`（Unity/Unreal 资产索引）无前端 | 整个功能缺前端 |
| `GET /api/engine/unreal-bridge/blueprint|actor` 的客户端有但**零调用点** | 缺 UI 展示（Unreal 蓝图/Actor 详情） |
| `POST /api/engine/unreal-bridge/install`（装桥接脚本）无前端 | 缺入口 |
| `POST /api/engine/check`（Godot 单文件校验）无前端 | 缺入口 |
| `POST /api/engine/diagnostics`（Unreal 诊断解析）无前端 | 缺入口 |
| `POST /api/playtest`（HTTP 玩测）无前端 | 只有 agent 工具能调 |
| `POST /api/runtime/probe`（装 GDScript 探针）无前端 | 缺入口 |
| 任务域的 `validate/snapshot/revert`（客户端有、零调用点） | 缺入口 |

这些属于同一类"后端有、前端没入口"，但**优先级低于本轮**：
先让"引擎是什么、能选什么"这件事存在，剩下的是逐个补面板。

## 6. 相关文件

| 文件 | 角色 |
| --- | --- |
| `frontend/src/workbench/components/EngineSetupPanel.vue` | **新**：引擎与项目设置块 |
| `frontend/src/workbench/composables/useEngineConfig.ts` | **新**：项目当前引擎的读取 |
| `frontend/src/workbench/api/engine.ts` | 改：补 `scan()` / `prepare()` / `skill()` 客户端；`prepare` 改用 `rawJson` |
| `frontend/src/workbench/components/SceneRuntimePanel.vue` | 改：启动读配置，不再写死 `godot` |
| `frontend/src/workbench/components/TaskEnginePanel.vue` | 改：同上 + 文案跟随引擎名 |
| `frontend/src/workbench/components/SettingsView.vue` | 改：新增「引擎与项目」分区 |
| `frontend/src/workbench/App.vue` | 改：`settingsSection` 加 `runtime` + 顶栏入口 |
| `frontend/src/workbench/eventBus.ts` | 改：删掉死事件 `docmind:open-engine` |
| `frontend/src/workbench/components/EngineConnectPopover.vue` | 改：删掉死事件监听 |
