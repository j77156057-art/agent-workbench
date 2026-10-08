# 三个 harness 的一手来源核实记录

> 核实日期：**2026-10-08**。全部结论来自**项目自身的一手文件**（README / docs /
> 包的 README），不用二手转述、不用搜索摘要。
> **Claude Code 泄露源码不作为来源**（既定纪律，本次遵守）。
>
> 这篇替代 `docs/next-steps.md` 第 3 节的旧版 —— 旧版是昨晚凭搜索摘要写的，
> 有两处不准确，已在第 5 节列出更正。

---

## 0. 一句话结论

三个 harness 在「agent 能不能越权」这同一个问题上给了**三种不同立场**，
**而我们目前做的那一种恰好是被最多方否掉的那一种**。

| harness | 立场 | 一句话 |
|---|---|---|
| **Pi** | **弃权** | 不做权限系统。transcript 审查**不是**安全边界，推给容器 |
| **Hermes** | **双轨** | 做审批（LLM 辅助风险判定 + fail-closed 超时 + 非交互默认拒）**且**做容器隔离；并明说写守卫只是纵深防御、不是边界 |
| **opencode codemode** | **语法化** | 让越权**在语法上不可表达**；授权明确列为 `Non-Goals`，归宿主 |
| **DocMind（现状）** | **文本推断** | 从用户那句话猜授权 —— **正是 Pi 明说「不构成安全边界」的那一类** |

---

## 1. Pi —— `earendil-works/pi`

来源：`README.md`、`packages/coding-agent/docs/security.md`

### 1.1 确实没有权限系统（核实）

README 原文：

> Pi does not include a built-in permission system for restricting filesystem,
> process, network, or credential access. By default, it runs with the permissions
> of the user and process that launched it.

`security.md` 原文：

> Pi can read, change, and execute files with the permissions of the account that
> started it, and **it does not ask for approval before every tool call**.

### 1.2 **关键发现：transcript 审查被明确否认为安全边界**

`security.md` 原文（逐字）：

> **Watching the transcript, using project trust, and reviewing changes do not
> create a security boundary.**

这一条**直接命中我们**。核实了我们自己的实现（不是凭印象）：

| 我们的机制 | 代码位置 | Pi 的分类 | 算不算边界 |
|---|---|---|---|
| `_write_intent(question)` 从消息文本判授权 | `agent.py:3172` | watching the transcript | ❌ **不是** |
| `_ambiguous_write_request()` 从消息文本判能力问句 | `agent.py:2048` | watching the transcript | ❌ **不是** |
| `_lease_blocked()` 查租约表 | `agent.py:3269` | 结构化授权 | ✅ 是 |
| `_intent_for_delegated()` 查 `tool_allowlist` + 父租约 | `agent.py:4457` | 结构化授权 | ✅ 是 |
| `loopback_url()` 回环校验 | `dev_server.py:53` | 限制可达资源 | ✅ 接近 Pi 的隔离思路 |

**这不否定已提交的代码。** 今天 17 笔里，租约 / 委派 / 结构化那部分是站得住的；
词表判定那部分修的是**过拦与误判**，方向是安全的。但它给出了一条硬约束：

> **别再往词表里加词了。** 每加一个词就多一处「靠散文维持」的地方，而 Pi 的判断是
> 那一整类都不构成边界。

这与我们在 `docs/next-steps.md` 第 1 节独立得出的结论一致，现在有外部一手来源背书。

### 1.3 Pi 给的替代方向

`security.md` 的三层运行方式，越往下越强：

| 怎么跑 | 还剩下什么没被保护 |
|---|---|
| 直接跑（当前用户权限） | 该用户碰不到的东西 |
| 整个跑在容器/虚拟机/sandbox 里 | 你没暴露给它的宿主文件与进程 |
| 隔离环境外 + 只有内置工具在隔离区内 | 通过那些工具做的事伤不到宿主（较窄） |

> Safety comes from limiting the files, credentials, processes, and network
> services Pi can access and affect ... **Watching the transcript, using project
> trust, and reviewing changes do not create a security boundary.**

**注意 Pi 连「project trust」一起否掉了** —— 那是个启动期加载哪些项目资源的机制，
与我们「角色提示词决定权限」的错误（`fa81aa3` 修的那族）同类：
`security.md` 说「Project trust does not limit what tool calls can access or affect」。

### 1.4 供应链硬化（核实，逐条）

README 的 "Supply-chain hardening" 一节：

- `We treat npm dependency changes as reviewed code changes.`
- 直接依赖**钉到精确版本**（`save-exact=true`）
- `.npmrc` 设 **`min-release-age=2`**，避免解析到当天发布的依赖
- `package-lock.json` 是「the dependency ground truth」
- pre-commit 阻止误提交 lockfile，除非 `PI_ALLOW_LOCKFILE_CHANGE=1`
- 生命周期脚本有**显式白名单**，新依赖若带 lifecycle 脚本则检查失败直到人工审
- 安装器从 `install-lock/` 装，把**传递依赖**也钉住
- 定时 workflow 跑 `npm audit --omit=dev` + `npm audit signatures`

#### 我们的现状（核实，2026-10-08）—— **差距比清单看起来小，但有一处 Pi 没有的洞**

| Pi 的做法 | 我们 | 依据 |
|---|---|---|
| `.npmrc` 设 `min-release-age=2` | ✅ **已加**（`frontend/.npmrc`）—— 实测生效，见下方专节 |
| 直接依赖钉精确版本（`save-exact=true`） | ⚠ **未钉** —— 33 个前端依赖（deps 22 + dev 11）里 **29 个**是 `^`/`~` 范围；**只有 4 个** `@fontsource/*` 是精确版。**但 `package-lock.json` 钉住了实际解析结果**，所以 `^` 只在**有人主动改依赖**时才产生漂移 | `frontend/package.json` |
| `package-lock.json` 是唯一真相 | ✅ **是** —— CI 用 `npm ci`（`.github/workflows/harness.yml:59`），启动同步器也用 `npm ci`（`dependency_bootstrap.py:414`） | 实测 |
| 生命周期脚本**白名单** | ⚠ **只在一条路径上有，且不能照抄** —— `tool_install.py:92`（agent 装 MCP 工具）用 `--ignore-scripts`（全禁）；启动同步的 `npm ci`（`dependency_bootstrap.py:414`）与 CI（`harness.yml:59`）**都没加，而加了会坏**（见下） | 实测 |
| pre-commit 阻止误提交 lockfile | ❌ 无 | 实测 |
| 传递依赖钉住（`install-lock/`） | ⚠ 部分 —— `package-lock.json` 本身含传递依赖，但**没有 Pi 那种「只从 install-lock 装」的通道** | 实测 |
| 定时 `npm audit` | ❌ 无 | 实测 |

