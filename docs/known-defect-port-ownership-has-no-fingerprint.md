# 端口归属判据不可用：页面里没有能区分项目的指纹

> 立档：**2026-10-08**。前一篇 `known-defect-dev-server-port-ownership.md`
> 建议「让 `preview_project` 校验端口归属」并把它列为未做项。
> **本篇说明那一项为什么做不了**，以及真正的修法。

## 1. 一句话

`dev_server.owns_port(port, markers=...)` 的判据是**页面上有没有那些字串**，
而 Agent 工作台 的页面里**没有任何只属于本项目的字串** —— 所以归属判据在生产里用不了。

那个假绿灯事故（拍到另一个 worktree 的画面并报成功）**仍未堵**。

## 2. `owns_port` 已实现，但接不了

`owns_port` 在 `bbc0207` 落地，有 7 条守卫（`tests/test_dev_server_owns_port.py`），
**但没有生产调用点**。原因不是没接线，是**没有能喂给它的指纹**。

### 三个候选指纹，逐个核实

| 候选 | 实测 | 可用 |
|---|---|---|
| 页面 `<title>` | `index.html` = `Agent 工作台 AI 问答`；`landing.html` = `Agent 工作台 · 本地 AI 研发脚手架`；`workbench.html` = `Agent 工作台` | ❌ **三个都以 `Agent 工作台` 开头**，而另一个 worktree 跑的是**同一份代码**，标题完全相同 |
| 项目名注入页面 | `git grep "APP_ID\|PROJECT_NAME\|VITE_APP" -- frontend` → **零命中** | ❌ 根本没注入 |
| vite dev 的 URL / 路径 | dev URL 就是 `http://127.0.0.1:5173/`，不含项目路径 | ❌ |

**结论：两个 worktree 的 vite 在 HTTP 响应上不可区分。**

而那个事故恰恰就是 worktree 撞端口（`known-defect-dev-server-port-ownership.md` §2
实测过 `[::1]:5173` 上是另一个项目的 vite）。

### 连「本会话的 dev server」也拿不到

原计划是「把 `dev_serve` 的 job 记录带进 `preview_project` 入参」。核实：

* `dev_server` 的公开函数只有 `free_port` / `loopback_url` / `parse_port_hint` /
  `port_taken` / `owns_port` / `probe` / `start` / `stop` / `render` ——
  **没有「按 job_id 查状态」**（`stop` 除外）。
* `process_runner` **没有「列出活跃 job」**的函数 ——
  `job_logs` / `job_count` / `job_cancel` / `start_job`，**没有枚举**。

所以 `preview_project` 无从知道「本会话起了哪些 dev server」。

**而且即使模型传了 `job_id:`，也只能验证「那个 job 还活着」，
验不了「这个端口上的服务是我的」** —— 撞端口的 worktree vite 不是经 `dev_serve` 起的。

### 一条好消息：入参契约不用改

`preview_project` 的 `input_schema` 只有 `input`（keyed 多行），**不是逐参数声明**，
所以将来加 `job_id:` / `project_id:` 这类键**不需要改 schema**，只要解析它。

## 3. 真正的修法：**注入身份**

要让归属可判，页面里必须有**只属于这个项目**的东西。按代价：

| 方案 | 代价 | 效果 |
|---|---|---|
| **A. vite dev 注入项目标识** —— 插件在 dev 时往 HTML 注入 `<meta name="docmind-project" content="<root 绝对路径的稳定哈希>">` | 改 `frontend/vite.config.ts`，**前端域** | `owns_port` 直接可用；**这是唯一能让归属真正可判的办法** |
| B. `dev_serve` 起服务时注入环境变量，页面读出来显示 | 改 `dev_server` + 前端读 env | 只覆盖走 `dev_serve` 的，**覆盖不了 worktree 撞端口那种** |
| C. `preview_project` 截图后核对页面内容 | 改 `visual_acceptance` | 弱，且仍需要「一个唯一串」—— 本质回到 A |

**结论：修法 A 属前端域。** 后端侧保持现状，等注入落地后接线即可。

## 4. 接口约定（给前端实现用）

要在 vite dev 的 HTML 里注入一个**每个项目唯一**的标记。约定：

| 项 | 值 | 理由 |
|---|---|---|
| meta 名字 | `docmind-project` | 与现有 `docmind=` 查询参数（`capture_project_preview` 在 URL 上加的）同源，便于将来一起用 |
| meta 内容 | **当前项目 root 绝对路径的稳定哈希**（建议 sha256 前 12 位十六进制） | 路径含 worktree 名与盘符，**明文会泄露本机目录结构**；哈希稳定且不泄露 |
| 注入位置 | `index.html` / `landing.html` / `workbench.html` **三个入口都要** | 只注入一处会让另两个入口的验收拿不到指纹 |
| 生产构建 | **不注入**（或注入构建时的值） | 打包产物是同一份，不该带开发机路径 |
| 同一项目多端口 | 内容**相同**（由 root 决定，与端口无关） | 端口会漂移，指纹不能跟着端口变 |

`owns_port` 侧只需把 `markers` 设为 `["docmind-project", "<该项目的哈希>"]`，
其余逻辑（页面取正文、判命中、没标记就拒绝猜）已经写好且有守卫。

## 5. 后端侧现状（**未接线，不是忘了**）

* `dev_server.owns_port()` —— 已实现，7 条守卫，**无生产调用点**
* `visual_acceptance.resolve_live_url()` —— 已实现，**不做静默换端口**
  （给定地址不通就早失败并列出候选）
* `capture_project_preview` 在给定 `url` 时**早失败**而非换端口 ——
  所以「打到别人服务并报成功」在**给定正确地址**时不会发生；
  但「模型自己填了个错地址，而那个地址上正站着别人」仍会。

**最后那半句是本篇要说清的真实敞口**：现有护栏挡的是「地址不通时乱换」，
挡不住「地址通、但不是我的项目」。

## 6. 复现核实

```bash
# 1) title 不可区分
Get-Content frontend\index.html -Encoding UTF8 | Select-String '<title>'
#   -> Agent 工作台 AI 问答（三个入口都以 Agent 工作台 开头）

# 2) 项目名没注入
git grep -n "APP_ID\|PROJECT_NAME\|VITE_APP" -- frontend    # 零命中

# 3) owns_port 的判据确实依赖 markers
.venv\Scripts\python.exe -B -X utf8 -c "import sys,inspect;sys.path.insert(0,'.');from agent_runtime.dev_server import owns_port;print(inspect.signature(owns_port))"

# 4) 没有枚举活跃 job 的能力
.venv\Scripts\python.exe -B -X utf8 -c "import sys;sys.path.insert(0,'.');from agent_runtime import process_runner as P;print([n for n in dir(P) if 'list' in n or 'jobs' in n or 'active' in n])"
```

## 7. 与其它文档的关系

* `known-defect-dev-server-port-ownership.md` §9 列了「`owns_port` 还没有生产调用点」
  作为未做项 —— **本篇解释为什么**。
* 那篇 §8.3「不静默换端口」与本篇的差别：那篇挡的是**地址不通时换一个**，
  本篇说的是**地址通但归属不对**。**两个洞，不是一个。**