# PARALLEL-LANES · 并行任务书

> **给接手的人（或另一个 AI）**：这是一份可以直接照着做的任务书。
> 上一条开发线刚跑通**真实工作流**（`godot_sample`，把 `values/level.gd` 的
> `HP_GROWTH` 0.12 → 0.15），一次真实 trace 挖出 8 个缺陷并修掉了7 个。
> 下面四件事与那条线**零文件重叠**，可以同时开工。
>
> 与 `HANDOFF-vibecoding.md` 的关系：那份记的是 vibecoding **能力**的边界
> （`dev_vue` / `dev_apptest` / 变异电池…）。这份只讲**现在这几件活怎么分工**。
> **不要改那份**——它在另一条 lane 的名下。

---

## 0. 先读这三条纪律（违反过两次，别做第三次）

### 0.1 共享工作树的索引是共享的 —— 用**私有索引**提交

`.git/index` 是共享的，另一条 lane 的 `git add` 会落进**同一个文件**。
本仓实测过一次：暂存区里多出 19 个别人的文件、约 3600 行被挂进别人的提交。

**规矩**（`HANDOFF-vibecoding.md` §7 规矩 1）：

- ❌ 永远不要 `git add -A`
- ❌ 永远不要 `git commit -- <路径>`（pathspec 会绕过索引，把别人同一批文件的
  在途改动一起扫进去）
- ✅ 只 `git add <自己的路径>`，然后**裸 `git commit`**
- ✅ **更好的做法**：私有索引

```powershell
$ErrorActionPreference = 'Stop'
$mine  = @('a.py','b.py')                      # 恰好只有你自己的
$idx   = Join-Path (Get-Location) ".tmp\private-$([Guid]::NewGuid().ToString('N').Substring(0,8)).index"
$env:GIT_INDEX_FILE = $idx
try {
  git read-tree HEAD
  foreach ($p in $mine) { git add -- $p }
  # 自检要查「有没有多出来的」，不能只查「我的在不在」——
  # 只问「我的东西在不在」的检查，对「多了不该有的东西」是**盲的**。
  $staged = @(git diff --cached --name-only HEAD)
  $d = @(Compare-Object ($mine|Sort-Object) ($staged|Sort-Object))
  if ($d) { throw "暂存区与预期不符：$($d | ForEach-Object { $_.SideIndicator + ' ' + $_.InputObject })" }
  # 暂存版做一次 ast.parse：半截 hunk 最容易留下语法残骸
  foreach ($p in ($mine | Where-Object { $_ -like '*.py' })) {
    git show ":./$p" | Out-File .tmp\stage-check.py -Encoding utf8
    .venv\Scripts\python.exe -c "import ast,io; ast.parse(io.open(r'.tmp\stage-check.py',encoding='utf-8-sig').read())"
    if ($LASTEXITCODE) { throw "$p 暂存版语法不完整" }
  }
  git commit -q -F commit_msg.tmp
} finally { $env:GIT_INDEX_FILE = $null; Remove-Item -Force $idx -ErrorAction SilentlyContinue }
```

**私有索引提交之后必须复位共享索引**（这条踩过两次）：

```powershell
$foreign = @(Compare-Object ($mine|Sort-Object) (@(git diff --cached --name-only HEAD)|Sort-Object) |
             Where-Object { $_.SideIndicator -eq '=>' })
if ($foreign) { "!! 共享索引里有别人的东西，别复位" } else { git reset -q HEAD }
```

不复位的后果：共享索引停在**旧 HEAD**，下一次任何人裸 `git commit` 都会
**撤销你刚做的提交**。

### 0.2 含非 ASCII 或转义的源码，**只用 write / edit 工具**

用 PowerShell 的 `(Get-Content -Raw) -replace ... | Set-Content -Encoding UTF8`
改源码，本会话里毁过三次：

