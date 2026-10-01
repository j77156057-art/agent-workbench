# HANDOFF · Vibecoding 能力补全（harness 侧）

> 状态：**Wave1–9、11–17 已入库**（14 `dev_headcheck` 提交自洽 / 15 `dev_testrun` 跑测试 / 16 `dev_vue` 真实 .vue 组件守卫 / 17 组件守卫第二层「挂载前驱动共享状态」+ `dev_apptest` 真后端整页集成守卫）。
> 发出人：本会话（用户直接指派，不占 R0–R15 槽位）。最后更新：2026-10-01。
>
> **这份文档是什么**：让没跟过这个过程的人（或另一个 AI）能直接接手**操作**这套 harness 的
> 开发闭环，并知道每条能力的**边界在哪里**、**被哪条测试守着**、**哪些还只是本地提交**。
> 它不复述 `HANDOFF.md` 的流水（那是 135 段的按日追加日志，含其它 lane 的内容）。
>
> **这份文档不是什么**：不是架构说明（看 `docs/agent-architecture-review.md`）、不是实时链路
> （R 系列）的任务表（看 `docs/realtime-*.md` 与 `HANDOFF.md` 的 R 段）。

---

## 1. 一分钟版：闭环每一段的入口

"vibecoding" 指的是让 Agent 自己完成 **找 → 读 → 改 → 跑 → 看 → 交** 这一圈。以前它缺腿，
现在每一段都有明确入口（工具名即 `tools.py::TOOLS` 的键）：

| 环节 | 入口 | 一句话边界 |
|---|---|---|
| 找文件 | `dev_glob` | 按名字/通配找，不用一层层 `list_dir` |
| 读代码 | `dev_find_references` / `python_exec` | TS/JS/Vue 走 tree-sitter，注释与字符串不误报 |
| 问历史 | `dev_git_log` / `dev_git_diff` | **只读**白名单：`rev-parse/status/diff/log` |
| 改代码 | `dev_patch`（原子统一差异）/ `dev_move`（走 `git mv`） | 全有或全无；拒绝在补丁里删文件 |
| 跑起来 | `dev_serve`（起/停本机 dev server）| 只允许回环；命令过同一道黑名单 |
| 跑测试 | `dev_testrun`（`paths` / `keyword` / `only_failed` / `related`）| 退出码 5 = 一个都没收集到，判**未通过**；覆盖率未启用（没装 coverage）|
| 看结果 | `preview_project`（截图 + console + 失败请求）/ `dev_page_action`（点/填/按键/等待）| **no_change 不是成功**；只回环 |
| 守组件 | `dev_vue`（真实 .vue 挂进真浏览器：props → 真点击 → 收 emit；第二层可**挂载前驱动共享状态**）| **不写断言＝未通过**；只证明一个组件在一组 props 下（§5.6、§5.7） |
| 跑整页 | `dev_apptest`（起真 `uvicorn api:app` + 真 `web/` 产物 + 真浏览器）| **旧构建判未通过**（`web/` 比 `frontend/src` 新才算数）；不点真模型、不听真网关（§5.8） |
| 验语音/摄像头 | `dev_media`（`build` / `run` / `voice`）| 假设备喂合成媒体；门限行为见 §5.3 |
| 静态检查 | `dev_diagnostics`（ruff + vue-tsc）| 退出码非 0 却解析不出发现时**报错误不报干净** |
| 交付 | `dev_propose`（补丁+草稿，写进状态目录）/ `dev_ci_status`（只读 GET）| 不 add/commit/push；CI 读不到就明说 |
| 提交自洽 | `dev_headcheck`（把 HEAD 挂成干净检出真 import 一遍）| 只读 + `worktree --detach`；抓的是"工作树能跑、单独检出必红"，见 §5.4 |
| 多人防撞 | `dev_lanes`（TTL 认领登记表 + 可选 worktree）| 冲突默认拒绝，`force: true` 才共管 |
| 对外出口 | `mcp_server`（harness 自己是 MCP server）/ `mcp_bridge`（连接器工具一等公民化）| 默认只读；写要 `--allow-writes`；`run_command` 等永不暴露 |
| 能力回归 | `dev_eval`（22 题门禁 + 基线 + CI 步骤）| 测的是 harness，不是模型智商 |

## 2. 环境事实（会咬人，先读）

- **解释器**：`.venv/Scripts/python.exe`。仓库脚本都按"从项目根目录运行"写。
- **pytest 必须带项目内 basetemp**：`--basetemp=.tmp/pt-xxx`。用户名含引号会让默认 `pytest-of-*` 目录 `PermissionError`。
- **推送被沙箱拦**：`git ls-remote` 可通，`git push` 到 github:443 不通。**所有提交都只在本地**，
  需要人在自己机器上推。要离线备份用 `git bundle`。
