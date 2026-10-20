# 任务域（P3）：任务范围/快照/回滚 + 性能剖析

> 2026-10-21。承接 [engine-wiring-p1.md](engine-wiring-p1.md) 第 5 节列的最后一个缺口
> （"任务域的 validate/snapshot/revert：客户端有、零调用点"），本文记录任务域这条线的接线与修法。

---

## 1. 起点：端点一直在，前端一个都不调

| 后端端点 | 用途 | 前端（改前） |
| --- | --- | --- |
| `POST /api/tasks/validate` | 校验任务文件是否都在声明的分区/允许路径内 | 客户端有，**零调用点** |
| `POST /api/tasks/snapshot` | 存任务文件快照（mtime/size + ≤512KB 的正文） | 客户端有，**零调用点** |
| `POST /api/tasks/revert` | 回滚到快照（覆盖或删除任务文件） | **连客户端都没有** |
| `POST /api/tasks/impact` | 影响范围（语义 + 关系图） | 有调用点，但失败体被丢 |
| `POST /api/performance` | 性能剖析（Python 走真实 cProfile） | **无客户端** |

## 2. 关键坑：这几个端点的失败体里**没有 `error` 字段**

后端表达"任务范围越界"的方式是 **HTTP 422 + body 里的 `scope`**：

```json
{"ok": false, "scope": {"ok": false, "errors": [{"path": "README.md", "error": "文件不在任务分区内"}], ...}}
```

而通用 `request()` / `postJson()` 遇到 `ok:false` 会**直接抛异常** → 前端拿到的只有
`请求失败（HTTP 422）`，**越界文件列表整个丢失**。用户看到的是"失败了"，但不知道是哪几个文件越界。

所以这一批端点一律走 `rawJson`（只在网络不可达 / 响应非 JSON 时才抛）。

各端点的失败体形状**并不一致**，接入时必须逐个对着后端实现确认：

| 端点 | 越界信息在哪 |
| --- | --- |
| `/api/tasks/validate` | **顶层** `errors`（它直接返回 `TaskScope` 本体，永远 200） |
| `/api/tasks/snapshot` | `scope.errors`（422） |
| `/api/tasks/revert` | `scope.errors`（422） |
| `/api/tasks/impact` | `scope.errors`（422） |
| `/api/performance` | 没有 `error`，有用的是 `output`（命令报错原文）+ `metric`（口径） |

## 3. 这次补上的

### 3.1 任务面板新增三个入口（`TaskEnginePanel.vue`）

| 按钮 | 行为 |
| --- | --- |
| 校验范围 | 调 `validate`，越界项**点名到文件**列在下方（`README.md · 文件不在任务分区内`） |
| 存快照 | 调 `snapshot`，成功后回滚按钮才可用 |
| 回滚快照 | 调 `revert`，**破坏性操作** → 走 `askConfirm` 二次确认，文案明说「这是破坏性操作」 |

回滚按钮用琥珀色（`--amber`）与旁边的普通动作区分开。

### 3.2 性能剖析（`EngineSetupPanel.vue` 的「引擎工具」分区）

`POST /api/performance`：Python 脚本走**真实 cProfile**（累计耗时前 15），
其余命令只测墙钟。结果行给出 **输出 / 口径 / 耗时** 三项，
口径写明 `cProfile(top15 by cumulative)` —— 不写口径的话，一张耗时表没法解读。

## 4. 途中修掉的三个问题

1. **`TaskScope` 导入后没人用**（eslint `no-unused-vars`）——
   `validate` 改成 `rawJson<TaskScope & {error?: string}>` 后类型才真正对上后端返回体。
2. **「分析影响范围」会无限挂起**——`impact` 合法范围下要走语义检索，
   实测冷启动能挂 **120 秒以上**不给回音。给它加了 **30 秒上限**，
   超时按业务失败返回明确文案，而不是让按钮一直转圈。
3. **越界列表会残留**——上一轮分析留下的越界项，在下一轮成功后仍然显示。
   现在成功路径统一清空 `scopeErrors`。

## 5. 实机验证（真实后端 8012 + Edge + 项目 `rag-agent`）

| 验证项 | 结果 |
| --- | --- |
| 校验范围（故意放 `README.md` 到 `tools` 分区） | `任务范围有 1 处越界`，列表点名 `README.md · 文件不在任务分区内` ✅ |
| 分析影响范围（越界） | `影响范围算不了：任务范围有 1 处越界`，同样点名到文件 ✅ |
| 分析影响范围（合法） | `影响文件 1 个`，越界列表已清空 ✅ |
| 存快照（合法范围） | `已存快照（1 个文件，可用于回滚）`，回滚按钮随之启用 ✅ |
| 回滚（点确认，真的执行） | 确认框文案含「这是破坏性操作」；执行后磁盘内容**真的回到快照那一刻** ✅ |
| 性能剖析 | 真跑出 cProfile top15 表，口径 `cProfile(top15 by cumulative)`，耗时 1.02s ✅ |
| 页面异常 | 无 ✅ |

回滚验证是**真执行**：在 `.tmp/revert-probe.txt` 存快照 → 把它改坏 →
走 UI 回滚 → 读盘确认内容已恢复 → 清理探针文件。

## 6. 门禁

构建 ✓（含 `vue-tsc`）/ eslint 16（**均既有**，本次改动的三个文件干净）/ 前端 node 单测 89 /
视觉回归 5/5 / 产物新鲜度 PASS / 皮肤令牌对 35 项 / 后端任务域单测 41 项。

## 7. 相关文件

| 文件 | 角色 |
| --- | --- |
| `frontend/src/workbench/api/engine.ts` | 改：`validate/snapshot/impact` 改 `rawJson`；**新** `revert` / `performance` 客户端 |
| `frontend/src/workbench/components/TaskEnginePanel.vue` | 改：新增校验范围 / 存快照 / 回滚快照三个入口 + 越界列表 |
| `frontend/src/workbench/components/EngineSetupPanel.vue` | 改：新增「性能剖析」工具与口径/耗时两行结果 |

## 8. 还没做的

- `POST /api/desktop/resize`（桌面宿主自调窗口尺寸）：需要先确认宿主是谁在调，否则是自调自的循环。
- Unreal 写回端点：后端恒失败，属死路，不接。