- PS 5.1 的 `-Encoding UTF8` 会**写 BOM** → `test_no_python_file_starts_with_a_bom` 当场变红
- 转义序列被吃掉
- `\n` 变成真换行，把函数体黏到 `def` 行上

同样地：**不要写 `.ps1` 脚本文件**（PS 5.1 读无 BOM 的 UTF-8 会把中文当 ANSI、
**吃掉引号**，脚本自己语法都不成立）。要跑 git 序列就写成一条内联命令。

### 0.3 改完立刻核对 `git diff --stat` 的插入/删除计数

本会话有三次「编辑看起来成功、实际吃掉了一行」，全靠这个计数发现。

---

## 1. 环境事实（会咬人）

- **解释器**：`.venv\Scripts\python.exe`，命令都按"从仓库根运行"写。
- **pytest 必须带项目内 basetemp**：
  ```powershell
  .venv\Scripts\python.exe -m pytest <目标> -p no:cacheprovider --basetemp=.tmp/pt-<你的名字> -q
  ```
  用户名含撇号，默认 `.pytest_cache` / `pytest-of-*` 会 `Permission denied`。
- **`git push` 在此沙箱不通**。所有提交只在本地，需要人在自己机器上推。
- **共享工作树**：多个 AI 同时改同一棵树。**实测一次全量里 12 条红全来自
  别人未提交的 WIP** ——看到红先归属，别急着改。
- **HEAD 单独检出才是 CI 看到的那棵树**。提交后验一次：
  ```powershell
  git worktree add D:\Temp\opencode\wt-<你> HEAD --detach
  .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, r'D:\Temp\opencode\wt-<你>'); import config,tools,agent; print('OK')"
  git worktree remove D:\Temp\opencode\wt-<你> --force; git worktree prune
  ```
- **前端**：`frontend/node_modules` 里有 esbuild / vue-tsc / eslint 10，**没有 vitest**。
  改前端后必须 `npm run build`（产物在 `web/`），否则页面还是旧的。

---

## 2. 认领：`dev_lanes`

**开工前先 `action: status`，动某文件前 `action: check`，领到活就 `claim`。**

```
action: status                          看谁占了什么
action: check  + path: <相对路径>        问这一个文件现在归谁
action: claim   + lane: <名字> + paths: <路径，可逗号或换行分隔>
                    + owner: <署名> + ttl_minutes: 180 + note: <≤120 字>
action: heartbeat                       长任务定期续租
action: release                         做完
```

### 三个坑（工具描述里都没写，是我踩出来的）

1. **lane 名只能用 ASCII**（字母数字与 `. _ -`，≤49 字符，不能含 `..`）。
   中文名会被直接拒：`拒绝 lane 名 'AI-xxx'：只能用字母数字与 . _ -`。
2. **`note` 上限 120 字符**，超了直接拒。
3. **`dev_lanes(arg)` 收的是 keyed 多行文本**，不是 kwargs。调用形如
   `tools.dev_lanes("action: check\npath: agent.py")`。

### 路径要带目录前缀

`*.vue` 这种**没有目录前缀**的模式等于**占下全仓**，会与所有 lane 冲突。
要写 `frontend/src/**/*.vue`。

### 当前登记表：空

上一条线已 `release`。

---

## 3. 已知红清单（**别当成自己造成的**）

以下 10 项在 `HEAD` 上就是红的，与任何新改动无关：

| 文件 | 项数 |
|---|---|
| `tests/test_agent.py::SubagentPlanningRoleTests::test_planner_cannot_widen_tools_or_enable_mcp` | 1 |
| `tests/test_agent_capacity.py::AgentCapacityTests::test_workflow_plan_surfaces_configured_child_step_limit` | 1 |
| `tests/test_cloud_agent_registration.py::CloudLlmOverrideTests::*` | 2 |
| `tests/test_orchestrator.py::AgentOrchestrateTests::test_planner_role_can_decompose_before_execution_role` | 1 |
| `tests/test_workflow_step_budget.py::*` | 5 |