- **共享工作树**：多个 AI 同时在改同一棵树（实测一次全量里 12 条红全来自别人未提交的 WIP）。
  规矩见 §7。
- **前端工具链**：node 24（本机）/ CI node "22"；`frontend/node_modules` 里有 esbuild 0.21.5 与
  vue-tsc，`npm run test:node` 走 node 的类型剥离。**没有 vitest**（见 §6）。
- **浏览器**：真实 Edge（`_edge_binary()` 找 `msedge.exe`，可用 `DOCMIND_EDGE_BIN` 覆盖）。
  任何"看一眼界面"的代码**必须**走 `visual_acceptance.browser_session()`：它把浏览器生在
  kill-on-close 作业对象里（99°C 漏浏览器事故的修法，见 §9），**禁止自己 Popen、禁止 playwright**。

## 3. 怎么自己复跑验证

```bash
# 能力门禁（19 题，不联网、不需要浏览器；含基线对比）
.venv/Scripts/python.exe -m agent_runtime.dev_eval --self-check \
    --baseline .github/dev-eval-baseline.json --json
# 本程序新增的四套回归（138 项，其中少量需要 Edge / node）
.venv/Scripts/python.exe -m pytest tests/test_media_fixture.py tests/test_page_action.py \
    tests/test_voice_loop.py tests/test_frontend_node_tests.py tests/test_head_check.py \
    tests/test_test_run.py tests/test_vue_component.py -q --basetemp=.tmp/pt-manual
# 让工具自己跑一次组件守卫（真浏览器，5~20 秒）：
#   dev_vue(component: src/workbench/components/WorkflowMembersDock.vue,
#           props: {"active":true,"members":[{"id":"t1","label":"研究","role":"r",
#                    "status":"ok","task":"查资料"}]},
#           clicks: .wmd-strip, expect_present: .wmd-panel, counts: .wmd-item=1,
#           expect_emit: focus, expect_emit_args: ["t1"])
# 前端行为测试（29 项：node:test 跑生产 .ts）
cd frontend && npm run test:node
```

## 4. 三条不可退让的不变量（以及守住它们的测试）

1. **新工具必须注册在三处**：实现 + `TOOLS` + `agent.py` 的提示目录。少第三处，模型就调不到。
   守它的测试：`tests/test_native_tools.py`（registry/schema 数量一致）、各波自带的
   "registered in all three places" 用例。
2. **绝不报"干净/通过"给没真跑成的检查**（仓库里叫"假干净"，是最贵的一类）。已修过的形态：
   ruff 输出被截断→看起来 0 发现；`node --test` 空 glob→退出码 0；console 列表去重→重复报错
   看不见；`close` 找不到会话→仍打印"已回收"。守它：`tests/test_page_action.py`、
   `tests/test_frontend_node_tests.py`、`tests/test_media_fixture.py`。
3. **网络与命令的边界是白名单，不是"看起来危险就拦"**：浏览器只回环、MCP/页面只回环、
   git 只读 + `git mv` 一处例外、命令黑名单与 `run_command` 共用。守它：`tests/test_safety_*.py`、
   `tests/test_mcp_server_expose.py`、`tests/test_page_action.py` 里的注入/越界用例。

## 5. 四条"真回路"能力现在能证明什么（也说不清什么）

### 5.1 `dev_page_action`（Wave13）
真实 CDP `Input.*`：`click` / `type` / `press` / `wait` / `read` / `locate`。会话持有浏览器
（上限 3、空闲 TTL 回收、`atexit` 兜底），`close` 回报**真实**回收证据。
- 断言的是"有没有真的发生"：`changed / navigated / reloaded / error_seen / no_change`，
  后两者一律 `ok=False`。
- 派发之前先拒：歧义（多命中没给 `nth`）、不可见、禁用、中心不在视口——**一个鼠标事件都不发**，
  并回候选清单。
- 不做：drag/hover/scroll/文件选择/对话框；iframe 与 Shadow DOM 定位不到；不共享登录态；
  每个动作后是"观察多久"（`settle`）而不是条件等待（那要用 `wait`）。

### 5.2 `dev_media action: run`（Wave7）
合成 WAV/Y4M 经 Chromium 假设备开关喂进真实 `getUserMedia`，回环 WS 探针按
`realtime_protocol.parse_binary_packet` 逐包判定，畸形包记成协议违规而不是丢掉。
- 实测锚点：440Hz、幅度 0.4 的正弦回到线上 RMS=0.2828（=0.4/√2），静音段**精确 0.0**，
  假设备会**循环播放**文件（所以 pattern 匹配用循环窗口）。
