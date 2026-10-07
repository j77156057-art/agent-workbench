# 下一步清单（写给上下文重置后的自己 / 接手的人）

> 生成于 HEAD `12f25a3`。目的是**上下文满掉之后不丢线索**。
> 详细缺陷分析见 `docs/known-defect-two-flaky-network-tests.md`。

## 0. 当前状态

- **HEAD `12f25a3`**，工作树里有 3 个前端文件被改（`skins.css` / `skins.ts` / `styleMode.ts`，-424/+63）
  —— **那是别人的工作，不是本会话改的，不要 reset、不要提交它们**。
- 本会话的提交全部用私有索引（`GIT_INDEX_FILE=.tmp/private-*.index`）只 add 自己的路径。

提交序列：

| commit | 内容 | 动生产代码？ |
|---|---|---|
| `e0bdb53` | 否定句：否定只挖掉它自己管的那个动词 | `agent.py` |
| `966212a` | 写意图改三值 granted/denied/uncertain | `agent.py` |
| `6069386` | 词表缺口（合法写请求被误拦 14/14 → 1/17） | `agent.py` |
| `cd21017` | 疑问句带写动词 = 在问，不是授权 | `agent.py` |
| `773deaf` | reaper pid 洞：读空 → 0，而 `pid_alive(0)` 恒真 → 测试恒红 | **只改测试** |
| `ab45a56` | 删掉写死的 3.5 秒预算，判据改用 fixture 造的刻度 | **只改测试** |
| `12f25a3` | 记录两条随机红 + 撤回 6 处 capability 误报 | 只加 docs |

全量验收：**4445 passed / 2 failed / 10 skipped / 1035 subtests**，与改动前 4441 正好 **+6**
（新增守卫数），`subtests` 仍 1035 —— **六笔没动任何带 subtest 的既有契约**。
**两条红都不由这六笔引起。**

---

## 1. 最该先做的：多轮批准会失效（假设，**未验证**）

**这是目前最值钱的一条，因为只有真跑才暴露。**

写意图分类是**逐条消息、无状态**的。真实会话里批准长这样：

```
用户：修复要几步        -> denied（已修，对）
agent：大概三步
用户：改吧              -> ???
```

「改吧」的「改」是**裸形式**，而裸 `修/改/动/写` 是**刻意排除**的词
（加了就会误放行「这个方案怎么改比较好？」）。

**若「改吧」落进 denied，则用户明确批准、agent 仍不动 —— 批准语恰好是被排除的词汇。**

**要验什么**：`_write_intent("改吧")` / `"改一下"` / `"动手改"` / `"可以改了吗"` 各自返回什么。
- 若返回 denied → 这是个真缺陷，且是**六笔的同一族**（多轮形状，单元测试每句独立，永远碰不到）
- 若返回 granted → 假设作废，记下来即可

**同族待验**：否定跨轮。用户第1轮「改一下 a.py」→ granted，第 2 轮「不过 b.py 别动」——
单条否定只挖掉 b.py，**对 a.py 的授权没有任何记账机制**。

---

## 2. 两条随机红的测试（详见已知缺陷文档）

### 2.1 `test_web_fetch.py::test_ddg_success_can_merge_parallel_backends` —— 根因已定位，修法未验证

三个独立随机源：

1. **`_baidu_search` 未被 patch** —— 实测三个后端都被调用，baidu 走真实网络。
   baidu 通 → 聚合结果多一个来源 → 红；不通 → 只剩 ddg → 绿。
   **此前「4 轮全绿」是 baidu 那 4 次都不通，不是稳定。**
2. **缓存层** —— 重复调用命中缓存、不再调后端（实测：打了 patch 后调用数为 0）。
3. `WEB_SEARCH_FANOUT` 决定的扇出规模。

**已否证的方案**：`WEB_SEARCH_FANOUT=1` 会追加 `（缓存结果）` 后缀，直接破坏断言。

**下一步**：先读 `_builtin_search`（`tools.py:1844`）的**缓存部分**，
再决定是「显式控制候选表」还是「断言包含关系而非逐字相等」。
**改完必须确认验证不被缓存 confound** —— 这是本条最容易再次误判的地方。

### 2.2 `test_app_test.py::test_the_deep_link_tier_visits_every_route_in_a_real_browser` —— 未定性

- **路由本身是对的**：6 条路径的 canonical / 两条 alias / 404 行为全部正确。
- 唯一不成立：`ERR_CONNECTION_RESET` 一条，导致 `deeplink_console_clean` +
  `deeplink_no_failed_requests` 失败。
- **只有一个样本**。`ERR_CONNECTION_RESET` 是瞬时特征，也可能指向某个资源的稳定问题。
- **取证缺口**：失败消息没说是哪个 URL。重跑时需在失败路径加诊断。

**下一步**：重跑取样本 + 加诊断指明是哪个资源。

> 顺带：跑这条测试会真起浏览器访问页面，可能**改写 `frontend/src/` 下的文件**。
> 跑之前建议先 `git status` 记下基线，跑完对比 —— 测试污染工作树本身可能就是缺陷。

---

## 3. 调研结论（三个 harness，尚**未**转化为改动）

