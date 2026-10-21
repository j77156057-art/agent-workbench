# HANDOFF · Vibecoding 能力补全（harness 侧）

> 状态：**Wave1–9、11–21 已入库**（14 `dev_headcheck` 提交自洽 / 15 `dev_testrun` 跑测试 / 16 `dev_vue` 真实 .vue 组件守卫 / 17 组件守卫第二层「挂载前驱动共享状态」+ `dev_apptest` 真后端整页集成守卫 / **21 补四个缺口：`dev_deps` 依赖对账、`dev_probe` 后端/CLI 观察、`dev_testrun coverage: true`、`dev_handoff` 交付决策材料** —— 见 §5.12–5.15）。
> 发出人：本会话（用户直接指派，不占 R0–R15 槽位）。最后更新：2026-10-02。
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
| 跑测试 | `dev_testrun`（`paths` / `keyword` / `only_failed` / `related` / `coverage`）| 退出码 5 = 一个都没收集到，判**未通过**；`coverage: true` 按改动文件给未覆盖行（§5.12）|
| 看结果 | `preview_project`（截图 + console + 失败请求）/ `dev_page_action`（点/填/按键/等待）| **no_change 不是成功**；只回环 |
| **看后端/CLI** | `dev_probe`（`mode: http` / `mode: cli`）| 补浏览器那套够不着的那半边（§5.13） |
| **查依赖** | `dev_deps`（对账 import ↔ 声明，`action: apply` 只追加）| 补「本地能跑、干净装不上」那段（§5.14） |
| 守组件 | `dev_vue`（真实 .vue 挂进真浏览器：props → 真点击 → 收 emit；第二层可**挂载前驱动共享状态**）| **不写断言＝未通过**；只证明一个组件在一组 props 下（§5.6、§5.7） |
| 跑整页 | `dev_apptest`（起真 `uvicorn api:app` + 真 `web/` 产物 + 真浏览器）| **旧构建判未通过**（`web/` 比 `frontend/src` 新才算数）；不点真模型（§5.8） |
| 跑流式 | `dev_apptest stream: 1`（假麦克风 + `scripted` 适配器 + 真 `/api/vision/live-stream`）| 证明**自家协议与页面接线**；厂商事件与打断语义仍归 R12 人跑（§5.10） |
| 验语音/摄像头 | `dev_media`（`build` / `run` / `voice`）| 假设备喂合成媒体；门限行为见 §5.3 |
| 静态检查 | `dev_diagnostics`（ruff + vue-tsc）| 退出码非 0 却解析不出发现时**报错误不报干净** |
| 守启动器 | `agent_runtime/bat_guard.py`（静态扫 `.bat`/`.cmd`，**不是工具**，由测试与门禁题驱动）| 只查**交给外部程序的实参**；`cd`/`set`/`if` 条件/`echo` 由 cmd 自己解析，有意不查（§5.11）|
| 交付 | `dev_propose`（补丁+草稿，写进状态目录）/ `dev_ci_status`（只读 GET）| 不 add/commit/push；CI 读不到就明说 |
| **交到门口** | `dev_handoff`（分支名 + commit message 建议 + 范围体检）| 仍然不 add/commit/push；只算决策材料（§5.15）|
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
- **`mode: contains`（2026-10-02 补）**：`read` 的文本/结构输出有 4000 字上限，所以
  「这句话在不在页面上」用整段文本来判断，实际只回答了「它在前 4000 字里在不在」。
  新模式把判断下推到页面里：`selector:` 是【容器】、`needle:` 是【要搜的词】，回
  命中数、覆盖几个元素、扫了多少字符、最多 3 段周围样本——返回量与页面大小无关。
  三条边界钉在用例与门禁里：① **可见性必须显式判**（`getClientRects()`），因为
  `innerText` 对不在渲染树里的元素会退回 `textContent`（真浏览器实测 `#hid` 照样搜得到），
  不可见命中单独报 `hidden_hits`，既不点亮 `found` 也不静默丢掉；② 容器没匹配到 /
  没扫完（200k 字符、单元素 500 次）时必须明说「这次搜索不构成证据」；③ 上限由 Python
  通过 `spec.limit` / `spec.samples` 下发，JS 不自带默认值（两处各一套数字时，调一处
  另一处仍按老数字静默生效——变异电池实测）。电池 **18/18**
  （`tests/mutation/battery_page_action.py`，纯层+门禁+现场三层对照组）。
- 顺带修掉一个**结构上永远为假**的判据：`INSPECT_JS` 先 `slice(0, 4000)`，Python 再判
  `len(text) > 4000` → `truncated` 恒为 False，"读了一半"看起来像"读完了"。现在页面在截断
  之前算好 `full_length` 一起回传，判据改用它（`dev_apptest` 的 `page_text_read_wholly`
  也因此从装饰变成活判据）。

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

### 5.9 `log_view`：长日志的重复折叠与级别筛选（2026-10-02，用户点名）

- **为什么不是"读更多"**：`run_command`/`dev_serve` 的输出缓冲本来就是"头 4000 + 尾 4000，丢中段"
  （`process_runner.py:30-31`），抬上限只会把噪声灌进上下文。真正缺的是**把判断下推到文本里**：
  折掉重复、按级别留内容、并如实说"哪些没看见"。
- **签名而不是整行**（这条是量出来的）：真起 uvicorn（`--log-level info`）打 24 次同一接口，
  抓到的 27 行日志**逐行去重还是 27 行**——每条 access 行都带临时客户端端口
  （`127.0.0.1:56617`）。按签名归一（数字、长十六进制 id、uuid）后 28 → 10 行、重复 21 次折成一段。
  样本入库为 `tests/fixtures/uvicorn-access.log`，测试直接对着它断言，不写"看起来短了"。
- **刻意不做两件事**：① 不归一引号里的内容——access 行的引号里是请求路径，把 `"/api/settings"`
  与 `"/api/models"` 折成一个签名就是**把不同事件合并**，比折不动更糟；② 不留"空白归一"和
  "IP 归一"两条规则——`\d+` 已经把 IP 吃掉，空白那条举不出真实用例，电池里它的变异直接活下来，
  说明是猜测，删了。
- **级别筛选必须按块，不能按行**：`Traceback` 下面的正文行没有任何级别词，逐行筛正好把最该看的
  部分筛掉。实现是无级别行**继承**上一条识别到的级别；文件开头还没识别到级别的行一律保留（保守方向）。
- **视图不许冒充全文**：`middle_omitted_chars`（缓冲丢掉的中段）、`line_cap_hit`（超过 2 万行没看）、
  `dropped_failure_marks`（被级别筛掉的段里还带着 4xx/5xx/失败 字样）都单独计数，`render` 把这三句
  放在正文前面。uvicorn 把 404 记成 INFO，所以 `level: warn` 筛掉它是合规的——但那份视图
  **永远不能被读成"没有错误"**。
- **计数自洽**：`lines_in - dropped_lines - folded_away + markers_added == lines_out`。
  `markers_added` 必须单列，因为「… ↑ 重复 N 次」是合成行，不是原日志。这条不变量与
  `dev_testrun` 的"摘要与退出码不自洽就判红"同型。
- 工具面：`dev_job_logs` 加 `fold: true` / `level: warn|error`。默认（不给这两个键）**保持原始增量
  文本不变**、只附一句 `view_hint`，避免改动别人在用的行为；视图模式是整块重算，**不能续读**，
  带了 `offset` 会明说被忽略并把 `next_offset` 置空。
- 证据：`tests/test_log_view.py` **26 项**；电池两块——`battery_log_view` **17/17**、
  `battery_job_logs` **6/6**（后者钉"参数有没有真的落到模块""视图有没有谎报可续读"）；
  门禁 +1 题（23→24，`log-view-collapses-real-spam`，9 条判据）。电池过程中一条变异
  （fold=False 仍分组）**活下来**，查明是我留了两条产出完全相同的路径 → 合并成一条，
  换成"去掉 emit 处的 fold 判断"这条真有牙的变异。

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
  **当天晚些时候两处都由各自的 lane 改成了 `"%~dp0."`**（现 `run_desktop.bat:35` 与
  `installer/uninstall.bat:13`，两边注释里写的理由就是"闭合引号被转义"这一条），
  工作树层现在 0 findings、`tests/test_bat_guard.py` 40 项全绿——这道门的闭环到此为止。
- 边界：只查 `.bat`/`.cmd` 里"目录展开 + 引号"这一类；`%VAR:~0,-1%` 之类剪切写法是否真的
  剪对了、PowerShell 自己那套引号规则、以及 `start`/`pushd` 之类的边缘行为都**没有**覆盖。

### 5.12 `dev_testrun coverage: true`：覆盖率从"没装"变成真数字（Wave21）

- **为什么不是又一个 stub**：早先这条只能回「本仓没装 coverage/pytest-cov，`coverage: true`
  回未启用——那是【没测】，不是 0%」。那是当时最诚实的选择，但它留了个洞：
  **没有任何测试能回答「我这次改动覆盖到了吗」**。依赖补上（`requirements.txt` 新增 `coverage>=7,<8`）后
  这条走 `agent_runtime/code_coverage.py` 真跑。
- **主输出不是百分比，是「你改的文件里哪些行没被执行过」**。全仓覆盖率是会骗人的数字：
  一条 87% 的横幅可能完全没碰到你刚写的三行分支。所以 `targets:` 圈定范围 + 逐行回报，
  `total_percent` 只是附带。
- **两个必须记下来的实测坑**：
  1. **报告必须写文件，不能走 stdout**。`coverage json -o -` 在本仓产出 200KB+，
     而 `process_runner` 的缓冲只留头尾各 4000 字符 → JSON 被拦腰截断、`json.loads` 抛错，
     而截断后的尾部停在 JSON 中段，**肉眼完全看不出它被切过**。改成 `-o <tmp文件>`。
  2. **`coverage json` 不带 `--ignore-errors` 会整体失败**：仓里有一个语法坏掉的 `.py`
     （`tests/mutation/battery_head_check.py`，另一条 lane 的在途改动）时，它 exit=1、
     stdout 只有一行 `Couldn't parse …`，**一条 JSON 都不吐**。加上 `--ignore-errors` 后
     它会跳过该文件、继续把其余文件的报告写完——但被跳过的文件既不算已覆盖也不算未覆盖，
     所以单独列进 `unparsed_sources` 并把 `complete` 置假。
- **「未知」与「很高」必须分开**（这个仓库最贵的一类教训在这里最容易复发）：
  报告读不出来、给的改动文件不在报告里、`--source` 圈错导致 0 条语句——三种都判
  「覆盖率未知」，绝不折算成 0% 或 100%。`excluded_lines` 不算未覆盖。
- **覆盖率低不是「测试失败」**：`test_run.run` 里覆盖率是**判定之后**单独跑的，
  `checks` 的结论不受它影响；`LOW`（70%）只提示一句。
- 测试：`tests/test_code_coverage.py` 31 项 + `test_test_run.py` 原有用例全过。
- 边界：只量「哪些行被执行过」。**一行被执行 100 次也可能断言是错的**——覆盖率满了
  不能替代 `dev_testrun` 的通过判定。也不含分支覆盖率。

### 5.13 `dev_probe`：给后端与命令行一条真凭据（Wave21）

- **补的是哪半边**：`preview_project` / `dev_page_action` / `dev_vue` / `dev_apptest`
  整段观察能力都长在浏览器上。对纯后端项目、一条 CLI、一个 worker，它们一样都够不着，
  于是那段闭环退回「`run_command` + 一屏文本由模型自己读」——而「没看到报错」会被读成
  「没报错」、「跑完了」会被读成「跑对了」。
- **`mode: http`**：只允许回环（复用 `dev_server.loopback_url`，与 `dev_serve`/`dev_page_action`
  同一道边界）。回状态码 / 响应体形状 / 耗时，并把 JSON 里的错误字段单独拎出。
  **5xx 有两个意思且必须分开报**：既是「服务确实在应答」的凭据（把它当"没应答"会让 Agent
  去重启一个好好在跑的服务），也是「这次不该通过」的证据。404 不算 server error。
- **`mode: cli`**：**除了退出码**还回 ERROR/CRITICAL 级行、真 traceback、
  `NameError: name 'x' is not defined` 的名字、以及「输出为空」这件事本身——
  退出码 0 的一个什么都不打印的 CLI 和一个真干了活的 CLI 在退出码上无法区分
  （`allow_silent: true` 可以为真静默的 CLI 开口子）。
- **命令解析与安全门复用 `run_command` 那一套**（注入 `_shell_argv` / `_cmd_is_blocked`）。
  这一条是**安全要求**不是风格问题：自己 `text.split()` 会把 `-c "print(1)"` 拆成三段
  （引号原样留在参数里，命令"跑了"但跑的不是你写的东西），更重要的是会**绕开那道黑名单**。
  测试里有源码级断言盯着接线（`SafetyTests::test_the_tool_passes_the_repo_argv_and_gate`）。
- **误报比漏报更贵**：正文里出现 "error" 字样的正常输出（`fix the error handling path`）
  **不算**错误行——只认行首等级标记。一个天天误报的检查用两次就会被关掉。
  access log 那种每行带随机端口的噪声折掉，不挤掉真正的错误。
- 测试：`tests/test_svc_probe.py` 36 项（含起真 `ThreadingHTTPServer` 的现场层）。

### 5.14 `dev_deps`：把「本地能跑、干净装不上」补上（Wave21）

- **要补的那一段**：`dev_headcheck` 能**发现**「提交引用了没声明的依赖」（它把 HEAD 挂成
  干净检出真 import 一遍），但发现之后**没有工具能修**——从「知道缺了」到「补上」之间是断的。
  而这正是 vibecoding 最常见的死法：本地因为恰好装过而一切正常，别人 clone 或 CI 拉干净
  检出时第一句 import 就 `ModuleNotFoundError`。
- **映射靠证据，不靠猜**（按可信度）：`importlib.metadata.packages_distributions()`
  （**本机实际装着的元数据**，最硬——实测 `websocket`→`websocket-client`、`webview`→`pywebview`、
  `yaml`→`PyYAML` 全靠它）→ 已声明依赖的 `Provides-Extra`（`psycopg[binary,pool]` 带出
  `psycopg-pool`）→ 内置查表 → 同名（标成**弱证据**并注明本机没装）。
  都不确定就归 `unknown` 并明确报出来，**绝不猜一个名字写进清单**——猜错等于装了个
  不相干的包而真正缺的那个还缺着。