**结论：npm 那半边的差距在 lockfile 已提供等价保护的情况下不大 ——
但 `--ignore-scripts` 只覆盖了一条路径（见下）。**

⚠ **核实出一条我先前说反了的事** —— 我先写「`--ignore-scripts` 比 Pi 的白名单
更严，所以 npm 侧强于 Pi」。**那是只看了 `tool_install.py:92` 一处**。
逐条列全仓的 npm 调用点后：

| 调用点 | 有 `--ignore-scripts` 吗 |
|---|---|
| `tool_install.py:92`（agent 装 MCP 工具） | ✅ 有 |
| `dependency_bootstrap.py:414`（**启动时同步前端依赖**） | ❌ **无** |
| `harness.yml:59`（CI） | ❌ **无** |

即 **lockfile 里任何一个带 lifecycle 脚本的包，都会在用户启动应用或跑 CI 时执行它**。
而「agent 自己请求装的包」反而是安全的 —— **安全与风险的分配正好反过来**。

##### ✅ `min-release-age=2` 已加（`frontend/.npmrc`）—— 三道门都实测过

动手前先答了三个问题，**每一个都是「不实测就会搞错」的**：

| 门 | 实测 | 探针 |
|---|---|---|
| 这个特性在本机 npm 上**生效**吗 | ✅ 生效。设 `9999` 天后 `npm install vite` 报 **`ENOVERSIONS` / `No versions available for vite`**（对照组装成功） | `probe_min_release_age.py` |
| 它会**破坏我们的 `npm ci`** 吗 | ❌ 不会。设 `9999` 天（lockfile 里任何包都不可用）时 `npm ci` 仍装上 **215 个包** | `probe_min_release_age_vs_ci.py` |
| 设 `2` 会**让人工安装失败**吗 | ❌ 不会。lockfile 里**最年轻的依赖是 33.9 天**（`@codemirror/state@6.7.4`），远高于 2 | `probe_min_release_age_value.py` |

⚠ **「对 `npm ci` 无影响」是实测出来的，不是推理出来的。**
推理上 `npm ci` 严格按 lockfile 装、不重新解析，所以一个**解析阶段**的特性
不该影响它 —— 但今晚的教训正是「推理听起来合理 ≠ 事实」。
若实测显示会影响，**这一行就不能加**（应用启动失败 + CI 全红）。

**覆盖面要说准，别高估**：`npm ci` 不受影响 = 这一行**只覆盖人工
`npm install`**。我们 6 处 `npm install` 全是文档/错误提示里的手写指令，
自动化路径全走 `npm ci`。所以它防的是「有人手动装一个当天发布的包」，
**不是「启动时被投毒」**（后者本来就被 lockfile + `npm ci` 关着了）。

**为什么是 2 而不是更大**：Pi 用 2。实测 7 也安全（最年轻的依赖 33.9 天），
但更严的代价是**真正的安全补丁也要等几天才能装**。
**照抄 Pi 的值，不自行加码。**

##### ⚠ 一个会把人带沟里的怪癖（守卫专门挡它）

设了之后 **`npm config get min-release-age` 回读 `null`** —— 但**行为生效**
（npm 的 config schema 没登记这个键）。

**不写清楚的话，下一个人查一下就会以为配置坏了、删掉它，而删掉后
没有任何测试会红。** `tests/test_min_release_age_pinned.py` 三条守卫：

| 守卫 | 挡什么 | 变异实测 |
|---|---|---|
| `_the_file_ships_with_the_flag` | 删文件 / 删行 / 改成 `0` / 改成 `9999` | red |
| `_the_npm_version_supports_it` | CI 换 node 导致特性消失，配置静默变成废话 | 需改 CI 才验 |
| `_the_flag_is_not_pointless` | **反向守卫**：加入「回读 null 即无效」这种**错误结论**也会红 | red |

五条变异（注释掉 / 改 0 / 改 9999 / 加坏注释 / 无关注释对照）
**全部符合预期**，见 `tools/mutation/mutate_min_release_age_guard.py`。

##### ⚠ 「能不能给 `npm ci` 加 `--ignore-scripts`」—— 当时的判断，两次都说过头了

查 `package-lock.json` 的 263 个包条目，**4 个标了 `hasInstallScript`**：

| 包 | dev? | optional? | 备注 |
|---|---|---|---|
| `@vueuse/{core,shared}/vue-demi` | ❌ **生产依赖** | — | `@vueuse/core` 由 `@vue-flow/core` 传递引入，而后者被 `FlowCanvas.vue` / `SceneCanvas.vue` / `SceneNodeCard.vue` import，`vite.config.ts:70-73` 还有专门的 chunk 规则 |
| `esbuild` | ✅ dev | — | 构建工具 |
| `fsevents` | ✅ dev | ✅ optional | macOS only |

**我先断言「加了会弄坏前端」，两次都被自己的数据推翻：**

1. 第一次写「`lib/index.mjs` 是 postinstall 的产物，包里没有」→ **不成立**。
   `npm pack vue-demi@0.14.10 --dry-run` 显示 tarball 里**有** `lib/index.mjs`
   （21 个文件，含 `lib/v2|2.7|v3/index.{cjs,mjs,d.ts}`）。
2. 改写「postinstall 在我们场景下是**幂等**的」—— 这条**成立**且可复算：
   `scripts/utils.js` 的 `copy()` 做的是
   `content.replace(/'vue'/g, "'${vue}'")`，而我们装的就是 `vue`，
   所以**替换等于原样**。实测 `lib/index.mjs` 与 `lib/v3/index.mjs`
   **逐字哈希相同**（`56d7ae03…`），文件里仍有 2 处裸 `'vue'`。