| harness | 关键事实 |
|---|---|
| `earendil-works/pi`（TS/MIT/113k★） | **刻意不做权限系统**，推给容器（Gondolin micro-VM / Docker / OpenShell）。7 包分层：`chord`/`telemetry`/`ai`/`durable`/`agent`/`coding-agent`/`tui`。`telemetry` 带 **conformance tests**。跳过 sub-agents 与 plan mode。供应链：`save-exact` + **`min-release-age=2`** + lockfile 唯一真相 + 生命周期脚本白名单 |
| `NousResearch/hermes-agent`（Py/MIT） | 内置学习闭环：FTS5 会话检索 + LLM 摘要、周期性 nudge、自主创建并自我改进 skill。**「写 Python 脚本经 RPC 调工具，把多步流水线压成 zero-context-cost turn」**。7 种终端后端。interrupt-and-redirect |
| `anomalyco/opencode` | 29 包。`codemode`：模型写 JS 程序编排调用，**只调宿主供给的工具，拿不到 ambient 权限**（acorn 静态解析 + limits）。**但 README 明写该包尚未发布**。`http-recorder`：录真实 HTTP/WebSocket 再确定性重放 |

**由此得到的方向**（尚未动工）：

- `codemode` 的价值是**「越权在语法上不可表达」**，不是省上下文。已确认
  `ToolSpec.capability` **111 个工具全部已声明**（`READ_LOCAL`50 / `WRITE_LOCAL`21 /
  `ADMIN`19 / `NETWORK`13 / `EXEC`4 / `WRITE_EXTERNAL`3 / `READ_EXTERNAL`1），
  前提已具备。护栏的权威依据可以从「猜用户意图」换成「查工具 capability + 本轮授权事实」。
- **「本轮授权事实」我们没有** —— 这正是第 1 节多轮失效的根因。
- **省上下文那一半建议放弃**：Python 栈要用 `exec` + 受限 builtins，比 acorn 静态解析明显更不安全。
- `http-recorder` 思路（别自己编 fixture，录真实交互）能治「测试在说谎」这一类病。

---

## 4. 已撤回、不要重查的结论

**「6 处 capability 与分类表不一致」全部是误报，零缺陷：**

- `dev_desktop_action` / `dev_preview_adapter_rollback` / `dev_preview_adapter_test`
  同时在 `_ADMIN_TOOLS`、`_WRITE_TOOLS`、`_EXEC_TOOLS` 是**多类别成员** ——
  `capability`（属于哪类）与 `side_effect`（会不会改东西）是**不同维度**，不是两份真相。
- `web_weather` 不在 `_NETWORK_TOOLS` 是**设计如此**：消费点
  `agent_runtime/tools.py:259` 为 `if name in _NETWORK_TOOLS or name.startswith("web_")`，
  表外的 `web_*` 由前缀兜住。

---

## 5. 方法论教训（同一晚栽了三次，别重犯）

| 探针 | 报出的假结论 | 真因 |
|---|---|---|
| `isinstance(spec, dict)` 查 capability | 「111 个工具全无声明」 | 值是 `ToolSpec` 不是 dict，对每个都失败 |
| `startswith("slowest durations")` | 「没解析出 durations」 | 表头前面一串 `=`；数据一直都在，白花 5 分钟重跑 |
| 容量相等就外推 | 「4 处 capability 漂移」 | 多类别成员 + 前缀兜底 |

**三条规则**：

1. **先怀疑探针，再怀疑被测物。**
2. **单个读数不足以定性** —— 同一段代码一行未改，A 轮全绿、B 轮 3/3 红。
3. **别拿「数量相等」当「逐项相等」** —— 同一天用同一个错法栽了两次。

---

## 6. 真业务题端到端测试（已规划，**未执行**）

目的：验「判据在真实主循环里有没有真的改变模型行为」。单元绿 ≠ 端到端绿。

**方法论前提（不做则结果不可解释）**：

- **必须有对照组**（护栏关掉，同任务跑一遍）。否则「agent 没改文件」无法区分是
  **护栏生效**还是**模型自己没想改**。
- **必须埋点**：每轮记 `user question → _write_intent 返回值 → 护栏是否触发 → 实际调了什么工具`。
- 任务要小到边界清晰、但大到护栏真会被触发。

**建议题型**：给某个文件加函数并跑测试，中途故意插入三类话——
「这个改起来要几步」（应回答不动文件）/「改吧」（应动手写，验第 1 节）/
「b.py 别动」（应只改 a.py）。

**六笔里只有四笔能被真场景验到** —— `773deaf` 与 `ab45a56` 只改测试，与模型行为无关。

---

## 7. 建议的优先级

1. 第 1 节（多轮批准）—— 一条只读命令就能验，可能直接推翻六笔的适用性判断
2. 第 2.2 节（段 1 取样）—— 单样本未定性，且需加诊断
3. 第 2.1 节（`test_web_fetch`）—— 需先读缓存部分才能动手
4. 第 6 节（真业务题）—— 成本最高，等上面收尾
5. 第 3 节（护栏改读 capability）—— 是**方向性变更**，且与第 1 节是同一个根因，
   建议**合并考虑**，别分两笔