- **对账用 `ast` 不用正则**：注释里的 `# import numpy` 与字符串里的 `"import os"`
  必须是两回事。正则扫全文会建议往 requirements.txt 里塞标准库。
- **本地包不误报**，而且是**逐个导入方**判的（`resolvable_locally`）：`python game_demo/main.py`
  会把 `game_demo/` 放进 `sys.path`，所以 `from starvoyager.config import …` 是项目内代码。
  这条误报**实测出现过**（把 `game_demo/starvoyager` 报成"缺一个叫 starvoyager 的发行包"）。
  同一个模块对 A 文件是本地、对 B 文件可以是真第三方。
- **写操作只有一处**：往**已存在**的清单追加缺失行（每行带"谁 import 了它"的注释），
  不改版本、不删行、不重排——版本是人的决定。没有依赖清单时**不替你新建**。
- **fail-closed**：解析失败的 `.py`、被扫描上限截断、没有依赖清单——三者都让报告
  **不能**被读成「依赖没问题」。「声明了但没扫到 import」只作**线索**列出
  （pytest/ruff 是命令行用的，不算多余依赖），不影响判定。
- **在本仓实测抓到 11 个真问题**（未修，按 §7 规矩不代改别人的文件）：
  `api.py` 直接 import `starlette`（30 个文件在用）/`pydantic`/`anyio` 却只声明了 `fastapi`、
  `secrets_store.py` 的 `cryptography`、`data_formats.py` 的 `PyYAML`、`make_shortcut.py` 的
  `pywin32` 那一族、`agent_runtime/retrieval.py` 的可选 `sentence-transformers`。
- 测试：`tests/test_deps_sync.py` 31 项。

### 5.15 `dev_handoff`：把交付补到门口（Wave21）

- **`dev_propose` 到草稿就停了**：产物躺在状态目录里，人还得自己决定分支名、自己写
  commit message、自己确认「改动里有没有混进别人的在途 WIP」。本工具算**决策材料**：
  建议分支名、建议 commit message、「这次提交会包含哪些文件」与规模。
- **仍然不 add / 不 commit / 不 push / 不切分支**。只跑只读 git（`rev-parse`/`status`/`log`，
  `diff` 走 `git_diff_preview`）。测试里有两道锁：源码级断言（传给 `_run_git` 的子命令必须
  全在白名单里，外加"模块里不得出现任何写类子命令的字面量"）+ 一条真的跑一遍然后比对
  HEAD 与 `git status` 前后完全一致（`analyzing_does_not_change_the_repository`）。
- **共享文件必须点名**：非 `tests/` 非 `.md` 的主干文件是 `git commit -- <路径>` 出事的
  高发区——本仓 `b828d10` 就是这么把别人 ~457 行在途 WIP 扫进提交、导致 HEAD 单独检出
  `import agent` 失败的。所以输出里直接点名并给出 `git diff --cached --stat` 的复核提醒。
- **未跟踪文件不说成"已交付"**：它们不在 `git diff` 里，会被单独列出并标明
  「**不会**被这次提交带上」。只有未跟踪文件时给的是**另一种**状态（不是"没有可提交的东西"——
  那样读起来像"这轮白干了"，而实测新建一批模块后正是这个误报）。
- **按仓库自己的习惯写**：先读最近 12 条提交标题判断该不该加 `type(scope):` 前缀；
  用户标题里已有的 Conventional 前缀**剥掉再拼**（否则 `feat: x` 会变成
  `chore(harness): feat: x`，分支名变成 `fix/harness-fix-x`——两个都实测踩到）。
  中文标题不会产出空分支名。
- 测试：`tests/test_handoff.py` 33 项（自带临时 git 仓库，不用共享工作树当断言依据——
  否则会因**别人**的改动而变红）。

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

### 5.18 Wave24：深链整页断言 + `/api/chat` 的 SSE 真问答链路（今天零覆盖的那两块）

**① 深链整页（`dev_apptest deep_link: 1`）**

- **补什么**：`dev_apptest` 早就有 `page:` 参数，但它只是"开哪一页"；**没有**任何断言证明
  "这一页真的渲染了自己"。而本仓三条页面路由（`/` → `web/index.html`、`/workbench`、
  `/trace`）各自独立、**没有 catch-all**（`api.py:3858` / `3863` / `3636`），所以谁把
  `/trace` 接到 `index.html` 上，整页守卫照样 14 条全绿。
- **判定方式**：三条路由的**挂载点互不相交**（`.ask-shell` / `.wb-shell` / `#tablewrap`），
  所以"这一页渲染了自己"和"它没渲染别人"是可判定的。`page_action` 的 `text:` 定位只在
  **可交互元素**里找，`AI 操作记录` 那个 `<h1>` 实测命中 0 个（Wave17 的学费），所以标记
  一律用 CSS 选择器。
- **每条路由一个新会话**：复用同一个页面 navigate 就是**站内导航**，而深链要的正是冷启动。
- **两个 `.html` 别名**（`/workbench.html`、`/trace.html`，307）还要求**停在规范路径**上——
  实测浏览器跟完 307 之后 `href` 就是 `/workbench`（连 `page_action` 加的 `?docmind=`
  缓存戳都掉了），所以"标记在"之外再加一条"落点对不对"。
- **反向对照**：站内不存在的路径**必须不渲染任何一页**。没有它的话，一个把所有路径都回成
  `index.html` 的 catch-all 会让上面那些断言**全部照样绿**。
- **一条自己招的恒真判据**：反向对照那一行起初传了空的 `foreign`，`any([])` 恒为 False，
  于是"没有 catch-all"这条唯一的凭据永远绿。改成必须数**三页的标记**，并立了一条元测试
  盯着"反向对照到底问了几个标记"。
- **`_foreign_for()` 从同一张表推**：`DEEP_LINK_ROUTES` 起初手写了 `foreign`，而 `_foreign_for`
  又推了一遍——加第 4 个页面时手写那份会静默变陈旧（§9「上限写两处」）。现在只写 `own`。
- **未知路径的 404 不算进"console 干净"**：实测未知路径就是 1 条 console 错误 + 1 条失败
  请求（404 本来如此）。这条判断抽成了纯函数 `_console_matters()`，所以它在 CI 上可验，
  不必依赖真浏览器。
- 产物：`tests/test_app_test.py` 新增 `DeepLinkTests` 15 项（纯层）+ 1 条现场用例；
  `battery_app_test` **46 → 56 条变异**（新增 10 条深链，`--no-live` 下 9 条被纯层抓到、
  1 条记未验证）；门禁 +1 题见 ②。

**② `/api/chat` 的 SSE 真问答链路（`agent_runtime/chat_sse.py`）**

- **先纠正前提**：任务里写的 `/api/ask` **不存在**。问答页真正打的端点是 **`POST /api/chat`**
  （`api.py:2100`，`frontend/src/workbench/api/chat.ts:36`；`askGrounded` 只是函数名）。
- **缺口的确切位置**：`/api/chat` 早有 6 条用例，但它们**全部**在 `Agent.run` 那一层打桩
  ——也就是说经由这个端点，`agent.py` 的 ReAct 主循环、`llm.py` 的 provider 分派、
  `tools.py` 的工具执行**一条都没真跑过**。
- **为什么不能直接用 `provider="mock"`**（先试了，量出来的）：mock 把 ReAct 循环与工具都
  跑起来了（实测 345 个 chunk、332 个 token 事件、两次 action/observation），但**结构性地
  结束不了一轮**——`_mock_chat` 的「拿到观察 → 作答」分支（`llm.py:1119-1122`）吐的是
  **不带 `Final Answer:` 标记的裸文本**，而 `agent.py:3443` 的格式失守判定会连拒两轮，
  然后发一条 `final(status="error")`。**即 mock provider 无法完成一次问答**——产品侧的
  离线演示模式同样跑不完。`llm.py` 正在被别人改，本轮**只报不改**。
- **改走可注入的剧本 LLM，仍零共享文件改动**：走 `agent_mod.LLMClient` 这个**构造接缝**
  （`agent.py:1703` 的 `self.llm = llm or LLMClient()`），于是请求内**任何**新建的 Agent
  都会拿到剧本对象。**不要**自己去猜 `_agent_for` 的缓存键——请求内外的 `_ctx_project_id()`
  可以不同，键一错就换到了另一个 Agent 上（实测那样会**静默用回 mock**）。
- **「收到 final」不等于「跑通一轮」**：`agent.py` 里有**六条不调模型也能产出一个 `final`**
  的早退路径（容量兜底 `offline` / 歧义追问 `clarification_required` / 强制收尾 `degraded` /
  格式失守 / 截断 / 任何异常 `status="error"`），判据逐条把它们挑出来。另有两条协议层的
  假象：`route` 与 `done` 是**无条件**发出的（`api.py:2349`/`2430`），所以"收到了 route + done"
  零信息量，判据**不把它们算作证据**。
- **凭据设计（照 §5.10）**：`final` 必须**原样**带着某条工具观察里的一截（任意 12 字窗口）。
  刻意**不**复刻 mock 的取片段方式——照抄会把判据和被测的桩绑成同一份代码，桩改格式判据
  跟着改、照样绿。
- **零网络的三个前提**（缺一个就红，且红的原因写清）：① 压 `llm_provider`；② **还必须**压
  `embedding_provider`——分支条件是 `_prov == "ollama" or _emb == "ollama"`（`api.py:2168`），
  而本仓 `.env` 把 `EMBEDDING_PROVIDER` 设成了 ollama，只压前者照样走 `check_ollama` 的真
  socket 探活；③ 断言**没有** `web_search`、观察里**没有**「未找到」（mock 看见「未找到」就
  改调联网）。用例直接断言 `check_ollama` **零次调用**、剧本 LLM 真的被调用过（被云端升级
  覆盖掉的话它是 0 次）。
- **一条自己招的恒真判据（同型）**：`_tool_name` 起初读 `payload["tool"]`，而真实 `action`
  事件的工具名在 **`text`** 里（`"search_assets(像素风角色…)"`）——于是永远拿到 `None`，
  "没调联网工具"那条判据恒真。现在从文本里解析，且**解析不出来也算红**（"没读出来"≠"没有工具"）。
- 产物：`agent_runtime/chat_sse.py`（新模块，纯函数：`parse_events` / `verdict`），
  `tests/test_chat_sse.py` **30 项**（真打一次 `/api/chat` 的 5 条 + 判据表 17 条 + 解析 8 条），
  `battery_chat_sse` **20 条变异 / 全抓 / 6 秒**（几乎全在纯层，所以 **CI 上跑得到**）。
- **边界**：不证明厂商行为、不证明模型理解了问题；剧本 LLM 是**注入**的，不是生产选
  provider 那条路（生产那条路没有对称的注册表可挂——`llm.py` 的 provider 选择是
  「`or` 链 + 字典查表」，没有 `register()` 钩子）；`RealTurnTests` **不作为电池的层**
  （它验的是"真链路满足判据"，不是"判据会不会红"，且它在常规 pytest 里就会跑到）。

**③ 一条只有本机能看到的时序敏感（不是本轮的代码问题）**

真浏览器那档本机**偶发**红，两种症状：`/api/health` 被前端自己的 2.5s 探测超时掐成
`ERR_ABORTED`（`no_failed_requests` 因此红）、以及 `页面加载超时`。**原有的**
`LivePageTests::test_the_real_page_passes_every_line` 一样红，所以不是本轮的改动引起的；
按 §9 的老规矩，读这条红之前先看同一时间有没有别的 lane 在起浏览器。

### 5.19 Wave25：把"浏览器坏了"查成一条可以量出来的读数

**症状**：Wave24 交付后真浏览器那一档 3 红 / 4 绿，而且红了**没有信息量**——
`rows: 0`、`console_errors: []`、`failed_requests: []`。我当时的结论是"本机浏览器那一档
现在是坏的"。**这个结论两个词都错**：它没坏，也不是纯环境问题。下面是量出来的东西。

- **`open_page` 返回 ≠ 页面数据到位**。`open_page` 的就绪判据是
  `document.readyState === 'complete'`，而工作台把 UI 门控在 `GET /api/config` 上。
  实测时间线：`open_page` 在 **1903 ms** 返回、文件树到 **5160 ms** 才出现——晚 3.3 秒。
  夹具在 `open_page` 返回后**立刻**读 `rows`，就是在跟这段时间赛跑：数据先到就绿、
  晚到就红。**修法**：读 facts 之前加两级等待（`.wb-shell` → `.ft-row`，各有截止期），
  并把 `shell_waited_ms` / `data_waited_ms` / `data_ready` 上报（红了能直接看出是"没等到"
  还是"等到了但内容不对"）。**顺带**：`_drive` 里本来就有 `wait_for(".wb-shell", timeout=5.0)`，
  只是写在了读 facts **之后**——等待不是没有，是放错了地方。
- **`/api/config` 冷启动 6.1 秒**（本机实测；热了 15 ms）。拆开看：`check_ollama(force=True)`
  自己只要 **2.0 秒**（`Ollama` 未运行时的探活，`check_ollama` 的文档注释早就写了
  "可能耗时数秒"），所以**另约 4 秒来源未定**——`api.py` 是别人在改的文件，本轮只报。
  这条冷路径也是"文档加载完"与"数据到位"之间那 3.3 秒的来源。
- **`/api/health` 那条 `ERR_ABORTED` 不是服务端慢**：health 单独 15 ms，**6 条并发慢
  `/api/config` 在飞时也只 24 ms**（我原以为是浏览器每主机 6 连接的饥饿，实测被推翻）。
  它是前端自己按设计取消的——`probeBackend()` 带 `AbortController`（4 s）并在 `catch` 里
  `return false`（`frontend/src/workbench/composables/demo.ts:13-31`），只在页面加载慢的
  那几次才会触发。
  **修法**：把浏览器报的失败请求**分类**成"真失败"与"客户端自己取消"（`ERR_ABORTED`），
  判据只对前者变红，两堆**都进报告**（`failed_requests` / `cancelled_requests`）。
  静默扔掉就叫假干净，而把客户端取消也算失败则是假红——本仓刚为"确定性红教人忽略门禁"
  付过账。
