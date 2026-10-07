# 已知缺陷：两条会随机红的测试（与写意图那六笔无关）

## 结论（先说这个）

`e0bdb53`→`ab45a56` 这六笔提交后的两次全量回归里，**出现的红没有一条由它们引起**——
`test_app_test.py` 与 `test_web_fetch.py` 我一次都没碰过，前端路由与搜索代码也没碰。

分段结果（HEAD = `ab45a56`，288 个文件分 6 段，每段 48 个）：

| 段 | 结果 | 与改动前对比 |
|---|---|---|
| 1 | 767 passed + **1 failed** / 1 skipped / 182 subtests | 上次 768 —— 差的正是那条红 |
| 2 | 713 passed / 1 skipped / 44 subtests | 逐项一致 |
| 3 | 762 passed / 3 skipped / 589 subtests | 逐项一致 |
| 4 | 788 passed / 4 skipped / 44 subtests | 逐项一致 |
| 5 | 617 passed / 1 skipped / 96 subtests | 逐项一致 |
| 6 | 798 passed + **1 failed** / 80 subtests | 793 + 新增守卫 6 条 |

合计 **4445 passed / 2 failed / 10 skipped / 1035 subtests**。
与改动前的 4441 相比正好 **+6**，等于本次新增的守卫数（pid_wait 4 + 刻度守卫 2）——
**没有用例丢失**。`subtests` 仍是 1035，说明这六笔没动任何带 subtest 的既有契约。

---

## 缺陷一：`test_web_fetch.py` 断言逐字相等，但有三个随机源

```
WebSearchFailoverTests::test_ddg_success_can_merge_parallel_backends
AssertionError: '聚合搜索结果（8 条候选；来源：ddg、baidu；...）' != '· DDG 标题\n  摘要\n  https://ddg.test/x'
```

### 已定位的根因

`tools.py:1844` `_builtin_search`：

```python
backends = {"ddg": _ddg_search, "bing": _bing_search, "baidu": _baidu_search}
order = [provider] if provider in backends else ["ddg", "baidu", "bing"]
...
selected = order[:fanout]
```

测试 patch 了 `_ddg_search` 与 `_bing_search`，**`_baidu_search` 未被 patch**。
实测确认三个后端**都被调用**，baidu 走真实网络：

- baidu 返回可用结果 → `meaningful` 有 2 项 → 走 `_merge_builtin_results` → 结果多一个来源 → 红
- baidu 不可达 → `meaningful` 只剩 ddg → 直接返回 ddg 原文 → 绿

**此前「4 轮全绿」不是稳定，是 baidu 那 4 次都不通。**

### 还没定的部分

- **`WEB_SEARCH_FANOUT=1` 的方案已被实测否证**：会追加 `（缓存结果）` 后缀，直接破坏断言。
- **存在缓存层**，重复调用会命中缓存、不再调用任何后端。这让「补 baidu 的 patch」这一最小修法
  **无法在当前条件下干净验证**（实测中打了 patch 的三次里后端调用数为 0）。
- 因此**修法未定，代码未改**。

### 为什么这条必然随机红

一个**逐字相等**断言整段聚合结果的测试，同时暴露在三个互相独立的随机源下：
① `_baidu_search` 真实网络的成败 ② 缓存 ③ `WEB_SEARCH_FANOUT` 决定的扇出规模。

代价：它让「全量绿」这个信号**失去可信度** —— 这三天已经因此污染了两次全量判读。

---

## 缺陷二：`test_app_test.py` 深链接，**未定性**

```
LivePageTests::test_the_deep_link_tier_visits_every_route_in_a_real_browser
"failed": ["deeplink_console_clean", "deeplink_no_failed_requests"]
"deep_console_errors": [{"level":"error","text":"network: Failed to load resource: net::ERR_CONNECTION_RESET"}]
```

### 已知：路由本身是对的

同一次运行里，6 条路径的判定**全部正确**——`/`、`/workbench`、`/trace` 三条 canonical 的
`own_seen` 均为真且 `landed` 正确；`/workbench.html`、`/trace.html` 两条别名的
`canonical` 收敛正确；`/no-such-page-for-deeplink` 的 `unknown_served_a_page` 为 `false`（404 行为正确）。

**唯一不成立的是「console 干净」与「无失败请求」，原因只有一条 `ERR_CONNECTION_RESET`。**

### 未知

- 只有一个样本。`ERR_CONNECTION_RESET` 是瞬时特征，但它也可能指向某个具体资源的稳定问题。
- 失败消息**没说是哪个 URL**——这是取证缺口，重跑时需加诊断才能定位。
- 会不会与 harness 服务重启有关（历史上出现过）——未查。

---

## 方法论教训（同一晚连续三次，值得钉住）

### 1. 单个读数不足以定性

同一段代码在**一行未改**的情况下，A 轮全绿、B 轮 3/3 红。所以「跑绿了」只能说明
**那一次没触发**，不能说明它稳定。

### 2. 先怀疑探针，再怀疑被测物

同一晚三次探针自身有 bug，导致我三次差点得出错误结论：

| 探针 | 症状 | 真因 |
|---|---|---|
| `isinstance(spec, dict)` 查 capability | 报「111 个工具全无声明」 | `TOOLS` 的值是 `ToolSpec` 不是 dict，判断对每个都失败 |
| `startswith("slowest durations")` | 报「没解析出 durations」 | 真实表头前面是一串 `=`；日志里数据一直都在，白花 5 分钟重跑 |
| 容量对不上就外推 | 报「4 处 capability 漂移」 | 其中 3 处是多类别成员，1 处是 `web_` 前缀兜底，**零缺陷** |

### 3. 别拿「数量相等」当「逐项相等」

`_ADMIN_TOOLS` 19 == `Capability.ADMIN` 19，我据此外推「四张表都对」——**错**。
同一天我用同一个错法栽了两次。

---

## 那 6 处 capability/分类表「不一致」——全部撤回

一度报出 4 处，逐项复查后**全部为误报**：

- `dev_desktop_action` / `dev_preview_adapter_rollback` / `dev_preview_adapter_test`
  同时出现在 `_ADMIN_TOOLS`、`_WRITE_TOOLS`、`_EXEC_TOOLS`，是因为这些表表达**不同维度**：
  `capability` 是「属于哪一类」，`side_effect` 是「会不会改东西」（这三个是 `MUTATING`）。
  一个工具既是 ADMIN 又是 MUTATING 完全合法，不是两份真相。
- `web_weather` 未收录于 `_NETWORK_TOOLS`：**设计如此**。
  消费点 `agent_runtime/tools.py:259` 是 `if name in _NETWORK_TOOLS or name.startswith("web_")`
  —— 表外的 `web_*` 由前缀兜住，表里 12 项恰是不叫 `web_*` 的网络工具。

**结论：`ToolSpec.capability` 与四张分类表之间没有漂移，无需改动。**

---

## 待办

1. 缺陷一的修法：需先读 `_builtin_search` 的缓存部分，再决定是「显式控制候选表」
   还是「断言包含关系而非逐字相等」。**改完必须验证不会被缓存 confound。**
2. 缺陷二的重跑取样：需在失败路径上加诊断，指明是哪个资源 `ERR_CONNECTION_RESET`。
3. 两条都应做到**不随机红**再谈其他——一个会随机红的测试会持续污染所有全量结论。