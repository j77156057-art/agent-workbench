# HANDOFF · Vibecoding 能力补全（harness 侧）

> 状态：**Wave1–9、11–19 已入库**（14 `dev_headcheck` 提交自洽 / 15 `dev_testrun` 跑测试 / 16 `dev_vue` 真实 .vue 组件守卫 / 17 组件守卫第二层「挂载前驱动共享状态」+ `dev_apptest` 真后端整页集成守卫）。
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
| 跑整页 | `dev_apptest`（起真 `uvicorn api:app` + 真 `web/` 产物 + 真浏览器）| **旧构建判未通过**（`web/` 比 `frontend/src` 新才算数）；不点真模型（§5.8） |
| 跑流式 | `dev_apptest stream: 1`（假麦克风 + `scripted` 适配器 + 真 `/api/vision/live-stream`）| 证明**自家协议与页面接线**；厂商事件与打断语义仍归 R12 人跑（§5.10） |
| 验语音/摄像头 | `dev_media`（`build` / `run` / `voice`）| 假设备喂合成媒体；门限行为见 §5.3 |
| 静态检查 | `dev_diagnostics`（ruff + vue-tsc）| 退出码非 0 却解析不出发现时**报错误不报干净** |
| 守启动器 | `agent_runtime/bat_guard.py`（静态扫 `.bat`/`.cmd`，**不是工具**，由测试与门禁题驱动）| 只查**交给外部程序的实参**；`cd`/`set`/`if` 条件/`echo` 由 cmd 自己解析，有意不查（§5.11）|
| 交付 | `dev_propose`（补丁+草稿，写进状态目录）/ `dev_ci_status`（只读 GET）| 不 add/commit/push；CI 读不到就明说 |
| 提交自洽 | `dev_headcheck`（把 HEAD 挂成干净检出：真 import，`gate: 1` 再真跑门禁整集，`tests:` 真跑指定测试）| 只读 + `worktree --detach`；红了先重跑再拿工作树当对照组，**比两边失败题名单的差集**，见 §5.4 |
| 多人防撞 | `dev_lanes`（TTL 认领登记表 + 可选 worktree）| 冲突默认拒绝，`force: true` 才共管 |
| 对外出口 | `mcp_server`（harness 自己是 MCP server）/ `mcp_bridge`（连接器工具一等公民化）| 默认只读；写要 `--allow-writes`；`run_command` 等永不暴露 |
| 能力回归 | `dev_eval`（24 题门禁 + 基线 + CI 步骤）| 测的是 harness，不是模型智商 |

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
# 守卫到底有没有牙：把源码逐条改坏，确认有测试/门禁因此变红（约 4 分钟，不起浏览器）
.venv/Scripts/python.exe tests/mutation/run_all.py --no-live          # 加 -v 看逐条
#   现场层是兜底：离线跑完仍有 2 条（整页的"就绪说谎"与"服务不回收"）验不了，
#   要在有 Edge 与前端的机器上完整跑：
#   .venv/Scripts/python.exe tests/mutation/run_all.py
# 能力门禁（24 题，不联网、不需要浏览器；含基线对比）
.venv/Scripts/python.exe -m agent_runtime.dev_eval --self-check \
    --baseline .github/dev-eval-baseline.json --json
# 本程序新增的回归（其中少量需要 Edge / node / 能起后端）
.venv/Scripts/python.exe -m pytest tests/test_media_fixture.py tests/test_page_action.py \
    tests/test_voice_loop.py tests/test_frontend_node_tests.py tests/test_head_check.py \
    tests/test_test_run.py tests/test_vue_component.py tests/test_app_test.py \
    tests/test_bat_guard.py tests/test_mutation_batteries.py \n    -p no:cacheprovider --basetemp=.tmp/pt-manual -q
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

### 5.4 `dev_headcheck`（Wave14；Wave21b 加第二档）：把 HEAD 挂成干净检出真 import、真跑套件
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

> **Wave21b 加的第二档（`gate: 1` / `tests:`）**——"import 绿"不等于"行为绿"。
> 2026-10-02 我自己第二次踩同型的坑就是证据：hunk 暂存收进了 `dev_eval` 里别人那段的**尾巴**
> （6 条 `_check`），它们读的局部量还在别人未提交的上半段里 → import 全绿、只验 import 的
> `dev_headcheck` 全绿、**行为红**，而当时是手工挂干净检出跑测试才抓到的。现在这一档自动化：
> 在那棵检出里真跑 dev_eval 整集（或指定 pytest 目标），**红了先在检出里重跑一次**（门禁题里有
> 计时敏感的，抖一下不该算提交坏了；翻绿也照样写在输出里），仍红才拿工作树跑同一条当对照组，
> 并且**比两边失败题名单的差集**——布尔值比较会被共同抖动污染（实测 `diagnostics-*` 两题在
> 两棵树里都红过），差集把公共噪声减掉、直接点名 `only_in_checkout`。
> 两条被实测纠正的形状：`dev_eval --json` 一行 ~26 KB 会被默认 4000 字符的输出窗拦腰截断
> （"读不出结论"和"门禁没跑成"是两件事，这一档显式放大窗口并有专门用例）；`run_bounded` 会
> 把 `timeout` **静默削到 `MAX_TIMEOUT=300`**，所以 `suite_timeout` 的上限钉在这个常量上，
> 有元测试盯着两者相等——配一个跑不到的秒数就是谎报能力。
> 凭据：`tests/test_head_check.py` 26 → 58 项、`battery_head_check` 14 → 44 条变异
> （`--no-live` 全抓）、门禁题 `headcheck-catches-untracked-reference` 8 → 17 条断言。
> 真检出那条端到端用例单跑 37 秒；拿事故 commit `f7ec14e` 复验：attempts=2、
> `only_in_checkout=[page-action-refuses-and-never-claims-success]`、
> `only_in_worktree=[]`——差集把两棵树共同抖掉的 `diagnostics` 减掉了。

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