- **深链那档给"页面加载超时"加了一次记账式重试**：`open_page` 的就绪预算是**硬编码**的
  `120 × 50ms = 6 秒`（`page_action.py` 的脚本里，不是参数；那个文件现阶段属别人，不动），
  而 `open_page` 会给 URL 加 `?docmind=<ns>` 缓存戳，于是每条路由都要重下整套资源
  （含 vendor 与 model-viewer）。最重的 `/workbench` 那几条偶尔顶破 6 秒。重试次数记进
  `deep_retried_routes`（**不是掩盖**），三次都超时就红，且错误里点名是哪条路由。
  **第一版把 `deep_retried_routes` 只在成功路径末尾赋值**，一抛异常连"是哪条路由超时"
  都看不到——所以改成先把同一个 list 对象挂进 state。
- **验证**：现场类 **7/7 通过**（141.29 s，含先前红的两条）；纯层与元测试
  **121 passed / 486 subtests**；七套确定性 **254 passed / 572 subtests**；
  十一块电池 `--no-live` **277/294 + 17 未验证 + 0 活下来**；
  `battery_app_test` 56 → **58 条变异**（新增两条把"分类"两个方向都钉住：全算失败 = 假红、
  全算取消 = 假干净）。
- **边界**：`/api/config` 那 6.1 秒里约 4 秒**来源未定**，本轮没有继续查（`api.py` 属别人）；
  客户端取消的那类请求被排除在 `no_failed_requests` 之外，这是一个**有意的收紧**——
  代价是"被客户端掐断的真实失败"也可能落进 `cancelled_requests`，所以它必须上报、
  不能静默丢。

### 5.20 Wave26：`llm_stub` —— 让问答链路在零网络下走到**生产 provider 那条路**

- **它补的是 §5.18 ② 漏掉的一层**：`tests/test_chat_sse.py` 的真回合是往 `api._agent_for`
  的 Agent 上**注入**剧本 LLM——那验的是 ReAct 循环、工具执行与 SSE 包装，但**不经过**
  `llm.py` 的 provider 选择、`base_url` 解析与 OpenAI SDK 那条真实客户端路径。而
  `dev_apptest` 把 uvicorn 起成**子进程**，注入法在那里根本用不上。
- **做法与 Wave19a 的 `realtime_scripted` 同型**（一个只在显式选中时才生效的测试双）：
  `llm.py:567-571` 对 `provider == "custom"` 会采纳 `LLM_BASE_URL`，所以只要把
  `custom` 指到 `agent_runtime/llm_stub.py` 起的回环假服务上，整条链路就走**产品里的正式
  provider**——`llm.py` / `api.py` / `config.py` **一个字没动**。
- **差一点走错的那一步**：进程内用 `os.environ` 设 `LLM_PROVIDER` **没用**——
  `config.LLM_PROVIDER` 是**导入时**读的（`.env` 里是 ollama），导入之后再设太晚，
  现象是"去连 11434 报连接被拒"。进程内必须 `config.set_runtime(...)`；`env_for()`
  是给**子进程**用的（那边才是导入时读）。
- **假服务的形状照 `llm.py` 的消费方式写**（`llm.py:274-295`）：每片
  `choices[0].delta.content` 带一小段正文；末尾一个 `choices` 为**空**的分片带 `usage`
  （`if not chunk.choices: continue` 正是为它准备的）；最后 `data: [DONE]`。
- **一条自己招的坑，已立测试盯着**：`observation_snippet` 起初在**所有**消息里搜
  `Observation:`，而**系统提示里本身就有一大段解释 ReAct 格式的文字**（含这个词），于是它
  把提示当成工具结果、第一轮就作答、**一个工具都不调**。`_mock_chat` 用的是"只看最后一条"
  这条窄规则（`llm.py:1108`），照它改才对。`tests/test_llm_stub.py` 里有夹具显式带着那段
  格式提示，专门钉这条错法。
- **产物与验证**：`agent_runtime/llm_stub.py`（纯函数 `reply_for` / `sse_frames` /
  `observation_snippet` / `env_for` + stdlib 回环服务），`tests/test_llm_stub.py` **14 项**
  （纯 12 + 真起服务 2 + 真打 `/api/chat` 的 provider 路径 2）。真回合实测：
  `check_ollama` **零次调用**、工具是 `search_assets`、`final` 原样引述了目录里的
  `· Pixel Platformer ［2d-sprites｜CC0］…`，`chat_sse.verdict` **14 条判据全绿**。
- **为什么这块没配变异电池**：本仓那条规矩是"**新守卫**必须配一条变异电池"，而
  `llm_stub` 是**测试双**不是守卫——它喂的那个守卫（`chat_sse.verdict`）已经有
  20 条变异的 `battery_chat_sse`。所以这里给的是测试，不是电池；这一点是**有意的选择**，
  写在这里免得后来人以为是漏了。
- **边界**：它不证明模型理解问题、不证明厂商行为；`custom` 是产品里的正式 provider，
  但"用假服务冒充它"这件事本身是测试手段——生产上没人给 `LLM_BASE_URL` 指回环地址；
  **页面那一层（问答页的输入框 + DOM 断言）仍没做**，见 §6 缺口 15。

### 5.21 Wave27：`dev_apptest ask: 1` —— 问答那一档真的走完一轮（缺口 15 的下半）

- **补什么**：§5.18 ② 只把 `/api/chat` 的真问答链路在 **HTTP 层**验过（而且它**注入** LLM），
  §5.20 的 `llm_stub` 把"注入"换成了**生产 provider 那条路**——但两处都还在 HTTP 层。
  **页面那一层**（页面 → `/api/chat` → ReAct → 工具 → DOM）一直没覆盖。
- **怎么点进那一轮**：输入区在子组件里（`AskApp.vue` 只渲染 `AskMessage`），所以不挖输入框，
  直接点**建议问题按钮**（`button.suggestion`，`@click="send(s)"`）——点一下就发一轮。
  代价是问题文案不由我们定，所以**凭据不能依赖问题内容**。
- **凭据设计**：服务端用 `llm_stub`（`provider=custom` + `LLM_BASE_URL` 指回环）当假模型，
  剧本第一轮调 `list_dir(.)`，于是观察里带着**那一次运行独有的随机夹具文件名**
  （`probe_<6位>.py`，见 `make_sandbox`）。回答里出现它，就只能解释为整条链路真跑过
  ——照 §5.8「夹具是随机命名的文件」那条。实测三次运行拿到三个不同名字。
- **三条被实测纠正的形状**：
  ① `click` 多命中时要 `nth`（问答页有 3 个建议按钮，不给就是"命中多个"直接抛）；
  ② 引述的**不能按位置挑**——`list_dir` 的**首行是表头**（`目录 (代码库根) 共 1 项：`），
     而观察的**末行常常是 harness 的提示**（`（请基于观察继续，或给出 Final Answer）`）。
     第一版先取首行、再改末行，各错一次；正解是**按动作找那类工具的输出行**（`_LINE_HINTS`）；
  ③ `_drive_ask` 的 `open_page` 起初写在 `try` **外面**——它一抛（问答页资源重、实测顶破
     `open_page` 那 6 秒就绪预算）`finally` 就不执行，报告里只剩"凭据: None"，连超时还是
     没挂上都看不出来。
- **桩要听懂 harness 的纠正提示**：第一版不认识 `Evidence check:` / `Reflection:` / `Nudge:`，
  于是一直回 `Action: list_dir`，一轮问答跑了 **6 轮**，最后靠"模型反复空转"的兜底停，
  渲染出来的是兜底证据块而不是一条干净的答复。让桩在这三类提示出现时**就着已有证据收尾**
  之后，一轮降到 **3 轮**，页面上的答复是干净的
  `根据检索到的内容，[FILE] probe_6f6c8f.py —— 以上就是可用清单。`
- **难度分级**：`ASKTier` 的判定表 8 条（外壳挂载 / 点中 / 问题被回显 / **凭据出现在回答里** /
  不是失败文案 / 没被截断 / console 干净 / 无失败请求）。失败文案那条与 §5.18 ② 同款：
  `HTTP 200 + 有回复 ≠ 成功`。
- **产物与验证**：`tests/test_app_test.py` 新增 `AskTierTests` **9 项**（纯层）+ 1 条现场用例；
  `battery_app_test` **58 → 66 条变异**（新增 8 条把那一档的两个方向都钉住，含
  "空凭据也算通过"这种恒真形状）；`tests/test_llm_stub.py` **14 → 18 项**。
  实测：页面档三轮跑完、`failed: []`、凭据三次运行三个不同名字；八套确定性
  **282 passed / 583 subtests**；十一块电池 `--no-live` **285/302 + 17 未验证 + 0 活下来**。
- **边界**：它证的是"页面把一轮问答跑通了"，**不**证模型理解问题、不证答案对不对；
  问题文案是页面自带的建议问题（不是我们选的）；`llm_stub` 是测试双，生产上没人给
  `LLM_BASE_URL` 指回环地址。

### 5.22 Wave28：把两条"报出来不动手"的缺口修掉（别人的模块，但已入库）

这两条先前只记账（§6 缺口 14 / 16），因为文件在别人手上。按用户口径，这一轮动手修。

**① 缺口 14：`mock` provider 收不了一轮问答**（`llm.py` 的 `_mock_chat`）

- 它「拿到观察 → 作答」那一支吐的是**裸文本**（既没 `Action:` 也没 `Final Answer:`），
  而 `agent.py:3443` 的格式失守判定要求二者有其一 → 连拒两轮 → 发 `final(status="error")`。
  **另两支（素材/知识库）都带 `Thought:` 与标记，只有这支漏了。**
- 后果不只是"测试不好写"：`mock` 是本仓**唯一零网络的模型路径**，也就是离线演示模式
  （没装 Ollama、没有 key 时用户看到的那条路）——它同样跑不完一轮。
- 修法一行：加 `Thought: … ` + `Final Answer: …` 前缀。验证：`/api/chat` 走 mock 打一轮，
  `final.status` 不再是 error、`chat_sse.verdict` 全绿、而且**第一条工具调用还在**。
- 一条自己写错的断言：起初我还断言 `final` 文本里应含 `Final Answer:` ——
  那是**被 agent 消费掉**的标记，`final` 事件里是它之后的正文。改成断言
  "mock 的答复真的到了客户端"（含 `演示模式·mock LLM`）。

**② 缺口 16：`/api/config` 冷启动 6 秒**（`api.py` 的 `_OLLAMA_CHECK_TTL`）

- **根因是环境，不是代码逻辑**：本机对**关闭的回环端口**做一次裸
  `socket.connect(("127.0.0.1", 11434))` 要 **2035ms**（正常应 <1ms；`urllib` 走同一条路
  2071ms）——这台机器上有东西在拖回环连接。所以任何"探活 Ollama"的代码在 Ollama 缺席时
  都要付 2 秒。
- **6.1 秒的账算清了**（逐项计时，不是估）：`_live_capability` **4081ms**（首次构造
  `LLMClient` 时它自己的实时探活）+ `check_ollama` **2030ms** + `_presets_payload` 11ms
  ≈ 6122ms，实测 6144ms。
- **病根是 TTL 与探活同量级**：`_OLLAMA_CHECK_TTL` 原值 **2.0**，而探活本身约 2 秒
  （HTTP 超时写死 3 秒），`ts` 又是在探活**结束**后才写——隔两三秒重开面板就必然重付。
  原注释里作者已经知道"Windows 上一次被拒也要 ~2s"，但选了 2s 认为"足够新鲜"，
  没意识到**缓存窗口小于操作耗时等于没有缓存**。
- 修法：TTL 2.0 → **30.0**，并把上面这些数字写进注释。代价写明：启动 Ollama 后最多 30 秒
  才看到状态翻转；要即时结果的地方仍可 `check_ollama(force=True)`（`GET /api/ollama/probe`）。
- **可重跑的对照（同进程 A/B，缓存从"已过期"起算，隔 3 秒再打一次）**：
  `TTL=2.0 → 2047ms`（又探一次）／`TTL=30.0 → 7ms`（命中缓存）。
  首次冷启动**仍是 ~6.2 秒**（那份是 `LLMClient` 首次构造的一次性探活，每个进程一次，TTL 帮不到）。
- 产物：`tests/test_ollama_probe_cache.py` **4 项**（钉住"TTL 必须大于一次探活的预算"
  这条不变量，以及"窗口内第二次不再探活"这条**行为**判据——只断言常量不够，
  `ts` 写在哪、`force` 怎么绕都能让一个"看着够大"的 TTL 形同虚设）。

**③ 为什么这两条现在能修**：`llm.py` 与 `api.py` 原本是别人未提交的在途改动（按纪律只报不改）；
另一条 lane 做了 `e651c9c chore: 归集多lane 的在途改动到仓库` 把它们**入库**之后，
两个文件的工作树是干净的——"别动别人的在途改动"这条约束在**未提交**这个意义上不再适用。
改动仍保持最小、且在两处注释里写明这是别人的模块。

- 回归：`test_ollama_probe_cache / test_chat_sse / test_llm_stub / test_format_stall /
  test_api_routes / test_api_model_config / test_llm_resilience / test_model_capability /
  test_empty_provider_response / test_dev_eval / test_head_check` **244 passed / 41 subtests**。

### 5.23 Wave29：`run_isolated` 的子进程输出不再"整条消失"（缺口 13）

- **实测的形状比 Wave23 记的更糟**。原来 `run_isolated`（以及 `enterprise_sandbox` 的非
  Windows 分支）都是 `text=True` 且**不给 `encoding`**，按**本机区域编码**解码（本机 cp936）。
  子进程只要吐一个非 GBK 字节，`Popen` 的读线程就抛 `UnicodeDecodeError`，
  `communicate()` 把那一路返回成 **`None`**：

  | 子进程 | 改前 | 改后 |
  |---|---|---|
  | ASCII | `rc=0 stdout='adapter-output\n'` | 同左 |
  | UTF-8 中文 | `rc=0 stdout='' stderr=''` | `rc=0 stdout='适配器输出\n'` |
  | 只写 stderr 的中文 | `rc=0 stdout='' stderr=''` | `rc=0 stderr='适配器 stderr'` |
  | 非 UTF-8 字节 | `rc=0 stdout='' stderr=''` | `rc=0 stdout='�� bad'` |

  唯一线索是一个打印到控制台、**调用方永远看不见**的后台线程异常。
