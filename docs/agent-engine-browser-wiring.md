# agent 控制面：引擎控制 + 内置浏览器

> 2026-10-21。承接 [task-wiring-p3.md](task-wiring-p3.md)。
> 本文记录两条"把后端已有能力接给**模型**（而不是给 UI）"的线：
> **引擎控制**（模型能自己起引擎）和**内置浏览器**（模型能真的上网并交互）。

---

## 1. 起点：能力都在，模型却碰不到

审计"后端有、前端没有"时，漏掉了一层——**后端有、模型也没有**：

| 能力 | HTTP 端点 | agent 工具（改前） |
| --- | --- | --- |
| 启动/停止/热重载引擎 | `POST /api/engine/start\|stop\|reload` | **无** |
| 在公网网页上点/填/读 | 无（`dev_page_action` 只许本机回环） | **无** |

后果很具体：

- **引擎**：模型只能等用户手动点 UI 把引擎跑起来，再用 `game_screenshot` 看一眼**已经**
  在跑的引擎。于是「改完代码 → 画面到底变了没有」这条闭环对模型是断的：模型自己不能起，
  也就没法自己判断。
- **浏览器**：`web_fetch` 只给纯文本，页面上那些**要交互才拿得到**的内容（展开折叠区、
  翻到第二页标签、填搜索框后看结果、展开一张图表）全都拿不到。模型于是开始"猜"页面上
  写了什么——那是这类任务里最贵的一种错。

## 2. 引擎控制（`engine_start` / `engine_stop` / `engine_reload`）

### 2.1 走同一条路径，不重写

`agent_runtime/engine_tools.py` **只调底层**：

```python
import game_workbench          # 刻意用模块 import，调用时取属性 —— monkeypatch 才打得到
game_workbench.engine_start(root, scene=..., host_hwnd=..., embed=...)
```

为什么不能另起一套：启动引擎远不止一行 `subprocess.Popen` —— 要拿 GPU 软租约（把卡号注入
`CUDA_VISIBLE_DEVICES`）、把 stdout/stderr 接到项目的 `engine.log`、登记进程与看门狗、
失败时释放租约、启动后按需把窗口嵌进桌面宿主。这些护栏**只在 `game_workbench` 那一份实现里**。
另起一套等于凭空多出第二套 GPU 租约、第二份引擎日志、第二份进程表：用户点的引擎和模型起的
引擎互相看不见（`engine_status` 认不出来），租约也会泄漏。

### 2.2 `host_hwnd` 的语义

`host_hwnd` 是桌面宿主窗口句柄（Win32 HWND），由调用方从 `desktop_bridge.host_hwnd()` 取。
**0 = 不做嵌入**：底层的嵌入分支会满世界找宿主窗口，没有宿主时既白等一轮轮询，
又容易把"没嵌上"说成"启动失败"。

此时返回里**如实**带 `embedded=False` 与 `embed_error`，让模型能分清三件事：
「引擎根本没起来」「引擎起来了但没嵌进窗口」「引擎起来了且已嵌入」。

### 2.3 分类

显式声明 `capability=exec` / `side_effect=mutating` / `parallel_safe=False`。
不声明的话它会落进默认档（`read_local` / `pure` / `parallel_safe=True`）——于是只读角色也能拿到
它，还被允许和别的工具并发。`tests/test_tool_taxonomy.py` 的
`test_every_declared_capability_is_in_some_table` 会把这类漏登记抓出来（本次实测就抓到了）。

## 3. 内置浏览器（`browser_action`）

### 3.1 与 `dev_page_action` 的分工：边界互补且不重叠

| | `dev_page_action` | `browser_action`（本轮新增） |
| --- | --- | --- |
| 地址 | 只许 `127.0.0.1` / `localhost` / `::1` | 只许**公网** |
| 内网地址 | 允许（它就是干这个的） | **拒绝** |
| 会话域 | `loopback` | `public` |
| 用途 | 点自己的开发服务、调前端 bug | 看文档、查资料、读公开 API |

两个域的会话**互不相通**（`page_action._get(page, domain)` 会校验）：
公网工具操作不了回环会话（否则"只许本机"就只是个建议），回环工具也操作不了公网会话
（它会把外站当成本机服务去点）。

