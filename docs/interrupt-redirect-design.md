# ④ interrupt-and-redirect 设计方案

> 来源：`docs/next-steps.md` §7 第 4 项 + `docs/harness-research-verified.md` §2.10。
> 状态：设计稿（待授权落地，对应 task #30）。
> 调研事实锚点：`api.py` / `agent.py` / `frontend/src/workbench/components/ChatDock.vue` / `frontend/src/ask/AskApp.vue`（均为写作时真实行号）。

---

## 1. 目标与范围（本轮交付什么）

Hermes 有 **三种** busy-input 模式（`§2.10` 已核实），我们三种都没有。本轮只交付**最有价值的 `interrupt`（默认）模式的一个安全 MVP**：

> 用户在 agent 干活时输入「等下，改成 X」→ **不必先点停止、不必丢掉当前轮** → 当前轮被中断、新指令以「重定向」身份进入、已完成的工具工作与已展示推理保留在对话历史中。

**本轮明确不做**（留作后续，已在 §6 记录）：
- `queue` 模式（静默排队，下一轮送达）
- `steer` 模式（注入当前 run、下一个工具调用后到达，不新开轮）
- 长时前台命令「移到后台 + 完成通知」（Hermes 的 backgrounding）。本轮用「硬串行等安全边界」替代（见 §3.2）。

---

## 2. 现状核实（与 Hermes 三模式都不对应）

| Hermes | 我们现状 | 证据 |
|---|---|---|
| `interrupt`：消息重定向当前轮，已完成推理/工作保留 | **无**。运行时 `canSend = !sending.value`（`AskApp.vue:155`）+ ChatDock `send()` 的 `\|\| sending.value`（`ChatDock.vue:521`）→ 发送被硬拦，**运行时根本无法输入** | 源码 |
| `queue`：静默排队，下轮送达 | **无** pending-user 队列 | 全仓无 |
| `steer`：注入当前 run | **无** | `agent.py` 无注入点 |
| `Ctrl+C`：中断当前操作 | ✅ 有，但语义是「停止=整轮作废」：`stop()`→`abortCtl?.abort()`（`ChatDock.vue:651`）→ SSE 断连 → `worker_stop.set()`（`api.py:2567` finally）→ `GeneratorExit`（`agent.py:4681`）→ 走 `aborted` 记账、**不续跑** | `api.py:2482/2503/2567`、`agent.py:4681` |

**关键风险（生产代码改动的核心难点）**：`selected_agent = _agent_for(session_id)` 是**按会话缓存的同一 Agent 实例**（`api.py:2317/556-568`），`_SESSION_AGENTS[key]` 全局复用；每个 `/api/chat` 请求在 `event_stream` 里**局部新建** `worker_stop`（`api.py:2482`）并起独立线程跑 `selected_agent.run(...)`（`api.py:2503`）。**全仓没有任何 per-session run 串行化锁**。若运行时允许第二条 SSE 进来并发跑 `run()`，两条线程会同时写 `self.history`（`agent.py:4402/4443/4490/5602` 等）并并发 `_sessions.save` → **会话文件交错损坏**。这是 ④ 必须首先解决的硬约束。

---

## 3. 设计方案

### 3.1 后端：per-session run 串行化 + 重定向注入（核心）

新增模块级注册表与锁（`api.py`，邻近 `_SESSION_AGENTS`）：

```python
# 每个会话同时只允许一个 run() 在跑；第二条请求先中断旧 run 再串行进入。
_ACTIVE_RUN: dict[str, dict] = {}        # key -> {"stop": Event, "thread": Thread}
_RUN_LOCKS: dict[str, threading.Lock] = {}

def _run_lock_for(key: str) -> threading.Lock:
    return _RUN_LOCKS.setdefault(key, threading.Lock())
```

`event_stream()` 改造（`api.py:2477` 起）：

1. **判定重定向**：在 `produce()` 启动前，读 `_ACTIVE_RUN.get(key)`。若存在且线程 alive，说明本次请求是「运行时新消息」→ 在 `hints` 里追加重定向提示（见 §3.5），并把 `redirected=True` 传入 `produce()`。
2. **`produce()` 内串行化**（`api.py:2497` 起）：