⚠ **这一节我连续四次说过头，逐条记下来 —— 因为它正是「恒红/恒绿判据」的样本。**

| # | 我先说 | 实测 | 真因 |
|---|---|---|---|
| 1 | 「`lib/index.mjs` 是 postinstall 的产物，包里没有」 | **不成立** | `npm pack --dry-run` 显示 tarball 里**有**（21 个文件） |
| 2 | 「postinstall 会把 v3 变体复制上去」 | **机制对，但幂等** | `copy()` 里 `content.replace(/'vue'/g, "'vue'")` —— 我们装的就是 vue，替换等于原样。实测 `index.mjs` 与 `v3/index.mjs` **逐字哈希相同** |
| 3 | 「`esbuild` 的 postinstall 在下载二进制，关了 CI 的 build 会失败」 | **不成立** | 二进制来自 **optionalDependency**（`@esbuild/win32-x64`，已装）。`install.js` 的正常路径是 `require.resolve('@esbuild/win32-x64/bin/esbuild')`（行255）；下载只在 optional 包缺失时兜底，而它自己的报错文案（行259-262）明说「**用 `--no-optional` 才会走那条路**」 |
| 4 | 「`--ignore-scripts` 大概率可行」 | **✅ 实测：确实可行，且零影响** | 见下方实测 |

#### ✅ 实测已做（`probe_npm_ignore_scripts.py`）—— **零影响，可以加**

```
npm ci --ignore-scripts     退出码 0（34.8s）
npm run build               退出码 0（44.2s）
产物 vs 基线（25 个 .js）    逐字节相同，零差异
复原校验                    逐字节回到基线
```

**判据是产物 SHA256 逐文件比对，不是「build 成没成功」** ——
后者只覆盖构建那一刻，测不出「产物其实不一样」。

⚠ **它顺带证伪了上表第 3 条**：
`node_modules/@esbuild/win32-x64/esbuild.exe`（9.9 MB）在包里，
**不靠 postinstall 下载**；关掉脚本后 `esbuild --version` 仍输出 `0.21.5`。

**结论：可以给两条 `npm ci` 路径（`dependency_bootstrap.py:414` 与
`harness.yml:59`）加 `--ignore-scripts`。** 这正是 Pi「显式白名单」在我们树上的
等价物 —— 而实测说明**全禁就够，不需要白名单**。

**✅ 已实施**（守卫 `tests/test_npm_ignore_scripts_pinned.py`，变异实测通过）：

| 守卫 | 判据 | 变异实测 |
|---|---|---|
| `test_bootstrap_passes_the_flag` | 桩掉 `_run`、**真跑一次 `ensure_dependencies`**、断言捕获到的**真实 argv** | 去掉 flag → 红 |
| `test_ci_passes_the_flag` | 解析 `harness.yml` 的 `run:`（**不是 grep 整份文件**） | 去掉 flag → 红 |
| `test_no_new_lifecycle_script_dependency_slips_in` | lockfile 里带 install 脚本的包**恰好是已实测的 4 个** | 新增一个 → 红 |
| （对照）无效变异：只加注释痕迹、**不动真实代码** | —— | **保持绿** |

**第三条是防「第 2 种破坏」的**：将来新增一个带 install 脚本的依赖，
「关掉脚本安全」这个前提就需要重验 —— 那时这条守卫会**逼人重跑探针**，
而不是让人稀里糊涂继承一个只对 4 个包成立的安全前提。

⚠ **写守卫时踩了两个坑，都记在那个文件的注释里**：

1. **判据位置换过一次**：第一版桩 `subprocess.run`，结果 `ensure_dependencies`
   先要建 venv、建 venv 也要走 subprocess —— 一路桩下去链条长到
   「信号被噪声淹没」，最后红在一个**与要验的东西无关**的地方
   （`virtual environment is incomplete`）。改成桩 `_run`
   （`dependency_bootstrap.py:51`，就是那个接收 argv 并执行的函数）。
2. **桩必须模拟被桩对象的副作用**：只记 argv 不造文件，第 328 行
   `python.is_file()` 为假 → 同样红在无关处。桩里补上创建占位 venv 文件。

以及**我自己的无效变异**（记在 `mutate_npm_ignore_scripts_guard.py` 里）：
第一版那个「无效变异对照」写错了 —— 它把 flag 挪进注释的**同时真从 argv 删了它**，
守卫变红是**完全正确的**。若当时为了「让对照通过」去放宽守卫，
那才是把守卫改坏。**无效变异的形态是「表面有动作，行为没变」。**

##### ⚠ 这个探针自己有缺陷：3 条判据里 2 条是我编的路径

它报的 `**缺** esbuild 平台二进制` 与 `**缺** vue 入口` **都是假缺**：

| 判据 | 我写的路径 | 实际 |
|---|---|---|
| esbuild 二进制 | `@esbuild/win32-x64/bin/esbuild` | **`@esbuild/win32-x64/esbuild.exe`**（在包根，无 `bin/`） |
| vue 入口 | `vue/lib/index.mjs` | **`dist/vue.runtime.esm-bundler.js`** —— `vue` 根本没有 `lib/index.mjs` |
| vue-demi 入口 | `vue-demi/lib/index.mjs` | ✅ 对（因为它的 `package.json` 我读过） |

**编路径的代价是具体的**：那两条若被当成结论依据，就会得出
「关掉脚本会坏」—— **恰好是我第 3 次那个错判**。结论侥幸对了，
是因为它靠产物哈希比对，**不靠那两条**。

**教训**：「文件存在吗」这类判据，路径**必须从 `package.json` 的
`main`/`module`/`exports` 取，不能凭印象写**。
与 `docs/next-steps.md` §5 同源，但更具体：**判据本身可以是编的，
而它的读数看起来完全正常。**

##### 另一条：破坏性探针必须前置自检

第一版脚本崩在 `npm ci` 调用处 —— Windows 上 `npm` 是 `npm.cmd`，
`shell=False` 的 `subprocess` 不解析它。

