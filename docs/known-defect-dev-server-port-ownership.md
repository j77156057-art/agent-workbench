# 5173 上「别人的服务」没有归属校验：注释声称有，实际不存在

> 状态：**未修**。发现于 2026-10-08 排查 DocMind 前端 dev server 的过程中。
> 归属：跨层（`agent_runtime/dev_server.py` + `visual_acceptance.py` + 前端 vite 配置），
> 需后端侧定夺，前端单方面改端口只会把契约撕开（见第 5 节）。

## 1. 一句话

`agent_runtime/dev_server.py:36` 的注释写着：

> 更糟的是那个地址上可能正站着**别人的服务**（见 `owns_port`）。

但 `owns_port` **全仓只出现在这一行注释里** —— 从未定义、从未被调用。
`dev_server.py` 的公开函数只有 `free_port` / `loopback_url` / `parse_port_hint` /
`port_taken` / `probe` / `start` / `stop` / `render`。其中 `port_taken(port)` 只回答
「这个端口有没有人在听」，不回答「在听的那个是不是我起的」。

注释把风险点名了，又用一个不存在的函数让读者以为它已被处理。

## 2. 本机实际存在的场景（不是假设）

```
TCP  [::1]:5173      LISTENING  24352   node  ← D:\study\tools\agent\new agent
                                                 \.worktrees\qoder\frontend 的 vite
                                                 （启动于 2026-10-08 00:08:36）
TCP  127.0.0.1:5173  LISTENING  (DocMind 自己的 vite)
```

两个 vite **同号、不同地址族**，因此互不冲突、同时存活。而
`netstat -ano -P TCPv4` 看不到 IPv6 那条监听 —— 排查者会误判成「那个进程没了」。
本次排查中我就误判过一次，是查 `CommandLine` 才发现那根本是另一个项目的服务。

## 3. 后果链

1. `agent.py:521` 的 `preview_project` 文档，叫模型传字面量
   `url: http://127.0.0.1:5173/`（并注明「只允许回环地址」）。
2. `visual_acceptance.py:788` 用 `loopback_url(url)` 校验 —— 但它只判「是不是回环」，
   不判「是不是本会话起的那个服务」。
3. `visual_acceptance.py:801`：给了 `url` 就**不再起自己的静态服务**，直接截那个地址。
4. DocMind 的 vite 一旦因撞端口漂到 5174（`dev_server.py:34` 自己描述的行为），
   而 5173 上站着别的项目的服务 —— **验收截图拍到的是别人的画面，并且报告成功。**

这是「把不知道的说成知道了」那一族里最糟的形状：证据看起来完全有效，
画面是真的、浏览器是真的、没有报错，只是**不是本项目**。

## 4. 已顺带修掉的另一半

`869773c`：vite 之前用默认配置只绑 `[::1]`，而上面第 3 节的契约字面量是
`127.0.0.1` —— 那条路不是偶发失败，是**必断**。改绑 IPv4 loopback 后，
`http://127.0.0.1:5173/` 与 `localhost` 都能通，且不再把 dev server 暴露到局域网
（`host: true` 会绑 `0.0.0.0`，实测 `http://10.215.20.161:5173` 可访问）。

旁证：`dev_server.py:30` 的 `_PORT_LINE` 与 `tests/test_dev_server.py:61` 用的字面量
正是 `http://127.0.0.1:5173/` —— 改完之后真实输出才符合测试早就在假设的形态。

**归属校验仍然缺失**，本篇记的就是这一半。

## 5. 为什么不能单方面改端口

5173 不是前端自己的常量，是被跨层字面引用的契约：

| 位置 | 引用形态 |
|---|---|
| `agent.py:521` | 工具文档里的 `url: http://127.0.0.1:5173/` |
| `tools.py:7141 / 7273 / 7625 / 8393 / 8408 / 8558` | 工具参数说明与示例 |
| `agent_runtime/dev_server.py:34 / 73 / 85` | 漂移行为描述与报错文案 |
| `tests/test_dev_server.py:61-73` | 断言漂移时认到 5174 |
| `tests/mutation/battery_dev_server.py:65,120` | 把 fallback `5173` 钉成变异守卫 |
| `docs/HANDOFF-vibecoding.md:852` | 交接文档描述同一行为 |

所以「换端口」要连带改上面全部；而 `strictPort: true` 更糟 —— 它让 vite **退出而不漂移**，
`parse_port_hint()` 就再也认不到活着的地址，等于废掉后端整条「从日志认端口」的路径。

## 6. 建议方向（留给后端定）

最小且不动契约的做法：把 `owns_port` 真的实现出来，并在 `preview_project` 收到
`url` 时要求它匹配**本会话 `dev_serve` 记录的实际端口**，不匹配就拒绝并说明
「5173 上有别的服务，本会话的服务在 5174」。也就是让第 2 节那条 `port_taken()`
升级为能回答归属问题，而不是新增端口约定。

## 7. 复现

```bash
# 1. 看谁在听 5173（必须不带 -P 过滤，否则漏掉 IPv6 条目）
netstat -ano | grep ":5173" | grep -i listen
# 2. 看那个 pid 到底是哪个项目
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>' | Select -Expand CommandLine"
```