```python
def produce():
    nonlocal gen, worker
    key = _agent_key(session_id, _ctx_project_id() or "")
    lock = _run_lock_for(key)
    # 重定向：中断同一会话正在跑的旧 run，并等它到安全边界退出（防 self.history 并发）
    prev = _ACTIVE_RUN.get(key)
    if prev is not None and prev.get("thread") is not None and prev["thread"].is_alive():
        prev["stop"].set()
        prev["thread"].join()          # 硬等，无超时：保证旧 run 完全退出后才新开
    with lock:
        _ACTIVE_RUN[key] = {"stop": worker_stop, "thread": worker}
        try:
            gen = selected_agent.run(..., cancel_event=worker_stop)
            for ev in gen:
                if worker_stop.is_set():
                    break
                events.put(("event", ev))
        finally:
            cur = _ACTIVE_RUN.get(key)
            if cur is not None and cur.get("thread") is worker:
                _ACTIVE_RUN.pop(key, None)
```

`event_stream` 在创建 `worker_stop`/`worker` 后、启动线程前，先把本请求的 `{"stop": worker_stop, "thread": worker}` 注册（保证 `prev` 抓到的是**上一条**而非自己）；`produce` 内部再覆盖为本条并 join 旧条。

> 注册时机细节：`worker` 变量在 `event_stream` 中 `worker = threading.Thread(...)` 处创建（`api.py:2524`）；把它与 `worker_stop` 一起写 `_ACTIVE_RUN[key]` 必须发生在 `worker.start()` 之前，否则并发请求可能读不到。写完再 `worker.start()`。

### 3.2 在途工具怎么办（safe boundary）

Hermes 原文：*"A running tool reaches its safe boundary before the correction is applied"*（中断不是立即杀在途工具，而是等它到安全边界）。

本轮采用**硬串行**实现该语义：
- `cancel_event` 只在 `run()` 主循环顶端 `_raise_if_cancelled()`（`agent.py:4678-4682`）被检查。单个工具调用（如 `execute_command` 跑一个长 subprocess）**不会被中途打断**，只在它返回后的下一个循环顶端抛 `GeneratorExit`。
- 因此 `prev["thread"].join()` 会**自然等到旧 run 当前在途工具返回、再到循环顶端 abort**——正好是 Hermes 的「safe boundary」。
- **不引入 join 超时软降级**：一旦允许「旧 run 还没退就开新 run」，就回到 self.history 并发损坏。所以 join 必须硬等（延迟上界 = 一个在途工具调用的剩余耗时，通常远小于一轮总时长）。
- 不做 backgrounding（长命令移后台 + 完成通知）——超本轮范围，见 §6。

### 3.3 SSE 并发归属（前端 epoch 守卫）

已有机制可复用，无需新写：
- `chatEpoch`（`ChatDock.vue:533`）每次 `send()` `++chatEpoch`；旧 SSE 的 `onEvent`（`ChatDock.vue:576-580`）与 `live()` 都用 `epoch !== chatEpoch` 提前返回 → **旧流失配自动停写 UI**。
- 重定向时：新 `send()` 先 `++chatEpoch`（旧流失效），再 abort 旧 `abortCtl`（加速旧 SSE 断连），然后起新请求。旧 run 被 `produce()` 内 `prev["stop"].set()` + join 真正中断；旧 SSE 走 `api.py:2567` finally 正常收尾。两条流不会同时写同一个 assistant turn。

### 3.4 前端 ChatDock `canSend` 放宽（④ 目标组件）

改动点 `ChatDock.vue:521`：把 `sending.value` 的硬拦改为「运行时新消息 = 重定向」。

```ts
const redirecting = sending.value
if ((!q && !imgs.length)) return false
if (redirecting) {
  // 打断当前轮并重定向：旧流随 chatEpoch 失配停写；后端 produce() 会中断并 join 旧 run
  ++chatEpoch
  abortCtl?.abort()
  abortCtl = null
  sending.value = false
}
// 后续正常 push user/assistant turn、起新 askGrounded 请求（触发后端 redirect+join+new run）
```