**崩在破坏性操作之前纯属侥幸**：`web/assets/` 在 `.gitignore` 里
（**没有 git 兜底**），若那个 `npm ci` 调通而崩在下一步，
就得手工复原一棵无版本控制的构建产物树。

所以加了 `preflight()`（`npm --version` + lockfile 存在性）并**接到流程最前面**：

> **破坏性探针的第一步必须是「确认工具链可用」，而不是「开始破坏」。**

**实验与复原在同一脚本内**（`finally` 语义），不依赖操作者记得跑复原。

**探针 `tools/mutation/probe_supply_chain_scripts.py` 的判据一
（`lib/index.mjs` 哈希）只能证明 postinstall **跑过**，不能证明「不跑就坏」** ——
那正是恒真判据的典型形态。已在探针输出里写明这条边界。

**教训（与 `docs/next-steps.md` §5 那四条同源）**：
我读了三处源码就下了四次结论，而**一次都没跑过 npm**。
「esbuild 要下载二进制」这个判断听起来非常合理 —— 它**看起来就该成立**，
所以更容易被当成事实写下来。**先跑，再写。**

⚠ **但核实出一条 Pi 清单里没有、我们独有的洞：Python 那半边会跑 lifecycle 脚本。**

`dependency_bootstrap.py:394`：

```python
_run([str(pip), "install", "--upgrade", "-r", str(requirements)], project,
     label="同步 Python 依赖")
```

**没有 `--only-binary` / `--require-hashes` / 任何 source 过滤**，而
`requirements.txt` 里**没有一行是钉死的**：70 行中 42 行有效 ——
**裸包名 26 条**（`openai`、`chromadb`、`fastapi`…）、
**带范围 16 条**（`websockets>=12,<17`、`pytest>=9.1,<10`…）、
**`==` 精确 0 条、`--hash` 0 条**。所以：

* ⚠ **更正一（我先写「每次启动都解析最新版」，不成立）**：
  `dependency_bootstrap.py:331` 有
  `python_changed = ... previous.get("requirements_hash") != req_hash`
  —— **`requirements.txt` 的 SHA-256 没变就不跑 pip**。
  所以不是「每次启动」，而是「**清单一改，下次启动就重解析**」。
  触发频率远低于我原先的说法，**但那正是最坏情况**：
  「改了清单」与「跑供应链」是同一个动作。
* 也就是说 Pi 的 `min-release-age` 想防的场景（解析到当天发布的新版本）
  在 Python 侧**完全无防护** —— 因为**只要真跑，就必然去解析范围最新版**。
  `min-release-age` 若照抄，**恰好能覆盖这一条**（npm 侧同理）。
* sdist 安装会执行 `setup.py` —— **任意代码执行**，而 npm 侧我们用
  `--ignore-scripts` 关掉了等价的东西
* 包名可被劫持（依赖混淆 / typosquatting）而版本约束护不住 —— 16 条带范围的
  至少限了大版本，**26 条裸包名的连那个都没有**

**这与 §1.4 的 npm 硬化不是同一个问题**，但严重度更高：npm 侧我们知道要关脚本，
Python 侧连「关脚本」这个概念都没有。

#### 「暴露面」分析 —— 为什么 npm 那两条差距其实不大（两次更正后的结论）

我先判「npm 无 `min-release-age` + 未钉精确版本 = 两处弱」，**两次都高估了**：

1. **「每次启动都解析最新版」→ 不成立**（见上，`requirements_hash` 门控）
2. **「`^` 没钉 = 会漂移」→ 大体不成立** —— `package-lock.json`（130 KB）
   钉住了实际解析结果，而 `dependency_bootstrap.py:414` 与 CI 都用 **`npm ci`**
   （`npm ci` 严格按 lockfile 装，不重新解析范围）。所以 `^` 只在
   **有人主动改 `package.json`** 时才产生漂移，而那正是人工审查的时刻。

**真正的暴露窗口只有一个**：改完 `package.json`、跑 `npm install` 解析新版本，
到那次解析发生为止 —— 这段时间里 `min-release-age` 才有意义。
**这是一个窄窗口，不是常态。**（若把 `--ignore-scripts` 也算上，
那个窗口里唯一还能出事的就是**装上包的代码本身**，而不是它的 install 脚本。）

⚠ **所以两条更正之后的判断是**：npm 侧**不值得**照抄 Pi 那七条
（除 `--ignore-scripts` 我们已经做得更严）。**Python 侧才是真洞。**

**代价要说清**：修 Python 侧不是加个 flag —— `--require-hashes` 要求
`requirements.txt` 每行带 sha256（**42 行有效行全部要重生成**，且以后每次加
依赖都要更新），`--only-binary=:all:` 会**排除掉一部分只有 sdist 的包**。
属供应链契约变更，且会让「加个依赖」这件事变重。

### 1.5 遥测 conformance tests（核实）

`packages/telemetry/README.md` —— 核实**不是**「给遥测写了测试」，而是
**一套给适配器用的、与 runner 无关的一致性套件**：

> `@earendil-works/pi-telemetry/testing` exports a **runner-independent conformance
> suite** modeled as grouped cases. A fixture supplies a fresh context and converts
> its backend's finished spans into normalized `RecordedTelemetrySpan` snapshots.

套件覆盖：同步单次准入、返回值与 rejection 的**同一性**、自动与显式状态、
属性合并、事件顺序、settle 后调用惰性、嵌套与并发的父子关系、
**不可读的遥测负载失败必须被吞掉**。

还有一条对我们重要的约束：

> Recording it **must not change** whether the account load runs, succeeds, fails,
> or is persisted.

即**插桩不得改变业务结果**。

---

## 2. Hermes —— `NousResearch/hermes-agent`

来源：`README.md`、`docs/user-guide/security`

### 2.1 它**有**审批（核实，旧记录漏了这条）

README 的文档索引里 Security 一行写着 "Command approval, DM pairing, container
isolation" —— 核实正文确认：安全模型**八层**，第二层就是
`Dangerous command approval — human-in-the-loop for destructive operations`。

三种模式（`approvals.mode`）：

| 模式 | 行为 |
|---|---|
| `smart`（默认） | **用一个辅助 LLM 评估风险**。低风险自动放行（仅对该条），真危险的自动拒，**不确定的上浮为人工询问** |
| `manual` | 危险命令一律问用户 |
| `off` | 全关（等于 `--yolo`） |