- **`rc=0` 配空输出**最危险的地方是会被读成"跑成功了但没输出"。查了两个调用方：
  `generated_adapter_runtime.py:314` 直接 `json.loads(stdout)`，被 `json.loads('')` 抛错
  堵住了；`enterprise_sandbox.py` 只转发。所以今天**没有**"静默成功"，真实的危害是
  **错误信息误导**（上层只看到「适配器没有返回合法 JSON」）+ **排查困难**（真实原因丢失）。
- **取舍写明**：固定成 `utf-8` + `errors="replace"`——不匹配的字节变 U+FFFD 而不是丢掉
  整条流，上层至少能判断"有输出、里面有乱码"。代价是本机真按 GBK 输出的子进程会看到
  少量替换字符，但**内容长度与结构仍然可见**，远好过整条流消失。
- **两条分支必须用同一套**（`enterprise_sandbox` 的非 Windows 分支也改了），否则跨平台
  复现出来的现象会不一样——有一条测试专门钉这个。
- **产物**：`tests/test_windows_sandbox.py` 的 `RunIsolatedOutputTests` +4 条（并入同一
  测试文件而不是新开一个：一块电池只有一个 `TEST_MODULE`，放外面那几条变异就没有类能抓）；
  `battery_windows_sandbox` **25 → 28 条变异**；`battery_chat_sse` 补注册
  `MockProviderTests`（Wave28 加的那个类忘了登记，元测试当场报"没有落到某一层"）。
- 一条自己写歪的变异：`SUBPROCESS_ENCODING = "utf-8"` → `locale.getpreferredencoding(False)`
  看着好，但 `windows_sandbox` **没有 import locale**，变异后模块装不进 —— 按 §9 的规矩
  装不进来的代码证明不了任何判据有牙（那条记成"这块电池的问题"）。换成
  `= "ascii"`（同样表达"固定 vs 推断"，而且装得进）。

### 5.24 Wave30：`dev_serve` 的端口归属要可证（缺口 4，以及它旁边那个假绿）

- **缺口 4 字面写的"就绪失败时换端口重试一次"，在当前架构下做不到**：`dev_serve` 传的
  `argv` 是 `_shell_argv(command)`（`tools.py:6718`），**里面没有端口参数**。端口是子进程
  自己选的，我们换了对它毫无影响。所以这一条按"字面实现"是空转的。
- **动手时挖到更要紧的一件事**（确定性复现，**不需要任何竞态**）：用户传 `port: X`，
  而 X 上已经有别人的服务时，`probe` 成功、`start()` 就**把别人的服务当成自己的**报就绪，
  并把那个 URL 交回去：

  ```
  占住端口: 60658（上面跑着一个不属于本项目的服务）
  ok=True  ready=True  url='http://127.0.0.1:60658/'  notes=['就绪探测：HTTP 200']
  ```

  它起的那个服务**根本没打印任何端口**，却因为"我们让它去探 60658"就被认领了。
  后面所有整页断言于是都打在别人的服务上。
- **修法：端口归属改成可证的**。
  ① **先认日志里它自己打印的端口**——那是可证的归属（是本进程打印的）；认不到才退回
  调用方给的 `port`，而那只是"优先去探这里"。
  ② **启动之前**先问"这个端口有没有别人在听"；如果启动前就有人、而本命令又没认领任何端口，
  那么那个应答的**不是**本命令起的服务 → 不判就绪，并说清"请换一个端口或先停掉占用它的进程"。
- **顺带修一个认错地址的老 bug**：`parse_port_hint` 原来用 `re.search` 取**第一个**端口行，
  而撞了端口的 dev server 会往后挪（vite 5173 → 5174），日志里**两个地址都在**——第一个
  往往是**没起来那个**。改成取最后一个。
- **我自己写歪的三处**（都被实测或守���抓出来）：
  ① `busy_before` 一开始算在 `start_job` **之后**——那时端口上坐的正是我们自己的进程，
  `port_taken` 恒为 True，正向路径当场被误伤成 `ready=False` + "等了 8s 没探测到就绪
  （HTTP 200）"这种自相矛盾的报告。**顺序错了整条判据就反了**；
  ② 一条变异（去掉 `if not os.path.isdir(workdir)`）是**等价变异**：`process_runner.start_job`
  自己也会拒绝不存在的目录、文案几乎一样，去掉哪一层行为都不变。换成"报错要说是哪个目录"；
  ③ `BIND` 写成了 `"ds"`，而测试模块里的名字是 `dev_server` —— **和 Wave26 同一个坑**，
  纯层从头到尾看的是真模块（8/15 → 修好后 13/15）。
- **顺带修掉一个我这轮引入的交替失败**：`LivePageTests` 连跑时深链那条稳定红、单跑绿。
  逐场景连跑查出来是**交替**失���（第 1/2/5/7 轮三次重试全超时、第 3/6 轮正常），而
  `live_pages` 每次都是空的 —— 说明会话没泄漏，是**浏览器进程**还没退出。
  `close_page` 只是从 `_sessions` 摘掉记录、Edge 退出是**异步**的，而我第一版重试只
  `sleep(1.0)`，等于把一次失败放大成三次连续抢资源。改成每次失败先 `close_all()` 再按
  次数退避（2s / 4s），交替失败消失（7 轮连跑全绿）。
- **产物**：`tests/test_dev_server.py` **17 项**（该模块此前**零测试**）；`battery_dev_server`
  **15 条变异 / 全抓 / 50 秒**（新电池，纯层、不需要浏览器）；门禁 +1 题
  `devserver-refuses-to-claim-someone-elses-port`（7 条断言），基线 27 → **28 题**。
- **验证**：确定性 8 个文件 **152 passed / 397 subtests**；`LivePageTests` **8/8**（164 秒）；
  `battery_dev_server` 15/15、`battery_app_test` 63/66 + 3 未验证。
- **边界**：归属判据仍有**固有**的 TOCTOU 窗口（启动前空闲、启动后被别人抢走），这一档防的是
  "明显是别人的服务"，不是并发竞态；真正的端口重试仍需要能往 argv 注入端口，那要猜
  各家 dev server 的 flag（vite `--port`、next `-p`、webpack-dev-server `--port`），是产品决策。

### 5.25 Wave31：交付环"真网络"的首跑排障 + 启动器失败不再一闪而过

**为什么不是"推上去验"**：`git ls-remote` 现在也连不上 github.com:443（§2 记的是 10-02 可通），
网络侧完全不可达。所以做的是**首跑排障**：把"第一次 GitHub run 会因为什么而失败"里
**确定且可预防**的部分找出来。留下的预检脚本在 `.tmp/w31_preflight.py`，它查三件事：
① `.bat`/`.cmd` 的 blob 是否守住 `.gitattributes` 的 `*{bat,cmd} -text`（即 CRLF）；
② `.py` 的 blob 是否带 BOM；③ CI 里 `compileall` 那一步在本地等价跑一遍。

**它当场抓到一条**：`启动DocMind.bat` 的 blob 是**纯 LF**（254 字节、8 个裸 LF），
其余两个 `.bat` 都正确保住了 CRLF。来源是 `e651c9c chore: 归集多lane 的在途改动到仓库`
——那个文件被重写成纯英文（cp936 乱码问题因此消失），但行尾没跟着回来。后果是
`tests/test_launchers.py::LauncherEncodingTests::test_primary_launcher_prefers_current_source`
**现在就红**（`.bat` 用 `-text`，blob 原样检出，所以 ubuntu 上会一样红，与平台无关）。

**但用户报告"CRLF 的启动器我这边打不开"，于是去量了 —— 结果推翻了那条测试的立论**：
① 把 `启动DocMind.bat` 的结构原样搬进临时目录、callee 换成无害 stub，**CRLF 与 LF 行为完全
一致**（都 exit=0、callee 正常执行）；② 再测一个**带 `goto` 标签**的版本，**CRLF 与 LF 都能
正常到达标签**。也就是说 `.gitattributes` 里"仅 LF 会导致 cmd 找不到 goto 标签并闪退"那句
注释，在**这个 Windows 版本上不成立**。所以**恢复 CRLF 不会修好用户的启动器**，那条红只是
"CI 会红"，两件事被混在一起了。

**真正的闪退机制与行尾无关**：`启动DocMind.bat` 原来是
`call "%~dp0run_desktop.bat"` 紧接 `exit /b %ERRORLEVEL%`。而 `run_desktop.bat` 的三条失败
分支（`:missing` / `:no_python` / `:bootstrap_failed`）**都调了 `pause`**（双击时窗口停住），
**唯独成功路径**（`.venv\Scripts\python.exe desktop.py` 之后）没有 —— 于是应用自己崩掉时，
脚本结束、窗口立刻关掉，错误刷过去就没了。用 stub callee 实测（同一台机器、同一文件名）：

| | exit | 屏幕字符 | 错误细节在 | 窗口停住 |
|---|---|---|---|---|
| 现状 | 1 | 25 | 是 | **否** |
| 修后 | 1 | 87 | 是 | **是**（且明说 `Agent 工作台 exited with code 1`） |

错误文本两种情况下都打印了 —— 差别只在**窗口会不会在你读到之前消失**。

**改法（按用户口径：行尾一个字节没动，仍是 LF）**：在 `call` 之后接三行，把退出码存下来、
失败时先 `echo` 出来再 `pause`。成功路径**不能**被拖住，所以 `pause` 只在非零时执行，
并有一条测试专门钉这一半。

- **产物**：`tests/test_launchers.py` 新增 `LauncherFailureVisibilityTests` **2 项**（用 stub
  callee 真跑 `cmd.exe`，钉住"失败要停住并说出退出码"与"成功不许被拖住"）；
  `启动DocMind.bat` 254 → 744 字节（只加 5 行，含 5 行说明为什么）。
- **那条 CRLF 断言收窄了**（用户口径：不碰行尾）。原断言有一条**在本机复现不出的机制**
  ——带 `goto` 的批处理按 CRLF 与 LF 各跑一次，两种都正常到达标签；而被断言的
  `启动DocMind.bat` 根本没有 label（只有 `call`），LF 与 CRLF 实测行为完全一致。
  改成**按内容判定**：用到 `goto :label` 的 .bat 才要求 CRLF。真正会咬人的那类照样被抓住
  （`run_desktop.bat` 是唯一含 goto 的，本来就在 CRLF 上）。
  **同时修好它的第二个问题**：它后半段在这个文件里找 `if exist "%DOCMIND_PYTHON%"`，
  而那段内容在 `e651c9c` 之后已搬进 `run_desktop.bat`（主启动器变成薄包装）——
  原来它先被 CRLF 那条挡着，所以这个"找错文件"一直没暴露。
- **预检从脚本变成常驻守卫**（`tests/test_repo_conventions.py`，3 项）：扫的是 **git 里
  真实入库的 blob**，不是工作树（"入库时丢了"这件事只存在于 blob 里；扫工作树会在 Windows
  上因自动转换而看不出问题）。它守两件事：① `.py` 的 blob 不许带 BOM（`.gitattributes`
  写了 `working-tree-encoding=UTF-8`，而 BOM 会让 `ast.parse` 直接 SyntaxError；
  `7aaf365` 修过一次 `llm.py` 顶部残留的 BOM，说明会再犯）；② **守卫自己有没有瞎**——
  它断言自己看得见那三个 `.bat` 且**看得见非 ASCII 文件名**（第一版预检脚本就是被
  git 的路径转义整个跳过了 `启动DocMind.bat`，差点漏掉要找的东西本身）。
- **CRLF 规则不在新守卫里**：`test_launchers.py` 那条已经是它的家。第一版我在两处各写一份
  （一处收窄、一处全称），结果同一个文件一处绿一处红——**规则分散的代价不是"多写一次"，
  是"两份会分叉，然后你不知道该信哪个"**。新守卫改为**验证收窄后的规则确实还有牙**
  （构造一个"用 goto 却写成 LF"的坏例子喂给同一条判定）。
- **边界**：所有结论都来自**这台 Windows**；别的 Windows 版本上 `goto` 与行尾的关系我没有
  证据，所以只写"复现不出"，不写"LF 没问题"；`*.bat` 的 CRLF 约定在 `.gitattributes`
  里**没有被改动**。

### 5.26 Wave33：前端 eslint 那一腿的骨架（**只报不卡**，§6 缺口 5 的前半）

- **为什么它是缺口**：`dev_diagnostics` 的前端那条腿**只有 typecheck**
  （`diagnostics.py` 跑的是 `npm run typecheck` = `vue-tsc --noEmit && tsc --noEmit`），
  所以**类型**层面有人管、**写法**层面没人管。
- **为什么先"只报不卡"**：作用域是 `frontend/src` **129 个文件 / 39352 行**（71 个 `.ts`
  10486 行 + 58 个 `.vue` 28866 行，最大两个是 `AutonomousCockpit.vue` 3210 行与
  `SettingsView.vue` 2306 行），而 `node_modules` 里**连 eslint 都没有**（连传递依赖都没有）。
  贸然让它参与判定，第一天大概率是几千条；而几千条红会变成噪声——**比没有 lint 更坏，
  因为它让人误以为有人在管**。所以这一腿的发现进 `issues`/`notes`，**不进** `run()` 的
  `issues`、**不改** `ok`。等真装上 eslint、拿到存量数字，改一个常量就能切成卡。
- **没有配就明说没跑**，而且说清它不构成任何证据。`render()` 里有一句独立的
  「注意: eslint 这一腿**没跑**（…）。上面那些结论只覆盖类型检查，不覆盖写法。」
  —— 只挂在 notes 里会被淹掉，看不见就等于"假干净"。
- **接入形状照 typecheck 那条腿**：`frontend_lint_configured()`（有 `lint` 脚本**或**
  eslint 配置文件即算配了 —— 只看脚本会漏"依赖没装"，只看配置会漏"没接进 npm run"）、
  `parse_eslint()`、`frontend_lint()`，然后由 `frontend_diagnostics()` 调用。