最后那条 `test_orchestrator` 的来历值得一说：它**此前一直没人看得见**，
因为同文件里另一个测试会**无限挂死**，整个文件跑不完。挂死修好后它才浮出来，
并已用 worktree 在 `HEAD` 上复验为**既有红**（断言要 `"子代理结论"`，
实际拿到一段能力说明文本）。

**归属方法**（§7 规矩 3）：把 HEAD 挂成干净 worktree 跑同一批文件。
红的在两边都出现 → 不是自洽问题；只在工作树出现 → 是你的。

---

## 4. 四件可以并行的活

### 任务 A：跑那 7 个从未被执行过的 e2e / live 测试 ★最高价值

**为什么重要**：上一条线历次回归都用文件名过滤器挑文件，**这 7 个一直没跑过**，
所以它报出的「多少项通过」**不包含它们**。而它们恰好是「工作流真跑起来会怎样」
最相关的证据。

**文件**（共 1290 行，**只跑、只报告，先别改**）：

```
tests/test_live_llm_workflow.py               90
tests/test_game_workflow_e2e                 184
tests/test_real_project_workflow_e2e         205
tests/test_vision_workflow_e2e               247
tests/test_web_research_product              265
tests/test_realtime_bench_tool               208
tests/test_postgres_workflow_integration      91
```

**怎么做**

```powershell
# 一个一个跑，别一起跑——一起跑分不清是谁的锅
foreach ($f in @('test_live_llm_workflow','test_game_workflow_e2e', ...)) {
  "=== $f ==="
  .venv\Scripts\python.exe -m pytest "tests\$f.py" -p no:cacheprovider --basetemp=.tmp/pt-e2e-$f -q 2>&1 |
    Select-String -Pattern '^FAILED|ERROR|passed|failed|skipped' | Select-Object -First 12
}
```

**完成判据**

- 一张表：每个文件 → 通过 / 失败 / skip / 收集失败，各多少条
- 每条失败归到三类之一：**真红** / **环境缺**（无 Edge、无 node、真网络不可达）
  / **别人未提交的 WIP**
- 若某个文件**无限挂死**，**单独记下来并给出证据**（如"单跑 5 分钟 CPU 持续
  增长不返回"）——上一条线就是靠这个发现 `test_orchestrator.py` 的挂死的
- **不许改任何产品代码**。发现问题写进报告，由主线决定谁修。

---

### 任务 B：给上一条线新增的 19 项守卫补变异电池 ★它自己欠的账

**为什么重要**：本仓的规矩是「**新守卫必须配一条变异电池**」
（`HANDOFF-vibecoding.md` §7.5）——「加一条测试/门禁时先做变异测试：把它故意改坏，
看有没有用例会红；不红就是装饰」。

上一条线新增了 19 项守卫、**0 条电池**：

```
tests/test_workflow_tool_events.py        （工具事件落流 / retry 端点契约 / 跨层一致性）
tests/test_reflection_sees_evidence.py    （反思判据看得见工具观察 / 模型等待事件配对）
tests/test_empty_model_turn.py            （空响应不崩成 AttributeError）
tests/test_subagent_step_floor.py（子代理兜底上限）
tests/test_plan_dag_not_truncated.py      （多步 DAG 不被静默截断）
```

**文件**：新建 `tests/mutation/battery_workflow_events.py` 等（**全新文件，与人无冲突**）

**怎么做**：照抄 `tests/mutation/battery_windows_sandbox.py` 的结构
（`MODULE` / `TARGET` / `CASE_ID` / `TEST_MODULE` / `BIND` / `UNIT_CLASSES` /
`LIVE` / `MUTATIONS`）。**没有现场层的也要显式写 `LIVE = ()`**——元测试读
`battery.LIVE` 而不是 `getattr(..., ())`，缺属性就是 `AttributeError`。