**`smart` 模式的三值判定与我们的三值判定是同一个形状** —— 它也把「拿不准」单独
分出来上浮人工。我们花了 17 笔才做出 `_WRITE_INTENT_UNCERTAIN`，它是默认行为。

### 2.2 非交互上下文默认 `deny`（核实）

> `cron_mode: deny` / `single_query_mode: deny` / `unattended_mode: deny`

原文解释：

> These surfaces have **no human who can answer/approve**, so instead of blocking
> for the full approval timeout, **deny blocks the command instantly**.

**这正是我们的 `_intent_for_delegated()` 要求父持有租约**（`fa81aa3`）。独立得出了
同一条规则 —— 没有人能在场批准时，不要等，直接拒。

### 2.3 超时 fail-closed（核实）

> `timeout: 300` ... If no response is given within the timeout, the command is
> **denied by default (fail-closed)**.

并且：过期的询问**不可重开**，且

> A timeout is **not** counted as a denial, so asking again is never penalized.

我们的租约 TTL 夹在 300/900s，**同一个量级**（独立撞上，值得注意）。

### 2.4 硬地板在 `--yolo` 之下（核实）

`Hardline Blocklist (Always-On Floor)`：`rm -rf /`、fork bomb、`mkfs`、`dd if=/dev/zero`、
把不可信 URL 管给 rootfs 的 shell —— 这些**在审批层之前**就拦，
`--yolo` / `mode: off` / cron 的 headless approve / 用户点「always」**都绕不过**，
且**没有 override flag**。

另有一条我们会喜欢的**解析失败即拒**：

> The floor also **fails closed** on a command whose shell quoting cannot be parsed.

### 2.5 **写守卫被明说不是硬边界**（核实）

> ### Defense-in-depth, not a hard boundary
>
> Write guards apply to `write_file` and `patch` only ... **The terminal tool runs as
> the same OS user and can still `cat` or overwrite denied paths via shell commands.**
> The denylist reduces accidental damage and gives models a clear stop signal; **it
> does not sandbox a hostile or compromised agent.**

**与 Pi 那句话是同一个判断，只是更具体。** 写路径 denylist + 可选 safe-root 沙箱
（`HERMES_WRITE_SAFE_ROOT`）是**减少事故**，不是**防敌手**。

顺带一条它比我们细的：`~/.ssh/config` 走**审批**而不是硬拦（改 host 别名是例行动作，
但它能携带 `ProxyCommand`/`Match exec`，所以写入**从不静默**），而私钥与
`authorized_keys` 硬拦。

### 2.6 容器后端**跳过**危险命令检查（核实）

> When running in docker, singularity, modal, daytona, or vercel_sandbox backends,
> **dangerous command checks are skipped because the container itself is the security boundary.**

七种后端的隔离强度表里，`local` = `None — runs on host`，其余六个容器/远程后端
才有隔离。

### 2.7 它也撞上了我们同一个语言学难题（核实，最有价值的一条）

Context File Injection Protection 一节末尾，原文：

> The translation-and-execution check **does not connect translation and execution
> verbs across unrelated comma-separated role prose**. ... **These patterns are
> heuristics, not semantic intent detection.**

**「不要跨无关的逗号并列散文连接两个动词」** —— 这正是 `docs/write-intent-corpus.md`
A/B/D 三节反复记录的那个问题（否定只取消它自己管的那个动词、谓语动词缺口、
`不必X只要X`）。一个独立 harness 在**完全不同的功能域**上写下了同一条边界。

### 2.8 从历史审批里挖 allowlist 建议（核实）

`hermes approvals suggest`：扫会话库里**真正执行过**的危险命令，聚合成模式提议。
安全规则三条：默认只读、**破坏类永不提议**（`rm -rf build/` 批准 100 次也永不产出
`rm` 条目）、凭据在打印与 JSON 里都掩码且**掩码过的文本永不作为 allowlist 模式**。

### 2.9 其他核实项

- **FTS5 会话检索**：`FTS5 session search with LLM summarization for cross-session recall` ✅
- **zero-context-cost turn**：`Write Python scripts that call tools via RPC, collapsing
  multi-step pipelines into zero-context-cost turns` ✅
- **七种终端后端**：local / Docker / SSH / Singularity / Modal / Daytona / Vercel Sandbox ✅
- **interrupt-and-redirect** ✅ —— 详见 §2.10（**三种模式**，不只是「有」）

### 2.10 interrupt-and-redirect：**三种模式，不是一个开关**（核实）

来源：`docs/user-guide/cli`，"Redirecting the Agent Mid-Turn" 一节。
配置键 `display.busy_input_mode`：

| 模式 | 原文行为 |
|---|---|
| `"interrupt"`（默认） | "Your message **redirects the active turn**. Model generation restarts with **displayed reasoning and completed work preserved**. A running foreground terminal command is **moved to the background (not killed** — you get a completion notification**) so your message is read immediately; other running tools finish first" |
| `"queue"` | "Your message is **silently queued** and sent as the next turn after the agent finishes" |
| `"steer"` | "Your message is **injected into the current run** via `/steer`, arriving at the agent **after the next tool call** — **no interrupt, no new turn**" |

⚠ **一条容易漏的约束**（原文单独一行）：

> "Completed tool work and reasoning already shown stay in context"
> "**A running tool reaches its safe boundary before the correction is applied**"

即：**中断不是立即杀掉在途工具**，而是等它到安全边界。这与「`rm -rf` 跑到一半被砍」
是两件不同的事 —— 前者保数据完整性，后者留半个文件。

`Ctrl+C` 是另一件事（"interrupt the current operation, press twice within 2s to force exit"），
与 busy-input 的 `interrupt` 模式不是同一个东西。

#### 我们的现状（核实，**与 Hermes 三种模式都不对应**）