- 边界：探针页面**刻意不接能量门**，逐帧无条件推流；它证明"采集→WS→R0 线上"，不证明门限。

### 5.3 `dev_media action: voice`（Wave8 后半）
不装 vitest 的前提下验**接线行为**：用前端自带 esbuild 把生产模块编成 ESM 给页面 import
（**不是抄一份逻辑**），胶水照抄 `AutonomousCockpit.vue` 的 `handleLiveAudioFrame` /
`sendNativeAudioReady` / `startLiveMic`，再用假麦克风 + 真点击跑。
- 能量门限逻辑本身归 `frontend/tests/liveAudioControl.test.mjs`（node:test，17 项，已入库）。
- 实测形状（2026-10-01）：`lead=.×1（audio-ready 标记）+ T×7 + q×3 + .×12`，
  **尾后 2.69 秒不再发包**（证明不是无条件泵），抢话发出 cancel，探针按尾收口。
- 反证已跑：纯静音脚本 → 只有 hello.ok 后生产必发的**那 1 片** audio-ready 标记，
  零片响亮语音、永不收口 → 判定未通过。
- 一条实测事实要记住：`quiet` 段在**文件里** RMS≈0.0057 < `startRms` 0.02，但经浏览器 AEC/NS
  后能量被抬到足以开门 —— "低于门限"只对生成文件成立，别拿它在回路上断言"绝不开门"。
- 自查（独立审 + 逐条核实 + 变异电池 **16/16 抓红**）改掉的真问题：
  ① **截断在静音尾中间的无条件推流页面会全项通过** —— "尾后没再发包"以前只看列表末尾，
  现在必须"尾部之后又继续观察够 `RESUME_GAP_SECONDS`"才算 `stopped=yes`（`tail_stats`
  返回三态 `yes/no/not_observed`）；
  ② `sequence_monotonic` 对**单包**和**非整数 sequence** 都返回过 True（等于没判）；
  ③ 只看探针自己发的 `model.delta final` —— 现在页面 `serverEvents` 里必须也有它；
  ④ `connections` 从没进判定（开两条连接=重复起流）；
  ⑤ console 错误/失败请求没进 `no_page_errors`；⑥ `render()` 打印的是常量阈值而不是
  实际用的阈值；⑦ esbuild 返回 0 但产物是旧的/0 字节仍算编译成功（现在先删旧产物、再验长度）；
  ⑧ 胶水页在 `startMic` 里归零 `sequence`，与生产不一致 —— **真机跑出来才看见**：
  `hello.ok` 后的标记片和第一片语音撞同一个序号，`sequence_monotonic` 当场变红。
- 边界：`.vue` 的接线是**转写**的、不是 import 的；真网关（`api.py` + DashScope）仍归 R12 人跑。

### 5.4 `dev_headcheck`（Wave14）：把 HEAD 挂成干净检出真 import 一遍
共享工作树里有一类错**在工作树里永远看不见**：提交引用了没入库的文件（漏 `git add`，
或 `git commit -- <路径>` 绕过索引把别人的 WIP 一起提交掉）。本地测试全绿，CI / 别人的
`git worktree add` / 任何 clone 第一批 `import agent` 就红。所以必须回到"只有已提交内容"
的那棵树上跑。
- `ref` **先钉成 sha** 再用（HEAD 会被别人推着走，验过的和报告里的必须是同一个提交）；
  `git worktree add --detach` 到项目状态目录（仓外）；在**那棵树**里用 `sys.executable -c`
  真 import `config,tools,agent`；用完 `worktree remove` + `prune`。
- 失败归因分两种：**在磁盘但不在提交** = 别人未提交的新文件被提交引用（本仓 2026-10-01
  的 `agent_runtime/run_budget.py` 就是这个形状，第一次真跑就抓到了）；**两边都不在** =
  漏提交/改名/本该进 requirements。同时回引用点 `agent.py:55（import …）`。
- 三条由事故直接决定的守卫：空 `modules` 不许当通过（`all([])` 是 True）、子进程回
  `{"ok": "truthy字符串"}` 不许当通过（只认 `is True`）、**没有本工具的归属标记就绝不
  `worktree remove --force`**（不拆别人的检出）。变异电池 **14/14 抓红**。