- **`parse_eslint` 的一个坑**：eslint `stylish` 结尾那行 `✖ 3 problems (2 errors, 1 warning)`
  **不是条目**。不排除它，它会被读成一个"文件名就叫这个"的假条目。
  同样地，**出现在任何文件行之前**的条目行不能硬归属——那说明输出被截断了。
- **产物**：`tests/test_lint_leg.py` **12 项**（解析 5 + 配置检测 3 + 「没配就不许被当成跑了」2 +
  渲染诚实性 2）。门禁那两道 diagnostics 题都写死 `scope: py`，**不受影响**。
- **边界**：eslint 本身**没有安装**（装包需要批准外部来源），所以这一档从未真跑过 eslint；
  解析器是对着 `stylish` 的**真实输出形状**测的，`npm run lint` 那条路只有 stub 级覆盖。
  **规则集与阈值都还是空的**——那要等实测存量数字再定。

### 5.27 Wave34：eslint 真落地（实测三个候选，把规则集与阈值都定下来）

Wave33 只搭了"只报不卡"的骨架。这一轮把 eslint 真装上、量准、落进配置，并**切成卡**。

- **三个候选的实测**（eslint 10.12.0，`frontend/src` 129 文件 / 39352 行）：

  | 配置 | 总条数 | error | warning | 有问题的文件 |
  |---|---|---|---|---|
  | A `ts recommended`（只 `.ts`） | 7 | 7 | 0 | 5 / 71 |
  | B `+ vue/recommended` | **7413** | 34 | **7379** | 62 / 129 |
  | C `+ vue/essential` | **34** | 34 | 0 | 18 / 129 |
  | C + 三条豁免（**采用**） | **15** | 15 | 0 | 14 / 129 |

  B → C 差的那 7379 条**全是排版规则**（`max-attributes-per-line` 4079、
  `singleline-html-element-content-newline` 1758、`html-indent` 976…）——那是 Prettier 的活。
  用 B 会把真问题埋掉，所以用 C。
- **三条豁免**（都是实测出来的"刻意写法"，不是疏忽）：
  `no-unused-vars` 放行 `_` 前缀（`_ok` / `_e` 是故意从 rest 里摘掉的）；
  `no-explicit-any` 关掉（`window as any` 是浏览器 API 探测的标准写法）；
  `no-empty` 开 `allowEmptyCatch`（隐私模式下 sessionStorage 写失败不该炸 UI）。
- **中途踩到两个坑，都写进了配置注释**：
  ① **漏了 `parserOptions.parser`** → `vue-eslint-parser` 用纯 JS parser 去解
  `<script lang="ts">`，实测 **57 个 `.vue` 全部报 `Parsing error: The keyword
  'interface' is reserved`——而它们一行都没被检查**。识别线索就是那句 `interface is
  reserved`。这是典型的假干净：绿，但盲。
  ② **eslint 9 已 EOL**（装的时候 npm 提示 `9.39.5 no longer supported`），而
  `@eslint/js@10` 要求 `eslint@^10` —— 我一开始只钉 `eslint@9`，撞了一次 ERESOLVE。
  **两个必须成对钉**（`eslint` + `@eslint/js`），这是 `package.json` 里的注释。
  最后量了 **eslint 10 与 9 的数字完全一致**（同 15 条、同 5 条规则、同 14 个文件），
  所以版本之争没有代价 —— 用 10。
- **这一轮我自己犯的两个错，都很贵**：
  ① **Wave33 的骨架本身是假干净的样本**。我只想了"没配要明说没跑"，没想到
     "**配了但跑挂了**"：eslint 没装 → `npm run lint` 报 `'eslint' is not
     recognized`、退出码 1 → `parse_eslint` 解析出 0 条 → 我只加了条 note →
     报告顶部照样写「**没有发现问题**（已跑：frontend）」。这比没做 lint 更坏，
     它让工具**看起来在管**。现在：退出码非 0 而一条都没解析出来 = **error**，
     工具变红，报告首行就是「诊断工具报错」。
  ② **我把"探针里能跑"和"真实配置里能跑"混了**。那份 15 条是在 `.tmp/lintprobe`
     的配置里量的，我却当结论；落地到 `frontend/eslint.config.mjs` 之后，
     `npm run lint` 在 `frontend/node_modules` 里根本找不到 eslint。**探针与真实配置的
     差距只能靠真跑一次工具发现**，不是靠推理。
- **15 条 error 逐条判读**（结论：多数是死代码，但有两类不同）：
  - 死 import，可删、零风险：`App.vue:3`（`Teleport`）、`agent.ts:2`（`withProject`）、
    `session.ts:2`（`request`）、`AskApp.vue:5`（`watch`）、`AskApp.vue:22`（`AskPrefs`）
  - **"算出来了/定义了没人用"** —— 像没接完的接线，要人判断：
    `AskSidebar.vue:48`（`residentNames` 全文件只出现 1 次 = 定义处，确实是死代码）、
    `SemanticLocateBar.vue:47`、`SceneCanvas.vue:620`、`RelationGraph.vue:192`
  - **`frame.src = frame.src`（`workflowPreviewCapture.ts:54`）不是 bug** —— 上方注释写着
    "Reload the displayed frame and wait for its new document"，用自赋值强制 iframe 重载
    （`src` 不变浏览器不会重新加载）。eslint 只看到"自己赋自己"，看不到这个意图。
    **这是"lint 报 ≠ 是 bug"的第一例**，也说明内联豁免比全局关规则合适。
  - **`CodeView.vue:181` 在 `watch` 里 `props.tab.dirty = ...`** —— 改 prop 在 Vue 里
    不会触发更新，**大概率是 bug**，但我没有独立证据，**只报不改**。
  - 风格 1 条：`Icon.vue` 单字组件名（`vue/multi-word-component-names`）
  - 安全 1 条：`useMessageRender.ts:31` `let` → `const`（那个变量 `.set()` 但从不重赋值）
- **端到端实测**（eslint 真装上之后）：`dev_diagnostics` 报 **15 条**、格式正确
  （`文件 → 行:列 error 规则 消息 [eslint]`）、`ok=True`；门禁整集 **28/28**；
  诊断相关 **66 passed**。
- **边界**：这些结论都来自**这台机器 + 这个 eslint 版本**；换规则集或升级插件都要重跑一次。
  15 条里有 12 条在**别人的业务代码**里，未代改；`dev_diagnostics` 的 lint 这一腿
  现在**参与判定**，所以那 15 条会一直报到有人处理为止。

## 6. 已知缺口与建议下一步


| # | 缺口 | 实测依据 | 建议 |
|---|---|---|---|
| 1 | ~~`.vue` 接线层无组件级守卫~~ **已做（Wave16 `dev_vue`，零新依赖）**；~~深链到具体路由/文件的整页断言~~ **已做（Wave24，§5.18）** | vitest 那条量化过不走：1 critical + 3 moderate + 1 high dev 公告，且 vitest 3 要 vite 6/7 而本树 vite 5。改走"仓库自带的 compiler-sfc + esbuild + 真浏览器点击" | 仍**没有**的是：真模型往返（`dev_apptest` 明确不点模型）、`dev_apptest` 上的问答回合（§5.18 ② 那条只在 HTTP 层做了，页面那一层还没走）|
| 2 | ~~没有"跑测试→读失败→只重跑失败"的结构化工具~~ **已做（Wave15 `dev_testrun`）**；~~覆盖率入口仍无~~ **已做（Wave21 `coverage: true`，§5.12）** | 早先实测 `.venv` 里 `coverage`/`pytest_cov` 都不存在；`.pytest_cache` 在本工作树是 Permission denied，`--lf` 不可用 | Wave21 已加 `coverage>=7,<8` 并真跑。仍**没有**的是：分支覆盖率、`--source` 的配置化、以及给"改动行"本身（而不只是改动文件）的覆盖判定 |
| 3a | ~~真 ws 回合零自动覆盖~~ **已做（Wave19：`scripted` 适配器 + `dev_apptest stream`）** | 页面 + 麦克风 + `/api/vision/live-stream` 以前只有人对着说话才能验 | 厂商事件、打断语义、听感仍归 R12 的人跑 |
| 3 | 交付环"真网络"从未验证 | CI 新增的 pytest lane 与 dev_eval 门**从未在 GitHub 真跑过**（ubuntu 行为未证）。2026-10-03 实测：git ls-remote 也连不上了（§2 记的是 10-02 可通），网络侧完全不可达 | ① 已做首跑排障（Wave31，§5.25）：预检脚本抓出**一条确定会红**（启动DocMind.bat 的 blob 是纯 LF，那条 CRLF 测试现在与本地就红）；② 仍需人 push 一次把首跑结果记回本文件；③ **已收尾（Wave32）**：那条 CRLF 断言按内容收窄成「用到 goto 标签的才要求 CRLF」（实测在本机复现不出原注释说的机制），并新增常驻守卫 	ests/test_repo_conventions.py 盯 .py 的 BOM 与「守卫自己有没有瞎」 |
| 4 | ~~`dev_serve` 端口被抢不自动重试~~ **已换成可证的归属判定（Wave30，§5.24）** | 字面那条做不到：`dev_serve` 的 `argv` 里**没有端口参数**（`tools.py` 的 `_shell_argv`），端口是子进程自己选的。顺带挖到一个**确定性假绿**：用户给的端口上已有别人的服务时，`probe` 成功就被认成自己的服务并把那个 URL 交回去 | 已改：先认日志里它自报的端口（可证），且启动前就问"这个端口有没有别人在听"，是别人的就不判就绪；`parse_port_hint` 改成取**最后一个**地址行。**仍缺**：真正的端口重试要能往 argv 注入端口（各家 flag 不同），是产品决策 |
| 5 | ~~前端无 eslint 配置~~ **已修（Wave34，§5.27）** | 实测三个候选：`vue/recommended` 7413 条（7379 是排版）、`vue/essential` 34 条、+三条豁免 **15 条** | 已落地 `frontend/eslint.config.mjs` + `lint` 脚本，eslint 10；`dev_diagnostics` 那一腿**已改成参与判定**。**仍缺**：15 条里 12 条在别人的业务代码（5 条死 import、6 条「定义了没人用」、1 条改 prop 疑 bug），要那条 lane 自己收 |
| 6 | 连接器 UI 未接 `inline_tools` 开关 | Wave10 未做项 | 小改，UI 侧 |
| 7 | 一条计时断言只有 0.12s 余量 | `tests/test_orchestrator.py` 的 `elapsed < 0.32`，负载高时必红（属别人文件，未代改） | 改成相对余量（sleep 拉到 1.0s、断言 <1.6s） |
| 8 | ~~依赖声明无人对账~~ **已做（Wave21 `dev_deps`，§5.14）**；它抓到的 11 个真问题**已在 2026-10-03 补进 `requirements.txt`**（`WB-parallel` lane） | `api.py` 直接 import `starlette`（32 处）却只声明 `fastapi`；另有 `pydantic`/`anyio`/`cryptography`/`PyYAML`/`pywin32`/`sentence-transformers`/`numpy`/`psycopg-pool` | **版本号仍留空等人填**（`dev_deps` 刻意不猜版本，追加时只写包名 + `# 来源` 尾注）。补完复扫 429 个 `.py`，`missing` 清零。**边界**：`sentence-transformers` 那行是**弱证据**——本机没装发行包，映射靠同名推断；`pywin32` 三个模块（`pythoncom`/`win32com`/`win32con`）合并成一行 |
| 9 | ~~观察能力只覆盖浏览器~~ **已做（Wave21 `dev_probe`，§5.13）** | 整段观察能力都长在浏览器上，后端/CLI 只能退回"一屏文本自己读" | 仍**没有**的是：worker/队列类长驻进程的观测（现在只有 `cli` 与 `http` 两种档）、以及"改了哪几行"级别的覆盖判定 |
| 10 | ~~交付到草稿为止~~ **已做（Wave21 `dev_handoff`，§5.15）** | `dev_propose` 停在草稿，人还要自己定分支名与 message | **有意不做**：建分支 / 开 PR / push。那是人的决定，与命令黑名单同一条纪律 |
| 11 | ~~引擎不查锚点唯一性~~ **已做（Wave23）** | `tests/mutation/engine.py` 做 `source.replace(old, new, 1)`，锚点出现两次时只改第一处。**先量影响面**：十块电池 256 条锚点里只有 3 条不唯一，全在 `battery_head_check`（`render` 里有 12 空格缩进的同名块，4 空格那份是它的子串） | 已加 `source.count(old) == 1` → 报**问题**；3 条锚点改成带上下文。**仍有**：引擎没有"按平台分层"的概念，所以非 Windows fallback 分支的变异在两个平台里必有一个红（见 §5.17） |
| 12 | ~~`run_isolated` 仍然零测试~~ **已做（Wave23）** | 它与 `spawn_isolated` 共用 `_create_job`，本次出事故的那行在两个函数里各有一份 | 已补 5 条跨平台 + 3 条 Windows-only 用例（含一条**加入失败后已起来的子进程必须被收掉**的漏进程判据），电池 19 → **25 条变异** |
| 13 | ~~`run_isolated` 的子进程输出**可能整个丢掉且不报错**~~ **已修（Wave29，§5.23）** | `text=True` 没给 `encoding`，按本机区域编码（本机 cp936）解码：子进程吐一个非 GBK 字节 → 读线程抛 `UnicodeDecodeError` → `communicate()` 把那一路返回成 **`None`**。实测中文 / 非 UTF-8 字节 / 只写 stderr 全是 `rc=0` + 空输出 | 已固定 `utf-8` + `errors="replace"`，两条分支（Windows 与 `enterprise_sandbox` 的非 Windows）共用同一套并有测试钉住。**代价**：本机真按 GBK 输出的子进程会看到少量 U+FFFD，但内容长度与结构仍可见 |
| 14 | ~~**`mock` provider 无法完成一轮问答**~~ **已修（Wave28，§5.22）** | `_mock_chat` 的「拿到观察 → 作答」分支吐**不带 `Final Answer:` 标记的裸文本**，而 `agent.py:3443` 的格式失守判定连拒两轮后发 `final(status="error")`。后果是**离线演示模式**（`mock` 是本仓唯一零网络的模型路径）同样跑不完 | 已加 `Thought:` + `Final Answer:` 前缀；`tests/test_chat_sse.py::MockProviderTests` 2 项钉住"能收尾且先调了工具" |
| 15 | ~~`dev_apptest` 的问答回合~~ **已做（Wave27 `ask: 1`，§5.21）** | HTTP 层由 §5.18 ②（注入 LLM）与 §5.20（生产 provider 路径）补上；页面那一层由 §5.21 补上——点建议按钮发一轮、按 DOM 断言答案里带着那一次独有的随机夹具名 | **仍缺**：真模型往返（`dev_apptest` 明确不点模型）、以及"页面把答案渲染对了没有"（内容正确性）——本轮只证链路通 |
| 16 | ~~**`GET /api/config` 冷启动 6.1 秒**~~ **已定位并缓解（Wave28，§5.22）** | 账算清了：`_live_capability` 4081ms（首次构造 `LLMClient` 的一次性实时探活）+ `check_ollama` 2030ms + 11ms。**根因是环境**——本机对关闭的回环端口裸 `connect()` 要 **2035ms**（正常 <1ms）。病根是 `_OLLAMA_CHECK_TTL=2.0` 与探活同量级 → 缓存几乎从不命中 | TTL 2.0 → **30.0**（同进程 A/B：隔 3 秒再打 `2047ms → 7ms`）。**仍剩**：首次冷启动 ~6.2 秒（`LLMClient` 首次构造的一次性探活），要再降得让 `check_ollama` 与 `probe_ollama_context` 共用一次探活——跨 `api.py`+`llm.py`，未做 |

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
  第二档那 3 条真检出用例是主要成本；`bat_guard` 35/35 只用 4 秒——纯文本判据便宜到没借口不跑全）。
  那 1 条活下来在 `test_run` 里，**不是我这轮的事**：别人未提交的 `test_run.py`（他们的
  `coverage:` 档）让一条变异变成等价变异；同一块电池在**干净检出的 HEAD** 上重跑是 18/18，
  `head_check`+`bat_guard` 在 HEAD 上重跑是 79/79（467 秒）。全量在 HEAD 工作树合起来
  **2165 passed / 64 skipped / 348 秒**；带浏览器
  `--only vue,app_test` **72/72，99 秒**。