动作原语（click / type / press / wait / read / locate / list / close）、证据判定
（`changed` / `navigated` / `reloaded` / `typed` / `pixels` / `error_seen` / `no_change`）、
浏览器启动通道与回收机制**全部共用**——只有**地址域**换了。

### 3.2 地址策略（`agent_runtime/url_policy.py`）

四道闸 + 一道复核：

1. **协议**：只允许 `http` / `https`。
2. **主机名**：拒掉 `.local` / `.internal` / `.lan` / `.home.arpa` 等内网后缀与 `localhost`。
3. **IP 字面量**：必须是公网地址（私有段 / 回环 / 链路本地 / CGNAT / 保留段 / 组播一律拒）。
4. **DNS 解析结果**：域名解析到内网就拒（挡 `evil.test → 127.0.0.1` 这类）。
5. **导航后复核**：页面可能把自己重定向到内网。每次动作后重新判断当前地址，
   越界就把 `ok` 判 false 并要求 `close`。

为什么不做"允许所有 https 就够了"：本工具是**模型可调用**的，页面内容是敌意输入
（页面提示注入是真实攻击面）。护栏必须假设页面在试图把模型引到内网。

### 3.3 凭据护栏

往 `type=password` 的输入框写字**必须显式** `allow_password: true`。理由同上：
外部站点不可控，而"把密码填进去"的指令完全可能来自页面内容。

### 3.4 分类

```python
"capability": "network", "side_effect": "mutating", "parallel_safe": False, "group": "web"
```

`group="web"` 不是装饰：**联网开关判的是 `spec.group == "web"`，不是 `capability == NETWORK`**
（见 `agent.py` 的 `_web_blocked`，2026-10-15 专门删过 NETWORK 子句）。不声明 `group`，
这个工具就无视用户的联网开关。

## 4. 途中修掉的两个真问题

### 4.1 `javascript:` / `data:` 绕过了协议检查

地址校验原本是"没有 `://` 就补 `http://` 前缀"。于是 `javascript:alert(1)` 被补成
`http://javascript:alert(1)` —— scheme 变成 `http`，**协议检查被整个绕过**，
连 `parsed.port` 都会炸。

改成：**先看冒号前面那段是不是合法 scheme**（`[A-Za-z][A-Za-z0-9+.\-]*`）——
是就按 scheme 判，不是就当成 `host[:port]/path`。

### 4.2 慢站点上 `open` 直接失败

实测 `Page.navigate` 的 CDP 回包在慢站点上会**迟迟不来**（`Page.navigate` 会等导航拿到初步响应）。
原先 socket 接收超时与就绪预算都是 25 秒，两者赛跑，socket 先断 → 整次 `open` 失败，
而模型只看到一句"连接超时"，既不知道是哪个站、也不知道该重试还是该放弃。

三处一起改：

1. **公网用宽松的就绪判据**（`PUBLIC_READY_JS`）：`readyState !== 'loading'` 就算就绪。
   本机预览要等 `complete`（要等 bundle + `/api/config`），公网站点没有这个语义——
   长轮询、流式响应会让 `readyState` 永远停在 `interactive`。
2. **就绪探针超时不致命**：页面可能还在加载，但它已经能读能点了，那就继续用；
   超时如实报告"就绪状态未知"。
3. **导航回包超时能救回来**：先轮询一次当前地址，救得回来就继续用；救不回来才报错，
   并且错误文案点名站点与秒数：「打开 X 超时：站点在 45 秒内没有响应（网络慢、不可达或被拦）。
   换个站点或稍后重试。」

## 5. 实机验证

### 5.1 引擎控制（真实 Godot 4.7.2，项目 `godot_sample`）

| 步骤 | 结果 |
| --- | --- |
| `engine_start` | `ok=true, running=true, pid=33452, gpu=0, embedded=false` ✅ |
| `engine_status` | `running=true, pid=33452` ✅ |
| `engine_reload` | `ok=true, reloaded=true, reload_mode=process_restart, previous_pid=33452` ✅ |
| `engine_stop` | `ok=true, stopped=true, killed=[28152]` ✅ |