| Hermes | 我们 | 核实依据 |
|---|---|---|
| `interrupt`：消息**重定向当前轮**，已展示的推理与已完成工作保留 | **没有**。运行时 `canSend = !sending.value`（`frontend/src/ask/AskApp.vue:156`）→ **发送按钮禁用**，用户中途**根本无法输入** | 源码 |
| `queue`：静默排队，下一轮送达 | **没有排队概念** —— 用户不发送，就没有待发消息 | 全仓无 pending-user 队列 |
| `steer`：注入当前 run，下一个工具调用后到达 | **没有** | `agent.py` 无注入点 |
| `Ctrl+C`：中断当前操作 | ✅ **有，但语义不同** —— 前端 `stop()` 走 `abortCtl?.abort()`（`AskApp.vue:556`）→ SSE 断连 → `api.py:2512` `worker_stop.set()` → `agent.py:4272` 抛 `GeneratorExit` → **整轮作废** | 见下 |

⚠ **我们那条不是「转向」，是「停止」** —— 这是必须说清的差别：

* `worker_stop` 在 `api.py:2428` **局部创建**，`set()` 只出现在 `2512`（客户端断连的 `finally`），
  **没有任何 HTTP/WS 端点能触它** → 「停止」是靠**断开连接**实现的，不是独立控制指令。
* `GeneratorExit` 走 `agent.py:4076` 的 `aborted` 分支，`4101` 按
  "请求停止或客户端断连" 记账 —— **该轮不续跑**。
* 已完成的工具调用结果与恢复现场**保留**（`4129` `aborted=aborted` 落 trace），
  这一点与 Hermes 的 "completed work preserved" 一致。
* 那条 websocket 的 `cancel`（`api.py:2984`）是**语音抢话**（打断 TTS / provider 生成），
  **不碰 `run()` 的生成器**，与文本改向是两件事。

**所以缺口是一条**：用户在 agent 干活时**想说「等下，改成 X」**，必须先点停止、
丢掉当前轮、再重发。Hermes 的 `interrupt` 保留了已完成工作与推理，
我们**整轮作废**。

⚠ **但这条缺口的成本比看上去高**：它不是加个按钮，而是要给
`run()` 加一条**注入路径**（消息进当前 run 的哪个点）+ 定清
「已在途的工具调用怎么办」（Hermes 明确选「到安全边界」）+
前端解开 `canSend` 后**并发两条 SSE 怎么归属会话**。
属生产代码改动，且要改模型可见的输入。**不在调研收尾里顺手做。**
- SSRF 防护：拦 RFC1918 / loopback / link-local / CGNAT / **云元数据地址**
  （含 `169.254.169.254` 与 `metadata.google.internal`），**DNS 失败按拦处理**
  （fail-closed），**重定向每一跳都重新校验**
- 环境变量过滤：`execute_code` 按名字含 `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD|AUTH` 拦
- MCP 子进程只透传 `PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR` + `XDG_*`

---

## 3. opencode —— `anomalyco/opencode`

来源：`packages/codemode/README.md`、`packages/http-recorder/README.md`

### 3.1 codemode：**确认尚未发布**（核实）

> The package is currently **private to this workspace**.

安装方式是 `"@opencode-ai/codemode": "workspace:*"`。**外部装不了。**
旧记录写「README 明写尚未发布」——方向对，措辞不够准。

### 3.2 codemode 的价值是「越权在语法上不可表达」（核实）

> CodeMode lets a model write a small JavaScript program that can call **only the
> tools supplied by the host**, ... **without receiving ambient filesystem,
> process, network, module, or application authority.**

> **A program cannot gain authority through prose or generated code.** It can only
> exercise authority already present in the supplied tools.
> **Do not expose a broad tool and expect the prompt to restrict it.**

那句 "Do not expose a broad tool and expect the prompt to restrict it" 是对
「用提示词/意图推断去限制一个宽工具」的直接否定 —— **与 Pi 那句同向**。

机制上它靠**不用 `eval`** 的受限 JS 子集解释器 + 三个限额
（`timeoutMs` / `maxToolCalls` / `maxOutputBytes`）。

### 3.3 **codemode 不提供授权**（核实，旧记录漏了这条）

`Non-Goals` 一节原文包含：

> - **Generic permission prompts or approval workflows.**
> - **Application authorization or product policy.**
> - Durable pause/resume, replay, or storage adapters.
> - Exactly-once external side effects.

`Authority Boundary` 一节：

> **The host owns:** Authentication and authorization. Tool selection and immutable
> scope. Credentials and network clients. Persistence, idempotency, **approval**, and
> durable side effects. Logging and redaction policy.

**所以 codemode 不是我们租约/审批的替代品。** 它是一个**可借鉴的边界技术**
（把限制下沉到执行层，而不是靠提示词），授权仍要宿主自建。

### 3.4 **三个执行限额默认全是「无」**（核实，这条容易漏）

> No limit has a default, on purpose: execution budgets are **host policy, not
> library policy**.

并警告：

> A host with neither should set `maxOutputBytes`, or **oversized results silently
> flood model context**.

**「静默淹没上下文」** —— 正是我们处理过多次的「失败时没有声音」那一族。

### 3.5 工具目录有 token 预算与检索（核实）

`Discovery` 一节：目录默认 **2000 estimated tokens**（`characters / 4`），
按命名空间**轮转**挑选以保证公平，并**明说目录是否完整**
（`COMPLETE list` vs `PARTIAL - N of M shown`）；配 `tools.$codemode.search`
做确定性字段加权检索（路径精确 20 / 路径子串 8 / 描述子串 4 / 可搜文本 2）。

**这一条对我们直接相关，但我们已经有等价物了。** 核实后发现
`agent.py:690-698` 早就实现了同一思路，`DOCMIND_TOOL_EXPANSION=progressive`
**默认开启**：

* `CORE_TOOL_NAMES` 14 个核心工具给完整用法
* 其余进 `【扩展工具索引】`，只给名称 + 42 字概要（`_short_desc` 的 `limit=42`）
* 需要时用 `tool_search(工具名)` 取回完整用法
* 注释里明写「**参考 Claude Code 的同名设计**」

所以**不需要按 codemode 再建一套目录**。实测 `SYSTEM_PROMPT` 16442 字符
（≈4110 tokens）：核心 14 + 索引 83。