- **怎么读这份证据**：电池绿＝"这些测试确实会红"。它**不**保证变异集合覆盖了这个模块的每个守卫；
  新加一条守卫时同时加一条变异，否则那条守卫没有凭据。

## 8. 文件地图（本程序新增，均在 `agent_runtime/` 除注明外）

`code_intel.py`(扩展) · `ts_index.py` · `diagnostics.py` · `patch_apply.py` · `lanes.py` ·
`dev_server.py` · `mcp_server.py` · `mcp_bridge.py` · `dev_eval.py` · `proposal.py` · `ci_status.py` ·
`media_fixture.py` · `page_action.py` · `voice_loop.py` · `head_check.py` · `test_run.py` ·
`vue_component.py` · `app_test.py` · `realtime_scripted.py` · `bat_guard.py` ·
**Wave21 新增**：`deps_sync.py`（依赖对账，§5.14）· `svc_probe.py`（后端/CLI 观察，§5.13）·
`code_coverage.py`（覆盖率，§5.12）· `handoff.py`（交付决策材料，§5.15）·
（另有 `tests/mutation/engine.py` + `run_all.py` + `battery_*.py`，条数下限写在每块电池自己的 `MINIMUM_MUTATIONS` 里）·
`visual_acceptance.py`（被抽出 `browser_session` / `serve_static` / `stop_static` / `sanitize_flags`，
并被别的 lane 加了作业对象回收）· 测试：`test_dev_glob_git_log` / `test_lanes_registry` /
`test_ts_index` / `test_dev_diagnostics` / `test_dev_patch_move` / `test_mcp_server_expose` /
`test_mcp_inline_tools` / `test_dev_serve_preview` / `test_dev_eval` / `test_dev_propose_ci` /
`test_media_fixture` / `test_page_action` / `test_voice_loop` / `test_frontend_node_tests` /
`test_head_check` / `test_test_run` / `test_vue_component` / `test_app_test` / `test_log_view` / `test_mutation_batteries` / `test_realtime_scripted` / `test_bat_guard` ·
**Wave21 新增测试**：`test_deps_sync`（31）· `test_svc_probe`（36）· `test_code_coverage`（31）· `test_handoff`（33）·
电池（Wave18 入库，可重跑的凭据）：`tests/mutation/battery_*.py` = vue / app_test / head_check /
test_run / voice_loop / **bat_guard** / **page_action** / **log_view** / **job_logs**；真日志样本夹具
`tests/fixtures/uvicorn-access.txt`（**刻意 .txt**——`.gitignore` 里有一条 `*.log`，
叫 `.log` 的夹具进不了仓库，新检出上那批用例就会静默跳过而全量仍显示绿灯）。
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
- **一条变异把整块电池崩掉，而我差点把"崩掉"报成"全抓"**：Wave21b 里我写了条只替换半句的变异，
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
- **电池接错线：`BIND` 写错时，纯层看的是真模块**（Wave24 实测）。`battery_chat_sse` 的
  `BIND` 我写了 `"cs"`，而测试文件里的模块级名字是 `chat_sse`——引擎的
  `setattr(测试模块, "cs", 变异副本)` 只是新建了一个**没人用**的属性，于是纯层从头到尾看的
  是真模块，**11 条变异全部"活下来"**。看着像守卫没牙，其实是电池接错了线；那 9 条被抓住的
  全是**门禁层**抓的（门禁题在函数体里 `from agent_runtime import chat_sse`，拿到的是
  `sys.modules` 里的变异副本）。**教训**：报"活下来"时，先怀疑**接线**（BIND 对不对、
  锚点唯一不唯一），再怀疑守卫。
- **夹具形状抄错，等于整块纯层没跑**（Wave24，同一个电池的另一半）。`VerdictTableTests`
  起初把 `action` 事件写成 `{"tool": "search_assets"}`，而**真实**载荷的工具名在
  `text` 里（`"search_assets(像素风角色…)"`）。于是纯层压根没走到真实代码路径。
  **教训**：写判据夹具之前，先把**一次真跑**的事件 dump 出来照抄——本会话就是这么发现
  `_tool_name` 读错字段（永远 `None`）的，而那条"没调联网工具"的判据因此**恒真**。
- **恒真判据的第三种形态：参数缺省让判据结构上不可能为假**（Wave24，两条同型）。
  ① 反向对照（"不存在的路径不许渲染出任何一页"）那一行传了空的 `foreign`，`any([])` 恒为
  `False`；② 深链判据里读不到行时 `rows.get(path) or {}` 给出空 dict，于是"没数据"被当成
  "没关系"。两处的修法一样：**让它必须真的数过东西**（前者要求数三页的标记，后者加一条
  "该读的路径都读了"）。对照 §9 上面那条"判据可以结构上永远为假"。
- **抽成纯函数是把"只有现场能验"变成"CI 能验"的最省事办法**（Wave24）。"未知路径的 404
  不该算进 console 干净"这条判断本来埋在 `_drive_deep_link` 里，只有真浏览器能验；抽成
  `_console_matters(spec)` 之后它和别的判据一样在纯层就能被变异抓住。**边界**：抽出来的是
  **判断**，不是"这条判断在真页面上对不对"——后者仍归现场档。
- **"能应答"证明不了"应答的是谁"**（Wave30 实测，确定性复现）。`dev_serve` 传的 `argv`
  里没有端口参数（`tools.py` 的 `_shell_argv(command)`），所以 `port:` 只是"优先去探这里"。
  于是用户给的端口上若已有别人的服务，`probe` 成功、`start()` 就把**别人的服务**当成自己的
  报就绪并交出那个 URL——后面所有整页断言都打在别人的服务上，全程无任何错误。
  修法把归属变成**可证**的：先认日志里它自己打印的端口（是本进程打印的），
  并在**启动之前**问"这个端口有没有别人在听"。**教训**：探活类判据要问的从来不是
  "有东西在应答吗"，而是"应答的那个是不是**我起的**"；后者需要一处**事前**的观察，
  事后看是补不回来的。
- **防御性重复检查会造出等价变异**（Wave30 实测）。电池里"去掉 `if not os.path.isdir(workdir)`"
  这条必然活下来——因为 `process_runner.start_job` 自己也会拒绝不存在的目录、文案几乎一样，
  去掉哪一层行为都不变。**教训**：写变异前先问"这两层检查是不是在做同一件事"；
  是的话，去掉任何一层都等价，那条变异测不出任何东西（换成"报错要说是哪个目录"才可区分）。
- **重试之间不给资源回收的时间，等于把一次失败放大成多次**（Wave30 实测）。整页现场那档
  连跑时深链稳定红、单跑绿，逐场景连跑查出来是**交替**失败（1/2/5/7 坏、3/6 好），
  而 `live_pages` 每次都是空的——会话没泄漏，是**浏览器进程**还没退出；`close_page`
  只从 `_sessions` 摘记录、Edge 退出是异步的，而重试只 `sleep(1.0)`。改成每次失败先
  `close_all()` 再按次数退避（2s/4s），交替失败消失。**教训**：`sleep(1)` 不是"重试的间隔"，
  它是" hope 资源够了"；要退避就得按次数放大，而且先把上一个真的收掉。
- **`returncode=0` 配空输出，是最难读的一种"失败"**（Wave29 实测）。`text=True` 而不给
  `encoding` 时按本机区域编码解码（本机 cp936），子进程吐一个非 GBK 字节就让 `Popen` 的读
  线程抛 `UnicodeDecodeError`，而 `communicate()` 把那一路返回成 **`None`**——中文、非 UTF-8
  字节、只写 stderr，全都得到 `rc=0` + 空输出，**唯一线索是一个打印到控制台、调用方永远
  看不见的后台线程异常**。查了两个调用方确认今天**没有**"静默成功"
  （`generated_adapter_runtime` 的 `json.loads('')` 会抛），真实危害是**错误信息误导** +
  **排查困难**。**教训**：`text=True` 是个**隐式决策**（按谁的编码？），不是"打开文本模式"
  这种无害的写法；凡是跨进程收文本的地方，编码必须是**显式常量**加**显式的错误策略**，
  而且两条分支（Windows / 非 Windows）必须同一套，否则跨平台现象不一致。
- **"看着好"的变异可能根本装不进模块**（Wave29 自己踩的）。把
  `SUBPROCESS_ENCODING = "utf-8"` 变异成 `locale.getpreferredencoding(False)` 语义上很到位，
  但那个模块**没有 import locale**——变异后模块装不进。按 §9 的规矩，装不进来的代码
  证明不了任何判据有牙（引擎把它记成"这块电池的问题"而不是"抓住"）。
  改成 `= "ascii"`：同样表达"固定 vs 推断"，而且装得进。**教训**：写变异时先确认
  替换后的模块**编译得过**——引擎会替你报，但那时你已经浪费了一轮。
- **缓存窗口小于操作耗时，等于没有缓存**（Wave28 实测）。`_OLLAMA_CHECK_TTL = 2.0`，
  而一次探活本身就要 ~2 秒、HTTP 超时写死 3 秒 —— 一个"看着像个合理的新鲜度"的数字，
  实际让缓存在冷路径上**几乎从不命中**（`ts` 还是在探活结束之后才写的）。原注释里作者
  其实已经记下"Windows 上一次被拒也要 ~2s"，但还是选了 2s，因为判断依据是
  **业务上多新鲜算新鲜**，而不是**这个操作要多久**。**教训**：给一个昂贵操作设缓存时，
  先量它的耗时，再问"窗口比它大吗"——TTL 的量纲是**秒**，参照物必须包含操作本身的耗时，
  否则那行代码读起来像优化、实际是装饰。
- **"2 秒的拒绝"不是 2 秒的逻辑，是 2 秒的环境**（Wave28）。追一条 6 秒的冷启动，
  逐项计时只凑出 2 秒，剩下 4 秒怎么都对不上；最后量**最底下那一层**才发现：本机对
  **关闭的回环端口**做裸 `socket.connect("127.0.0.1", 11434)` 要 **2035ms**
  （正常应 <1ms，`ECONNREFUSED` 立刻返回）。**教训**：把"某次调用很慢"归因到代码之前，
  先在**最底层的原语**上量一次——`connect`/`open`/`stat` 这些在别的机器上是微秒级的操作，
  在这台机器上可能有三个数量级的差异，而应用层的每一层都在替它背锅。
- **测试双要照契约活，不是照脚本活**（Wave28，缺口 14 的另一面）。`mock` provider 的
  「作答」那一支漏了 `Final Answer:` 标记，于是被 `agent.py:3443` 的格式失守判定连拒两轮
  ——**它不是"答得不好"，是根本没走完契约要求的形状**。同型的还有一批：另两支（素材/知识库）
  都带了 `Thought:` 与标记，只有这支漏了，而它偏偏是本仓**唯一零网络**的模型路径
  （离线演示模式）。**教训**：一个 provider 的五条分支里，四条对、一条不对，
  在"默认走哪条"之前谁都不会发现。
- **凭据不能按"位置"挑，要按"它是什么"挑**（Wave27，同一条实测错两次）。
  页面那一档要让桩引述工具输出里的一行当凭据。第一版取**首行**——`list_dir` 的首行是
  表头（`目录 (代码库根) 共 1 项：`），凭据（随机文件名）在第二行；改成取**末行**——观察的
  末行常常是 **harness 的提示**（`（请基于观察继续，或给出 Final Answer）`），于是 final 成了
  「根据检索到的内容，（请基于观察继续，或给出 Final Answer）—— 以上就是可用清单。」，
  凭据换成了一句套话。正解是**按动作找那类工具的输出行**（`_LINE_HINTS`：`list_dir` 看
  `[FILE]`/`[DIR]`，`search_assets` 看 `·`）。**教训**：从一段文本里"取一行当代表"时，
  先问"这一行是谁写的"——同一个字符串里可能混着工具输出、表头和 harness 提示。