- 电池暴露的两个"测试自己没牙"值得记住：① 非法 `ref` 只断言"被拒"是废话——删掉形状校验后
  git 自己也会报错，8 个用例照样全绿；改成断言**拒绝理由**必须是"形状不被接受"才有牙。
  ② 去掉 `--detach` 是**等价变异**（git 对非分支名本来就 detach），真正的风险是写成
  `-b <name>` 会在共享仓凭空占分支；补了"分支表 / HEAD / 工作树 / `worktree list` 都不许变"
  之后那条变异当场被抓住。
- 用法：**每次 commit 之后、push 之前跑一次**。边界：只验"能不能跑起来"，默认三个入口之外的
  深层 import 要靠 import 链真的走到才暴露；不跑测试、不装依赖、不验 requirements 完整性。

### 5.5 `dev_testrun`（Wave15）：跑测试 → 读失败 → 只重跑失败
`agent_runtime/test_run.py`。存在的理由是三个**实测**到的漏法：pytest 在 0 收集时退出码是 5
（"没看到 FAILED"不等于通过）、`-q` 的失败摘要最容易漏读、以及本工作树
`.pytest_cache` 是 `Permission denied` → `--lf` 根本不可用。
- 7 条判定全 fail-closed：`runner_ran` / `finished` / `tests_collected` / `summary_parsed` /
  `no_failures` / `counts_agree_with_exit`（摘要与退出码不自洽＝报告不可信）/ `rerun_record_kept`。
- 记账：失败 nodeid 写到项目状态目录 `testrun/last-failed.json`，`only_failed: true` 用它重跑；
  `total` 存的是**真实失败条数**（清单会被 200 条上限截断，看截后的清单长度就永远触发不了
  "整片红别逐条重跑"这条守卫——实测出来的）。
- `related: agent_runtime/x.py` 按**磁盘上的文件名**选相干测试。别用
  `--collect-only` 拿全仓 nodeid：实测本仓收集结果 243,591 字符，有界缓冲只留 8,022
  （`truncated=True`），大部分 nodeid 在截断里丢了，选择会退化成"什么都找不到"。
- 覆盖率：`coverage` 与 `pytest_cov` 都不在 `.venv` 里，`coverage: true` 直接回「未启用、
  缺依赖」且**不起任何进程**——那是【没测】，不是 0%。
- 变异电池 **18/18**，并抓到两条"测试自己没牙"：`tests_collected` 的三个信号原来被同一个
  用例同时设坏，删掉任一个都还绿；改成每个用例让另外两个信号说"绿"才抓得住。
- 边界：只报"跑到什么、哪里红了"，不判断根因；`--tb=line` 是一行式现场；记账是覆盖写，
  两条并发 `dev_testrun` 会互相顶掉。

### 5.6 `dev_vue`（Wave16）：把真实 `.vue` 组件挂进真浏览器
补的是最后一段没有守卫的接线：`voice_loop` 里的胶水是**转写**的，`.vue` 本体被改坏以前没人发现。
**零新依赖**——编译器是仓库里已有的 `@vue/compiler-sfc`（vue-tsc 带的，与 `vue` 同版本），
打包是前端自己的 `esbuild` 的 **JS API**，点击是 Wave13 的 `dev_page_action`。
- 一条命令能断言的东西：`props` → 渲染出什么（`expect_present` / `expect_absent` /
  `counts: .wmd-item=2` / `texts: 1/2`）→ 真点几下（`clicks: .wmd-strip, .wmd-item#1`，
  `#n` 是 nth）→ **emit 的名字与实参**（`expect_emit: focus` + `expect_emit_args: ["t2"]`）。
- 三件被实测决定的实现细节：① `compileScript` 的输出**仍是 TS**，esbuild 的 `loader` 必须跟
  SFC 的 `lang` 走（写死 `js` 会在 `interface` 那行炸）；② 页面要的 `createApp`/`h` 必须
  **从同一个 bundle 导出**，否则是两个 vue 实例；③ 产物目录必须在**前端树内**
  （`frontend/.tmp/vue-guard/`，命中 `.gitignore` 的 `.tmp/`）——裸说明符 `from 'vue'`
  是从入口目录向上找 `node_modules` 的。
- 判定 fail-closed：**不写任何断言＝未通过**（"挂上了"不是证据）；`expect_emit` 写了组件
  `defineEmits` 里没有的名字＝未通过（否则永远等不到也看不出来）；读状态那次点击抛错
  **单独**红（`state_read_back`），不然四条事实断言一起"莫名没成立"。
- 反证是真的：把组件**副本**（`frontend/.tmp/`，gitignore 内）的 `emit('focus', id)` 改成常量、
  或把 `v-if="visible"` 改成 `v-if="false"`，守卫当场红。变异电池 **22/22**，其中 5 条一开始
  活下来都是因为"只有现场类才走到"——于是把参数解析抽成 `parse_request()`、产物目录抽成
  `workdir_for()`、清理测试挪进纯层，才补上牙。