**但那条机制漏了 22 个工具** —— 它们从未被写进 `_SYSTEM_PROMPT_FULL`，
既不在核心也不在索引，模型**完全不知道它们存在**（`dev_patch` 出现 0 次）。
已修，见 `docs/known-defect-tool-visibility-and-dual-text.md`。

**反方向也不适用**：把两级目录接进提示是**降级** ——
`SYSTEM_PROMPT` 里 96 个工具的说明是**独立手写**的（长度中位数 60 字符），
`ToolSpec.description` 中位数 163 字符。手写版是刻意精简的，不是 desc 的复制。

### 3.6 核实：三家里**没有一家**按问题相关性挑工具（2026-10-08 追加）

这是查 opencode 的 `Discovery` 时顺带发现的，比上面那条更根本：

| harness | 怎么决定暴露什么 |
|---|---|
| **Pi** | **不涉及工具选择** —— 它的安全模型是「进程边界」而非「工具边界」。`security.md` 通篇讲文件/凭据/进程/网络的可达性与三种隔离运行方式；**没有任何按问题挑工具的机制** |
| **Hermes** | **用户/平台显式 toolsets**（`hermes tools`、`--toolsets "web,terminal"`），另有平台 preset 与动态 `mcp-<server>` 工具集。**零自动召回** |
| **opencode** | **预算截断 + 轮转公平 + 检索**，**无相关性打分** |

opencode 原文：

> **Every tool namespace is always listed** with its tool count regardless of
> budget. Selection is **round-robin** across namespaces for fairness.

我们此前的 `ContextRouter` 关键词判定（`_CODE` / `_WEB`）是**三家里唯一的异类**，
也是唯一失效的那个（动词型请求「跑一下测试」一个都不命中）。
详见 `docs/known-defect-context-router-recall.md`。

> ⚠ **更正**：本节早前写「Pi 有 4 个工具（read/write/edit/bash）」——
> **核实不到**。`security.md` 与 `README.md` 都没写工具数量，那个数字来自第三方
> 站点（搜索摘要）。已删掉 —— **不拿二手来源的数字当事实**。上表改为只陈述
> 一手来源里确实写了的机制。

### 3.7 `http-recorder`：**Public beta，且真的能用**（核实）

> **Public beta.** The API depends on Effect 4 beta and may change.
> Install: `bun add -d @opencode-ai/http-recorder@beta`

机制：本地首次跑**打真实 API 并录成 cassette**，之后确定性重放。

> When **`CI=true`, missing cassettes fail instead of recording.**

三条设计值得抄：

1. **没有覆盖模式，只有删除后重录**：
   > There is intentionally **no public overwrite mode**. Deletion makes the set of
   > recordings being refreshed **visible and reviewable**.
2. **落盘前扫凭据，不安全就拒写**：
   > Before writing, the recorder scans the complete cassette for common credential
   > formats ... **Unsafe cassettes fail without replacing an existing recording.**
3. **明确 redaction 不能替代人看**：
   > Redaction is defense in depth, **not a substitute for review**.

**限制（README 自陈）**：响应在录制与重放时都被缓冲，所以**不适合断言流式时序、
取消或背压**；WebSocket V1 不复现终端 close code 与传输失败。

**对我们的用处**：治 `docs/known-defect-two-flaky-network-tests.md` 那两条随机红。
把三后端搜索的真实响应录一次、提交 cassette、之后重放 ——
**注意它只治上游 HTTP 依赖，治不了「探针脱离 pytest 看见缓存」那类**。

---

## 4. 对我们的方向（2026-10-08 按实测结果重写）

> 本节早前是一张「候选清单」，但**没跟上已完成的改动**。现在改成
> 「已完成 / 待做（带前置与代价）/ 已否决」三段，每项都能核到提交或实测。

### 4.1 已完成（可核到提交）

| 做了什么 | 依据 | 提交 |
|---|---|---|
| **工具说明不再教模型写死端口** | codemode `Discovery` 的「按需披露」 | `5cbe94b` |
| **端口归属判据 `owns_port`** | `dev_server.py:36` 那条注释引用的符号，此前**从未定义** | `5cbe94b` |
| **22 个工具从未被披露** | 逐条核实 `ToolSpec.description` vs `SYSTEM_PROMPT` | `bbc0207` |
| **索引段明示完整度 + 本轮可调更少** | codemode：`The instructions state exactly how comprehensive the list is` | `59c1cda` |
| **撤除按关键词收窄工具组** | 三家都不按措辞挑工具（见 §3.6） | `e8db3ea` |
| **外网开关成为唯一闸门** | 用户显式意图不该被关键词再猜一遍 | `e8db3ea` |
| **删除失效的「检索即放行」** | 撤除后实测 8 种场景零变化 | `393b5e1` |
| **两条随机红的取证缺口** | http-recorder 的 cassette 思路（仅取证那一半） | `7526f4e` |

### 4.2 待做（按性价比，含前置与代价）

#### ① 给适配器边界加 conformance suite —— **需要先回答「解决什么」**

依据：Pi `pi-telemetry` 的 `createTelemetryAdapterConformance()` 是
**runner-independent 的一致性套件**，fixture 提供一个适配器与快照读取器，
套件检查「同步单次准入 / 返回值与 rejection 的**同一性** / 属性合并 /
settle 后调用惰性 / 不可读遥测失败必须被吞掉」等**可观测语义**。

**我们有适配器边界**：`probe` / `owns_port`、preview adapter、MCP connector。

⚠ **但「加一套 conformance 测试」本身不是目标。** 必须先说清要解决什么：

| 候选问题 | 是否值得 |
|---|---|
| 有多个实现同一契约、行为已经漂移 | 那才值得 —— 但得先有**第二个实现** |
| 只是想让测试更整齐 | **不值得** —— 那是在给一个没有问题的边界加仪式 |

**核实结果：目前没有第二个实现。** `mcp_bridge` 里 `transport` 只出现 4 处，
而且**不是两条实现** —— 是同一份代码把 transport 记成 `entry["http"]` 这个布尔字段，
再据此打不同的 `group` 标签（`group = "web" if entry.get("http") else "mcp"`）。
`discover` / `attach` / `build_specs` 都**只有一份**。