### 5.10 `dev_apptest stream: 1`（Wave19）：整页真的走一轮实时对话，零网络

- **为什么现在才做得动**：这条链路（真页面 + 真麦克风 + 真 ws）以前只能人工验——它要一个
  "会回话的模型"。Wave19a 补的不是模型，是**一个离线剧本适配器**
  （`agent_runtime/realtime_scripted.py`，`DOCMIND_REALTIME_PROVIDER=scripted` 选中）：
  它走的是生产同一套注册表与事件契约（`RealtimeProvider` + `_emit(EVENT_TRANSCRIPT /
  EVENT_TEXT_DELTA / EVENT_AUDIO_DELTA / EVENT_DONE)`），所以被测的是**我们自己的接线**，
  不是厂商行为。注册只改了 `realtime_provider._BUILTIN_MODULES` 一行，`api.py` 一个字没动。
- **凭据设计**：剧本回合的内容全是从**收到的 PCM 现算**出来的量（片数 / 字节数 / 峰值 RMS）。
  于是页面字幕里出现这些数字，就只能解释为"麦克风真的流到了服务端"。判据不写死数字
  （浏览器推几片取决于时钟与夹具循环位置），而是查**自洽**：
  `字节数 == 片数 × 3200`（16k mono 100ms）、峰值落在 `(0.05, 1.0)`（喂静音凑不出这个带），
  且助手回包引用的字节数与转写是**同一次实测**（两个独立事件互相印证），并至少出**两轮**。
- 收轮规则故意照抄生产契约：**尾静音满 10 片**才算一轮说完（停止说话 ≠ 停止推流，Wave8 为
  这条付过账）——所以"喂 9 片静音"必须没有回合，夹具与守卫同一条线。
- 三条实测形状：① 假设备开关必须在**开页时**给（`page_action.open_page(flags=...)`，
  页面起来之后再传无效），漏传的现象不是红而是"没有字幕"，一开始就把我带去查上行；
  ② 「开麦对话」按钮在 `liveVisionActive` 之前**根本不存在**（`AutonomousCockpit.vue:2636`），
  固定 sleep 1 秒会报"命中 0 个"，必须带截止期重试；③ 轮次正则必须**不跨行**——
  `.*` 加 DOTALL 会把第一条转写配到最后一条轮次，两轮只认出一轮。
- 默认行为没被动过：`test_the_default_path_is_untouched_when_no_provider_is_configured` 钉住
  "没配 env 时 hello.ok 依旧报 `sampled-frames` + 原因"。
- 测试：`tests/test_realtime_scripted.py` 21 项（15 纯 + 5 真 `TestClient` websocket + 注册表），
  `tests/test_app_test.py` 55 项（50 纯 + 5 现场，现场含真流式那一档）；
  电池 `app_test` 46 条变异（其中 11 条是流式那一档）。
- 边界：**不**证明厂商事件、打断语义、听感；跑的是 `web/` 里那份构建产物；
  假麦克风只在有 Edge 的机器上验（CI 上那条现场用例是 skip，不是通过）。

### 5.11 `bat_guard`（Wave20）：启动器引号的静态门，第一天就抓到两个

- **事故本身**：用户贴来 `run_desktop.bat` 的运行失败 `missing D:\WorkBuddy\rag-agent"\requirements.txt`。
  根因是 `--root "%~dp0"`：`%~dp0` 展开后**以反斜杠结尾**，而外部程序按 MSVCRT 规则解析 argv，
  `\"` 是「转义过的引号」→ 闭合引号消失 → 参数值尾部多一个字面 `"`。源码里看着完全正常，
  只有双击运行才炸；这类东西必须静态层就咬住。
- **前提是被量出来的，不是写在注释里的**：门禁题最后一条直接调 `subprocess.list2cmdline`
  （Python 自己拼 argv 的实现）对比——值 `D:\my dir\` 的正确写法是 `"D:\my dir\\"`，
  而手写 `"%~dp0"` 发出去的是 `"D:\my dir\"`，**两串字节不同**，后者恰好是"转义引号"那个形状。
  不联网、不起进程、ubuntu 上也能跑。