```powershell
.venv\Scripts\python.exe tests/mutation/run_all.py --no-live -v
```

**完成判据**：每条变异要么被抓、要么记「本环境未验证」、**不许有「活下来」**。

**坑（`HANDOFF-vibecoding.md` §5.17 / §9 有完整版，这里列最常见的）**

- **等价变异**是自己写给自己的假分数。写完问两句：
  「它依赖的是**代码**还是**环境**？」「它给的树有**几层**？」
- **锚点不唯一**会被打进没人调用的函数。优先挑**唯一**的锚点（多带一行上下文）。
- **`BIND` 写错时纯层看的是真模块**，纯层会全"活下来"、只有门禁层抓到。
  报"活下来"时**先怀疑接线**，再怀疑守卫。
- **装不进来的变异证明不了任何判据**。引擎会替你报 `ast.parse`，但那时你已经
  浪费了一轮。

---

### 任务 C：补 `requirements.txt` 里 8 个未声明的依赖

**为什么重要**：`api.py` 直接 `import starlette`（30 个文件在用）却只声明了
`fastapi`。**本机因为恰好装过所以一切正常，别人 clone 或 CI 拉干净检出时第一句
import 就 `ModuleNotFoundError`** —— 这正是 vibecoding 最常见的死法。

**文件**：`requirements.txt`（**只改这一个文件**）

**当前仍缺**（用 `dev_deps` 复核过）：

```
starlette    pydantic    anyio    cryptography
PyYAML       pywin32     sentence-transformers    psycopg    pywebview
（websocket-client 已声明）
```

**怎么做**：优先跑工具，它只往**已存在**的清单追加缺失行、每行带"谁 import 了它"
的注释，**不改版本、不删行、不重排**：

```
dev_deps(action: apply)
```

**完成判据**：补完之后 `dev_deps` 报告里不再有 `missing` 项。

⚠️ **版本由人定** —— 工具会留空版本让你填，**不要自己挑版本号**。
不确定的项它会归 `unknown` 并明确报出来，**绝不猜一个名字写进清单**（猜错等于
装了个不相干的包而真正缺的那个还缺着）。

---

### 任务 D：更新 `docs/HANDOFF-vibecoding.md`

**为什么重要**：那份是下一条 lane 的**唯一入口**，而它现在**账已经过期、
本会话的发现一条都没记**——不更新就等于让下一个人再踩一遍。

**文件**：`docs/HANDOFF-vibecoding.md`（**独占**，别和别的 lane 同时改）

**要改两处**

**① §10 的账过期了。** 现在写着：

> 全量最后一次：**2569 passed / 12 failed**，12 条已用干净 worktree 证明全部
> 来自另一条 lane 未提交的 step-budget / capacity / game_workflow WIP

实际：上一条线用 `e0d182d` 修掉了 **game_workflow 那 4 项**，剩 **8 项**。
照原账判断会错。

**② 补记本会话的发现。** 每条都写清「**实测依据**」，别写结论式描述：