- 顺手修掉 Wave13 的一个真缺陷：`_target_spec` 把"没写 `nth`"与"`nth: 0`"都归成 0，
  于是多项命中时**怎么指定都点不到第一个**；现在 `nth_given` 区分两者（+2 回归用例）。
- 边界：只证明**这一个组件在这一组 props 下**的行为；不证明整页集成/路由/父组件传参，
  也不听真网关。依赖共享状态的组件见 §5.7（mount 包装已做），整页见 §5.8。

### 5.7 `dev_vue` 第二层：挂载前驱动**共享状态**（Wave17a）

- **为什么不是 provide/inject**：这条曾被写进边界当"以后再说"，实测把它纠正了——
  `frontend/src` 里 `provide(`/`inject(` **零命中**，本仓前端的组合式（`useWorkbench`）是
  **模块级单例**。所以 mount 包装的正确形状是"入口与组件同源 import 同一份 + 挂载前调它的方法"，
  不是造一个假 provider。
- 接口：`composable:` + `store_call:` + `setup: [{"call":..., "args":[...]}]`。名字校验分两级：
  `store_call` 与（没给 store_call 时的）setup 名按 `exported_names()` **静态**校验——扫不出任何导出
  时**拒执行**而不是返回空清单（返回空清单等于把所有名字判成"不存在"，看着安全实则把整类组件挡在门外）；
  给了 `store_call` 时方法名来自运行期才拿得到的对象，静态放行，但运行期取不到函数、抛错、
  **或返回 Promise** 都记成页面错误 → `setup_applied` / `no_component_errors` 判红。
  （异步方法同步确认不了，这条守卫因此把自己限在"同步可确认"的范围内。）
- **读状态改成非交互**：`ContextMenu.vue` 监听 window `mousedown`，任何一次点击都会把它自己关掉——
  实测"菜单点开了、读回来是空的"。现在页面自带 `MutationObserver` 盯 `#app`，`#state` 永远是当前
  状态，调用方只读不改。同时把 `rendered` 从"判决"降级为"报出来的事实"：菜单关着时组件什么都不渲染
  **是正确状态**，要断言"渲染出了什么"得写 `expect_present`（那才有牙）。
- 现场凭据：`store_call: useWorkbench` + `setup: openRootMenu({clientX:30,clientY:30})` 让
  `ContextMenu.vue` 真的长出 `.cm-menu` 且 `count:.cm-item=2`；不驱动时同一份断言必须判红（对照组）。
- 测试 **79 项**（67 纯 + 12 现场），变异电池 **37/37**（纯层 / 门禁 / 现场三层对照组）。
  其中一条变异反过来抓出我写的**假防御**：`setup_plan` 里的 `json.loads(json.dumps(args))`
  深拷贝加那条"改不动计划"的测试——入参本来就是 JSON 字符串，每次调用重新解析，别名根本不可能发生。
  代码与假测试一起删掉。

### 5.8 `dev_apptest`（Wave17b）：真后端 + 真构建产物 + 真浏览器整页

- **补什么**：`dev_vue` 的 props 是我喂进去的，整页要的是真的 `/api` 往返。做法：
  `free_port` → `sys.executable -m uvicorn api:app`（`DOCMIND_STATE_ROOT` 指到临时目录；
  实测冷启动就绪 4.7s，整轮含点击 8.5s）→ `POST /api/projects` 注册一个**只含本次夹具**的临时项目 →
  `GET /api/projects` **读回来核对** `current` 与清单 → 开 `/workbench` → 逐条判定 → 拆干净。
  进程生命周期不自己实现：服务走 `dev_server`（`process_runner` 的 job，Windows 上 born-in-job），
  浏览器走 `page_action`，产物回收复用 `vue_component.clean_target`。
- **凭据是"这一次独有的数据"**：夹具是一个随机命名的文件 `probe_<6位>.py`，要求它出现在文件树的
  文本里——随机名让"缓存/旧页面里恰好有这句话"冒充不了。
- 三条被实测纠正的形状（都是先写错、跑真页面才发现）：① `page_action` 的 `text:` 定位只在
  **可交互元素**（a/button/input/label/…）里找，文件树那行是 `<span>`，实测命中 0 个 → 改成读
  `.ft-body` 文本；② `locate` 的候选清单在 **64 个**就 `return`（`page_action.py:101`），
  实测切工作区前后 `#app *` 都报 64，html 长度又被 4000 字上限砍平 → 内容换没换改用 `main`
  正文**指纹**；③ `page_action` 禁止 selector 与 text 同时给，「素材」在顶栏「素材区」徽标上也有，
  所以点击必须带 `scope: .ws-bar`。