`embedded=false` 是因为 agent 侧没有桌面宿主窗口——如实报告，不是启动失败。

### 5.2 内置浏览器（真实公网）

| 步骤 | 结果 |
| --- | --- |
| `open https://www.iana.org/domains/example` | 标题 `Example Domains`；**如实报告发生了重定向**（落到 `/help/example-domains`）✅ |
| `read mode=contains needle='Domains'` | `hits=3, scanned_all=true` ✅ |
| `locate selector='a'` | `count=31, ambiguous=true` ✅（歧义如实报） |
| `click selector='a' nth=3` | `effect=navigated`，真跳到 `https://www.iana.org/numbers`，作用元素 `<a> 'Numbers'` ✅ |
| 导航后 `read` | 拿到新页面文本 ✅ |
| `open https://cn.bing.com/` | `redirected=true`（跳到 cn.bing.com）✅ |
| `locate #sb_form_q` | `count=1` ✅ |
| `type 'asyncio'` | `effect=typed, value_landed=true, focused=true` ✅ |
| `press Enter` | **真提交搜索**：URL 变成 `cn.bing.com/search?q=asyncio…`，标题变成 `asyncio - 搜索` ✅ |
| `close` | `job_closed=true, profile_released=true` ✅ |

### 5.3 护栏（不该放行的，一个都没放）

| 地址 | 结果 |
| --- | --- |
| `http://127.0.0.1:8000/` | 拒：「回环地址属于本机……要看本机跑着的东西，请用 `dev_page_action`」 ✅ |
| `http://192.168.1.1/` | 拒：「私有地址段（内网）」 ✅ |
| `http://evil.local/` | 拒：「`.local` 是内网域名后缀」 ✅ |
| `file:///C:/Windows/win.ini` | 拒：「只支持 http/https」 ✅ |

### 5.4 一个环境事实（不是代码问题）

这台机器的线路到 **python.org / docs.python.org / duckduckgo 不通**（实测各要 45 秒超时），
而 iana.org 3.0s、百度 2.6s、必应 3.2s、MDN 1.9s 都通。内置浏览器在**可达站点**上工作正常；
对不可达站点给出的是明确可执行的错误，而不是笼统的"连接超时"。

⚠️ 顺带一提：验证过程中一度以为是工具坏了——`locate` 在 example.com 上命中 0。
实际是 **example.com 现在的 HTML 里一个 `<a>` 都没有**（只有一段 `<p>` 加一个注入脚本），
所以"命中 0"是页面事实。**探针选错页面比工具出错更常见。**

## 6. 相关文件

| 文件 | 角色 |
| --- | --- |
| `agent_runtime/url_policy.py` | **新**：公网地址策略（协议 / 主机名 / IP 段 / DNS / 复核） |
| `agent_runtime/browser_tool.py` | **新**：内置浏览器的动作分发与会话域固定 |
| `agent_runtime/engine_tools.py` | **新**：引擎控制的 agent 出口（只调底层，不重写） |
| `agent_runtime/page_action.py` | 改：会话加 `domain`（回环/公网隔离）、`open_external_page`、公网宽松就绪判据、导航超时可救回 |
| `agent_runtime/tools.py` | 改：`browser_action` 进 `_NETWORK_TOOLS`、`engine_*` 进 `_EXEC_TOOLS`（名字表必须同步，否则按 name 求并集的护栏漏放） |
| `agent_runtime/tool_domains.py` | 改：`web/网页交互` 与 `game/引擎控制` 两个 facet |
| `tools.py` | 改：4 个新 agent 工具的包装层与注册（含 capability/side_effect/group 显式声明） |
| `tests/test_browser_tool.py` | **新**：地址策略、访问域隔离、密码框护栏（29 项） |
| `tests/test_engine_tools.py` | **新**：引擎控制的行为与失败语义（15 项） |

## 7. 还没做的

- `POST /api/desktop/resize`（桌面宿主自调）：需要先确认宿主是谁在调，否则是自调自的循环。
- Unreal 写回端点：后端恒失败，属死路，不接。
- 内置浏览器的**下载**与**文件上传**：目前动作原语里没有，`browser_action` 也还没有对应动作。