- **把 `open_page` 写在 `try` 外面，等于把失败原因也写在报告外面**（Wave27 实测）。
  `_drive_ask` 起初是"先 `session = open_page(...)`，再 `try:`"——它一抛（问答页资源重，
  实测会顶破 `open_page` 那 6 秒就绪预算）`finally` 不执行，`ask_credential` 从没被设上，
  报告里只剩一行"凭据: None"，连是超时还是没挂上都看不出来。**教训**：凡是"失败时要留下
  线索"的东西，必须在 `try` **里面**——`finally` 只保护它包住的那一段。
- **桩要听懂被测方的纠正提示，否则会空转**（Wave27 实测）。`llm_stub` 起初不认识
  `Evidence check:` / `Reflection:` / `Nudge:`，于是一轮问答跑了 **6 轮**（一直重复
  `Action: list_dir`），最后靠"模型反复空转"的兜底停——页面渲染出来的是**兜底证据块**
  而不是一条干净的答复。让桩在这三类提示出现时就着已有证据收尾之后，一轮降到 **3 轮**。
  **教训**：测试双要照**契约**活，不是照"脚本演完"活；被测方发回来的纠正文案本身就是契约的一部分。
- **共享工作树的索引是共享的：`git add <我的路径>` 不会让索引里只有我的路径**（Wave26 实测，
  比 `b828d10` 那次更值得记）。我 `git add` 只点名 4 个路径，可另一条 lane 在他们那边
  `git add` 落进了**同一个 `.git/index`**——暂存区里于是多出 19 个别人的文件
  （`pricing.py` / `llm.py` / `tools.py` / `plan_state.py` / 十几个别人的 `tests/test_*.py`），
  裸 `git commit` 会把约 3600 行挂到我的提交上。而且它是**持续**的：检查途中 `agent.py`
  又冒出来一个。**所以"暂存完裸 commit"这套流程的前提——索引只归我一个人——在共享工作树里
  不成立。**
  **修法**：改用**私有索引**——`GIT_INDEX_FILE=.tmp/<name>.index`，`git read-tree HEAD` 起，
  只放自己的 blob，再 `git commit -F`。既不碰共享索引（别人的暂存原样保留），也没有竞态窗口。
- **自检要查"有没有多出来的"，不能只查"我的在不在"**（Wave26，同一个事故的另一半）。
  我原来的自检逐条断言"我的标记在暂存版里"，全绿；而索引里躺着别人 19 个文件它一个字都没说。
  **教训**：一个只问"我的东西在不在"的检查，对"多了不该有的东西"是**盲的**——
  而那正是这个仓库最贵的那类事故。现在自检断言"索引里恰好只有我的文件"。
- **拿工作树的文件去 `hash-object` 之前，先确认它相对 HEAD 只差我的那几行**（Wave26 我自己
  犯的）。重建 blob 那一步我把**工作树里的** `docs/HANDOFF-vibecoding.md` 喂给了
  `hash-object`（本该喂重建后的那份），于是把别人 154 行 Wave21 的在途文档一起提交了
  ——**`b828d10` 那个形状，方向相反**。按用户口径保留不改历史、如实披露（见 `HANDOFF.md`）。
  **机械护栏**：提交前断言"这份文件相对 HEAD 的差异里不含别人的标记"（`### 5.12` /
  `dev_deps` / `dev_probe` …），命中就拒绝提交——就是把这次的事故变成一条会红的东西。
- **在"所有消息里搜某个词"之前，先问"提示词里有没有这个词"**（Wave26 实测）。
  `llm_stub` 的 `observation_snippet` 起初在**所有**消息里找 `Observation:`，而**系统提示
  里本身就有一大段解释 ReAct 格式的文字**（含这个词）。后果不是报错，而是**行为悄悄变了**：
  桩以为已经拿到工具结果，第一轮就直接作答、**一个工具都不调**——判据报的是
  `at_least_one_tool_action` 红，看起来像 Agent 没调工具，其实是桩读错了消息。
  修法是窄化到"只看最后一条"（`_mock_chat` 就是这么做的，`llm.py:1108`）。
  **教训**：从消息数组里提取信息时，先把"哪些角色/位置的消息可能含这个词"想一遍；
  提示词是**最容易被误读成数据**的那一类。
- **等价变异之外的一种"白写"：正则按错误的形状写**（Wave26）。`tool_name_in` 起初按
  **事件**形状写（`name(args)`），而它要解析的是 **ReAct 正文**（`Action: search_assets`）
  ——于是永远返回空串，用它做断言的那条测试恒红（而不是恒绿，所以它至少会叫）。
  **教训**：解析函数要跟"它实际拿到的那份文本"对齐——先 dump 一次真输入再写正则。
- **"红了三次"不等于"坏了"——先量再下结论**（Wave25 我自己犯的）。真浏览器那一档连红三次
  之后，我写下"本机浏览器那一档现在是坏的"，并据此把它从电池的 LIVE 名单里摘掉了。
  实际一量：浏览器好得很——`open_page` 在 **1903 ms** 返回、文件树到 **5160 ms** 才出现，
  夹具在"文档加载完"之后**立刻**读数据，就是在跟中间那 3.3 秒赛跑。
  **教训**：三次同样的红只说明"这件事稳定地错"，**不说明错在哪**；"坏"是个需要证据的
  结论，而不是"红了几次"的推论。这个仓的规矩本来就有"红了先在同一环境重跑一次"，
  但重跑只够排除抖动——要定位就得**把中间量出来**（这里是时间线：`open_page` 何时返回、
  数据何时到位）。
- **"客户端取消"与"真失败"是两件事，分开归类而不是丢掉**（Wave25）。工作台的存活探针
  `probeBackend()` 自带 `AbortController` 并在 `catch` 里故意吞掉，所以页面加载慢时那条
  `/api/health` 是**前端自己掐的**（Chrome 报 `net::ERR_ABORTED`）。把它算成"请求失败"
  会让 `no_failed_requests` 在慢加载时**假红**——而假红和不报一样有害，它教人忽略门禁。
  修法是**分类**：两堆都进报告（`failed_requests` / `cancelled_requests`），判据只对前者变红。
  **代价要写明**：被客户端掐断的**真实**失败也可能落进 `cancelled_requests`，所以它必须
  上报、不能静默丢。
- **等待不是没有，是放错了地方**（Wave25）。`_drive` 里本来就有一个
  `wait_for(".wb-shell", timeout=5.0)`，但它写在**读 facts 之后**——于是"等页面就绪"
  这件事只对后面的 console 采集生效，对 `rows` 那条判据完全没生效。
  **教训**：加等待时先问"我下面要读的东西，是等到哪个条件才出现的"；等错了对象等于没等。
- **等价变异的第四种来源：协议形状下根本区分不出来**（Wave24）。`if not line.startswith("data:")`
  → `if False:` 看着是一条好变异，但真实 SSE 里非 `data:` 行去掉前缀后都不是合法 JSON
  （`event: {...}` 会剩下 `: {...}`），所以"只认 data:"这道门在这个形状下**测不出来**。
  换成"连切片一起去掉、于是裸 JSON 行也成事件"才能区分。**教训**：写变异时问一句
  "**在真实的输入形状下**，这两个版本的行为真的不同吗"——不同才算变异。
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
- **墙钟杀掉的测试留下"像在推进"的假象，而 `unittest -q` 连半句结论都不给**
  （Wave36 `WB-parallel` 实测）。`unittest discover -q` 跑完才写汇总，被 `timeout` 杀掉时
  输出的只有 `ResourceWarning` 之类的东西——**没有 `Ran N tests`**。我盯着输出文件的
  字节数从 4686 → 9106 → 58679 一路涨，就判"它在推进、只是慢"，
  **全是假的**：那些字节是它路过的模块打的告警，不是进度。
  本仓 `tests/test_*.py` 有 **243 个文件**，其中 `test_preview_adapter_tools.py` 一项
  **13 个测试只跑 2.81 秒、进程占用 44.09 秒**（用户态 0.22s + 系统态 0.61s，差额在等后端起停）；
  前 45 个文件单跑一趟就是 **22 分 35 秒**仍未结束。**教训**：
  ① 长跑测试**必须**把输出重定向到文件**并且**看 `Ran N tests` / 退出码，
     **字节数在涨不等于有进展**；② 判断"慢"还是"卡"，比 `user+sys` CPU 时间有用得多
     ——本例 44 秒里 CPU 只占 0.8 秒，剩下全在 I/O 等待（起进程），
     这个数一眼就能分开"在算"和"在等"；③ 跑不完就分批，
     别用"延长墙钟"当解决方案，那是把问题推到下一个人身上。

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
3. 全量最后一次：**2569 passed / 8 failed**，8 条已用干净 worktree 证明全部来自
   另一条 lane 未提交的 step-budget / capacity WIP（`test_orchestrator.py` 那条既有红
   在 `e0d182d` 修掉 game_workflow 那 4 项后浮出，实测为 HEAD 既有红；本程序不代修、不代提交）。
3b. **本会话真实跑一次工作流挖出的缺陷**（`godot_sample`，把 `values/level.gd` 的
   `HP_GROWTH` 0.12 → 0.15；修掉 7 个，剩 1 个未定位）：
   - **子代理无限挂死**：`MAX_AGENT_STEPS` / `SUBAGENT_MAX_STEPS` / `SUBAGENT_STEPS_HARD_CAP`
     三处默认全 0，而 `_run` 的 `while True:` 唯一上界是 `tool_step_limit > 0` → 结构上无界。
     假模型每轮换参数 + 换措辞，`executed` 与 `_repeated_sentence` 两道防空转都拦不住。
     **实测依据**：单跑 `test_orchestrator.py` 超 8 分钟 CPU 持续增长不返回；修好后 3.13 秒跑完。
     **状态**：已修（`SUBAGENT_STEP_FLOOR=200`）。
   - **挂死掩盖了一条既有红**：`test_orchestrator.py::test_planner_role_can_decompose_before_execution_role`
     此前一直没人看得见，因为同文件里另一个测试会无限挂死，整个文件跑不完。挂死修好后它才浮出来，
     已用 worktree 在 HEAD 上复验为既有红（断言要"子代理结论"，实际拿到一段能力说明文本）。
     **状态**：已修挂死；红未代改。
   - **「重试该成员」100% 报 500**：`workflow_callbacks` 返回 `{runner, synth_runner, replanner}`，
     端点三个一起 splat，而 `retry_subagent` 签名里没有 `replanner`。**无任何测试覆盖该端点**。
     **实测依据**：真点按钮必 500，前端显示「响应不是合法JSON」。
     **状态**：已修（签名加 `replanner=None`）。
   - **「实际工具调用」恒 0 条**：面板按 `before_tool/after_tool/…` 从持久事件数组过滤，
     而工具事件只进子代理本地 hook 缓冲，从不落工作流事件流（实测那条 trace 43 条事件里工具事件为 0）。
     **实测依据**：同一屏的任务面板里明明列着 read_file / grep / python_exec，但工具面板显示 0。
     **状态**：已修（`record_agent_step` 转发工具事件到持久流）。
   - **hook 缓冲配额被工具事件吃光**：`if len(hook_events) >= 48: return` 在最前面；
     每次工具调用发 before+after 两条 → **24 次调用就耗尽**。
     **实测依据**：trace 里工具事件在 24 次调用后突然消失。
     **状态**：已修（配额检查挪到最后）。
   - **子代理失败只存标签、丢掉堆栈**：磁盘上只有 `error = AttributeError: 'NoneType' object has no attribute 'strip'`，
     无堆栈 → 只能全文撒网猜 `.strip()` 在哪，**猜错两次**。
     **实测依据**：`impl_update_hp_growth` 任务失败时错误信息只有这一行。
     **状态**：已修（`_exc_traceback` 捕获完整堆栈）。
   - **多步 DAG 静默塌成根任务**：`len(clean_deps) > max_tasks` 拿依赖个数比任务总数上限，
     而 `max_tasks=0` 的约定是"不限" → 默认配置下任何声明 `depends_on` 的任务都被静默丢弃，
     5 步只剩 1 步，全程零报错。
     **实测依据**：输入 5 步计划（survey → implement → testrun → fix → report），落库只剩 `['survey']`。
     **状态**：已修（比较对象改成 `_MAX_TASK_DEPS`）。
   - **反思判据结构上只能判"结论复述了吗"**：`traj` 传进来了但 prompt 里只有
     「步骤数：N；失败步骤：M」，**观察一个字都没给** → 判据不知道观察里有 `WRITE_OK: True`，
     于是三次正确的字节级写入被连续判失败、逼出两轮 replan。
     **实测依据**：`wf-f3da9d3ffb32` 的 trace 里三次正确写入都被判失败。
     **状态**：已修（`_traj_evidence_block` 压成有界证据块喂进 prompt）。
   - **空响应崩成 AttributeError**：`llm.py` 的 `getattr(msg, "content", None)` 在 `content=None` 时原样返回；
     有个测试把它钉成正确行为（对 tool-call-only 消息确实对），缺的是 agent 侧没区分
     "带 tool_calls 的 None"（正常）与"不带 tool_calls 的 None"（空响应）。
     **实测依据**：`acc.strip()` 当字符串用，而 `acc` 可能是 None。
     **状态**：已修（改 agent 不改 llm，归一成空串）。
   - **试做区隔离疑似失效**：子代理用相对路径 `open("values/level.gd","wb")` 写入，
     事后核对：真实项目 mtime 22:53:12 已改、试做区 workspace/baseline 仍是 9-11 的原始 mtime。
     已排除三种猜测（scope 机制正常、是 contextvar 不会跨线程、并行批次已做 context 复制）。
     **实测依据**：mtime 对比；`python_exec` 现在会在观察里自报实际执行目录。
     **状态**：**未定位**，只加了诊断（下次真跑一次工作流就会给出答案）。
   - **全程 98% 静默**：那次 360 秒里相邻事件间隔 ≥5s 的总时长 354 秒；**每段静默都是一次模型调用**。
     **实测依据**：时间线分析；事件流里此前完全没有对应标记。
     **状态**：已修（`llm_wait_start/end` 配对事件）。
   - **时间线六成是英文**：实测那次发出 27 种 kind，前端 `timelineLabel` 只覆盖 20 种，
     包括最该看懂的 `graph_replan_start`。
     **实测依据**：从真实 trace 里取 kind 集合比对前端标签表。
     **状态**：已修（补 16 个中文标签 + 跨层一致性测试）。