- **两条规则互为镜像**：`quoted_dir_expansion`（加了引号但展开后以 `\` 结尾）与
  `unquoted_dir_expansion`（目录展开裸着传，路径有空格就裂）。只写前一条等于建议"别加引号"，
  那是另一个 bug。正解只有一种：`"%~dp0."`（补一个点）或 `"%ROOT:~0,-1%"`（切掉尾字符）。
- **门的价值全在"不越界"**：`cd /d "%~dp0"`、`if exist "%~dp0x.bat" goto y`、`set "R=%~dp0"`、
  `echo %~dp0`、`call "%~dp0lib.bat" --root "%~dp0"` 都由 **cmd 自己解析**，不是 MSVCRT，
  判红就是误报，而误报的门的下场就是被人关掉。所以 `arguments_of()` 是公开函数，
  电池里一半的变异是用**结构断言**（实参清单逐词比对）钉住的，而不是"报没报错"。
- **一跳追踪**：`set "ROOT=%~dp0"` 之后的 `--root "%ROOT%"` 是**同一个坑**，而且是把这行
  "修掉"的最自然写法；不追这一跳的门等于鼓励误修。两跳以上（变量套变量）不追，写在文档里。
- 测试：`tests/test_bat_guard.py` 40 项（+48 subtest），电池 35 条变异 `--no-live` **全抓、4 秒**，
  门禁题 `bat-guard-refuses-an-escaped-closing-quote` 11 条断言。语料层分两格：
  **工作树**扫（`RealRepoLauncherTests`，故意不进电池：它测的是别人的文件状态）与
  **HEAD** 扫（`git show HEAD:<path>`，CI 上看到的那份）。
- **第一天实测**：工作树层抓到 2 处 —— `run_desktop.bat:27` 与 `installer/uninstall.bat:9`
  （`powershell.exe -InstallDir "%~dp0"`，同一 bug 类的第二个实例），两处都在**别人未提交**的
  文件里，本程序不代改；HEAD 层绿（提交出去的那两份没这个问题）。
  **当天晚些时候两处都由各自那条 lane 改成了 `"%~dp0."`**（现 `run_desktop.bat:35`、
  `installer/uninstall.bat:13`，注释里写的理由就是"闭合引号被转义"）：工作树语料层现在
  0 findings、`tests/test_bat_guard.py` 40 项全绿——这道门的闭环到此为止，留着它防的是
  下一次有人再写回这个形状。
- 边界：只查 `.bat`/`.cmd` 里"目录展开 + 引号"这一类；`%VAR:~0,-1%` 之类剪切写法是否真的
  剪对了、PowerShell 自己那套引号规则、以及 `start`/`pushd` 之类的边缘行为都**没有**覆盖。

### 5.16 作业对象的进程预算：Wave22 修掉的那条"确定性红"

- **这不是补能力，是修一条把门禁压红的确定性缺陷**。接手 `e6078bb` 时
  `dev_headcheck gate: 1` 稳定红三题（`diagnostics-find-real-errors`、
  `diagnostics-clean-file-is-clean`、`headcheck-catches-untracked-reference`），
  重跑一模一样，**不是负载抖动**。根因在 `agent_runtime/windows_sandbox.py:80`：
  `limits.BasicLimitInformation.ActiveProcessLimit = 4`（裸字面量）。
- **链条**：门禁整集的子进程树是 `python -m dev_eval` → `python -m ruff` → `ruff.exe`
  （工具自己还会再开一层），整集里别的题同时还留着并发的后端/浏览器/git 进程。并发预算
  花光后 `CreateProcess` 直接回 **`WinError 1816`（配额不足）**。三步实验把因果钉死：
  ① 拿到完整原始输出——`diagnostics` 把它**误报成**「ruff 退出码 1 但输出没能解析
  （可能被截断）」，而退出码 1 的真实内容是一段 `subprocess.py:1554` 的 traceback；
  ② 排除深度——6 个**串行**孩子全过（`Popen` 返回即前一个已退出），限制卡的是**并发**；
  ③ 测出阈值——并发到第 4 个活进程就 1816。
- **为什么"比差集"抓不到它**：那个红在检出与工作树里**都**出现，于是归因如实说
  "不是自洽问题"，同时报【未通过】却不给任何可行动信号——**等于这个门每天教人忽略自己**。
  Wave21b 的"先重跑再比差集"是为负载抖动设计的，对确定性红无效。
- **修法**：`ACTIVE_PROCESS_LIMIT = 32`（具名常量 + 写清理由）。门禁整集**实测峰值 7**
  （作业对象 `QueryInformationJobObject` 采样，105 次采样 0 次失败），32 是它的 4 倍余量。
  单变量验证：只把 `4` 临时改成 `64`，**同一条调用**从 22/25（3 红）变成 **25/25 全绿**，
  真 `dev_headcheck gate: 1` 变成【通过】、干净检出 24/24。
- **为什么抬高它不削弱 99°C 那道修法**：真正回收整棵树的是
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，与进程数无关；浏览器路径至今**只**设这一个 flag
  （`visual_acceptance.py:242`），也就是说"只靠 kill-on-close"在本仓已经是交付过一次事故
  的写法。真正的资源上限是 `JOB_OBJECT_LIMIT_JOB_MEMORY`（1 GiB），`ACTIVE_PROCESS` 只是
  第二道刹车，所以新值**保留上限**而不是写成无界。
- **顺带把限位构造抽成 `job_limits()` 纯函数**：内联在 `_create_job` 里的时候，非 Windows
  上没有任何函数能读到那张表，于是 `KILL_ON_JOB_CLOSE` 掉了、预算写回 4 了，CI 上一个红都
  没有。同时把模块级的 `import ctypes.wintypes` 改成容错导入——它在非 Windows 上抛的是
  `ValueError` 不是 `ImportError`，而模块 docstring 早就写着"非 Windows 走 fallback，
  好让契约在 CI 上可测"，只是这条 import 把它堵死了。
- **产物与数量**：`windows_sandbox.py`（+46/-12，唯一的行为变化就是 4 → 32）；
  `tests/test_windows_sandbox.py` **13 项**（9 纯层 + 2 fail-closed + 2 现场）；
  `tests/mutation/battery_windows_sandbox.py` **19 条变异 / 全抓 / 33 秒**；
  门禁新增 1 题 `sandbox-process-budget-survives-a-real-tree` **7 条断言**。
  **基线不用动**：`compare_to_baseline` 把基线里没有的新题放进 `new_cases` 放行，
  只有"基线里已有且这轮翻转"的题才判回退（整集实测 **26/26**，`new_cases` 就是那一题）。
- **边界（不许越界宣称）**：它证明的是"作业对象的限位表摆得对、且门禁那棵树真的塞得进去"，
  **不**证明并发无关的稳定性；嵌套树的现场检查只在 Windows 上跑，CI（ubuntu）上那两条
  是 skip 而不是通过；纯层能验的是**限位表与常量**，验不出"进程有没有真的被加进作业对象"
  ——那条只有现场层（关一次句柄、看整棵树会不会跟着走）能抓，而它正是电池里唯一
  只能靠现场抓的那一类。

### 5.17 Wave23：把"平台"当成第二现场层，以及两条自己招的等价变异

这一节记的是**同一个错误的两种形态**：让一条变异"活下来"，而原因都不在守卫身上。

- **CI 的电池跑在 ubuntu 上，所以"只有 Windows 能验"必须算现场层**。`.github/workflows/
  harness.yml` 是 `ubuntu-latest` + `run_all.py --no-live`。Wave22 那块电池里 3 条
  fail-closed 变异只被 `@skipUnless(os.name == "nt")` 的用例抓着 —— 在 ubuntu 上那些
  用例被 skip，于是**无人抓 → 记成活下来 → 电池红 → CI 步骤失败**。也就是说 Wave22
  那个提交在 CI 上是红的，而本机（Windows）跑出来是 19/19 全抓，看不出来。
  修法不是"给 CI 换个平台"，而是把这类用例归到一个 `WindowsOnlyLiveTests` 并声明成
  `LIVE`：ubuntu 上它们记「本环境未验证」——既不算抓住也不算活下来。
  **本电池的 `LIVE` 语义因此与别处不同**（那边是"需要真浏览器"，这边是"需要 Windows"），
  这是在电池文件里自己声明的，没有改引擎的契约。
- **等价变异的第二种来源：依赖被装上了**。`test_run` 那条"缺覆盖率依赖时不开口"报活下来，
  原因是 Wave21 把 `coverage>=7` 装进了 `.venv`，`coverage_available()` 恒为真，那条守卫
  在本机根本走不到 —— 把它变异成 `if False:` 是**等价变异**，不是守卫没牙。修法是让用例
  不依赖本机环境：直接把 `coverage_available` 打成 False，再断言**runner 一次都没被调用**。
  写新判据时问一句"它依赖的是代码还是环境？"
- **等价变异的第三种来源：两个机制互为冗余**。`run_isolated` 超时那条我先写成"孩子直接写
  标记"，于是「杀掉直接子进程」（`process.kill()`）与「终止整个作业对象」
  （`TerminateJobObject`）**单独去掉哪一个都照样绿**，两条变异一起报活下来 —— 看着像两条
  缺口，其实是测试的树只有一层。改成"孩子拉一个孙进程去写标记"之后两个机制才可区分，
  而这恰恰就是作业对象存在的理由。
- **引擎侧的通用修法**：`anchors_valid()` 现在查 `source.count(old) == 1`，非唯一即报
  **问题**。先量影响面再改：十块电池 256 条锚点里只有 3 条不唯一，且全在 `battery_head_check`
  （`render` 里有 12 空格缩进的同名块，4 空格那行是它的子串），改锚点不影响别人。
- **仍然没有**：引擎没有"按平台分层"的概念，所以给非 Windows 的 fallback 分支写变异会
  让两个平台里必有一个红（那种变异在 ubuntu 上被纯层抓住、在 Windows 上却是活下来）。
  本轮**选择不写**那种变异，并在电池 docstring 里写明为什么——这比写一条让 CI 红的变异诚实。

## 6. 已知缺口与建议下一步

| # | 缺口 | 实测依据 | 建议 |
|---|---|---|---|
| 1 | ~~`.vue` 接线层无组件级守卫~~ **已做（Wave16 `dev_vue`，零新依赖）** | vitest 那条量化过不走：1 critical + 3 moderate + 1 high dev 公告，且 vitest 3 要 vite 6/7 而本树 vite 5。改走"仓库自带的 compiler-sfc + esbuild + 真浏览器点击" | 已做 Wave17：`dev_vue` 第二层驱动共享状态（§5.7）+ `dev_apptest` 真后端整页（§5.8）。仍**没有**的是：深链到具体路由/文件的整页断言、真模型往返（`dev_apptest` 明确不点模型）|
| 2 | ~~没有"跑测试→读失败→只重跑失败"的结构化工具~~ **已做（Wave15 `dev_testrun`）**；覆盖率入口仍无 | 实测 `.venv` 里 `coverage`/`pytest_cov` 都不存在；`.pytest_cache` 在本工作树是 Permission denied，`--lf` 不可用 | 覆盖率要先决定加不加依赖；记账由 `last-failed.json` 自己做 |
| 3a | ~~真 ws 回合零自动覆盖~~ **已做（Wave19：`scripted` 适配器 + `dev_apptest stream`）** | 页面 + 麦克风 + `/api/vision/live-stream` 以前只有人对着说话才能验 | 厂商事件、打断语义、听感仍归 R12 的人跑 |
| 3 | 交付环"真网络"从未验证 | CI 新增的 pytest lane 与 dev_eval 门**从未在 GitHub 真跑过**（ubuntu 行为未证），沙箱 push 不通 | 人 push 一次，把首跑结果记回本文件 |
| 4 | `dev_serve` 端口被抢不自动重试 | Wave6 未做项 | 就绪失败时换端口重试一次 |
| 5 | 前端无 eslint 配置 | `frontend/` 无 eslint.config/.eslintrc | 先做配置决策，否则 `dev_diagnostics` 前端只有 typecheck 一条腿 |
| 6 | 连接器 UI 未接 `inline_tools` 开关 | Wave10 未做项 | 小改，UI 侧 |
| 7 | 一条计时断言只有 0.12s 余量 | `tests/test_orchestrator.py` 的 `elapsed < 0.32`，负载高时必红（属别人文件，未代改） | 改成相对余量（sleep 拉到 1.0s、断言 <1.6s） |
| 11 | ~~引擎不查锚点唯一性~~ **已做（Wave23）** | `tests/mutation/engine.py` 做 `source.replace(old, new, 1)`，锚点出现两次时只改第一处。**先量影响面**：十块电池 256 条锚点里只有 3 条不唯一，全在 `battery_head_check`（`render` 里有 12 空格缩进的同名块，4 空格那份是它的子串） | 已加 `source.count(old) == 1` → 报**问题**；3 条锚点改成带上下文。**仍有**：引擎没有"按平台分层"的概念，所以非 Windows fallback 分支的变异在两个平台里必有一个红（见 §5.17） |
| 12 | ~~`run_isolated` 仍然零测试~~ **已做（Wave23）** | 它与 `spawn_isolated` 共用 `_create_job`，本次出事故的那行在两个函数里各有一份 | 已补 5 条跨平台 + 3 条 Windows-only 用例（含一条**加入失败后已起来的子进程必须被收掉**的漏进程判据），电池 19 → **25 条变异** |
| 13 | `run_isolated` 的子进程输出**可能整个丢掉且不报错** | `Popen(..., text=True)` 没给 `encoding`，按本机区域编码解码；本机是 GBK，子进程吐 UTF-8 中文时读线程抛 `UnicodeDecodeError`，那一路变成 `None`（`stdout is None` 而不是报错） | 调用方要么传 ASCII 输出，要么给 `windows_sandbox` 加 `errors="replace"` + 显式 encoding。**本轮只报不改**——它会改变所有适配器进程的行为，不在 Wave23 范围内 |

## 7. 共享工作树里的提交规范（实测有效的那套）

1. 只提交自己的路径：新文件用 `git add <路径>`，共享文件按 hunk `git apply --cached` 只挑自己的块；
   暂存后**只能裸 `git commit`**。**永远不要** `git commit -- <路径>`——pathspec 会绕过索引直接提交
   工作树内容，把别人在同一批文件里的在途改动一起扫进去（本仓 `b828d10` 就是这么连累 HEAD 的，
   见 `HANDOFF.md` §9）；也永远不要 `git add -A`。提交后 `git show --stat` 核对，再跑一次 `dev_headcheck`。
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

## 7.5 变异电池放在哪、怎么读（Wave18）

- `tests/mutation/engine.py`：共用引擎。**三条不可退让**：① 对照组（未变异）必须在电池要用到的
  每一层都干净，否则整块电池不作数——不然环境一红，每条变异都"被抓到"，分数好看而毫无意义；
  ② 锚点找不到原文＝电池与源码脱钩，报**失败**而不是跳过；③ 现场层跑不了（没浏览器/工具链）时
  相关变异记「本环境未验证」，**既不算抓住也不算活下来**。
- `tests/mutation/battery_*.py`：九份声明式电池（`MODULE` / `TARGET` / `CASE_ID` / `TEST_MODULE`
  / `BIND` / `UNIT_CLASSES` / `LIVE` + `MUTATIONS`）。第 4 个元素 `("live",)` 表示
  "只有真浏览器才看得见"。**没有现场层的也要显式写 `LIVE = ()`**——元测试读 `battery.LIVE`
  而不是 `getattr(..., ())`，缺属性就是 `AttributeError`（Wave20 第一天就撞在这上面）。
  测"别人文件现在干不干净"的那一层要写进 `LAYER_EXEMPT`：把它当对照组，环境一脏整块电池作废。
- `tests/test_mutation_batteries.py`：元测试（0.4 秒，`unittest discover` 会自动收到）。查
  锚点漂移、测试类/现场用例改名、门禁题号变更、**每块电池自己声明的 `MINIMUM_MUTATIONS`
  下限**（别的 lane 加电池不用改中央清单），以及**"新写的测试类有没有落到某一层"**——
  这条是我自己撞出来的：给 `dev_apptest` 加完流式测试后跑电池，10 条流式变异全活下来，
  原因不是守卫没牙，是电池根本不知道有新类。不想让它成为层的，必须写进
  `LAYER_EXEMPT` 并说明原因（如"要 import tools 全量注册面"）。
- CI：`.github/workflows/harness.yml` 的 `Run mutation batteries` 一步，跑 `--no-live`，
  只在 sqlite matrix 付费。本机实测（2026-10-02，九块电池）：`--no-live`
  **229/237 抓到 + 7 条本环境未验证 + 1 条活下来，587 秒**（`head_check` 44 条要 529 秒，
  第二档那 3 条真检出用例是主要成本；`bat_guard` 35/35 只用 4 秒——纯文本判据便宜到没借口
  不跑全）。那 1 条活下来在 `test_run` 里，**不是我这轮的事**：别人未提交的 `test_run.py`
  （他们的 `coverage:` 档）让一条变异变成等价变异；同一块电池在**干净检出的 HEAD** 上重跑是
  18/18，`head_check`+`bat_guard` 在 HEAD 上合跑 **79/79（467 秒）**，我的四套在 HEAD 干净
  检出里 **133 passed**。全量在 HEAD 工作树合起来 **2165 passed / 64 skipped / 348 秒**；带浏览器
  `--only vue,app_test` **72/72，99 秒**。
- **怎么读这份证据**：电池绿＝"这些测试确实会红"。它**不**保证变异集合覆盖了这个模块的每个守卫；
  新加一条守卫时同时加一条变异，否则那条守卫没有凭据。

## 8. 文件地图（本程序新增，均在 `agent_runtime/` 除注明外）

`code_intel.py`(扩展) · `ts_index.py` · `diagnostics.py` · `patch_apply.py` · `lanes.py` ·
`dev_server.py` · `mcp_server.py` · `mcp_bridge.py` · `dev_eval.py` · `proposal.py` · `ci_status.py` ·
`media_fixture.py` · `page_action.py` · `voice_loop.py` · `head_check.py` · `test_run.py` ·
`vue_component.py` · `app_test.py` · `realtime_scripted.py` · `bat_guard.py` ·（另有 `tests/mutation/engine.py` + `run_all.py` + `battery_*.py`，条数下限写在每块电池自己的 `MINIMUM_MUTATIONS` 里）·
`visual_acceptance.py`（被抽出 `browser_session` / `serve_static` / `stop_static` / `sanitize_flags`，
并被别的 lane 加了作业对象回收）· 测试：`test_dev_glob_git_log` / `test_lanes_registry` /
`test_ts_index` / `test_dev_diagnostics` / `test_dev_patch_move` / `test_mcp_server_expose` /
`test_mcp_inline_tools` / `test_dev_serve_preview` / `test_dev_eval` / `test_dev_propose_ci` /
`test_media_fixture` / `test_page_action` / `test_voice_loop` / `test_frontend_node_tests` /
`test_head_check` / `test_test_run` / `test_vue_component` / `test_app_test` / `test_mutation_batteries` / `test_realtime_scripted` / `test_bat_guard` ·
电池（可重跑的凭据，Wave18 入库）：`tests/mutation/battery_*.py` = vue / app_test / head_check /
test_run / voice_loop / **bat_guard** / page_action / log_view / job_logs。
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
- **同一个错我第三次才记住**（`Edit` 的锚点带上下相邻行）：Wave17 吃掉 `agent.py` 提示目录的
  一整行，`dev_vue` 那次把函数体黏到 `def` 行上，Wave20 插入门禁题时又把
  `DEV_DATASET` 第一题的 `steps` 那行删掉了——**两次都是"用下一行的前缀当锚点"**。
  这次靠 `git diff --stat` 的插入/删除计数不对才看见（140/4 变成 139/1）。流程教训：
  往共享的热文件里插东西，用**唯一且完整**的锚点，或者干脆走"读全文 → 替换 → `ast.parse` 过了
  才落盘"的脚本；改完立刻核对 `git diff --stat` 的两个数字。
- **一条变异把整块电池崩掉，而我差点把"崩掉"报成"全抓"**：Wave21 里我写了条只替换半句的变异，
  `exec_module` 抛 `IndentationError`，整轮电池死在 traceback 上；而我第一次读结果时把汇总行
  看成了 `44/44 抓到`（其实是 `41/44 + 3 条活下来`）。两条修法是同一件事的两面：
  ① 引擎在**事前**用 `ast.parse` 检查"变异后的源码还编译得过吗"，编译不过就报成这条电池的
  问题（不是"抓住"——一份装不进的代码证明不了任何判据有牙）；② 跑批不再 `| tail`，
  管道会把退出码换成 `tail` 的（`$?` 说 0 而电池其实红着，这个坑本仓已经记过一次）。
- **对照组要比"名单差集"，不能比"整集布尔值"**：拿真事故 commit（`f7ec14e`）跑这一档，两棵树
  **都**因为 `diagnostics` 那两题在负载下抖而红；如果比布尔值，结论就成了"两棵树都红，
  不是自洽问题"，而真正要指出的那一题（只在检出里红的 `page-action-...`）被埋掉。
  改成 `checkout_failed - control_failed` 之后，公共噪声自己减掉，报告直接点名那一题。
  教训通用：**任何"两边比一比"的判据，都要问比的是布尔值还是集合**。
- **等价变异是自己写给自己的假分数**：`row["failed_names"] = [] or sorted(...)` 恒等于原式，
  所以它"活下来"根本不是守卫没牙，是我那条变异写歪了。同一条批次里还有两条"活下来"是真的
  ——`tests:` 那条路的失败题名单从来没被任何用例读过。两种"活下来"要分开处理：
  等价变异换掉，真缺口补判据（`test_the_pytest_path_also_names_its_failed_tests`）。
- **子进程的句柄比"进程已终止"活得久**：`dev_apptest` 第一次跑完在系统临时目录留了两份
  沙箱——`process_runner.job_cancel` 回来时 uvicorn 自己开的 sqlite/状态文件句柄还没被 OS 收走，
  `clean_target` 默认的 4 次 ×0.25s（约 1s）不够，而几秒后手工能删。回收预算提到 10 次 ×0.4s 后
  实测 0 残留。**删不干净这件事本身是判红项**（`artifacts_cleaned`），不是静默重试。
- **判据可以"结构上永远为假"，比写反更难发现**：`page_action.read_page` 的 `truncated` 判据是
  `len(text) > 4000`，而 `INSPECT_JS` 在返回前就 `slice(0, 4000)` —— 这个条件不可能成立，于是每次
  整段读取都显示"没截断"。凡是"我有没有被截断/丢包/跳过"这类判据，都要问一句
  **它读的是不是已经被削过的那份**。同型错误本仓还有一次：拿 `.is_file()` 查目录，导致整类现场
  用例静默跳过而全量仍显示绿灯。修法都一样——【多上报一个原始量】（`full_length`），而不是猜。
- **上限写两处，就等于写了一处旧的**：`contains` 的样本上限原本 JS 写死 3、Python 再切一次，
  变异电池当场"抓不到"（删掉任何一边都看不出差别）。现在 `limit` / `samples` 都由 Python 随请求
  下发，并有专门用例断言发出去的数值。
- **门禁自己有一个负载敏感点（本仓文件，不是别人的）**：
  `tests/test_dev_eval.py::GateBitesCases::test_cli_exit_codes` 是在**进程里把整套题跑一遍**
  再看退出码，所以任何一题（要起浏览器的 `media-fixture`、要真跑 pytest 的 `testrun`）现场卡顿
  都会连带它红。实测：9 个套件串在一起跑时它红过一次（84 passed / 3 failed），单跑同一文件
  15 passed、三文件组合重跑 87 passed。读这条红之前，先看同一时间有没有别的 lane 在起浏览器
  （按 `--user-data-dir` 归组数进程），别第一时间怀疑代码。

- **"已知红"要重新验一遍，它可能是确定性的，而差集归因天生抓不到确定性红**（Wave22）。
  接手时 `diagnostics-*` 两题被记为"负载下会抖"，实测重跑两次一模一样。根因是作业对象
  `ActiveProcessLimit = 4`：`python -m dev_eval` → `python -m ruff` → `ruff.exe` 这条树加上
  别的题留下的并发进程把预算花光，`CreateProcess` 回 **`WinError 1816`（配额不足）**。
  两层遮蔽让它格外难查：① `diagnostics` 把这个 traceback **误报成**「ruff 输出没能解析
  （可能被截断）」，错误信息把方向指向完全不同的地方；② 因为它在检出与工作树里都红，
  "比失败题名单差集"的归因会把它当公共噪声减掉，于是工具如实说"不是自洽问题"、同时报
  【未通过】且不给任何可行动信号。**教训**：把一条红归为"环境噪声"之前，先问它**是不是
  每次都以同样的名字红**；确定性的红不会被差集减掉，只会被差集**藏起来**。
- **"活下来"先怀疑锚点，再怀疑守卫**（Wave22 实测）。`battery_windows_sandbox` 有一条
  "进程没被加入作业对象也不报错"报活下来，我按机制推断现场层一定能抓（并用探针证实了
  机制：不加入作业对象的孩子确实不会被句柄关闭带走）。真因是**锚点不唯一**——
  `run_isolated` 与 `spawn_isolated` 的 `if ... AssignProcessToJobObject` 那一行字面完全
  相同，而引擎做 `replace(old, new, 1)`，变异被打进没人调用的 `run_isolated`。
  现有元测试只查"锚点在不在"，查不出"锚点是不是只出现一次"。**教训**：加变异时优先挑
  **唯一**的锚点（多带一行上下文）；报活下来时，先问"这条变异改的地方真被调用吗"。
- **`Popen` 返回 ≠ 进程起来了，而且"没人读的管道"会把子进程永久卡死**（Wave22 两条现场
  用例都被这两件事骗过）。① `Popen` 在 `CreateProcess` 成功那一刻就返回，所以"拉起 8 个
  进程"只证明系统肯创建，不证明 8 个**同时占着预算**——现场判据要改成"每个孙进程先写标记
  文件再睡，等齐标记才回报"。② `spawn_isolated` 把子进程 stdout 接成 `PIPE` 并**指望调用方
  读**；Windows 匿名管道缓冲约 4 KB，而门禁要吐 27 KB JSON，于是子进程卡在 write 上永不
  退出——我那个量峰值的探针为此挂了 15 分钟。`process_runner.run_bounded` 会抽干管道所以没
  这问题，**自己 Popen 时必须 `communicate()` 或把输出接到文件**。
  顺带：中途 `kill()` 一个还在加载 DLL 的 Python 会打出 `Windows fatal exception: access
  violation`，看到它先查是不是在杀没起来的进程，别当成代码崩了。
- **判据"永远为真"的同型：让被观察的对象自己退出**（Wave22 第一版的假测试）。回收那条
  现场用例起个孩子、关掉作业对象句柄、断言孩子没了——可那个孩子**自己也会退出**，于是
  断言恒为真，什么都没验就绿。修法是让孩子睡 300 秒、并在关句柄**之前**用就绪标记确认它
  真的活着。**教训**：断言"某物消失"之前，先问"它还有什么别的原因会消失"。

- **等价变异还有两个来源，都不是守卫没牙（Wave23 实测）**：① **依赖被装上了**——那条
  "缺覆盖率依赖时不开口"的守卫只在本机没装 `coverage` 时才走得到，Wave21 装上之后它成了
  恒真的空路径，变异成 `if False:` 自然"活下来"。② **两个机制互为冗余**——超时回收里
  「杀直接子进程」与「终止作业对象」单独去掉哪一个都照样过，因为测试的树只有一层；改成
  "孩子拉一个孙进程去写标记"才可区分。**教训**：写判据时问两句——它依赖的是**代码**还是
  **环境**？它给的树有**几层**？两句都答不上来的判据，变异活下来是必然的。
- **本机全绿 ≠ CI 绿，因为它们不是同一个平台**（Wave22 提交在 CI 上是红的）：
  `.github/workflows/harness.yml` 在 **ubuntu-latest** 上跑 `run_all.py --no-live`，而我那块
  电池有 3 条变异只被 `@skipUnless(os.name == "nt")` 的用例抓着。ubuntu 上那些用例被 skip
  → 无人抓 → 记成**活下来** → 电池红 → CI 步骤失败。**教训**：给一个只有某平台能验的守卫
  写变异之前，先问"CI 那条命令在这个平台上会看到什么"。修法有两条：把这类用例声明成
  `LIVE`（本环境未验证，既不算抓住也不算活下来），或者让用例不依赖平台。**别把"本机跑过
  全抓"当成 CI 会绿**——本仓 §6 第 3 条早就记着"CI 从未在 GitHub 真跑过"。

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
   真网关回路，把结果回写本文件 §5.3 与 §6#3。push 之前顺手跑一次
   `dev_headcheck` 带 `gate: 1`（约 1 分钟）：CI 看到的是那棵树，不是这棵树。
5. **Wave20 的门抓到过、当天就被各自那条 lane 修好了**：那两处 `"%~dp0"`（`run_desktop.bat`
   与 `installer/uninstall.bat`）现在都是 `"%~dp0."`，本程序全程没代改别人一行文件；
   `tests/test_bat_guard.py::RealRepoLauncherTests` 现在绿，留着它防的是下一次再写回这个形状
   ——**它已经生效了，不是待办**。同一批账里 `dev_headcheck` 加了第二档（`gate: 1`，本会话
   记作 **Wave21b**：另一条 lane 也把他们的四个缺口叫作 Wave21，尚未提交，用 b 后缀避免撞号）：
   **push 之前跑它**，因为 CI 看到的
   是那棵树而不是这棵树；本会话就是靠它在事后抓到 `f7ec14e` 的行为红（见 §5.4）。
   基线 `.github/dev-eval-baseline.json` 按【我的提交】口径刷成 `23 → 24`；工作树里还有别人
   那一题（`log-view-collapses-real-spam`），所以本地刷全量会得 25——那一行不由我提交。
