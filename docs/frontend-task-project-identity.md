# 前端要做的一件事：让 vite dev 的页面带上「项目身份」

> 需求方：后端。**前端域改动**，后端不越界代做。
> 完整背景与实测见 `docs/known-defect-port-ownership-has-no-fingerprint.md`。

## 为什么需要（真实事故，不是假想）

本机曾同时跑着两个 vite：一个在 `127.0.0.1:5173`（本项目），
另一个在 `[::1]:5173`（**另一个 worktree 的项目**）。两者同号、不同地址族，
互不冲突、同时存活。

后果：`preview_project` 打 `127.0.0.1:5173` 会拍到**哪个项目的画面取决于谁的监听在**，
而「浏览器没报错、截图是真的」—— **一个假绿灯**。

后端已经有判据 `dev_server.owns_port(port, markers=...)`（判「页面上有没有某串字」），
**但它现在用不了，因为页面里没有任何只属于本项目的字串。** 实测三个候选全部不可用：

| 候选 | 实测 | 结论 |
|---|---|---|
| `<title>` | `index.html` = `Agent 工作台 AI 问答`；另两个入口 = `Agent 工作台 · …` / `Agent 工作台` | ❌ 三个都以 `Agent 工作台` 开头，而 worktree 跑的是**同一份代码**，标题完全相同 |
| 项目名注入页面 | `git grep "APP_ID\|PROJECT_NAME\|VITE_APP" -- frontend` → **零命中** | ❌ 没注入 |
| dev URL / 路径 | `http://127.0.0.1:5173/`，不含项目路径 | ❌ 无区分 |

## 要做什么

在 vite dev 的 HTML 里注入**一个每个项目唯一**的标记。

### 约定（后端已经按这个写好接线，只需你注入这一半）

| 项 | 值 | 理由 |
|---|---|---|
| meta 名 | `docmind-project` | 与 `capture_project_preview` 已在 URL 上加的 `docmind=` 查询参数同源 |
| meta 内容 | **当前项目 root 绝对路径的稳定哈希**，建议 `sha256(绝对路径)` 取前 12 位十六进制 | 明文会泄露本机盘符与目录结构；哈希稳定且不泄露 |
| 注入位置 | `index.html` / `landing.html` / `workbench.html` **三个入口都要** | 只注入一处，另两个入口的验收拿不到指纹 |
| 生产构建 | **不要注入**（或注入一个固定值） | 打包产物只有一份，不该带任何开发机信息 |
| 端口变化 | 内容**不变** | 指纹由 root 决定，与端口无关 —— 端口会漂移（5173→5174），指纹不能跟着变 |

### 建议的落法

`frontend/vite.config.ts` 加一个只作用于 dev 的插件。加在 `plugins:` 数组里
（该文件已有 `cleanAssets()` 与 `serveSessionScript()` 两个先例，风格照它们）。

**注意该文件现有的 import**：`node:path` 只导了 `resolve`（**没有 `path`**），
`node:crypto` **完全没导**。所以下面需要新增 `createHash`：

```ts
// 新增到文件顶部
import { createHash } from 'node:crypto'

/** dev 时往每个入口 HTML 注入项目身份，让归属判据能工作。
 *  为什么：两个 worktree 会同号不同地址族地并存，页面若没有只属于自己的标记，
 *  视觉验收会拍到别人的画面并报成功（假绿灯）。
 *  为什么用哈希不用明文：路径含盘符与目录名，明文会泄露本机结构。
 *  为什么只在 dev：打包产物只有一份，不该带开发机信息。
 */
function projectIdentity(): Plugin {
  return {
    name: 'docmind-project-identity',
    apply: 'serve',                 // 只在 dev server 下生效，build 时不做
    transformIndexHtml(html: string) {
      const digest = createHash('sha256')
        .update(resolve(__dirname, '..'))   // 项目 root = frontend/ 的上一级
        .digest('hex')
        .slice(0, 12)
      if (html.includes('name="docmind-project"')) return html
      const tag = `<meta name="docmind-project" content="${digest}">`
      return html.replace(/<head>/i, `<head>\n    ${tag}`)
    },
  }
}
```

然后在 `plugins:` 里加一项 `projectIdentity(),`。

**具体写法你定** —— 上面只是把约定摆出来，并核对了三件事：
`Plugin` 类型该文件已导入、`resolve` 与 `__dirname` 可直接用
（`serveSessionScript()` 里就是这么写的）、`plugins:` 数组在第 47 行。

**`resolve(__dirname, '..')` 的取值要你确认**：项目 root 是 `frontend/` 的上一级
（`package.json` 在 `frontend/`，而后端读的是 `code_root`）。若你认为该用别的基准，
以你的判断为准 —— **只要同一项目两次算出一致、不同项目算出不同**即可。

## 守卫建议

前端若已有 node:test 或 vitest 体系，加一条断言**结构**而不是具体哈希值：

1. `apply: "serve"` 存在 —— 否则生产构建也会注入
2. 三个入口在 dev 构建产物里**都**含 `name="docmind-project"`
3. **同一 root 两次构建哈希相同**（稳定性）
4. **不同 root 哈希不同**（区分性）—— 这条是本需求的全部意义所在
5. 生产构建产物里**不含**该 meta

## 后端侧会做什么（不用你管）

注入落地后，后端把 `owns_port` 接进 `capture_project_preview`：

```python
owns_port(port, markers=["docmind-project", <该项目的哈希>])
```

`owns_port` 的其余逻辑已经写好且有 7 条守卫：

* 命中标记 → 认领
* 页面正常应答但**没有标记** → **拒绝认领**（不猜，宁可拒）
* **不给标记时不猜** → 返回「无法判归属」并要求调用方补标记

## 一个诚实的边界

即使注入了指纹，仍有一个残留场景：**指纹只在页面正文里，而 `preview_project`
给 `url` 时是直接打那个地址的**。所以：

* 「地址不通 → 我换一个」—— **已堵**（`resolve_live_url` 早失败并列出候选，不静默换）
* 「地址通、但不是我的项目」—— **注入指纹后才堵得住**
* 「我的项目恰好和别人共用同一个 root」—— 指纹相同，仍不可区分（但那时它们本就是同一份代码，危害小得多）

## 如果不做

后端会保留现状，并在 `docs/known-defect-dev-server-port-ownership.md` 里
标着「归属校验未接线」。**那个假绿灯就仍然敞着** —— 它只在
「模型填了个属于别人的端口，而那个端口上确实有服务」时发生，概率不高但不是零。