**结论：暂不做。** 等真出现第二个实现（或第三个 transport）时再用这个模式。
`test_web_fetch_stub_completeness.py` 已经是这个思路的一个实例 ——
**结构性扫桩**而非逐条打桩，那条已落地。

#### ② cassette 治上游 HTTP 依赖 —— **只对仍未修的那条有意义**

依据：http-recorder 三条可抄的设计：
1. `CI=true` 时**缺 cassette 直接失败**，不录制
2. **没有覆盖模式**，只能删除后重录 —— 让刷新范围在 diff 里可见
3. 落盘前扫凭据，**不安全就拒写**

⚠ **但我们最需要它的那条已经修好了** —— `test_web_fetch` 那条随机红
根因是 `_baidu_search` 没打桩，`9362e65` 已补，并加了结构性扫桩守卫
（连跑 5 次全绿）。**所以「治两条随机红」这个目标已经不存在。**

剩下的 `ERR_CONNECTION_RESET` 是**前端字体 CDN**（已定性），不是 HTTP 依赖问题。

**结论：暂不做，除非将来出现新的「上游 HTTP 抖动导致随机红」。**

#### ③ 写路径 denylist + safe-root —— **真缺口，已立档**

依据：Hermes 有 `HERMES_WRITE_SAFE_ROOT`（可选沙箱，越界即硬拦）
与一份**永远阻塞**的 denylist（`~/.ssh/`、`~/.aws/`、`/etc/sudoers` …），
且明说「写守卫是纵深防御**不是**硬边界」。

**核实我们的现状**（探针 `tools/mutation/probe_write_denylist_gap.py`，可复算）：

```
项目内写 .env                -> 放行
项目内写 .ssh/id_rsa         -> 放行
code_root=$HOME 时写 ~/.ssh/id_rsa -> 放行     <- 真敞口
```

`tools.py` 里 `.env` 只出现 1 次，且是在 `game_release_check` 的 description 里
（功能描述，不是保护）；那 2 处 `BLOCKED` 是 `_BLOCKED_CMDS`（shell 命令黑名单）。

**所以我们完全没有凭据路径保护**，而项目内的 `.env` 是最常见的凭据存放处。
Hermes 对它是「**写可、读不回**」。

**结论：值得做，但要单独立项。** 完整实测、判据选项与
「哪些是敞口 / 哪些只是设计债」见
**`docs/known-defect-no-write-path-floor.md`**（含我三次自我更正 ——
初判的差距比实测大得多）。

### 4.3 已否决（带依据，别重做）

| 方向 | 否决依据 |
|---|---|
| **文本算法修召回** | 实测：`_TERM_SPLIT` 切词 **1/12**、n-gram **5/12**。且「高铁」压根不在 `web_search` 描述里 —— **任何文本重合算法都救不了**。探针留在 `tools/mutation/probe_recall_*.py` |
| **把两级目录接进提示** | `SYSTEM_PROMPT` 里 96 个工具说明是**手写**的（中位数 **60** 字符），`ToolSpec.description` 中位数 **163** —— 换是**降级** |
| **录放批准状态** | 根因是词表逐条消息判定；且结构化授权（租约）已存在。见 `write-intent-corpus.md` J1 |
| **零上下文成本那半边**（Python 脚本调工具） | 需 `exec` + 受限 builtins，比 acorn 静态解析更不安全；且 codemode `Non-Goals` 把授权划给宿主 |
| **`strictPort: true`** | 会让 `parse_port_hint` 的**存在意义消失**（它存在的唯一理由是「从含死地址的日志里认活的」）。见 `known-defect-dev-server-port-ownership.md` §8.5 |
| **`owns_port` 接生产（在注入身份之前）** | 页面里没有能区分项目的指纹 —— 三个入口 title 全以 `DocMind` 开头，而 worktree 跑的是同一份代码。见 `known-defect-port-ownership-has-no-fingerprint.md` |
| **Claude Code 泄露源码** | 纪律 |

### 4.4 还没转化为改动的观察

这些**核实过但没做**，记在这里免得重新查一遍：

| 观察 | 出处 | 为什么没做 |
|---|---|---|
| **辅助 LLM 判风险 + 三值上浮** | Hermes `approvals.mode: smart` | 我们已有三值判定（`_WRITE_INTENT_UNCERTAIN`），**但判据是词表不是 LLM**。换 LLM 是成本问题不是设计问题 |
| **非交互上下文默认 `deny`** | Hermes `cron_mode/single_query_mode/unattended_mode` | 我们已有等价物（`_intent_for_delegated` 要求父租约），独立得出同一条规则 |
| **超时 fail-closed** | Hermes `approvals.timeout: 300` 默认拒绝 | 我们的租约 TTL 是 300/900s，同量级 |
| **硬地板在 `--yolo` 之下** | Hermes hardline blocklist | 我们的 `run_command` 有黑名单，但**没有「不可绕过」这一层** —— 待确认是否需要 |
| **从历史审批挖 allowlist 建议** | Hermes `hermes approvals suggest` | 有意思但依赖跨会话审批数据库，我们没有 |
| **子集化的字体** | Hermes/Pi 未涉及，是我们前端在做 | 前端域 |

---

## 5. 对旧记录的更正

| 旧记录（`next-steps.md` 第 3 节） | 核实后 |
|---|---|
| 「`telemetry` 带 conformance tests」 | ✅ 但**不是**给遥测写的测试，是**给适配器用的 runner-independent 一致性套件** |
| Hermes 只记了 FTS5 + zero-context-cost | ❌ **漏了它有完整审批系统**，且那才是对我们最有参考价值的部分 |
| 「codemode 的价值是越权在语法上不可表达」 | ✅ 但**漏了 `Non-Goals` 明确排除授权** —— 它不能替代我们的租约 |
| 「README 明写尚未发布」 | ✅ 更准：`currently private to this workspace` |
| 「`http-recorder` 能治『测试在说谎』」 | ⚠ **过宽**。它治上游 HTTP 依赖，治不了探针脱离 pytest 看见缓存那类 |

**还有一处是我自己当天写错的**：`docs/write-intent-corpus.md` J1 原先把
「已证不适用」归因到 http-recorder 名下 —— 那是两件不同的事（录**批准状态** vs 录
**上游 HTTP**）。已在该节更正。