- **旧构建不算现在的代码**：`bundle_built_after_latest_source` 要求 `web/**` 的最新 mtime
  不早于 `frontend/src/**`。实测这条在本机当场抓到过旧了 4829 秒的构建；同时 `node_modules`/`.tmp`/
  `__pycache__` 必须从"源码"里排除，否则这条永远红。本工具**不代跑构建**，只拒绝把旧的当新的。
- 隔离判据是双的：临时状态根里真的落下了文件，**且** `GET /api/projects` 只列得出这一次的项目——
  漏传 `DOCMIND_STATE_ROOT` 时后面那条必红（仓库真登记表里项目不止一个）。
- 其余 fail-closed：后端没应答、注册只回了 200 没核对、console 有错、有失败请求、点了导航却没换
  活动标签或正文、正文被截断（截断后的"没找到"不是证据）、沙箱/服务/页面没收回——一律未通过。
- 现场三条：全表 14 条绿（8.5s）；`page: /no-such-page-for-apptest` → 后端仍绿但 `shell_mounted`
  与 `backend_data_reaches_page` 红；`api.py` 一导入就抛错 → `backend_started` 红并带出
  `ImportError`。测试 **43 项**（38 纯 + 5 现场），变异电池 **35/35**，门禁 +1 题（22→23）。
- 边界：不点真模型、不听真网关、不替组件级断言背书；页面与接口一律锁在回环与站内路径上。

## 6. 已知缺口与建议下一步

| # | 缺口 | 实测依据 | 建议 |
|---|---|---|---|
| 1 | ~~`.vue` 接线层无组件级守卫~~ **已做（Wave16 `dev_vue`，零新依赖）** | vitest 那条量化过不走：1 critical + 3 moderate + 1 high dev 公告，且 vitest 3 要 vite 6/7 而本树 vite 5。改走"仓库自带的 compiler-sfc + esbuild + 真浏览器点击" | 已做 Wave17：`dev_vue` 第二层驱动共享状态（§5.7）+ `dev_apptest` 真后端整页（§5.8）。仍**没有**的是：深链到具体路由/文件的整页断言、真模型往返（`dev_apptest` 明确不点模型）|
| 2 | ~~没有"跑测试→读失败→只重跑失败"的结构化工具~~ **已做（Wave15 `dev_testrun`）**；覆盖率入口仍无 | 实测 `.venv` 里 `coverage`/`pytest_cov` 都不存在；`.pytest_cache` 在本工作树是 Permission denied，`--lf` 不可用 | 覆盖率要先决定加不加依赖；记账由 `last-failed.json` 自己做 |
| 3 | 交付环"真网络"从未验证 | CI 新增的 pytest lane 与 dev_eval 门**从未在 GitHub 真跑过**（ubuntu 行为未证），沙箱 push 不通 | 人 push 一次，把首跑结果记回本文件 |
| 4 | `dev_serve` 端口被抢不自动重试 | Wave6 未做项 | 就绪失败时换端口重试一次 |
| 5 | 前端无 eslint 配置 | `frontend/` 无 eslint.config/.eslintrc | 先做配置决策，否则 `dev_diagnostics` 前端只有 typecheck 一条腿 |
| 6 | 连接器 UI 未接 `inline_tools` 开关 | Wave10 未做项 | 小改，UI 侧 |
| 7 | 一条计时断言只有 0.12s 余量 | `tests/test_orchestrator.py` 的 `elapsed < 0.32`，负载高时必红（属别人文件，未代改） | 改成相对余量（sleep 拉到 1.0s、断言 <1.6s） |

## 7. 共享工作树里的提交规范（实测有效的那套）