4. 需要人做的两件：**push**（或 `git bundle` 离线备份），以及在配好 DashScope 的机器上跑一次
   真网关回路，把结果回写本文件 §5.3 与 §6#3。push 之前顺手跑一次
   `dev_headcheck` 带 `gate: 1`（约 1 分钟）：CI 看到的是那棵树，不是这棵树。
5. **Wave20 的门抓到过、也被修掉了**：那两处（`run_desktop.bat` 与 `installer/uninstall.bat`
   里的 `"%~dp0"`）在 2026-10-02 当天由各自那条 lane 改成 `"%~dp0."`，本程序全程没代改一行
   别人的文件。`tests/test_bat_guard.py::RealRepoLauncherTests` 现在绿；留着它防的是下一次
   有人再写回这个形状——**它已经是有效的了，不是待办**。
   同一批账里 `dev_headcheck` 加了第二档（`gate: 1`，本会话记作 Wave21b——另一条 lane 也把
   他们的四个缺口叫作 Wave21，尚未提交；这里用 b 后缀避免撞号）：**push 之前跑它**，因为 CI 看到的
   是那棵树而不是这棵树；本会话就是靠它在事后抓到 `f7ec14e` 的行为红（见 §5.4）。
   基线 `.github/dev-eval-baseline.json` 按【我的提交】口径刷成 `23 → 24`；工作树里还有别人
   那一题（`log-view-collapses-real-spam`），所以本地刷全量会得 25——那一行不由我提交。

6. **`WB-parallel` lane（2026-10-03，四件并行）四件全部完成**。
   下面每条都写「实测依据」与「边界」，凡未实测的一律不写成结论。

   **6a. 七个e2e/live 文件全跑过一遍（任务A：只跑、只报告，没改任何产品代码）**

   | 文件 | 结果 | 墙钟 | 归属 |
   |---|---|---|---|
   | `test_live_llm_workflow` | 1 skipped | 3.34s | 环境缺：需 `DOCMIND_LLM_LIVE=1` +非 mock 的 `LLM_PROVIDER` |
   | `test_game_workflow_e2e` | 1 passed | 15.03s | 真跑（Ollama 不可用→ 降级 local 哈希向量） |
   | `test_real_project_workflow_e2e` | 1 skipped | 1.81s | 环境缺：需 `DOCMIND_REAL_GAME_E2E=1` + 真 Godot 项目与可执行文件 |
   | `test_vision_workflow_e2e` | 5 passed | 3.67s | 真跑 |
   | `test_web_research_product` | 26 passed | 1.64s | 真跑 |
   | `test_realtime_bench_tool` | 13 passed | 62.84s | 真跑 |
   | `test_postgres_workflow_integration` | 1 skipped | 2.51s | 环境缺：需 `DOCMIND_TEST_POSTGRES_DSN` |

   合计 **46 passed / 3 skipped / 0 failed / 0 收集失败 / 0 挂死**（逐文件420 秒墙钟兜底，
   驱动脚本 `.tmp/run_e2e.py`）。三条 skip 的判定条件都读过，确认是**环境缺**而非代码问题。
   **边界**：`pytest-timeout` 本机未安装，所以没有测试级超时，只有外层墙钟 + `kill`；
   另有一次「`ROOT` 少算一层」把输出全丢了（脚本在 `.tmp/` 下却按它算仓库根），
   修法是 `dirname(dirname(abspath(__file__)))`。

   **6b. 上一条线新增的守卫补了变异电池（任务B：6/48 → 40/40，0 活下来）**

   五块电池（`empty_model_turn` 4、`plan_dag` 10、`reflection_evidence` 13、
   `subagent_step_floor` 7、`workflow_events` 6）带现场层合并跑：**40/40 抓到，
   0 条本环境未验证，0 条活下来，用掉 102 秒**。修掉的**根因级**问题按危害排序：

   1. **门禁题 id 写错 → 整块电池 0 变异执行，汇总却打「0/14 抓到」**。
      `CASE_ID` 写了一个 `DEV_DATASET` 里不存在的 id，`gate_failures` 回「门禁题不存在」，
      引擎在 `if report["problems"]: return` 处直接返回。**汇总看不出「没跑」**。
      实测：改用真实存在的 id 后 0/14 → 11/14。
      **教训**：`0/14 抓到` 有两种成因（没牙 / 没开工），必须去看 `problems` 才分得清。
   2. **from-import 接线失效（plan_dag 8 条、workflow_events 12 条）**。
      测试写的是 `from agent_runtime.game_workflow import _MAX_TASK_DEPS, GameWorkflowManager, …`，
      这些名字在**导入时**就把旧模块符号绑死在测试模块上了，引擎的 `BIND='gw'` 换掉模块对象
      **没人用**——断言读的还是那批旧符号。实测纯层与现场层**全部照绿**，
      连 `SINK_TOOL_HOOKS = ()` 这种一眼可见的破坏都断不住。
      修法：引擎加 `BINDS`（复数绑定），`_bind_spec()` 优先读它。**2/10 → 10/10**。
   3. **`BINDS` 漏掉模块对象本身 → 现场层 4 条全绿**。`BINDS` 换了那三个 from-import 名字，
      但 `agent.Agent(llm=...)` 走的是**模块对象**上的属性，`agent.Agent` 仍是真模块的类，
      现场层真跑起来测的还是真代码。加 `('agent', '*')` 特例后 3/9 → 5/9。
   4. **变异与守卫错配**：reflection 前两条改的是 `_reflection_result`（调用点），
      而守卫只测 `_traj_evidence_block` 这个纯函数本身。
   5. **死代码路径**：`TrajEvidenceBlockTests` 名义在测「截断取尾部」，但它那 30 步正文只有
      **639 字符 < limit 默认 1600**，走的是 `if len(body) <= limit` **早退分支**——
      `reversed(lines)` 一次都没被调用。补了 `TruncatedEvidenceKeepsTheTailTests`（每步 300 字符
      → 正文约 9000 字符，必然截断）。
   6. **`_child_trace` 双重截断**：`record_hook` 内有 `if len(hook_events) >= 48: return`，
      而 `_child_trace` 里还有 `[:48]`。`hook_events` 的**唯一出口**就是 `_child_trace`，
      所以从 `out["trace"]["hooks"]` 观测**恒为 48**，与 `record_hook` 里配额怎么改无关。
      两条变异（「配额抬到 10\*\*9」「配额检查整个挪掉」）因此**不可验证**——
      已从电池删掉（不可验证 ≠ 活下来，也不能改写成别的形状骗抓取率），原因写进注释。
   7. **探针自己制造假结论**：`.tmp/probe_quota.py` 用 `'10**9' in m[2]` 过滤变异，
      而载荷写的是 `10 ** 9`（**带空格**）→ 列表为空 → for 循环**一次都没执行** →
      前面残留的输出被误读成「变异后仍是 48」。空循环 + 陈旧输出 = 假结论的配方。
   8. **元测试会咬掉 `BINDS` 改造**：`tests/test_mutation_batteries.py:223` 直接
      `battery.BIND`，把 `BIND` 换成 `BINDS` 就 AttributeError。两块电池因此补回一个
      兼容用的单数 `BIND`（真正生效的是 `BINDS`）。

   **6c. 层归属量出来的两条硬事实（不是结论式描述）**

   - **`plan_dag` 的 `LIVE` 初版是三元素**，而引擎 `engine.py:149` 与元测试
     `test_mutation_batteries.py:231` 都写 `class_name, cases = battery.LIVE`（恰好两元素），
     带现场层跑五块合并时崩：`ValueError: too many values to unpack (expected 2)`。
     改成两元素只让崩溃消失，**层归属本身仍是错的**：探针量到
     `TruncationMustNotBeSilentTests` 0.25s、`DagApprovalPathTests` 0.79s，
     两个类只碰 `tempfile.mkdtemp()`，**不碰浏览器/工具链/网络** → 按引擎对现场层的定义
     属纯层材料。且本电池一条 `("live",)` 标记的变异都没有，元测试
     `test_live_marks_and_live_layer_agree_both_ways` 两个方向都查，会判「现场层是装饰」直接红。
     处置：两个类移进 `UNIT_CLASSES`、`LIVE = ()`。
   - **`subagent_step_floor` 的现场层必须删掉，理由是它会**挂死**整块电池**。
     受控实验（`.tmp/probe_hang.py`，外层 40 秒墙钟，唯一差别是那一行改没改）：

     | 组 | 结果 |
     |---|---|
     | 对照组（未变异） | `用时 0.9s ran=2 红=0`，exit=0 |
     | 变异组（拆掉 `child.tool_step_override = cap if cap > 0 else SUBAGENT_STEP_FLOOR`） | 40 秒被杀，`exit=124`，一行输出都没有 |

     原因就是这个电池要治的病：`while True:` 唯一上界 `tool_step_limit > 0`
     重新变成**结构上无界**，那条 60 秒断言**根本没机会执行**——它不是「跑得慢然后红」，
     是永不返回。而引擎对变异后的模块**没有超时**（`_run_mutations` 只有纯层/门禁/现场三层，
     没有看门狗）。**现场层在这里不是慢，是死；慢还能靠超时兜，死不能。**
     同时逐条量到 7 条变异**全部在纯层 0.00 秒内就红**（`.tmp/probe_pure4.py`，
     抓它们的是 `test_the_child_gets_an_override_even_when_cap_resolves_to_zero`——
     它用 spy 替换了 `Agent.run`，**只读 `child.tool_step_override` 这个数，不跑 ReAct**）。
     处置：`LIVE = ()`，`SubagentRunTerminatesTests` 进 `LAYER_EXEMPT` 并写清实测依据。
     **边界**：它仍是一条有价值的守卫（`tests/` 里跑 pytest 会真跑一遍 0.9s），
     只是不能当**变异判据**的那一层。
   - **修完之后** `tests/test_mutation_batteries.py` **24 passed / 462 subtests** 全绿
     （修前 5 处红：2 处 `battery.BIND` AttributeError、1 处 `LIVE` 三元素只被算进第一个类名、
     2 处「声明了 LIVE 却无 live 标记变异」）。

   **6d. 依赖声明已补（任务C，版本由人定）**：`requirements.txt` **13 行纯追加、零删除**，
   补完 `dev_deps` 复扫 429 个 `.py`，`missing` 清零。三行重复的 `pywin32` 收敛成一行
   尾注。**边界**：`sentence-transformers` 是弱证据（本机未装发行包，映射靠同名）；
   **版本号一律留空**——`dev_deps` 刻意不猜版本，任务书也明确要求版本由人定。

   **6e. 未解决 / 留给下一个人**

   - `record_hook` 的配额（`>= 48`）与 `_child_trace` 的 `[:48]` **双重截断**只做了规避
     （把不可验证的两条变异移出电池），**没有改产品代码**。真要提高可观测性，
     得让 `_child_trace` 暴露「被截断了多少」而不是静默取前 48 条。
   - `SubagentRunTerminatesTests` 那道 60 秒断言在**真 hang** 时是**假的**
     （`assertLess(elapsed, 60)` 永远执行不到）。它只能证「跑得慢」，证不了「挂死」。
     真要兜住 hang，得靠 `run_isolated` 那套子进程 + 超时，而不是在同进程里计时。
   - 引擎仍**没有看门狗**：任何"变异让代码挂死"的场景都会挂到 CI 超时，
     而不是报「这条变异让测试挂住了」。这与 6c 那条是同一个洞。
   - **提交后元测试从 24 passed 退回 `4 failed / 24 passed / 458 subtests`，
     归因是另一条 lane 的 `a97f6c7`（改 `game_workflow.py` + 加新测试类），不是本 lane**。
     两条实测依据：
     ① `battery_plan_dag` 有两条锚点现在 `count=2`（引擎只替换第一处，会打错地方）——
        `诊断不剥 ``` 围栏`（锚 `re.sub(r"^```(?:json)?\s*|\s*```$", "", text,`）
        与 `诊断不认 plan.tasks 嵌套`（锚 `if inner is None and isinstance(raw.get("plan"), Mapping):`）
        都在对方新加的那段诊断函数里**出现了第二份**。
     ② `battery_workflow_events`：`tests/test_workflow_tool_events.py:219` 新增了
        `ExecutionRootIsReportedTests`，而元测试要求每个带 `test_` 的类
        必须在 `UNIT_CLASSES`/`LIVE`/`LAYER_EXEMPT` 三者之一，否则报"永远不会被拿来判变异"。
     **为什么没顺手改**：`game_workflow.py` 与 `test_workflow_tool_events.py` 都在对方
     正在动的范围里（双写者纪律）。锚点要改成"带更多上下文直到唯一"、
     新类要归到哪一层，**得由改这些文件的那条 lane 决定**（也可能他们本来就在做这件事）。
     **边界**：这两条红在对方提交落地的瞬间就存在，与本 lane 无关；
     本 lane 自己那五块电池**带现场层跑仍是 40/40 全绿**（已复跑确认）。
   - **§10 第 3 条那个全量数字在本机一趟跑不出来**（实测 `exit=124`：
     `timeout 3000` 秒墙钟被杀，**50 分钟仍未结束**）。已定位到**不是**本轮改动引入：
     本仓 `tests/test_*.py` 共 **243 个文件**，而其中
     `tests/test_preview_adapter_tools.py` 每次都真起后端做 HTTP 探活——
     **13 个测试只跑 2.81 秒，整个进程却占用 44.09 秒**（`time` 实测：用户态仅 0.22s、
     系统态 0.61s，差额全在等后端起停）。再取前 45 个文件单跑一趟：**22 分 35 秒**，
     仍未结束。**边界**：`unittest -q` 在跑完前不写汇总，被墙钟杀掉时
     **连 `Ran N tests` 那一行都没有**——输出文件里只有 `ResourceWarning` 之类的东西，
     看着像"还在推进"，其实拿不到任何结论（这一点已经骗过我一次：
     我按文件字节数在涨就以为它没卡）。正确姿势是**分批跑并把输出重定向到文件**。
     在拿到真实汇总之前，§10 第 3 条那个 `2569 passed / 8 failed` **仍然有效**——
     它是上一轮的实测值，本轮**没有**推翻它的证据，只是没能复现它。
     要根治得给那类测试做进程级后端复用（现在每个文件各起各停）。