- 保留 `gateBlocking` 拦截（审批门期间仍禁止，`:523`）——重定向不应绕过工作流审批互斥。
- `stop()`（`:637`）保持现状（显式停止 = 整轮作废，与 redirect 区分）。
- AskApp.vue 已具备 interrupt+resume 范式（`:155/:187/:737`），本轮**同步对齐**：其 `send()` 同样放开运行时输入走 redirect 路径，保证两入口行为一致（AskApp 的 `canSend` 用的是 `!sending.value`，需同步放宽 `send()` 内的 `if (sending.value) return` 类守卫）。

### 3.5 重定向提示词注入（保留「已完成工作」语义）

当 `redirected=True`，在 `request_context`（`api.py:2472` 的 `hints`）追加系统上下文：

```
【用户中途重定向】用户在本轮回答生成途中打断了上一轮并给出新指令。
上一轮已执行的工具结果、已展示的推理与对话历史均保留在上下文中，
请以用户本轮的新指令为准继续，不要重复已完成的工作，也不要把上轮未完成的
内容当作既定结论。
```

这样模型层面的「completed work preserved」语义被显式声明，而非仅靠历史隐式携带。

---

## 4. 验收判据（单变量可复算）

1. **串行化不变量（关键）**：同会话并发发两条 `/api/chat`，断言两条 `produce()` 不会同时处于 `selected_agent.run()` 临界区——通过 `_ACTIVE_RUN` 注册 + `with lock` 保证；用集成测试模拟「先发长任务、200ms 内发重定向」验证旧 run 退出后新 run 才进入（断言新 run 不早于旧 run 的 `aborted` 记账）。
2. **重定向可达**：前端运行中输入新消息 → 旧 assistant turn 停在「已中断/stopped」、新 assistant turn 以新指令作答；新回答不重复旧轮已完成工具工作（检查 history 不出现两份相同 user 提问导致的重复工具链）。
3. **self.history 无损坏**：重定向后 `_sessions.save` 写出的会话 JSON 可被 `_sessions.history()` 正常解析（无半截写入）；连发 5 次「长任务 + 立即重定向」后读取历史无异常。
4. **safe boundary**：旧 run 在途的长工具（如 `execute_command` sleep 3s）不被中途杀，待其返回后再 abort（断言工具退出码正常、无僵尸进程）。
5. **回归**：既有 `tests/test_api_routes.py` 与 chat 相关单测仍通过（断言一次业务结果，防路由/覆盖回归）。

---

## 5. 风险与残留

- **延迟上界**：重定向生效前最多等一个在途工具调用返回（硬串行，无超时）。长命令（>数秒）场景下用户会感知到「输入后短暂等待」。这是安全换取的正确性代价，符合 Hermes safe-boundary 语义。
- **不做 backgrounding**：Hermes 把前台长命令移后台并给完成通知；本轮不做，因需新增「后台任务 → SSE 完成通知」通道，超出 MVP 且非当前痛点（长命令罕见）。
- **不做 queue/steer**：明确排除，避免注入点复杂度（§2.10 指出的「改模型可见输入」风险）。
- **并发请求来自不同标签页**：`_agent_for` 按 `session_id` 缓存；若用户开两个标签页同 session 各发一条，串行化锁同样保护（按 key 串行）。跨 session 互不影响。

---

## 6. 文件级改动清单（落地时）

| 文件 | 改动 |
|---|---|
| `api.py` | 新增 `_ACTIVE_RUN`/`_RUN_LOCKS`/`_run_lock_for`；`event_stream`/`produce` 加重定向判定 + 串行化 + 注册表清理；`hints` 加重定向提示 |
| `frontend/src/workbench/components/ChatDock.vue` | `:521` 放开 `sending.value` 硬拦为 redirect 路径；保持 `gateBlocking` 拦截 |
| `frontend/src/ask/AskApp.vue` | 同步放开运行时输入的 `send()` 守卫（对齐两入口） |
| `tests/` | 新增集成测试：同会话并发/重定向串行化 + 历史无损坏 + safe boundary |
| `docs/next-steps.md` §7 第 4 项 | 标记实现状态（落地的 design 链接） |