1. 只提交自己的路径：`git add <新文件>` + `git commit -- <只属于我的路径>`；**永远不要** `git add -A`。
2. 若自己改的文件**同时含别人的在途改动**（本会话实测：`tools.py` 16 个 hunk 里 5 个是我的、
   `agent.py` 34 个里 1 个），按 hunk 暂存：`git diff` 拆 hunk → 只留自己的 → `git apply --cached` →
   **裸 `git commit`（不带 pathspec！）**。`git commit -- <路径>` 会**绕过索引**，按这些路径的
   工作树内容临时建索引再提交——本会话就这么把别人 ~457 行在途 WIP 挂进我的提交（`b828d10`），
   而且因为扫进去的只有"引用未跟踪模块的那几行 import"，**HEAD 单独检出会 `import agent` 失败**
   （干净 worktree 实测：`ModuleNotFoundError: agent_runtime.run_budget`）。
   提交后的两道自检：`git show --stat` 看共享文件行数有没有超过自己的 hunk；
   `git worktree add --detach <tmp> HEAD` 里 `import agent` 一次，确认 HEAD 自洽。
   对暂存版本还要 `git show :./<path>` + `ast.parse`（半截 hunk 最容易留下语法残骸）。
   现在这一步有机械替代：**提交完直接跑 `dev_headcheck`**——它把新 HEAD 挂成干净检出真
   import 一遍，误收 WIP / 漏 `git add` 当场报"在磁盘但不在提交"，不用手工挂 worktree。
3. 判定某条红是不是自己造成的：把 HEAD 挂成干净 worktree（`git worktree add --detach`）跑同一批
   文件。**本会话两次这样验**：12 条红在我的提交上 183/183 全过 → 归属明确。用完 `git worktree remove`
   之后必须再 `rmdir` 空壳目录（Windows 上会被自己的 cwd 占住）。
4. `HANDOFF.md` 整文件提交**可能带走别人未入库的段落**（本会话实测：我的 `7965eb8` 扫进过一段
   别人的 R15 说明）。提交前**立刻**重新 `git diff -- HANDOFF.md` 看 hunk 数，事后 `git show --stat`
   复核并如实披露。
5. 不推。不代别人提交。跨进程写回有锁竞争时**不要原地改被 watcher 监听的源文件**（做变异测试时在
   `.tmp` 副本上做，见 §9）。

## 8. 文件地图（本程序新增，均在 `agent_runtime/` 除注明外）

`code_intel.py`(扩展) · `ts_index.py` · `diagnostics.py` · `patch_apply.py` · `lanes.py` ·
`dev_server.py` · `mcp_server.py` · `mcp_bridge.py` · `dev_eval.py` · `proposal.py` · `ci_status.py` ·
`media_fixture.py` · `page_action.py` · `voice_loop.py` · `head_check.py` · `test_run.py` ·
`vue_component.py` · `app_test.py` ·
`visual_acceptance.py`（被抽出 `browser_session` / `serve_static` / `stop_static` / `sanitize_flags`，
并被别的 lane 加了作业对象回收）· 测试：`test_dev_glob_git_log` / `test_lanes_registry` /
`test_ts_index` / `test_dev_diagnostics` / `test_dev_patch_move` / `test_mcp_server_expose` /
`test_mcp_inline_tools` / `test_dev_serve_preview` / `test_dev_eval` / `test_dev_propose_ci` /
`test_media_fixture` / `test_page_action` / `test_voice_loop` / `test_frontend_node_tests` /
`test_head_check` / `test_test_run` / `test_vue_component` / `test_app_test` ·
前端：`frontend/tests/liveAudioControl.test.mjs`（+ `test:node` 脚本与 CI 步骤）。

## 9. 事故与教训（避免重蹈）

- **99°C 漏浏览器**（2026-09-30 之前）：会话被硬杀时 `finally` 不执行，每次留下 ~15 个 headless
  Edge + profile，隔夜累积 462 进程 / 19GB。修法是"生在作业对象里"，不是事后补绑。任何新增的
  "起浏览器"代码都必须走 `browser_session()`。
- **假干净三连**（同一程序里反复出现）：CI 用 `unittest discover` 静默跳过 22 个 pytest 风格文件；
  ruff 输出截断被当成 0 发现；`node --test` 空 glob 退出码 0。**加一条测试/门禁时先做变异测试**：
  把它故意改坏，看有没有用例会红；不红就是装饰。
- **本会话我自己犯的 5 个错**（都留下了对应测试）：① 原地改生产文件做变异测试 → 还原撞上
  `OSError 22`，文件短暂留在变异态；② 把 `dev_media` 的异常文案合并成一句，打断既有断言；
  ③ 现场测试的浏览器探针写错模块别名被宽 `except` 吞掉 → **整类静默跳过**；④ `preview_target`
  抛错时会话名额没归还（变异测试跑出来的真 bug）；⑤ **`git commit -- <路径>` 绕过索引**：hunk
  级暂存和 `git diff --cached --stat` 核对都做对了，最后一步用 pathspec 提交，仍把别人 ~457 行
  在途 WIP 挂进 `b828d10`，且扫进去的 import 引用了未跟踪模块 → **HEAD 单独检出 `import agent`
  失败**（干净 worktree 实测）。流程教训：hunk 暂存之后只能裸 `git commit`，且提交后要
  `git show --stat` + 在干净 worktree 里 import 一次。