| 缺陷 | 实测依据 | 状态 |
|---|---|---|
| 子代理**无限挂死** | `MAX_AGENT_STEPS` / `SUBAGENT_MAX_STEPS` / `SUBAGENT_STEPS_HARD_CAP` 三处默认全 0，而 `_run` 的 `while True:`唯一上界是 `tool_step_limit > 0` →结构上无界。假模型每轮换参数 + 换措辞，`executed` 与 `_repeated_sentence` 两道防空转都拦不住 | 已修（`SUBAGENT_STEP_FLOOR=200`） |
| 挂死**掩盖了一条既有红** | `test_orchestrator.py` 修复前超 8 分钟不返回；修好后 3.13 秒跑完，露出 `test_planner_role_can_decompose_before_execution_role`，已用 worktree 复验为既有红 | 已修挂死；红未代改 |
| 「重试该成员」**100% 报 500** | `workflow_callbacks` 返回 `{runner, synth_runner, replanner}`，端点三个一起 splat，而 `retry_subagent` 签名里没有 `replanner`。**无任何测试覆盖该端点** | 已修 |
| 「实际工具调用」**恒 0 条** | 面板按 `before_tool/after_tool/…` 从**持久事件数组**过滤，而工具事件只进子代理本地 hook 缓冲，从不落工作流事件流（实测那条 trace 43 条事件里工具事件为 0） | 已修 |
| hook 缓冲配额被工具事件吃光 | `if len(hook_events) >= 48: return` 在**最前面**；每次工具调用发 before+after 两条 → **24 次调用就耗尽** | 已修 |
| 子代理失败**只存标签、丢掉堆栈** | 磁盘上只有 `error = AttributeError: 'NoneType' object has no attribute 'strip'`，无堆栈 → 只能全文撒网猜 `.strip()` 在哪，**猜错两次** | 已修 |
| 多步 DAG **静默塌成根任务** | `len(clean_deps) > max_tasks` 拿**依赖个数**比**任务总数上限**，而 `max_tasks=0` 的约定是"不限" → 默认配置下**任何声明 `depends_on` 的任务都被静默丢弃**，5 步只剩 1 步，全程零报错 | 已修 |
| 反思判据**结构上只能判"结论复述了吗"** | `traj` 传进来了但 prompt 里只有 `步骤数：N；失败步骤：M`，**观察一个字都没给** → 判据不知道观察里有 `WRITE_OK: True`，于是三次正确的字节级写入被连续判失败、逼出两轮 replan | 已修 |
| 空响应**崩成 AttributeError** | `llm.py` 的 `getattr(msg, "content", None)` 在 `content=None` 时原样返回；有个测试把它钉成正确行为（对 tool-call-only 消息确实对），缺的是 agent 侧**没区分"带 tool_calls 的 None"（正常）与"不带 tool_calls 的 None"（空响应）** | 已修（改 agent 不改 llm，改 llm 会破坏原生 FC 通道） |
| 试做区隔离**疑似失效** | 子代理用相对路径 `open("values/level.gd","wb")` 写入，事后核对：真实项目 mtime 22:53:12 已改、试做区 workspace/baseline 仍是 9-11 的原始 mtime。已排除三种猜测（scope 机制正常、是 contextvar 不会跨线程、并行批次已做 context 复制） | **未定位**，只加了诊断 |
| 全程 **98% 静默** | 那次 360 秒里相邻事件间隔 ≥5s 的总时长 354 秒；**每段静默都是一次模型调用** | 已修（`llm_wait_start/end`） |
| 时间线**六成是英文** | 实测那次发出 27 种 kind，前端 `timelineLabel` 只覆盖 20 种，包括最该看懂的 `graph_replan_start` | 已修（补 16 个 + 跨层一致性测试） |

**写法要求**（照 §5 那些条目的成色）：每条都写「**为什么**」和「**实测依据**」，
并写清**边界**（不证明什么）。别写「已修复」了事。

---

## 5. 怎么报告

每条任务交付时给出：

1. **命令与真实输出**（不是"我跑过了，没问题"）。
2. **红的逐条归属**：真红 / 环境缺 / 别人的 WIP。拿不准就按 §7 规矩 3 用
   worktree 验，别猜。
3. **没做完的明说**，别用"基本完成"。
4. **发现但没修的，单独列**——本会话里最有价值的几条都是这么冒出来的：
   挂死的测试文件**会把自己挤出所有回归范围**，而且没人发现。

## 6. 还没定位的那件事（谁跑工作流就会撞上）

**试做区隔离为什么失效**尚未定位。`python_exec` 现在会在观察里自报实际执行目录：

```
执行目录：<路径>   ← 不在试做区内时会明说「相对路径会写进真实项目」
```

**下次真跑一次工作流就会给出答案。** 这条只能靠真跑拿，静态查已经排除了三种猜测。