- **Wave17 的三条"量出来的形状"**（都是先按直觉写、跑真页面才纠正）：① `page_action` 的文本定位
  只在**可交互元素**里找，文件树里那行 `<span>` 永远命中 0；② `locate` 的候选清单在 64 个就
  `return`（`page_action.py:101`），于是"`#app *` 计数前后一样"完全可能是**饱和**而不是没变；
  ③ `read_page` 有 4000 字上限，拿 html 长度比"内容换没换"会得到两个一样的截断值。
  教训：凡是用"数量没变"当证据的地方，要先问这个数量会不会**撞到上限**。
- **本会话自己犯的两次同类错**（都由变异电池或复核抓出，不是我主动发现的）：
  ① 在 `setup_plan` 里写了 `json.loads(json.dumps(args))` 的"防御性深拷贝"并配了一条测试——
  入参本来就是 JSON 字符串，每次调用重新解析，别名根本不可能发生，那条变异**存活**证明测试是假的
  → 代码与假测试一起删。② 一次 `Edit` 想插入新行，`old_string` 带了行尾换行而 `new_string` 没带，
  结果把 `test_missing_component_...` 的函数体黏到了 `def` 行上；同一会话另一次同型错直接**吃掉**了
  `agent.py` 提示目录里的 `dev_testrun` 整行。流程教训：整行替换必须"旧串=新串的前缀关系"要清楚，
  改完立刻看 `git diff --stat` 的插入/删除计数对不对。
- **子进程的句柄比"进程已终止"活得久**：`dev_apptest` 第一次跑完在系统临时目录留了两份
  沙箱——`process_runner.job_cancel` 回来时 uvicorn 自己开的 sqlite/状态文件句柄还没被 OS 收走，
  `clean_target` 默认的 4 次 ×0.25s（约 1s）不够，而几秒后手工能删。回收预算提到 10 次 ×0.4s 后
  实测 0 残留。**删不干净这件事本身是判红项**（`artifacts_cleaned`），不是静默重试。
- **门禁自己有一个负载敏感点（本仓文件，不是别人的）**：
  `tests/test_dev_eval.py::GateBitesCases::test_cli_exit_codes` 是在**进程里把整套题跑一遍**
  再看退出码，所以任何一题（要起浏览器的 `media-fixture`、要真跑 pytest 的 `testrun`）现场卡顿
  都会连带它红。实测：9 个套件串在一起跑时它红过一次（84 passed / 3 failed），单跑同一文件
  15 passed、三文件组合重跑 87 passed。读这条红之前，先看同一时间有没有别的 lane 在起浏览器
  （按 `--user-data-dir` 归组数进程），别第一时间怀疑代码。

## 10. 本会话未结事项

1. **HEAD 已自愈（2026-10-02）**：`b828d10` 误收导致的"引用未跟踪模块"由那条 lane 用
   `0dbaf51 chore: 把 HEAD 已依赖但未跟踪的 4 个模块入库` 修好；本会话新加的 `dev_headcheck`
   第一次真跑抓到的正是这个，现在同一条检查报【通过】。误收本身仍按用户口径**保留不改历史**，
   披露与复现见 `HANDOFF.md` 那节。
2. **`voice_loop` 已入库**：独立审计 11 条里 8 条成立并改掉，变异电池 16/16 抓红
   （其中"开麦时归零序号"是真机跑出来的真 bug）。剩下的只是**边界**：真网关与听感归 R12。
   ~~`.vue` 接线层没有组件级守卫~~ 这条已经在 Wave16/17 补掉（`dev_vue` + `dev_apptest`，
   按选项3 的决定不走 vitest）。
2b. **`dev_apptest` 与"别人正在改前端"是耦合的**：它的 `bundle_built_after_latest_source`
   比较的是 mtime，而 `frontend/src/**` 随时可能被别的 lane 改动（实测本机 1 分钟内就变了两次）。
   所以这条红**通常是"该重新构建了"而不是代码坏了**；现场测试对这一条单独放行
   （只允许它是唯一一条红，其余 13 条必须绿）。本工具不代跑 `npm run build`。
3. 全量最后一次：**2569 passed / 12 failed**，12 条已用干净 worktree 证明全部来自
   另一条 lane 未提交的 step-budget / capacity / game_workflow WIP（本程序不代修、不代提交）。
4. 需要人做的两件：**push**（或 `git bundle` 离线备份），以及在配好 DashScope 的机器上跑一次
   真网关回路，把结果回写本文件 §5.3 与 §6#3。
